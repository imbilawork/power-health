"""Cairo chart drawing for the GTK window.

Deliberately plain: one series per chart, no legend (the heading names the
series), a recessive grid, a 2px line, and direct labels only at the ends
rather than a number on every point. Colours arrive from the caller as theme
tokens so light and dark are each chosen against their own surface instead of
one being an alpha-flip of the other.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Theme:
    """Resolved theme colours, as (r, g, b, a) in 0..1."""

    ink: tuple
    ink_dim: tuple
    grid: tuple
    accent: tuple
    surface: tuple
    fill: tuple


def _set(cr, rgba):
    cr.set_source_rgba(*rgba)


def _text(cr, x, y, label, rgba, size=11.0, anchor="left"):
    _set(cr, rgba)
    cr.set_font_size(size)
    extents = cr.text_extents(label)
    if anchor == "right":
        x -= extents.width
    elif anchor == "center":
        x -= extents.width / 2.0
    cr.move_to(x, y)
    cr.show_text(label)


def empty_state(cr, width, height, theme: Theme, message: str):
    """What a chart shows before it has anything to show."""
    _set(cr, theme.grid)
    cr.set_line_width(1.0)
    cr.set_dash([4.0, 4.0])
    cr.rectangle(8.5, 8.5, width - 17, height - 17)
    cr.stroke()
    cr.set_dash([])

    cr.set_font_size(12.0)
    words, lines, current = message.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if cr.text_extents(trial).width > width - 48 and current:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    lines = lines[:4]
    start = height / 2.0 - (len(lines) - 1) * 8.0
    for index, line in enumerate(lines):
        _text(cr, width / 2.0, start + index * 16.0, line, theme.ink_dim, 12.0, "center")


def line_chart(
    cr,
    width: int,
    height: int,
    points: list[tuple[float, float]],
    theme: Theme,
    *,
    y_label: str = "",
    y_suffix: str = "%",
    value_fmt: str = "{:.1f}",
    fill: bool = True,
    y_pad_frac: float = 0.18,
    y_floor: float | None = None,
    y_ceil: float | None = None,
    empty_message: str = "No data yet.",
):
    """Draw one time series. ``points`` is [(unix_ts, value), ...] ascending."""
    if len(points) < 2:
        empty_state(cr, width, height, theme, empty_message)
        return

    pad_left, pad_right = 46.0, 14.0
    pad_top, pad_bottom = 16.0, 26.0
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    if plot_w <= 10 or plot_h <= 10:
        return

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    # A line chart may use a truncated y-axis -- both ends are always labelled
    # so the truncation is visible rather than implied.
    span = y_max - y_min
    if span < 1e-9:
        pad = max(abs(y_max) * 0.05, 0.5)
    else:
        pad = span * y_pad_frac
    lo, hi = y_min - pad, y_max + pad
    if y_floor is not None:
        lo = max(lo, y_floor)
    if y_ceil is not None:
        hi = min(hi, y_ceil)
    if hi - lo < 1e-9:
        hi = lo + 1.0

    def sx(value):
        if x_max == x_min:
            return pad_left + plot_w / 2.0
        return pad_left + (value - x_min) / (x_max - x_min) * plot_w

    def sy(value):
        return pad_top + plot_h - (value - lo) / (hi - lo) * plot_h

    # Grid: three hairlines, recessive, with the axis values on the left.
    cr.set_line_width(1.0)
    for frac in (0.0, 0.5, 1.0):
        value = lo + (hi - lo) * frac
        y = sy(value)
        _set(cr, theme.grid)
        cr.move_to(pad_left, math.floor(y) + 0.5)
        cr.line_to(pad_left + plot_w, math.floor(y) + 0.5)
        cr.stroke()
        _text(
            cr,
            pad_left - 8,
            y + 3.5,
            f"{value:.1f}{y_suffix}",
            theme.ink_dim,
            10.0,
            "right",
        )

    if fill:
        gradient_top = (theme.fill[0], theme.fill[1], theme.fill[2], theme.fill[3])
        _set(cr, gradient_top)
        cr.move_to(sx(points[0][0]), sy(points[0][1]))
        for ts, value in points[1:]:
            cr.line_to(sx(ts), sy(value))
        cr.line_to(sx(points[-1][0]), pad_top + plot_h)
        cr.line_to(sx(points[0][0]), pad_top + plot_h)
        cr.close_path()
        cr.fill()

    _set(cr, theme.accent)
    cr.set_line_width(2.0)
    cr.set_line_join(1)  # round
    cr.move_to(sx(points[0][0]), sy(points[0][1]))
    for ts, value in points[1:]:
        cr.line_to(sx(ts), sy(value))
    cr.stroke()

    # End markers, ringed in the surface colour so they read on top of the line.
    for ts, value in (points[0], points[-1]):
        x, y = sx(ts), sy(value)
        _set(cr, theme.surface)
        cr.arc(x, y, 5.0, 0, 2 * math.pi)
        cr.fill()
        _set(cr, theme.accent)
        cr.arc(x, y, 3.5, 0, 2 * math.pi)
        cr.fill()

    # Direct labels at the ends only.
    first_value = value_fmt.format(points[0][1]) + y_suffix
    last_value = value_fmt.format(points[-1][1]) + y_suffix
    _text(cr, sx(points[-1][0]), sy(points[-1][1]) - 10, last_value, theme.ink, 11.0, "right")

    date_fmt = "%d %b" if (x_max - x_min) > 86400 * 2 else "%H:%M"
    _text(
        cr,
        pad_left,
        height - 8,
        datetime.fromtimestamp(x_min).strftime(date_fmt),
        theme.ink_dim,
        10.0,
    )
    _text(
        cr,
        pad_left + plot_w,
        height - 8,
        datetime.fromtimestamp(x_max).strftime(date_fmt),
        theme.ink_dim,
        10.0,
        "right",
    )
    if y_label:
        _text(cr, pad_left, pad_top - 4, y_label, theme.ink_dim, 10.0)
    return first_value, last_value
