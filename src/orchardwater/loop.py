"""The weekly WhatsApp loop: run the engine for every block, send one message, save replies.

Commands (run `python -m orchardwater.loop <command> --help` for each):

    send            run the engine for each block of a farm and send the Monday message
    update          rerun mid-week; send a short update only if the plan changed a lot
    serve           a small web server that receives Meta's webhook (farmer replies)
    simulate-reply  pretend the farmer tapped a button or typed, to test without Meta
    replies         list the saved replies
    meta-template   print the template text to paste into Meta's template editor

Files it writes, all under --data-dir (default data/whatsapp, never committed):
    outbox.jsonl    one line per message sent (or mock-sent)
    replies.jsonl   one line per farmer reply
    state/<farm>/<block>.json   each block's saved soil state (see state.py)
"""

import argparse
import dataclasses
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from orchardwater import chatbot
from orchardwater.card import CardTemplateError
from orchardwater.farm import Block, Farm, FarmFileError, load_farm
from orchardwater.result import (
    KIND_IRRIGATE_BY_DEADLINE,
    KIND_NO_IRRIGATION_DUE,
    KIND_WEEKLY_AMOUNT,
    WeeklyResult,
)
from orchardwater.weekly import CASABLANCA_TZ, compute_weekly_result
from orchardwater.weekly_message import (
    BUTTON_IDS,
    SECTION_BY_KIND,
    MessageWording,
    block_line,
    block_plan,
    build_message,
    format_hours_minutes,
    load_wording,
    meta_template_body,
)
from orchardwater.whatsapp import (
    MODE_TEMPLATE,
    MODE_TEXT,
    CloudApiSender,
    MockSender,
    Reply,
    append_record,
    create_template,
    read_records,
    template_create_payload,
    template_payload,
    text_payload,
)

DEFAULT_DATA_DIR = Path("data") / "whatsapp"
OUTBOX_NAME = "outbox.jsonl"
REPLIES_NAME = "replies.jsonl"
# Replies a person (or later the chatbot) should answer. "ok" needs no answer.
NEEDS_ANSWER_BUTTONS = ("question", "more_info")
# Mid-week updates: only when a block's hours for a day change by at least
# this many minutes, or its line changes kind (for example "no water" becomes
# hours); and at most this many updates per farm per week, so farmers are not
# spammed. Both are guesses to tune in field trials.
UPDATE_THRESHOLD_MINUTES = 15
MAX_UPDATES_PER_WEEK = 2
KIND_WEEKLY_MESSAGE = "weekly"
KIND_UPDATE_MESSAGE = "update"

# The engine call for one block: (block, now, state file, watered as planned). A parameter
# so tests can pass a fake that needs no internet.
ComputeFunction = Callable[[Block, datetime, Path, bool], WeeklyResult]

# Engine results whose block line told the farmer to water every day until the END of
# the forecast week. Only these can be "watered as planned" by Monday: a deadline line
# (irrigate_by_deadline) only covered the days up to the deadline, so after it the soil
# dried with nothing planned.
KINDS_PLANNED_TO_WEEK_END = (KIND_WEEKLY_AMOUNT,)


def compute_block(block: Block, now: datetime, state_path: Path, watered: bool) -> WeeklyResult:
    """Run the drip engine (weekly.py) for one block, with the block's own state file.

    watered True means the farmer confirmed last week's plan with OK, so the soil
    is taken as full now (the engine's reset, the same as --reset after watering).
    This holds until the meter reader can tell real water use.
    """
    return compute_weekly_result(
        block.lat,
        block.lon,
        block.area_ha,
        block.flow_m3_per_h,
        block.efficiency,
        block.soil,
        block.ec_water_ds_m,
        now=now,
        reset=watered,
        state_path=state_path,
        monthly_kc=block.monthly_kc,
    )


def week_key(moment: datetime) -> str:
    """The ISO week a moment falls in, Casablanca time, like "2026-W45"."""
    year, week, _weekday = moment.astimezone(CASABLANCA_TZ).isocalendar()
    return f"{year}-W{week:02d}"


