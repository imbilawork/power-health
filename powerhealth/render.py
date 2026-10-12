"""Terminal rendering shared by the CLI subcommands."""

from __future__ import annotations

import os
import sys

from . import advice, history, probe

LEVEL_COLOUR = {
    advice.GOOD: "32",
    advice.INFO: "36",
    advice.WARNING: "33",
    advice.SERIOUS: "31",
}
LEVEL_MARK = {
    advice.GOOD: "ok",
    advice.INFO: "note",
    advice.WARNING: "warn",
    advice.SERIOUS: "!!",
}


def _colour_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def paint(text: str, code: str) -> str:
    if not _colour_enabled():
        return text
    return f"\033[{code}m{text}\033[0m"


def bar(fraction: float | None, width: int = 18) -> str:
    if fraction is None:
        return "?" * width
    filled = max(0, min(width, round(fraction * width)))
    return "▓" * filled + "░" * (width - filled)


def duration(seconds: float | None) -> str:
    if not seconds:
        return "unknown"
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def status_line(snap: probe.Snapshot) -> str:
    """One line, for a prompt or a bar widget."""
    if not snap.present:
        return "No battery"
    bits = [f"{snap.percent:.0f}%" if snap.percent is not None else "?%"]
    if snap.health_pct is not None:
        bits.append(f"health {snap.health_pct:.0f}%")
    if snap.draw_w is not None:
        bits.append(f"{snap.draw_w:.1f}W")
    if snap.seconds_remaining:
        word = "to full" if snap.remaining_kind == "full" else "left"
        bits.append(f"{duration(snap.seconds_remaining)} {word}")
    return "  ·  ".join(bits)


def report(snap: probe.Snapshot, trend: history.Trend) -> str:
    out: list[str] = []

    def head(title):
        out.append("")
        out.append(paint(title.upper(), "1"))

    out.append(paint("POWER & BATTERY HEALTH", "1"))
    out.append(f"{snap.product or 'unknown machine'}  ·  {snap.model or 'unknown pack'}")

    if not snap.present:
        out.append("")
        out.append("No battery present.")
        return "\n".join(out)

    head("health")
    health = snap.health_pct
    if health is not None:
        colour = "32" if health >= 80 else "33" if health >= 65 else "31"
        out.append(
            f"  {paint(f'{health:5.1f}%', colour)}  {bar(health / 100.0)}  "
            f"of design capacity"
        )
        out.append(
            f"  {snap.full_wh:.2f} Wh usable  of  {snap.design_wh:.2f} Wh when new"
            f"   (lost {snap.lost_wh:.2f} Wh)"
        )
    if snap.cycles is not None:
        rated = f" of ~{snap.rated_cycles} rated" if snap.rated_cycles else ""
        out.append(f"  {snap.cycles} charge cycles{rated}")

    head("right now")
    state = (snap.status or "unknown").lower()
    out.append(
        f"  {state}  ·  {snap.percent:.1f}% charged"
        if snap.percent is not None
        else f"  {state}"
    )
    if snap.percent_of_design is not None:
        out.append(
            f"  carrying {snap.percent_of_design:.1f}% of a new pack's capacity"
        )
    if snap.draw_w is not None:
        out.append(
            f"  {snap.draw_w:.2f} W at the battery terminals"
            f"  ({snap.volts:.2f} V, {snap.amps:.3f} A)"
        )
    if snap.seconds_remaining:
        word = "until full" if snap.remaining_kind == "full" else "of runtime left"
        out.append(f"  {duration(snap.seconds_remaining)} {word}")
    if snap.ac is not None:
        out.append(f"  mains {'connected' if snap.ac else 'disconnected'}")

    head("thermal")
    if snap.batt_temp_c is not None:
        out.append(f"  battery {snap.batt_temp_c:.1f} °C")
    if snap.cpu_temp_c is not None:
        out.append(f"  cpu package {snap.cpu_temp_c:.1f} °C")
    for fan in snap.fans:
        envelope = ""
        if fan["min"] and fan["max"]:
            envelope = f"  (range {fan['min']}–{fan['max']})"
        out.append(f"  {fan['name']} {fan['rpm']} rpm{envelope}")

    head("policy")
    out.append(f"  power profile      {snap.profile or 'unavailable'}")
    out.append(f"  cpu driver         {snap.driver or 'unknown'}")
    out.append(f"  governor           {snap.governor or 'unknown'}")
    out.append(f"  energy preference  {snap.epp or 'unavailable'}")
    thresholds = snap.thresholds or {}
    if thresholds.get("supported"):
        out.append(
            f"  charge limit       {thresholds.get('start') or 0}–{thresholds.get('end')}%"
        )
    else:
        out.append("  charge limit       not supported by this firmware")

    head("trend")
    if trend.enough_data:
        per_month = trend.pct_of_design_per_month or 0.0
        out.append(
            f"  {trend.samples} samples over {trend.span_days:.1f} days"
        )
        out.append(
            f"  capacity {trend.first_full_wh:.2f} Wh → {trend.last_full_wh:.2f} Wh"
            f"  ({per_month:+.2f}% of design per month)"
        )
        if trend.months_to_70:
            out.append(f"  reaches 70% health in ~{trend.months_to_70:.0f} months at this rate")
        if trend.cycles_per_year:
            out.append(f"  cycle pace ~{trend.cycles_per_year:.0f} cycles/year")
    else:
        out.append(f"  {trend.samples} samples over {trend.span_days:.1f} days")
        out.append(f"  {trend.reason}")
    if trend.median_discharge_w:
        out.append(f"  median measured draw on battery {trend.median_discharge_w:.2f} W")
    if trend.runtime_now_h and trend.runtime_new_h:
        lost_min = (trend.runtime_new_h - trend.runtime_now_h) * 60
        out.append(
            f"  runtime at that draw {trend.runtime_now_h:.1f} h now vs "
            f"{trend.runtime_new_h:.1f} h when new  ({lost_min:.0f} min lost)"
        )
    for name, info in (trend.draw_by_profile or {}).items():
        if info["median_w"]:
            out.append(
                f"    {name:<14} {info['median_w']:5.2f} W  ({info['samples']} samples)"
            )

    head("what to do")
    for note in advice.build(snap, trend):
        mark = paint(f"{LEVEL_MARK[note.level]:>4}", LEVEL_COLOUR[note.level])
        out.append(f"  {mark}  {paint(note.title, '1')}")
        for line in _wrap(note.detail, 72):
            out.append(f"        {line}")
    out.append("")
    return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if len(trial) > width and current:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines
