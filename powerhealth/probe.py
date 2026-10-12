"""Hardware probing for power and battery health.

Every reader degrades to ``None`` instead of raising. This module runs on a
timer, on a laptop whose firmware exposes a different slice of sysfs per model,
so a missing file is normal rather than exceptional.

Unit conventions are fixed once, here, so nothing downstream has to guess:

* Capacities and charges come back in watt-hours, converted at
  ``voltage_min_design`` -- the convention upower uses. Our numbers therefore
  agree with ``upower -i`` and the Omarchy bar instead of drifting a watt-hour
  away from them.
* ``draw_w`` is true instantaneous power: ``power_now`` when the firmware
  publishes it, otherwise ``current_now * voltage_now``. That is what the
  machine pulls right now, a different (larger) number than upower's
  design-voltage rate. Both are correct; they answer different questions, and
  the UI labels which one it is showing.
* Time-to-empty and time-to-full are computed in the battery's own native
  domain -- amp-hours over amps for a charge-reporting pack -- so no voltage
  assumption enters the estimate at all.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field

SUPPLY_PATH = os.environ.get("POWER_HEALTH_SUPPLY_PATH", "/sys/class/power_supply")
HWMON_PATH = "/sys/class/hwmon"
CPUFREQ = "/sys/devices/system/cpu/cpu0/cpufreq"

# Vendor-published design cycle ratings, keyed by DMI product-name prefix. Used
# only to put cycle_count in context. A missing key means "unknown", which is
# reported as unknown rather than guessed.
RATED_CYCLES = {
    "MacBookPro": 1000,
    "MacBookAir": 1000,
}


def _read(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _int(path: str) -> int | None:
    raw = _read(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _run(argv: list[str]) -> str | None:
    if not shutil.which(argv[0]):
        return None
    try:
        out = subprocess.run(
            argv, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def find_battery() -> str | None:
    """Path of the first present battery, or None on a desktop."""
    for entry in sorted(glob.glob(os.path.join(SUPPLY_PATH, "*"))):
        if _read(os.path.join(entry, "type")) != "Battery":
            continue
        present = _read(os.path.join(entry, "present"))
        if present in (None, "1"):
            return entry
    return None


def ac_online() -> bool | None:
    """True when any mains supply reports itself connected."""
    seen = False
    for entry in sorted(glob.glob(os.path.join(SUPPLY_PATH, "*"))):
        if _read(os.path.join(entry, "type")) != "Mains":
            continue
        online = _read(os.path.join(entry, "online"))
        if online is None:
            continue
        seen = True
        if online == "1":
            return True
    return False if seen else None


def product_name() -> str | None:
    return _read("/sys/class/dmi/id/product_name")


def rated_cycles() -> int | None:
    name = product_name() or ""
    for prefix, count in RATED_CYCLES.items():
        if name.startswith(prefix):
            return count
    return None


# --------------------------------------------------------------------------- #
# Thermals
# --------------------------------------------------------------------------- #

def _hwmon_dirs() -> list[str]:
    return sorted(glob.glob(os.path.join(HWMON_PATH, "hwmon*")))


def cpu_package_temp_c() -> float | None:
    """Package temperature from coretemp/k10temp, in degrees C."""
    for hw in _hwmon_dirs():
        name = _read(os.path.join(hw, "name"))
        if name not in ("coretemp", "k10temp", "zenpower"):
            continue
        # Prefer the sensor explicitly labelled as the package.
        for label_file in sorted(glob.glob(os.path.join(hw, "temp*_label"))):
            label = _read(label_file) or ""
            if "Package" in label or label in ("Tctl", "Tdie"):
                value = _int(label_file.replace("_label", "_input"))
                if value is not None:
                    return value / 1000.0
        value = _int(os.path.join(hw, "temp1_input"))
        if value is not None:
            return value / 1000.0
    return None


def fans() -> list[dict]:
    """Every fan the kernel exposes, with its min/max envelope when known.

    applesmc hangs its fan attributes off the hwmon *device* node rather than
    the hwmon directory itself, so both locations are globbed.
    """
    found: list[dict] = []
    patterns = [
        os.path.join(HWMON_PATH, "hwmon*", "fan*_input"),
        os.path.join(HWMON_PATH, "hwmon*", "device", "fan*_input"),
    ]
    seen: set[str] = set()
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            rpm = _int(path)
            if rpm is None:
                continue
            stem = path[: -len("_input")]
            found.append(
                {
                    "name": os.path.basename(stem),
                    "rpm": rpm,
                    "min": _int(stem + "_min"),
                    "max": _int(stem + "_max"),
                }
            )
    return found


# --------------------------------------------------------------------------- #
# CPU / profile
# --------------------------------------------------------------------------- #

def power_profile() -> str | None:
    return _run(["powerprofilesctl", "get"])


def available_profiles() -> list[str]:
    """Profiles this machine offers, ordered saver -> performance."""
    out = _run(["omarchy-powerprofiles-list"])
    if out:
        # omarchy lists performance-first; our UI reads saver-first.
        listed = [line.strip() for line in out.splitlines() if line.strip()]
        if listed:
            return list(reversed(listed))
    out = _run(["powerprofilesctl", "list"])
    if not out:
        return []
    names = []
    for line in out.splitlines():
        stripped = line.strip().lstrip("* ").strip()
        if stripped.endswith(":") and " " not in stripped[:-1]:
            names.append(stripped[:-1])
    order = {"power-saver": 0, "balanced": 1, "performance": 2}
    return sorted(names, key=lambda n: order.get(n, 99))


def set_power_profile(name: str) -> tuple[bool, str]:
    """Switch profile, preferring Omarchy's wrapper so the choice is remembered.

    omarchy-powerprofiles-set stores the profile per AC/battery state, which is
    what the rest of the desktop expects. Falling back to powerprofilesctl
    still switches, it just will not survive a plug/unplug.
    """
    if shutil.which("omarchy-powerprofiles-set"):
        argv = ["omarchy-powerprofiles-set", "autodetect", name]
    elif shutil.which("powerprofilesctl"):
        argv = ["powerprofilesctl", "set", name]
    else:
        return False, "no power-profiles-daemon client found"
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout or "failed").strip()
    return True, name


def energy_performance_preference() -> str | None:
    return _read(os.path.join(CPUFREQ, "energy_performance_preference"))


def scaling_driver() -> str | None:
    return _read(os.path.join(CPUFREQ, "scaling_driver"))


def scaling_governor() -> str | None:
    return _read(os.path.join(CPUFREQ, "scaling_governor"))


def charge_thresholds(battery: str | None) -> dict:
    """Charge-limit support, which most Intel Macs simply do not expose.

    Reported explicitly as unsupported rather than silently omitted, because
    "can I cap charging at 80%?" is the first question a worn battery raises.
    """
    if not battery:
        return {"supported": False, "start": None, "end": None}
    end = _int(os.path.join(battery, "charge_control_end_threshold"))
    start = _int(os.path.join(battery, "charge_control_start_threshold"))
    behaviour = _read(os.path.join(battery, "charge_behaviour"))
    return {
        "supported": end is not None or behaviour is not None,
        "start": start,
        "end": end,
        "behaviour": behaviour,
    }


# --------------------------------------------------------------------------- #
# The snapshot
# --------------------------------------------------------------------------- #

@dataclass
class Snapshot:
    """One instant of the machine's power state."""

    ts: float
    present: bool = False
    status: str | None = None
    ac: bool | None = None
    percent: float | None = None
    capacity_raw: int | None = None

    now_wh: float | None = None
    full_wh: float | None = None
    design_wh: float | None = None

    volts: float | None = None
    amps: float | None = None
    draw_w: float | None = None
    draw_is_measured: bool = False

    cycles: int | None = None
    rated_cycles: int | None = None
    technology: str | None = None
    manufacturer: str | None = None
    model: str | None = None

    batt_temp_c: float | None = None
    cpu_temp_c: float | None = None
    fans: list = field(default_factory=list)

    profile: str | None = None
    epp: str | None = None
    governor: str | None = None
    driver: str | None = None
    thresholds: dict = field(default_factory=dict)
    product: str | None = None

    seconds_remaining: float | None = None
    remaining_kind: str | None = None  # "empty" | "full"

    @property
    def health_pct(self) -> float | None:
        """Full-charge capacity as a share of design capacity."""
        if not self.full_wh or not self.design_wh:
            return None
        return 100.0 * self.full_wh / self.design_wh

    @property
    def lost_wh(self) -> float | None:
        if self.full_wh is None or self.design_wh is None:
            return None
        return max(0.0, self.design_wh - self.full_wh)

    @property
    def percent_of_design(self) -> float | None:
        """Charge right now as a share of a *new* pack's capacity.

        The honest headline for a worn battery: at 100% charge this machine
        still only holds ``health_pct`` of what it shipped with.
        """
        if self.now_wh is None or not self.design_wh:
            return None
        return 100.0 * self.now_wh / self.design_wh

    @property
    def charging(self) -> bool:
        return (self.status or "").lower() == "charging"

    @property
    def discharging(self) -> bool:
        return (self.status or "").lower() == "discharging"

    def as_dict(self) -> dict:
        data = asdict(self)
        data["health_pct"] = self.health_pct
        data["lost_wh"] = self.lost_wh
        data["percent_of_design"] = self.percent_of_design
        return data


