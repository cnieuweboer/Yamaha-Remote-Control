"""Volume button interaction and window geometry without devices or native hooks."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from contextlib import ExitStack
from pathlib import Path
import runpy
import unittest
import time
from unittest.mock import Mock, patch
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
REMOTE = runpy.run_path(str(ROOT / 'YamahaRemote.pyw'), run_name='volume_controls_tests')
CLS = REMOTE['YamahaRemote']


class VolumeControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def window(self, dark=True):
        config = REMOTE['_default_config_copy']()
        config.update(DarkMode=dark, Mica=False)
        with ExitStack() as stack:
            for method in ('_setup_calibration', '_setup_activation', '_setup_ipc',
                           '_setup_hotkeys', '_setup_tray', '_setup_config_watcher', 'initialize'):
                stack.enter_context(patch.object(CLS, method, lambda self: None))
            window = CLS(config)
        window.discovery.checked["Inputs"] = time.monotonic()
        window.show()
        self.app.processEvents()
        def cleanup():
            window._shutting_down = True
            window.poll_timer.stop()
            window.startup_status_timer.stop()
            window.hide()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()
        self.addCleanup(cleanup)
        return window

    def test_reference_content_width_and_volume_columns_in_both_themes(self):
        for dark in (False, True):
            window = self.window(dark)
            self.assertEqual(window.width(), 303)
            self.assertEqual(window.volume_down.text(), '−')
            self.assertEqual(window.volume_up.text(), '+')
            self.assertEqual(window.volume_value.font().pointSizeF(), 13)
            self.assertGreaterEqual(window.volume_value.font().weight(), 600)
            widths = [widget.width() for widget in
                      (window.volume_down, window.volume_value, window.volume_up)]
            self.assertLessEqual(max(widths) - min(widths), 1)
            self.assertEqual(window.volume_down.geometry().left(), window.mute_button.geometry().left())
            self.assertEqual(window.volume_up.geometry().right(), window.speaker_combo.geometry().right())
            self.assertEqual(window.volume_down.height(), 34)
            self.assertEqual(window.volume_up.height(), 34)
            window.hide()
            window.show()
            self.app.processEvents()
            self.assertEqual(window.width(), 303)

    def test_combos_match_button_height_and_window_fits_sections(self):
        for dark in (False, True):
            with self.subTest(dark=dark):
                window = self.window(dark)
                window._fit_and_lock_window()
                self.app.processEvents()
                self.assertEqual(window.input_combo.height(), 34)
                self.assertEqual(window.speaker_combo.height(), 34)
                self.assertEqual(window.input_combo.height(), window.volume_up.height())
                self.assertEqual(window.height(), window.sizeHint().height())

    def test_press_is_immediate_hold_repeats_and_release_adds_no_step(self):
        window = self.window()
        for button, direction in ((window.volume_down, -1), (window.volume_up, 1)):
            with self.subTest(direction=direction), patch.object(window, 'adjust_volume') as adjust:
                button.setEnabled(True)
                QTest.mousePress(button, Qt.LeftButton)
                adjust.assert_called_once_with(direction)
                QTest.qWait(300)
                self.assertEqual(adjust.call_count, 1)
                QTest.qWait(450)
                self.assertGreaterEqual(adjust.call_count, 3)
                self.assertTrue(all(call.args == (direction,) for call in adjust.call_args_list))
                before = adjust.call_count
                QTest.mouseRelease(button, Qt.LeftButton)
                QTest.qWait(150)
                self.assertEqual(adjust.call_count, before)

    def test_disabled_volume_controls_cannot_repeat(self):
        window = self.window()
        with patch.object(window, 'adjust_volume') as adjust:
            window.volume_up.setEnabled(False)
            QTest.mousePress(window.volume_up, Qt.LeftButton)
            QTest.qWait(650)
            QTest.mouseRelease(window.volume_up, Qt.LeftButton)
            adjust.assert_not_called()

    def test_db_display_fits_and_preserves_conversion(self):
        window = self.window()
        window.config.update(VolumeDb=True, VolumeDbAtZero=-97.0, VolumeDbStepSize=0.5)
        window.last_status = {'power': 'on', 'volume': 100, 'mute': False, 'input': 'tv'}
        window.show_cached_status()
        self.app.processEvents()
        self.assertEqual(window.volume_value.text(), '-47 dB')
        self.assertGreaterEqual(window.volume_value.width(),
                                window.volume_value.fontMetrics().horizontalAdvance('-47 dB'))


if __name__ == '__main__':
    unittest.main()
