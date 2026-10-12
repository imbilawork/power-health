#!/usr/bin/env bash
# Install (or remove) power-health for the current user. Touches nothing
# outside $HOME and needs no root.
set -euo pipefail

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
UNIT_DIR="$HOME/.config/systemd/user"

say() { printf '  %s\n' "$*"; }

uninstall() {
  echo "Removing power-health"
  systemctl --user disable --now power-health-logger.timer 2>/dev/null || true
  rm -f "$UNIT_DIR/power-health-logger.timer" "$UNIT_DIR/power-health-logger.service"
  rm -f "$BIN_DIR/power-health" "$DESKTOP_DIR/power-health.desktop"
  systemctl --user daemon-reload 2>/dev/null || true
  update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
  say "Removed. The sample log in ~/.local/share/power-health was left alone."
  exit 0
}

[[ "${1:-}" == "--uninstall" ]] && uninstall

echo "Installing power-health from $APP_ROOT"
mkdir -p "$BIN_DIR" "$DESKTOP_DIR" "$UNIT_DIR"

# The launcher carries the source path, so the tree can live anywhere.
sed "s|^APP_ROOT=.*|APP_ROOT=\"\${POWER_HEALTH_ROOT:-$APP_ROOT}\"|" \
  "$APP_ROOT/bin/power-health" > "$BIN_DIR/power-health"
chmod +x "$BIN_DIR/power-health"
say "command    $BIN_DIR/power-health"

install -m 644 "$APP_ROOT/share/power-health.desktop" "$DESKTOP_DIR/power-health.desktop"
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
say "launcher   $DESKTOP_DIR/power-health.desktop"

install -m 644 "$APP_ROOT/systemd/power-health-logger.service" "$UNIT_DIR/"
install -m 644 "$APP_ROOT/systemd/power-health-logger.timer" "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now power-health-logger.timer
say "logger     every 10 minutes (systemctl --user status power-health-logger.timer)"

"$BIN_DIR/power-health" log >/dev/null && say "first sample logged"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "NOTE: $BIN_DIR is not on your PATH yet." ;;
esac

echo
echo "Done. Try:  power-health report"
