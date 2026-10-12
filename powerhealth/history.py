"""Sample log and wear-trend analysis.

upower keeps its own history under /var/lib/upower, but those files are
root-only (0640 root:root), so an unprivileged app cannot read them. This
module therefore keeps its own append-only CSV, written by a systemd user
timer, and derives the trends from that.

Pure standard library on purpose: this is imported by a timer unit that must
not grow a numpy dependency to append one line.
"""

from __future__ import annotations

import csv
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import probe

FIELDS = [
    "ts",
    "iso",
    "status",
    "ac",
    "percent",
    "now_wh",
    "full_wh",
    "design_wh",
    "cycles",
    "draw_w",
    "volts",
    "amps",
    "batt_temp_c",
    "cpu_temp_c",
    "fan_rpm",
    "profile",
    "epp",
]

DAY = 86400.0

# The log is append-only and sampled every ten minutes, so it grows by roughly
# 17 kB a day. These bounds keep it from growing without limit on a machine
# that runs for years: at that cadence the first trim happens after about four
# years, and it keeps the most recent three.
MAX_SAMPLES = 200_000
KEEP_SAMPLES = 150_000


def data_dir() -> str:
    base = os.environ.get(
        "XDG_DATA_HOME", os.path.join(os.path.expanduser("~"), ".local", "share")
    )
    return os.path.join(base, "power-health")


def log_path() -> str:
    return os.environ.get(
        "POWER_HEALTH_LOG", os.path.join(data_dir(), "samples.csv")
    )


