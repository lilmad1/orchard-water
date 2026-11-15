"""Turn one week of block results into the farmer's Monday message.

Like card.py, this file contains no sentences for farmers. Every word comes
from a message wording file that the operator writes (see messages/README.md). This
file reads that file, checks it, turns each block's water volume into hours
per day, and fills in the blanks.

Data flow:
    farm.Farm + one WeeklyResult per block --+
                                              +--> build_message() -> WeeklyMessage
    load_wording(file) -----------------------+       (text, buttons, template parts)
"""

import math
import string
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from orchardwater.card import CardTemplateError, _split_file
from orchardwater.result import (
    KIND_DEADLINE_PASSED,
    KIND_IRRIGATE_BY_DEADLINE,
    KIND_NO_IRRIGATION_DUE,
    KIND_PUMP_TOO_SLOW,
    KIND_SOIL_AT_LIMIT,
    KIND_SOIL_LIMIT_AHEAD,
    KIND_UNAVAILABLE,
    KIND_WEEKLY_AMOUNT,
    KIND_WEEKLY_PUMP_TOO_SLOW,
    WeeklyResult,
)
from orchardwater.weekly import CASABLANCA_TZ

# Which wording section a block line uses, for each kind of engine result.
SECTION_BY_KIND: dict[str, str] = {
    KIND_WEEKLY_AMOUNT: "block_hours",
    KIND_IRRIGATE_BY_DEADLINE: "block_hours",
    KIND_NO_IRRIGATION_DUE: "block_no_water",
    KIND_PUMP_TOO_SLOW: "block_pump_too_slow",
    KIND_WEEKLY_PUMP_TOO_SLOW: "block_pump_too_slow",
    # The engine cannot size a safe amount in these three: someone must look.
    KIND_DEADLINE_PASSED: "block_check_field",
    KIND_SOIL_AT_LIMIT: "block_check_field",
    KIND_SOIL_LIMIT_AHEAD: "block_check_field",
    KIND_UNAVAILABLE: "block_unavailable",
}

# Blanks each section may use, and must use. A block line without its
# hours or its end date could mislead, so those are required.
ALLOWED_BLANKS: dict[str, tuple[str, ...]] = {
    "greeting": ("farmer_name", "made_on"),
    # First line of a mid-week update (the plan changed with the weather).
    "update_greeting": ("farmer_name", "made_on"),
    "block_hours": ("block", "hours_per_day", "until_date"),
    # Used instead of block_hours when the days get different hours (weather).
    "block_schedule": ("block", "schedule"),
    "schedule_day": ("day", "hours"),
    "day_off": (),
    "day_names": (),
    "block_no_water": ("block", "until_date"),
    "block_pump_too_slow": ("block",),
    "block_check_field": ("block",),
    "block_unavailable": ("block",),
    "closing": (),
}
REQUIRED_BLANKS = ALLOWED_BLANKS

BUTTON_IDS = ("ok", "question", "more_info")
BUTTON_SECTIONS = tuple(f"button_{button_id}" for button_id in BUTTON_IDS)
ALL_SECTIONS = (*ALLOWED_BLANKS, *BUTTON_SECTIONS)
BLOCK_SECTIONS = tuple(name for name in ALLOWED_BLANKS if name.startswith("block_"))
ONE_LINE_SECTIONS = (*BLOCK_SECTIONS, "schedule_day", "day_off", "day_names")
# Between the days of a schedule line. Arabic text uses the Arabic comma.
SCHEDULE_SEPARATOR = {"ltr": ", ", "rtl": "، "}
LTR_ISOLATE_START = "\u2066"
POP_ISOLATE = "\u2069"


def ltr_run(text: str, wording: "MessageWording") -> str:
    """Keep a Latin name or a date in its own left-to-right run inside Arabic text.

    Without this, a phone shows 2026-11-10 flipped as 10-11-2026 after Arabic
    letters, and "Block A" jumps around its line. The marks are invisible.
    Left-to-right wordings are returned unchanged.
    """
    if wording.direction != "rtl":
        return text
    return f"{LTR_ISOLATE_START}{text}{POP_ISOLATE}"


