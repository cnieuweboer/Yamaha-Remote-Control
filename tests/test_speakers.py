"""Delayed speaker status and section layout with fake devices; no real outputs."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import runpy
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
import test_volume_controls as ui_fixture

ROOT = Path(__file__).resolve().parents[1]
REMOTE = runpy.run_path(str(ROOT / 'YamahaRemote.pyw'), run_name='speaker_tests')
GLOBALS = REMOTE['YamahaApi'].set_speakers.__globals__


class Clock:
    def __init__(self):
        self.now = 0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class SpeakerDevice:
    def __init__(self, state=(True, False), outputs=('a', 'b'), delay=2):
        self.state = state
        self.outputs = outputs
        self.delay = delay
        self.pending = None
        self.remaining = 0
        self.commands = []
        self.observed = []
        self.never_settle = False

    def get(self, path):
        if path == 'system/getFeatures':
            return {'system': {'func_list': ['speaker_' + name for name in self.outputs]}}
        if path == 'system/getFuncStatus':
            if self.pending is not None:
                if self.remaining == 0 and not self.never_settle:
                    self.state = self.pending
                    self.pending = None
                else:
                    self.remaining -= 1
            self.observed.append(self.state)
            return {f'speaker_{name}': self.state[i] for i, name in enumerate(('a', 'b')) if name in self.outputs}
        self.commands.append(path)
        state = list(self.state)
        index = 0 if 'SpeakerA' in path else 1
        state[index] = path.endswith('true')
        self.pending = tuple(state)
        self.remaining = self.delay
        return {'response_code': 0}


class SpeakerApiTests(unittest.TestCase):
    def api(self, device):
        api = REMOTE['YamahaApi']('127.0.0.1')
        api._get_json = device.get
        return api

    def test_capabilities_and_unsupported_devices(self):
        for outputs in (('a', 'b'), ('a',), ('b',), ()):
            self.assertEqual(self.api(SpeakerDevice(outputs=outputs)).get_speaker_outputs(), outputs)

    def test_invalid_capabilities_and_boolean_states_are_rejected(self):
        api = self.api(SpeakerDevice())
        for system in (None, [], {'func_list': None}):
            api._get_json = Mock(return_value={'system': system})
            with self.assertRaises(REMOTE['DeviceResponseError']):
                api.get_speaker_outputs()
        for state in ({}, {'speaker_a': 1}, {'speaker_a': 'false'}):
            api._get_json = Mock(return_value=state)
            with self.assertRaises(REMOTE['DeviceResponseError']):
                api.get_speaker_state(('a',))

    def test_switch_a_to_b_verifies_off_before_enabling_b(self):
        device = SpeakerDevice()
        clock = Clock()
        with patch.dict(GLOBALS, time=clock):
            result = self.api(device).set_speakers((False, True), ('a', 'b'))
        self.assertEqual(result, (False, True))
        self.assertEqual(device.commands, ['system/setSpeakerA?enable=false', 'system/setSpeakerB?enable=true'])
        self.assertNotIn((True, True), device.observed)
        self.assertGreater(clock.now, 0)

    def test_off_and_a_plus_b_are_supported(self):
        for start, target in (((True, True), (False, False)), ((False, False), (True, True))):
            device = SpeakerDevice(state=start)
            with patch.dict(GLOBALS, time=Clock()):
                self.assertEqual(self.api(device).set_speakers(target, ('a', 'b')), target)

    def test_a_only_never_calls_b_and_rejects_b_combinations(self):
        device = SpeakerDevice(outputs=('a',))
        api = self.api(device)
        with patch.dict(GLOBALS, time=Clock()):
            self.assertEqual(api.set_speakers((False, False), ('a',)), (False, False))
        self.assertTrue(all('SpeakerB' not in command for command in device.commands))
        for target in ((False, True), (True, True), (True, 1), None):
            with self.assertRaises(REMOTE['DeviceResponseError']):
                api.set_speakers(target, ('a',))

    def test_timeout_never_enables_new_output_without_confirming_off(self):
        device = SpeakerDevice()
        device.never_settle = True
        with patch.dict(GLOBALS, time=Clock()):
            with self.assertRaises(REMOTE['DeviceResponseError']):
                self.api(device).set_speakers((False, True), ('a', 'b'))
        self.assertEqual(device.commands, ['system/setSpeakerA?enable=false'])


class SpeakerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ui_fixture.VolumeControlTests.setUpClass()

    def setUp(self):
        self.fixture = ui_fixture.VolumeControlTests()
        self.window = self.fixture.window()
        self.addCleanup(self.fixture.doCleanups)
        self.window.connected = True
        self.window.last_status = {'power': 'on', 'input': 'tv', 'volume': 20}
        self.globals = ui_fixture.REMOTE['YamahaRemote'].load_speaker_outputs.__globals__

    def discover(self, outputs=('a', 'b')):
        def worker(function, **callbacks):
            callbacks['on_result'](function())
            callbacks['on_finished']()
        with patch.object(self.window, 'run_worker', side_effect=worker), \
                patch.object(self.window.api, 'get_speaker_outputs', return_value=outputs), \
                patch.object(self.window.api, 'get_speaker_state', return_value=(True, False)):
            self.window.load_speaker_outputs()
        self.window.update_control_states()

    def index(self, state):
        return next(i for i in range(self.window.speaker_combo.count())
                    if self.window.speaker_combo.itemData(i) == state)

    def test_discovery_displays_first_get_without_waiting_for_poll_or_grace(self):
        with patch.object(self.window, 'poll_status') as poll:
            self.discover()
        self.assertEqual(self.window.speaker_combo.currentData(), (True, False))
        self.assertIsNotNone(self.window._speaker_state_at)
        self.assertIsNone(self.window._speaker_target)
        self.assertIsNone(self.window._speaker_grace_deadline)
        poll.assert_not_called()

    def test_discovery_keeps_capabilities_when_initial_state_read_fails(self):
        error = ui_fixture.REMOTE['DeviceResponseError']("Optional speaker status unavailable")
        def worker(function, **callbacks):
            callbacks['on_result'](function())
            callbacks['on_finished']()
        with patch.object(self.window, 'run_worker', side_effect=worker), \
                patch.object(self.window.api, 'get_speaker_outputs', return_value=('a', 'b')), \
                patch.object(self.window.api, 'get_speaker_state', side_effect=error):
            self.window.load_speaker_outputs()
        self.assertEqual(self.window._speaker_outputs, ('a', 'b'))
        self.assertEqual(self.window.speaker_combo.count(), 4)
        self.assertIsNone(self.window._speaker_state)
        self.assertFalse(self.window.speaker_combo.isEnabled())
        self.window._accept_speaker_state((True, False))
        self.window.update_control_states()
        self.assertTrue(self.window.speaker_combo.isEnabled())

    def test_supported_combinations_and_unknown_unsupported_states(self):
        for outputs, states in ((('a', 'b'), [(False, False), (True, False), (False, True), (True, True)]),
                                (('a',), [(False, False), (True, False)]),
                                ((), [])):
            self.window._speaker_outputs = None
            self.discover(outputs)
            combo = self.window.speaker_combo
            self.assertEqual([combo.itemData(i) for i in range(combo.count())], states)
            self.assertEqual(combo.isEnabled(), bool(outputs))

    def test_optimistic_selection_ignores_lagging_get_then_reconciles(self):
        self.discover()
        target = (False, True)
        with patch.object(self.window, 'run_action', return_value=True) as run:
            self.window.select_speakers(self.index(target))
        self.assertEqual(self.window.speaker_combo.currentData(), target)
        self.window._accept_speaker_state((True, False))
        self.assertEqual(self.window.speaker_combo.currentData(), target)
        run.call_args.kwargs['on_result'](target)
        self.window._accept_speaker_state((True, False))
        self.assertEqual(self.window.speaker_combo.currentData(), target)
        deadline = self.window._speaker_grace_deadline
        with patch.dict(self.globals, time=SimpleNamespace(monotonic=lambda: deadline + 1)):
            self.window._accept_speaker_state((True, False))
        self.assertEqual(self.window.speaker_combo.currentData(), (True, False))
        self.assertIsNone(self.window._speaker_target)

    def test_matching_get_confirms_target_without_waiting_full_grace(self):
        self.discover()
        with patch.object(self.window, 'run_action', return_value=True):
            self.window.select_speakers(self.index((False, False)))
        self.window._accept_speaker_state((False, False))
        self.assertIsNone(self.window._speaker_target)
        self.assertEqual(self.window.speaker_combo.currentData(), (False, False))

    def test_rejected_busy_selection_reverts_and_calibration_sends_nothing(self):
        self.discover()
        with patch.object(self.window, 'run_action', return_value=False) as run:
            self.window.select_speakers(self.index((False, True)))
            self.assertEqual(self.window.speaker_combo.currentData(), (True, False))
            run.reset_mock()
            self.window.calibration_active = True
            self.window.select_speakers(self.index((False, False)))
            run.assert_not_called()

    def test_failed_command_clears_optimism_until_next_status(self):
        self.discover()
        with patch.object(self.window, 'run_action', return_value=True) as run:
            self.window.select_speakers(self.index((False, True)))
        with patch.object(self.window, 'handle_request_error'):
            run.call_args.kwargs['on_error'](ui_fixture.REMOTE['DeviceResponseError']('Rejected'))
        self.assertIsNone(self.window._speaker_target)
        self.assertEqual(self.window.speaker_combo.currentIndex(), -1)

    def test_speaker_read_error_does_not_disable_power_or_volume(self):
        self.discover()
        self.window._speaker_read_failed(ui_fixture.REMOTE['DeviceResponseError']('Unavailable'))
        self.window.update_control_states()
        self.assertFalse(self.window.speaker_combo.isEnabled())
        self.assertTrue(self.window.volume_up.isEnabled())
        self.assertTrue(self.window.power_button.isEnabled())

    def test_delayed_set_keeps_gui_responsive_and_selection_optimistic(self):
        self.discover()
        window = self.window
        window.config['Host'] = '127.0.0.1'
        entered, release = threading.Event(), threading.Event()
        target = (False, True)
        def delayed_set(target, outputs):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('Fake SET was not released')
            return target
        with patch.object(window.api, 'set_speakers', side_effect=delayed_set), \
             patch.object(window, 'poll_status'):
            try:
                window.select_speakers(self.index(target))
                self.assertTrue(entered.wait(1))
                self.assertEqual(window.speaker_combo.currentData(), target)
                gui_ran = []
                QTimer.singleShot(0, lambda: gui_ran.append(True))
                QTest.qWait(20)
                self.assertTrue(gui_ran)
                self.assertTrue(window.action_running)
            finally:
                release.set()
                deadline = time.monotonic() + 2
                while window._workers and time.monotonic() < deadline:
                    QTest.qWait(10)
            self.assertFalse(window.action_running)
            self.assertEqual(window.speaker_combo.currentData(), target)

    def test_failed_optional_speaker_get_keeps_main_status_poll_successful(self):
        self.discover()
        window = self.window
        window.initialized = True
        status = {'power': 'on', 'input': 'tv', 'volume': 50}
        def worker(function, **callbacks):
            callbacks['on_result'](function())
        with patch.object(window, '_should_poll', return_value=True), \
             patch.object(window.api, 'get_status', return_value=status), \
             patch.object(window.api, 'get_speaker_state',
                          side_effect=ui_fixture.REMOTE['DeviceResponseError']('Unavailable')), \
             patch.object(window, 'run_worker', side_effect=worker):
            window.poll_status()
        self.assertTrue(window.connected)
        self.assertEqual(window.last_status, status)
        self.assertFalse(window.speaker_combo.isEnabled())
        self.assertTrue(window.volume_up.isEnabled())
        self.assertTrue(window.power_button.isEnabled())

    def test_input_favorites_are_below_combo_and_window_grows_to_fit(self):
        window = self.window
        before = window.height()
        window.input_settings.update({
            'tv': {'Name': 'TV', 'NameOverride': '', 'Hidden': True, 'Favorite': True},
            'pc': {'Name': 'PC', 'NameOverride': '', 'Hidden': False, 'Favorite': True},
        })
        window._build_favorites_from_config()
        window._fit_and_lock_window()
        self.fixture.app.processEvents()
        button = window.favorite_buttons['tv']
        self.assertGreater(button.geometry().top(), window.input_combo.geometry().bottom())
        self.assertGreater(window.height(), before)
        self.assertEqual(window.height(), window.sizeHint().height())
        self.assertLessEqual(abs(window.mute_button.width() - window.speaker_combo.width()), 1)
        self.assertLessEqual(abs(window.mute_button.width() - button.width()), 1)


if __name__ == '__main__':
    unittest.main()
