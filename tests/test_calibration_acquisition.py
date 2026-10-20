"""Calibration acquisition and exclusive speaker-only amplifier mutations."""
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
REMOTE = runpy.run_path(str(ROOT / 'YamahaRemote.pyw'), run_name='acquisition_tests')
CLS = REMOTE['YamahaRemote']
GLOBALS = CLS._calibration_perform.__globals__


class CalibrationAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.window = SimpleNamespace(
            _shutting_down=False, config={'Host': 'device.local'},
            _calibration_operation=False, _native_volume_hook=SimpleNamespace(installed=True),
            calibration_server=SimpleNamespace(token='session'), _workers=set(),
            pool=SimpleNamespace(activeThreadCount=Mock(return_value=0)),
            _update_polling_state=Mock(), run_worker=Mock(), api=Mock(),
        )
        self.done = Mock()
        clock_patch = patch.object(REMOTE['time'], 'monotonic', return_value=100.0)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        timer_patch = patch.object(REMOTE['QTimer'], 'singleShot')
        self.timer = timer_patch.start()
        self.addCleanup(timer_patch.stop)
        self.platform = patch.dict(GLOBALS, {'sys': SimpleNamespace(platform='win32')})
        self.platform.start()
        self.addCleanup(self.platform.stop)

    def acquire(self):
        CLS._calibration_perform(self.window, 'acquire', {}, self.done)

    def assert_no_device_commands(self):
        self.window.run_worker.assert_not_called()
        self.assertFalse(self.window.api.mock_calls)

    def test_idle_acquisition_succeeds_without_device_commands(self):
        self.acquire()
        self.done.assert_called_once_with(True, '')
        self.assertFalse(self.window._calibration_operation)
        self.window._update_polling_state.assert_called_once_with(refresh=True)
        self.assert_no_device_commands()

    def test_pending_workers_and_active_threads_must_drain_before_acquisition(self):
        self.window._workers.add('pending')
        self.acquire()
        self.done.assert_not_called()
        self.assertTrue(self.window._calibration_operation)
        self.assertEqual(self.timer.call_args.args[0], 25)
        ready = self.timer.call_args.args[1]
        self.window._workers.clear()
        self.window.pool.activeThreadCount.return_value = 1
        ready()
        self.done.assert_not_called()
        self.window.pool.activeThreadCount.return_value = 0
        ready()
        self.done.assert_called_once_with(True, '')
        self.assertFalse(self.window._calibration_operation)
        self.assert_no_device_commands()

    def test_undrained_requests_fail_at_deadline(self):
        self.window._workers.add('pending')
        self.acquire()
        self.clock.return_value = 105.0
        self.timer.call_args.args[1]()
        self.done.assert_called_once_with(False, 'Device requests did not finish in time.')
        self.assertFalse(self.window._calibration_operation)
        self.assert_no_device_commands()

    def test_ended_session_during_drain_is_rejected(self):
        self.window._workers.add('pending')
        self.acquire()
        self.window.calibration_server.token = None
        self.timer.call_args.args[1]()
        self.done.assert_called_once_with(False, 'Calibration session ended.')
        self.assertFalse(self.window._calibration_operation)
        self.assert_no_device_commands()

    def test_missing_windows_hook_cannot_acquire(self):
        for hook in (None, SimpleNamespace(installed=False)):
            with self.subTest(hook=hook):
                self.done.reset_mock()
                self.window._native_volume_hook = hook
                self.acquire()
                self.assertFalse(self.done.call_args.args[0])
                self.assertIn('native volume hook', self.done.call_args.args[1])
                self.assertFalse(self.window._calibration_operation)
                self.assert_no_device_commands()

    def test_shutdown_and_missing_configuration_cannot_acquire(self):
        for shutting_down, host in ((True, 'device.local'), (False, '')):
            with self.subTest(shutting_down=shutting_down, host=host):
                self.done.reset_mock()
                self.window._shutting_down = shutting_down
                self.window.config['Host'] = host
                self.acquire()
                self.assertFalse(self.done.call_args.args[0])
                self.assertFalse(self.window._calibration_operation)
                self.assert_no_device_commands()

    def test_mutations_and_other_actions_are_rejected(self):
        for action, args in (('set_mute', {'value': True}), ('set_mute', {'value': False}),
                             ('set_volume', {'value': 50}), ('adjust_volume', {'value': 1}),
                             ('unknown', {})):
            with self.subTest(action=action, args=args):
                self.done.reset_mock()
                CLS._calibration_perform(self.window, action, args, self.done)
                self.done.assert_called_once_with(False, 'Unsupported calibration operation.')
                self.assertFalse(self.window._calibration_operation)
                self.assert_no_device_commands()
        self.window._update_polling_state.assert_not_called()
        self.timer.assert_not_called()