def already_sent(outbox: Path, farm_id: str, week: str, sender: str) -> bool:
    """True if this farm already got this week's message from this sender."""
    for record in read_records(outbox):
        if record["farm_id"] == farm_id and record["week"] == week and record["sender"] == sender:
            return True
    return False


def blocks_watered_as_planned(data_dir: Path, farm_id: str, now: datetime, sender: str) -> set[str]:
    """Blocks the farmer confirmed last week's plan for, by tapping OK. UNTESTED IN THE FIELD.

    Only counts when ALL of these hold, so an OK is never used twice or on the wrong plan:
    - the plan is the LAST message sent to this farm (same sender) in the week
      right before now's week. A skipped week means no reset: the soil kept drying.
    - the OK answers exactly that message (its send_id), not an older or newer one.
    - nothing was sent to this farm yet in now's week. A forced resend in the same
      week does not reset again: the first send already did.
    Then every block whose line said "water H:MM a day until the week's end"
    counts as watered. No OK means no block counts, so the model keeps drying
    the soil and the block ends up asking for a field check: the safe side.
    """
    this_week = week_key(now)
    last_week = week_key(now - timedelta(days=7))
    sent = [
        r
        for r in read_records(data_dir / OUTBOX_NAME)
        if r["farm_id"] == farm_id and r.get("sender") == sender
    ]
    if any(r["week"] == this_week for r in sent):
        return set()
    last_week_sent = [r for r in sent if r["week"] == last_week]
    if not last_week_sent or not last_week_sent[-1].get("send_id"):
        return set()
    plan = last_week_sent[-1]
    got_ok = any(
        reply.get("send_id") == plan["send_id"] and reply.get("button_id") == "ok"
        for reply in read_records(data_dir / REPLIES_NAME)
    )
    if not got_ok:
        return set()
    return {
        block["block_id"]
        for block in plan["blocks"]
        if block["kind"] in KINDS_PLANNED_TO_WEEK_END and block.get("hours_per_day") is not None
    }


def run_blocks(
    farm: Farm, now: datetime, data_dir: Path, compute: ComputeFunction, sender: str
) -> tuple[list[tuple[Block, WeeklyResult]], list[str]]:
    """Run the engine for every block. Returns the results and the errors (block name: error).

    One block failing (for example no internet) does not stop the others, but
    the caller sees every error and decides whether to send.
    """
    results = []
    errors = []
    watered_ids = blocks_watered_as_planned(data_dir, farm.farm_id, now, sender)
    for block in farm.blocks:
        state_path = data_dir / "state" / farm.farm_id / f"{block.block_id}.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            results.append((block, compute(block, now, state_path, block.block_id in watered_ids)))
        except Exception as error:  # noqa: BLE001 - every failure is shown, none is hidden
            errors.append(f"{block.name}: {error}")
    return results, errors


def block_summary(block: Block, result: WeeklyResult) -> dict[str, Any]:
    """The numbers behind one block's line, saved in the outbox so a week can be checked later.

    hours_per_day is None when the line gave no hours (no water, check field,
    or a plan that did not fit in a day and became a "pump too slow" line).
    """
    plan = block_plan(result)
    shown = None
    if plan is not None and plan.fits:
        shown = format_hours_minutes(plan.per_day)
    return {
        "block_id": block.block_id,
        "kind": result.kind,
        "volume_m3": result.volume_m3,
        "pump_hours": result.pump_hours,
        "first_day": plan.first_day.isoformat() if plan else None,
        "days": plan.days if plan else None,
        "hours_per_day": round(plan.per_day, 3) if plan and plan.fits else None,
        "shown_per_day": shown,
        "shown_by_day": list(plan.shown_by_day) if plan and plan.fits else None,
        "daily_net_use_mm": [list(day) for day in result.daily_net_use_mm],
        "net_mm": result.net_mm,
        "start_dryness_mm": _start_dryness_mm(result, plan) if plan else None,
        "until": plan.until_text if plan else None,
        "report": result.text,
    }


def _start_dryness_mm(result: WeeklyResult, plan: Any) -> float | None:
    """How dry the soil already was when the plan started (mm), or None if unknown.

    net_mm minus the use of the plan's days (at least 0): the same number
    the day split spreads over the days (weekly_message._split_by_use).
    """
    if result.net_mm is None or not result.daily_net_use_mm:
        return None
    use = dict(result.daily_net_use_mm)
    days = [(plan.first_day + timedelta(days=i)).isoformat() for i in range(plan.days)]
    return round(max(0.0, result.net_mm - sum(use.get(day, 0.0) for day in days)), 4)


def send_weekly(
    farm: Farm,
    wording: MessageWording,
    sender_name: str,
    mode: str,
    data_dir: Path,
    now: datetime,
    compute: ComputeFunction | None = None,
    send_anyway: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Run every block, build the message, send it, and save it to the outbox.

    compute is the engine call per block; None means the real one (compute_block).
    Stops (SystemExit) if this farm already got this week's message from this
    sender, unless force: checked BEFORE the engine runs, because a run saves
    each block's soil state. Also stops before sending if any block failed,
    unless send_anyway. Returns the outbox record. Not safe against two sends
    started at the same moment (no lock): run one at a time.
    """
    week = week_key(now)
    if not force and already_sent(data_dir / OUTBOX_NAME, farm.farm_id, week, sender_name):
        raise SystemExit(f"{farm.farm_id} already got week {week}; add --force to resend")
    results, errors = run_blocks(farm, now, data_dir, compute or compute_block, sender_name)
    for error in errors:
        print(f"block failed: {error}", file=sys.stderr)
    if errors and not send_anyway:
        raise SystemExit("not sent: fix the errors above, or add --send-anyway")
    for block, result in results:
        print(f"--- {block.name} ---\n{result.text}\n", file=sys.stderr)
    return _send_and_record(
        farm, wording, sender_name, mode, data_dir, now, results, errors, KIND_WEEKLY_MESSAGE
    )


def _send_and_record(
    farm: Farm,
    wording: MessageWording,
    sender_name: str,
    mode: str,
    data_dir: Path,
    now: datetime,
    results: list[tuple[Block, WeeklyResult]],
    errors: list[str],
    message_kind: str,
) -> dict[str, Any]:
    """Build the message from block results, send it, and add it to the outbox."""
    lines = [block_line(block.name, result, wording) for block, result in results]
    is_update = message_kind == KIND_UPDATE_MESSAGE
    greeting = "update_greeting" if is_update else "greeting"
    template = wording.meta_update_template if is_update else wording.meta_template
    message = build_message(farm.farmer_name, lines, now, wording, greeting)
    if mode == MODE_TEMPLATE:
        payload = template_payload(farm.phone, message, template, wording.meta_language)
    else:
        payload = text_payload(farm.phone, message)
    sender = MockSender() if sender_name == "mock" else CloudApiSender.from_environment()
    sent = sender.send(payload, message.text)
    record = {
        # Our own id for this send. Mock sends have no Meta id, and a reply's OK
        # must point at exactly one plan, so every send gets one.
        "send_id": uuid.uuid4().hex,
        "message_kind": message_kind,
        "sent_at": now.isoformat(),
        "week": week_key(now),
        "farm_id": farm.farm_id,
        "phone": farm.phone,
        "sender": sender_name,
        "mode": mode,
        "message_id": sent.message_id,
        "text": message.text,
        "blocks": [block_summary(block, result) for block, result in results],
        "errors": errors,
    }
    append_record(data_dir / OUTBOX_NAME, record)
    return record


def send_update(
    farm: Farm,
    wording: MessageWording,
    sender_name: str,
    mode: str,
    data_dir: Path,
    now: datetime,
    compute: ComputeFunction | None = None,
) -> dict[str, Any] | None:
    """Rerun the engine mid-week and send an update if the plan changed a lot. UNTESTED IN FIELD.

    Returns the outbox record, or None when nothing was sent (and says why on
    the error stream). Rules:
    - only after this week's Monday message, never twice on one day, and at
      most MAX_UPDATES_PER_WEEK updates a week;
    - the engine runs as if the farmer followed the plan so far (soil full now)
      on a THROWAWAY copy of each block's state, so the real saved state is
      untouched and next Monday works as before;
    - sent only if a block's line changes kind, or a day's hours change by at
      least UPDATE_THRESHOLD_MINUTES. The update lists every block from today on.
    The newest message of the week is the plan an OK confirms (see
    blocks_watered_as_planned), so after an update the farmer's OK must answer it.
    """
    week = week_key(now)
    today = now.astimezone(CASABLANCA_TZ).date()
    sent = [
        r
        for r in read_records(data_dir / OUTBOX_NAME)
        if r["farm_id"] == farm.farm_id and r.get("sender") == sender_name and r["week"] == week
    ]
    if not sent:
        print("no update: no weekly message sent this week yet", file=sys.stderr)
        return None
    last = sent[-1]
    if _local_date(last["sent_at"]) == today:
        print("no update: a message already went out today", file=sys.stderr)
        return None
    updates = [r for r in sent if r.get("message_kind") == KIND_UPDATE_MESSAGE]
    if len(updates) >= MAX_UPDATES_PER_WEEK:
        print(f"no update: already {len(updates)} updates this week", file=sys.stderr)
        return None
    results, errors = _preview_blocks(farm, now, data_dir, compute or compute_block)
    for error in errors:
        print(f"block failed: {error}", file=sys.stderr)
    if errors:
        print("no update: a block failed, the Monday plan stays", file=sys.stderr)
        return None
    old_by_block = {block["block_id"]: block for block in last["blocks"]}
    results = [
        (block, _add_unspent_dryness(result, old_by_block.get(block.block_id), today))
        for block, result in results
    ]
    changed = []
    for block, result in results:
        new = block_summary(block, result)
        old = old_by_block.get(block.block_id)
        if old is None or plan_changed(old, new, last["sent_at"], today):
            changed.append(block.name)
    if not changed:
        print("no update: the plan did not change enough", file=sys.stderr)
        return None
    print(f"update: plan changed for {', '.join(changed)}", file=sys.stderr)
    return _send_and_record(
        farm, wording, sender_name, mode, data_dir, now, results, [], KIND_UPDATE_MESSAGE
    )


def _add_unspent_dryness(
    result: WeeklyResult, old: dict[str, Any] | None, today: date
) -> WeeklyResult:
    """Add back the part of the old plan's start dryness not yet watered.

    The update runs with the soil taken as full now. But the old plan spread
    the soil's start dryness over all its days, so the days from today on
    still owed their share of it. Without this, an update would quietly drop
    that water. Share = the old plan's start dryness x the fraction of its
    days' use that falls on today or later. Turned into valve hours with the
    old plan's own hours-per-mm, so efficiency and leaching are the same as
    before.
    """
    if old is None or not old.get("start_dryness_mm") or not old.get("net_mm"):
        return result
    if result.kind not in (KIND_WEEKLY_AMOUNT, KIND_IRRIGATE_BY_DEADLINE, KIND_NO_IRRIGATION_DUE):
        return result
    if result.hours_available is None or old.get("pump_hours") is None:
        return result
    first = date.fromisoformat(old["first_day"])
    use = {
        date.fromisoformat(day): max(0.0, mm)
        for day, mm in old.get("daily_net_use_mm") or []
        if first <= date.fromisoformat(day) < first + timedelta(days=old["days"])
    }
    total = sum(use.values())
    if total <= 0:
        return result
    left = sum(mm for day, mm in use.items() if day >= today) / total
    owed_mm = old["start_dryness_mm"] * left
    hours_per_mm = old["pump_hours"] / old["net_mm"]
    return dataclasses.replace(
        result,
        kind=KIND_WEEKLY_AMOUNT if result.kind == KIND_NO_IRRIGATION_DUE else result.kind,
        net_mm=(result.net_mm or 0.0) + owed_mm,
        pump_hours=(result.pump_hours or 0.0) + owed_mm * hours_per_mm,
    )


def _preview_blocks(
    farm: Farm, now: datetime, data_dir: Path, compute: ComputeFunction
) -> tuple[list[tuple[Block, WeeklyResult]], list[str]]:
    """Run every block on a copy of its state, with the soil taken as full now."""
    results = []
    errors = []
    with tempfile.TemporaryDirectory() as scratch:
        for block in farm.blocks:
            real_state = data_dir / "state" / farm.farm_id / f"{block.block_id}.json"
            copy = Path(scratch) / f"{block.block_id}.json"
            if real_state.exists():
                shutil.copyfile(real_state, copy)
            try:
                results.append((block, compute(block, now, copy, True)))
            except Exception as error:  # noqa: BLE001 - every failure is shown, none is hidden
                errors.append(f"{block.name}: {error}")
    return results, errors


def plan_changed(old: dict[str, Any], new: dict[str, Any], old_sent_at: str, today: date) -> bool:
    """True if one block's plan from today on differs enough to tell the farmer.

    old and new are block_summary() records. The line's type (hours, no water,
    check field, ...) changing always counts. Two hours plans are compared day
    by day from today to the old plan's last day: a day missing from one plan
    counts as 0:00.
    """
    if _line_type(old) != _line_type(new):
        return True
    if _line_type(new) != "hours":
        return False
    old_days = _minutes_by_day(old, _local_date(old_sent_at))
    new_days = _minutes_by_day(new, today)
    # Only the days the old plan covered: new days after its end (next week)
    # are not a change, next Monday's message plans them anyway.
    old_end = max(old_days, default=today)
    dates = {day for day in (*old_days, *new_days) if today <= day <= old_end}
    return any(
        abs(old_days.get(day, 0) - new_days.get(day, 0)) >= UPDATE_THRESHOLD_MINUTES
        for day in dates
    )


def _line_type(summary: dict[str, Any]) -> str:
    """Which kind of line a block got: "hours", or the wording section it used."""
    if summary.get("shown_by_day"):
        return "hours"
    section = SECTION_BY_KIND.get(summary["kind"], "unknown")
    return "block_pump_too_slow" if section == "block_hours" else section


def _minutes_by_day(summary: dict[str, Any], fallback_first_day: date) -> dict[date, int]:
    """{calendar day: minutes} from a summary's shown_by_day."""
    first = summary.get("first_day")
    start = date.fromisoformat(first) if first else fallback_first_day
    result = {}
    for index, shown in enumerate(summary.get("shown_by_day") or []):
        hours, minutes = shown.split(":")
        result[start + timedelta(days=index)] = int(hours) * 60 + int(minutes)
    return result


