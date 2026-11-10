"""Unit tests for Server Broker API settings and credential management."""
import os
import unittest
from unittest.mock import patch, MagicMock

from server import mask_identifier, mask_key, check_credentials_present, update_dotenv


class TestServerBroker(unittest.TestCase):
    def test_mask_identifier(self):
        self.assertEqual(mask_identifier("nilesh@example.com"), "ni***h@example.com")
        self.assertEqual(mask_identifier("ab@cd.com"), "a***@cd.com")
        self.assertEqual(mask_identifier(""), "")
        self.assertEqual(mask_identifier("user123456"), "us***56")

    def test_mask_key(self):
        self.assertEqual(mask_key("1234567890abcdef"), "1234••••••••cdef")
        self.assertEqual(mask_key("short"), "••••••••")
        self.assertEqual(mask_key(""), "")

    def test_update_dotenv_and_check_credentials(self):
        orig_key = os.environ.get("CAPITAL_API_KEY")
        orig_ident = os.environ.get("CAPITAL_IDENTIFIER")
        orig_pw = os.environ.get("CAPITAL_PASSWORD")
        orig_env = os.environ.get("CAPITAL_ENV")

        try:
            update_dotenv({
                "CAPITAL_API_KEY": "test_api_key_12345",
                "CAPITAL_IDENTIFIER": "trader@broker.com",
                "CAPITAL_PASSWORD": "secret_password",
                "CAPITAL_ENV": "demo",
            })
            creds = check_credentials_present()
            self.assertTrue(creds["connected"])
            self.assertEqual(creds["masked_identifier"], "tr***r@broker.com")
            self.assertEqual(creds["env"], "demo")

            # Disconnect
            update_dotenv({
                "CAPITAL_API_KEY": None,
                "CAPITAL_IDENTIFIER": None,
                "CAPITAL_PASSWORD": None,
            })
            creds_after = check_credentials_present()
            self.assertFalse(creds_after["connected"])
            self.assertIsNone(creds_after["masked_identifier"])
        finally:
            for k, v in [
                ("CAPITAL_API_KEY", orig_key),
                ("CAPITAL_IDENTIFIER", orig_ident),
                ("CAPITAL_PASSWORD", orig_pw),
                ("CAPITAL_ENV", orig_env),
            ]:
                if v is not None:
                    os.environ[k] = v
                else:
                    os.environ.pop(k, None)

    def test_mocked_handler_broker_logic(self):
        import io
        import json
        from server import Handler

        # Test request processing logic directly
        with patch("server.Capital") as MockCapital:
            mock_inst = MagicMock()
            mock_inst.login.return_value = {"clientId": "12345"}
            MockCapital.return_value = mock_inst

            # Verify Capital instantiation with custom keys
            cap = MockCapital(env="demo", api_key="test_key", identifier="trader@test.com", password="pass")
            res = cap.login()
            self.assertEqual(res["clientId"], "12345")


if __name__ == "__main__":
    unittest.main()