_HEADER_KEYS = (
    "language",
    "direction",
    "meta_template",
    "meta_update_template",
    "meta_language",
    "plan_separator",
)
# WhatsApp limits (Meta Cloud API docs): a reply button title is at most 20
# characters, and a message with buttons has at most 1024 characters of text.
MAX_BUTTON_CHARS = 20
MAX_BODY_CHARS = 1024
# Hours per day are rounded UP to whole steps of this many minutes. A farmer sets
# a timer or watches a clock; 5-minute steps are as fine as that gets. Up, not
# nearest, so the week's total is never short of what the engine asked for.
ROUND_TO_MINUTES = 5
# A plan period that ends before this time of day does not get that last day:
# there is not enough of it left to water a full day's share.
LAST_DAY_CUTOFF = time(12, 0)
# Days only get different hours when the weather really differs: if the
# biggest and smallest day are less than this many minutes apart, the
# farmer gets one simple "H:MM per day" line instead of a noisy schedule.
MIN_SCHEDULE_SPREAD_MINUTES = 15


@dataclass(frozen=True)
class MessageWording:
    """A checked wording file: settings plus one piece of text per section."""

    language: str
    direction: str
    meta_template: str
    meta_update_template: str
    meta_language: str
    plan_separator: str
    sections: dict[str, str]


@dataclass(frozen=True)
class WeeklyMessage:
    """One farmer's message, ready for a sender.

    text is the full message as the farmer reads it. buttons are (id, title)
    pairs. template_params are the same facts in the order the approved Meta
    template expects them ({{1}}, {{2}}, ...), each on one line, because Meta
    does not allow line breaks inside a template blank.
    """

    text: str
    buttons: tuple[tuple[str, str], ...]
    template_params: tuple[str, ...]


def load_wording(path: Path) -> MessageWording:
    """Read and check a message wording file. Raises CardTemplateError if unusable."""
    header, sections = _split_file(path.read_text(encoding="utf-8-sig"))
    settings = _read_header(header)
    for name in ALL_SECTIONS:
        if name not in sections:
            raise CardTemplateError(f"missing section [{name}]")
    unknown = [name for name in sections if name not in ALL_SECTIONS]
    if unknown:
        raise CardTemplateError(f"unknown section [{unknown[0]}]; known: {', '.join(ALL_SECTIONS)}")
    for name in ALLOWED_BLANKS:
        _check_blanks(name, sections[name])
    for name in ONE_LINE_SECTIONS:
        _check_one_line(name, sections[name])
    _check_day_names(sections["day_names"])
    for name in BUTTON_SECTIONS:
        _check_button(name, sections[name])
    return MessageWording(
        language=settings["language"],
        direction=settings["direction"],
        meta_template=settings["meta_template"],
        meta_update_template=settings["meta_update_template"],
        meta_language=settings["meta_language"],
        plan_separator=settings["plan_separator"],
        sections=sections,
    )


def _read_header(lines: list[str]) -> dict[str, str]:
    """Read the `key = value` settings and check that all are present."""
    settings: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in _HEADER_KEYS:
            raise CardTemplateError(f"unreadable settings line: {line.strip()!r}")
        settings[key] = value.strip()
    for key in _HEADER_KEYS:
        if not settings.get(key):
            raise CardTemplateError(f"missing setting: {key}")
    if settings["direction"] not in ("ltr", "rtl"):
        raise CardTemplateError("direction must be ltr or rtl")
    return settings


def _check_blanks(section: str, wording: str) -> None:
    """Only allowed, plain blanks, and every required one present."""
    if not wording:
        raise CardTemplateError(f"section [{section}] has no wording")
    try:
        parsed = list(string.Formatter().parse(wording))
    except ValueError as error:
        raise CardTemplateError(f"section [{section}]: bad curly brackets ({error})") from None
    found = set()
    for _text, name, format_spec, conversion in parsed:
        if name is None:
            continue
        if format_spec or conversion or name not in ALLOWED_BLANKS[section]:
            allowed = ", ".join(ALLOWED_BLANKS[section]) or "none"
            raise CardTemplateError(
                f"section [{section}]: blank {{{name}}} not allowed; allowed here: {allowed}"
            )
        found.add(name)
    for required in REQUIRED_BLANKS[section]:
        if required not in found:
            raise CardTemplateError(f"section [{section}]: must contain {{{required}}}")


def _check_one_line(section: str, wording: str) -> None:
    """A block line goes inside one Meta template blank, which cannot hold a line break."""
    if "\n" in wording or "\t" in wording or "    " in wording:
        raise CardTemplateError(
            f"section [{section}] must be one line, with no tabs and no runs of 4 spaces"
        )


def _check_day_names(wording: str) -> None:
    """Seven day names, Monday first, separated by |."""
    names = [name.strip() for name in wording.split("|")]
    if len(names) != 7 or not all(names):
        raise CardTemplateError("section [day_names] must be 7 names, Monday first, split by |")


