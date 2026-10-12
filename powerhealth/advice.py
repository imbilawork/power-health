"""Recommendations derived from one snapshot plus the logged trend.

House rule for this module: say what the numbers support and nothing more. No
advice is emitted for a condition that was not measured, and anything resting
on a vendor figure rather than a reading says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import history, probe

# Severity ladder. Status colour is paired with this label everywhere it is
# rendered, so the state never depends on colour alone.
GOOD, INFO, WARNING, SERIOUS = "good", "info", "warning", "serious"


@dataclass
class Note:
    level: str
    title: str
    detail: str


def _health_notes(snap: probe.Snapshot) -> list[Note]:
    notes: list[Note] = []
    health = snap.health_pct
    if health is None:
        return notes

    lost = snap.lost_wh or 0.0
    if health >= 90:
        notes.append(
            Note(
                GOOD,
                f"Battery health {health:.0f}%",
                f"Holding {snap.full_wh:.1f} Wh of its {snap.design_wh:.1f} Wh "
                "design capacity. Nothing to do.",
            )
        )
    elif health >= 80:
        notes.append(
            Note(
                INFO,
                f"Battery health {health:.0f}%",
                f"Down {lost:.1f} Wh from new. Normal for a pack with some age "
                "on it; runtime is noticeably shorter but the battery is fine.",
            )
        )
    elif health >= 65:
        notes.append(
            Note(
                WARNING,
                f"Battery health {health:.0f}%",
                f"{lost:.1f} Wh of the original {snap.design_wh:.1f} Wh is gone. "
                "A full charge now does roughly three-quarters of the work it "
                "did when new. Worth planning a replacement around, not an "
                "emergency.",
            )
        )
    else:
        notes.append(
            Note(
                SERIOUS,
                f"Battery health {health:.0f}%",
                f"Only {snap.full_wh:.1f} Wh of {snap.design_wh:.1f} Wh remains "
                "usable. Expect short, unpredictable runtime and sudden "
                "shutdowns under load. Replacement is the real fix.",
            )
        )
    return notes


def _cycle_notes(snap: probe.Snapshot) -> list[Note]:
    if snap.cycles is None:
        return []
    rated = snap.rated_cycles
    if not rated:
        return [
            Note(
                INFO,
                f"{snap.cycles} charge cycles",
                "No published cycle rating is known for this model, so there is "
                "nothing to compare this against.",
            )
        ]
    share = 100.0 * snap.cycles / rated
    vendor = f"{rated} cycles is the rating published for this model class"
    if share < 80:
        return [
            Note(
                GOOD,
                f"{snap.cycles} of ~{rated} rated cycles",
                f"{share:.0f}% of the rated life. {vendor}.",
            )
        ]
    if share < 100:
        return [
            Note(
                INFO,
                f"{snap.cycles} of ~{rated} rated cycles",
                f"Approaching the rating ({share:.0f}%). {vendor}; a pack past "
                "it still works, it just holds less.",
            )
        ]
    return [
        Note(
            WARNING,
            f"{snap.cycles} cycles, past the ~{rated} rating",
            f"{share:.0f}% of rated life. {vendor}. Capacity loss from here is "
            "expected rather than a fault, and tends to accelerate.",
        )
    ]


def _thermal_notes(snap: probe.Snapshot) -> list[Note]:
    notes: list[Note] = []
    temp = snap.batt_temp_c
    if temp is None:
        return notes
    if temp >= 45:
        notes.append(
            Note(
                SERIOUS,
                f"Battery at {temp:.0f} °C",
                "Sustained heat above about 45 °C is the fastest way to lose "
                "capacity permanently. Take it off charge and off your lap, and "
                "check what is loading the CPU.",
            )
        )
    elif temp >= 37:
        notes.append(
            Note(
                WARNING,
                f"Battery at {temp:.0f} °C",
                "Warm. Heat does more cumulative damage to a lithium pack than "
                "cycle count does, so it is worth easing off sustained load "
                "while charging.",
            )
        )
    elif snap.charging and temp >= 32:
        notes.append(
            Note(
                INFO,
                f"Battery at {temp:.0f} °C while charging",
                "Mild warmth under charge is expected. Only worth acting on "
                "above roughly 37 °C.",
            )
        )
    return notes


def _charging_notes(snap: probe.Snapshot) -> list[Note]:
    notes: list[Note] = []
    thresholds = snap.thresholds or {}
    if not thresholds.get("supported"):
        notes.append(
            Note(
                INFO,
                "No charge limit available",
                "This machine exposes no charge-control threshold in sysfs, "
                "which is normal for an Intel Mac -- the T2 firmware keeps that "
                "to itself. Capping charge at 80% is therefore not possible "
                "from Linux here. The manual equivalent is to avoid leaving it "
                "plugged in at 100% for days at a time.",
            )
        )
    elif thresholds.get("end") and thresholds["end"] < 100:
        notes.append(
            Note(
                GOOD,
                f"Charge capped at {thresholds['end']}%",
                "Holding below a full charge measurably slows capacity loss.",
            )
        )
    if snap.ac and snap.percent and snap.percent >= 99 and not snap.charging:
        notes.append(
            Note(
                INFO,
                "Sitting on mains at full charge",
                "Fine occasionally. If this machine lives on the charger, the "
                "pack spends its life at high voltage, which is the condition "
                "that ages it quickest after heat.",
            )
        )
    return notes


def _profile_notes(snap: probe.Snapshot) -> list[Note]:
    notes: list[Note] = []
    if snap.discharging and snap.profile == "performance":
        notes.append(
            Note(
                WARNING,
                "Performance profile on battery",
                "This raises the CPU energy-performance preference and the "
                "power ceiling. Switching to balanced or power-saver while "
                "unplugged is the single biggest runtime lever available here.",
            )
        )
    if snap.ac and snap.profile == "power-saver":
        notes.append(
            Note(
                INFO,
                "Power-saver profile on mains",
                "Harmless, but you are leaving performance on the table while "
                "plugged in. Omarchy can remember a different profile per "
                "power source.",
            )
        )
    return notes


def _draw_notes(snap: probe.Snapshot, trend: history.Trend) -> list[Note]:
    notes: list[Note] = []
    if snap.discharging and snap.draw_w and snap.draw_w > 20:
        notes.append(
            Note(
                WARNING,
                f"Drawing {snap.draw_w:.1f} W on battery",
                "High for a 15 W-class ultraportable. Check for a runaway "
                "process, an external display, or a browser tab pinning a core.",
            )
        )
    if trend.draw_by_profile and len(trend.draw_by_profile) > 1:
        ranked = sorted(
            (
                (name, info)
                for name, info in trend.draw_by_profile.items()
                if info["median_w"]
            ),
            key=lambda item: item[1]["median_w"],
        )
        if len(ranked) > 1:
            low, high = ranked[0], ranked[-1]
            delta = high[1]["median_w"] - low[1]["median_w"]
            if delta > 0.5:
                notes.append(
                    Note(
                        INFO,
                        "Measured cost of your power profiles",
                        f"On battery this machine has actually drawn a median "
                        f"{low[1]['median_w']:.1f} W on {low[0]} versus "
                        f"{high[1]['median_w']:.1f} W on {high[0]} -- a "
                        f"{delta:.1f} W difference, from your own logs.",
                    )
                )
    return notes


def _trend_notes(trend: history.Trend) -> list[Note]:
    if not trend.enough_data:
        return [
            Note(
                INFO,
                "Wear trend still building",
                trend.reason
                + " The logger samples in the background; this fills in on its "
                "own.",
            )
        ]
    per_month = trend.pct_of_design_per_month or 0.0
    if per_month < -0.05:
        detail = (
            f"Capacity has fallen about {abs(per_month):.2f}% of design per "
            f"month over {trend.span_days:.0f} days of samples "
            f"({trend.first_full_wh:.1f} Wh to {trend.last_full_wh:.1f} Wh)."
        )
        if trend.months_to_70:
            detail += f" At that rate it reaches 70% health in about {trend.months_to_70:.0f} months."
        return [Note(WARNING if abs(per_month) > 0.5 else INFO, "Capacity is declining", detail)]
    if per_month > 0.05:
        return [
            Note(
                INFO,
                "Reported capacity rose",
                f"Measured full-charge capacity went up {per_month:.2f}% of "
                "design per month. That is the firmware recalibrating after a "
                "deep cycle, not the battery healing -- treat the longer trend "
                "as the real signal.",
            )
        ]
    return [
        Note(
            GOOD,
            "Capacity is holding steady",
            f"No measurable decline across {trend.span_days:.0f} days of samples.",
        )
    ]


def _tooling_notes() -> list[Note]:
    import shutil

    missing = [name for name in ("powertop", "tlp") if not shutil.which(name)]
    if not missing:
        return []
    return [
        Note(
            INFO,
            "Optional tools not installed",
            f"{' and '.join(missing)} are absent. Nothing here needs them -- "
            "powertop would add per-device wakeup attribution, and tlp would "
            "add tunables that overlap with power-profiles-daemon (running "
            "both is a known conflict). Install only if you want those.",
        )
    ]


LEVEL_ORDER = {SERIOUS: 0, WARNING: 1, INFO: 2, GOOD: 3}


def build(snap: probe.Snapshot, trend: history.Trend | None = None) -> list[Note]:
    """Every applicable note, most urgent first."""
    trend = trend if trend is not None else history.analyse()
    if not snap.present:
        return [
            Note(
                INFO,
                "No battery detected",
                "This looks like a desktop or a machine with the pack removed. "
                "Live power figures still work; health figures do not apply.",
            )
        ]
    notes: list[Note] = []
    notes += _health_notes(snap)
    notes += _cycle_notes(snap)
    notes += _trend_notes(trend)
    notes += _thermal_notes(snap)
    notes += _charging_notes(snap)
    notes += _profile_notes(snap)
    notes += _draw_notes(snap, trend)
    notes += _tooling_notes()
    return sorted(notes, key=lambda n: LEVEL_ORDER.get(n.level, 9))
