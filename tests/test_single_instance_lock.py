import os
import shutil
import tempfile
import unittest

from app.main import _acquire_single_instance_lock


class TestSingleInstanceLock(unittest.TestCase):
    """
    _TALLY_HTTP_LOCK in TallyClient only serializes requests within one process — two
    separate middleware processes running at once (confirmed live during development:
    two independent `python run.py` instances left running simultaneously against the
    same Tally company) each hold their own copy of that lock and can send genuinely
    concurrent, unserialized XML imports to Tally, which this codebase already
    documents as capable of corrupting Tally's current-company context or crashing its
    process outright. A second live process must be refused at startup, not merely
    discouraged.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.lock_path = os.path.join(self.temp_dir, "middleware.lock")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_second_acquisition_is_refused_while_first_is_held(self):
        first = _acquire_single_instance_lock(self.lock_path)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                _acquire_single_instance_lock(self.lock_path)
            self.assertIn("already holds the lock", str(ctx.exception))
        finally:
            if first:
                first.close()

    def test_lock_is_reacquirable_after_release(self):
        first = _acquire_single_instance_lock(self.lock_path)
        if first:
            first.close()

        second = _acquire_single_instance_lock(self.lock_path)
        self.assertIsNotNone(second) if os.name == "nt" else None
        if second:
            second.close()


if __name__ == "__main__":
    unittest.main()