def _check_button(section: str, wording: str) -> None:
    """A button title: one line, not empty, at most 20 characters."""
    if not wording or "\n" in wording:
        raise CardTemplateError(f"section [{section}] must be one short line")
    if len(wording) > MAX_BUTTON_CHARS:
        raise CardTemplateError(
            f"section [{section}] is {len(wording)} characters; WhatsApp allows {MAX_BUTTON_CHARS}"
        )


@dataclass(frozen=True)
class DailyPlan:
    """How one block's valve hours are spread over calendar days.

    days: calendar days to water, today included. last_day: the last of them
    (Casablanca date). per_day: valve hours each day if spread evenly, before
    rounding. by_day: the hours actually planned for each day, today first
    (split by each day's forecast water use when known, else even).
    fits: False if one day's share does not fit in the hours that day has
    (today only has the hours left after the message; a deadline day only
    the hours before the deadline). until_text: the end shown to the farmer:
    the date, plus the clock time when the last day is the deadline day.
    """

    days: int
    last_day: date
    per_day: float
    fits: bool
    until_text: str
    by_day: tuple[float, ...] = ()

    @property
    def first_day(self) -> date:
        return self.last_day - timedelta(days=self.days - 1)

    @property
    def shown_by_day(self) -> tuple[str, ...]:
        """Each day's hours as the farmer sees them (H:MM, rounded up)."""
        return tuple(format_hours_minutes(hours) for hours in self.by_day)


def hours_per_day(pump_hours: float, days: int) -> float:
    """Spread a block's valve hours evenly over a number of calendar days.

    Example: 10 h over 7 days -> 1.43 h per day.
    UNTESTED IN THE FIELD. Spreading evenly is a choice, not FAO-56.
    """
    if pump_hours < 0 or days < 1:
        raise ValueError("need pump_hours >= 0 and days >= 1")
    return pump_hours / days


def daily_plan(
    pump_hours: float,
    start: datetime,
    end: datetime,
    is_deadline: bool,
    daily_use_mm: tuple[tuple[str, float], ...] = (),
    net_mm: float | None = None,
) -> DailyPlan:
    """Which calendar days a block is watered on, and how much each day.

    start: when the message is made. end: the deadline, or the end of the
    forecast. Days are counted on the calendar in Casablanca, because the
    message says "per day until <date>" and a farmer reads that as dates,
    not as 24-hour periods. The end day only counts if the period ends at or
    after midday (LAST_DAY_CUTOFF); otherwise the plan stops the day before.
    Example: Monday 07:00 to Wednesday 06:54 -> Monday and Tuesday -> 2 days.

    daily_use_mm (from the engine, per day: crop use minus rain) splits the
    water over the days: a hot day gets more, a rainy day may get none.
    With net_mm (the engine's soil refill for the period) the split also
    corrects one thing the weekly total cannot see: watered every day, the
    soil is full when rain comes, so a rainy day's extra rain drains away
    instead of refilling the soil. Then each day gets its own use, plus a
    share of the dryness the soil already had at the start, so a rainy week
    can get a bit MORE than the weekly total, never less. If that split does not fit in the
    hours a day really has, the even split is used; if neither fits, fits is False.
    UNTESTED IN THE FIELD: a choice, not FAO-56.
    """
    if end <= start:
        raise ValueError("the plan must end after it starts")
    start_local = start.astimezone(CASABLANCA_TZ)
    end_local = end.astimezone(CASABLANCA_TZ)
    today = start_local.date()
    last = end_local.date()
    if end_local.time() < LAST_DAY_CUTOFF:
        last -= timedelta(days=1)
    last = max(last, today)
    days = (last - today).days + 1
    per_day = hours_per_day(pump_hours, days)
    usable = _usable_hours(start_local, end_local, last)
    even = tuple(per_day for _ in range(days))
    weighted = _split_by_use(pump_hours, today, days, daily_use_mm, net_mm)
    by_day = even
    fits = _fits(even, usable)
    if weighted is not None and _spread_minutes(weighted) >= MIN_SCHEDULE_SPREAD_MINUTES:
        if _fits(weighted, usable):
            by_day, fits = weighted, True
    until_text = last.strftime("%Y-%m-%d")
    if is_deadline and last == end_local.date():
        until_text += end_local.strftime(" %H:%M")
    return DailyPlan(days, last, per_day, fits, until_text, by_day)


