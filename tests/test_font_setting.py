"""Font overrides stay optional, scale the volume display, and report unavailable names."""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from contextlib import ExitStack
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
R = runpy.run_path(str(ROOT / 'YamahaRemote.pyw'), run_name='font_setting_tests')
CLS = R['YamahaRemote']


def window_with(**members):
    """A validated configuration whose window group carries the given overrides."""
    return R['validate_config']({'Window': dict(members)}, allow_incomplete=True)['Window']


class FontDefaultsTests(unittest.TestCase):
    def test_font_settings_are_part_of_the_window_group(self):
        self.assertEqual(R['DEFAULT_CONFIG']['Window']['FontName'], '')
        self.assertIs(R['DEFAULT_CONFIG']['Window']['FontSize'], None)
        self.assertEqual(R['_default_config_copy']()['Window']['FontName'], '')
        window = R['validate_config']({}, allow_incomplete=True)['Window']
        self.assertEqual((window['FontName'], window['FontSize']), ('', None))

    def test_load_adds_and_saves_the_font_settings(self):
        for name in ('Segoe UI Variable Text', ''):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'YamahaRemote.json'
                document = {'Host': '127.0.0.1', 'Window': {'DarkMode': False}}
                if name:
                    document['Window'].update({'FontName': name, 'FontSize': 11})
                path.write_text(json.dumps(document), encoding='utf-8')
                with patch.dict(R['load_config'].__globals__, CONFIG_PATH=path):
                    config = R['load_config']()
                saved = json.loads(path.read_text())
                expected = (name, 11) if name else ('', None)
                for settings in (saved['Window'], config['Window']):
                    self.assertEqual((settings['FontName'], settings['FontSize']), expected)
                self.assertEqual(set(saved), set(R['DEFAULT_CONFIG']))

    def test_surrounding_font_names_are_trimmed(self):
        self.assertEqual(window_with(FontName='  Consolas  ', FontSize=None)['FontName'], 'Consolas')


class FontValidationTests(unittest.TestCase):
    def test_invalid_font_settings_are_rejected(self):
        for value in ({'FontName': None}, {'FontName': 12}, {'FontName': []}, {'FontName': 'x' * 129},
                      {'FontName': 'first\nsecond'}, {'FontName': 'tab\there'},
                      {'FontSize': 0}, {'FontSize': -1}, {'FontSize': 25}, {'FontSize': 9.5},
                      {'FontSize': '9'}, {'FontSize': True}, {'FontSize': {}},
                      {'Name': 'Consolas', 'Size': 11}, {'Font': {'Name': 'Consolas'}}, []):
            with self.subTest(value=value), self.assertRaises(R['ConfigError']):
                R['validate_config']({'Window': value}, allow_incomplete=True)

    def test_valid_font_settings_are_accepted(self):
        for value in ({}, {'FontName': ''}, {'FontSize': None}, {'FontName': '', 'FontSize': None},
                      {'FontName': 'Consolas', 'FontSize': 6}, {'FontName': 'Segoe UI Variable Text', 'FontSize': 24},
                      {'FontName': 'Any Font Name Without Checking It'}):
            with self.subTest(value=value):
                self.assertEqual(set(window_with(**value)), set(R['GROUP_MEMBERS']['Window']))

    def test_font_settings_do_not_gate_the_required_device_settings(self):
        config = R['validate_config']({'Window': {'FontSize': 12}}, allow_incomplete=True)
        self.assertEqual(R['missing_config_settings'](config), ['Host'])
        with self.assertRaises(R['ConfigError']):
            R['validate_config']({'Window': {'FontSize': 12}})


class FontSizeTests(unittest.TestCase):
    def test_default_sizes_are_unchanged(self):
        self.assertEqual(R['_font_sizes'](None), (R['UI_FONT_PT'], R['VOLUME_FONT_PT']))
        self.assertEqual(R['_font_sizes'](None), (9, 13))
        self.assertEqual(R['_font_sizes'](9), (9, 13))

    def test_volume_display_tracks_the_requested_base_size(self):
        for size, display in ((6, 9), (7, 10), (8, 12), (9, 13), (10, 14), (11, 16),
                              (12, 17), (16, 23), (24, 35)):
            with self.subTest(size=size):
                base, volume = R['_font_sizes'](size)
                self.assertEqual((base, volume), (size, display))
                self.assertGreaterEqual(volume, base + 2)


class FontApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.original = QApplication.font()
        self.addCleanup(QApplication.setFont, self.original)

    def config_with(self, name='', size=None):
        return R['validate_config']({'Host': '127.0.0.1',
                                    'Window': {'FontName': name, 'FontSize': size}},
                                   allow_incomplete=True)

    def other_available_family(self):
        # The offscreen Qt platform exposes no font database at all, so this path
        # can only be exercised in a desktop run.
        families = [name for name in QFontDatabase.families() if name != self.original.family()]
        if not families:
            self.skipTest('No font database is available here; verify on the desktop platform')
        return sorted(families)[0]

    def test_defaults_leave_the_application_font_alone(self):
        with patch.object(R['QMessageBox'], 'warning') as warning, \
             patch.object(R['QFontDatabase'], 'hasFamily') as has_family:
            R['configure_app_font'](self.config_with())
        warning.assert_not_called()
        self.assertFalse(has_family.called)  # Both defaults never query the font database.
        self.assertEqual(QApplication.font().family(), self.original.family())
        self.assertEqual(QApplication.font().pointSizeF(), self.original.pointSizeF())

    def test_an_available_family_is_applied(self):
        family = self.other_available_family()
        R['configure_app_font'](self.config_with(name=family))
        self.assertEqual(QApplication.font().family(), family)

    def test_a_requested_size_is_applied_without_changing_the_family(self):
        R['configure_app_font'](self.config_with(size=12))
        self.assertEqual(QApplication.font().pointSizeF(), 12)
        self.assertEqual(QApplication.font().family(), self.original.family())

    def test_an_unavailable_family_is_reported_and_ignored(self):
        with patch.object(R['QMessageBox'], 'warning') as warning:
            R['configure_app_font'](self.config_with(name='No Such Font 9f', size=12))
        warning.assert_called_once()
        self.assertIn('No Such Font 9f', warning.call_args.args[2])
        self.assertEqual(QApplication.font().family(), self.original.family())
        # The requested size is an independent setting and still applies.
        self.assertEqual(QApplication.font().pointSizeF(), 12)


class FontWindowTests(unittest.TestCase):
    """A window built with a Font override uses the base and the scaled number."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def build_window(self, font):
        config = R['_default_config_copy']()
        config.update(Host='127.0.0.1')
        config['Window'].update(DarkMode=False, Mica=False,
                                FontName=font['Name'], FontSize=font['Size'])
        with ExitStack() as stack:
            for method in ('_setup_calibration', '_setup_activation', '_setup_ipc',
                           '_setup_hotkeys', '_setup_tray', '_setup_config_watcher', 'initialize'):
                stack.enter_context(patch.object(CLS, method, lambda self: None))
            window = CLS(config)
        window.show()
        self.app.processEvents()
        def cleanup():
            window._shutting_down = True
            window.poll_timer.stop()
            window.startup_status_timer.stop()
            window.hide()
            window.deleteLater()
            self.app.processEvents()
        self.addCleanup(cleanup)
        return window

    def sizes(self, window):
        return (window.volume_up.font().pointSizeF(),
                window.volume_value.font().pointSizeF(),
                window.volume_value.font().weight())

    def test_default_sizes_are_exactly_as_before(self):
        window = self.build_window({'Name': '', 'Size': None})
        base, display, weight = self.sizes(window)
        self.assertEqual((base, display), (9.0, 13.0))
        self.assertGreaterEqual(weight, 600)
        self.assertEqual(window.width(), 303)

    def test_requested_sizes_reach_the_built_controls(self):
        for size, display in ((12, 17), (7, 10)):
            with self.subTest(size=size):
                base, volume, weight = self.sizes(
                    self.build_window({'Name': '', 'Size': size}))
                self.assertEqual((base, volume), (float(size), float(display)))
                self.assertGreaterEqual(weight, 600)
