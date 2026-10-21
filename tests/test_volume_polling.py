"""Bounded queued volume batches retain authoritative device freshness."""
from collections import deque
from pathlib import Path
import runpy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

REMOTE = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'YamahaRemote.pyw'), run_name='volume_tests')
CLS = REMOTE['YamahaRemote']


class VolumePollingTests(unittest.TestCase):
    def window(self, pending=('volume_up',) * 8):
        window = SimpleNamespace(
            calibration_active=False, _shutting_down=False, connected=True,
            action_running=False, _last_status_at=100.0, _volume_batch_count=0,
            _hotkey_lock=threading.Lock(), _pending_hotkeys=deque(pending),
            last_status={'input': 'tv', 'volume': 20}, poll_status=Mock(),
            _schedule_hotkey_drain=Mock(), handle_request_error=Mock(),
        )
        window._request_poll = lambda delay_ms=0, groups=None: window.poll_status()
        window.polling = SimpleNamespace(finished=Mock(), invalidate=Mock(), GROUPS={'main', 'speakers'})
        window._continue_volume_batch = lambda: CLS._continue_volume_batch(window)
        self.callbacks = []
        window.run_worker = lambda action, **callbacks: self.callbacks.append((action, callbacks))
        return window

    def complete(self, window, *, batch=True, error=None):
        CLS.run_action(window, Mock(), background=True, batch_volume=batch)
        action, callbacks = self.callbacks.pop()
        callbacks['on_error'](error) if error else callbacks['on_result'](None)

    def test_fast_burst_reads_once_per_four_commands_and_at_end(self):
        window = self.window()
        with patch.object(REMOTE['time'], 'monotonic', return_value=100.1):
            for index in range(8):
                window._pending_hotkeys.popleft()
                self.complete(window)
                if index == 3:
                    self.assertEqual(window.poll_status.call_count, 1)
                    window._volume_batch_count = 0  # successful device status
            self.assertEqual(window.poll_status.call_count, 2)
        self.assertEqual(window.last_status['volume'], 20)
        self.assertEqual(window._last_status_at, 100.0)

    def test_slow_command_forces_read_before_count_limit(self):
        window = self.window()
        with patch.object(REMOTE['time'], 'monotonic', return_value=101.1):
            self.complete(window)
        window.poll_status.assert_called_once()

    def test_continuous_input_refreshes_for_longer_than_ownership_lease(self):
        window = self.window()
        for second in range(1, 12):
            with patch.object(REMOTE['time'], 'monotonic', return_value=100.0 + second):
                self.complete(window)
            self.assertEqual(window.poll_status.call_count, second)
            window._last_status_at = 100.0 + second
            window._volume_batch_count = 0

    def test_mute_or_changed_route_ends_batch(self):
        for pending in [('mute',), (('volume_up', 'optical1'),), ()]:
            with self.subTest(pending=pending):
                window = self.window(pending)
                with patch.object(REMOTE['time'], 'monotonic', return_value=100.1):
                    self.complete(window)
                window.poll_status.assert_called_once()

    def test_failure_and_ordinary_actions_refresh_immediately(self):
        for error in [None, ValueError('rejected')]:
            window = self.window()
            self.complete(window, batch=bool(error), error=error)
            window.poll_status.assert_called_once()
            window._schedule_hotkey_drain.assert_called_once()

    def test_calibration_cannot_continue_batch(self):
        window = self.window()
        window.calibration_active = True
        self.assertFalse(window._continue_volume_batch())

    def test_expired_batch_refreshes_before_dequeue(self):
        window = self.window()
        window._volume_batch_count = 1
        window._hotkey_drain_scheduled = True
        window.poll_running = False
        window._volume_hotkeys_eligible = lambda: True
        window.adjust_volume = Mock()
        with patch.object(REMOTE['time'], 'monotonic', return_value=101.1), patch.object(REMOTE['QApplication'], 'activeModalWidget', return_value=None):
            CLS._drain_hotkeys(window)
        self.assertEqual(len(window._pending_hotkeys), 8)
        window.poll_status.assert_called_once()
        window.adjust_volume.assert_not_called()


if __name__ == '__main__':
    unittest.main()
