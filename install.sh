#!/usr/bin/env bash
# Install WinClip for the current Linux user.
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user; it will call sudo for apt packages." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/winclip"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/winclip"
AUTOSTART_DIR="$HOME/.config/autostart"
AUTOSTART="$AUTOSTART_DIR/winclip.desktop"

for file in winclip.py winclip_app.py winclip_store.py; do
    if [ ! -f "$SCRIPT_DIR/$file" ]; then
        echo "Missing $SCRIPT_DIR/$file" >&2
        exit 1
    fi
done

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This installer expects an apt-based distribution." >&2
    exit 1
fi

# --------------------------------------------------------------------- packages
missing=0
for pkg in python3-pyqt5 xdotool x11-utils; do
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed"; then
        missing=1
    fi
done
if [ "$missing" -eq 1 ]; then
    echo "Installing python3-pyqt5, xdotool and x11-utils..."
    sudo apt-get update
    sudo apt-get install -y python3-pyqt5 xdotool x11-utils
else
    echo "Dependencies already installed."
fi

# -------------------------------------------------------------------------- app
install -d -m 700 "$APP_DIR"
install -m 0644 "$SCRIPT_DIR/winclip.py" "$APP_DIR/winclip.py"
install -m 0644 "$SCRIPT_DIR/winclip_app.py" "$APP_DIR/winclip_app.py"
install -m 0644 "$SCRIPT_DIR/winclip_store.py" "$APP_DIR/winclip_store.py"
if [ -f "$SCRIPT_DIR/README.md" ]; then
    install -m 0644 "$SCRIPT_DIR/README.md" "$APP_DIR/README.md"
fi

install -d -m 755 "$BIN_DIR"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec python3 "$APP_DIR/winclip.py" "\$@"
EOF
chmod 0755 "$LAUNCHER"

# --------------------------------------------------------------------- autostart
install -d -m 755 "$AUTOSTART_DIR"
cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=WinClip
Comment=Windows 11 style clipboard history
Exec="$LAUNCHER" --daemon
Icon=edit-paste
Terminal=false
StartupNotify=false
X-GNOME-Autostart-enabled=true
EOF

# Stop an older copy and start the new build in this login session. The
# autostart entry handles future logins even if the current spawn fails.
if [ -n "${DISPLAY:-}" ]; then
    "$LAUNCHER" quit >/dev/null 2>&1 || true
    # Give the old daemon time to release its socket and instance lock, so the
    # new build is the one that starts.
    sleep 0.6
    nohup "$LAUNCHER" --daemon >/dev/null 2>&1 &
    disown 2>/dev/null || true
fi

# ------------------------------------------------------------------ shortcuts
if command -v gsettings >/dev/null 2>&1 && [ -n "${DISPLAY:-}" ]; then
    python3 -c "
import subprocess, ast
try:
    res = subprocess.run(['gsettings', 'get', 'org.cinnamon.desktop.keybindings', 'custom-list'], capture_output=True, text=True)
    if res.returncode == 0:
        lst = ast.literal_eval(res.stdout.strip())
        if not isinstance(lst, list): lst = []
        conf = {
            'custom0': ('WinClip Snip', '$LAUNCHER snip', \"['<Super><Shift>s', '<Shift><Super>s']\"),
            'custom1': ('WinClip', '$LAUNCHER show', \"['<Super>v']\"),
            'custom2': ('WinClip Full Shot', '$LAUNCHER shot', \"['<Primary>z', '<Control>z']\"),
        }
        for k, (name, cmd, bind) in conf.items():
            if k not in lst: lst.append(k)
            pfx = f'org.cinnamon.desktop.keybindings.custom-keybinding:/org/cinnamon/desktop/keybindings/custom-keybindings/{k}/'
            subprocess.run(['gsettings', 'set', pfx, 'name', name], check=True)
            subprocess.run(['gsettings', 'set', pfx, 'command', cmd], check=True)
            subprocess.run(['gsettings', 'set', pfx, 'binding', bind], check=True)
        subprocess.run(['gsettings', 'set', 'org.cinnamon.desktop.keybindings', 'custom-list', str(lst)], check=True)
except Exception:
    pass
" 2>/dev/null || true
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo
        echo "Note: $BIN_DIR is not on PATH in this shell."
        echo "Log out and back in before binding the 'winclip show' shortcut."
        ;;
esac

cat <<'EOF'

WinClip is installed.

Set the shortcuts in Cinnamon / your desktop environment:
  Menu > Keyboard > Shortcuts > Custom Shortcuts

  1. Clipboard History:
     Name:    WinClip History
     Command: winclip show
     Key:     Super+V

  2. Screen Snipping Tool:
     Name:    WinClip Snip
     Command: winclip snip
     Key:     Shift+Super+S

  3. Full-Screen Instant Shot (Save only, no clipboard):
     Name:    WinClip Full Shot
     Command: winclip shot
     Key:     Ctrl+Z

Then press Super+V, Shift+Super+S, or Ctrl+Z. See ~/.local/share/winclip/README.md
for complete details, custom settings, and keyboard navigation.
EOF