def _local_date(iso_text: str) -> date:
    """The Casablanca calendar date of a saved ISO time."""
    return datetime.fromisoformat(iso_text).astimezone(CASABLANCA_TZ).date()


def handle_reply(reply: Reply, data_dir: Path) -> dict[str, Any]:
    """Save one farmer reply, tied to the message it answers, then call the chatbot hook.

    The reply is matched to our message by Meta's id when WhatsApp gives it,
    otherwise to the newest message sent to that phone number before the reply.
    A reply that cannot be matched is still saved, with no farm and no week.
    """
    sent = _find_sent_message(data_dir / OUTBOX_NAME, reply)
    needs_answer = reply.kind != "button" or reply.button_id in NEEDS_ANSWER_BUTTONS
    record: dict[str, Any] = {
        "received_at": reply.received_at,
        "farm_id": sent["farm_id"] if sent else None,
        "week": sent["week"] if sent else None,
        "send_id": sent.get("send_id") if sent else None,
        "phone": reply.phone,
        "kind": reply.kind,
        "button_id": reply.button_id,
        "text": reply.text,
        "message_id": reply.message_id,
        "replied_to": reply.replied_to,
        "needs_answer": needs_answer,
        "auto_answer": None,
    }
    if needs_answer:
        record["auto_answer"] = chatbot.answer(record)
    append_record(data_dir / REPLIES_NAME, record)
    return record


