"""Monthly crop coefficient (Kc), fitted per farm.

What Kc is: the crop's water use divided by the weather's "reference" demand
(ET0). Kc 0.5 means the orchard uses half of what a short, well-watered grass
field would use in the same weather. The engine turns weather into crop water
use with it: crop water use = Kc x ET0.

Why fitted per farm: in a backtest on one test orchard (flux-tower data, not
included in this repository) the textbook FAO-56 numbers overshot measured
water use by about a third. One Kc learned from that orchard's own measured
water use halved the daily error (about 1.0 to 0.54 mm/day). That is the reason
for this file. This is a single-site result, not a general claim.

Why monthly, and why pulled toward the farm average: trees use water
differently through the year, so one number per month can follow that. But a
month with few measured days gives a noisy number. In that backtest, raw
monthly Kc (0.57 mm/day) was slightly WORSE than one flat number (0.54). So
each month is pulled toward the farm's own flat Kc, more strongly the fewer
days that month has (see fit_monthly_kc). With lots of data a month speaks for
itself; with little data it falls back to the farm's average.

What data it learns from: daily pairs of (ET0, measured water use ETa). Today
that is a flux tower. Later it can be a farm water meter (applied water when
the soil is not getting wetter or drier over a month) or a soil probe plus the
meter. The fitting only needs the pairs, not where they came from.

Where it plugs in: water_balance.run_forecast_period(monthly_kc=...). When a
block has no fitted Kc, the engine keeps its old FAO-56 dual Kc method, so
nothing changes for blocks without data.

For the FNO/PINO neural operator later: Kc is the crop side (how much water the crop wants). The
soil side is soil_model.py. The FNO/PINO neural operator replaces the soil side and keeps using
this Kc as its demand input, so this file does not change when it arrives.
"""

import argparse
import csv
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# How many "pretend days" of the farm average each month gets before its own
# data (see fit_monthly_kc). 30 = about one month of data, so a month with 30
# measured days is half its own number and half the farm average. Chosen
# before scoring on the test orchard, not tuned.
PRIOR_DAYS = 30.0

# Fewer measured days than this in total, and the fit refuses: not enough to
# trust a farm Kc. About two weeks per season.
MIN_DAYS_FOR_FIT = 60

# A fitted Kc outside this range almost surely means bad data (wrong units,
# ET0 and ETa swapped, a broken sensor), not a real orchard. The fit refuses
# instead of quietly clipping it. FAO-56 Table 12 citrus Kc runs about
# 0.45-0.85; this range is wider on purpose.
PLAUSIBLE_MIN_KC = 0.15
PLAUSIBLE_MAX_KC = 1.5

SOURCE_FITTED = "fitted"
SOURCE_MANUAL = "manual"


class KcFitError(ValueError):
    """The data cannot give a trustworthy Kc. The message says why."""


@dataclass(frozen=True)
class KcObservation:
    """One measured day: the weather's demand (ET0) and what the crop used (ETa), mm."""

    day: date
    et0_mm: float
    eta_mm: float


@dataclass(frozen=True)
class MonthlyKc:
    """Twelve Kc values, January first, plus where they came from.

    days_per_month says how many measured days each month's value is based on
    (all 0 when typed in by hand). flat_kc is the farm's one-number Kc that
    thin months were pulled toward (None when typed in by hand).
    """

    values: tuple[float, ...]
    source: str = SOURCE_MANUAL
    days_per_month: tuple[int, ...] = (0,) * 12
    flat_kc: float | None = None

    def __post_init__(self) -> None:
        if len(self.values) != 12 or len(self.days_per_month) != 12:
            raise ValueError("monthly Kc needs exactly 12 values, January to December")
        for value in self.values:
            if not (math.isfinite(value) and PLAUSIBLE_MIN_KC <= value <= PLAUSIBLE_MAX_KC):
                raise ValueError(
                    f"monthly Kc value {value!r} is outside {PLAUSIBLE_MIN_KC}-"
                    f"{PLAUSIBLE_MAX_KC}; check the numbers"
                )

    def for_month(self, month: int) -> float:
        """Kc for a calendar month, 1 = January."""
        return self.values[month - 1]

    def for_timestamp(self, timestamp: str) -> float:
        """Kc for a weather hour label such as '2026-11-08T14:00'.

        The label is the farm's local clock time (see weather.py), so its month
        is the farm's month. Month changes at midnight local time, which is
        what we want.
        """
        return self.for_month(datetime.fromisoformat(timestamp).month)

    def to_json(self) -> dict[str, object]:
        return {
            "kind": "orchardwater_monthly_kc",
            "version": 1,
            "source": self.source,
            "values": [round(v, 4) for v in self.values],
            "days_per_month": list(self.days_per_month),
            "flat_kc": None if self.flat_kc is None else round(self.flat_kc, 4),
        }


def usable(obs: KcObservation) -> bool:
    """A day can be used for fitting only if both numbers are real and above 0."""
    return (
        math.isfinite(obs.et0_mm)
        and math.isfinite(obs.eta_mm)
        and obs.et0_mm > 0
        and obs.eta_mm > 0
    )


