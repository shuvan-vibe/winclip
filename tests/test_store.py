#!/usr/bin/env python3
"""Storage-rule tests; no display or PyQt5 required."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from winclip_store import (  # noqa: E402
    DEFAULT_CATEGORIES,
    HistoryStore,
    UNPINNED_LIMIT,
    image_fingerprint,
    text_fingerprint,
)


class HistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.store = HistoryStore(self.data)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _add(self, text: str) -> dict:
        item = self.store.add_text(text)
        self.assertIsNotNone(item)
        return item

    def test_newest_first_and_deduplicates_by_moving_to_top(self) -> None:
        self._add("alpha")
        self._add("beta")
        self._add("alpha")
        self.assertEqual([i["text"] for i in self.store.items], ["alpha", "beta"])
        self.assertEqual(len(self.store.items), 2)

    def test_empty_text_is_ignored(self) -> None:
        self.assertIsNone(self.store.add_text(""))
        self.assertEqual(self.store.items, [])

    def test_persists_across_instances(self) -> None:
        item = self._add("persisted")
        self.store.toggle_pin(item["id"])
        rebuilt = HistoryStore(self.data)
        self.assertEqual(len(rebuilt.items), 1)
        self.assertEqual(rebuilt.items[0]["text"], "persisted")
        self.assertTrue(rebuilt.items[0]["pinned"])

    def test_keeps_25_unpinned_and_never_drops_pinned(self) -> None:
        pinned = self._add("keep me")
        self.store.toggle_pin(pinned["id"])
        for index in range(UNPINNED_LIMIT + 7):
            self._add(f"item {index}")
        unpinned = [i for i in self.store.items if not i["pinned"]]
        self.assertEqual(len(unpinned), UNPINNED_LIMIT)
        self.assertEqual(self.store.get(pinned["id"]), pinned)
        self.assertEqual(unpinned[0]["text"], f"item {UNPINNED_LIMIT + 6}")

    def test_clear_all_keeps_pinned(self) -> None:
        pinned = self._add("pinned")
        self._add("temporary")
        self.store.toggle_pin(pinned["id"])
        removed = self.store.clear_unpinned()
        self.assertEqual(removed, 1)
        self.assertEqual([i["text"] for i in self.store.items], ["pinned"])
        self.assertTrue(self.store.items[0]["pinned"])

    def test_delete_removes_text(self) -> None:
        item = self._add("delete me")
        self.assertTrue(self.store.delete(item["id"]))
        self.assertFalse(self.store.delete(item["id"]))
        self.assertEqual(self.store.items, [])

    def test_image_add_dedupe_and_file_cleanup(self) -> None:
        png = b"\x89PNG\r\n\x1a\nfake-but-consistent"
        fp = image_fingerprint(png)
        first = self.store.add_image(png, fp, 100, 50)
        self.assertIsNotNone(first)
        image_file = self.data / "images" / first["image"]
        self.assertTrue(image_file.exists())
        duplicate = self.store.add_image(png, fp, 100, 50)
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(len(self.store.items), 1)
        self.store.delete(first["id"])
        self.assertFalse(image_file.exists())

    def test_image_cleared_with_unpinned_items(self) -> None:
        png = b"\x89PNG\r\n\x1a\nimage"
        item = self.store.add_image(png, image_fingerprint(png), 10, 10)
        self.assertIsNotNone(item)
        path = self.data / "images" / item["image"]
        self.store.clear_unpinned()
        self.assertFalse(path.exists())

    def test_unpin_enforces_limit(self) -> None:
        items = [self._add(f"text {i}") for i in range(UNPINNED_LIMIT)]
        self.store.toggle_pin(items[-1]["id"])  # oldest item is pinned
        for index in range(3):
            self._add(f"new {index}")
        # Unpinning can push the unpinned set over the cap; enforce again.
        self.store.toggle_pin(items[-1]["id"])
        unpinned = [i for i in self.store.items if not i["pinned"]]
        self.assertEqual(len(unpinned), UNPINNED_LIMIT)

    def test_corrupt_json_is_preserved_and_does_not_crash(self) -> None:
        self.data.joinpath("history.json").write_text("{not valid json", encoding="utf-8")
        store = HistoryStore(self.data)
        self.assertEqual(store.items, [])
        backups = list(self.data.glob("history.corrupt-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertIn("not valid", backups[0].read_text(encoding="utf-8"))

    def test_missing_image_files_are_dropped(self) -> None:
        png = b"\x89PNG\r\n\x1a\nimage"
        item = self.store.add_image(png, image_fingerprint(png), 10, 10)
        self.assertIsNotNone(item)
        (self.data / "images" / item["image"]).unlink()
        rebuilt = HistoryStore(self.data)
        self.assertEqual(rebuilt.items, [])

    def test_json_has_expected_shape(self) -> None:
        self._add("shape")
        payload = json.loads((self.data / "history.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["items"][0]["type"], "text")
        self.assertIn("category", payload["items"][0])

    def test_default_categories_are_created_and_persisted(self) -> None:
        self.assertEqual(self.store.categories, list(DEFAULT_CATEGORIES))
        settings = json.loads(
            (self.data / "settings.json").read_text(encoding="utf-8")
        )
        self.assertEqual(settings["categories"], list(DEFAULT_CATEGORIES))
        rebuilt = HistoryStore(self.data)
        self.assertEqual(rebuilt.categories, list(DEFAULT_CATEGORIES))

    def test_pin_assigns_category_and_unpin_clears_it(self) -> None:
        item = self._add("categorized")
        self.store.toggle_pin(item["id"], "TG")
        rebuilt = HistoryStore(self.data)
        self.assertTrue(rebuilt.items[0]["pinned"])
        self.assertEqual(rebuilt.items[0]["category"], "TG")
        rebuilt.toggle_pin(item["id"])
        self.assertFalse(rebuilt.items[0]["pinned"])
        self.assertIsNone(rebuilt.items[0]["category"])

    def test_invalid_category_falls_back_to_uncategorized(self) -> None:
        item = self._add("no such category")
        self.store.toggle_pin(item["id"], "Missing")
        self.assertIsNone(self.store.items[0]["category"])

    def test_category_crud_updates_pinned_items(self) -> None:
        self.assertIsNone(self.store.add_category("tg"))  # duplicate
        self.assertEqual(self.store.add_category("News"), "News")
        item = self._add("news clip")
        self.store.toggle_pin(item["id"], "News")

        self.assertTrue(self.store.rename_category("News", "Updates"))
        self.assertEqual(self.store.get(item["id"])["category"], "Updates")
        rebuilt = HistoryStore(self.data)
        self.assertIn("Updates", rebuilt.categories)
        self.assertEqual(rebuilt.get(item["id"])["category"], "Updates")

        self.assertTrue(rebuilt.remove_category("Updates"))
        self.assertTrue(rebuilt.get(item["id"])["pinned"])
        self.assertIsNone(rebuilt.get(item["id"])["category"])
        self.assertNotIn("Updates", HistoryStore(self.data).categories)

    def test_set_category_requires_a_pinned_item(self) -> None:
        item = self._add("not pinned")
        self.assertIsNone(self.store.set_category(item["id"], "TG"))
        self.store.toggle_pin(item["id"], "TG")
        self.assertIsNotNone(self.store.set_category(item["id"], "VK"))
        self.assertEqual(self.store.items[0]["category"], "VK")
        self.assertIsNone(self.store.set_category(item["id"], "Missing"))

    def test_fingerprints_are_type_safe(self) -> None:
        self.assertNotEqual(text_fingerprint("same"), image_fingerprint(b"same"))

    def test_move_pinned_reordering(self) -> None:
        a = self._add("item A")
        b = self._add("item B")
        c = self._add("item C")
        self.store.toggle_pin(a["id"])  # pin_order 0
        self.store.toggle_pin(b["id"])  # pin_order 1
        self.store.toggle_pin(c["id"])  # pin_order 2
        self.assertEqual(
            [item["text"] for item in self.store.pinned_items()],
            ["item A", "item B", "item C"],
        )
        # Move C from index 2 to index 0
        self.assertTrue(self.store.move_pinned(c["id"], 0))
        self.assertEqual(
            [item["text"] for item in self.store.pinned_items()],
            ["item C", "item A", "item B"],
        )
        # Verify persistence across store reloads
        reloaded = HistoryStore(self.data)
        self.assertEqual(
            [item["text"] for item in reloaded.pinned_items()],
            ["item C", "item A", "item B"],
        )
        # Move C to index 1 (between A and B)
        self.assertTrue(reloaded.move_pinned(c["id"], 1))
        self.assertEqual(
            [item["text"] for item in reloaded.pinned_items()],
            ["item A", "item C", "item B"],
        )
        # Moving to same index returns False
        self.assertFalse(reloaded.move_pinned(c["id"], 1))
        # Unpinned items cannot be moved
        unpinned = self._add("unpinned")
        self.assertFalse(self.store.move_pinned(unpinned["id"], 0))


if __name__ == "__main__":
    unittest.main()
