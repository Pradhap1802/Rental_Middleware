import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from app.models.domain import AppConfig
from app.configuration.store import ConfigStore
from app.services.config_service import ConfigService
from app.security.masking import mask_secret, mask_payload_secrets, mask_log_message


class TestSecurityAndMasking(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = ConfigStore(self.temp_dir)
        self.svc = ConfigService(self.temp_dir)

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_config_encryption_at_rest(self):
        cfg = AppConfig(
            rentasst_url="http://localhost:8000",
            rentasst_api_key="super_secret_token_12345",
            external_url="http://localhost:9000",
            external_api_key="tally_secret_key_99999",
        )
        self.store.save(cfg)

        # Verify config.json.enc file exists on disk
        enc_file = os.path.join(self.temp_dir, "config.json.enc")
        self.assertTrue(os.path.exists(enc_file))

        # Verify file contents are encrypted ciphertext and do NOT contain plaintext secrets
        with open(enc_file, "rb") as f:
            raw = f.read()

        self.assertNotIn(b"super_secret_token_12345", raw)
        self.assertNotIn(b"tally_secret_key_99999", raw)

        # Verify decryption works cleanly
        loaded = self.store.load_safe()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.rentasst_api_key, "super_secret_token_12345")
        self.assertEqual(loaded.external_api_key, "tally_secret_key_99999")

    def test_environment_variable_overrides(self):
        cfg = AppConfig(rentasst_api_key="stored_token")
        self.store.save(cfg)

        with patch.dict(os.environ, {"RENTASST_API_TOKEN": "override_token_from_env"}):
            loaded = self.store.load_safe()
            self.assertEqual(loaded.rentasst_api_key, "override_token_from_env")

    def test_secret_masking_utilities(self):
        masked = mask_secret("sk_live_1234567890abcdef")
        self.assertEqual(masked, "sk_l****cdef")
        self.assertNotIn("1234567890", masked)

        payload = {"username": "admin", "password": "super_secret_password", "token": "abc123token"}
        masked_payload = mask_payload_secrets(payload)
        self.assertEqual(masked_payload["username"], "admin")
        self.assertNotIn("super_secret_password", str(masked_payload["password"]))
        self.assertNotIn("abc123token", str(masked_payload["token"]))

        log_str = "Authenticated with Bearer secret_bearer_token_xyz"
        masked_log = mask_log_message(log_str)
        self.assertNotIn("secret_bearer_token_xyz", masked_log)
        self.assertIn("Bearer ****", masked_log)

    def test_masked_config_update_preservation(self):
        original_cfg = AppConfig(
            rentasst_url="http://localhost:8000",
            rentasst_api_key="original_secret_token_777",
        )
        self.svc.save_config(original_cfg)

        update_cfg = AppConfig(
            rentasst_url="http://localhost:8000",
            rentasst_api_key="orig****777",
            sync_interval_minutes=15,
        )
        self.svc.save_config(update_cfg)

        reloaded = self.store.load_safe()
        self.assertEqual(reloaded.rentasst_api_key, "original_secret_token_777")
        self.assertEqual(reloaded.sync_interval_minutes, 15)

    def test_invalid_env_secret_key_fails_loudly(self):
        """
        An invalid RENTAL_MIDDLEWARE_SECRET_KEY must raise, not silently downgrade to a
        SHA-256-derived key from whatever string was provided — a weak passphrase should
        never be silently accepted as if it were a real Fernet key.
        """
        with patch.dict(os.environ, {"RENTAL_MIDDLEWARE_SECRET_KEY": "not-a-valid-fernet-key"}):
            with self.assertRaises(ValueError):
                self.store._get_fernet()

    def test_invalid_env_secret_key_not_masked_by_load_safe(self):
        """
        load_safe()'s broad except-and-fallback-to-auto-discovery must not swallow a
        misconfigured secret key — that would silently discard the user's real saved
        config in favor of auto-discovered defaults instead of surfacing the real problem.
        """
        cfg = AppConfig(rentasst_url="http://localhost:8000", rentasst_api_key="real_token")
        self.store.save(cfg)

        with patch.dict(os.environ, {"RENTAL_MIDDLEWARE_SECRET_KEY": "not-a-valid-fernet-key"}):
            with self.assertRaises(ValueError):
                self.store.load_safe()


if __name__ == "__main__":
    unittest.main()
