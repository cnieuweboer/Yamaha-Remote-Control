"""Amplifier calibration observes device state; mutations belong to the TV."""
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


if __name__ == '__main__':
    unittest.main()
