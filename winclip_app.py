#!/usr/bin/env python3
"""The PyQt5 daemon, clipboard watcher and Windows 11 style popup."""

from __future__ import annotations

import ctypes
import gc
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PyQt5.QtCore import (
    QBuffer,
    QEasingCurve,
    QEvent,
    QIODevice,
    QMimeData,
    QObject,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QBrush,
    QClipboard,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QImage,
    QImageReader,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextLayout,
    QTextOption,
)
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from winclip_store import HistoryStore, image_fingerprint, text_fingerprint


PANEL_WIDTH = 360
SHADOW_MARGIN = 16
MAX_PANEL_HEIGHT = 480
MAX_SCROLL_HEIGHT = 360
CARD_RADIUS = 8
PANEL_RADIUS = 12
PREVIEW_WIDTH = 226
ACTION_AREA_WIDTH = 54
PASTE_DELAY_MS = 85

COLOR_PANEL = "#202020"
COLOR_CARD = "#2a2a2a"
COLOR_CARD_HOVER = "#323232"
COLOR_CARD_SELECTED = "#363636"
COLOR_TEXT = "#f2f2f2"
COLOR_MUTED = "#8e8e8e"
COLOR_ACCENT = "#4cc2ff"
COLOR_BORDER = "#333333"

PEN_CARD_BORDER = QPen(QColor("#333333"), 1)
PEN_CARD_HOVER_BORDER = QPen(QColor("#444444"), 1)
PEN_CARD_SEL_BORDER = QPen(QColor("#525252"), 1)
BRUSH_CARD = QBrush(QColor("#2a2a2a"))
BRUSH_CARD_HOVER = QBrush(QColor("#323232"))
BRUSH_CARD_SEL = QBrush(QColor("#363636"))

TERMINAL_TOKENS = {
    "alacritty",
    "blackbox",
    "contour",
    "cool-retro-term",
    "eterm",
    "foot",
    "ghostty",
    "guake",
    "hyper",
    "kgx",
    "kitty",
    "konsole",
    "lxterminal",
    "mate-terminal",
    "ptyxis",
    "qterminal",
    "rxvt",
    "sakura",
    "st",
    "tabby",
    "terminal",
    "terminator",
    "terminology",
    "tilix",
    "urxvt",
    "wezterm",
    "warp-terminal",
    "xfce4-terminal",
    "xterm",
    "yakuake",
}