def _split_by_use(
    pump_hours: float,
    today: date,
    days: int,
    daily_use_mm: tuple[tuple[str, float], ...],
    net_mm: float | None,
) -> tuple[float, ...] | None:
    """Each day's valve hours from each day's use (a negative day = 0), or None if unknown.

    Without net_mm: the total is split in proportion to the use.
    With net_mm > 0: hours per mm = pump_hours / net_mm. The soil's dryness at
    the start is net_mm minus all the period's use (at least 0); it is spread
    in proportion to the use too. Day i gets (its use + its share of that
    start dryness) x hours per mm. With no rainy day this equals the plain
    split; with one, the total is higher by that day's extra rain.
    """
    use = dict(daily_use_mm)
    uses = [use.get((today + timedelta(days=index)).isoformat(), 0.0) for index in range(days)]
    weights = [max(0.0, value) for value in uses]
    total = sum(weights)
    if not use or total <= 0:
        return None
    if net_mm is None or net_mm <= 0:
        return tuple(pump_hours * weight / total for weight in weights)
    hours_per_mm = pump_hours / net_mm
    start_dryness = max(0.0, net_mm - sum(uses))
    return tuple((weight + start_dryness * weight / total) * hours_per_mm for weight in weights)


def _spread_minutes(by_day: tuple[float, ...]) -> int:
    """Minutes between the biggest and smallest day, as shown to the farmer."""
    shown = [_minutes(format_hours_minutes(hours)) for hours in by_day]
    return max(shown) - min(shown)


def _fits(by_day: tuple[float, ...], usable: list[float]) -> bool:
    """Every day's shown (rounded-up) hours fit in the hours that day has."""
    return all(
        _minutes(format_hours_minutes(hours)) / 60 <= hours_that_day
        for hours, hours_that_day in zip(by_day, usable, strict=True)
    )


def _usable_hours(start: datetime, end: datetime, last: date) -> list[float]:
    """Hours each planned day has for watering: today after start, the end day before end."""
    usable = []
    day = start.date()
    while day <= last:
        day_start = max(start, datetime.combine(day, time(0, 0), CASABLANCA_TZ))
        day_end = min(end, datetime.combine(day + timedelta(days=1), time(0, 0), CASABLANCA_TZ))
        usable.append((day_end - day_start).total_seconds() / 3600)
        day += timedelta(days=1)
    return usable


def _minutes(hours_minutes: str) -> int:
    """'1:25' -> 85."""
    hours, minutes = hours_minutes.split(":")
    return int(hours) * 60 + int(minutes)


def format_hours_minutes(hours: float) -> str:
    """Hours as H:MM, rounded UP to whole ROUND_TO_MINUTES minutes.

    Up, so the farmer never waters less than the engine asked for. A real
    amount above zero never shows as 0:00. A tiny rounding margin (1e-6 min)
    stops float noise from adding a whole step.
    Example: 1.43 h -> 85.7 min -> 90 min -> "1:30".
    """
    steps = math.ceil(hours * 60 / ROUND_TO_MINUTES - 1e-6)
    minutes = max(steps, 0) * ROUND_TO_MINUTES
    return f"{minutes // 60}:{minutes % 60:02d}"


def block_plan(result: WeeklyResult) -> DailyPlan | None:
    """The daily plan behind a "water H:MM a day" line, or None for other kinds."""
    if SECTION_BY_KIND[result.kind] != "block_hours":
        return None
    if result.pump_hours is None or result.hours_available is None:
        raise ValueError(f"result of kind {result.kind!r} has no pump hours")
    end = _period_end(result)
    start = result.made_at or end - timedelta(hours=result.hours_available)
    return daily_plan(
        result.pump_hours,
        start,
        end,
        result.deadline is not None,
        result.daily_net_use_mm,
        result.net_mm,
    )


def block_line(block_name: str, result: WeeklyResult, wording: MessageWording) -> str:
    """The one line about one block, filled from the section that fits its result.

    A plan whose daily share does not fit in a day's hours gets the
    "pump too slow" line instead: better a call than an impossible number.
    """
    section = SECTION_BY_KIND[result.kind]
    block_name = ltr_run(block_name, wording)
    values = {"block": block_name}
    if section == "block_hours":
        plan = block_plan(result)
        assert plan is not None
        if not plan.fits:
            return wording.sections["block_pump_too_slow"].format(block=block_name)
        shown = plan.shown_by_day
        if len(set(shown)) > 1:
            schedule = schedule_text(plan, wording)
            return wording.sections["block_schedule"].format(block=block_name, schedule=schedule)
        values["hours_per_day"] = shown[0]
        values["until_date"] = ltr_run(plan.until_text, wording)
    elif section == "block_no_water":
        values["until_date"] = ltr_run(_last_plan_day(result).strftime("%Y-%m-%d"), wording)
    return wording.sections[section].format(**values)


