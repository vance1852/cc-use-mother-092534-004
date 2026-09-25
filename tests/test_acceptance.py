import unittest

from festival_foundation.acceptance import run
from festival_foundation.festival_acceptance import run as run_festival


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["first_replayed"])
        self.assertTrue(result["second_replayed"])
        self.assertEqual(1, result["records"])

    def test_festival_offline_acceptance(self):
        result = run_festival()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual("released", result["plan_status"])
        self.assertEqual(0, result["blocked_units"])
        self.assertEqual(2, result["impact_assessments"])
        self.assertEqual(0, result["resource_changes"])


if __name__ == "__main__":
    unittest.main()