class CalibrationSpeakerTests(unittest.TestCase):
    def window(self):
        return SimpleNamespace(_shutting_down=False, config={'Host': 'device.local'},
            calibration_active=True, calibration_server=SimpleNamespace(token='owner', publish=Mock()),
            _speaker_outputs=('a', 'b'), _speaker_target=(False, True),
            _speaker_state=(True, False), _speaker_state_at=99.0,
            _calibration_operation=False, _update_polling_state=Mock(),
            _accept_speaker_state=Mock(), _update_speaker_ui=Mock(),
            handle_request_error=Mock(), api=SimpleNamespace(set_speakers=Mock(return_value=(False, False))),
            run_worker=Mock())

    def test_speaker_mutation_verifies_in_worker_and_publishes_before_result(self):
        window = self.window()
        done = Mock()
        CLS._calibration_perform(window, 'set_speakers', {'value': [False, False]}, done)
        self.assertTrue(window._calibration_operation)
        function = window.run_worker.call_args.args[0]
        callbacks = window.run_worker.call_args.kwargs
        with patch.dict(GLOBALS, time=SimpleNamespace(monotonic=lambda: 100.0)):
            result = function()
        callbacks['on_result'](result)
        window.api.set_speakers.assert_called_once_with((False, False), ('a', 'b'))
        window._accept_speaker_state.assert_called_once_with((False, False), 100.0)
        window.calibration_server.publish.assert_called_once()
        done.assert_called_once_with(True, '')
        self.assertFalse(window._calibration_operation)

    def test_invalid_or_unowned_speaker_operations_never_touch_device(self):
        for value in (None, [], [False, 1], [False, False, False]):
            window = self.window()
            done = Mock()
            CLS._calibration_perform(window, 'set_speakers', {'value': value}, done)
            self.assertFalse(done.call_args.args[0])
            window.run_worker.assert_not_called()
        for active, token, outputs in ((False, 'owner', ('a',)), (True, None, ('a',)), (True, 'owner', ())):
            window = self.window()
            window.calibration_active = active
            window.calibration_server.token = token
            window._speaker_outputs = outputs
            done = Mock()
            CLS._calibration_perform(window, 'set_speakers', {'value': [False, False]}, done)
            self.assertFalse(done.call_args.args[0])
            window.run_worker.assert_not_called()

    def test_failed_mutation_publishes_unknown_state_without_optimism(self):
        window = self.window()
        done = Mock()
        CLS._calibration_perform(window, 'set_speakers', {'value': [False, False]}, done)
        error = REMOTE['DeviceResponseError']('Not supported')
        window.run_worker.call_args.kwargs['on_error'](error)
        self.assertIsNone(window._speaker_state)
        self.assertIsNone(window._speaker_target)
        self.assertIsNone(window._speaker_state_at)
        self.assertFalse(window._calibration_operation)
        window.handle_request_error.assert_called_once_with(error, background=True, command=True)
        done.assert_called_once_with(False, 'Not supported')

    def test_snapshot_reports_confirmed_state_and_its_own_freshness(self):
        window = self.window()
        window._status_sequence = 4
        window.connected = True
        window._amplifier_snapshot = lambda: {'version': 1, 'type': 'state', 'valid_for_ms': 6000}
        with patch.dict(GLOBALS, time=SimpleNamespace(monotonic=lambda: 100.0)):
            snapshot = CLS._calibration_snapshot(window)
        self.assertEqual(snapshot['speaker_state'], [True, False])
        self.assertEqual(snapshot['speaker_outputs'], ['a', 'b'])
        self.assertEqual(snapshot['speaker_valid_for_ms'], 5000)
        window._speaker_state_at = 90.0
        with patch.dict(GLOBALS, time=SimpleNamespace(monotonic=lambda: 100.0)):
            self.assertEqual(CLS._calibration_snapshot(window)['speaker_valid_for_ms'], 0)



if __name__ == '__main__':
    unittest.main()
