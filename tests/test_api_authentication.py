import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock
from fastapi import HTTPException

from app.security.api_key import get_or_create_api_key
from app.security.auth import require_api_key


class TestApiAuthentication(unittest.TestCase):
    """
    Before this, every /api/* route (including one that writes RentAsst/Tally
    credentials and one that restores a backup) had no authentication at all, on an
    app that binds 0.0.0.0 by default. These tests cover the local API key that now
    guards every route in app.api.all_routers.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_api_key_generated_and_persisted(self):
        key1 = get_or_create_api_key(self.temp_dir)
        self.assertTrue(len(key1) >= 32)

        key_path = os.path.join(self.temp_dir, "api.key")
        self.assertTrue(os.path.exists(key_path))

        # A second call must return the SAME key, not regenerate one — otherwise every
        # restart would invalidate the key the browser UI already has.
        key2 = get_or_create_api_key(self.temp_dir)
        self.assertEqual(key1, key2)

    def test_two_data_dirs_get_different_keys(self):
        dir_a = tempfile.mkdtemp()
        dir_b = tempfile.mkdtemp()
        try:
            self.assertNotEqual(get_or_create_api_key(dir_a), get_or_create_api_key(dir_b))
        finally:
            shutil.rmtree(dir_a, ignore_errors=True)
            shutil.rmtree(dir_b, ignore_errors=True)

    async def _call_require_api_key(self, expected_key, supplied_header):
        request = MagicMock()
        request.app.state.api_key = expected_key
        await require_api_key(request, x_middleware_key=supplied_header)

    def test_missing_header_is_rejected(self):
        import asyncio
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(self._call_require_api_key("real-key-123", None))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_wrong_header_is_rejected(self):
        import asyncio
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(self._call_require_api_key("real-key-123", "wrong-key"))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_correct_header_is_accepted(self):
        import asyncio
        # Must not raise
        asyncio.run(self._call_require_api_key("real-key-123", "real-key-123"))

    def test_unset_app_key_rejects_everything(self):
        """If app.state.api_key is somehow unset, fail closed, not open."""
        import asyncio
        with self.assertRaises(HTTPException):
            asyncio.run(self._call_require_api_key(None, "anything"))


if __name__ == "__main__":
    unittest.main()
