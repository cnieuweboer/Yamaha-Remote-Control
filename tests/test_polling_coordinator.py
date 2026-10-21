"""Scheduling policy with real Qt timers and no device or IPC connection."""
from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock, patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

REMOTE = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'YamahaRemote.pyw'),
                        run_name='polling_coordinator_tests')


class PollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.w = QWidget()
        self.w._shutting_down = self.w._calibration_operation = False
        self.w.calibration_active = self.w.poll_running = False
        self.w._speaker_outputs = ('a', 'b')
        self.w._speaker_discovery_running = False
        self.w.action_running = False
        self.w._polling_initialized = True
        self.w._native_volume_hook = None
        self.w.amplifier_server = None
        self.w.config = dict(REMOTE['DEFAULT_CONFIG'], Host='192.168.1.2')
        self.w.poll_status = Mock()
        self.p = REMOTE['PollingCoordinator'](self.w)
        self.w.show()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.p.pause()
        self.w.close()

    def test_overlapping_refreshes_coalesce(self):
        self.p.request(20)
        self.p.request(40)
        QTest.qWait(80)
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)
        self.assertFalse(self.p.pending)

    def test_active_poll_defers_refresh_until_finished(self):
        self.w.poll_running = True
        self.p.request()
        QTest.qWait(10)
        self.w.poll_status.assert_not_called()
        self.w.poll_running = False
        self.p.finished()
        QTest.qWait(10)
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_hidden_refresh_waits_for_visibility(self):
        self.w.hide()
        self.p.request()
        self.assertTrue(self.p.pending)
        self.w.poll_status.assert_not_called()
        self.w.show()
        self.p.update()
        QTest.qWait(10)
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_pause_cancels_delayed_callbacks_and_resume_refreshes(self):
        self.p.request(20)
        self.p.pause()
        QTest.qWait(40)
        self.w.poll_status.assert_not_called()
        self.p.resume()
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)
        self.assertTrue(self.p.periodic.isActive())

    def test_refresh_defers_behind_action_and_calibration_operation(self):
        for field in ('action_running', '_calibration_operation'):
            with self.subTest(field=field):
                self.w.poll_status.reset_mock()
                setattr(self.w, field, True)
                self.p.request()
                QTest.qWait(10)
                self.w.poll_status.assert_not_called()
                setattr(self.w, field, False)
                self.p.finished()
                QTest.qWait(10)
                self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_periodic_tick_does_not_preempt_command_settling(self):
        self.p.request(100)
        self.p._periodic_due()
        self.w.poll_status.assert_not_called()
        self.p.pause()

    def test_calibration_and_background_modes_allow_hidden_polling(self):
        self.w.hide()
        self.w.config['GlobalVolumeHotkeys'] = False
        self.assertFalse(self.p.allowed())
        self.w.calibration_active = True
        self.p.update()
        self.assertEqual(self.p.periodic.interval(), 250)
        self.assertTrue(self.p.allowed())
        self.w.calibration_active = False
        self.w.config['GlobalVolumeHotkeys'] = True
        self.w._native_volume_hook = Mock(installed=True)
        self.assertTrue(self.p.allowed())
        self.w._native_volume_hook.installed = False
        self.assertFalse(self.p.allowed())
        self.w.amplifier_server = Mock(clients={'tv': object()})
        self.p.update()
        self.assertTrue(self.p.allowed())
        self.assertEqual(self.p.periodic.interval(), REMOTE['POLL_INTERVAL_MS'])

    def test_missing_config_and_shutdown_block_requests(self):
        self.w.config['Host'] = ''
        self.p.request()
        self.w.poll_status.assert_not_called()
        self.w.config['Host'] = 'device.local'
        self.w._shutting_down = True
        self.p.update(refresh=True)
        self.assertFalse(self.p.periodic.isActive())
        self.w.poll_status.assert_not_called()

    def test_periodic_tick_does_not_queue_behind_running_poll(self):
        self.w.poll_running = True
        self.p._periodic_due()
        self.assertFalse(self.p.pending)
        self.w.poll_running = False
        self.p._periodic_due()
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_initialization_gate(self):
        self.w._polling_initialized = False
        self.p.request()
        self.w.poll_status.assert_not_called()
        self.w._polling_initialized = True
        self.p.update()
        QTest.qWait(10)
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_fresh_main_status_does_not_suppress_speaker_reads(self):
        self.p.observed('main', 100)
        with patch.object(REMOTE['time'], 'monotonic', return_value=100.5):
            self.p._periodic_due()
        self.w.poll_status.assert_called_once_with(groups={'speakers'})
        self.w.poll_status.reset_mock()
        with patch.object(REMOTE['time'], 'monotonic', return_value=101):
            self.p._periodic_due()
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_pending_main_refresh_leaves_speakers_available(self):
        self.p.request(500, groups={'main'})
        self.p._periodic_due()
        self.w.poll_status.assert_called_once_with(groups={'speakers'})
        self.assertNotIn('main', self.p._observed)

    def test_failed_refresh_is_not_fresh_and_can_retry(self):
        self.p.observed('main', REMOTE['time'].monotonic())
        self.p.request(groups={'main'})
        self.w.poll_status.assert_called_once_with(groups={'main'})
        self.assertFalse(self.p.pending)
        self.w.poll_status.reset_mock()
        self.p._periodic_due()
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_independent_group_deadlines(self):
        self.p.request(10, groups={'main'})
        self.p.request(100, groups={'speakers'})
        QTest.qWait(40)
        self.w.poll_status.assert_called_once_with(groups={'main'})
        QTest.qWait(100)
        self.assertEqual(self.w.poll_status.call_count, 2)
        self.assertEqual(self.w.poll_status.call_args.kwargs['groups'], {'speakers'})

    def test_calibration_bypasses_freshness(self):
        for group in self.p.GROUPS:
            self.p.observed(group, REMOTE['time'].monotonic())
        self.w.calibration_active = True
        self.p._periodic_due()
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_update_does_not_reset_periodic_timer(self):
        self.p.update()
        QTest.qWait(30)
        remaining = self.p.periodic.remainingTime()
        self.p.update(refresh=True)
        self.assertLessEqual(self.p.periodic.remainingTime(), remaining)

    def test_speaker_refresh_waits_for_discovery(self):
        self.w._speaker_discovery_running = True
        self.p.request(groups={'speakers'})
        self.assertTrue(self.p.pending)
        self.w.poll_status.assert_not_called()
        self.w._speaker_discovery_running = False
        self.p.finished()
        QTest.qWait(10)
        self.w.poll_status.assert_called_once_with(groups={'speakers'})

    def test_unsupported_speakers_are_not_periodically_requested(self):
        self.w._speaker_outputs = ()
        self.p._periodic_due()
        self.w.poll_status.assert_called_once_with(groups={'main'})
