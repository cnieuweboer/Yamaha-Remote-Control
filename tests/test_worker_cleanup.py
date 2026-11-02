"""Worker signal ownership must not retain one object graph per device request."""
from contextlib import ExitStack
import gc
import json
from pathlib import Path
import runpy
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref
from shiboken6 import isValid

from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
BRAND = 'Sony' if (ROOT / 'SonyRemote.pyw').exists() else 'Yamaha'
R = runpy.run_path(str(ROOT / (BRAND + 'Remote.pyw')), run_name='worker_cleanup_tests')
CLS = R[BRAND + 'Remote']
G = CLS.run_worker.__globals__


class WorkerCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(1)
        self.w = SimpleNamespace(
            _shutting_down=False, config={'Host': '127.0.0.1', 'PreSharedKey': 'test-only'},
            _workers=set(), _command_workers=set(), _worker_generation=0,
            pool=self.pool, command_pool=self.pool,
            request_history=R['RequestHistory'](), handle_request_error=Mock())
        self.refs = []
        self.signals = []
        original = R['Worker']
        def create(function):
            worker = original(function)
            self.refs.append(weakref.ref(worker))
            self.signals.append(weakref.ref(worker.signals))
            return worker
        self.context = patch.dict(G, Worker=create)
        self.context.start()
        self.addCleanup(self.context.stop)
        self.addCleanup(self.drain)

    def drain(self):
        self.pool.waitForDone()
        for _ in range(3):
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            gc.collect()

    def assert_released(self):
        self.assertFalse(self.w._workers)
        self.assertFalse(self.w._command_workers)
        # PySide may cache invalid Python wrappers. Their native signal objects
        # must be destroyed, and executed jobs must release captured request data.
        self.assertFalse(any(ref() is not None and isValid(ref()) for ref in self.signals))
        for ref in self.refs:
            worker = ref()
            if worker is not None and not isValid(worker):
                self.assertIsNone(worker.function)

    def test_repeated_success_and_failure_batches_deliver_then_release(self):
        results, errors, finished, threads = [], [], [], []
        def job(index):
            if index % 2:
                raise ValueError(str(index))
            return index
        def result(value):
            results.append(value)
            threads.append(threading.get_ident())
        def error(exc):
            errors.append(str(exc))  # Do not retain traceback frames in the test.
            threads.append(threading.get_ident())
        for batch in range(4):
            for index in range(batch * 100, (batch + 1) * 100):
                self.assertTrue(CLS.run_worker(self.w, lambda i=index: job(i),
                    on_result=result, on_error=error, on_finished=lambda: finished.append(True)))
            self.drain()
            self.assertEqual(len(results), (batch + 1) * 50)
            self.assertEqual(len(errors), (batch + 1) * 50)
            self.assertEqual(len(finished), (batch + 1) * 100)
            self.assertEqual(set(threads), {threading.get_ident()})
            self.assert_released()

    def test_stale_generation_callbacks_are_suppressed_but_objects_are_released(self):
        callback = Mock()
        for index in range(50):
            CLS.run_worker(self.w, lambda i=index: i, on_result=callback,
                           on_error=callback, on_finished=callback)
        self.pool.waitForDone()
        self.w._worker_generation += 1
        self.drain()
        callback.assert_not_called()
        self.assert_released()

    @unittest.skipUnless(BRAND == 'Sony', 'Separate TV command pool')
    def test_serialized_command_workers_are_also_released(self):
        results = []
        for batch in range(8):
            for index in range(8):
                self.assertTrue(CLS.run_worker(self.w, lambda: 42, on_result=results.append, command=True))
            self.drain()
            self.assertEqual(len(results), (batch + 1) * 8)
            self.assert_released()

    def test_pause_releases_jobs_discarded_from_each_queue(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            config = R['_default_config_copy']()
            config.update(Host='127.0.0.1', Mica=False, SystemTray=False, CloseToTray=False)
            if BRAND == 'Sony':
                config['PreSharedKey'] = 'test-only'
            path = Path(folder) / (BRAND + 'Remote.json')
            path.write_text(json.dumps(config))
            stack.enter_context(patch.dict(G, CONFIG_PATH=path))
            for name in ('initialize', '_setup_calibration', '_setup_activation'):
                stack.enter_context(patch.object(CLS, name, lambda window: None))
            if BRAND == 'Sony':
                stack.enter_context(patch.object(CLS, '_install_windows_volume_hook', lambda window: None))
                stack.enter_context(patch.object(R['AmplifierClient'], 'start', lambda client: None))
            else:
                stack.enter_context(patch.object(CLS, '_setup_hotkeys', lambda window: None))
                def ipc(window):
                    window.amplifier_server = SimpleNamespace(close=Mock(), publish=Mock(),
                                                               focused_volume={}, clients={})
                stack.enter_context(patch.object(CLS, '_setup_ipc', ipc))
            window = CLS(config)
            gates = []
            queued = Mock()
            delivered = Mock()
            try:
                pools = [(window.pool, False)]
                if BRAND == 'Sony':
                    pools.append((window.command_pool, True))
                for pool, command in pools:
                    pool.setMaxThreadCount(1)
                    started, release = threading.Event(), threading.Event()
                    gates.append(release)
                    def running(started=started, release=release):
                        started.set()
                        if not release.wait(5):
                            raise TimeoutError('Test did not release worker')
                    kwargs = {'command': command} if BRAND == 'Sony' else {}
                    window.run_worker(running, on_result=delivered, on_error=delivered, **kwargs)
                    self.assertTrue(started.wait(2))
                    for _ in range(4):
                        window.run_worker(queued, on_result=delivered, **kwargs)
                    clear = pool.clear
                    def clear_then_release(clear=clear, release=release):
                        clear()
                        release.set()
                    stack.enter_context(patch.object(pool, 'clear', side_effect=clear_then_release))
                window._pause_background_work()
                self.assertFalse(window._workers)
                self.drain()
                queued.assert_not_called()
                delivered.assert_not_called()
                self.assert_released()
            finally:
                for gate in gates:
                    gate.set()
                window._shutdown()
                window.deleteLater()
                self.drain()


if __name__ == '__main__':
    unittest.main()