def schedule_text(plan: DailyPlan, wording: MessageWording) -> str:
    """The days of one block on one line, like "Mon 1:20, Tue 1:30, Wed none".

    A day with 0:00 shows the [day_off] wording. If the plan ends on a
    deadline day, its clock time is added after that day.
    """
    names = [name.strip() for name in wording.sections["day_names"].split("|")]
    parts = []
    for index, hours in enumerate(plan.shown_by_day):
        day = plan.first_day + timedelta(days=index)
        shown = hours if hours != "0:00" else wording.sections["day_off"]
        parts.append(wording.sections["schedule_day"].format(day=names[day.weekday()], hours=shown))
    text = SCHEDULE_SEPARATOR[wording.direction].join(parts)
    if " " in plan.until_text:  # a deadline day: "2026-11-04 15:00"
        text += f" ({ltr_run(plan.until_text, wording)})"
    return text


def _last_plan_day(result: WeeklyResult) -> date:
    """The last calendar day a "no water" line covers, by the same midday rule as daily_plan.

    A forecast that ends at 00:00 on the 9th covers up to the 8th, so the line
    says "until the 8th", like a watering line would.
    """
    end = _period_end(result).astimezone(CASABLANCA_TZ)
    last = end.date()
    if end.time() < LAST_DAY_CUTOFF:
        last -= timedelta(days=1)
    if result.made_at is not None:
        last = max(last, result.made_at.astimezone(CASABLANCA_TZ).date())
    return last


def _period_end(result: WeeklyResult) -> datetime:
    """End of the period a block line covers: the deadline if any, else the forecast end."""
    end = result.deadline if result.deadline is not None else result.forecast_end
    if end is None:
        raise ValueError(f"result of kind {result.kind!r} has no end date")
    return end


def build_message(
    farmer_name: str,
    lines: list[str],
    made_at: datetime,
    wording: MessageWording,
    greeting_section: str = "greeting",
) -> WeeklyMessage:
    """Put greeting, block lines and closing together, plus the buttons.

    lines are the block lines from block_line(), in the farm file's order.
    """
    made_on = made_at.astimezone(CASABLANCA_TZ).strftime("%Y-%m-%d")
    values = {
        "farmer_name": ltr_run(farmer_name, wording),
        "made_on": ltr_run(made_on, wording),
    }
    greeting = wording.sections[greeting_section].format(**values)
    text = "\n\n".join([greeting, "\n".join(lines), wording.sections["closing"]])
    if len(text) > MAX_BODY_CHARS:
        raise ValueError(f"message is {len(text)} characters; WhatsApp allows {MAX_BODY_CHARS}")
    plan_one_line = f"{wording.plan_separator} ".join(lines)
    params = _template_params(wording.sections[greeting_section], values, plan_one_line)
    buttons = tuple(
        (button_id, wording.sections[f"button_{button_id}"]) for button_id in BUTTON_IDS
    )
    return WeeklyMessage(text=text, buttons=buttons, template_params=params)


def _template_params(greeting: str, values: dict[str, str], plan: str) -> tuple[str, ...]:
    """The values for {{1}}, {{2}}, ... in the order the blanks appear in the greeting.

    The plan (all block lines on one line) always comes last, matching
    meta_template_body() below.
    """
    params = [values[name] for name in _blank_order(greeting)]
    params.append(plan)
    return tuple(params)


def _blank_order(wording: str) -> list[str]:
    """Blank names in the order they first appear."""
    order: list[str] = []
    for _text, name, _spec, _conversion in string.Formatter().parse(wording):
        if name is not None and name not in order:
            order.append(name)
    return order


def meta_template_body(wording: MessageWording, greeting_section: str = "greeting") -> str:
    """The template text to paste into Meta's template editor, with {{1}}, {{2}}, ...

    Business-started WhatsApp messages must use a template Meta approved in
    advance. This prints that template from the same wording file, so the two
    cannot drift apart. The block lines become one blank at the end.
    """
    numbers = {
        name: f"{{{{{index}}}}}"
        for index, name in enumerate(_blank_order(wording.sections[greeting_section]), start=1)
    }
    greeting = wording.sections[greeting_section].format(**numbers)
    plan_blank = f"{{{{{len(numbers) + 1}}}}}"
    return "\n\n".join([greeting, plan_blank, wording.sections["closing"]])
