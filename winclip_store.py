#!/usr/bin/env python3
"""Persistent clipboard history for WinClip.

This module deliberately has no Qt dependency so the storage rules can be
tested (and reasoned about) without a display server.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

UNPINNED_LIMIT = 25
HISTORY_VERSION = 1
SETTINGS_VERSION = 1
DEFAULT_CATEGORIES = ("TG", "VK", "Insta")
MAX_CATEGORIES = 12
MAX_CATEGORY_LENGTH = 20


def text_fingerprint(text: str) -> str:
    """Return a stable, type-prefixed hash used for de-duplication."""
    return "text:" + hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def image_fingerprint(png_bytes: bytes) -> str:
    """Return a stable, type-prefixed hash for PNG image data."""
    return "image:" + hashlib.sha256(png_bytes).hexdigest()


def clean_category_name(value: Any) -> Optional[str]:
    """Normalise a user-entered category label."""
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())
    if not name:
        return None
    return name[:MAX_CATEGORY_LENGTH].strip()


class HistoryStore:
    """An ordered, persisted clipboard history.

    Newest items are first in ``items``. Pinned items stay in the same list
    but get grouped visually by the popup. There can be any number of pinned
    items; only the 25 newest unpinned items are retained.

    Categories are labels that pinned items can be assigned to. They live in
    ``settings.json`` next to the history so the popup always knows which
    buttons to show, even before it has loaded any items.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.images_dir = self.data_dir / "images"
        self.history_file = self.data_dir / "history.json"
        self.settings_file = self.data_dir / "settings.json"
        self.items: List[Dict[str, Any]] = []
        self.categories: List[str] = []
        self._ensure_dirs()
        self._load_settings()
        self._load()

    # ------------------------------------------------------------------ paths

    def _ensure_dirs(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.images_dir.mkdir(parents=True, exist_ok=True)
            # Clipboard data can contain passwords / private text.
            os.chmod(self.data_dir, 0o700)
            os.chmod(self.images_dir, 0o700)
        except OSError as exc:
            self._warn(f"Could not prepare data directory: {exc}")

    def image_path(self, item: Dict[str, Any]) -> Optional[Path]:
        """Return a safe path for an image item, or None if it is missing."""
        filename = item.get("image")
        if not isinstance(filename, str) or Path(filename).name != filename:
            return None
        path = self.images_dir / filename
        return path if path.is_file() else None

    # --------------------------------------------------------------- settings

    def _load_settings(self) -> None:
        if not self.settings_file.exists():
            self.categories = list(DEFAULT_CATEGORIES)
            self._save_settings()
            return

        try:
            with self.settings_file.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            raw_categories = data.get("categories")
            if not isinstance(raw_categories, list):
                raise ValueError("settings.json does not contain a category list")
        except (OSError, ValueError, TypeError) as exc:
            self._warn(f"Could not read settings.json ({exc}); using defaults")
            self.categories = list(DEFAULT_CATEGORIES)
            self._save_settings()
            return

        cleaned: List[str] = []
        seen = set()
        changed = False
        for raw in raw_categories:
            name = clean_category_name(raw)
            if name is None or name.casefold() in seen:
                changed = True
                continue
            seen.add(name.casefold())
            cleaned.append(name)

        if len(cleaned) > MAX_CATEGORIES:
            cleaned = cleaned[:MAX_CATEGORIES]
            changed = True
        self.categories = cleaned
        if changed:
            self._save_settings()

    def _save_settings(self) -> bool:
        payload = {"version": SETTINGS_VERSION, "categories": self.categories}
        temp = self.settings_file.with_name("settings.json.tmp")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.settings_file)
            return True
        except (OSError, TypeError, ValueError) as exc:
            self._warn(f"Could not save settings: {exc}")
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def add_category(self, name: Any) -> Optional[str]:
        cleaned = clean_category_name(name)
        if cleaned is None or len(self.categories) >= MAX_CATEGORIES:
            return None
        if any(existing.casefold() == cleaned.casefold() for existing in self.categories):
            return None
        self.categories.append(cleaned)
        self._save_settings()
        return cleaned

    def rename_category(self, old: str, new: Any) -> bool:
        if old not in self.categories:
            return False
        cleaned = clean_category_name(new)
        if cleaned is None:
            return False
        if any(
            existing.casefold() == cleaned.casefold() and existing != old
            for existing in self.categories
        ):
            return False
        if cleaned == old:
            return True
        self.categories[self.categories.index(old)] = cleaned
        for item in self.items:
            if item.get("category") == old:
                item["category"] = cleaned
        self._save_settings()
        self._save()
        return True

    def remove_category(self, name: str) -> bool:
        if name not in self.categories:
            return False
        self.categories.remove(name)
        changed_items = False
        for item in self.items:
            if item.get("category") == name:
                item["category"] = None
                changed_items = True
        self._save_settings()
        if changed_items:
            self._save()
        return True

    # --------------------------------------------------------------- loading

    def _load(self) -> None:
        changed = False
        if not self.history_file.exists():
            return

        try:
            with self.history_file.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise ValueError("history.json does not contain an item list")
        except (OSError, ValueError, TypeError) as exc:
            self._warn(f"Could not read history.json ({exc}); starting fresh")
            self._backup_corrupt_history()
            return

        cleaned: List[Dict[str, Any]] = []
        seen_ids = set()
        seen_fingerprints = set()

        for raw in data.get("items", []):
            item = self._clean_item(raw)
            if item is None:
                changed = True
                continue
            if item["id"] in seen_ids or item["fingerprint"] in seen_fingerprints:
                changed = True
                continue
            seen_ids.add(item["id"])
            seen_fingerprints.add(item["fingerprint"])
            cleaned.append(item)

        if len(cleaned) != len(data.get("items", [])):
            changed = True
        self.items = cleaned

        if self._normalise_pin_orders():
            changed = True
        removed = self._enforce_unpinned_limit()
        if removed:
            changed = True
        if changed:
            self._save()

    def _clean_item(self, raw: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        kind = raw.get("type")
        item_id = raw.get("id")
        fingerprint = raw.get("fingerprint")
        if not isinstance(item_id, str) or not item_id:
            return None
        if not isinstance(fingerprint, str) or not fingerprint:
            return None

        item: Dict[str, Any] = {
            "id": item_id,
            "type": kind,
            "pinned": raw.get("pinned") is True,
            "category": None,
            "pin_order": None,
            "created_at": raw.get("created_at")
            if isinstance(raw.get("created_at"), (int, float))
            else 0,
            "fingerprint": fingerprint,
        }

        if kind == "text":
            text = raw.get("text")
            if not isinstance(text, str) or text == "":
                return None
            item["text"] = text
        elif kind == "image":
            path = self.image_path(raw)
            if path is None:
                return None
            width = raw.get("width")
            height = raw.get("height")
            if not isinstance(width, int) or not isinstance(height, int):
                return None
            item["image"] = path.name
            item["width"] = width
            item["height"] = height
        else:
            return None

        category = raw.get("category")
        if item["pinned"] and isinstance(category, str) and category in self.categories:
            item["category"] = category
        order = raw.get("pin_order")
        if item["pinned"] and type(order) is int:
            item["pin_order"] = order
        return item

    def _backup_corrupt_history(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self.history_file.with_name(f"history.corrupt-{stamp}.json")
        try:
            os.replace(self.history_file, backup)
            self._warn(f"Corrupt history preserved at {backup}")
        except OSError as exc:
            self._warn(f"Could not preserve corrupt history: {exc}")

    # --------------------------------------------------------------- mutation

    def get(self, item_id: str) -> Optional[Dict[str, Any]]:
        for item in self.items:
            if item["id"] == item_id:
                return item
        return None

    def add_text(self, text: str) -> Optional[Dict[str, Any]]:
        if not isinstance(text, str) or text == "":
            return None
        existing = self._promote(text_fingerprint(text))
        if existing is not None:
            self._save()
            return existing

        item = {
            "id": uuid.uuid4().hex,
            "type": "text",
            "text": text,
            "pinned": False,
            "category": None,
            "pin_order": None,
            "created_at": time.time(),
            "fingerprint": text_fingerprint(text),
        }
        self.items.insert(0, item)
        self._enforce_unpinned_limit()
        self._save()
        return item

    def add_image(
        self,
        png_bytes: bytes,
        fingerprint: str,
        width: int,
        height: int,
    ) -> Optional[Dict[str, Any]]:
        if not png_bytes or not fingerprint:
            return None
        existing = self._promote(fingerprint)
        if existing is not None:
            self._save()
            return existing

        item_id = uuid.uuid4().hex
        filename = f"{item_id}.png"
        if not self._write_image(filename, png_bytes):
            return None

        item = {
            "id": item_id,
            "type": "image",
            "image": filename,
            "width": int(width),
            "height": int(height),
            "pinned": False,
            "category": None,
            "pin_order": None,
            "created_at": time.time(),
            "fingerprint": fingerprint,
        }
        self.items.insert(0, item)
        self._enforce_unpinned_limit()
        self._save()
        return item

    def toggle_pin(
        self, item_id: str, category: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        item = self.get(item_id)
        if item is None:
            return None
        if item["pinned"]:
            item["pinned"] = False
            item["category"] = None
            item["pin_order"] = None
            self._enforce_unpinned_limit()
        else:
            item["pinned"] = True
            item["category"] = (
                category
                if isinstance(category, str) and category in self.categories
                else None
            )
            orders = [
                entry["pin_order"]
                for entry in self.items
                if entry["pinned"] and type(entry.get("pin_order")) is int
            ]
            item["pin_order"] = max(orders) + 1 if orders else 0
        self._save()
        return self.get(item_id)

    def set_category(
        self, item_id: str, category: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        item = self.get(item_id)
        if item is None or not item["pinned"]:
            return None
        if category is not None and category not in self.categories:
            return None
        item["category"] = category
        self._save()
        return item

    def delete(self, item_id: str) -> bool:
        item = self.get(item_id)
        if item is None:
            return False
        self.items.remove(item)
        self._remove_image(item)
        self._save()
        return True

    def clear_unpinned(self) -> int:
        removed = [item for item in self.items if not item["pinned"]]
        if not removed:
            return 0
        self.items = [item for item in self.items if item["pinned"]]
        for item in removed:
            self._remove_image(item)
        self._save()
        return len(removed)

    # --------------------------------------------------------------- internal

    def _promote(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        for index, item in enumerate(self.items):
            if item["fingerprint"] == fingerprint:
                if index != 0:
                    self.items.insert(0, self.items.pop(index))
                return item
        return None

    def pinned_items(self) -> List[Dict[str, Any]]:
        """Return pinned items in their saved display order."""
        ordered = [item for item in self.items if item["pinned"]]
        ordered.sort(
            key=lambda item: (
                item["pin_order"]
                if type(item.get("pin_order")) is int
                else 2**31,
                self.items.index(item),
            )
        )
        return ordered

    def _normalise_pin_orders(self) -> bool:
        """Give every pinned item a unique, contiguous display position."""
        ordered = self.pinned_items()
        changed = False
        for index, item in enumerate(ordered):
            if item.get("pin_order") != index:
                item["pin_order"] = index
                changed = True
        for item in self.items:
            if not item["pinned"] and item.get("pin_order") is not None:
                item["pin_order"] = None
                changed = True
        return changed

    def move_pinned(self, item_id: str, new_index: int) -> bool:
        """Move a pinned item to ``new_index`` in the pinned list."""
        item = self.get(item_id)
        if item is None or not item["pinned"]:
            return False
        ordered = self.pinned_items()
        try:
            current = ordered.index(item)
        except ValueError:
            return False
        try:
            target = int(new_index)
        except (TypeError, ValueError):
            return False
        target = max(0, min(target, len(ordered) - 1))
        if target == current:
            return False
        ordered.pop(current)
        # ``target`` is the desired final position in the list without the
        # moved item. Inserting there is correct for both up and down moves.
        ordered.insert(max(0, min(target, len(ordered))), item)
        for index, entry in enumerate(ordered):
            entry["pin_order"] = index
        self._save()
        return True

    def _enforce_unpinned_limit(self) -> int:
        unpinned_seen = 0
        removed = 0
        kept: List[Dict[str, Any]] = []
        for item in self.items:
            if item["pinned"]:
                kept.append(item)
                continue
            unpinned_seen += 1
            if unpinned_seen <= UNPINNED_LIMIT:
                kept.append(item)
            else:
                self._remove_image(item)
                removed += 1
        self.items = kept
        return removed

    def _write_image(self, filename: str, png_bytes: bytes) -> bool:
        target = self.images_dir / filename
        temp = self.images_dir / f".{filename}.tmp"
        try:
            with temp.open("wb") as handle:
                handle.write(png_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, target)
            return True
        except OSError as exc:
            self._warn(f"Could not save clipboard image: {exc}")
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _remove_image(self, item: Dict[str, Any]) -> None:
        path = self.image_path(item)
        if path is None:
            return
        try:
            path.unlink()
        except OSError as exc:
            self._warn(f"Could not remove old clipboard image: {exc}")

    def _save(self) -> bool:
        payload = {"version": HISTORY_VERSION, "items": self.items}
        temp = self.history_file.with_name("history.json.tmp")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, self.history_file)
            return True
        except (OSError, TypeError, ValueError) as exc:
            self._warn(f"Could not save clipboard history: {exc}")
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    @staticmethod
    def _warn(message: str) -> None:
        print(f"winclip: {message}", file=sys.stderr)