def append(snap: probe.Snapshot | None = None) -> str:
    """Append one sample. Returns the log path."""
    snap = snap or probe.snapshot()
    path = log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fresh = not os.path.exists(path) or os.path.getsize(path) == 0
    row = {
        "ts": int(snap.ts),
        "iso": datetime.fromtimestamp(snap.ts).isoformat(timespec="seconds"),
        "status": snap.status or "",
        "ac": "" if snap.ac is None else int(snap.ac),
        "percent": _fmt(snap.percent, 2),
        "now_wh": _fmt(snap.now_wh, 3),
        "full_wh": _fmt(snap.full_wh, 3),
        "design_wh": _fmt(snap.design_wh, 3),
        "cycles": "" if snap.cycles is None else snap.cycles,
        "draw_w": _fmt(snap.draw_w, 3),
        "volts": _fmt(snap.volts, 3),
        "amps": _fmt(snap.amps, 3),
        "batt_temp_c": _fmt(snap.batt_temp_c, 1),
        "cpu_temp_c": _fmt(snap.cpu_temp_c, 1),
        "fan_rpm": snap.fans[0]["rpm"] if snap.fans else "",
        "profile": snap.profile or "",
        "epp": snap.epp or "",
    }
    with open(path, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if fresh:
            writer.writeheader()
        writer.writerow(row)
    _trim(path)
    return path


def _trim(path: str) -> None:
    """Keep the log bounded, oldest rows first to go.

    Counting lines is cheap next to the cost of letting this file grow for
    years; the rewrite only happens on the rare sample that crosses the bound.
    """
    try:
        with open(path, newline="") as fh:
            lines = fh.readlines()
    except OSError:
        return
    if len(lines) - 1 <= MAX_SAMPLES:
        return
    header, body = lines[0], lines[1:]
    kept = body[-KEEP_SAMPLES:]
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", newline="") as fh:
            fh.write(header)
            fh.writelines(kept)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _fmt(value, places: int) -> str:
    return "" if value is None else f"{value:.{places}f}"


def _float(row: dict, key: str):
    raw = (row.get(key) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def read_samples(path: str | None = None) -> list[dict]:
    """Parse the log, tolerating rows written by an older field set."""
    path = path or log_path()
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    try:
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                ts = _float(row, "ts")
                if ts is None:
                    continue
                out.append(
                    {
                        "ts": ts,
                        "status": (row.get("status") or "").strip(),
                        "ac": (row.get("ac") or "").strip(),
                        "percent": _float(row, "percent"),
                        "now_wh": _float(row, "now_wh"),
                        "full_wh": _float(row, "full_wh"),
                        "design_wh": _float(row, "design_wh"),
                        "cycles": _float(row, "cycles"),
                        "draw_w": _float(row, "draw_w"),
                        "batt_temp_c": _float(row, "batt_temp_c"),
                        "cpu_temp_c": _float(row, "cpu_temp_c"),
                        "profile": (row.get("profile") or "").strip(),
                    }
                )
    except OSError:
        return []
    out.sort(key=lambda r: r["ts"])
    return out


# --------------------------------------------------------------------------- #
# Statistics (small, explicit, no dependencies)
# --------------------------------------------------------------------------- #

def median(values: list[float]) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return clean[mid]
    return (clean[mid - 1] + clean[mid]) / 2.0


def _linfit(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Least-squares slope and intercept, or None if x never varies."""
    n = len(points)
    if n < 2:
        return None
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    if sxx <= 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    slope = sxy / sxx
    return slope, mean_y - slope * mean_x


@dataclass
class Trend:
    """What the log can honestly say about this battery's decline."""

    samples: int = 0
    span_days: float = 0.0
    enough_data: bool = False
    reason: str = ""

    design_wh: float | None = None
    first_full_wh: float | None = None
    last_full_wh: float | None = None
    wh_per_day: float | None = None
    pct_of_design_per_month: float | None = None
    months_to_70: float | None = None
    months_to_50: float | None = None

    first_cycles: float | None = None
    last_cycles: float | None = None
    cycles_per_day: float | None = None
    cycles_per_year: float | None = None

    daily_full_wh: list = field(default_factory=list)  # (ts, wh) one per day
    health_series: list = field(default_factory=list)  # (ts, health %)
    draw_by_profile: dict = field(default_factory=dict)
    median_discharge_w: float | None = None
    runtime_now_h: float | None = None
    runtime_new_h: float | None = None


# A week is the shortest window in which a real capacity change can be told
# apart from the firmware's own recalibration noise, which moves charge_full by
# a few hundred mWh after a deep cycle.
MIN_SPAN_DAYS = 7.0
MIN_DAYS = 3


def analyse(samples: list[dict] | None = None) -> Trend:
    samples = read_samples() if samples is None else samples
    trend = Trend(samples=len(samples))
    if not samples:
        trend.reason = "No samples logged yet."
        return trend

    trend.span_days = (samples[-1]["ts"] - samples[0]["ts"]) / DAY
    capacity_rows = [s for s in samples if s["full_wh"] and s["design_wh"]]
    if not capacity_rows:
        trend.reason = "No capacity readings in the log."
        return trend

    trend.design_wh = capacity_rows[-1]["design_wh"]
    trend.first_full_wh = capacity_rows[0]["full_wh"]
    trend.last_full_wh = capacity_rows[-1]["full_wh"]
    trend.health_series = [
        (s["ts"], 100.0 * s["full_wh"] / s["design_wh"]) for s in capacity_rows
    ]

    # Collapse to one median reading per calendar day. charge_full only moves
    # when the firmware recalibrates, so sub-daily points add noise, not signal.
    by_day: dict[int, list[float]] = {}
    for row in capacity_rows:
        by_day.setdefault(int(row["ts"] // DAY), []).append(row["full_wh"])
    trend.daily_full_wh = [
        ((day + 0.5) * DAY, median(values))
        for day, values in sorted(by_day.items())
    ]

    cycle_rows = [s for s in samples if s["cycles"] is not None]
    if cycle_rows:
        trend.first_cycles = cycle_rows[0]["cycles"]
        trend.last_cycles = cycle_rows[-1]["cycles"]
        cycle_span = (cycle_rows[-1]["ts"] - cycle_rows[0]["ts"]) / DAY
        if cycle_span >= 1.0:
            per_day = (trend.last_cycles - trend.first_cycles) / cycle_span
            trend.cycles_per_day = per_day
            trend.cycles_per_year = per_day * 365.0

    # Real measured draw while on battery, per profile. This is the figure a
    # runtime estimate should use -- not a datasheet number.
    discharge = [
        s
        for s in samples
        if s["status"].lower() == "discharging" and s["draw_w"] and s["draw_w"] > 0.5
    ]
    trend.median_discharge_w = median([s["draw_w"] for s in discharge])
    per_profile: dict[str, list[float]] = {}
    for row in discharge:
        per_profile.setdefault(row["profile"] or "unknown", []).append(row["draw_w"])
    trend.draw_by_profile = {
        name: {"median_w": median(values), "samples": len(values)}
        for name, values in sorted(per_profile.items())
    }
    if trend.median_discharge_w:
        if trend.last_full_wh:
            trend.runtime_now_h = trend.last_full_wh / trend.median_discharge_w
        if trend.design_wh:
            trend.runtime_new_h = trend.design_wh / trend.median_discharge_w

    if len(trend.daily_full_wh) < MIN_DAYS or trend.span_days < MIN_SPAN_DAYS:
        trend.reason = (
            f"Need about {MIN_SPAN_DAYS:.0f} days of samples to separate real "
            f"wear from recalibration noise; have "
            f"{trend.span_days:.1f} days across {len(trend.daily_full_wh)} day(s)."
        )
        return trend

    fit = _linfit([(ts / DAY, wh) for ts, wh in trend.daily_full_wh])
    if fit is None:
        trend.reason = "Capacity has not moved yet."
        return trend

    slope, _ = fit
    trend.enough_data = True
    trend.wh_per_day = slope
    trend.pct_of_design_per_month = 100.0 * slope * 30.0 / trend.design_wh

    # Only project a date when capacity is actually falling.
    health_now = 100.0 * trend.last_full_wh / trend.design_wh
    per_month = -trend.pct_of_design_per_month
    if per_month > 0.01:
        if health_now > 70:
            trend.months_to_70 = (health_now - 70) / per_month
        if health_now > 50:
            trend.months_to_50 = (health_now - 50) / per_month
    return trend
