#!/usr/bin/env python3
"""UI and integration-helper tests. Run with PyQt5 installed.

    QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from PyQt5.QtCore import QEvent, QMimeData, QPoint, QRect, Qt
    from PyQt5.QtGui import QColor, QImage, QMouseEvent, QPixmap
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QApplication, QComboBox, QLabel, QLineEdit
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PyQt5 not available: {exc}")

from winclip_app import (  # noqa: E402
    CategorySettingsDialog,
    ClipboardPopup,
    MAX_PANEL_HEIGHT,
    MAX_SCROLL_HEIGHT,
    PANEL_WIDTH,
    SnipOverlay,
    ToastNotification,
    encode_clipboard_image,
    is_terminal_class,
    mime_is_sensitive,
    query_visible_windows,
    save_screenshot_to_file,
)
from winclip_store import HistoryStore  # noqa: E402


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.store = HistoryStore(self.data)
        self.popup = None

    def tearDown(self) -> None:
        if self.popup is not None:
            self.popup.hide()
            self.popup.deleteLater()
            self.app.processEvents()
        self.temp.cleanup()

    def _make_popup(
        self,
        texts=("first", "second", "third"),
        pin_index=None,
        pin_category=None,
    ):
        for index, text in enumerate(texts):
            item = self.store.add_text(text)
            if pin_index == index:
                self.store.toggle_pin(item["id"], pin_category)
        self.popup = ClipboardPopup(self.store.images_dir, self.store.categories)
        self.popup.show_history(self.store.items)
        self.app.processEvents()
        return self.popup

    def test_popup_size_theme_and_focus(self) -> None:
        popup = self._make_popup()
        self.assertEqual(popup.panel.width(), PANEL_WIDTH)
        self.assertLessEqual(popup.panel.height(), MAX_PANEL_HEIGHT)
        self.assertTrue(popup.isVisible())
        self.assertTrue(bool(popup.windowFlags() & Qt.WindowDoesNotAcceptFocus))
        self.assertEqual(popup.findChildren(QLineEdit), [])
        self.assertEqual(popup.category_tabs.count(), 4)
        self.assertEqual(
            [popup.category_tabs.tabText(i) for i in range(4)],
            ["All", "TG", "VK", "Insta"],
        )

    def test_three_line_preview_and_cards(self) -> None:
        popup = self._make_popup(
            ("short", "word " * 100, "third")
        )
        self.assertEqual(len(popup._cards), 3)
        longest = max(card.height() for card in popup._cards)
        self.assertGreaterEqual(longest, 65)
        self.assertLessEqual(longest, 90)

    def test_category_tabs_filter_pinned_items(self) -> None:
        telegram = self.store.add_text("telegram template")
        self.store.toggle_pin(telegram["id"], "TG")
        vk = self.store.add_text("vk template")
        self.store.toggle_pin(vk["id"], "VK")
        self.store.add_text("recent clip")

        popup = self._make_popup(())
        self.assertEqual(len(popup._cards), 3)
        self.assertIsNone(popup.current_category())

        popup.category_tabs.setCurrentIndex(1)  # TG
        self.app.processEvents()
        self.assertEqual(popup.current_category(), "TG")
        self.assertEqual(
            [card.item["text"] for card in popup._cards], ["telegram template"]
        )
        self.assertEqual(popup.selected_item_id(), popup._cards[0].item["id"])

        popup.category_tabs.setCurrentIndex(2)  # VK
        self.app.processEvents()
        self.assertEqual(
            [card.item["text"] for card in popup._cards], ["vk template"]
        )

        popup.category_tabs.setCurrentIndex(3)  # Insta, nothing pinned yet
        self.app.processEvents()
        self.assertEqual(popup._cards, [])

        popup.category_tabs.setCurrentIndex(0)  # All
        self.app.processEvents()
        self.assertEqual(len(popup._cards), 3)

        QTest.keyClick(popup, Qt.Key_Right)
        self.app.processEvents()
        self.assertEqual(popup.current_category(), "TG")

    def test_keyboard_navigation_and_actions(self) -> None:
        popup = self._make_popup(("one", "two", "three"))
        first = popup.selected_item_id()
        QTest.keyClick(popup, Qt.Key_Down)
        second = popup.selected_item_id()
        QTest.keyClick(popup, Qt.Key_Down)
        third = popup.selected_item_id()
        QTest.keyClick(popup, Qt.Key_Up)
        self.assertEqual(popup.selected_item_id(), second)
        self.assertEqual(len({first, second, third}), 3)

        pasted: list[str] = []
        pinned: list[tuple[str, object]] = []
        deleted: list[str] = []
        opened_settings: list[bool] = []
        popup.pasteRequested.connect(pasted.append)
        popup.pinRequested.connect(
            lambda item_id, category: pinned.append((item_id, category))
        )
        popup.deleteRequested.connect(deleted.append)
        popup.settingsRequested.connect(lambda: opened_settings.append(True))
        QTest.keyClick(popup, Qt.Key_Return)
        QTest.keyClick(popup, Qt.Key_P, Qt.ControlModifier)
        QTest.keyClick(popup, Qt.Key_Delete)
        QTest.keyClick(popup, Qt.Key_Comma, Qt.ControlModifier)
        self.assertEqual(pasted, [second])
        self.assertEqual(pinned, [(second, None)])
        self.assertEqual(deleted, [second])
        self.assertEqual(opened_settings, [True])

    def test_pinned_section_and_pin_buttons(self) -> None:
        popup = self._make_popup(("one", "two", "three"), pin_index=1)
        pinned_cards = [c for c in popup._cards if c.item["pinned"]]
        self.assertEqual(len(pinned_cards), 1)
        self.assertTrue(pinned_cards[0].pin_button.isVisible())
        self.assertEqual(pinned_cards[0].pin_button.toolTip(), "Unpin")

        unpinned = [c for c in popup._cards if not c.item["pinned"]][0]
        self.assertTrue(unpinned.pin_button.isHidden())
        unpinned._set_hovered(True)
        self.assertFalse(unpinned.pin_button.isHidden())
        self.assertFalse(unpinned.delete_button.isHidden())

    def test_pinned_card_in_all_view_shows_pinned_without_category(self) -> None:
        popup = self._make_popup(("templated",), pin_index=0, pin_category="Insta")
        labels = [label.text() for label in popup.content.findChildren(QLabel)]
        self.assertIn("Pinned", labels)
        self.assertNotIn("Pinned · Insta", labels)
        for cat in popup._categories:
            self.assertNotIn(f"Pinned · {cat}", labels)

    def test_all_view_shows_recent_first_then_pinned(self) -> None:
        recent_item = self.store.add_text("recent clip")
        pinned_item = self.store.add_text("pinned clip")
        self.store.toggle_pin(pinned_item["id"])
        popup = self._make_popup(())
        self.assertEqual(len(popup._cards), 2)
        # Recent first, pinned last
        self.assertEqual(popup._cards[0].item["id"], recent_item["id"])
        self.assertFalse(popup._cards[0].item["pinned"])
        self.assertEqual(popup._cards[1].item["id"], pinned_item["id"])
        self.assertTrue(popup._cards[1].item["pinned"])
        labels = [label.text() for label in popup.content.findChildren(QLabel)]
        self.assertIn("Recent", labels)
        self.assertIn("Pinned", labels)

    def test_reorder_pinned_via_keyboard(self) -> None:
        c1 = self.store.add_text("clip 1")
        c2 = self.store.add_text("clip 2")
        self.store.toggle_pin(c1["id"])
        self.store.toggle_pin(c2["id"])
        popup = self._make_popup(())
        reorders = []
        popup.reorderRequested.connect(lambda item_id, idx: reorders.append((item_id, idx)))
        # Select clip 2 (the second pinned card, at pinned index 1)
        popup._selected_id = c2["id"]
        popup._apply_selection()
        # Move up with Ctrl+Up
        QTest.keyClick(popup, Qt.Key_Up, Qt.ControlModifier)
        self.assertEqual(reorders, [(c2["id"], 0)])
        # Move down with Ctrl+Down
        popup._selected_id = c1["id"]
        popup._apply_selection()
        reorders.clear()
        QTest.keyClick(popup, Qt.Key_Down, Qt.ControlModifier)
        self.assertEqual(reorders, [(c1["id"], 1)])

    def test_pin_menu_assigns_selected_category(self) -> None:
        self.store.add_text("menu clip")
        popup = self._make_popup(())
        requests: list[tuple[str, object]] = []
        popup.pinRequested.connect(
            lambda item_id, category: requests.append((item_id, category))
        )
        item_id = popup._cards[0].item["id"]

        from unittest import mock

        from PyQt5.QtWidgets import QMenu

        def choose_first(menu, *args, **kwargs):
            return next(
                action for action in menu.actions() if action.data() == "VK"
            )

        with mock.patch.object(QMenu, "exec_", choose_first):
            popup._request_pin(item_id)
        self.assertEqual(requests, [(item_id, "VK")])

    def test_pin_menu_skipped_in_category_view(self) -> None:
        self.store.add_text("category clip")
        popup = self._make_popup(())
        popup.category_tabs.setCurrentIndex(1)  # TG
        self.app.processEvents()
        requests: list[tuple[str, object]] = []
        popup.pinRequested.connect(
            lambda item_id, category: requests.append((item_id, category))
        )
        item_id = self.store.items[0]["id"]

        from unittest import mock

        from PyQt5.QtWidgets import QMenu

        with mock.patch.object(QMenu, "exec_", side_effect=AssertionError):
            popup._request_pin(item_id)
        self.assertEqual(requests, [(item_id, "TG")])

    def test_resize_refreshes_after_pin_change(self) -> None:
        self.store.add_text("resize clip")
        popup = self._make_popup(())
        item_id = self.store.items[0]["id"]
        self.store.toggle_pin(item_id)
        popup.refresh_history(self.store.items, item_id)
        self.app.processEvents()
        self.assertEqual(
            popup.scroll.height(),
            min(popup.list_layout.sizeHint().height(), MAX_SCROLL_HEIGHT),
        )
        self.store.toggle_pin(item_id)
        popup.refresh_history(self.store.items, item_id)
        self.app.processEvents()
        self.assertEqual(
            popup.scroll.height(),
            min(popup.list_layout.sizeHint().height(), MAX_SCROLL_HEIGHT),
        )

    def test_max_height_with_many_items(self) -> None:
        popup = self._make_popup(tuple(f"item {i} " * 20 for i in range(30)))
        self.assertLessEqual(popup.panel.height(), MAX_PANEL_HEIGHT)
        self.assertEqual(len(popup._cards), 15)
        self.assertLess(popup.scroll.height(), popup.list_layout.sizeHint().height())

    def test_empty_state_and_clear_button(self) -> None:
        popup = self._make_popup(())
        self.assertEqual(len(popup._cards), 0)
        self.assertFalse(popup.clear_button.isEnabled())
        popup.category_tabs.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(len(popup._cards), 0)

    def test_image_card(self) -> None:
        image = QImage(200, 100, QImage.Format_ARGB32)
        image.fill(QColor("#3399ff"))
        encoded = encode_clipboard_image(image)
        self.assertIsNotNone(encoded)
        png, width, height, fingerprint = encoded
        self.store.add_image(png, fingerprint, width, height)
        self.popup = ClipboardPopup(self.store.images_dir, self.store.categories)
        self.popup.show_history(self.store.items)
        self.app.processEvents()
        self.assertEqual(len(self.popup._cards), 1)
        self.assertEqual(self.popup._cards[0].item["type"], "image")

    def test_escape_hides(self) -> None:
        popup = self._make_popup()
        self.assertTrue(popup.isVisible())
        QTest.keyClick(popup, Qt.Key_Escape)
        self.app.processEvents()
        self.assertFalse(popup.isVisible())

    def test_settings_dialog_lists_categories_and_pinned_items(self) -> None:
        item = self.store.add_text("pinned settings clip")
        self.store.toggle_pin(item["id"], "TG")
        dialog = CategorySettingsDialog(self.store)
        try:
            self.assertEqual(dialog.category_list.count(), 3)
            self.assertEqual(
                dialog.category_list.item(0).data(Qt.UserRole), "TG"
            )
            combos = dialog.items_host.findChildren(QComboBox)
            self.assertEqual(len(combos), 1)
            self.assertEqual(combos[0].currentData(), "TG")
            self.assertEqual(combos[0].count(), 4)
            combos[0].setCurrentIndex(combos[0].findData("Insta"))
            self.app.processEvents()
            self.assertTrue(dialog.changed)
            self.assertEqual(self.store.get(item["id"])["category"], "Insta")
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()

    def test_settings_dialog_handles_long_pinned_previews(self) -> None:
        item = self.store.add_text("very long pinned text " * 40)
        self.store.toggle_pin(item["id"], "TG")
        dialog = CategorySettingsDialog(self.store)
        try:
            dialog.show()
            self.app.processEvents()
            self.assertLessEqual(dialog.width(), 560)
            combos = dialog.items_host.findChildren(QComboBox)
            self.assertEqual(len(combos), 1)
            self.assertLessEqual(
                combos[0].geometry().right(),
                dialog.items_scroll.viewport().width(),
            )
        finally:
            dialog.close()
            dialog.deleteLater()
            self.app.processEvents()

    def test_terminal_detection(self) -> None:
        for value in (
            "gnome-terminal-server",
            "xfce4-terminal",
            "kitty",
            "Alacritty",
            "xterm",
            "Konsole",
            "Tilix",
            "terminator",
            "org.wezfurlong.wezterm",
            "xterm-256color",
        ):
            self.assertTrue(is_terminal_class(value), value)
        for value in ("firefox", "code", "org.gnome.Nautilus", ""):
            self.assertFalse(is_terminal_class(value), value)

    def test_password_hint_detection(self) -> None:
        mime = QMimeData()
        mime.setText("secret-value")
        self.assertFalse(mime_is_sensitive(mime))
        mime.setData("x-kde-passwordManagerHint", b"secret")
        self.assertTrue(mime_is_sensitive(mime))

    def test_toast_notification(self) -> None:
        pos = QPoint(300, 400)
        toast = ToastNotification("Copied to clipboard", duration_ms=50, pos=pos)
        self.assertIn("Copied to clipboard", toast.label.text())
        self.assertTrue(bool(toast.windowFlags() & Qt.ToolTip))
        self.assertTrue(bool(toast.windowFlags() & Qt.WindowStaysOnTopHint))
        self.assertAlmostEqual(toast.x() + toast.width() // 2, 300, delta=20)
        toast.show()
        self.app.processEvents()
        toast.close()
        toast.deleteLater()
        self.app.processEvents()

    def test_query_visible_windows_structure(self) -> None:
        windows, failed = query_visible_windows()
        self.assertIsInstance(windows, list)
        self.assertIsInstance(failed, bool)
        for win in windows:
            self.assertIn("id", win)
            self.assertIn("rect", win)
            self.assertIsInstance(win["rect"], QRect)

    def test_save_screenshot_to_file(self) -> None:
        img = QImage(100, 100, QImage.Format_ARGB32)
        img.fill(QColor(255, 0, 0))
        target_dir = self.data / "Screenshots"
        os.environ["WINCLIP_SCREENSHOTS_DIR"] = str(target_dir)
        try:
            path = save_screenshot_to_file(img)
            self.assertIsNotNone(path)
            self.assertTrue(path.exists())
            self.assertTrue(path.name.startswith("Screenshot_"))
            self.assertTrue(path.name.endswith(".png"))
        finally:
            os.environ.pop("WINCLIP_SCREENSHOTS_DIR", None)

    def test_snip_overlay_drag_capture(self) -> None:
        pix = QPixmap(800, 600)
        pix.fill(QColor(10, 20, 30))
        geom = QRect(0, 0, 800, 600)
        captured = []

        overlay = SnipOverlay(
            pixmap=pix,
            total_geom=geom,
            windows=[],
            detection_failed=False,
            on_captured=lambda img: captured.append(img),
        )
        overlay.show()
        self.app.processEvents()
        self.assertEqual(overlay.cursor().shape(), Qt.CrossCursor)

        # Press at (50, 50) and drag to (250, 150) -> 200x100
        QTest.mousePress(overlay, Qt.LeftButton, pos=QPoint(50, 50))
        self.app.processEvents()
        self.assertFalse(overlay._is_dragging)

        move_ev = QMouseEvent(
            QEvent.MouseMove,
            QPoint(250, 150),
            Qt.NoButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        self.app.sendEvent(overlay, move_ev)
        self.app.processEvents()
        self.assertTrue(overlay._is_dragging)
        self.assertEqual(overlay._current_selection_rect(), QRect(50, 50, 200, 100))

        QTest.mouseRelease(overlay, Qt.LeftButton, pos=QPoint(250, 150))
        self.app.processEvents()

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].width(), 200)
        self.assertEqual(captured[0].height(), 100)

    def test_snip_overlay_window_click(self) -> None:
        pix = QPixmap(800, 600)
        pix.fill(QColor(10, 20, 30))
        geom = QRect(0, 0, 800, 600)
        windows = [{"id": "0x123", "rect": QRect(100, 80, 400, 300)}]
        captured = []

        overlay = SnipOverlay(
            pixmap=pix,
            total_geom=geom,
            windows=windows,
            detection_failed=False,
            on_captured=lambda img: captured.append(img),
        )
        overlay.show()
        self.app.processEvents()

        # Click inside the window frame without dragging (diff <= 5px)
        QTest.mousePress(overlay, Qt.LeftButton, pos=QPoint(200, 200))
        QTest.mouseRelease(overlay, Qt.LeftButton, pos=QPoint(202, 201))
        self.app.processEvents()

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].width(), 400)
        self.assertEqual(captured[0].height(), 300)

    def test_snip_overlay_desktop_click_closes_without_capture(self) -> None:
        pix = QPixmap(800, 600)
        pix.fill(QColor(10, 20, 30))
        geom = QRect(0, 0, 800, 600)
        windows = [{"id": "0x123", "rect": QRect(100, 80, 400, 300)}]
        captured = []

        overlay = SnipOverlay(
            pixmap=pix,
            total_geom=geom,
            windows=windows,
            detection_failed=False,
            on_captured=lambda img: captured.append(img),
        )
        overlay.show()
        self.app.processEvents()

        # Click outside window (e.g. at 20, 20)
        QTest.mousePress(overlay, Qt.LeftButton, pos=QPoint(20, 20))
        QTest.mouseRelease(overlay, Qt.LeftButton, pos=QPoint(20, 20))
        self.app.processEvents()

        self.assertEqual(len(captured), 0)

    def test_snip_overlay_escape_cancels(self) -> None:
        pix = QPixmap(800, 600)
        geom = QRect(0, 0, 800, 600)
        captured = []

        overlay = SnipOverlay(
            pixmap=pix,
            total_geom=geom,
            windows=[],
            detection_failed=False,
            on_captured=lambda img: captured.append(img),
        )
        overlay.show()
        self.app.processEvents()

        QTest.keyClick(overlay, Qt.Key_Escape)
        self.app.processEvents()

        self.assertEqual(len(captured), 0)

    def test_snip_overlay_right_click_cancels(self) -> None:
        pix = QPixmap(800, 600)
        geom = QRect(0, 0, 800, 600)
        captured = []

        overlay = SnipOverlay(
            pixmap=pix,
            total_geom=geom,
            windows=[],
            detection_failed=False,
            on_captured=lambda img: captured.append(img),
        )
        overlay.show()
        self.app.processEvents()

        QTest.mousePress(overlay, Qt.RightButton, pos=QPoint(100, 100))
        self.app.processEvents()

        self.assertEqual(len(captured), 0)

    def test_snip_overlay_fallback_when_detection_fails(self) -> None:
        pix = QPixmap(800, 600)
        pix.fill(QColor(40, 50, 60))
        geom = QRect(0, 0, 800, 600)
        captured = []

        overlay = SnipOverlay(
            pixmap=pix,
            total_geom=geom,
            windows=[],
            detection_failed=True,
            on_captured=lambda img: captured.append(img),
        )
        overlay.show()
        self.app.processEvents()

        # When detection fails, plain click captures entire screen
        QTest.mousePress(overlay, Qt.LeftButton, pos=QPoint(50, 50))
        QTest.mouseRelease(overlay, Qt.LeftButton, pos=QPoint(50, 50))
        self.app.processEvents()

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].width(), 800)
        self.assertEqual(captured[0].height(), 600)

    def test_snip_overlay_hover_tracking(self) -> None:
        pix = QPixmap(800, 600)
        geom = QRect(0, 0, 800, 600)
        windows = [{"id": "0x123", "rect": QRect(100, 80, 400, 300)}]

        overlay = SnipOverlay(
            pixmap=pix,
            total_geom=geom,
            windows=windows,
            detection_failed=False,
        )
        overlay.show()
        self.app.processEvents()

        # Hover outside
        overlay._update_hover(QPoint(20, 20))
        self.assertIsNone(overlay._hovered_rect)

        # Hover inside window
        overlay._update_hover(QPoint(150, 150))
        self.assertEqual(overlay._hovered_rect, QRect(100, 80, 400, 300))

        overlay.close()
        self.app.processEvents()

    def test_daemon_request_shot_saves_without_clipboard(self) -> None:
        target_dir = self.data / "Screenshots"
        os.environ["WINCLIP_SCREENSHOTS_DIR"] = str(target_dir)
        try:
            # Set initial text on clipboard
            self.app.clipboard().setText("do-not-overwrite-me")
            self.app.processEvents()

            from winclip_app import WinClipDaemon

            socket_file = self.data / "test.sock"
            daemon = WinClipDaemon(self.app, self.data, socket_file, None)
            try:
                daemon.request_shot()
                self.app.processEvents()

                # Verify file was created in screenshots dir
                files = list(target_dir.glob("Screenshot_*.png"))
                self.assertGreaterEqual(len(files), 1)

                # Verify clipboard was untouched
                self.assertEqual(self.app.clipboard().text(), "do-not-overwrite-me")
            finally:
                daemon.cleanup()
                self.app.processEvents()
        finally:
            os.environ.pop("WINCLIP_SCREENSHOTS_DIR", None)

    def test_popup_shadow_cache(self) -> None:
        popup = ClipboardPopup(self.data / "images", ["Work"])
        self.assertIsNone(popup._shadow_cache)
        pix1 = popup._get_shadow_cache()
        self.assertFalse(pix1.isNull())
        self.assertIs(popup._shadow_cache, pix1)
        # Calling again without geometry change returns same cache
        pix2 = popup._get_shadow_cache()
        self.assertIs(pix1, pix2)
        popup.close()
        self.app.processEvents()

    def test_popup_closes_on_outside_click(self) -> None:
        from winclip_app import _x11_helper
        popup = self._make_popup()
        popup._focus_armed = True
        popup.move(100, 100)
        self.assertTrue(popup.isVisible())

        # Simulate button press outside popup
        with mock.patch.object(_x11_helper, "is_button_pressed", return_value=True):
            with mock.patch("PyQt5.QtGui.QCursor.pos", return_value=QPoint(800, 800)):
                popup._check_outside_click()
                self.assertFalse(popup.isVisible())

    def test_popup_retains_on_inside_click(self) -> None:
        from winclip_app import _x11_helper
        popup = self._make_popup()
        popup._focus_armed = True
        popup.move(100, 100)
        self.assertTrue(popup.isVisible())

        # Simulate button press inside panel
        panel_geom = popup.panel.geometry()
        inside_point = popup.mapToGlobal(panel_geom.center())
        with mock.patch.object(_x11_helper, "is_button_pressed", return_value=True):
            with mock.patch("PyQt5.QtGui.QCursor.pos", return_value=inside_point):
                popup._check_outside_click()
                self.assertTrue(popup.isVisible())

    def test_popup_closes_on_active_window_change(self) -> None:
        from winclip_app import _x11_helper
        popup = self._make_popup()
        popup._focus_armed = True
        popup._initial_active_window = 12345
        self.assertTrue(popup.isVisible())

        # If another window activates (e.g. 99999), popup hides
        with mock.patch.object(_x11_helper, "get_active_window", return_value=99999):
            popup._check_outside_click()
            self.assertFalse(popup.isVisible())

    def test_popup_widget_cache_reuses_cards_when_unchanged(self) -> None:
        popup = self._make_popup(("item A", "item B"))
        self.assertEqual(len(popup._cards), 2)
        initial_card0 = popup._cards[0]
        initial_card1 = popup._cards[1]

        # Call show_history with the exact same items
        popup.show_history(self.store.items)
        self.assertEqual(len(popup._cards), 2)
        # Verify cards were preserved rather than destroyed and rebuilt
        self.assertIs(popup._cards[0], initial_card0)
        self.assertIs(popup._cards[1], initial_card1)

        # Call show_history with modified items (new copy added)
        self.store.add_text("item C")
        popup.show_history(self.store.items)
        self.assertEqual(len(popup._cards), 3)
        self.assertIsNot(popup._cards[0], initial_card0)


if __name__ == "__main__":
    unittest.main()
