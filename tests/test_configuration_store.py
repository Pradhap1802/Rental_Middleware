import os
import shutil
import tempfile
import unittest

from app.configuration.store import ConfigStore
from app.models.domain import AppConfig


class TestConfigStoreUpdateFields(unittest.TestCase):
    """
    SyncService.execute_sync() loads its own AppConfig once at the start of a sync
    pass that can run for minutes, then (for a couple of auto-detected settings)
    saves a learned field back at the end. QueueWorker runs up to 4 entity-type sync
    passes concurrently (ThreadPoolExecutor in app/queue/worker.py), each with its
    own independently-loaded snapshot. Confirmed live: tally_order_processing_
    available flip-flopped back to unset across consecutive sync cycles because one
    pass's cfg_store.save(cfg) blindly wrote its entire stale snapshot, silently
    reverting the field a different concurrent pass had just persisted — a Tally
    company already confirmed to reject the native "Sales Order" voucher type kept
    re-attempting it and failing identically instead of the learned setting sticking.
    update_fields() exists specifically to close that window.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = ConfigStore(self.temp_dir)
        # An explicit AppConfig, not load_safe()'s auto-discovery path — that scans
        # real local install paths (see DiscoveryService.COMMON_RENTASST_PATHS),
        # which would make this test depend on whatever happens to be on the
        # machine running it.
        self.store.save(AppConfig(external_url="http://localhost:9000"))

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_update_fields_persists_and_returns_the_merged_config(self):
        result = self.store.update_fields(tally_order_processing_available=False)
        self.assertFalse(result.tally_order_processing_available)

        reloaded = self.store.load_safe()
        self.assertFalse(reloaded.tally_order_processing_available)

    def test_update_fields_does_not_lose_a_concurrent_callers_change(self):
        """
        Simulates the exact race confirmed live: two concurrent sync passes each
        loaded their own AppConfig snapshot before either had learned anything, then
        each independently discovers and tries to persist a different field.
        """
        caller_a_cfg = self.store.load_safe()  # loaded before either learns anything
        caller_b_cfg = self.store.load_safe()

        self.assertIsNone(caller_a_cfg.tally_order_processing_available)
        self.assertFalse(caller_b_cfg.tally_edu_mode)

        # Caller A learns order-processing is unavailable and persists it first.
        self.store.update_fields(tally_order_processing_available=False)
        # Caller B learns (independently, on ITS OWN stale snapshot) that edu mode is
        # required, and persists that afterward — must not clobber A's change.
        self.store.update_fields(tally_edu_mode=True)

        final = self.store.load_safe()
        self.assertFalse(final.tally_order_processing_available)  # A's change survived
        self.assertTrue(final.tally_edu_mode)  # B's change also applied


if __name__ == "__main__":
    unittest.main()
