import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from app.database.connection import DatabaseManager
from app.clients.rentasst_client import RentAsstClient
from app.models.domain import AppConfig, RentAsstLoginRequest


class TestBearerTokenDB(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_state.db")
        self.db_mgr = DatabaseManager(self.db_path)

    def tearDown(self):
        self.db_mgr.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_save_and_get_bearer_token_by_email(self):
        email = "admin@example.com"
        token = "bearer_token_xyz_123"
        tenant_id = "B100001"

        # Initially token should not exist
        res = self.db_mgr.get_bearer_token(email)
        self.assertIsNone(res)

        # Save token
        self.db_mgr.save_bearer_token(email, token, tenant_id)

        # Fetch token by login mail ID
        fetched = self.db_mgr.get_bearer_token(email)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched["token"], token)
        self.assertEqual(fetched["tenant_id"], tenant_id)

        # Case-insensitive mail ID search
        fetched_upper = self.db_mgr.get_bearer_token("ADMIN@EXAMPLE.COM")
        self.assertIsNotNone(fetched_upper)
        self.assertEqual(fetched_upper["token"], token)

    def test_save_and_get_bearer_token_by_email_and_business(self):
        email = "admin@example.com"
        self.db_mgr.save_bearer_token(email, "token_business_1", "B100001")
        self.db_mgr.save_bearer_token(email, "token_business_2", "B200002")

        first = self.db_mgr.get_bearer_token(email, "B100001")
        second = self.db_mgr.get_bearer_token(email, "B200002")

        self.assertEqual(first["token"], "token_business_1")
        self.assertEqual(second["token"], "token_business_2")

    def test_rentasst_client_login_fetches_from_db(self):
        email = "user@tenant.com"
        token = "sanctum_bearer_token_999"
        tenant_id = "B200002"

        self.db_mgr.save_bearer_token(email, token, tenant_id)

        cfg = AppConfig(rentasst_url="http://localhost:8000")
        client = RentAsstClient(cfg)

        # Login using only login mail ID and db_mgr
        res = client.login(email=email, business_code=tenant_id, db_mgr=self.db_mgr)
        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("token"), token)
        self.assertEqual(res.get("tenant_id"), tenant_id)
        self.assertEqual(res.get("source"), "database")

    def test_rentasst_client_login_sends_email_without_password(self):
        cfg = AppConfig(rentasst_url="http://localhost:8000")
        client = RentAsstClient(cfg)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"token": "token_123", "tenant_id": "B100001"}
        client.session.get = MagicMock(return_value=mock_response)

        res = client.login(email="user@tenant.com", business_code="B100001")

        self.assertEqual(res.get("token"), "token_123")
        posted_payload = client.session.get.call_args.kwargs["params"]
        self.assertEqual(posted_payload["email"], "user@tenant.com")
        self.assertEqual(posted_payload["mail_id"], "user@tenant.com")
        self.assertEqual(posted_payload["login_email"], "user@tenant.com")
        self.assertEqual(posted_payload["login_mail_id"], "user@tenant.com")
        self.assertEqual(posted_payload["username"], "user@tenant.com")
        self.assertEqual(posted_payload["business_code"], "B100001")
        self.assertNotIn("password", posted_payload)

    def test_rentasst_client_login_skips_password_required_endpoint(self):
        cfg = AppConfig(rentasst_url="http://localhost:8000")
        client = RentAsstClient(cfg)
        password_required_response = MagicMock()
        password_required_response.status_code = 422
        password_required_response.json.return_value = {"message": "The password field is required."}
        token_response = MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {"token": "token_after_fallback", "tenant_id": "B100001"}
        client.session.get = MagicMock(side_effect=[password_required_response, token_response])

        res = client.login(email="user@tenant.com", target_url="http://localhost:8000")

        self.assertEqual(res.get("token"), "token_after_fallback")
        self.assertEqual(client.session.get.call_count, 2)

    def test_rentasst_client_login_uses_post_when_token_get_not_allowed(self):
        cfg = AppConfig(rentasst_url="http://localhost:8000")
        client = RentAsstClient(cfg)
        get_not_allowed_response = MagicMock()
        get_not_allowed_response.status_code = 405
        post_token_response = MagicMock()
        post_token_response.status_code = 200
        post_token_response.json.return_value = {"bearer_token": "post_token_123", "business_code": "B100001"}
        client.session.get = MagicMock(return_value=get_not_allowed_response)
        client.session.post = MagicMock(return_value=post_token_response)

        res = client.login(email="user@tenant.com", target_url="http://localhost:8000")

        self.assertEqual(res.get("bearer_token"), "post_token_123")
        self.assertNotIn("password", client.session.post.call_args.kwargs["json"])

    def test_rentasst_client_login_falls_back_to_config_api_key(self):
        cfg = AppConfig(rentasst_url="http://localhost:8000", rentasst_api_key="config_token_fallback_555", rentasst_tenant_id="B100001")
        client = RentAsstClient(cfg)
        not_found_response = MagicMock()
        not_found_response.status_code = 404
        client.session.get = MagicMock(return_value=not_found_response)
        client.session.post = MagicMock(return_value=not_found_response)

        res = client.login(email="new_user@tenant.com", db_mgr=self.db_mgr)

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("token"), "config_token_fallback_555")
        self.assertEqual(res.get("source"), "config")

        # Verify it was saved to DB for future lookups
        db_record = self.db_mgr.get_bearer_token("new_user@tenant.com")
        self.assertIsNotNone(db_record)
        self.assertEqual(db_record["token"], "config_token_fallback_555")

    def test_rentasst_client_login_with_mobile_number(self):
        cfg = AppConfig(rentasst_url="http://localhost:8000", rentasst_api_key="mobile_token_123")
        client = RentAsstClient(cfg)
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        client.session.get = MagicMock(return_value=mock_resp)
        client.session.post = MagicMock(return_value=mock_resp)

        res = client.login(email="9876543210", db_mgr=self.db_mgr)

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("token"), "mobile_token_123")
        db_record = self.db_mgr.get_bearer_token("9876543210")
        self.assertIsNotNone(db_record)
        self.assertEqual(db_record["token"], "mobile_token_123")

    def test_rentasst_login_request_model(self):
        req = RentAsstLoginRequest(email="test@rentasst.com")
        self.assertEqual(req.email, "test@rentasst.com")



if __name__ == "__main__":
    unittest.main()
