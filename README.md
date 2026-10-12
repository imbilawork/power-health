# power-health

Power and battery health for Omarchy. A GTK4/libadwaita window plus a CLI,
built against what this machine actually exposes rather than what a laptop is
generally assumed to expose.

```
power-health             # open the window
power-health report      # the whole picture, in the terminal
power-health status      # one line, for a prompt or a bar
power-health advice      # just the recommendations
power-health json        # machine-readable
power-health trend       # what the sample log says so far
power-health log         # append one sample (the timer calls this)
```

`SUPER + ALT + B` opens the window. It is also in the Omarchy menu under
**Battery health**.

## What it shows

**Now** — charge, state, instantaneous watts at the pack, time to empty or
full, battery and CPU temperature, fan speed, and a power-profile switcher that
goes through `omarchy-powerprofiles-set` so the choice is remembered separately
for mains and battery.

**Health** — full-charge capacity against design capacity, watt-hours lost,
cycle count against the published rating, and whether a charge limit is
available.

**Trend** — capacity over time and measured draw while on battery, from the
background sample log, plus the real cost of each power profile in watts.

**Advice** — what the numbers actually support, worst first.

## Things that are easy to get wrong, and how this handles them

**The kernel's battery percentage is wrong here.** On this pack (SMP
`bq20z451`, MacBookPro16,3) `/sys/class/power_supply/BAT0/capacity` is reported
against *design* capacity, so it reads 69% where upower and the Omarchy bar
both say 90%. Percentage is therefore derived from `charge_now / charge_full`,
which matches the rest of the desktop. The design-relative number is still
shown, labelled as what it is.

**Watt-hours need a reference voltage.** This pack reports amp-hours, not
watt-hours, so a voltage has to be chosen to convert. upower uses
`voltage_min_design`, so this does too — otherwise the figures here would drift
from every other tool on the system.

**Watts and upower's "energy rate" are different numbers.** The app shows
`current_now × voltage_now`, the true instantaneous draw (~14 W here). upower
reports the same current at *design* voltage (~13 W). Both are correct; the UI
says which one it is showing.

**Time remaining does not involve voltage at all.** It is computed in the
battery's own units — amp-hours over amps — so it cannot inherit a conversion
error from the watt-hour figures.

**A week of samples before calling anything a trend.** `charge_full` jumps
around by a few hundred mWh when the firmware recalibrates after a deep cycle.
Below seven days and three distinct days of readings, the app says it is still
collecting rather than drawing a line through noise. A rise in capacity is
reported as recalibration, not as the battery healing.

**No charge limit on this hardware.** Intel Macs expose no
`charge_control_end_threshold`; the T2 keeps charge control to itself. The app
says so explicitly instead of hiding the row, because "cap it at 80%" is the
first thing a worn battery suggests.

## The sample log

upower keeps history in `/var/lib/upower`, but those files are `0640
root:root`, so an unprivileged app cannot read them. This keeps its own:

```
~/.local/share/power-health/samples.csv
```

A systemd user timer appends a row every 10 minutes — about 17 kB a day. The
log is capped at 200,000 rows and trimmed to the most recent 150,000, which at
that cadence is roughly four years before the first trim.

```bash
systemctl --user status power-health-logger.timer
power-health trend --csv        # the raw log
```

## Install

As a pacman package, from the latest release:

```bash
sudo pacman -U https://github.com/imbilawork/power-health/releases/download/v1.0.0/power-health-1.0.0-1-any.pkg.tar.zst
systemctl --user enable --now power-health-logger.timer
```

Or from a checkout, for the current user only:

```bash
./install.sh             # user-only: ~/.local/bin, ~/.config/systemd/user
./install.sh --uninstall # leaves the sample log alone
```

No root, no new packages: it uses the GTK4, libadwaita and PyGObject already on
an Omarchy system, and talks to `power-profiles-daemon` through Omarchy's own
wrapper. `powertop` and `tlp` are not required and not recommended here — tlp
and power-profiles-daemon conflict.

## Layout

```
powerhealth/probe.py     hardware reading; every unit convention fixed in one place
powerhealth/history.py   the sample log and the trend maths
powerhealth/advice.py    the recommendation rules
powerhealth/chart.py     cairo line charts, theme tokens passed in
powerhealth/render.py    terminal output
powerhealth/gui.py       the GTK4 window
powerhealth/cli.py       argument parsing and subcommands
```

`probe.py` degrades to `None` rather than raising, everywhere — a missing sysfs
file is normal across machines, not an error.

## License

MIT. See [LICENSE](LICENSE).