def _warn(message: str) -> None:
    print(f"winclip: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# X11 / xdotool integration


class X11Helper:
    """Zero-dependency X11 helper for global pointer query and active window tracking."""

    def __init__(self) -> None:
        self._display = None
        self._x11 = None
        self._net_active_atom = 0
        self._root = None
        try:
            self._x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
            self._display = self._x11.XOpenDisplay(None)
            if self._display:
                self._root = self._x11.XDefaultRootWindow(self._display)
                self._net_active_atom = self._x11.XInternAtom(
                    self._display, b"_NET_ACTIVE_WINDOW", False
                )
        except Exception:
            self._display = None
            self._x11 = None

    def is_button_pressed(self) -> bool:
        """Return True if mouse buttons 1, 2, or 3 are pressed anywhere on the X11 screen."""
        if not self._display or not self._x11 or not self._root:
            return QApplication.mouseButtons() != Qt.NoButton
        root_ret = ctypes.c_ulong()
        child_ret = ctypes.c_ulong()
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        win_x = ctypes.c_int()
        win_y = ctypes.c_int()
        mask = ctypes.c_uint()
        try:
            res = self._x11.XQueryPointer(
                self._display,
                self._root,
                ctypes.byref(root_ret),
                ctypes.byref(child_ret),
                ctypes.byref(root_x),
                ctypes.byref(root_y),
                ctypes.byref(win_x),
                ctypes.byref(win_y),
                ctypes.byref(mask),
            )
            if res:
                # Button1Mask = 0x100, Button2Mask = 0x200, Button3Mask = 0x400
                return bool(mask.value & 0x700)
        except Exception:
            pass
        return False

    def get_active_window(self) -> Optional[int]:
        """Read _NET_ACTIVE_WINDOW directly from the X11 root window without subprocesses."""
        if not self._display or not self._x11 or not self._root or not self._net_active_atom:
            return None
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        nitems = ctypes.c_ulong()
        bytes_after = ctypes.c_ulong()
        prop = ctypes.c_void_p()
        try:
            # 33 = XA_WINDOW atom
            res = self._x11.XGetWindowProperty(
                self._display,
                self._root,
                self._net_active_atom,
                0,
                1,
                False,
                33,
                ctypes.byref(actual_type),
                ctypes.byref(actual_format),
                ctypes.byref(nitems),
                ctypes.byref(bytes_after),
                ctypes.byref(prop),
            )
            if res == 0 and prop.value:
                wid = ctypes.cast(prop, ctypes.POINTER(ctypes.c_ulong)).contents.value
                self._x11.XFree(prop)
                return wid if wid > 0 else None
        except Exception:
            pass
        return None

    def close(self) -> None:
        if self._display and self._x11:
            try:
                self._x11.XCloseDisplay(self._display)
            except Exception:
                pass
            self._display = None


_x11_helper = X11Helper()


def _run_xdotool(args: List[str], timeout: float = 1.2) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["xdotool", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        _warn("xdotool was not found; install it with: sudo apt install xdotool")
        return None
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn(f"xdotool failed: {exc}")
        return None


def active_window_id() -> Optional[int]:
    """Read the currently focused X11 window before our popup is shown."""
    # Fast path: xdotool getwindowfocus gets the exact focused subwindow/popup
    out = _run_xdotool(["getwindowfocus"], timeout=0.6)
    if out is not None and out.returncode == 0:
        try:
            wid = int(out.stdout.strip().splitlines()[-1])
            if wid > 0:
                return wid
        except (ValueError, IndexError):
            pass

    # In-process C query for _NET_ACTIVE_WINDOW without subprocess
    wid = _x11_helper.get_active_window()
    if wid is not None:
        return wid

    # Fallback to _NET_ACTIVE_WINDOW directly from the root window using xprop
    try:
        res = subprocess.run(
            ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=0.6,
            check=False,
        )
        if res.returncode == 0:
            match = re.search(
                r"_NET_ACTIVE_WINDOW\(WINDOW\): window id # (0x[0-9a-fA-F]+|\d+)",
                res.stdout,
            )
            if match:
                val = match.group(1)
                wid = int(val, 16) if val.startswith("0x") else int(val)
                if wid > 0:
                    return wid
    except Exception:
        pass

    # Final fallback to getactivewindow
    result = _run_xdotool(["getactivewindow"], timeout=1.0)
    if result is None or result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def window_class(window_id: Optional[int]) -> str:
    """Read WM_CLASS with xprop (xdotool has no class-name command)."""
    if window_id is None:
        return ""
    try:
        result = subprocess.run(
            ["xprop", "-id", str(window_id), "WM_CLASS"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1.0,
            check=False,
        )
    except FileNotFoundError:
        _warn("xprop was not found; install it with: sudo apt install x11-utils")
        return ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn(f"xprop failed: {exc}")
        return ""
    if result.returncode != 0:
        return ""
    quoted = re.findall(r'"([^"]*)"', result.stdout)
    if quoted:
        return " ".join(quoted)
    return result.stdout.strip()


def is_terminal_class(wm_class: str) -> bool:
    """Recognise common terminal emulators from their WM_CLASS value."""
    if not wm_class:
        return False
    tokens = re.split(r"[^a-z0-9]+", wm_class.casefold())
    return any(token in TERMINAL_TOKENS for token in tokens)


def refocus_and_paste(window_id: Optional[int], terminal: bool) -> None:
    """Refocus the previous window and inject the right paste shortcut."""
    if window_id is not None:
        target = str(window_id)
        # Use windowfocus first to preserve popup/subwindow focus without deactivating it via WM
        res = _run_xdotool(["windowfocus", target], timeout=0.8)
        if res is None or res.returncode != 0:
            _run_xdotool(["windowactivate", "--sync", target], timeout=1.0)
    time.sleep(PASTE_DELAY_MS / 1000.0)
    combo = "ctrl+shift+v" if terminal else "ctrl+v"
    _run_xdotool(["key", "--clearmodifiers", combo], timeout=1.0)


def query_visible_windows() -> Tuple[List[Dict[str, Any]], bool]:
    """Retrieve top-level visible non-minimized windows in top-to-bottom stacking order.

    Returns (windows, detection_failed). Each window is a dict with 'id' and 'rect' (QRect in virtual desktop coords).
    """
    has_xprop = shutil.which("xprop") is not None
    has_xwininfo = shutil.which("xwininfo") is not None
    has_xdotool = shutil.which("xdotool") is not None

    if not has_xprop or not (has_xwininfo or has_xdotool):
        _warn("xprop or xwininfo/xdotool not found; window detection disabled")
        return [], True

    try:
        res = subprocess.run(
            ["xprop", "-root", "_NET_CLIENT_LIST_STACKING"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1.0,
            check=False,
        )
        out = res.stdout
        if "_NET_CLIENT_LIST_STACKING" not in out:
            res = subprocess.run(
                ["xprop", "-root", "_NET_CLIENT_LIST"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=1.0,
                check=False,
            )
            out = res.stdout
    except Exception as exc:
        _warn(f"could not query window stack: {exc}")
        return [], True

    wids = re.findall(r"0x[0-9a-fA-F]+", out)
    if not wids:
        return [], False

    # _NET_CLIENT_LIST_STACKING is bottom-to-top; reverse to walk top-to-bottom
    wids.reverse()
    # Optimization: Inspect at most the top 20 windows (lower ones are hidden beneath)
    wids = wids[:20]

    results: List[Dict[str, Any]] = []
    for wid in wids:
        try:
            p_res = subprocess.run(
                [
                    "xprop",
                    "-id",
                    wid,
                    "_NET_WM_STATE",
                    "_NET_WM_WINDOW_TYPE",
                    "_NET_FRAME_EXTENTS",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=0.4,
                check=False,
            )
            p_out = p_res.stdout
        except Exception:
            continue

        if "_NET_WM_STATE_HIDDEN" in p_out:
            continue
        if "_NET_WM_WINDOW_TYPE_DESKTOP" in p_out:
            continue

        x: Optional[int] = None
        y: Optional[int] = None
        w: Optional[int] = None
        h: Optional[int] = None
        border = 0

        if has_xwininfo:
            try:
                w_res = subprocess.run(
                    ["xwininfo", "-id", wid],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=0.4,
                    check=False,
                )
                w_out = w_res.stdout
                if "Map State: IsViewable" not in w_out:
                    continue
                mx = re.search(r"Absolute upper-left X:\s+(-?\d+)", w_out)
                my = re.search(r"Absolute upper-left Y:\s+(-?\d+)", w_out)
                mw = re.search(r"Width:\s+(\d+)", w_out)
                mh = re.search(r"Height:\s+(\d+)", w_out)
                mb = re.search(r"Border width:\s+(\d+)", w_out)
                if mx and my and mw and mh:
                    x = int(mx.group(1))
                    y = int(my.group(1))
                    w = int(mw.group(1))
                    h = int(mh.group(1))
                    border = int(mb.group(1)) if mb else 0
            except Exception:
                pass

        if x is None and has_xdotool:
            try:
                d_res = subprocess.run(
                    ["xdotool", "getwindowgeometry", wid],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=0.4,
                    check=False,
                )
                d_out = d_res.stdout
                mp = re.search(r"Position:\s*(-?\d+),(-?\d+)", d_out)
                mg = re.search(r"Geometry:\s*(\d+)x(\d+)", d_out)
                if mp and mg:
                    x = int(mp.group(1))
                    y = int(mp.group(2))
                    w = int(mg.group(1))
                    h = int(mg.group(2))
            except Exception:
                pass

        if x is None or y is None or w is None or h is None or w <= 0 or h <= 0:
            continue

        # Adjust for window decorations (title bar and borders) using _NET_FRAME_EXTENTS
        ext = re.search(
            r"_NET_FRAME_EXTENTS\(CARDINAL\) = (\d+),\s*(\d+),\s*(\d+),\s*(\d+)",
            p_out,
        )
        if ext:
            left, right, top, bottom = map(int, ext.groups())
            x -= left
            y -= top
            w += left + right
            h += top + bottom
        elif border > 0:
            x -= border
            y -= border
            w += border * 2
            h += border * 2

        results.append({"id": wid, "rect": QRect(x, y, w, h)})

        # Optimization: If this window is fullscreen, it occludes all windows behind it
        if "_NET_WM_STATE_FULLSCREEN" in p_out:
            break

    return results, False


def grab_full_screen() -> Tuple[QPixmap, QRect]:
    """Capture all connected monitors into one composite pixmap covering the virtual desktop."""
    screens = QGuiApplication.screens()
    if not screens:
        return QPixmap(), QRect()

    total_geom = screens[0].geometry()
    for s in screens[1:]:
        total_geom = total_geom.united(s.geometry())

    pixmap = QPixmap(total_geom.size())
    pixmap.fill(Qt.black)
    painter = QPainter(pixmap)
    for s in screens:
        sg = s.geometry()
        spix = s.grabWindow(0, sg.x(), sg.y(), sg.width(), sg.height())
        pos = sg.topLeft() - total_geom.topLeft()
        painter.drawPixmap(pos, spix)
    painter.end()

    return pixmap, total_geom


def get_screenshots_directory() -> Path:
    """Return the destination folder for saved screenshots, creating it if needed."""
    override = os.environ.get("WINCLIP_SCREENSHOTS_DIR")
    if override:
        p = Path(override).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    pictures_dir = Path.home() / "Pictures"
    if shutil.which("xdg-user-dir"):
        try:
            res = subprocess.run(
                ["xdg-user-dir", "PICTURES"],
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                pictures_dir = Path(res.stdout.strip())
        except Exception:
            pass

    target = pictures_dir / "Screenshots"
    try:
        target.mkdir(parents=True, exist_ok=True)
        return target
    except Exception:
        try:
            pictures_dir.mkdir(parents=True, exist_ok=True)
            return pictures_dir
        except Exception:
            return Path.home()


def save_screenshot_to_file(image: QImage) -> Optional[Path]:
    """Save captured screenshot into user's PC with a timestamped filename."""
    try:
        directory = get_screenshots_directory()
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        candidate = directory / f"Screenshot_{timestamp}.png"
        counter = 1
        while candidate.exists():
            candidate = directory / f"Screenshot_{timestamp}_{counter}.png"
            counter += 1
        if image.save(str(candidate), "PNG"):
            return candidate
    except Exception as exc:
        _warn(f"could not save screenshot file: {exc}")
    return None


# ---------------------------------------------------------------------------
# Clipboard helpers


def mime_is_sensitive(mime: QMimeData) -> bool:
    """Honour the de-facto Linux password-manager clipboard hints."""
    markers = (
        "passwordmanagerhint",
        "password-manager-hint",
        "password_manager_hint",
        "x-gnome-password",
        "keepassxc",
        "clipboard-secret",
        "x-clipboard-secret",
    )
    try:
        for fmt in mime.formats():
            lowered = str(fmt).casefold()
            if any(marker in lowered for marker in markers):
                return True
    except Exception:
        pass
    return False


def encode_clipboard_image(image: QImage) -> Optional[Tuple[bytes, int, int, str]]:
    """Normalise an image and return PNG bytes, size and content hash."""
    if image is None or image.isNull():
        return None
    normalised = image.convertToFormat(QImage.Format_ARGB32)
    if normalised.isNull() or normalised.width() <= 0 or normalised.height() <= 0:
        return None
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    try:
        if not normalised.save(buffer, "PNG"):
            return None
        png = bytes(buffer.data())
    finally:
        buffer.close()
    if not png:
        return None
    return png, normalised.width(), normalised.height(), image_fingerprint(png)


def clipboard_image(mime: QMimeData) -> Optional[QImage]:
    """Extract an image from MIME data without trusting one backend alone."""
    try:
        if isinstance(mime, QMimeData) and mime.hasImage():
            data = mime.imageData()
            if isinstance(data, QImage) and not data.isNull():
                return data
            if isinstance(data, QPixmap) and not data.isNull():
                return data.toImage()
    except Exception:
        pass
    try:
        image = QApplication.clipboard().image()
        return image if not image.isNull() else None
    except Exception:
        return None


def mime_fingerprint(mime: QMimeData) -> Optional[str]:
    """Fingerprint the item currently on the clipboard, for self-copy guards."""
    try:
        image = clipboard_image(mime)
        if image is not None:
            encoded = encode_clipboard_image(image)
            return encoded[3] if encoded else None
        text = mime.text()
        if text:
            return text_fingerprint(text)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Small painting helpers


def make_settings_icon() -> QIcon:
    """Draw a small sliders icon so no icon theme dependency is needed."""
    icon = QIcon()
    for size in (16, 32):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        scale = size / 16.0
        pen = QPen(QColor(COLOR_MUTED), 1.4 * scale)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(QColor(COLOR_MUTED))
        for y, knob_x in ((4.5, 10.5), (8.0, 5.5), (11.5, 9.5)):
            painter.drawLine(
                QPointF(2.0 * scale, y * scale),
                QPointF(14.0 * scale, y * scale),
            )
            painter.drawEllipse(
                QPointF(knob_x * scale, y * scale),
                1.6 * scale,
                1.6 * scale,
            )
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class ActionButton(QAbstractButton):
    """A tiny, vector-drawn pin / close button for history cards."""

    def __init__(self, kind: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self._filled = False
        self.setFixedSize(26, 26)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip({"pin": "Pin", "close": "Delete"}[kind])
        self.setAccessibleName(self.toolTip())
        self.setAttribute(Qt.WA_Hover, True)

    def set_filled(self, filled: bool) -> None:
        self._filled = bool(filled)
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if self.underMouse() or self.isDown():
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(255, 255, 255, 24 if self.underMouse() else 38))
            painter.drawEllipse(QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5))

        if self.kind == "pin":
            color = QColor(COLOR_ACCENT) if (self._filled or self.underMouse()) else QColor("#a8a8a8")
        else:
            color = QColor("#ff6b6b") if self.underMouse() else QColor("#a8a8a8")

        painter.save()
        painter.translate(self.width() / 2.0, self.height() / 2.0)
        if self.kind == "pin":
            painter.rotate(45)
            path = QPainterPath()
            path.moveTo(-4.0, -7.5)
            path.lineTo(4.0, -7.5)
            path.lineTo(4.0, -4.8)
            path.lineTo(2.0, -4.8)
            path.lineTo(2.0, 1.0)
            path.lineTo(4.0, 3.0)
            path.lineTo(4.0, 4.0)
            path.lineTo(-4.0, 4.0)
            path.lineTo(-4.0, 3.0)
            path.lineTo(-2.0, 1.0)
            path.lineTo(-2.0, -4.8)
            path.lineTo(-4.0, -4.8)
            path.closeSubpath()
            painter.setPen(QPen(color, 1.3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            if self._filled:
                painter.setBrush(color)
            else:
                painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
            painter.drawLine(QPointF(0, 4.0), QPointF(0, 8.5))
        else:
            pen = QPen(color, 1.5, Qt.SolidLine, Qt.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(-4.0, -4.0), QPointF(4.0, 4.0))
            painter.drawLine(QPointF(4.0, -4.0), QPointF(-4.0, 4.0))
        painter.restore()


# ---------------------------------------------------------------------------
# History card content widgets


class TextPreview(QWidget):
    """Paints up to three wrapped lines, with an ellipsis on the last line."""

    MAX_LINES = 3

    def __init__(self, text: str, font: QFont, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._font = QFont(font)
        self._text = text[:20000]
        self.setFont(self._font)
        self.setFixedWidth(PREVIEW_WIDTH)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._lines: List[str] = []
        self._build_lines()
        metrics = QFontMetrics(self._font)
        self._ascent = metrics.ascent()
        self._line_spacing = metrics.lineSpacing()
        self._pen = QPen(QColor(COLOR_TEXT))
        self.setFixedHeight(max(1, len(self._lines)) * self._line_spacing)

    def _build_lines(self) -> None:
        # Preserve explicit line breaks while collapsing horizontal runs of
        # spaces for a tidy preview.
        source = self._text.replace("\r\n", "\n").replace("\r", "\n")
        paragraphs = source.split("\n")
        raw_lines: List[str] = []
        for paragraph in paragraphs:
            paragraph = re.sub(r"[ \t\f\v]+", " ", paragraph).strip()
            if not paragraph:
                raw_lines.append("")
            else:
                layout = QTextLayout(paragraph, self._font)
                option = QTextOption()
                option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
                layout.setTextOption(option)
                layout.beginLayout()
                try:
                    while True:
                        line = layout.createLine()
                        if not line.isValid():
                            break
                        line.setLineWidth(PREVIEW_WIDTH)
                        start = line.textStart()
                        length = line.textLength()
                        raw_lines.append(paragraph[start : start + length].rstrip())
                        if len(raw_lines) > self.MAX_LINES:
                            break
                finally:
                    layout.endLayout()
            if len(raw_lines) > self.MAX_LINES:
                break

        if not raw_lines:
            self._lines = [" "]
            return

        if len(raw_lines) > self.MAX_LINES:
            metrics = QFontMetrics(self._font)
            self._lines = raw_lines[: self.MAX_LINES - 1]
            remainder = " ".join(part for part in raw_lines[self.MAX_LINES - 1 :] if part)
            if not remainder:
                remainder = "…"
            last = metrics.elidedText(remainder, Qt.ElideRight, PREVIEW_WIDTH)
            if not last.endswith("…"):
                while last and metrics.horizontalAdvance(last + "…") > PREVIEW_WIDTH:
                    last = last[:-1]
                last = last.rstrip() + "…"
            self._lines.append(last)
        else:
            self._lines = raw_lines

        while len(self._lines) < self.MAX_LINES and self._lines and self._lines[-1] == "":
            self._lines.pop()
        if not self._lines:
            self._lines = [" "]

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.setFont(self._font)
        painter.setPen(self._pen)
        y = self._ascent
        for line in self._lines:
            painter.drawText(QPointF(0, y), line)
            y += self._line_spacing


class ImagePreview(QWidget):
    """Aspect-fit image thumbnail displayed directly without borders or letterboxes."""

    MAX_WIDTH = 240
    MAX_HEIGHT = 80

    def __init__(self, path: Optional[Path], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        if path is not None:
            self._load(path)
        else:
            self.setFixedSize(110, 48)

    def _load(self, path: Path) -> None:
        try:
            reader = QImageReader(str(path))
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and size.width() > 0 and size.height() > 0:
                scaled_size = size.scaled(
                    QSize(self.MAX_WIDTH, self.MAX_HEIGHT), Qt.KeepAspectRatio
                )
                reader.setScaledSize(scaled_size)
                image = reader.read()
                if not image.isNull():
                    self._pixmap = QPixmap.fromImage(image)
                    self.setFixedSize(scaled_size)
                    return
        except Exception:
            pass
        self._pixmap = None
        self.setFixedSize(110, 48)

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._pixmap is None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QColor(COLOR_MUTED))
            painter.drawText(self.rect(), Qt.AlignCenter, "Image unavailable")
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(self.rect()), 6, 6)
        painter.setClipPath(clip)
        painter.drawPixmap(0, 0, self._pixmap)


class ClipCard(QWidget):
    """One history entry. Whole-card click pastes; buttons never do."""

    activated = pyqtSignal(str)
    pinClicked = pyqtSignal(str)
    deleteClicked = pyqtSignal(str)
    dragDropped = pyqtSignal(str, object)
    moveUpRequested = pyqtSignal(str)
    moveDownRequested = pyqtSignal(str)

    def __init__(
        self,
        item: Dict[str, Any],
        image_path: Optional[Path],
        font: QFont,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self._selected = False
        self._hovered = False
        self._draggable = False
        self._drag_origin: Optional[QPoint] = None
        self._dragging = False
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.timeout.connect(self._refresh_hover)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 9, 10)
        row.setSpacing(8)

        if item["type"] == "image":
            preview = ImagePreview(image_path, self)
            preview.setToolTip(f"{item.get('width', '?')} × {item.get('height', '?')}")
            row.addWidget(preview, 0, Qt.AlignVCenter)
            card_height = max(preview.height() + 18, 54)
        else:
            preview = TextPreview(str(item.get("text", "")), font, self)
            row.addWidget(preview, 0, Qt.AlignVCenter)
            card_height = max(preview.height(), 26) + 20

        row.addStretch(1)

        actions = QWidget(self)
        actions.setFixedWidth(ACTION_AREA_WIDTH)
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(2)
        actions_layout.setAlignment(Qt.AlignCenter)
        actions_layout.setSizeConstraint(QHBoxLayout.SetFixedSize)

        self.pin_button = ActionButton("pin", actions)
        self.delete_button = ActionButton("close", actions)
        pinned = bool(item.get("pinned"))
        self.pin_button.set_filled(pinned)
        self.pin_button.setToolTip("Unpin" if pinned else "Pin")
        self.pin_button.setAccessibleName(self.pin_button.toolTip())
        actions_layout.addWidget(self.pin_button)
        actions_layout.addWidget(self.delete_button)
        row.addWidget(actions, 0, Qt.AlignVCenter)

        self.pin_button.clicked.connect(
            lambda: self.pinClicked.emit(str(self.item["id"]))
        )
        self.delete_button.clicked.connect(
            lambda: self.deleteClicked.emit(str(self.item["id"]))
        )
        # Keep the card hover state while the pointer is over its buttons.
        self.pin_button.installEventFilter(self)
        self.delete_button.installEventFilter(self)

        self.setFixedHeight(card_height)
        self._sync_action_visibility()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() in (QEvent.Enter, QEvent.Leave):
            self._hover_timer.start(0)
        return super().eventFilter(watched, event)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_timer.start(0)
        super().leaveEvent(event)

    def _refresh_hover(self) -> None:
        inside = self.rect().contains(self.mapFromGlobal(QCursor.pos()))
        self._set_hovered(inside)

    def _set_hovered(self, hovered: bool) -> None:
        if self._hovered == hovered:
            return
        self._hovered = hovered
        self._sync_action_visibility()
        self.update()

    def _sync_action_visibility(self) -> None:
        pinned = bool(self.item.get("pinned"))
        self.pin_button.setVisible(pinned or self._hovered)
        self.delete_button.setVisible(self._hovered)

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self.update()

    def set_draggable(self, draggable: bool) -> None:
        self._draggable = bool(draggable)
        self.setCursor(Qt.OpenHandCursor if self._draggable else Qt.PointingHandCursor)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._selected:
            painter.setBrush(BRUSH_CARD_SEL)
            painter.setPen(PEN_CARD_SEL_BORDER)
        elif self._hovered:
            painter.setBrush(BRUSH_CARD_HOVER)
            painter.setPen(PEN_CARD_HOVER_BORDER)
        else:
            painter.setBrush(BRUSH_CARD)
            painter.setPen(PEN_CARD_BORDER)

        painter.drawRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)

        if self._selected:
            # Signature Windows 11 vertical accent pill on left edge
            indicator_h = min(16.0, rect.height() * 0.45)
            indicator_w = 3.0
            indicator_x = rect.left() + 2.0
            indicator_y = rect.top() + (rect.height() - indicator_h) / 2.0
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(COLOR_ACCENT))
            painter.drawRoundedRect(
                QRectF(indicator_x, indicator_y, indicator_w, indicator_h), 1.5, 1.5
            )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_origin = event.pos()
            self._dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (
            self._draggable
            and self._drag_origin is not None
            and event.buttons() & Qt.LeftButton
            and (event.pos() - self._drag_origin).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            self._dragging = True
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            dragged = self._dragging
            self._dragging = False
            self._drag_origin = None
            if self._draggable:
                self.setCursor(Qt.OpenHandCursor)
            if dragged:
                self.dragDropped.emit(str(self.item["id"]), event.globalPos())
                event.accept()
                return
            if self.rect().contains(event.pos()):
                self.activated.emit(str(self.item["id"]))
                event.accept()
                return
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        menu.setStyleSheet(
            f"""
            QMenu {{
                background-color: {COLOR_PANEL};
                color: {COLOR_TEXT};
                border: 1px solid {COLOR_BORDER};
                padding: 4px;
            }}
            QMenu::item {{
                padding: 5px 22px 5px 12px;
                border-radius: 4px;
            }}
            QMenu::item:selected {{
                background-color: #3f6f9f;
            }}
            QMenu::item:disabled {{
                color: {COLOR_MUTED};
            }}
            """
        )
        item_id = str(self.item["id"])
        pinned = bool(self.item.get("pinned"))
        if pinned:
            up_act = menu.addAction("Move up (Ctrl+Up)")
            down_act = menu.addAction("Move down (Ctrl+Down)")
            if not self._draggable:
                up_act.setEnabled(False)
                down_act.setEnabled(False)
            menu.addSeparator()
            unpin_act = menu.addAction("Unpin")
            del_act = menu.addAction("Delete")
            chosen = menu.exec_(event.globalPos())
            if chosen == up_act:
                self.moveUpRequested.emit(item_id)
            elif chosen == down_act:
                self.moveDownRequested.emit(item_id)
            elif chosen == unpin_act:
                self.pinClicked.emit(item_id)
            elif chosen == del_act:
                self.deleteClicked.emit(item_id)
        else:
            pin_act = menu.addAction("Pin")
            del_act = menu.addAction("Delete")
            chosen = menu.exec_(event.globalPos())
            if chosen == pin_act:
                self.pinClicked.emit(item_id)
            elif chosen == del_act:
                self.deleteClicked.emit(item_id)


class SmoothScrollArea(QScrollArea):
    """A scroll area with smooth interpolated wheel scrolling and BitBlt acceleration."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._anim = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._target_value = 0

    def stop_animation(self) -> None:
        if self._anim.state() == QPropertyAnimation.Running:
            self._anim.stop()

    def wheelEvent(self, event) -> None:  # noqa: N802
        # Touchpads provide smooth high-frequency pixel deltas; let Qt handle them directly.
        if not event.pixelDelta().isNull():
            self.stop_animation()
            super().wheelEvent(event)
            return

        num_degrees = event.angleDelta().y() / 8.0
        num_steps = num_degrees / 15.0
        if num_steps == 0:
            super().wheelEvent(event)
            return

        # 1 wheel notch moves ~56px (roughly one card)
        delta = -num_steps * 56.0
        bar = self.verticalScrollBar()

        if self._anim.state() == QPropertyAnimation.Running:
            current_target = self._target_value
        else:
            current_target = bar.value()

        new_target = int(max(bar.minimum(), min(bar.maximum(), current_target + delta)))
        self._target_value = new_target

        if new_target != bar.value():
            self._anim.stop()
            self._anim.setStartValue(bar.value())
            self._anim.setEndValue(new_target)
            self._anim.start()
        event.accept()


# ---------------------------------------------------------------------------
# Popup


class ClipboardPopup(QWidget):
    """Frameless, self-drawn popup. It is only shown when summoned."""

    pasteRequested = pyqtSignal(str)
    pinRequested = pyqtSignal(str, object)
    deleteRequested = pyqtSignal(str)
    reorderRequested = pyqtSignal(str, int)
    clearRequested = pyqtSignal()
    settingsRequested = pyqtSignal()

    def __init__(
        self, images_dir: Path, categories: Optional[List[str]] = None
    ) -> None:
        super().__init__(None)
        self._images_dir = images_dir
        self.setWindowTitle("Clipboard history")
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setFixedWidth(PANEL_WIDTH + SHADOW_MARGIN * 2)

        self._items: List[Dict[str, Any]] = []
        self._cards: List[ClipCard] = []
        self._selected_id: Optional[str] = None
        self._focus_armed = False
        self._categories: List[str] = []
        self._category: Optional[str] = None
        self._opened_above = False
        self._anchor_bottom = 0
        self._shadow_cache: Optional[QPixmap] = None
        self._shadow_panel_geom: Optional[QRect] = None
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(140)
        self._hide_timer.timeout.connect(self._hide_if_inactive)
        self._click_check_timer = QTimer(self)
        self._click_check_timer.setInterval(25)
        self._click_check_timer.timeout.connect(self._check_outside_click)
        self._initial_active_window: Optional[int] = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        outer.setSpacing(0)

        self.panel = QFrame(self)
        self.panel.setObjectName("winclipPanel")
        self.panel.setFixedWidth(PANEL_WIDTH)
        self.panel.setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(self.panel)

        panel_layout = QVBoxLayout(self.panel)
        panel_layout.setContentsMargins(14, 14, 14, 12)
        panel_layout.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        title = QLabel("Clipboard history", self.panel)
        title.setObjectName("titleLabel")
        header.addWidget(title)
        header.addStretch(1)
        self.settings_button = QToolButton(self.panel)
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setIcon(make_settings_icon())
        self.settings_button.setIconSize(QSize(16, 16))
        self.settings_button.setToolTip("Categories and settings")
        self.settings_button.setCursor(Qt.PointingHandCursor)
        self.settings_button.setFocusPolicy(Qt.NoFocus)
        self.settings_button.clicked.connect(self.settingsRequested.emit)
        header.addWidget(self.settings_button, 0, Qt.AlignVCenter)
        panel_layout.addLayout(header)
        panel_layout.addSpacing(8)

        self.clear_button = QToolButton(self)
        self.clear_button.setObjectName("clearButton")
        self.clear_button.setText("Clear all")
        self.clear_button.setCursor(Qt.PointingHandCursor)
        self.clear_button.setFocusPolicy(Qt.NoFocus)
        self.clear_button.setToolTip("Remove all unpinned items")
        self.clear_button.clicked.connect(self.clearRequested.emit)
        self.clear_button.hide()

        self.category_tabs = QTabBar(self.panel)
        self.category_tabs.setObjectName("categoryTabs")
        self.category_tabs.setFocusPolicy(Qt.NoFocus)
        self.category_tabs.setExpanding(False)
        self.category_tabs.setDrawBase(False)
        self.category_tabs.setDocumentMode(True)
        self.category_tabs.setUsesScrollButtons(True)
        self.category_tabs.setElideMode(Qt.ElideNone)
        self.category_tabs.setMovable(False)
        self.category_tabs.currentChanged.connect(self._on_category_changed)
        panel_layout.addWidget(self.category_tabs)
        panel_layout.addSpacing(8)

        self.scroll = SmoothScrollArea(self.panel)
        self.scroll.setObjectName("historyScroll")
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setFocusPolicy(Qt.NoFocus)
        self.scroll.viewport().setAutoFillBackground(True)
        self.scroll.setStyleSheet(f"background-color: {COLOR_PANEL}; border: none;")
        self.scroll.viewport().setStyleSheet(f"background-color: {COLOR_PANEL}; border: none;")

        self.content = QWidget()
        self.content.setObjectName("historyContent")
        self.content.setAutoFillBackground(True)
        self.content.setStyleSheet(f"background-color: {COLOR_PANEL}; border: none;")
        self.list_layout = QVBoxLayout(self.content)
        self.list_layout.setContentsMargins(0, 0, 2, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.content)
        panel_layout.addWidget(self.scroll)

        self._apply_styles()
        self.set_categories(categories or [])

        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_focus_changed)
        gui_app = QGuiApplication.instance()
        if gui_app is not None:
            try:
                gui_app.focusWindowChanged.connect(self._on_focus_window_changed)
            except AttributeError:
                pass

    # -------------------------------------------------------------- appearance

    def _apply_styles(self) -> None:
        self.panel.setStyleSheet(
            f"""
            QFrame#winclipPanel {{
                background-color: {COLOR_PANEL};
                border: 1px solid {COLOR_BORDER};
                border-radius: {PANEL_RADIUS}px;
            }}
            QLabel#titleLabel {{
                color: #ffffff;
                font-size: 13pt;
                font-weight: 600;
            }}
            QToolButton#clearButton {{
                color: {COLOR_MUTED};
                background-color: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 8.5pt;
                font-weight: 500;
            }}
            QToolButton#clearButton:hover {{
                color: #ffffff;
                background-color: rgba(255, 255, 255, 0.12);
                border-color: rgba(255, 255, 255, 0.15);
            }}
            QToolButton#clearButton:pressed {{
                background-color: rgba(255, 255, 255, 0.08);
            }}
            QToolButton#clearButton:disabled {{
                color: #555555;
                background-color: transparent;
                border-color: transparent;
            }}
            QToolButton#settingsButton {{
                color: {COLOR_MUTED};
                background: transparent;
                border: none;
                border-radius: 6px;
                padding: 4px;
            }}
            QToolButton#settingsButton:hover {{
                color: #ffffff;
                background-color: rgba(255, 255, 255, 0.10);
            }}
            QToolButton#settingsButton:pressed {{
                background-color: rgba(255, 255, 255, 0.06);
            }}
            QTabBar#categoryTabs {{
                background: transparent;
            }}
            QTabBar#categoryTabs::tab {{
                color: #c4c4c4;
                background-color: rgba(255, 255, 255, 0.05);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 12px;
                padding: 3px 12px;
                margin-right: 6px;
                min-width: 0;
                font-size: 9pt;
                font-weight: 500;
            }}
            QTabBar#categoryTabs::tab:selected {{
                color: #ffffff;
                background-color: #0078d4;
                border: 1px solid #1a88e1;
                font-weight: 600;
            }}
            QTabBar#categoryTabs::tab:hover:!selected {{
                color: #ffffff;
                background-color: rgba(255, 255, 255, 0.10);
                border-color: rgba(255, 255, 255, 0.14);
            }}
            QTabBar#categoryTabs QToolButton {{
                background: transparent;
                border: none;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 5px;
                margin: 2px 1px 2px 0;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(255, 255, 255, 0.22);
                min-height: 24px;
                border-radius: 2.5px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba(255, 255, 255, 0.38);
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0;
                background: transparent;
            }}
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            QToolTip {{
                color: #f0f0f0;
                background-color: #2b2b2b;
                border: 1px solid #454545;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 9pt;
            }}
            """
        )

    def _get_shadow_cache(self) -> QPixmap:
        p_geom = self.panel.geometry()
        if (
            self._shadow_cache is not None
            and self._shadow_panel_geom == p_geom
            and self._shadow_cache.size() == self.size()
        ):
            return self._shadow_cache

        pix = QPixmap(self.size())
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        panel_rect = QRectF(p_geom)
        painter.setPen(Qt.NoPen)
        for step in range(16, 0, -1):
            spread = float(step)
            norm = (17 - step) / 16.0
            alpha = int(22 * (norm ** 2.2))
            if alpha <= 0:
                continue
            shadow_rect = panel_rect.adjusted(
                -spread, -spread + 3.5, spread, spread + 3.5
            )
            painter.setBrush(QColor(0, 0, 0, alpha))
            painter.drawRoundedRect(
                shadow_rect,
                PANEL_RADIUS + spread,
                PANEL_RADIUS + spread,
            )
        painter.end()
        self._shadow_cache = pix
        self._shadow_panel_geom = QRect(p_geom)
        return pix

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._get_shadow_cache())

    # ---------------------------------------------------------------- categories

    def set_categories(self, categories: List[str]) -> None:
        """Refresh the category tabs while preserving the current selection."""
        self._categories = list(categories)
        previous = self._category
        self.category_tabs.blockSignals(True)
        while self.category_tabs.count():
            self.category_tabs.removeTab(0)
        self.category_tabs.addTab("All")
        for name in self._categories:
            self.category_tabs.addTab(name)
        target = 0
        if previous is not None and previous in self._categories:
            target = self._categories.index(previous) + 1
        self.category_tabs.setCurrentIndex(target)
        self.category_tabs.blockSignals(False)
        self._category = None if target == 0 else self._categories[target - 1]
        self.category_tabs.setVisible(bool(self._categories))

    def current_category(self) -> Optional[str]:
        return self._category

    def _on_category_changed(self, index: int) -> None:
        if index < 0:
            return
        self._category = None if index == 0 else self.category_tabs.tabText(index)
        self._selected_id = None
        self._rebuild(keep_selection=False)
        self._resize_to_contents()
        if self.isVisible():
            self._clamp_current_position()

    # ---------------------------------------------------------------- lifecycle

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key_Escape:
            self.hide()
            event.accept()
            return
        if key == Qt.Key_Up:
            if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
                if self._move_selected_pin(-1):
                    event.accept()
                    return
            self._navigate(-1)
            event.accept()
            return
        if key == Qt.Key_Down:
            if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
                if self._move_selected_pin(1):
                    event.accept()
                    return
            self._navigate(1)
            event.accept()
            return
        if key in (Qt.Key_Left, Qt.Key_Right) and self.category_tabs.count() > 1:
            step = -1 if key == Qt.Key_Left else 1
            index = (self.category_tabs.currentIndex() + step) % self.category_tabs.count()
            self.category_tabs.setCurrentIndex(index)
            event.accept()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._activate_selected()
            event.accept()
            return
        if key == Qt.Key_Delete:
            self._delete_selected()
            event.accept()
            return
        if key == Qt.Key_P and event.modifiers() & Qt.ControlModifier:
            self._toggle_selected_pin()
            event.accept()
            return
        if key == Qt.Key_Comma and event.modifiers() & Qt.ControlModifier:
            self.settingsRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def _arm_focus(self) -> None:
        if self.isVisible():
            self._focus_armed = True
            if self._initial_active_window is None:
                self._initial_active_window = _x11_helper.get_active_window()

    def event(self, event) -> bool:
        if event.type() == QEvent.WindowActivate:
            self._hide_timer.stop()
        elif event.type() == QEvent.WindowDeactivate:
            self._schedule_hide_check()
        return super().event(event)

    def _schedule_hide_check(self) -> None:
        if self.isVisible():
            self._hide_timer.start()

    def _other_popup_open(self) -> bool:
        popup = QApplication.activePopupWidget()
        if popup is not None and popup is not self:
            return True
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QMenu) and widget.isVisible():
                return True
        return False

    def _check_outside_click(self) -> None:
        if not self.isVisible() or not self._focus_armed:
            return
        if self._other_popup_open():
            return
        if QWidget.mouseGrabber() is not None:
            return

        # 1. Did the user press a mouse button anywhere outside the popup panel?
        if _x11_helper.is_button_pressed():
            panel_geom = self.panel.geometry()
            top_left = self.mapToGlobal(panel_geom.topLeft())
            panel_global = QRect(top_left, panel_geom.size())
            if not panel_global.contains(QCursor.pos()):
                self.hide()
                return

        # 2. Did the active window change (e.g. user Alt-Tabbed or clicked another window)?
        if self._initial_active_window is not None:
            current_active = _x11_helper.get_active_window()
            if (
                current_active is not None
                and current_active != self._initial_active_window
                and current_active != int(self.winId())
            ):
                self.hide()
                return

    def _hide_if_inactive(self) -> None:
        if not self.isVisible() or not self._focus_armed:
            return
        if self._other_popup_open():
            return
        if bool(self.windowFlags() & Qt.WindowDoesNotAcceptFocus):
            return
        handle = self.windowHandle()
        if handle is not None and handle.isActive():
            return
        if QApplication.activeWindow() is self:
            return
        self.hide()

    def _on_focus_changed(self, old, new) -> None:
        if not self.isVisible() or not self._focus_armed:
            return
        if self._other_popup_open():
            return
        if bool(self.windowFlags() & Qt.WindowDoesNotAcceptFocus):
            return
        if new is None or not (new is self or self.isAncestorOf(new)):
            self._schedule_hide_check()

    def _on_focus_window_changed(self, window) -> None:
        if not self.isVisible() or not self._focus_armed:
            return
        if self._other_popup_open():
            return
        if bool(self.windowFlags() & Qt.WindowDoesNotAcceptFocus):
            return
        own = self.windowHandle()
        if own is not None and window is not own:
            # Another window (another app, Alt-Tab, the desktop, etc.) became
            # active. Re-check after the focus transition settles, so resizing
            # or rebuilding the popup cannot hide it by accident.
            self._schedule_hide_check()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._focus_armed = False
        QTimer.singleShot(180, self._arm_focus)
        self.raise_()
        try:
            self.grabKeyboard()
        except Exception:
            pass
        self._click_check_timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._focus_armed = False
        self._click_check_timer.stop()
        self._initial_active_window = None
        try:
            self.releaseKeyboard()
        except Exception:
            pass
        if hasattr(self, "scroll") and hasattr(self.scroll, "stop_animation"):
            self.scroll.stop_animation()
        super().hideEvent(event)

    def reactivate(self) -> None:
        self.raise_()
        try:
            self.grabKeyboard()
        except Exception:
            pass
        self._focus_armed = False
        self._initial_active_window = _x11_helper.get_active_window()
        QTimer.singleShot(180, self._arm_focus)
        self._click_check_timer.start()

    # ---------------------------------------------------------------- contents

    def show_history(self, items: List[Dict[str, Any]]) -> None:
        self._items = list(items)
        self._selected_id = None
        self._rebuild(keep_selection=False)
        self._resize_to_contents()
        self._position_near_cursor()
        self._initial_active_window = _x11_helper.get_active_window()
        self.show()

    def refresh_history(
        self, items: List[Dict[str, Any]], selected_id: Optional[str] = None
    ) -> None:
        self._items = list(items)
        preferred = selected_id if selected_id is not None else self._selected_id
        self._rebuild(keep_selection=True, preferred_id=preferred)
        if self.isVisible():
            self._resize_to_contents()
            self._clamp_current_position()

    def _clear_cards(self) -> None:
        if hasattr(self, "clear_button") and self.clear_button is not None:
            self.clear_button.setParent(self)
            self.clear_button.hide()
        while self.list_layout.count():
            layout_item = self.list_layout.takeAt(0)
            widget = layout_item.widget()
            if widget is not None and widget is not self.clear_button:
                widget.deleteLater()
        self._cards = []

    def _rebuild(
        self,
        keep_selection: bool,
        preferred_id: Optional[str] = None,
    ) -> None:
        previous = self._selected_id
        self._clear_cards()

        if self._category is None:
            pinned = [item for item in self._items if item.get("pinned")]
            pinned.sort(
                key=lambda item: (
                    item["pin_order"]
                    if type(item.get("pin_order")) is int
                    else 2**31
                )
            )
            recent = [item for item in self._items if not item.get("pinned")]
        else:
            pinned = [
                item
                for item in self._items
                if item.get("pinned") and item.get("category") == self._category
            ]
            pinned.sort(
                key=lambda item: (
                    item["pin_order"]
                    if type(item.get("pin_order")) is int
                    else 2**31
                )
            )
            recent = []

        if not pinned and not recent:
            if hasattr(self, "clear_button") and self.clear_button is not None:
                self.clear_button.hide()
            self._add_empty_state()
        else:
            if recent:
                self.list_layout.addWidget(self._recent_section_header())
                for item in recent:
                    self._add_card(item)
            else:
                if hasattr(self, "clear_button") and self.clear_button is not None:
                    self.clear_button.hide()
            if pinned:
                if recent:
                    self.list_layout.addSpacing(2)
                    divider = QFrame(self.content)
                    divider.setFixedHeight(1)
                    divider.setStyleSheet("background-color: #3c3c3c; border: none;")
                    self.list_layout.addWidget(divider)
                    divider.show()
                if self._category is None:
                    tooltip = (
                        "Drag a pinned clip, or select it and press Ctrl+Up / "
                        "Ctrl+Down, to change its position."
                    )
                else:
                    tooltip = ""
                self.list_layout.addWidget(self._section_label("Pinned", tooltip))
                for item in pinned:
                    self._add_card(item)

        visible_ids = [str(card.item["id"]) for card in self._cards]
        if keep_selection:
            selected = preferred_id or previous
        else:
            selected = None
        if selected not in visible_ids:
            selected = visible_ids[0] if visible_ids else None
        self._selected_id = selected
        self._apply_selection()

        unpinned = any(not item.get("pinned") for item in self._items)
        self.clear_button.setEnabled(unpinned)

    def _add_empty_state(self) -> None:
        holder = QWidget(self.content)
        holder.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(16, 24, 16, 24)
        layout.setSpacing(5)
        if self._category is None:
            title = "No clipboard history yet"
            subtitle = "Things you copy will appear here."
        else:
            title = f"Nothing pinned in {self._category}"
            subtitle = "Pin a clip from All and assign it to this category."
        first = QLabel(title, holder)
        first.setAlignment(Qt.AlignCenter)
        first.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 10pt;")
        second = QLabel(subtitle, holder)
        second.setAlignment(Qt.AlignCenter)
        second.setStyleSheet(f"color: {COLOR_MUTED}; font-size: 9pt;")
        for label in (first, second):
            label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(first)
        layout.addWidget(second)
        holder.show()
        first.show()
        second.show()
        self.list_layout.addWidget(holder)

    def _recent_section_header(self) -> QWidget:
        row = QWidget(self.content)
        row.setObjectName("recentHeaderRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(4, 4, 2, 4)
        layout.setSpacing(0)

        label = QLabel("Recent", row)
        label.setStyleSheet(
            f"color: {COLOR_MUTED}; font-size: 8.5pt; font-weight: 600; letter-spacing: 0.5px;"
        )
        layout.addWidget(label, 0, Qt.AlignVCenter)
        layout.addStretch(1)

        self.clear_button.setParent(row)
        layout.addWidget(self.clear_button, 0, Qt.AlignVCenter)
        self.clear_button.show()
        row.show()
        return row

    def _section_label(self, text: str, tooltip: str = "") -> QLabel:
        label = QLabel(text, self.content)
        label.setContentsMargins(4, 4, 0, 2)
        label.setStyleSheet(
            f"color: {COLOR_MUTED}; font-size: 8.5pt; font-weight: 600; letter-spacing: 0.5px;"
        )
        if tooltip:
            label.setToolTip(tooltip)
        # New widgets do not get their stylesheet-dependent size hint until
        # they are polished. Resizing the popup before the event loop would
        # otherwise measure the label as zero-height and clip it.
        label.ensurePolished()
        label.show()
        return label

    def _add_card(self, item: Dict[str, Any]) -> None:
        preview_font = QFont(self.font())
        preview_font.setPointSizeF(10.0)
        card = ClipCard(item, self._image_path(item), preview_font, self.content)
        card.activated.connect(self.pasteRequested.emit)
        card.pinClicked.connect(self._request_pin)
        card.deleteClicked.connect(self.deleteRequested.emit)
        card.dragDropped.connect(self._on_drag_dropped)
        card.moveUpRequested.connect(lambda i=str(item["id"]): self._move_pin_by_id(i, -1))
        card.moveDownRequested.connect(lambda i=str(item["id"]): self._move_pin_by_id(i, 1))
        card.set_draggable(self._category is None and bool(item.get("pinned")))
        self.list_layout.addWidget(card)
        card.show()
        self._cards.append(card)

    def _image_path(self, item: Dict[str, Any]) -> Optional[Path]:
        filename = item.get("image")
        if not isinstance(filename, str) or Path(filename).name != filename:
            return None
        path = self._images_dir / filename
        return path if path.is_file() else None

    # --------------------------------------------------------------- selection

    def _apply_selection(self) -> None:
        for card in self._cards:
            card.set_selected(str(card.item["id"]) == self._selected_id)
        if self._selected_id is not None:
            QTimer.singleShot(0, self._scroll_to_selection)

    def _scroll_to_selection(self) -> None:
        if self._selected_id is None:
            return
        for card in self._cards:
            if str(card.item["id"]) == self._selected_id:
                self.scroll.ensureWidgetVisible(card, 0, 6)
                break

    def _navigate(self, delta: int) -> None:
        ids = [str(card.item["id"]) for card in self._cards]
        if not ids:
            return
        try:
            index = ids.index(self._selected_id) if self._selected_id in ids else 0
        except ValueError:
            index = 0
        index = max(0, min(len(ids) - 1, index + delta))
        self._selected_id = ids[index]
        self._apply_selection()

    def selected_item_id(self) -> Optional[str]:
        return self._selected_id

    def _activate_selected(self) -> None:
        if self._selected_id is not None:
            self.pasteRequested.emit(self._selected_id)

    def _delete_selected(self) -> None:
        if self._selected_id is not None:
            self.deleteRequested.emit(self._selected_id)

    def _toggle_selected_pin(self) -> None:
        if self._selected_id is None:
            return
        item = next(
            (
                entry
                for entry in self._items
                if str(entry["id"]) == str(self._selected_id)
            ),
            None,
        )
        if item is not None and not item.get("pinned"):
            # Keyboard pinning uses the active category (or uncategorized in
            # the All view) instead of opening the mouse category menu.
            self.pinRequested.emit(self._selected_id, self._category)
        else:
            self.pinRequested.emit(self._selected_id, None)

    def _move_pin_by_id(self, item_id: str, delta: int) -> bool:
        if self._category is not None:
            return False
        pinned_cards = [c for c in self._cards if c.item.get("pinned")]
        if not pinned_cards:
            return False
        current_index = next(
            (i for i, c in enumerate(pinned_cards) if str(c.item["id"]) == str(item_id)),
            None,
        )
        if current_index is None:
            return False
        new_index = current_index + delta
        if 0 <= new_index < len(pinned_cards):
            self.reorderRequested.emit(str(item_id), new_index)
            return True
        return False

    def _move_selected_pin(self, delta: int) -> bool:
        if self._selected_id is None:
            return False
        card = next(
            (c for c in self._cards if str(c.item["id"]) == str(self._selected_id)),
            None,
        )
        if card is None or not card.item.get("pinned"):
            return False
        self._move_pin_by_id(self._selected_id, delta)
        return True

    def _on_drag_dropped(self, item_id: str, global_pos: QPoint) -> None:
        if self._category is not None:
            return
        pinned_cards = [c for c in self._cards if c.item.get("pinned")]
        if not pinned_cards:
            return
        source_index = next(
            (i for i, c in enumerate(pinned_cards) if str(c.item["id"]) == str(item_id)),
            None,
        )
        if source_index is None:
            return

        content_pos = self.content.mapFromGlobal(global_pos)
        drop_y = content_pos.y()

        target_index = min(
            range(len(pinned_cards)),
            key=lambda i: abs(drop_y - pinned_cards[i].geometry().center().y()),
        )

        if target_index != source_index:
            self.reorderRequested.emit(str(item_id), target_index)

    def _request_pin(self, item_id: str) -> None:
        item = next(
            (entry for entry in self._items if str(entry["id"]) == str(item_id)),
            None,
        )
        if item is None:
            return
        if item.get("pinned"):
            self.pinRequested.emit(item_id, None)
            return
        if not self._categories:
            self.pinRequested.emit(item_id, None)
            return
        if self._category is not None:
            self.pinRequested.emit(item_id, self._category)
            return

        menu = QMenu(self)
        menu.setStyleSheet(
            """
            QMenu {
                background-color: #2b2b2b;
                color: #ececec;
                border: 1px solid #414141;
                padding: 4px;
            }
            QMenu::item {
                padding: 5px 22px 5px 12px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #3f6f9f;
            }
            QMenu::item:disabled {
                color: #9b9b9b;
            }
            """
        )
        heading = menu.addAction("Pin to category")
        heading.setEnabled(False)
        for name in self._categories:
            action = menu.addAction(name)
            action.setData(name)
        menu.addSeparator()
        uncategorized = menu.addAction("Uncategorized")
        uncategorized.setData(None)
        chosen = menu.exec_(QCursor.pos())
        if chosen is not None:
            self.pinRequested.emit(item_id, chosen.data())

    # ------------------------------------------------------------------ geometry

    def _resize_to_contents(self) -> None:
        self.list_layout.activate()
        content_height = self.list_layout.sizeHint().height()
        self.scroll.setFixedHeight(max(1, min(content_height, MAX_SCROLL_HEIGHT)))
        self.panel.layout().activate()
        panel_height = self.panel.sizeHint().height()
        total_height = panel_height + SHADOW_MARGIN * 2
        self.setFixedSize(PANEL_WIDTH + SHADOW_MARGIN * 2, total_height)

    def _screen_for_cursor(self):
        position = QCursor.pos()
        app = QGuiApplication.instance()
        screen = app.screenAt(position) if app is not None else None
        return screen or QGuiApplication.primaryScreen()

    def _position_near_cursor(self) -> None:
        screen = self._screen_for_cursor()
        if screen is None:
            return
        available = screen.availableGeometry()
        cursor = QCursor.pos()

        panel_width = PANEL_WIDTH
        panel_height = self.panel.sizeHint().height()

        gap_below = 16
        gap_above = 36
        screen_margin = 12

        # Horizontal positioning: center the panel horizontally over the cursor
        px = cursor.x() - panel_width // 2
        px = max(
            available.left() + screen_margin,
            min(px, available.right() - screen_margin - panel_width + 1),
        )

        # Vertical positioning: open below if there is room, otherwise open above with generous clearance
        space_below = available.bottom() - screen_margin - (cursor.y() + gap_below)
        if space_below >= panel_height:
            self._opened_above = False
            py = cursor.y() + gap_below
        else:
            self._opened_above = True
            py = cursor.y() - panel_height - gap_above

        py = max(available.top() + screen_margin, min(py, available.bottom() - screen_margin - panel_height + 1))

        self._anchor_bottom = py + panel_height

        wx = px - SHADOW_MARGIN
        wy = py - SHADOW_MARGIN
        self.move(int(wx), int(wy))

    def _clamp_current_position(self) -> None:
        screen = self._screen_for_cursor()
        if screen is None:
            return
        available = screen.availableGeometry()
        panel_width = PANEL_WIDTH
        panel_height = self.panel.sizeHint().height()
        screen_margin = 12

        px = self.x() + SHADOW_MARGIN
        px = max(available.left() + screen_margin, min(px, available.right() - screen_margin - panel_width + 1))

        if getattr(self, "_opened_above", False) and hasattr(self, "_anchor_bottom"):
            py = self._anchor_bottom - panel_height
        else:
            py = self.y() + SHADOW_MARGIN

        py = max(available.top() + screen_margin, min(py, available.bottom() - screen_margin - panel_height + 1))

        wx = px - SHADOW_MARGIN
        wy = py - SHADOW_MARGIN
        self.move(int(wx), int(wy))


# ---------------------------------------------------------------------------
# Settings dialog


class SettingsComboBox(QComboBox):
    """QComboBox with a vector-drawn chevron down arrow."""

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        is_hovered = self.underMouse()
        color = QColor("#ffffff" if is_hovered else "#a8a8a8")
        pen = QPen(color, 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        cx = self.width() - 14
        cy = self.height() // 2 - 1
        path = QPainterPath()
        path.moveTo(cx - 3.5, cy - 2)
        path.lineTo(cx, cy + 1.5)
        path.lineTo(cx + 3.5, cy - 2)
        painter.drawPath(path)


class CategoryItemDelegate(QStyledItemDelegate):
    """Paints category items with aligned name on left and pill badge on right."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

    def sizeHint(self, option: Any, index: Any) -> QSize:
        return QSize(option.rect.width(), 32)

    def paint(self, painter: QPainter, option: Any, index: Any) -> None:
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        rect = option.rect
        is_selected = bool(option.state & QStyle.State_Selected)
        is_hovered = bool(option.state & QStyle.State_MouseOver)

        # Background selection / hover highlight
        if is_selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#0078d4"))
            painter.drawRoundedRect(rect.adjusted(3, 1, -3, -1), 6, 6)
        elif is_hovered:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#2c2c2c"))
            painter.drawRoundedRect(rect.adjusted(3, 1, -3, -1), 6, 6)

        # Data
        name = str(index.data(Qt.UserRole) or index.data(Qt.DisplayRole) or "")
        count = index.data(Qt.UserRole + 1)
        if count is None:
            count = 0

        # Colors & Font
        text_color = QColor("#ffffff") if is_selected else QColor("#f0f0f0")
        font = painter.font()
        font.setPointSize(9)
        font.setWeight(QFont.DemiBold if is_selected else QFont.Normal)
        painter.setFont(font)

        badge_width = 0
        if count > 0:
            badge_text = f"{count} pinned"
            badge_font = QFont(font)
            badge_font.setPointSize(8)
            badge_metrics = QFontMetrics(badge_font)
            if hasattr(badge_metrics, "horizontalAdvance"):
                badge_text_w = badge_metrics.horizontalAdvance(badge_text)
            else:
                badge_text_w = badge_metrics.width(badge_text)
            badge_width = badge_text_w + 14
            badge_height = 18
            badge_x = rect.right() - 8 - badge_width
            badge_y = rect.top() + (rect.height() - badge_height) // 2
            badge_rect = QRect(badge_x, badge_y, badge_width, badge_height)

            # Badge pill
            painter.setPen(Qt.NoPen)
            badge_bg = (
                QColor(255, 255, 255, 45)
                if is_selected
                else QColor(255, 255, 255, 20)
            )
            painter.setBrush(badge_bg)
            painter.drawRoundedRect(badge_rect, 9, 9)

            # Badge text
            painter.setFont(badge_font)
            badge_fg = QColor("#ffffff") if is_selected else QColor("#9e9e9e")
            painter.setPen(badge_fg)
            painter.drawText(badge_rect, Qt.AlignCenter, badge_text)

        # Draw Category Name (elided if it gets too long)
        painter.setFont(font)
        painter.setPen(text_color)
        max_name_width = rect.width() - 20 - (badge_width + 8 if badge_width else 0)
        metrics = QFontMetrics(font)
        elided_name = metrics.elidedText(name, Qt.ElideRight, max(20, max_name_width))
        name_rect = QRect(rect.left() + 10, rect.top(), max_name_width, rect.height())
        painter.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, elided_name)

        painter.restore()


