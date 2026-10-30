# WinClip 📋

[![Python](https://img.shields.io/badge/Python-3.8+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![PyQt5](https://img.shields.io/badge/GUI-PyQt5-41CD52?style=flat-square&logo=qt&logoColor=white)](https://riverbankcomputing.com/software/pyqt/)
[![Platform](https://img.shields.io/badge/Platform-Linux%20(X11)-FCC624?style=flat-square&logo=linux&logoColor=black)](https://www.kernel.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)](LICENSE)

**WinClip** is a lightweight, modern, and high-performance **Windows 11 clipboard history alternative for Linux**. 

Built for Linux users who miss the clean, effortless <kbd>Win</kbd> + <kbd>V</kbd> workflow from Windows 11, WinClip brings the same minimalist Fluent dark aesthetic, instant paste capability, category organization, and pinned clips directly to your Linux desktop.

---

## ✨ Features

- **🪟 Windows 11 Fluent Aesthetic**: Modern dark theme with rounded cards, subtle shadows, and centered positioning right near your cursor or text caret.
- **⚡ 0% Idle CPU Usage**: Purely event-driven via native Qt clipboard signals (`QClipboard.dataChanged`). No polling loops or background CPU drain.
- **🖼️ Text & Image History**: Stores rich text snippets, URLs, multi-line code blocks, and images with crisp high-DPI thumbnails and dimensions.
- **📌 Pinned Clips & Categories**: Pin your most important clips so they never expire. Organize pinned items into custom category tabs (e.g. *Work*, *Templates*, *Social*).
- **🔀 Drag & Drop Reordering**: Easily reorder pinned clips via drag-and-drop or keyboard shortcuts (<kbd>Ctrl</kbd> + <kbd>Up</kbd> / <kbd>Down</kbd>).
- **🎯 Context-Aware Pasting**: Automatically detects the focused window and restores focus on paste. Detects terminal emulators to inject <kbd>Ctrl</kbd> + <kbd>Shift</kbd> + <kbd>V</kbd> instead of <kbd>Ctrl</kbd> + <kbd>V</kbd>.
- **🔒 Password Manager Safe**: Respects privacy hints from password managers (`x-kde-passwordManagerHint`, `x-kde-hiddenPassword`) to prevent accidental credential capture.
- **⚙️ Integrated Settings**: Built-in settings dialog to add, rename, or delete categories and manage clip associations.
- **📦 Single Background Daemon**: Controlled over a lightweight Unix domain socket (`winclip show`, `winclip --daemon`, `winclip quit`).

---

## 🚀 Quick Start

### 1. Requirements

- **OS**: Linux (Linux Mint, Ubuntu, Debian, Arch, Fedora, etc.)
- **Display Server**: X11 session
- **System Packages**: `python3-pyqt5`, `xdotool`, `x11-utils` (installed automatically by `install.sh`)

### 2. Installation

Clone the repository and run the installation script:

```bash
git clone https://github.com/shuvan-vibe/winclip.git
cd winclip
./install.sh
```

The installer will:
1. Install system dependencies (`python3-pyqt5`, `xdotool`, `x11-utils`).
2. Copy files to `~/.local/share/winclip`.
3. Create the executable launcher at `~/.local/bin/winclip`.
4. Register the autostart entry at `~/.config/autostart/winclip.desktop`.
5. Start the background daemon for your active desktop session.

---

## ⌨️ Setting up the <kbd>Win</kbd> + <kbd>V</kbd> Shortcut

### Linux Mint (Cinnamon)

1. Open **Menu > Keyboard > Shortcuts**.
2. Go to **Custom Shortcuts** and click **Add custom shortcut**.
3. Fill in:
   - **Name**: `WinClip`
   - **Command**: `winclip show`
4. Click the keyboard binding button and press <kbd>Super</kbd> + <kbd>V</kbd> (or <kbd>Win</kbd> + <kbd>V</kbd>).

> [!TIP]
> If <kbd>Super</kbd> + <kbd>V</kbd> was previously assigned to another application (like CopyQ), remove or change that assignment first in **Keyboard > Shortcuts**.

### GNOME (Ubuntu, Fedora, Debian)

1. Open **Settings > Keyboard > Keyboard Shortcuts > Custom Shortcuts**.
2. Click **+** to add a new shortcut:
   - **Name**: `WinClip`
   - **Command**: `winclip show`
   - **Shortcut**: <kbd>Super</kbd> + <kbd>V</kbd>

### XFCE / KDE Plasma

- In **Keyboard Settings > Application Shortcuts**, add `winclip show` bound to <kbd>Super</kbd> + <kbd>V</kbd>.

---

## 🎮 Keyboard Controls

| Shortcut | Action |
| :--- | :--- |
| <kbd>Super</kbd> + <kbd>V</kbd> | Open clipboard history popup near cursor |
| <kbd>↑</kbd> / <kbd>↓</kbd> | Navigate clips |
| <kbd>←</kbd> / <kbd>→</kbd> | Switch between category tabs |
| <kbd>Enter</kbd> | Paste selected clip into active application |
| <kbd>Ctrl</kbd> + <kbd>P</kbd> | Pin or unpin selected clip |
| <kbd>Ctrl</kbd> + <kbd>↑</kbd> / <kbd>↓</kbd> | Move pinned item up or down |
| <kbd>Delete</kbd> | Delete selected clip |
| <kbd>Ctrl</kbd> + <kbd>,</kbd> | Open WinClip Settings |
| <kbd>Esc</kbd> | Close clipboard popup |

---

## 📁 Storage & Privacy

All data is stored locally in your user profile:
- **Clipboard History**: `~/.local/share/winclip/history.json`
- **Settings & Categories**: `~/.local/share/winclip/settings.json`
- **Cached Images**: `~/.local/share/winclip/images/`
- **Autostart Service**: `~/.config/autostart/winclip.desktop`

Sensitive clips flagged by password managers are automatically filtered out. History files feature automatic corruption recovery and preserve backups before resetting.

---

## 🧪 Testing

The codebase includes comprehensive unit tests for the history store and Qt UI layer:

```bash
QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v
```

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
