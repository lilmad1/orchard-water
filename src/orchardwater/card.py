"""Turn a weekly result into a farmer card, using wording from a template file.

The code in this file contains no sentences for farmers. Every word on a card
comes from a template file that the operator writes (see cards/README.md); this
file only reads that file, checks it, and fills in the numbers.

Data flow:
    result.WeeklyResult  --+
                           +--> render_text()  -> plain text for WhatsApp
    load_template(file) ---+--> render_html()  -> a printable web page

Both outputs use the same wording, so the words are written once.
"""

import html
import string
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from orchardwater.result import (
    ALL_KINDS,
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
from orchardwater.weekly import CASABLANCA_TZ, format_amount

# Blanks a template section may use, per kind of card. A blank is a word in curly
# brackets, like {deadline_date}. Cards are deliberately short: after seeing a full card
# should say just when and how much, so only the deadline and the water volume are
# offered, plus, on the cards that have no deadline, the last day the advice covers
# (valid_until): without it a farmer cannot tell when a weekly amount or a "nothing
# needed" card runs out. The two cards with no numbers at all, "check the field now" and
# "no estimate", carry the day they were made (made_on): an old printed or forwarded copy
# would otherwise look like a fresh one. The result still carries every other number (mm,
# pump hours, hours left) if a later card ever needs one.
_DEADLINE = ("deadline_date", "deadline_time")
# The day and time the model says the soil reaches its limit at a later hour, on the
# soil_limit_ahead card. Kept apart from the deadline blanks: it is a warning about a
# moment, not a time to water by.
_LIMIT = ("limit_date", "limit_time")
ALLOWED_BLANKS: dict[str, tuple[str, ...]] = {
    KIND_IRRIGATE_BY_DEADLINE: (*_DEADLINE, "volume_m3"),
    KIND_PUMP_TOO_SLOW: (*_DEADLINE, "volume_m3"),
    # No {volume_m3} once the deadline has passed: the report calls that amount out of
    # date, and a card must not turn it into an instruction.
    KIND_DEADLINE_PASSED: _DEADLINE,
    KIND_WEEKLY_AMOUNT: ("volume_m3", "valid_until"),
    KIND_WEEKLY_PUMP_TOO_SLOW: ("volume_m3", "valid_until"),
    KIND_NO_IRRIGATION_DUE: ("valid_until",),
    KIND_SOIL_AT_LIMIT: ("made_on",),
    KIND_SOIL_LIMIT_AHEAD: (*_LIMIT, "made_on"),
    KIND_UNAVAILABLE: ("made_on",),
}

# Blanks a section MUST contain, because a card without them could mislead: a
# deadline card that never says the deadline, or an amount card that never says
# the amount. The kinds with no numbers need nothing.
REQUIRED_BLANKS: dict[str, tuple[str, ...]] = {
    KIND_IRRIGATE_BY_DEADLINE: (*_DEADLINE, "volume_m3"),
    KIND_PUMP_TOO_SLOW: (*_DEADLINE, "volume_m3"),
    KIND_DEADLINE_PASSED: _DEADLINE,
    KIND_WEEKLY_AMOUNT: ("volume_m3", "valid_until"),
    KIND_WEEKLY_PUMP_TOO_SLOW: ("volume_m3", "valid_until"),
    KIND_NO_IRRIGATION_DUE: ("valid_until",),
    KIND_SOIL_AT_LIMIT: ("made_on",),
    KIND_SOIL_LIMIT_AHEAD: (*_LIMIT, "made_on"),
    KIND_UNAVAILABLE: ("made_on",),
}

_HEADER_KEYS = ("language", "direction", "title")

# A section that is not a kind of result. Its wording is added to the end of every
# "water by a deadline" card. The full report warns that the amount is worked out
# for the moment of the deadline and that applying it all earlier could overwater;
# a card that only says "water by then" would drop that. The code cannot write the
# sentence, so the template must.
CAUTION_SECTION = "early_watering_caution"

# Second section added to the end of a deadline card, after the caution. The next
# card is only right if the tool is told that water was applied (drip: --reset;
# flood: a new date, time and fullness). The person who runs the tool is not the
# farmer reading the card, so the card carries what the farmer can do: report back
# when and how much was watered. The words come from the template.
AFTER_WATERING_SECTION = "after_watering_note"
DEADLINE_CARD_EXTRAS = (CAUTION_SECTION, AFTER_WATERING_SECTION)

# Added to the two drip cards that have no deadline. The full report says that amount
# is worked out from the soil at the END of the forecast week, not for any single
# earlier hour, so applying all of it right away could overwater. A card that only
# shows the volume would drop that.
WEEKLY_TIMING_SECTION = "weekly_timing_note"

# Which extra sections are added to the end of which kind of card.
EXTRA_SECTIONS_BY_KIND: dict[str, tuple[str, ...]] = {
    KIND_IRRIGATE_BY_DEADLINE: DEADLINE_CARD_EXTRAS,
    # Every card that says "water X m3" also asks the farmer to report back after watering:
    # the next card is only right if the tool is told water was applied.
    KIND_PUMP_TOO_SLOW: (AFTER_WATERING_SECTION,),
    KIND_WEEKLY_AMOUNT: (WEEKLY_TIMING_SECTION, AFTER_WATERING_SECTION),
    KIND_WEEKLY_PUMP_TOO_SLOW: (WEEKLY_TIMING_SECTION, AFTER_WATERING_SECTION),
}
EXTRA_SECTION_NAMES = (*DEADLINE_CARD_EXTRAS, WEEKLY_TIMING_SECTION)


class CardTemplateError(ValueError):
    """The template file is not usable. The message says what to fix."""


@dataclass(frozen=True)
class CardTemplate:
    """A checked template: page settings plus one piece of wording per kind."""

    language: str
    direction: str
    title: str
    sections: dict[str, str]


def load_template(path: Path) -> CardTemplate:
    """Read and check a template file. Raises CardTemplateError if it is not usable.

    File format: settings lines first (`language = ...`, `direction = ltr`
    or `rtl`, `title = ...`), then one `[kind]` heading per kind of card,
    with its wording underneath. Lines starting with # are ignored.
    """
    # utf-8-sig: Notepad's default "UTF-8 with BOM" puts an invisible mark at the start of
    # the file that would otherwise make the first settings line unreadable.
    header, sections = _split_file(path.read_text(encoding="utf-8-sig"))
    settings = _read_header(header)
    required_sections = (*ALL_KINDS, *EXTRA_SECTION_NAMES)
    for kind in required_sections:
        if kind not in sections:
            raise CardTemplateError(f"missing section [{kind}]")
        _check_blanks(kind, sections[kind])
    unknown = [name for name in sections if name not in required_sections]
    if unknown:
        known = ", ".join(required_sections)
        raise CardTemplateError(f"unknown section [{unknown[0]}]; known: {known}")
    return CardTemplate(settings["language"], settings["direction"], settings["title"], sections)


def _split_file(text: str) -> tuple[list[str], dict[str, str]]:
    """Split a template file into its settings lines and its [section] bodies."""
    header: list[str] = []
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        # A line of only spaces counts as a blank line, so it still starts a new paragraph.
        line = line.rstrip()
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            name = stripped[1:-1].strip()
            if name in sections:
                raise CardTemplateError(f"section [{name}] appears twice")
            current = sections.setdefault(name, [])
        elif current is None:
            header.append(line)
        else:
            current.append(line)
    return header, {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _read_header(lines: list[str]) -> dict[str, str]:
    """Read the `key = value` settings and check that all three are present."""
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


def _check_blanks(kind: str, wording: str) -> None:
    """Check one section: only allowed, plain blanks, and every required one present."""
    if not wording:
        raise CardTemplateError(f"section [{kind}] has no wording")
    allowed = ALLOWED_BLANKS.get(kind, ())
    found: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(wording))
    except ValueError as error:
        raise CardTemplateError(f"section [{kind}]: bad curly brackets ({error})") from None
    for _text, name, format_spec, conversion in parsed:
        if name is None:
            continue
        if format_spec or conversion:
            raise CardTemplateError(f"section [{kind}]: use plain blanks like {{{name}}}")
        if name not in allowed:
            allowed_text = ", ".join(allowed) or "none"
            raise CardTemplateError(
                f"section [{kind}]: unknown blank {{{name}}}; allowed here: {allowed_text}"
            )
        found.add(name)
    for required in REQUIRED_BLANKS.get(kind, ()):
        if required not in found:
            raise CardTemplateError(f"section [{kind}]: must contain {{{required}}}")


def blank_values(result: WeeklyResult) -> dict[str, str]:
    """The water volume, deadline, valid-until day and made-on day a card can show, as text.

    The volume uses format_amount, the same formatter the report uses, so a real
    small amount never shows as 0.0. The deadline is shown in Casablanca clock
    time, date as YYYY-MM-DD and time as HH:MM.
    """
    values: dict[str, str] = {}
    if result.volume_m3 is not None:
        values["volume_m3"] = format_amount(result.volume_m3)
    if result.deadline is not None:
        local = _to_casablanca(result.deadline)
        prefix = "limit" if result.kind == KIND_SOIL_LIMIT_AHEAD else "deadline"
        values[f"{prefix}_date"] = local.strftime("%Y-%m-%d")
        values[f"{prefix}_time"] = _time_text(local)
    if result.made_at is not None:
        values["made_on"] = _to_casablanca(result.made_at).strftime("%Y-%m-%d")
    if result.forecast_end is not None:
        values["valid_until"] = _to_casablanca(result.forecast_end).strftime("%Y-%m-%d")
    return values


def _to_casablanca(moment: datetime) -> datetime:
    return moment.astimezone(CASABLANCA_TZ)


def _time_text(local: datetime) -> str:
    """The clock time as HH:MM, with the UTC offset added only if it is ambiguous.

    When Morocco's clocks go back, one hour is shown twice that day, so "01:00"
    could mean two moments an hour apart. The report already adds the offset;
    the card does the same for that one case and leaves every other time as
    plain HH:MM.
    """
    text = local.strftime("%H:%M")
    if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
        offset = local.utcoffset()
        assert offset is not None  # an aware time always has an offset
        text += f" (UTC{int(offset.total_seconds() // 3600):+d})"
    return text


def render_text(result: WeeklyResult, template: CardTemplate) -> str:
    """The card as plain text, ready to paste into WhatsApp."""
    wording = template.sections[result.kind]
    for extra in EXTRA_SECTIONS_BY_KIND.get(result.kind, ()):
        wording = f"{wording}\n\n{template.sections[extra]}"
    allowed = ALLOWED_BLANKS.get(result.kind, ())
    values = blank_values(result)
    missing = [name for name in _blanks_used(wording, allowed) if name not in values]
    if missing:
        # A result of this kind should always carry these numbers. If it does
        # not, stop rather than print a card with a hole in it.
        raise ValueError(f"result of kind {result.kind!r} has no value for {missing}")
    return wording.format(**values)


def _blanks_used(wording: str, allowed: tuple[str, ...]) -> list[str]:
    """The names of the blanks that appear in this wording."""
    used = []
    for _text, name, _spec, _conversion in string.Formatter().parse(wording):
        if name is not None and name in allowed:
            used.append(name)
    return used


def render_html(result: WeeklyResult, template: CardTemplate) -> str:
    """The card as a printable web page (open in a browser, then Print).

    All wording is escaped with html.escape, so a "<" in the template can
    not break the page. Paragraphs are separated by blank lines in the
    template; single line breaks stay as line breaks. The page carries no
    decoration: a deep green heading, dark text on white, large type.
    """
    text = render_text(result, template)
    paragraphs = [chunk for chunk in text.split("\n\n") if chunk.strip()]
    body = "\n".join(
        f"<p>{html.escape(chunk).replace(chr(10), '<br>')}</p>" for chunk in paragraphs
    )
    return _PAGE.format(
        language=html.escape(template.language, quote=True),
        direction=template.direction,
        title=html.escape(template.title),
        body=body,
    )


_PAGE = """<!doctype html>
<html lang="{language}" dir="{direction}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font-family: system-ui, sans-serif; color: #111; background: #fff;
  max-width: 34em; margin: 2em auto; padding: 0 1em; }}
h1 {{ color: #14532d; font-size: 1.5em; margin: 0 0 1em; }}
p {{ font-size: 1.2em; line-height: 1.5; margin: 0 0 1em; }}
@media print {{ body {{ margin: 0; max-width: none; }} }}
</style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>
"""
