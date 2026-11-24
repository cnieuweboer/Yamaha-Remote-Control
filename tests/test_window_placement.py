"""Placement is remembered in a runtime file beside the script, never in the settings."""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from contextlib import ExitStack
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QMoveEvent
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
R = runpy.run_path(str(ROOT / 'YamahaRemote.pyw'), run_name='window_placement_tests')
GLOBALS = R['YamahaRemote'].__init__.__globals__
ABSENT = object()
VIRTUAL_SCREEN = QRect(0, 0, 1920, 1080)


def virtual_screens():
    return [SimpleNamespace(availableGeometry=lambda: VIRTUAL_SCREEN)]


class PlacementSettingsTests(unittest.TestCase):
    def document(self, settings=ABSENT):
        config = {'Host': '127.0.0.1'}
        if settings is not ABSENT:
            config['Window'] = {'RememberPlacement': settings}
        return config

    def test_it_is_a_member_of_the_window_group(self):
        self.assertIs(R['DEFAULT_CONFIG']['Window']['RememberPlacement'], True)
        self.assertIn('RememberPlacement', R['GROUP_MEMBERS']['Window'])
        self.assertIn('RememberPlacement', R['GROUP_BOOLEANS']['Window'])
        self.assertIs(R['validate_config']({}, allow_incomplete=True)['Window']['RememberPlacement'], True)
        self.assertIs(R['validate_config'](self.document(False), allow_incomplete=True)
                      ['Window']['RememberPlacement'], False)

    def test_invalid_values_are_rejected(self):
        for value in ('true', 0, 1, None, [], {}, 'Remember'):
            with self.subTest(value=value), self.assertRaisesRegex(R['ConfigError'], 'true or false'):
                R['validate_config'](self.document(value), allow_incomplete=True)

    def test_a_group_that_is_not_an_object_is_rejected(self):
        for value in ('window', [], None, 5):
            with self.subTest(value=value), self.assertRaisesRegex(R['ConfigError'],
                                                                   r'"Window" must be an object'):
                R['validate_config']({'Host': '127.0.0.1', 'Window': value}, allow_incomplete=True)

    def test_an_unknown_member_of_the_group_is_rejected(self):
        for member in ('AlwaysOnTop', 'RememberWindowPlacement', 'Font'):
            with self.subTest(member=member), self.assertRaisesRegex(R['ConfigError'],
                                                                     r'Unknown "Window" setting\(s\)'):
                R['validate_config']({'Host': '127.0.0.1', 'Window': {member: True}},
                                     allow_incomplete=True)

    def test_load_adds_the_setting_without_losing_an_explicit_value(self):
        for value in (ABSENT, False):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'YamahaRemote.json'
                path.write_text(json.dumps(self.document(value)), encoding='utf-8')
                with patch.dict(GLOBALS, CONFIG_PATH=path):
                    config = R['load_config']()
                saved = json.loads(path.read_text())
                expected = True if value is ABSENT else value
                self.assertEqual(saved['Window']['RememberPlacement'], expected)
                self.assertEqual(config['Window']['RememberPlacement'], expected)


class PlacementFileTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config_path = Path(self.folder.name) / 'YamahaRemote.json'
        self.config_path.write_text(json.dumps({'Host': '127.0.0.1'}), encoding='utf-8')
        context = patch.dict(GLOBALS, CONFIG_PATH=self.config_path)
        context.start()
        self.addCleanup(context.stop)

    def settings(self, remember=True):
        return {'Window': {'RememberPlacement': remember}}

    def state_path(self):
        return R['window_state_path']()

    def write_state(self, document):
        self.state_path().write_text(json.dumps(document), encoding='utf-8')

    def state(self):
        return json.loads(self.state_path().read_text())

    def test_the_state_file_lives_beside_the_settings_file(self):
        self.assertEqual(self.state_path(), self.config_path.with_name('YamahaRemote.window.json'))

    def test_a_saved_placement_is_read_back_without_touching_the_settings(self):
        before = self.config_path.read_bytes()
        R['save_window_position'](SimpleNamespace(_placement_position=QPoint(140, 90)))
        self.assertEqual(self.state(), {'format_version': 1,
                                        'Auto': {'X': 140, 'Y': 90}, 'Manual': {'X': None, 'Y': None}})
        self.assertEqual(R['read_window_placement'](self.settings()), (140, 90))
        # A placement write must not look like a settings change to the file watcher.
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_a_window_that_was_never_placed_saves_nothing(self):
        R['save_window_position'](SimpleNamespace(_placement_position=None))
        self.assertFalse(self.state_path().exists())

    def test_a_manual_value_survives_later_saves_and_overrides_the_recorded_placement(self):
        self.write_state({'format_version': 1, 'Auto': {'X': 140, 'Y': 90}, 'Manual': {'X': 20, 'Y': 30}})
        R['save_window_position'](SimpleNamespace(_placement_position=QPoint(400, 250)))
        self.assertEqual(self.state()['Auto'], {'X': 400, 'Y': 250})
        self.assertEqual(self.state()['Manual'], {'X': 20, 'Y': 30})
        self.assertEqual(R['read_window_placement'](self.settings()), (20, 30))

    def test_forgetting_placement_only_ignores_the_recorded_value(self):
        self.write_state({'format_version': 1, 'Auto': {'X': 140, 'Y': 90}, 'Manual': {'X': None, 'Y': None}})
        self.assertEqual(R['read_window_placement'](self.settings()), (140, 90))
        self.assertIsNone(R['read_window_placement'](self.settings(remember=False)))
        # An explicit position is a deliberate choice, not remembered placement.
        self.write_state({'format_version': 1, 'Auto': {'X': None, 'Y': None}, 'Manual': {'X': 20, 'Y': 30}})
        self.assertEqual(R['read_window_placement'](self.settings(remember=False)), (20, 30))

    def test_absent_damaged_or_unknown_state_reads_as_unset(self):
        cases = (('absent', None), ('not json', '{oops'), ('a list', '[1, 2]'),
                 ('unknown version', {'format_version': 99, 'Auto': {'X': 1, 'Y': 1}}),
                 ('no auto', {'format_version': 1}),
                 ('text coordinates', {'format_version': 1, 'Auto': {'X': '140', 'Y': '90'}}),
                 ('boolean coordinates', {'format_version': 1, 'Auto': {'X': True, 'Y': False}}),
                 ('float coordinates', {'format_version': 1, 'Auto': {'X': 1.5, 'Y': 9}}),
                 ('absurd coordinates', {'format_version': 1, 'Auto': {'X': 10 ** 9, 'Y': 9}}),
                 ('partial coordinates', {'format_version': 1, 'Auto': {'X': 10}}))
        for name, document in cases:
            with self.subTest(state=name):
                if document is None:
                    self.state_path().unlink(missing_ok=True)
                elif isinstance(document, str):
                    self.state_path().write_text(document, encoding='utf-8')
                else:
                    self.write_state(document)
                self.assertIsNone(R['read_window_placement'](self.settings()))

    def test_negative_multi_monitor_offsets_are_valid(self):
        self.write_state({'format_version': 1, 'Auto': {'X': -1920, 'Y': -240},
                          'Manual': {'X': None, 'Y': None}})
        self.assertEqual(R['read_window_placement'](self.settings()), (-1920, -240))


class PlacementScreenTests(unittest.TestCase):
    SECOND = QRect(-1920, -1080, 1920, 1080)

    def two_screens(self):
        return [SimpleNamespace(availableGeometry=lambda: VIRTUAL_SCREEN),
                SimpleNamespace(availableGeometry=lambda: self.SECOND)]

    def placed(self, x, y, screens=virtual_screens, width=300, height=700):
        with patch.object(R['QGuiApplication'], 'screens', staticmethod(screens)):
            return R['_position_within_screens'](x, y, QRect(0, 0, width, height).size())

    def test_a_reachable_position_is_left_alone(self):
        self.assertEqual(self.placed(120, 80), (120, 80))

    def test_a_position_that_fits_a_second_monitor_is_left_alone(self):
        self.assertEqual(self.placed(-1920, -700, screens=self.two_screens), (-1920, -700))

    def test_a_position_that_is_nowhere_is_brought_onto_the_primary_screen(self):
        self.assertEqual(self.placed(-1920, -240), (0, 0))

    def test_an_off_desktop_position_is_brought_onto_a_screen(self):
        for x, y in ((99999, 99999), (-99999, -99999), (99999, 10), (10, 99999)):
            with self.subTest(position=(x, y)):
                placed = self.placed(x, y)
                self.assertIsNotNone(placed)
                self.assertTrue(0 <= placed[0] <= VIRTUAL_SCREEN.width() - 300)
                self.assertTrue(0 <= placed[1] <= VIRTUAL_SCREEN.height() - 700)

    def test_no_screen_means_no_placement_is_applied(self):
        self.assertIsNone(self.placed(120, 80, screens=lambda: []))


class PlacementStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def build_window(self, remember=True):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        config = R['_default_config_copy']()
        config.update(Host='127.0.0.1')
        config['Window'].update(Mica=False, DarkMode=False)
        config['Tray'].update(Enabled=False, CloseToTray=False)
        config['Window']['RememberPlacement'] = remember
        path = Path(folder.name) / 'YamahaRemote.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        context = patch.dict(GLOBALS, CONFIG_PATH=path)
        context.start()
        self.addCleanup(context.stop)
        with ExitStack() as stack:
            for method in ('_setup_calibration', '_setup_activation', '_setup_ipc', '_setup_hotkeys',
                           '_setup_tray', 'initialize'):
                stack.enter_context(patch.object(R['YamahaRemote'], method, lambda self: None))
            window = R['YamahaRemote'](config)
        # An idle stand-in keeps the production IPC endpoint unbound while
        # _publish_hotkey_state and _shutdown still follow their normal paths.
        window.amplifier_server = SimpleNamespace(
            close=lambda: None, publish=lambda: None, focused_volume={}, clients={})
        window._fit_and_lock_window()
        def cleanup():
            window._shutting_down = True
            window._shutdown()
            window.poll_timer.stop()
            window.startup_status_timer.stop()
            window.hide()
            window.deleteLater()
            self.app.processEvents()
        self.addCleanup(cleanup)
        return window

    def store(self, auto=None, manual=None):
        R['window_state_path']().write_text(json.dumps({
            'format_version': 1,
            'Auto': auto or {'X': None, 'Y': None},
            'Manual': manual or {'X': None, 'Y': None},
        }), encoding='utf-8')

    def start(self, window, *arguments):
        with patch.object(window, 'move') as move, \
             patch.object(window, 'restoreGeometry') as restore, \
             patch.object(R['QGuiApplication'], 'screens', staticmethod(virtual_screens)), \
             patch.object(sys, 'argv', ['YamahaRemote.pyw', *arguments]):
            R['show_startup_window'](window, window.config)
        self.app.processEvents()
        return move, restore

    def test_no_stored_placement_moves_nothing(self):
        window = self.build_window()
        move, restore = self.start(window)
        move.assert_not_called()
        restore.assert_not_called()

    def test_a_recorded_position_is_applied_on_a_new_start(self):
        window = self.build_window()
        self.store(auto={'X': 130, 'Y': 120})
        move, _ = self.start(window)
        move.assert_called_once_with(130, 120)

    def test_a_manual_position_wins_over_the_recorded_one(self):
        window = self.build_window()
        self.store(auto={'X': 130, 'Y': 120}, manual={'X': 60, 'Y': 40})
        move, _ = self.start(window)
        move.assert_called_once_with(60, 40)

    def test_forgetting_placement_suppresses_only_the_recorded_position(self):
        window = self.build_window(remember=False)
        self.store(auto={'X': 130, 'Y': 120}, manual={'X': 60, 'Y': 40})
        move, _ = self.start(window)
        move.assert_called_once_with(60, 40)
        self.store(auto={'X': 130, 'Y': 120}, manual={'X': None, 'Y': None})
        move, _ = self.start(window)
        move.assert_not_called()

    def test_an_off_desktop_recorded_position_is_clamped_before_it_is_applied(self):
        window = self.build_window()
        self.store(auto={'X': 99999, 'Y': 99999})
        move, _ = self.start(window)
        applied = move.call_args.args
        self.assertEqual(len(applied), 2)
        self.assertTrue(0 <= applied[0] <= VIRTUAL_SCREEN.width() - window.width() + 1)
        self.assertTrue(0 <= applied[1] <= VIRTUAL_SCREEN.height() - window.height() + 1)

    def test_restart_arguments_take_priority_over_stored_placement(self):
        window = self.build_window()
        window.show()
        self.app.processEvents()
        geometry = bytes(window.saveGeometry()).hex()
        self.store(auto={'X': 130, 'Y': 120})
        move, restore = self.start(window, f'--restart-window-geometry={geometry}')
        move.assert_not_called()
        restore.assert_called_once()

    def test_remembered_placement_uses_frame_coordinates(self):
        window = self.build_window()
        # Simulate a title bar offset between the client origin and frame.
        with patch.object(window, 'frameGeometry', return_value=QRect(260, 210, 305, 500)):
            window.moveEvent(QMoveEvent(QPoint(268, 241), QPoint(0, 0)))
        window._shutdown()
        self.assertEqual(R["read_window_placement"](window.config), (260, 210))

    def test_a_real_move_is_recorded_and_written_once_on_shutdown(self):
        window = self.build_window()
        window.show()
        self.app.processEvents()
        window.move(260, 210)
        self.app.processEvents()
        self.assertIsNotNone(window._placement_position)
        self.assertEqual(window._placement_position, window.pos())
        window._shutdown()
        written = json.loads(R['window_state_path']().read_text())
        self.assertEqual(written['Auto'], {'X': window.pos().x(), 'Y': window.pos().y()})
        window._shutdown()
        self.assertEqual(json.loads(R['window_state_path']().read_text()), written)
