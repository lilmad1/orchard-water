"""The outcome of one weekly run, kept as data.

weekly.py used to hand back only a finished English report. A farmer card
needs the same run as separate facts: which of a few situations happened,
and the numbers behind it. WeeklyResult carries both, so the report text
stays exactly as before and card.py can fill a template from the facts
without cutting sentences apart.

This file holds only data and one small rule (summary_kind). It imports
nothing from the rest of orchardwater, so weekly.py and card.py can both use it.
"""

import dataclasses
from datetime import datetime

# The situations a card can describe. Each one is a section in a card
# template file that the operator writes the wording for (see cards/README.md).
KIND_IRRIGATE_BY_DEADLINE = "irrigate_by_deadline"  # water this much, by this time
KIND_PUMP_TOO_SLOW = "pump_too_slow"  # a deadline exists, but the pump cannot make it
KIND_DEADLINE_PASSED = "deadline_passed"  # the deadline is already over
KIND_WEEKLY_AMOUNT = "weekly_amount"  # drip: an amount for the week, no deadline
KIND_WEEKLY_PUMP_TOO_SLOW = "weekly_pump_too_slow"  # same, but the pump is too slow
KIND_NO_IRRIGATION_DUE = "no_irrigation_due"  # flood: nothing needed this week
KIND_SOIL_AT_LIMIT = "soil_at_limit"  # soil at, or within the first hour of, the limit
KIND_SOIL_LIMIT_AHEAD = "soil_limit_ahead"  # soil reaches the limit at a later hour
KIND_UNAVAILABLE = "unavailable"  # the tool cannot give an estimate

ALL_KINDS = (
    KIND_IRRIGATE_BY_DEADLINE,
    KIND_PUMP_TOO_SLOW,
    KIND_DEADLINE_PASSED,
    KIND_WEEKLY_AMOUNT,
    KIND_WEEKLY_PUMP_TOO_SLOW,
    KIND_NO_IRRIGATION_DUE,
    KIND_SOIL_AT_LIMIT,
    KIND_SOIL_LIMIT_AHEAD,
    KIND_UNAVAILABLE,
)


@dataclasses.dataclass(frozen=True)
class WeeklyResult:
    """One run's outcome: its kind, its finished report text, and the numbers.

    kind is one of ALL_KINDS. text is the same report weekly.py always
    printed. The numbers are None when the kind has no such number (for
    example an "unavailable" run has no amount at all).

    net_mm is the modeled soil refill, gross_water_mm what must be pumped
    after efficiency and leaching (both mm), volume_m3 the same water as a
    volume, pump_hours the pump time it needs. deadline is the real moment
    to have applied it by (timezone-aware), and hours_available the hours
    from now until then, or until the end of the forecast when there is no
    deadline. forecast_end is the last moment the weather forecast covers
    (timezone-aware): a card with no deadline is only valid until then, and on
    a run late in the day that can be well under seven days away. made_at is
    when the run was made (timezone-aware), so a card can carry its own date and
    an old printed copy cannot pass for a fresh one.

    daily_net_use_mm is the forecast crop water use minus the rain that soaks
    in, per Casablanca calendar day ("2026-11-02", mm), over the hours this
    amount covers (until the deadline if there is one). It only shapes HOW
    the amount is split over the days (the daily schedule); the amount itself
    stays the engine's. Empty when the run has no such forecast.
    """

    kind: str
    text: str
    net_mm: float | None = None
    gross_water_mm: float | None = None
    volume_m3: float | None = None
    pump_hours: float | None = None
    deadline: datetime | None = None
    hours_available: float | None = None
    forecast_end: datetime | None = None
    made_at: datetime | None = None
    daily_net_use_mm: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in ALL_KINDS:
            raise ValueError(f"unknown result kind: {self.kind!r}")

    @property
    def pump_short_hours(self) -> float | None:
        """Hours the pump is short by, or None when it is fast enough (or unknown)."""
        if self.pump_hours is None or self.hours_available is None:
            return None
        if self.pump_hours > self.hours_available:
            return self.pump_hours - self.hours_available
        return None


def summary_kind(has_deadline: bool, hours_available: float | None, pump_hours: float) -> str:
    """Which of the amount-giving kinds a finished calculation is.

    Mirrors the three outcomes build_summary() in weekly.py prints a
    different warning for, using the same comparisons, so the card and the
    report can never disagree about the situation (tests check this).
    With a deadline: already passed (hours_available below 0), pump too
    slow (more pump hours needed than hours available), or fine. Without
    one, only "pump too slow" or a plain weekly amount are possible.
    """
    if has_deadline:
        if hours_available is not None and hours_available < 0:
            return KIND_DEADLINE_PASSED
        if hours_available is not None and pump_hours > hours_available:
            return KIND_PUMP_TOO_SLOW
        return KIND_IRRIGATE_BY_DEADLINE
    if hours_available is not None and pump_hours > hours_available:
        return KIND_WEEKLY_PUMP_TOO_SLOW
    return KIND_WEEKLY_AMOUNT