class CategorySettingsDialog(QDialog):
    """Manage the category buttons and assign pinned items to them."""

    def __init__(self, store: HistoryStore, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.store = store
        self.changed = False
        self.setWindowTitle("WinClip settings")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.resize(490, 520)
        self.setStyleSheet(
            """
            QDialog {
                background-color: #202020;
            }
            QLabel {
                color: #f0f0f0;
            }
            QLabel#sectionTitle {
                font-size: 11pt;
                font-weight: 600;
                color: #ffffff;
            }
            QLabel#hint {
                color: #8e8e8e;
                font-size: 9pt;
            }
            QListWidget {
                background-color: #262626;
                color: #f0f0f0;
                border: 1px solid #363636;
                border-radius: 8px;
                padding: 4px;
                outline: none;
            }
            QPushButton#categoryBtn {
                background-color: #2e2e2e;
                color: #f0f0f0;
                border: 1px solid #3d3d3d;
                border-radius: 6px;
                padding: 5px 14px;
                font-size: 9pt;
                font-weight: 500;
                min-height: 22px;
            }
            QPushButton#categoryBtn:hover {
                background-color: #383838;
                border-color: #4f4f4f;
                color: #ffffff;
            }
            QPushButton#categoryBtn:pressed {
                background-color: #242424;
            }
            QPushButton#categoryBtn:disabled {
                background-color: #242424;
                border-color: #303030;
                color: #555555;
            }
            QScrollArea#itemsScroll {
                background-color: #262626;
                border: 1px solid #363636;
                border-radius: 8px;
            }
            QComboBox {
                background-color: #2e2e2e;
                color: #f0f0f0;
                border: 1px solid #3d3d3d;
                border-radius: 6px;
                padding: 4px 10px;
                font-size: 9pt;
                min-height: 20px;
            }
            QComboBox:hover {
                background-color: #363636;
                border-color: #4d4d4d;
            }
            QComboBox:focus {
                border-color: #4cc2ff;
            }
            QComboBox::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 22px;
                border-left: none;
            }
            QComboBox QAbstractItemView {
                background-color: #262626;
                color: #f0f0f0;
                border: 1px solid #3d3d3d;
                border-radius: 6px;
                padding: 4px;
                selection-background-color: #0078d4;
                selection-color: #ffffff;
                outline: none;
            }
            QComboBox QAbstractItemView::item {
                min-height: 26px;
                padding: 4px 8px;
                border-radius: 4px;
            }
            QDialogButtonBox {
                dialogbuttonbox-buttons-have-icons: 0;
            }
            QDialogButtonBox QPushButton {
                background-color: #303030;
                color: #f0f0f0;
                border: 1px solid #404040;
                border-radius: 6px;
                padding: 6px 20px;
                font-size: 9.5pt;
                font-weight: 500;
                min-width: 72px;
            }
            QDialogButtonBox QPushButton:hover {
                background-color: #3a3a3a;
                border-color: #505050;
                color: #ffffff;
            }
            QDialogButtonBox QPushButton:pressed {
                background-color: #242424;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 5px;
                margin: 2px 1px 2px 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.22);
                min-height: 20px;
                border-radius: 2.5px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(255, 255, 255, 0.38);
            }
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {
                height: 0;
                background: transparent;
            }
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {
                background: transparent;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)

        categories_title = QLabel("Categories")
        categories_title.setObjectName("sectionTitle")
        layout.addWidget(categories_title)

        categories_hint = QLabel(
            "These appear as buttons above clipboard history. "
            "Pinned items can be assigned to one category."
        )
        categories_hint.setObjectName("hint")
        categories_hint.setWordWrap(True)
        layout.addWidget(categories_hint)

        self.category_list = QListWidget(self)
        self.category_list.setItemDelegate(CategoryItemDelegate(self.category_list))
        self.category_list.setFixedHeight(116)
        layout.addWidget(self.category_list)

        category_buttons = QHBoxLayout()
        category_buttons.setSpacing(8)
        self.add_button = QPushButton("Add")
        self.rename_button = QPushButton("Rename")
        self.delete_button = QPushButton("Delete")
        for button in (self.add_button, self.rename_button, self.delete_button):
            button.setObjectName("categoryBtn")
            button.setCursor(Qt.PointingHandCursor)
            category_buttons.addWidget(button)
        category_buttons.addStretch(1)
        layout.addLayout(category_buttons)
        layout.addSpacing(4)

        pinned_title = QLabel("Pinned items")
        pinned_title.setObjectName("sectionTitle")
        layout.addWidget(pinned_title)

        pinned_hint = QLabel("Choose the category each pinned clip belongs to.")
        pinned_hint.setObjectName("hint")
        layout.addWidget(pinned_hint)

        self.items_scroll = QScrollArea(self)
        self.items_scroll.setObjectName("itemsScroll")
        self.items_scroll.setWidgetResizable(True)
        self.items_scroll.setFixedHeight(190)
        self.items_scroll.setFocusPolicy(Qt.NoFocus)
        self.items_host = QWidget()
        self.items_host.setStyleSheet("background-color: #262626; border: none;")
        self.items_scroll.viewport().setStyleSheet(
            "background-color: #262626; border: none;"
        )
        self.items_layout = QVBoxLayout(self.items_host)
        self.items_layout.setContentsMargins(6, 6, 6, 6)
        self.items_layout.setSpacing(6)
        self.items_scroll.setWidget(self.items_host)
        layout.addWidget(self.items_scroll)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn = buttons.button(QDialogButtonBox.Close)
        if close_btn is not None:
            close_btn.setIcon(QIcon())
            close_btn.setCursor(Qt.PointingHandCursor)
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)

        self.add_button.clicked.connect(self._add_category)
        self.rename_button.clicked.connect(self._rename_category)
        self.delete_button.clicked.connect(self._delete_category)
        self.category_list.itemSelectionChanged.connect(self._sync_buttons)
        self.category_list.itemDoubleClicked.connect(
            lambda _item: self._rename_category()
        )
        self._reload()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if not getattr(self, "_positioned", False):
            self._positioned = True
            app = QGuiApplication.instance()
            screen = app.screenAt(QCursor.pos()) if app else None
            if not screen and app:
                screen = QGuiApplication.primaryScreen()
            if screen:
                geo = screen.availableGeometry()
                x = geo.center().x() - self.width() // 2
                y = geo.center().y() - self.height() // 2
                self.move(max(geo.left(), x), max(geo.top(), y))

    # --------------------------------------------------------------- category UI

    def _selected_category(self) -> Optional[str]:
        entry = self.category_list.currentItem()
        if entry is None:
            return None
        return entry.data(Qt.UserRole)

    def _sync_buttons(self) -> None:
        has_selection = self._selected_category() is not None
        self.rename_button.setEnabled(has_selection)
        self.delete_button.setEnabled(has_selection)

    def _add_category(self) -> None:
        name, accepted = QInputDialog.getText(self, "Add category", "Category name:")
        if not accepted:
            return
        added = self.store.add_category(name)
        if added is None:
            QMessageBox.warning(
                self,
                "Category not added",
                "The name is empty or already used, or the category limit was reached.",
            )
            return
        self.changed = True
        self._reload()

    def _rename_category(self) -> None:
        old = self._selected_category()
        if old is None:
            return
        name, accepted = QInputDialog.getText(
            self, "Rename category", "Category name:", text=old
        )
        if not accepted:
            return
        if not self.store.rename_category(old, name):
            QMessageBox.warning(
                self,
                "Category not renamed",
                "The name is empty or already used by another category.",
            )
            return
        self.changed = True
        self._reload()

    def _delete_category(self) -> None:
        name = self._selected_category()
        if name is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete category",
            f"Delete “{name}”?\n\nPinned items in it stay pinned but become uncategorized.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if self.store.remove_category(name):
            self.changed = True
            self._reload()

    # ----------------------------------------------------------------- contents

    def _reload(self) -> None:
        selected = self._selected_category()
        self.category_list.blockSignals(True)
        self.category_list.clear()
        selected_row = -1
        for row, name in enumerate(self.store.categories):
            count = sum(
                1
                for item in self.store.items
                if item.get("pinned") and item.get("category") == name
            )
            label = f"{name}    {count} pinned" if count else name
            entry = QListWidgetItem(label)
            entry.setData(Qt.UserRole, name)
            entry.setData(Qt.UserRole + 1, count)
            self.category_list.addItem(entry)
            if name == selected:
                selected_row = row
        if selected_row >= 0:
            self.category_list.setCurrentRow(selected_row)
        elif self.category_list.count():
            self.category_list.setCurrentRow(0)
        self.category_list.blockSignals(False)
        self._sync_buttons()
        self._reload_pinned()

    def _reload_pinned(self) -> None:
        while self.items_layout.count():
            layout_item = self.items_layout.takeAt(0)
            widget = layout_item.widget()
            if widget is not None:
                widget.deleteLater()

        pinned = [item for item in self.store.items if item.get("pinned")]
        if not pinned:
            empty_box = QWidget(self.items_host)
            empty_box.setStyleSheet("background: transparent; border: none;")
            empty_layout = QVBoxLayout(empty_box)
            empty_layout.setContentsMargins(16, 36, 16, 36)
            empty_layout.setAlignment(Qt.AlignCenter)
            empty_label = QLabel("No pinned items yet", empty_box)
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet(
                "font-size: 10pt; font-weight: 600; color: #9e9e9e; background: transparent;"
            )
            empty_hint = QLabel(
                "Pin clips from clipboard history to assign them to categories.",
                empty_box,
            )
            empty_hint.setAlignment(Qt.AlignCenter)
            empty_hint.setStyleSheet(
                "font-size: 8.5pt; color: #737373; background: transparent; margin-top: 4px;"
            )
            empty_layout.addWidget(empty_label)
            empty_layout.addWidget(empty_hint)
            self.items_layout.addWidget(empty_box)
            self.items_layout.addStretch(1)
            return

        for item in pinned:
            row = QFrame(self.items_host)
            row.setObjectName("pinnedRow")
            row.setStyleSheet(
                """
                QFrame#pinnedRow {
                    background-color: #2e2e2e;
                    border: 1px solid #383838;
                    border-radius: 6px;
                }
                QFrame#pinnedRow:hover {
                    background-color: #363636;
                    border-color: #484848;
                }
                """
            )
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 5, 8, 5)
            row_layout.setSpacing(8)

            if item.get("type") == "image":
                preview_text = f"🖼  Image · {item.get('width', '?')}×{item.get('height', '?')}"
            else:
                preview_text = " ".join(str(item.get("text", "")).split())
                if len(preview_text) > 60:
                    preview_text = preview_text[:59].rstrip() + "…"

            preview = QLabel(preview_text, row)
            preview.setToolTip(str(item.get("text", "")) if item.get("type") == "text" else "")
            preview.setStyleSheet(
                "color: #ececec; font-size: 9pt; background: transparent; border: none;"
            )
            preview.setMinimumWidth(0)
            preview.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            row_layout.addWidget(preview, 1)

            combo = SettingsComboBox(row)
            combo.setCursor(Qt.PointingHandCursor)
            combo.setMinimumWidth(115)
            combo.setMaximumWidth(150)
            combo.addItem("Uncategorized", None)
            for name in self.store.categories:
                combo.addItem(name, name)
            target = item.get("category")
            index = combo.findData(target)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.currentIndexChanged.connect(
                lambda _index, item_id=item["id"], box=combo: self._assign_category(
                    item_id, box.currentData()
                )
            )
            row_layout.addWidget(combo, 0)
            self.items_layout.addWidget(row)
        self.items_layout.addStretch(1)

    def _assign_category(self, item_id: str, category: Optional[str]) -> None:
        if self.store.set_category(item_id, category) is not None:
            self.changed = True
            self._reload_category_counts()

    def _reload_category_counts(self) -> None:
        selected = self._selected_category()
        self.category_list.blockSignals(True)
        for row, name in enumerate(self.store.categories):
            count = sum(
                1
                for item in self.store.items
                if item.get("pinned") and item.get("category") == name
            )
            label = f"{name}    {count} pinned" if count else name
            entry = self.category_list.item(row)
            if entry is not None:
                entry.setText(label)
                entry.setData(Qt.UserRole, name)
                entry.setData(Qt.UserRole + 1, count)
        if selected in self.store.categories:
            self.category_list.setCurrentRow(self.store.categories.index(selected))
        self.category_list.blockSignals(False)
        self._sync_buttons()


# ---------------------------------------------------------------------------
# Snip tool and notification widgets


class ToastNotification(QWidget):
    """Sleek 500ms Windows 11 style notification toast."""

    def __init__(
        self,
        message: str = "Copied to clipboard",
        duration_ms: int = 500,
        pos: Optional[QPoint] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.ToolTip
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 10)
        layout.setSpacing(8)

        self.label = QLabel(message, self)
        label_font = QFont("Segoe UI", 10)
        label_font.setWeight(QFont.DemiBold)
        self.label.setFont(label_font)
        self.label.setStyleSheet("color: #f3f3f3; background: transparent;")
        layout.addWidget(self.label)

        self.adjustSize()
        self._position_toast(pos)

        QTimer.singleShot(duration_ms, self.close)

    def _position_toast(self, pos: Optional[QPoint] = None) -> None:
        cursor = pos if pos is not None else QCursor.pos()
        screen = None
        for s in QGuiApplication.screens():
            if s.geometry().contains(cursor):
                screen = s
                break
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return

        avail = screen.availableGeometry()
        margin = 12

        # Center horizontally over cursor, clamped to available screen
        x = cursor.x() - self.width() // 2
        x = max(avail.left() + margin, min(x, avail.right() - margin - self.width() + 1))

        # Position vertically: below cursor if space permits, otherwise above
        gap = 18
        if cursor.y() + gap + self.height() <= avail.bottom() - margin:
            y = cursor.y() + gap
        else:
            y = cursor.y() - gap - self.height()

        y = max(avail.top() + margin, min(y, avail.bottom() - margin - self.height() + 1))
        self.move(int(x), int(y))

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setBrush(QColor("#242424"))
        painter.setPen(QPen(QColor("#3d3d3d"), 1))
        painter.drawRoundedRect(rect, 8, 8)


class SnipOverlay(QWidget):
    """Interactive fullscreen screenshot capture overlay with window detection and region drag."""

    def __init__(
        self,
        pixmap: QPixmap,
        total_geom: QRect,
        windows: List[Dict[str, Any]],
        detection_failed: bool = False,
        on_captured: Optional[Callable[[QImage], None]] = None,
        on_closed: Optional[Callable[[], None]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._pixmap = pixmap
        self._windows = windows
        self._detection_failed = detection_failed
        self._on_captured = on_captured
        self._on_closed = on_closed

        self._press_pos: Optional[QPoint] = None
        self._current_pos: Optional[QPoint] = None
        self._is_dragging = False
        self._hovered_rect: Optional[QRect] = None

        self.setWindowFlags(
            Qt.WindowStaysOnTopHint
            | Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setGeometry(total_geom)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._on_closed:
            self._on_closed()
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.RightButton:
            self.close()
            return
        if event.button() == Qt.LeftButton:
            self._press_pos = event.pos()
            self._current_pos = event.pos()
            self._is_dragging = False

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press_pos is not None:
            pos = event.pos()
            if not self._is_dragging:
                diff = pos - self._press_pos
                if abs(diff.x()) > 5 or abs(diff.y()) > 5:
                    self._is_dragging = True
            if self._is_dragging:
                self._current_pos = pos
                self.update()
        else:
            self._update_hover(event.pos())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self._press_pos is None:
            return

        is_drag = self._is_dragging
        drag_rect = self._current_selection_rect() if is_drag else QRect()
        self._press_pos = None
        self._is_dragging = False

        if is_drag:
            if drag_rect.width() > 1 and drag_rect.height() > 1:
                self._finish_capture(drag_rect)
            else:
                self.close()
        else:
            # Click (5 px or less)
            if self._detection_failed:
                self._finish_capture(self.rect())
                return

            desktop_pt = event.pos() + self.geometry().topLeft()
            matched = None
            for w in self._windows:
                if w["rect"].contains(desktop_pt):
                    matched = w
                    break

            if matched is not None:
                rect = (
                    matched["rect"]
                    .translated(-self.geometry().topLeft())
                    .intersected(self.rect())
                )
                if not rect.isEmpty() and rect.width() > 1 and rect.height() > 1:
                    self._finish_capture(rect)
                else:
                    self.close()
            else:
                # Landed on desktop or no window found
                self.close()

    def _update_hover(self, pos: QPoint) -> None:
        if self._detection_failed:
            if self._hovered_rect is not None:
                self._hovered_rect = None
                self.update()
            return

        desktop_pt = pos + self.geometry().topLeft()
        found_rect = None
        for win in self._windows:
            if win["rect"].contains(desktop_pt):
                found_rect = (
                    win["rect"]
                    .translated(-self.geometry().topLeft())
                    .intersected(self.rect())
                )
                break

        if self._hovered_rect != found_rect:
            self._hovered_rect = found_rect
            self.update()

    def _current_selection_rect(self) -> QRect:
        if self._press_pos is None or self._current_pos is None:
            return QRect()
        x1 = min(self._press_pos.x(), self._current_pos.x())
        y1 = min(self._press_pos.y(), self._current_pos.y())
        x2 = max(self._press_pos.x(), self._current_pos.x())
        y2 = max(self._press_pos.y(), self._current_pos.y())
        rect = QRect(x1, y1, max(0, x2 - x1), max(0, y2 - y1))
        return rect.intersected(self.rect())

    def _finish_capture(self, rect: QRect) -> None:
        clamped = rect.intersected(self.rect())
        if clamped.isEmpty() or clamped.width() <= 0 or clamped.height() <= 0:
            self.close()
            return

        scale_x = self._pixmap.width() / self.width() if self.width() > 0 else 1.0
        scale_y = self._pixmap.height() / self.height() if self.height() > 0 else 1.0

        crop_rect = QRect(
            int(clamped.x() * scale_x),
            int(clamped.y() * scale_y),
            int(clamped.width() * scale_x),
            int(clamped.height() * scale_y),
        )
        cropped_pixmap = self._pixmap.copy(crop_rect)
        cropped_image = cropped_pixmap.toImage()

        callback = self._on_captured
        self.close()
        if callback is not None:
            callback(cropped_image)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 1. Base full screen capture
        painter.drawPixmap(0, 0, self._pixmap)

        # 2. Determine active undimmed area
        active_rect = None
        if self._is_dragging:
            active_rect = self._current_selection_rect()
        elif self._hovered_rect is not None:
            active_rect = self._hovered_rect

        # 3. 40% dimming with cutout
        dim_path = QPainterPath()
        dim_path.setFillRule(Qt.OddEvenFill)
        dim_path.addRect(QRectF(self.rect()))
        if active_rect is not None and not active_rect.isEmpty():
            dim_path.addRect(QRectF(active_rect))
        painter.fillPath(dim_path, QColor(0, 0, 0, 102))

        # 4. Highlight border and live size badge
        if active_rect is not None and not active_rect.isEmpty():
            painter.setPen(QPen(QColor("#0078d4"), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(QRectF(active_rect).adjusted(0.5, 0.5, -0.5, -0.5))

            if self._is_dragging:
                self._draw_size_label(painter, active_rect)

    def _draw_size_label(self, painter: QPainter, rect: QRect) -> None:
        if rect.width() <= 1 or rect.height() <= 1:
            return
        text = f"{rect.width()} × {rect.height()}"
        label_font = QFont("Segoe UI", 9)
        label_font.setWeight(QFont.DemiBold)
        fm = QFontMetrics(label_font)
        text_w = fm.horizontalAdvance(text)
        text_h = fm.height()
        pad_h, pad_v = 10, 4
        pill_w = text_w + pad_h * 2
        pill_h = text_h + pad_v * 2

        pill_x = rect.x() + (rect.width() - pill_w) // 2
        pill_y = rect.bottom() + 8
        if pill_y + pill_h > self.height() - 8:
            pill_y = rect.top() - pill_h - 8
        if pill_y < 8:
            pill_y = rect.top() + 8
        if pill_x < 8:
            pill_x = 8
        elif pill_x + pill_w > self.width() - 8:
            pill_x = self.width() - pill_w - 8

        pill_rect = QRectF(pill_x, pill_y, pill_w, pill_h)
        painter.setPen(QPen(QColor("#3d3d3d"), 1))
        painter.setBrush(QColor(24, 24, 24, 230))
        painter.drawRoundedRect(pill_rect, 5, 5)

        painter.setFont(label_font)
        painter.setPen(QColor("#f3f3f3"))
        painter.drawText(pill_rect, Qt.AlignCenter, text)


# ---------------------------------------------------------------------------
# Background daemon


class WinClipDaemon(QObject):
    """Owns the clipboard watcher, history store, socket and (lazy) popup."""

    def __init__(
        self,
        app: QApplication,
        data_directory: Path,
        socket_file: Path,
        instance_lock,
    ) -> None:
        super().__init__()
        self.app = app
        self.socket_file = Path(socket_file)
        self.instance_lock = instance_lock
        self.store = HistoryStore(data_directory)
        self.clipboard = app.clipboard()
        self.popup: Optional[ClipboardPopup] = None
        self.snip_overlay: Optional[SnipOverlay] = None
        self.toast: Optional[ToastNotification] = None
        self.target_window: Optional[int] = None
        self.target_is_terminal = False
        self._internal_fingerprint: Optional[str] = None
        self._internal_deadline = 0.0
        self._shutting_down = False
        self._clients: Dict[QLocalSocket, bytes] = {}

        self._start_server()
        self.clipboard.dataChanged.connect(self._on_clipboard_changed)

    def _start_server(self) -> None:
        try:
            self.socket_file.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(f"cannot create the socket directory: {exc}") from exc

        # We hold the instance lock here, so an existing socket file is stale.
        try:
            QLocalServer.removeServer(str(self.socket_file))
        except Exception:
            pass
        try:
            if self.socket_file.exists():
                self.socket_file.unlink()
        except OSError:
            pass

        self.server = QLocalServer(self)
        try:
            self.server.setSocketOptions(QLocalServer.UserAccessOption)
        except AttributeError:
            pass
        if not self.server.listen(str(self.socket_file)):
            raise RuntimeError(
                f"cannot listen on {self.socket_file}: {self.server.errorString()}"
            )
        self.server.newConnection.connect(self._accept_connections)

    # --------------------------------------------------------------- IPC socket

    def _accept_connections(self) -> None:
        while self.server.hasPendingConnections():
            client = self.server.nextPendingConnection()
            if client is None:
                return
            self._clients[client] = b""
            client.readyRead.connect(lambda c=client: self._read_client(c))
            client.disconnected.connect(lambda c=client: self._drop_client(c))

    def _read_client(self, client: QLocalSocket) -> None:
        try:
            chunk = bytes(client.readAll())
        except Exception:
            chunk = b""
        buffer = self._clients.get(client, b"") + chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            command = line.strip().decode("utf-8", "replace").casefold()
            if command:
                self._handle_command(client, command)
        if client in self._clients:
            self._clients[client] = buffer

    def _drop_client(self, client: QLocalSocket) -> None:
        self._clients.pop(client, None)
        client.deleteLater()

    def _reply(self, client: QLocalSocket, text: str) -> None:
        try:
            client.write((text + "\n").encode("ascii"))
            client.flush()
        except Exception:
            pass

    def _handle_command(self, client: QLocalSocket, command: str) -> None:
        if command == "show":
            self._reply(client, "ok")
            QTimer.singleShot(0, self.request_show)
        elif command == "snip":
            self._reply(client, "ok")
            QTimer.singleShot(0, self.request_snip)
        elif command == "shot":
            self._reply(client, "ok")
            QTimer.singleShot(0, self.request_shot)
        elif command == "ping":
            self._reply(client, "ok")
        elif command == "quit":
            self._reply(client, "ok")
            QTimer.singleShot(50, self.shutdown)
        else:
            self._reply(client, "error")

    # --------------------------------------------------------------- show / paste

    def request_show(self) -> None:
        try:
            if self.popup is not None and self.popup.isVisible():
                self.popup.hide()
                return

            # The order is important: capture the window before our popup is
            # shown or activated, then refocus it after the selection.
            self.target_window = active_window_id()
            self.target_is_terminal = is_terminal_class(
                window_class(self.target_window)
            )

            if self.popup is None:
                self.popup = self._create_popup()
            self.popup.show_history(self.store.items)
        except Exception as exc:
            _warn(f"could not show clipboard history: {exc}")

    def request_snip(self) -> None:
        try:
            if self.popup is not None and self.popup.isVisible():
                self.popup.hide()
                self.app.processEvents()

            if self.snip_overlay is not None and self.snip_overlay.isVisible():
                self.snip_overlay.raise_()
                self.snip_overlay.activateWindow()
                return

            pixmap, total_geom = grab_full_screen()
            windows, failed = query_visible_windows()

            self.snip_overlay = SnipOverlay(
                pixmap=pixmap,
                total_geom=total_geom,
                windows=windows,
                detection_failed=failed,
                on_captured=self._on_snip_captured,
                on_closed=self._on_snip_closed,
            )
            self.snip_overlay.show()
            self.snip_overlay.raise_()
            self.snip_overlay.activateWindow()
        except Exception as exc:
            _warn(f"could not open snip overlay: {exc}")

    def _on_snip_captured(self, image: QImage) -> None:
        try:
            save_screenshot_to_file(image)
            self.clipboard.setImage(image)
            self.toast = ToastNotification("Copied to clipboard", 500)
            self.toast.show()
        except Exception as exc:
            _warn(f"could not process captured snip: {exc}")

    def _on_snip_closed(self) -> None:
        self.snip_overlay = None

    def request_shot(self) -> None:
        """Capture full screen and save directly to disk without modifying clipboard."""
        try:
            if self.popup is not None and self.popup.isVisible():
                self.popup.hide()
                self.app.processEvents()

            pixmap, total_geom = grab_full_screen()
            if pixmap.isNull():
                return
            image = pixmap.toImage()
            path = save_screenshot_to_file(image)
            del pixmap, image
            gc.collect(0)
            if path:
                self.toast = ToastNotification("Screenshot saved", 600)
                self.toast.show()
        except Exception as exc:
            _warn(f"could not capture and save full screen: {exc}")

    def _create_popup(self) -> ClipboardPopup:
        popup = ClipboardPopup(self.store.images_dir, self.store.categories)
        popup.pasteRequested.connect(self._paste_item)
        popup.pinRequested.connect(self._toggle_pin)
        popup.deleteRequested.connect(self._delete_item)
        popup.clearRequested.connect(self._clear_unpinned)
        popup.settingsRequested.connect(self._open_settings)
        popup.reorderRequested.connect(self._reorder_pinned)
        return popup

    def _open_settings(self) -> None:
        try:
            if self.popup is not None:
                self.popup.hide()
            dialog = CategorySettingsDialog(self.store)
            dialog.exec_()
            if self.popup is not None:
                self.popup.set_categories(self.store.categories)
                self.popup.show_history(self.store.items)
        except Exception as exc:
            _warn(f"could not open settings: {exc}")

    def _refresh_popup(self, selected_id: Optional[str] = None) -> None:
        if self.popup is not None and self.popup.isVisible():
            try:
                self.popup.refresh_history(self.store.items, selected_id)
            except Exception as exc:
                _warn(f"could not refresh popup: {exc}")

    def _make_mime_for_item(self, item: Dict[str, Any]) -> Optional[QMimeData]:
        mime = QMimeData()
        if item.get("type") == "text":
            mime.setText(str(item.get("text", "")))
            return mime
        path = self.store.image_path(item)
        if path is None:
            return None
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            return None
        mime.setImageData(image)
        return mime

    def _paste_item(self, item_id: str) -> None:
        try:
            item = self.store.get(item_id)
            if item is None:
                return
            mime = self._make_mime_for_item(item)
            if mime is None:
                _warn("an image history item could not be read; removing it")
                self.store.delete(item_id)
                self._refresh_popup()
                return

            # Guard the dataChanged signal caused by this write. The entry
            # must not be duplicated or promoted while it is being pasted.
            self._internal_fingerprint = str(item["fingerprint"])
            self._internal_deadline = time.monotonic() + 2.0
            self.clipboard.setMimeData(mime, QClipboard.Clipboard)
            mime_sel = self._make_mime_for_item(item)
            if mime_sel is not None:
                self.clipboard.setMimeData(mime_sel, QClipboard.Selection)

            if self.popup is not None:
                self.popup.hide()
            # Hide first, then give the window manager a moment to return
            # focus. refocus_and_paste also waits before sending the keys.
            self.app.processEvents()
            if self.target_window is not None:
                refocus_and_paste(self.target_window, self.target_is_terminal)
        except Exception as exc:
            _warn(f"paste failed: {exc}")

    def _reorder_pinned(self, item_id: str, new_index: int) -> None:
        try:
            if self.store.move_pinned(item_id, new_index):
                self._refresh_popup(item_id)
        except Exception as exc:
            _warn(f"could not reorder pinned item: {exc}")

    def _toggle_pin(self, item_id: str, category: Optional[str] = None) -> None:
        try:
            item = self.store.toggle_pin(item_id, category)
            self._refresh_popup(item["id"] if item is not None else None)
        except Exception as exc:
            _warn(f"could not toggle pin: {exc}")

    def _delete_item(self, item_id: str) -> None:
        try:
            self.store.delete(item_id)
            self._refresh_popup()
        except Exception as exc:
            _warn(f"could not delete item: {exc}")

    def _clear_unpinned(self) -> None:
        try:
            self.store.clear_unpinned()
            self._refresh_popup()
        except Exception as exc:
            _warn(f"could not clear history: {exc}")

    # ------------------------------------------------------------- clipboard

    def _on_clipboard_changed(self) -> None:
        try:
            mime = self.clipboard.mimeData()
            if mime is None:
                return
            if mime_is_sensitive(mime):
                return

            if self._internal_fingerprint is not None:
                now = time.monotonic()
                if now <= self._internal_deadline:
                    current = mime_fingerprint(mime)
                    if current == self._internal_fingerprint:
                        self._internal_fingerprint = None
                        return
                self._internal_fingerprint = None

            image = clipboard_image(mime)
            if image is not None:
                encoded = encode_clipboard_image(image)
                if encoded is None:
                    return
                png, width, height, fingerprint = encoded
                item = self.store.add_image(png, fingerprint, width, height)
                if item is not None:
                    self._refresh_popup()
                return

            text = mime.text()
            if not text:
                return
            item = self.store.add_text(text)
            if item is not None:
                self._refresh_popup()
        except Exception as exc:
            # A clipboard watcher should never take the daemon down.
            _warn(f"clipboard update ignored: {exc}")

    # ----------------------------------------------------------------- lifecycle

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.cleanup()
        self.app.quit()

    def cleanup(self) -> None:
        if self.popup is not None:
            try:
                self.popup.hide()
                self.popup.close()
            except Exception:
                pass
            self.popup = None
        if self.snip_overlay is not None:
            try:
                self.snip_overlay.hide()
                self.snip_overlay.close()
            except Exception:
                pass
            self.snip_overlay = None
        if self.toast is not None:
            try:
                self.toast.close()
            except Exception:
                pass
            self.toast = None
        try:
            self.server.close()
        except Exception:
            pass
        try:
            self.socket_file.unlink()
        except OSError:
            pass
        _x11_helper.close()


def run_daemon(
    show_initial: bool,
    data_directory: Path,
    socket_file: Path,
    instance_lock,
    snip_initial: bool = False,
    shot_initial: bool = False,
) -> int:
    """Create the Qt application and enter the event-driven daemon loop."""
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication.instance()
    if app is None:
        app = QApplication([sys.argv[0]])
    app.setApplicationName("WinClip")
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")

    daemon = WinClipDaemon(app, Path(data_directory), Path(socket_file), instance_lock)
    app.aboutToQuit.connect(daemon.cleanup)

    def handle_signal(_signum, _frame) -> None:
        app.quit()

    try:
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
    except (ValueError, OSError):
        pass

    if show_initial:
        QTimer.singleShot(0, daemon.request_show)
    elif snip_initial:
        QTimer.singleShot(0, daemon.request_snip)
    elif shot_initial:
        QTimer.singleShot(0, daemon.request_shot)
    return app.exec_()


if __name__ == "__main__":
    print("winclip_app.py is started through the winclip command", file=sys.stderr)
    raise SystemExit(1)
