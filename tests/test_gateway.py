from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from app import DemoProvider, Gateway, Ledger, Policy, QuotaError


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.ledger = Ledger(Path(self.directory.name) / "ledger.db")
        for tenant in ["a", "b"]:
            self.ledger.create_account(tenant, 20000)
        self.providers = {name: DemoProvider(name) for name in ["fast", "quality"]}
        self.policies = {name: Policy(name) for name in self.providers}
        self.now = 1000.0
        self.gateway = Gateway(self.ledger, self.providers, self.policies, clock=lambda: self.now)

    def test_cache_isolated_by_tenant(self):
        first = self.gateway.request("a", "classify", "classify this")
        cached = self.gateway.request("a", "classify", "classify this")
        other = self.gateway.request("b", "classify", "classify this")
        self.assertFalse(first["cache_hit"])
        self.assertTrue(cached["cache_hit"])
        self.assertFalse(other["cache_hit"])
        self.assertEqual(cached["charged_credits"], 0)
        self.assertEqual(self.providers["fast"].calls, 2)

    def test_cache_expires(self):
        self.gateway.request("a", "classify", "same")
        self.now += 61
        self.assertFalse(self.gateway.request("a", "classify", "same")["cache_hit"])

    def test_model_change_invalidates_cache(self):
        self.gateway.request("a", "classify", "same")
        self.providers["fast"].model = "changed-model"
        self.assertFalse(self.gateway.request("a", "classify", "same")["cache_hit"])

    def test_failure_falls_back_and_opens_circuit(self):
        self.providers["fast"].fail = True
        result = self.gateway.request("a", "classify", "first")
        self.assertEqual(result["provider"], "quality")
        second = self.gateway.request("a", "classify", "different")
        self.assertEqual(second["trace"][0]["step"], "circuit_open")
        self.assertEqual(self.providers["fast"].calls, 1)
        self.assertEqual(self.ledger.balance("a")["reserved"], 0)

    def test_circuit_allows_new_attempt_after_cooldown(self):
        self.providers["fast"].fail = True
        self.gateway.request("a", "classify", "first")
        self.now += 31
        self.providers["fast"].fail = False
        self.assertEqual(self.gateway.request("a", "classify", "next")["provider"], "fast")

    def test_quota_denial_never_calls_provider(self):
        self.ledger.create_account("small", 1)
        with self.assertRaises(QuotaError):
            self.gateway.request("small", "classify", "hello")
        self.assertEqual(sum(p.calls for p in self.providers.values()), 0)

    def test_concurrent_reservations_do_not_overspend(self):
        self.ledger.create_account("limited", 100)
        def reserve(_):
            try:
                return self.ledger.reserve("limited", 60)
            except QuotaError:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(reserve, range(12)))
        self.assertEqual(sum(x is not None for x in ids), 1)
        self.assertEqual(self.ledger.balance("limited")["available"], 40)

    def test_settlement_is_not_double_charged(self):
        reservation = self.ledger.reserve("a", 100)
        self.ledger.settle(reservation, 30)
        with self.assertRaises(ValueError):
            self.ledger.settle(reservation, 30)
        self.assertEqual(self.ledger.balance("a")["used"], 30)

    def test_unsettled_work_survives_restart(self):
        self.ledger.reserve("a", 100)
        reopened = Ledger(Path(self.directory.name) / "ledger.db")
        self.assertEqual(reopened.balance("a")["reserved"], 100)

    def test_unknown_outcome_charges_full_reservation(self):
        reservation = self.ledger.reserve("a", 100)
        self.ledger.settle(reservation, None)
        self.assertEqual(self.ledger.balance("a")["used"], 100)

    def test_trace_omits_request_text(self):
        result = self.gateway.request("a", "reason", "PRIVATE-PROMPT")
        self.assertNotIn("PRIVATE-PROMPT", str(result["trace"]))

    def test_negative_quota_values_rejected(self):
        with self.assertRaises(ValueError):
            self.ledger.reserve("a", -10)
        with self.assertRaises(ValueError):
            Policy("bad", -1)


if __name__ == "__main__":
    unittest.main()
