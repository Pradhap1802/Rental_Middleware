import os
import shutil
import tempfile
import unittest

from app.scheduler.manager import SyncScheduler


class TestSyncSchedulerRestart(unittest.TestCase):
    """
    BackgroundScheduler.shutdown() permanently kills its executor's underlying
    concurrent.futures.ThreadPoolExecutor — calling start() again on the same instance
    resumes the scheduler loop fine (no error at start time), but every job submission
    once an interval trigger actually fires then raises
    "RuntimeError: cannot schedule new futures after shutdown". This is exactly what a
    login/logout cycle does live: logout calls scheduler.stop(), a later login/config
    save calls scheduler.start() again on the same SyncScheduler singleton.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.scheduler = SyncScheduler(self.temp_dir)

    def tearDown(self):
        if self.scheduler.is_running:
            self.scheduler.stop()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_restart_after_stop_uses_a_live_executor(self):
        self.scheduler.start(interval_minutes=1)
        self.scheduler.stop()
        self.scheduler.start(interval_minutes=1)

        # This is exactly what APScheduler's own ThreadPoolExecutor._do_submit_job does
        # internally when an interval trigger fires — if the pool was left shut down,
        # this raises "RuntimeError: cannot schedule new futures after shutdown".
        pool = self.scheduler.scheduler._executors["default"]._pool
        future = pool.submit(lambda: "ok")
        self.assertEqual(future.result(timeout=5), "ok")

    def test_multiple_stop_start_cycles_stay_usable(self):
        for _ in range(3):
            self.scheduler.start(interval_minutes=1)
            self.scheduler.stop()

        self.scheduler.start(interval_minutes=1)
        pool = self.scheduler.scheduler._executors["default"]._pool
        future = pool.submit(lambda: "ok")
        self.assertEqual(future.result(timeout=5), "ok")


if __name__ == "__main__":
    unittest.main()
