import unittest

from poc.core import Attempt, accepted_handoff, portal_cookies, profile_response_valid


class CoreTests(unittest.TestCase):
    def test_handoff_is_scoped_to_official_origin_and_scheme(self):
        source = "https://login.mobywatel.gov.pl/auth"
        self.assertTrue(accepted_handoff("mobywatel://example", source, {"mobywatel"}))
        for url in ["javascript:alert(1)", "https://example.com", "mobywatel://x\n", "intent://x"]:
            self.assertFalse(accepted_handoff(url, source, {"mobywatel", "javascript", "intent"}))
        for bad in ["https://login.mobywatel.gov.pl.evil.test", "http://login.mobywatel.gov.pl"]:
            self.assertFalse(accepted_handoff("mobywatel://example", bad, {"mobywatel"}))

    def test_cookies_are_scoped_and_expired_values_excluded(self):
        base = dict(name="session", value="test", path="/bknd", secure=True, expires=200)
        jar = portal_cookies([
            dict(base, domain=".info-kierowca.pl"),
            dict(base, domain="login.gov.pl"),
            dict(base, domain="evil-info-kierowca.pl"),
            dict(base, domain="info-kierowca.pl", expires=99),
        ], now=100)
        cookies = list(jar)
        self.assertEqual(len(cookies), 1)
        self.assertEqual(cookies[0].path, "/bknd")
        self.assertTrue(cookies[0].secure)

    def test_profile_schema_and_finished_link_redaction(self):
        self.assertTrue(profile_response_valid([]))
        self.assertTrue(profile_response_valid([{"pkkNumber": "dummy", "categoryName": "B"}]))
        self.assertFalse(profile_response_valid({"error": "unauthorized"}))
        self.assertFalse(profile_response_valid([{}]))
        attempt = Attempt("test", handoff_url="mobywatel://test", finished=True)
        self.assertIsNone(attempt.public()["handoff_url"])