def _find_sent_message(outbox: Path, reply: Reply) -> dict[str, Any] | None:
    """The outbox record this reply answers, or None if it cannot be told.

    - WhatsApp gave the id of our message: only that message counts, and only if
      it went to the phone that replied. An id we do not know means None, never
      a guess: a guess could confirm the wrong week's plan.
    - No id (a typed message): the newest message to that phone sent before the reply.
    """
    records = read_records(outbox)
    if reply.replied_to:
        for record in records:
            if record.get("message_id") == reply.replied_to:
                return record if record.get("phone") == reply.phone else None
        return None
    received = _parse_time(reply.received_at)
    found: dict[str, Any] | None = None
    for record in records:
        if record.get("phone") != reply.phone:
            continue
        sent_at = _parse_time(record.get("sent_at"))
        if received is not None and sent_at is not None and sent_at > received:
            continue
        found = record  # keeps going, so the newest one wins
    return found


def _parse_time(text: str | None) -> datetime | None:
    """An ISO time with a time zone, or None if missing or unreadable."""
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _load_farm_and_wording(farm_path: Path) -> tuple[Farm, MessageWording]:
    """Read both files, or stop with a one-line message saying what is wrong."""
    try:
        farm = load_farm(farm_path)
    except (OSError, FarmFileError) as error:
        raise SystemExit(f"farm file {farm_path}: {error}") from None
    try:
        wording = load_wording(farm.wording_file)
    except (OSError, CardTemplateError) as error:
        raise SystemExit(f"wording file {farm.wording_file}: {error}") from None
    return farm, wording