def fit_monthly_kc(
    observations: Iterable[KcObservation], prior_days: float = PRIOR_DAYS
) -> MonthlyKc:
    """Learn one Kc per month from measured days, pulled toward the farm's flat Kc.

    Step 1, the farm's flat Kc: all measured water use added up, divided by all
    ET0 added up. (A ratio of sums, not an average of daily ratios, so a few
    small-ET0 days cannot swing it.)

    Step 2, each month: the month's own ratio of sums, r_m, from its n_m days,
    blended with the flat Kc:

        Kc_m = (n_m x r_m + prior_days x flat) / (n_m + prior_days)

    A month with no data gets exactly the flat Kc. A month with very many days
    gets close to its own ratio. prior_days = 0 turns the pulling off (raw
    monthly ratios; months with no data still get the flat Kc).

    Raises KcFitError if there are fewer than MIN_DAYS_FOR_FIT usable days or a
    result is outside the plausible range.
    """
    if not (math.isfinite(prior_days) and prior_days >= 0):
        raise ValueError("prior_days must be 0 or more")
    days = [obs for obs in observations if usable(obs)]
    if len(days) < MIN_DAYS_FOR_FIT:
        raise KcFitError(
            f"only {len(days)} usable days; at least {MIN_DAYS_FOR_FIT} are needed to fit a Kc"
        )
    flat = sum(d.eta_mm for d in days) / sum(d.et0_mm for d in days)
    _check_plausible(flat, "the farm's flat Kc")

    values = []
    counts = []
    for month in range(1, 13):
        in_month = [d for d in days if d.day.month == month]
        n = len(in_month)
        counts.append(n)
        if n == 0:
            values.append(flat)
            continue
        own = sum(d.eta_mm for d in in_month) / sum(d.et0_mm for d in in_month)
        blended = (n * own + prior_days * flat) / (n + prior_days)
        _check_plausible(blended, f"month {month}'s Kc")
        values.append(blended)
    return MonthlyKc(tuple(values), SOURCE_FITTED, tuple(counts), flat)


def _check_plausible(kc: float, what: str) -> None:
    if not PLAUSIBLE_MIN_KC <= kc <= PLAUSIBLE_MAX_KC:
        raise KcFitError(
            f"{what} came out at {kc:.2f}, outside {PLAUSIBLE_MIN_KC}-{PLAUSIBLE_MAX_KC}. "
            "This usually means bad data: check units (mm/day), that ET0 and water use "
            "are not swapped, and that the sensor was working"
        )


def monthly_kc_from_list(values: Sequence[object]) -> MonthlyKc:
    """Build a MonthlyKc from 12 numbers typed into a farm file. Raises ValueError."""
    numbers = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("monthly_kc must be a list of 12 numbers")
        numbers.append(float(value))
    return MonthlyKc(tuple(numbers))


def read_observations_csv(path: Path) -> list[KcObservation]:
    """Read measured days from a CSV with columns date, et0_mm, eta_mm.

    Rows with an empty et0_mm or eta_mm are skipped (missing data is normal).
    """
    observations = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = {"date", "et0_mm", "eta_mm"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} is missing column(s): {', '.join(sorted(missing))}")
        for row in reader:
            if not row["et0_mm"] or not row["eta_mm"]:
                continue
            observations.append(
                KcObservation(
                    day=date.fromisoformat(row["date"]),
                    et0_mm=float(row["et0_mm"]),
                    eta_mm=float(row["eta_mm"]),
                )
            )
    return observations


def main(argv: list[str] | None = None) -> None:
    """Command line: fit a monthly Kc from a CSV and print it for a farm file."""
    parser = argparse.ArgumentParser(
        prog="python -m orchardwater.kc",
        description=(
            "Fit a monthly Kc from measured days (CSV columns: date, et0_mm, eta_mm) "
            'and print the 12 numbers to paste into a farm file as "monthly_kc".'
        ),
    )
    parser.add_argument("csv", type=Path, help="CSV file with date, et0_mm, eta_mm")
    parser.add_argument(
        "--prior-days",
        type=float,
        default=PRIOR_DAYS,
        help=f"how strongly thin months are pulled to the farm average (default {PRIOR_DAYS:g})",
    )
    parser.add_argument("--out", type=Path, help="also save the result as a JSON file here")
    args = parser.parse_args(argv)

    try:
        kc = fit_monthly_kc(read_observations_csv(args.csv), args.prior_days)
    except (KcFitError, ValueError) as error:
        parser.exit(1, f"Could not fit a Kc: {error}\n")
    names = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    print(f"Farm flat Kc: {kc.flat_kc:.3f}")
    for name, value, n in zip(names, kc.values, kc.days_per_month, strict=True):
        print(f"  {name}  {value:.3f}   ({n} measured days)")
    print('\nFor the farm file:\n  "monthly_kc": ' + json.dumps([round(v, 3) for v in kc.values]))
    if args.out is not None:
        args.out.write_text(json.dumps(kc.to_json(), indent=2) + "\n", encoding="utf-8")
        print(f"\nSaved {args.out}")


if __name__ == "__main__":
    main()
