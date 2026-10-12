"""GTK4 + libadwaita window.

Styling is left to libadwaita on purpose: no hardcoded colours, no custom
stylesheet beyond two spacing helpers. That is what makes the window follow the
Omarchy theme, the system accent and light/dark without being told about any of
them. Chart colours are resolved from the live widget state for the same reason.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

from . import advice, chart, history, probe  # noqa: E402
from .render import duration  # noqa: E402

APP_ID = "org.omarchy.PowerHealth"
REFRESH_SECONDS = 2

# Severity is always carried by a word as well as a colour.
LEVEL_STYLE = {
    advice.GOOD: ("success", "Good"),
    advice.INFO: ("accent", "Note"),
    advice.WARNING: ("warning", "Watch"),
    advice.SERIOUS: ("error", "Act"),
}
LEVEL_ICON = {
    advice.GOOD: "emblem-ok-symbolic",
    advice.INFO: "dialog-information-symbolic",
    advice.WARNING: "dialog-warning-symbolic",
    advice.SERIOUS: "dialog-error-symbolic",
}

# Status colours are stated explicitly rather than inherited from the accent,
# because the accent is whatever the user's theme sets and carries no meaning.
# These match libadwaita's own status palette and are legible on both surfaces.
# The verdict word beside the bar always says the same thing, so severity is
# never carried by colour alone.
EXTRA_CSS = b"""
.ph-hero { font-size: 46pt; font-weight: 300; }
.ph-tile { padding: 14px; }
.ph-chart { min-height: 170px; }
levelbar.ph-health block.ph-worn { background-color: #e5a50a; }
levelbar.ph-health block.ph-spent { background-color: #e01b24; }
levelbar.ph-health block.ph-sound { background-color: #2ec27e; }
"""


def _label(text, *classes, xalign=0.0, wrap=False, selectable=False):
    label = Gtk.Label(label=text, xalign=xalign)
    label.set_wrap(wrap)
    if wrap:
        label.set_wrap_mode(2)  # word-char
    label.set_selectable(selectable)
    for name in classes:
        label.add_css_class(name)
    return label


def _tile(title: str) -> tuple[Gtk.Widget, Gtk.Label, Gtk.Label]:
    """A stat tile: small caption, big value, optional footnote."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.add_css_class("card")
    box.add_css_class("ph-tile")
    box.set_hexpand(True)
    caption = _label(title.upper(), "caption", "dim-label")
    value = _label("—", "title-2", "numeric")
    note = _label("", "caption", "dim-label", wrap=True)
    box.append(caption)
    box.append(value)
    box.append(note)
    return box, value, note


def _row(title: str, subtitle: str = "") -> tuple[Adw.ActionRow, Gtk.Label]:
    row = Adw.ActionRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    value = _label("—", "numeric", "dim-label")
    row.add_suffix(value)
    return row, value


def _group(title: str, description: str = "") -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(title=title)
    if description:
        group.set_description(description)
    return group


class ChartArea(Gtk.DrawingArea):
    """A themed line chart whose colours re-resolve on every draw."""

    def __init__(self, heading: str, empty_message: str, **kwargs):
        super().__init__()
        self.add_css_class("ph-chart")
        self.set_hexpand(True)
        self.heading = heading
        self.empty_message = empty_message
        self.options = kwargs
        self.points: list[tuple[float, float]] = []
        self.set_draw_func(self._draw)

    def set_points(self, points):
        self.points = list(points or [])
        self.queue_draw()

    def _theme(self) -> chart.Theme:
        manager = Adw.StyleManager.get_default()
        dark = manager.get_dark()
        ink = self.get_color()  # follows the theme's foreground exactly
        base = (ink.red, ink.green, ink.blue)

        accent = (0.21, 0.52, 0.89)
        try:
            rgba = manager.get_accent_color_rgba()
            accent = (rgba.red, rgba.green, rgba.blue)
        except Exception:
            try:
                value = manager.get_accent_color()
                rgba = value.to_standalone_rgba(dark)
                accent = (rgba.red, rgba.green, rgba.blue)
            except Exception:
                pass

        surface = (0.12, 0.12, 0.13) if dark else (1.0, 1.0, 1.0)
        return chart.Theme(
            ink=(*base, 0.95),
            ink_dim=(*base, 0.55),
            grid=(*base, 0.14 if dark else 0.10),
            accent=(*accent, 1.0),
            surface=(*surface, 1.0),
            # Dark mode gets its own fill weight rather than inheriting light's.
            fill=(*accent, 0.22 if dark else 0.14),
        )

    def _draw(self, _area, cr, width, height):
        chart.line_chart(
            cr,
            width,
            height,
            self.points,
            self._theme(),
            y_label=self.heading,
            empty_message=self.empty_message,
            **self.options,
        )


class PowerHealthWindow(Adw.ApplicationWindow):
    def __init__(self, page: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Power & Battery Health")
        self.set_default_size(620, 760)

        self._suppress_profile_signal = False
        self._profile_buttons: dict[str, Gtk.ToggleButton] = {}

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("Re-read the hardware now")
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_: self.refresh())
        header.pack_end(refresh)

        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        self.stack.add_titled_with_icon(
            self._build_now(), "now", "Now", "battery-good-symbolic"
        )
        self.stack.add_titled_with_icon(
            self._build_health(), "health", "Health", "emblem-system-symbolic"
        )
        self.stack.add_titled_with_icon(
            self._build_trend(), "trend", "Trend", "view-list-ordered-symbolic"
        )
        self.stack.add_titled_with_icon(
            self._build_advice(), "advice", "Advice", "dialog-information-symbolic"
        )

        if page:
            self.stack.set_visible_child_name(page)

        self.refresh()
        self._timer = GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)
        self.connect("close-request", self._on_close)

    # ---------------------------------------------------------------- pages --

    def _build_now(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        hero_group = _group("")
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hero.set_margin_top(8)
        hero.set_margin_bottom(8)
        self.now_percent = _label("—", "ph-hero", "numeric", xalign=0.5)
        self.now_state = _label("", "title-4", "dim-label", xalign=0.5)
        self.now_remaining = _label("", "body", "dim-label", xalign=0.5, wrap=True)
        hero.append(self.now_percent)
        hero.append(self.now_state)
        hero.append(self.now_remaining)
        hero_group.add(hero)
        page.add(hero_group)

        tiles_group = _group("")
        tiles = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        tiles.set_homogeneous(True)
        draw_tile, self.tile_draw, self.tile_draw_note = _tile("Power draw")
        batt_tile, self.tile_batt, self.tile_batt_note = _tile("Battery temp")
        cpu_tile, self.tile_cpu, self.tile_cpu_note = _tile("CPU package")
        for widget in (draw_tile, batt_tile, cpu_tile):
            tiles.append(widget)
        tiles_group.add(tiles)
        page.add(tiles_group)

        live = _group("Live readings")
        self.row_terminal, self.val_terminal = _row(
            "At the battery terminals", "Volts and amps the pack reports"
        )
        self.row_mains, self.val_mains = _row("Mains adapter")
        self.row_fan, self.val_fan = _row("Fan")
        for row in (self.row_terminal, self.row_mains, self.row_fan):
            live.add(row)
        page.add(live)

        profile_group = _group(
            "Power profile",
            "Switched through Omarchy, so the choice is remembered separately "
            "for mains and battery.",
        )
        profile_row = Adw.ActionRow(title="Profile")
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        buttons.add_css_class("linked")
        buttons.set_valign(Gtk.Align.CENTER)
        group_leader = None
        for name in probe.available_profiles():
            button = Gtk.ToggleButton(label=name.replace("-", " "))
            if group_leader is None:
                group_leader = button
            else:
                button.set_group(group_leader)
            button.connect("toggled", self._on_profile_toggled, name)
            self._profile_buttons[name] = button
            buttons.append(button)
        if not self._profile_buttons:
            buttons.append(_label("power-profiles-daemon unavailable", "dim-label"))
        profile_row.add_suffix(buttons)
        profile_group.add(profile_row)

        self.row_epp, self.val_epp = _row(
            "Energy preference", "Kernel hint, set by the profile above"
        )
        self.row_gov, self.val_gov = _row("Scaling governor")
        profile_group.add(self.row_epp)
        profile_group.add(self.row_gov)
        page.add(profile_group)
        return page

    def _build_health(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        hero_group = _group("")
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        hero.set_margin_top(8)
        hero.set_margin_bottom(8)
        self.health_percent = _label("—", "ph-hero", "numeric", xalign=0.5)
        self.health_verdict = _label("", "title-4", xalign=0.5)
        self.health_bar = Gtk.LevelBar()
        self.health_bar.set_min_value(0)
        self.health_bar.set_max_value(100)
        self.health_bar.add_css_class("ph-health")
        # A LevelBar block takes the class of the lowest offset it still fits
        # under, so these read as bands: spent < 65 <= worn < 80 <= sound.
        self.health_bar.add_offset_value("ph-spent", 65)
        self.health_bar.add_offset_value("ph-worn", 80)
        self.health_bar.add_offset_value("ph-sound", 100)
        self.health_bar.set_margin_top(6)
        self.health_bar.set_margin_bottom(6)
        self.health_detail = _label("", "body", "dim-label", xalign=0.5, wrap=True)
        hero.append(self.health_percent)
        hero.append(self.health_verdict)
        hero.append(self.health_bar)
        hero.append(self.health_detail)
        hero_group.add(hero)
        page.add(hero_group)

        capacity = _group(
            "Capacity",
            "Watt-hours are converted at the pack's design voltage, the same "
            "convention upower uses, so these agree with the rest of the desktop.",
        )
        self.row_usable, self.val_usable = _row("Usable when full")
        self.row_design, self.val_design = _row("Design capacity")
        self.row_lost, self.val_lost = _row("Capacity lost")
        self.row_of_design, self.val_of_design = _row(
            "Charge now, against a new pack",
            "What this charge would be worth on the day it shipped",
        )
        for row in (self.row_usable, self.row_design, self.row_lost, self.row_of_design):
            capacity.add(row)
        page.add(capacity)

        wear = _group("Wear")
        self.row_cycles, self.val_cycles = _row("Charge cycles")
        self.row_limit, self.val_limit = _row(
            "Charge limit", "Caps charging below 100% to slow ageing"
        )
        self.row_tech, self.val_tech = _row("Chemistry")
        self.row_pack, self.val_pack = _row("Pack")
        for row in (self.row_cycles, self.row_limit, self.row_tech, self.row_pack):
            wear.add(row)
        page.add(wear)
        return page

    def _build_trend(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        chart_group = _group(
            "Battery health over time",
            "One point per day, taken from the background sample log.",
        )
        self.health_chart = ChartArea(
            "health, % of design",
            "No trend yet. The background logger samples your battery on a "
            "timer; this chart fills in once there are a few days of readings.",
            y_suffix="%",
            value_fmt="{:.1f}",
            y_floor=0.0,
            y_ceil=100.0,
        )
        chart_group.add(self.health_chart)
        page.add(chart_group)

        draw_group = _group(
            "Power draw while on battery",
            "Every logged discharge reading, newest at the right.",
        )
        self.draw_chart = ChartArea(
            "watts",
            "No discharge samples logged yet. Unplug for a while and this "
            "chart shows what the machine actually costs to run.",
            y_suffix=" W",
            value_fmt="{:.1f}",
            y_floor=0.0,
        )
        draw_group.add(self.draw_chart)
        page.add(draw_group)

        stats = _group("What the log says")
        self.row_samples, self.val_samples = _row("Samples collected")
        self.row_rate, self.val_rate = _row(
            "Decline", "Change in full-charge capacity per month"
        )
        self.row_project, self.val_project = _row(
            "Projection", "When this reaches 70% health at the current rate"
        )
        self.row_cycle_pace, self.val_cycle_pace = _row("Cycle pace")
        self.row_median, self.val_median = _row(
            "Median draw on battery", "From your own discharge samples"
        )
        self.row_runtime, self.val_runtime = _row(
            "Runtime now vs new", "At that measured draw"
        )
        for row in (
            self.row_samples,
            self.row_rate,
            self.row_project,
            self.row_cycle_pace,
            self.row_median,
            self.row_runtime,
        ):
            stats.add(row)
        page.add(stats)

        self.profile_cost_group = _group(
            "Measured cost of each profile",
            "Median watts drawn on battery, per power profile, from your logs.",
        )
        page.add(self.profile_cost_group)
        self._profile_cost_rows: list[Adw.ActionRow] = []

        logger = _group("Sample log")
        self.row_logger, self.val_logger = _row("Background logger")
        logger.add(self.row_logger)
        log_row = Adw.ActionRow(
            title="Log a sample now",
            subtitle="Adds one reading immediately instead of waiting",
        )
        button = Gtk.Button(label="Sample")
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", self._on_log_now)
        log_row.add_suffix(button)
        log_row.set_activatable_widget(button)
        logger.add(log_row)
        self.row_logpath, self.val_logpath = _row("Log file")
        self.val_logpath.set_selectable(True)
        logger.add(self.row_logpath)
        page.add(logger)
        return page

    def _build_advice(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        self.advice_group = _group(
            "What to do about it",
            "Derived from this machine's own readings. Anything resting on a "
            "published rating rather than a measurement says so.",
        )
        page.add(self.advice_group)
        self._advice_rows: list[Adw.ActionRow] = []
        return page

    # ------------------------------------------------------------- behaviour --

    def _on_close(self, *_args):
        if getattr(self, "_timer", None):
            GLib.source_remove(self._timer)
            self._timer = None
        return False

    def _tick(self):
        self.refresh()
        return GLib.SOURCE_CONTINUE

    def _on_profile_toggled(self, button, name):
        if self._suppress_profile_signal or not button.get_active():
            return
        ok, message = probe.set_power_profile(name)
        if ok:
            self.toasts.add_toast(Adw.Toast(title=f"Power profile set to {name}"))
        else:
            self.toasts.add_toast(Adw.Toast(title=f"Could not switch profile: {message}"))
        self.refresh()

    def _on_log_now(self, _button):
        try:
            history.append()
        except OSError as exc:
            self.toasts.add_toast(Adw.Toast(title=f"Could not write the log: {exc}"))
            return
        self.toasts.add_toast(Adw.Toast(title="Sample logged"))
        self.refresh()

    def _logger_state(self) -> str:
        import shutil
        import subprocess

        if not shutil.which("systemctl"):
            return "systemd not available"
        try:
            done = subprocess.run(
                ["systemctl", "--user", "is-active", "power-health-logger.timer"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        state = done.stdout.strip() or "inactive"
        return "running" if state == "active" else state

    # --------------------------------------------------------------- refresh --

    def refresh(self):
        snap = probe.snapshot()
        trend = history.analyse()
        self._refresh_now(snap)
        self._refresh_health(snap)
        self._refresh_trend(trend)
        self._refresh_advice(snap, trend)

    def _refresh_now(self, snap: probe.Snapshot):
        if not snap.present:
            self.now_percent.set_text("—")
            self.now_state.set_text("No battery detected")
            self.now_remaining.set_text("")
        else:
            self.now_percent.set_text(
                f"{snap.percent:.0f}%" if snap.percent is not None else "—"
            )
            self.now_state.set_text((snap.status or "unknown").capitalize())
            if snap.seconds_remaining:
                word = "until full" if snap.remaining_kind == "full" else "of runtime left"
                self.now_remaining.set_text(f"{duration(snap.seconds_remaining)} {word}")
            elif snap.ac:
                self.now_remaining.set_text("On mains")
            else:
                self.now_remaining.set_text("")

        if snap.draw_w is not None:
            self.tile_draw.set_text(f"{snap.draw_w:.1f} W")
            self.tile_draw_note.set_text(
                "measured at the pack" if snap.draw_is_measured else "estimated"
            )
        else:
            self.tile_draw.set_text("—")
            self.tile_draw_note.set_text("not reported")

        if snap.batt_temp_c is not None:
            self.tile_batt.set_text(f"{snap.batt_temp_c:.1f} °C")
            self.tile_batt_note.set_text(
                "hot — ages the pack" if snap.batt_temp_c >= 37 else "normal range"
            )
        else:
            self.tile_batt.set_text("—")
            self.tile_batt_note.set_text("not reported")

        if snap.cpu_temp_c is not None:
            self.tile_cpu.set_text(f"{snap.cpu_temp_c:.0f} °C")
            self.tile_cpu_note.set_text("package sensor")
        else:
            self.tile_cpu.set_text("—")
            self.tile_cpu_note.set_text("not reported")

        if snap.volts is not None and snap.amps is not None:
            self.val_terminal.set_text(f"{snap.volts:.2f} V  ·  {snap.amps:.3f} A")
        else:
            self.val_terminal.set_text("not reported")
        self.val_mains.set_text(
            "unknown" if snap.ac is None else ("connected" if snap.ac else "disconnected")
        )
        if snap.fans:
            fan = snap.fans[0]
            envelope = f"  (up to {fan['max']})" if fan.get("max") else ""
            self.val_fan.set_text(f"{fan['rpm']} rpm{envelope}")
        else:
            self.val_fan.set_text("none reported")

        self._suppress_profile_signal = True
        for name, button in self._profile_buttons.items():
            button.set_active(name == snap.profile)
        self._suppress_profile_signal = False

        self.val_epp.set_text(snap.epp or "unavailable")
        self.val_gov.set_text(snap.governor or "unavailable")

    def _refresh_health(self, snap: probe.Snapshot):
        health = snap.health_pct
        for style, _ in LEVEL_STYLE.values():
            self.health_verdict.remove_css_class(style)
        if health is None:
            self.health_percent.set_text("—")
            self.health_verdict.set_text("No health data")
            self.health_detail.set_text(
                "This machine does not report a design capacity, so wear cannot "
                "be calculated."
            )
            self.health_bar.set_value(0)
        else:
            self.health_percent.set_text(f"{health:.0f}%")
            self.health_bar.set_value(max(0.0, min(100.0, health)))
            if health >= 90:
                level = advice.GOOD
                verdict = "Healthy"
            elif health >= 80:
                level = advice.INFO
                verdict = "Lightly worn"
            elif health >= 65:
                level = advice.WARNING
                verdict = "Worn"
            else:
                level = advice.SERIOUS
                verdict = "Heavily worn"
            style, _ = LEVEL_STYLE[level]
            self.health_verdict.add_css_class(style)
            self.health_verdict.set_text(verdict)
            self.health_detail.set_text(
                f"A full charge holds {snap.full_wh:.1f} Wh, against "
                f"{snap.design_wh:.1f} Wh when the pack was new."
            )

        self.val_usable.set_text(
            f"{snap.full_wh:.2f} Wh" if snap.full_wh else "unknown"
        )
        self.val_design.set_text(
            f"{snap.design_wh:.2f} Wh" if snap.design_wh else "unknown"
        )
        self.val_lost.set_text(
            f"{snap.lost_wh:.2f} Wh" if snap.lost_wh is not None else "unknown"
        )
        self.val_of_design.set_text(
            f"{snap.percent_of_design:.1f}%"
            if snap.percent_of_design is not None
            else "unknown"
        )

        if snap.cycles is None:
            self.val_cycles.set_text("not reported")
        elif snap.rated_cycles:
            self.val_cycles.set_text(f"{snap.cycles}  of ~{snap.rated_cycles} rated")
        else:
            self.val_cycles.set_text(str(snap.cycles))

        thresholds = snap.thresholds or {}
        if thresholds.get("supported") and thresholds.get("end"):
            start = thresholds.get("start") or 0
            self.val_limit.set_text(f"{start}–{thresholds['end']}%")
        else:
            self.val_limit.set_text("not supported by this firmware")
        self.val_tech.set_text(snap.technology or "unknown")
        self.val_pack.set_text(
            " ".join(filter(None, [snap.manufacturer, snap.model])) or "unknown"
        )

    def _refresh_trend(self, trend: history.Trend):
        self.health_chart.set_points(
            [
                (ts, 100.0 * wh / trend.design_wh)
                for ts, wh in trend.daily_full_wh
                if wh and trend.design_wh
            ]
        )
        samples = history.read_samples()
        self.draw_chart.set_points(
            [
                (row["ts"], row["draw_w"])
                for row in samples
                if row["status"].lower() == "discharging" and row["draw_w"]
            ]
        )

        plural = "" if trend.samples == 1 else "s"
        self.val_samples.set_text(
            f"{trend.samples} sample{plural} over {trend.span_days:.1f} days"
        )
        if trend.enough_data and trend.pct_of_design_per_month is not None:
            self.val_rate.set_text(f"{trend.pct_of_design_per_month:+.2f}% of design/month")
        else:
            self.val_rate.set_text("still collecting")
        self.row_rate.set_subtitle(
            trend.reason if not trend.enough_data else
            f"{trend.first_full_wh:.2f} Wh → {trend.last_full_wh:.2f} Wh"
        )
        if trend.months_to_70:
            self.val_project.set_text(f"~{trend.months_to_70:.0f} months to 70%")
        elif trend.enough_data:
            self.val_project.set_text("not declining measurably")
        else:
            self.val_project.set_text("—")
        self.val_cycle_pace.set_text(
            f"~{trend.cycles_per_year:.0f} cycles/year"
            if trend.cycles_per_year
            else "needs a day or more of samples"
        )
        self.val_median.set_text(
            f"{trend.median_discharge_w:.2f} W"
            if trend.median_discharge_w
            else "no discharge samples yet"
        )
        if trend.runtime_now_h and trend.runtime_new_h:
            lost = (trend.runtime_new_h - trend.runtime_now_h) * 60
            self.val_runtime.set_text(
                f"{trend.runtime_now_h:.1f} h  vs  {trend.runtime_new_h:.1f} h "
                f"(−{lost:.0f} min)"
            )
        else:
            self.val_runtime.set_text("—")

        for row in self._profile_cost_rows:
            self.profile_cost_group.remove(row)
        self._profile_cost_rows.clear()
        entries = [
            (name, info)
            for name, info in (trend.draw_by_profile or {}).items()
            if info.get("median_w")
        ]
        if entries:
            for name, info in entries:
                plural = "" if info["samples"] == 1 else "s"
                row = Adw.ActionRow(
                    title=name,
                    subtitle=f"{info['samples']} discharge sample{plural}",
                )
                row.add_suffix(_label(f"{info['median_w']:.2f} W", "numeric", "dim-label"))
                self.profile_cost_group.add(row)
                self._profile_cost_rows.append(row)
        else:
            row = Adw.ActionRow(
                title="Nothing measured yet",
                subtitle="Spend time unplugged on more than one profile and the "
                "real difference shows up here.",
            )
            self.profile_cost_group.add(row)
            self._profile_cost_rows.append(row)

        self.val_logger.set_text(self._logger_state())
        self.val_logpath.set_text(history.log_path())

    def _refresh_advice(self, snap: probe.Snapshot, trend: history.Trend):
        for row in self._advice_rows:
            self.advice_group.remove(row)
        self._advice_rows.clear()
        for note in advice.build(snap, trend):
            style, word = LEVEL_STYLE[note.level]
            row = Adw.ActionRow(title=note.title, subtitle=note.detail)
            row.set_subtitle_lines(0)
            row.set_title_lines(0)
            icon = Gtk.Image.new_from_icon_name(LEVEL_ICON[note.level])
            icon.add_css_class(style)
            row.add_prefix(icon)
            tag = _label(word, "caption", style)
            tag.set_valign(Gtk.Align.CENTER)
            row.add_suffix(tag)
            self.advice_group.add(row)
            self._advice_rows.append(row)


PAGES = ("now", "health", "trend", "advice")


class PowerHealthApp(Adw.Application):
    def __init__(self, page: str | None = None):
        super().__init__(application_id=APP_ID)
        self._page = page

    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(EXTRA_CSS)
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def do_activate(self):
        window = self.props.active_window
        if window is None:
            window = PowerHealthWindow(application=self, page=self._page)
        elif self._page:
            # A second invocation with --page re-aims the window already open.
            window.stack.set_visible_child_name(self._page)
        window.present()


def main(argv=None, page: str | None = None) -> int:
    return PowerHealthApp(page=page).run(argv or [])
