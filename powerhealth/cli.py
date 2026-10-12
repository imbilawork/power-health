"""Command line entry point.

``power-health`` with no arguments opens the window; every other surface is a
subcommand so the same code backs the GUI, the terminal and the bar widget.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, advice, history, probe, render


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="power-health",
        description="Power and battery health for Omarchy. "
        "With no subcommand, opens the window.",
    )
    parser.add_argument("--version", action="version", version=f"power-health {__version__}")
    subs = parser.add_subparsers(dest="command")

    window = subs.add_parser("gui", help="open the graphical window (default)")
    window.add_argument(
        "--page",
        choices=("now", "health", "trend", "advice"),
        help="open straight to one page instead of Now",
    )
    subs.add_parser("report", help="full health, trend and advice report")
    subs.add_parser("status", help="one line, for a prompt or a bar")
    subs.add_parser("advice", help="just the recommendations")
    subs.add_parser("log", help="append one sample to the log (used by the timer)")

    show = subs.add_parser("json", help="machine-readable snapshot and trend")
    show.add_argument("--compact", action="store_true", help="no indentation")

    trend = subs.add_parser("trend", help="what the sample log says so far")
    trend.add_argument("--csv", action="store_true", help="dump the raw sample log")
    return parser


def _json_payload() -> dict:
    snap = probe.snapshot()
    trend = history.analyse()
    return {
        "version": __version__,
        "snapshot": snap.as_dict(),
        "trend": {
            key: value
            for key, value in vars(trend).items()
            # The raw per-sample series is large and already on disk; the
            # derived figures are what a consumer of this JSON wants.
            if key not in ("health_series",)
        },
        "advice": [vars(note) for note in advice.build(snap, trend)],
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "gui"

    if command == "gui":
        try:
            from . import gui
        except (ImportError, ValueError) as exc:
            print(
                f"Cannot start the window: {exc}\n"
                "GTK4 and libadwaita are needed for the graphical mode. "
                "Try 'power-health report' instead.",
                file=sys.stderr,
            )
            return 1
        return gui.main([sys.argv[0]], page=getattr(args, "page", None))

    if command == "report":
        print(render.report(probe.snapshot(), history.analyse()))
        return 0

    if command == "status":
        print(render.status_line(probe.snapshot()))
        return 0

    if command == "advice":
        snap = probe.snapshot()
        for note in advice.build(snap, history.analyse()):
            mark = render.paint(
                f"{render.LEVEL_MARK[note.level]:>4}", render.LEVEL_COLOUR[note.level]
            )
            print(f"{mark}  {note.title}")
            for line in render._wrap(note.detail, 76):
                print(f"      {line}")
        return 0

    if command == "log":
        path = history.append()
        print(f"sample appended to {path}")
        return 0

    if command == "json":
        payload = _json_payload()
        print(json.dumps(payload, default=str, indent=None if args.compact else 2))
        return 0

    if command == "trend":
        if args.csv:
            try:
                with open(history.log_path()) as fh:
                    sys.stdout.write(fh.read())
            except OSError as exc:
                print(f"No log to read: {exc}", file=sys.stderr)
                return 1
            return 0
        trend = history.analyse()
        plural = "" if trend.samples == 1 else "s"
        print(f"{trend.samples} sample{plural} over {trend.span_days:.2f} days")
        if trend.enough_data:
            print(
                f"capacity {trend.first_full_wh:.2f} Wh -> {trend.last_full_wh:.2f} Wh "
                f"({trend.pct_of_design_per_month:+.2f}% of design per month)"
            )
            if trend.months_to_70:
                print(f"reaches 70% health in ~{trend.months_to_70:.0f} months")
        else:
            print(trend.reason)
        if trend.median_discharge_w:
            print(f"median draw on battery {trend.median_discharge_w:.2f} W")
        for name, info in (trend.draw_by_profile or {}).items():
            if info["median_w"]:
                print(f"  {name:<14} {info['median_w']:6.2f} W  ({info['samples']} samples)")
        print(f"log: {history.log_path()}")
        return 0

    parser.print_help()
    return 2