def _command_send(args: argparse.Namespace) -> None:
    farm, wording = _load_farm_and_wording(args.farm)
    now = datetime.now(CASABLANCA_TZ)
    outbox = args.data_dir / OUTBOX_NAME
    record = send_weekly(
        farm,
        wording,
        args.sender,
        args.mode,
        args.data_dir,
        now,
        send_anyway=args.send_anyway,
        force=args.force,
    )
    sent_id = record["message_id"] or "(mock, nothing sent)"
    print(f"\nsaved to {outbox}; message id: {sent_id}", file=sys.stderr)


def _command_update(args: argparse.Namespace) -> None:
    farm, wording = _load_farm_and_wording(args.farm)
    now = datetime.now(CASABLANCA_TZ)
    record = send_update(farm, wording, args.sender, args.mode, args.data_dir, now)
    if record is not None:
        sent_id = record["message_id"] or "(mock, nothing sent)"
        print(f"\nupdate saved; message id: {sent_id}", file=sys.stderr)


def _command_simulate_reply(args: argparse.Namespace) -> None:
    farm, _wording = _load_farm_and_wording(args.farm)
    now = datetime.now(CASABLANCA_TZ)
    reply = Reply(
        message_id=f"mock-reply-{now.timestamp():.0f}",
        phone=farm.phone,
        received_at=now.isoformat(),
        kind="button" if args.button else "text",
        button_id=args.button,
        text=args.text or args.button or "",
        replied_to=None,
    )
    record = handle_reply(reply, args.data_dir)
    print(f"saved reply: {record}")


def _command_replies(args: argparse.Namespace) -> None:
    for record in read_records(args.data_dir / REPLIES_NAME):
        flag = "ANSWER" if record["needs_answer"] else "ok"
        what = record["button_id"] or record["text"]
        print(f"{record['received_at']}  {record['farm_id']}  {record['week']}  {flag:6}  {what}")


def _command_meta_template(args: argparse.Namespace) -> None:
    try:
        wording = load_wording(args.wording)
    except (OSError, CardTemplateError) as error:
        raise SystemExit(f"wording file {args.wording}: {error}") from None
    print(
        f"name: {wording.meta_template}   language: {wording.meta_language}   category: Utility\n"
    )
    print(meta_template_body(wording))
    print(
        f"\n--- second template (mid-week update) ---\n"
        f"name: {wording.meta_update_template}   language: {wording.meta_language}"
        "   category: Utility\n"
    )
    print(meta_template_body(wording, "update_greeting"))
    buttons = ", ".join(wording.sections[f"button_{button_id}"] for button_id in BUTTON_IDS)
    print(f"\nquick reply buttons, in this order: {buttons}")


def meta_create_payload(wording: MessageWording, which: str, today: date) -> dict[str, Any]:
    """The Meta request that creates the weekly or the update template from a wording file."""
    greeting = "update_greeting" if which == KIND_UPDATE_MESSAGE else "greeting"
    name = wording.meta_update_template if which == KIND_UPDATE_MESSAGE else wording.meta_template
    example_line = wording.sections["block_hours"].format(
        block="A", hours_per_day="2:00", until_date=(today + timedelta(days=6)).isoformat()
    )
    examples = ["Test", today.isoformat(), example_line]
    buttons = [wording.sections[f"button_{button_id}"] for button_id in BUTTON_IDS]
    body = meta_template_body(wording, greeting)
    return template_create_payload(name, wording.meta_language, body, examples, buttons)


def _command_meta_create(args: argparse.Namespace) -> None:
    for path in args.wording:
        try:
            wording = load_wording(path)
        except (OSError, CardTemplateError) as error:
            raise SystemExit(f"wording file {path}: {error}") from None
        today = datetime.now(CASABLANCA_TZ).date()
        payload = meta_create_payload(wording, args.which, today)
        print(f"--- {payload['name']} ({payload['language']}) ---")
        try:
            print(create_template(args.waba_id, payload))
        except RuntimeError as error:
            print(f"FAILED: {error}")


def _command_serve(args: argparse.Namespace) -> None:
    # Imported here so the other commands do not load the web server code.
    from orchardwater.webhook import serve

    serve(args.port, args.data_dir, args.no_signature_check)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Weekly WhatsApp loop.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, dest="data_dir")
    commands = parser.add_subparsers(dest="command", required=True)

    send = commands.add_parser("send", help="run every block and send the weekly message")
    send.add_argument("--farm", type=Path, required=True, help="farm file (see farms/example.json)")
    send.add_argument("--sender", choices=("mock", "cloud"), default="mock")
    send.add_argument("--mode", choices=(MODE_TEMPLATE, MODE_TEXT), default=MODE_TEMPLATE)
    send.add_argument("--force", action="store_true", help="send even if sent this week")
    send.add_argument(
        "--send-anyway",
        action="store_true",
        dest="send_anyway",
        help="send even if some blocks failed (they are left out)",
    )
    send.set_defaults(run=_command_send)

    update = commands.add_parser(
        "update", help="mid-week: rerun and send an update only if the plan changed a lot"
    )
    update.add_argument("--farm", type=Path, required=True)
    update.add_argument("--sender", choices=("mock", "cloud"), default="mock")
    update.add_argument("--mode", choices=(MODE_TEMPLATE, MODE_TEXT), default=MODE_TEMPLATE)
    update.set_defaults(run=_command_update)

    simulate = commands.add_parser("simulate-reply", help="pretend the farmer replied")
    simulate.add_argument("--farm", type=Path, required=True)
    choice = simulate.add_mutually_exclusive_group(required=True)
    choice.add_argument("--button", choices=BUTTON_IDS)
    choice.add_argument("--text")
    simulate.set_defaults(run=_command_simulate_reply)

    replies = commands.add_parser("replies", help="list saved replies")
    replies.set_defaults(run=_command_replies)

    template = commands.add_parser("meta-template", help="print the text for Meta's editor")
    template.add_argument("--wording", type=Path, required=True)
    template.set_defaults(run=_command_meta_template)

    create = commands.add_parser(
        "meta-create", help="submit templates to Meta for review (needs WHATSAPP_TOKEN)"
    )
    create.add_argument("--wording", type=Path, nargs="+", required=True)
    create.add_argument(
        "--which", choices=(KIND_WEEKLY_MESSAGE, KIND_UPDATE_MESSAGE), default=KIND_UPDATE_MESSAGE
    )
    create.add_argument(
        "--waba-id", required=True, dest="waba_id", help="WhatsApp Business Account ID"
    )
    create.set_defaults(run=_command_meta_create)

    serve = commands.add_parser("serve", help="receive Meta's webhook")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument(
        "--no-signature-check",
        action="store_true",
        dest="no_signature_check",
        help="local testing only: accept bodies Meta did not sign",
    )
    serve.set_defaults(run=_command_serve)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Read the command line and run the chosen command."""
    for stream in (sys.stdout, sys.stderr):
        # Arabic text must print on a Windows console too (same fix as weekly.py).
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
