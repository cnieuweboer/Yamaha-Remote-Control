"""Command rejection must not masquerade as lost device status."""
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
REMOTE = runpy.run_path(str(ROOT / 'YamahaRemote.pyw'), run_name='command_error_tests')
CLS = REMOTE['YamahaRemote']


class CommandErrorTests(unittest.TestCase):
    def window(self):
        self.tooltips = []
        window = SimpleNamespace(
            connected=True, last_status={'power': 'on', 'volume': 154, 'mute': False},
            _last_status_at=123.0, action_running=False, _shutting_down=False, calibration_active=False,
            power_button=SimpleNamespace(setToolTip=self.tooltips.append), show_error=Mock(),
            poll_status=Mock(), _schedule_hotkey_drain=Mock())
        def invalidate():
            window.connected = False
            window.last_status = None
            window._last_status_at = None
        window.set_offline = Mock(side_effect=invalidate)
        window.set_error_status = Mock(side_effect=invalidate)
        window.handle_request_error = lambda error, **kwargs: CLS.handle_request_error(window, error, **kwargs)
        def run_worker(function, on_result=None, on_error=None, on_finished=None):
            try:
                result = function()
            except Exception as error:
                on_error(error)
            else:
                on_result(result)
            if on_finished:
                on_finished()
        window.run_worker = run_worker
        return window

    def test_background_command_rejection_preserves_connection_and_cached_status(self):
        window = self.window()
        before = window.last_status
        error = REMOTE['DeviceResponseError']('Amplifier rejected volume command')
        window.handle_request_error(error, background=True, command=True)
        self.assertTrue(window.connected)
        self.assertIs(window.last_status, before)
        self.assertEqual(window._last_status_at, 123.0)
        window.set_offline.assert_not_called()
        window.set_error_status.assert_not_called()
        window.show_error.assert_not_called()
        self.assertIn('rejected volume command', self.tooltips[-1])

    def test_unexpected_background_command_error_does_not_invalidate_connection(self):
        window = self.window()
        window.handle_request_error(ValueError('unexpected'), background=True, command=True)
        self.assertTrue(window.connected)
        self.assertIsNotNone(window.last_status)
        self.assertIn('ValueError', self.tooltips[-1])
        window.set_error_status.assert_not_called()
        window.show_error.assert_not_called()

    def test_failed_status_read_still_invalidates_status(self):
        window = self.window()
        window.handle_request_error(REMOTE['DeviceResponseError']('Invalid device status'), background=True)
        window.set_error_status.assert_called_once()
        self.assertFalse(window.connected)
        self.assertIsNone(window.last_status)
        window.show_error.assert_not_called()

    def test_connection_errors_still_mark_device_offline(self):
        for command in (False, True):
            with self.subTest(command=command):
                window = self.window()
                window.handle_request_error(REMOTE['YamahaConnectionError']('Device unreachable'),
                                            background=True, command=command)
                window.set_offline.assert_called_once()
                self.assertFalse(window.connected)
                self.assertIsNone(window.last_status)
                window.show_error.assert_not_called()

    def test_foreground_command_rejection_keeps_existing_feedback_and_connection(self):
        window = self.window()
        window.handle_request_error(REMOTE['DeviceResponseError']('Input unavailable'), command=True)
        window.show_error.assert_called_once_with('Input unavailable')
        self.assertTrue(window.connected)
        window.set_error_status.assert_not_called()

    def test_real_api_nonzero_response_refreshes_status_without_disabling_connection(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b'{"response_code":4}'
        api = REMOTE['YamahaApi']('127.0.0.1', REMOTE['_default_config_copy']())
        window = self.window()
        with patch.object(REMOTE['DEVICE_HTTP_OPENER'], 'open', return_value=Response()):
            self.assertTrue(CLS.run_action(window, api.volume_up, background=True))
        self.assertFalse(window.action_running)
        self.assertTrue(window.connected)
        self.assertIsNotNone(window.last_status)
        window.set_error_status.assert_not_called()
        window.show_error.assert_not_called()
        window.poll_status.assert_called_once()
        window._schedule_hotkey_drain.assert_called_once()
        self.assertIn('response code 4', self.tooltips[-1])

    def test_command_connection_failure_still_refreshes_without_automatic_retry(self):
        window = self.window()
        action = Mock(side_effect=REMOTE['YamahaConnectionError']('Disconnected'))
        self.assertTrue(CLS.run_action(window, action, background=True))
        action.assert_called_once()
        window.set_offline.assert_called_once()
        window.poll_status.assert_called_once()
        self.assertFalse(window.action_running)
        window.show_error.assert_not_called()


if __name__ == '__main__':
    unittest.main()