def snapshot(now: float | None = None) -> Snapshot:
    """Read the whole power state in one pass."""
    import time

    snap = Snapshot(ts=now if now is not None else time.time())
    snap.product = product_name()
    snap.ac = ac_online()
    snap.profile = power_profile()
    snap.epp = energy_performance_preference()
    snap.governor = scaling_governor()
    snap.driver = scaling_driver()
    snap.cpu_temp_c = cpu_package_temp_c()
    snap.fans = fans()

    battery = find_battery()
    snap.thresholds = charge_thresholds(battery)
    if battery is None:
        return snap

    snap.present = True
    snap.status = _read(os.path.join(battery, "status"))
    snap.technology = _read(os.path.join(battery, "technology"))
    snap.manufacturer = _read(os.path.join(battery, "manufacturer"))
    snap.model = _read(os.path.join(battery, "model_name"))
    snap.cycles = _int(os.path.join(battery, "cycle_count"))
    snap.rated_cycles = rated_cycles()

    temp = _int(os.path.join(battery, "temp"))
    if temp is not None:
        snap.batt_temp_c = temp / 10.0

    voltage_now = _int(os.path.join(battery, "voltage_now"))
    voltage_design = _int(os.path.join(battery, "voltage_min_design"))
    if voltage_now is not None:
        snap.volts = voltage_now / 1e6

    # The reference voltage for every Wh conversion below. Design voltage is
    # what upower uses; voltage_now is the fallback so an unusual pack still
    # yields sane figures rather than none.
    ref_v = None
    if voltage_design:
        ref_v = voltage_design / 1e6
    elif voltage_now:
        ref_v = voltage_now / 1e6

    charge_now = _int(os.path.join(battery, "charge_now"))
    charge_full = _int(os.path.join(battery, "charge_full"))
    charge_design = _int(os.path.join(battery, "charge_full_design"))
    energy_now = _int(os.path.join(battery, "energy_now"))
    energy_full = _int(os.path.join(battery, "energy_full"))
    energy_design = _int(os.path.join(battery, "energy_full_design"))

    current_now = _int(os.path.join(battery, "current_now"))
    power_now = _int(os.path.join(battery, "power_now"))
    if current_now is not None:
        snap.amps = abs(current_now) / 1e6

    if charge_full is not None and ref_v:
        # Charge-reporting pack (amp-hours): the Apple/SMP case.
        if charge_now is not None:
            snap.now_wh = charge_now / 1e6 * ref_v
        snap.full_wh = charge_full / 1e6 * ref_v
        if charge_design is not None:
            snap.design_wh = charge_design / 1e6 * ref_v
    elif energy_full is not None:
        # Energy-reporting pack (watt-hours) -- no voltage assumption needed.
        if energy_now is not None:
            snap.now_wh = energy_now / 1e6
        snap.full_wh = energy_full / 1e6
        if energy_design is not None:
            snap.design_wh = energy_design / 1e6

    # Percentage is derived from charge-now over charge-full rather than taken
    # from the kernel's "capacity" file. On this hardware (SMP bq20z451 in a
    # MacBookPro16,3) capacity is reported against *design* capacity, so it
    # reads 69% where upower and the Omarchy bar both say 90%. Deriving it
    # keeps every surface on this machine telling the same story; the
    # design-relative figure is still exposed below, where it is labelled as
    # what it actually is.
    snap.capacity_raw = _int(os.path.join(battery, "capacity"))
    if snap.now_wh is not None and snap.full_wh:
        snap.percent = 100.0 * snap.now_wh / snap.full_wh
    elif snap.capacity_raw is not None:
        snap.percent = float(snap.capacity_raw)

    # Instantaneous power.
    if power_now is not None:
        snap.draw_w = abs(power_now) / 1e6
        snap.draw_is_measured = True
    elif current_now is not None and voltage_now is not None:
        snap.draw_w = abs(current_now) * voltage_now / 1e12
        snap.draw_is_measured = True

    snap.seconds_remaining, snap.remaining_kind = _time_remaining(
        snap,
        charge_now=charge_now,
        charge_full=charge_full,
        current_now=current_now,
        energy_now=energy_now,
        energy_full=energy_full,
        power_now=power_now,
    )
    return snap


def _time_remaining(
    snap: Snapshot,
    *,
    charge_now,
    charge_full,
    current_now,
    energy_now,
    energy_full,
    power_now,
) -> tuple[float | None, str | None]:
    """Estimate time to empty or full in the battery's native units.

    Working in amp-hours over amps (or watt-hours over watts) keeps the
    estimate free of any voltage conversion, so it cannot inherit an error from
    the Wh figures shown elsewhere.
    """
    if not (snap.charging or snap.discharging):
        return None, None

    numerator = denominator = None
    if charge_full is not None and current_now:
        denominator = abs(current_now)
        if snap.discharging and charge_now is not None:
            numerator = charge_now
        elif snap.charging and charge_now is not None:
            numerator = max(0, charge_full - charge_now)
    elif energy_full is not None and power_now:
        denominator = abs(power_now)
        if snap.discharging and energy_now is not None:
            numerator = energy_now
        elif snap.charging and energy_now is not None:
            numerator = max(0, energy_full - energy_now)

    if not numerator or not denominator:
        return None, None
    hours = numerator / denominator
    if hours <= 0 or hours > 100:  # Nonsense from a settling current reading.
        return None, None
    return hours * 3600.0, "empty" if snap.discharging else "full"
