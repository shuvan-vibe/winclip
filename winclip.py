#!/usr/bin/env python3
"""WinClip command line entry point and single-instance coordinator.

Examples:
    winclip show      Show the history (starting the daemon if necessary)
    winclip daemon    Start in the background without showing the popup
    winclip quit      Stop the background process
"""

from __future__ import annotations

import argparse
import fcntl
import os
import socket
import sys
import time
from pathlib import Path
from typing import Optional


APP_NAME = "winclip"
SOCKET_TIMEOUT = 0.75


def data_dir() -> Path:
    override = os.environ.get("WINCLIP_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "winclip"


def socket_path() -> Path:
    override = os.environ.get("WINCLIP_SOCKET_PATH")
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        candidate = Path(runtime) / "winclip.sock"
        if len(os.fsencode(candidate)) < 100:
            return candidate
    return Path("/tmp") / f"winclip-{os.getuid()}.sock"


class InstanceLock:
    """An advisory lock that guarantees only one daemon per data directory."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "instance.lock"
        self.fd: Optional[int] = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            self.fd = fd
            return True
        except OSError as exc:
            print(f"{APP_NAME}: cannot create instance lock: {exc}", file=sys.stderr)
            return False

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None


def send_message(path: Path, message: str) -> bool:
    """Send one command to a running daemon over its Unix socket.

    The blocking socket is intentional: this is a short-lived client process
    and we want the command acknowledged before the shortcut command returns.
    """
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(SOCKET_TIMEOUT)
    try:
        client.connect(str(path))
        client.sendall(message.encode("ascii", "ignore") + b"\n")
        try:
            reply = client.recv(64)
        except socket.timeout:
            reply = b""
        return reply.startswith(b"ok")
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError):
        return False
    finally:
        try:
            client.close()
        except OSError:
            pass


def _send_with_retries(path: Path, message: str, attempts: int = 30) -> bool:
    for _ in range(attempts):
        if send_message(path, message):
            return True
        time.sleep(0.05)
    return False


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="winclip",
        description="Windows 11 style clipboard history for Linux Mint Cinnamon",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="show",
        choices=("show", "snip", "shot", "daemon", "quit"),
        help="default: show (snip: screen capture overlay, shot: full-screen auto-save)",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="start in the background without showing the popup",
    )
    args = parser.parse_args(argv)
    command = "daemon" if args.daemon else args.command

    path = socket_path()

    if command == "quit":
        if send_message(path, "quit"):
            return 0
        print(f"{APP_NAME}: not running")
        return 0

    requested = (
        "show"
        if command == "show"
        else (
            "snip"
            if command == "snip"
            else ("shot" if command == "shot" else "ping")
        )
    )
    if send_message(path, requested):
        return 0

    if not (os.environ.get("DISPLAY") or os.environ.get("QT_QPA_PLATFORM")):
        print(f"{APP_NAME}: no X11 display found", file=sys.stderr)
        return 1

    # No live daemon answered. Take the instance lock. If another process is
    # in the middle of starting, it holds the lock and we retry its socket.
    lock = InstanceLock(data_dir())
    if not lock.acquire():
        if _send_with_retries(path, requested):
            return 0
        print(
            f"{APP_NAME}: another instance is running but is not responding",
            file=sys.stderr,
        )
        return 1

    try:
        from winclip_app import run_daemon

        return run_daemon(
            show_initial=(command == "show"),
            snip_initial=(command == "snip"),
            shot_initial=(command == "shot"),
            data_directory=data_dir(),
            socket_file=path,
            instance_lock=lock,
        )
    except ImportError as exc:
        print(
            f"{APP_NAME}: PyQt5 is not available ({exc}).\n"
            "Install it with: sudo apt install python3-pyqt5",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # Keep the launcher failure readable.
        print(f"{APP_NAME}: could not start: {exc}", file=sys.stderr)
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
