import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure script directory is in Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from delhivery import DelhiveryConnector
from hubspot import HubSpotConnector


class TestHubSpotConnector(unittest.TestCase):

    def setUp(self):
        self.connector = HubSpotConnector(access_token="test_token_123")

    def test_invalid_contact_id(self):
        """Verify regex blocks malformed IDs before making network calls."""
        res = self.connector.get_contact("invalid_abc")
        self.assertEqual(res.get("code"), "INVALID_RECORD_ID")

    def test_invalid_email_search(self):
        """Verify malformed email returns INVALID_ARGUMENT."""
        res = self.connector.search_contacts(email="not-an-email")
        self.assertEqual(res.get("code"), "INVALID_ARGUMENT")

    @patch("httpx.Client.request")
    def test_get_contact_success(self, mock_request):
        """Mock successful contact lookup (200 OK)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "12345",
            "properties": {
                "firstname": "Sarah",
                "lastname": "Connor",
                "email": "sarah@example.com",
                "phone": "+1234567890",
            },
        }
        mock_request.return_value = mock_resp

        res = self.connector.get_contact("12345")
        self.assertEqual(res["id"], "12345")
        self.assertEqual(res["first_name"], "Sarah")
        self.assertEqual(res["email"], "sarah@example.com")

    @patch("httpx.Client.request")
    def test_auth_failure(self, mock_request):
        """Mock 401 Unauthorized response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"message": "Invalid token"}
        mock_request.return_value = mock_resp

        res = self.connector.get_contact("12345")
        self.assertEqual(res.get("code"), "PROVIDER_AUTH_FAILED")
        self.assertTrue(res.get("reconnect_required"))

    def test_unsupported_commerce_capability(self):
        """Verify unsupported commerce methods return NOT_SUPPORTED error."""
        res = self.connector.get_order_details("ord_123", "comp_1")
        self.assertEqual(res.get("code"), "NOT_SUPPORTED")


class TestDelhiveryConnector(unittest.TestCase):

    def setUp(self):
        self.connector = DelhiveryConnector(api_token="test_delhivery_token")

    def test_unsupported_commerce_method(self):
        """Verify non-tracking commerce methods return NOT_SUPPORTED."""
        res = self.connector.get_orders("company_1")
        self.assertIsInstance(res, list)
        self.assertEqual(res[0].get("code"), "NOT_SUPPORTED")


if __name__ == "__main__":
    unittest.main()
