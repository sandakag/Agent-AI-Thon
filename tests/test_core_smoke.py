"""Fast, offline checks used by the CI and self-healing gate."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from pipeline import etl, etl_hardened, pricing
from scripts import demo_vulnerability
from governance import github_gov
from incidents import KNOWN_RUNTIME_INCIDENTS, ids
from reset import phase_one_demo_source


class EtlSmokeTests(unittest.TestCase):
    """Core ETL contract asserted against the LIVE pipeline/etl.py.

    These hold in BOTH the hardened and the Phase 2 runtime-vulnerable state, so
    inducing a runtime incident never turns CI red — the runtime hardening itself
    is covered by EtlHardenedReferenceTests below.
    """

    def test_amount_is_price_times_size(self) -> None:
        parsed = etl.parse_trades([{"product": "BTC-USD", "price": "100", "size": "0.25"}])
        self.assertEqual(parsed[0]["amount"], 25.0)

    def test_revenue_aggregates_per_product(self) -> None:
        parsed = etl.parse_trades([
            {"product": "BTC-USD", "price": "10", "size": "2"},
            {"product": "ETH-USD", "price": "5", "size": "4"},
        ])
        agg = etl.aggregate(parsed)
        self.assertEqual(agg["total_revenue"], 40.0)
        self.assertEqual(agg["per_product"]["BTC-USD"], 20.0)

    def test_all_invalid_rows_fail_closed(self) -> None:
        with patch.object(etl, "load") as load:
            result = etl.run_etl([{"product": "BTC-USD", "price": "bad", "size": "1"}])
        self.assertTrue(result["failed"])
        load.assert_not_called()


class EtlHardenedReferenceTests(unittest.TestCase):
    """The runtime hardening the guardian stages as a Phase 2 fix PR.

    Asserted against pipeline/etl_hardened.py — the verified remediation target —
    so this coverage never depends on the live parser's current demo state.
    """

    def test_schema_aliases_produce_revenue(self) -> None:
        parsed = etl_hardened.parse_trades(
            [{"product": "BTC-USD", "px": "100", "qty": "0.25"}])
        self.assertEqual(parsed[0]["amount"], 25.0)

    def test_duplicate_trades_are_not_double_counted(self) -> None:
        rows = [
            {"product": "BTC-USD", "price": "10", "size": "2", "trade_id": "same"},
            {"product": "BTC-USD", "price": "10", "size": "2", "trade_id": "same"},
        ]
        with patch.object(etl_hardened, "load", return_value={}) as load:
            result = etl_hardened.run_etl(rows)
        self.assertFalse(result["failed"])
        self.assertEqual(result["aggregate"]["total_revenue"], 20.0)
        load.assert_called_once()


class PricingTests(unittest.TestCase):
    def test_round_amount_rounds_to_cents(self) -> None:
        self.assertEqual(pricing.round_amount(12.3456), 12.35)
        self.assertEqual(pricing.round_amount(1.0), 1.0)


class DemoVulnerabilityTests(unittest.TestCase):
    def test_demo_fault_is_reversible(self) -> None:
        hardened = demo_vulnerability.HARDENED
        vulnerable = demo_vulnerability.enable(hardened)
        self.assertIn(demo_vulnerability.DEMO_FAULT, vulnerable)
        self.assertEqual(demo_vulnerability.disable(vulnerable), hardened)

    def test_guardian_accepts_a_syntax_checked_copilot_repair(self) -> None:
        vulnerable = "def parser():\n    return 'vulnerable'\n"
        repaired = "def parser():\n    return 'repaired'\n"

        class FakeCopilot:
            available = True

            def chat(self, *_args, **_kwargs):
                return repaired

        with patch.object(github_gov, "CopilotCliBrain", return_value=FakeCopilot()):
            result = github_gov._copilot_code_fix({}, None, vulnerable)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], repaired)


class RepeatableDemoTests(unittest.TestCase):
    def test_reset_source_has_one_controlled_ci_fault(self) -> None:
        pricing_source = (demo_vulnerability.ROOT / "pipeline" / "pricing.py").read_text(
            encoding="utf-8"
        )
        source = phase_one_demo_source(pricing_source)
        self.assertIn("return round(amount)", source)
        self.assertNotIn("return round(amount, 2)", source)
        compile(source, "pipeline/pricing.py", "exec")
        namespace: dict = {}
        exec(source, namespace)
        # The independent Phase 1 fault: rounding silently drops decimal places.
        self.assertEqual(namespace["round_amount"](12.3456), 12)
        # Phase 1's fix never touches pipeline/etl.py -- Phase 2's vulnerability
        # (missing alias resolution / dedup / null-quarantine) is unaffected.
        repaired = source.replace(
            "    return round(amount)\n", "    return round(amount, 2)\n", 1
        )
        namespace = {}
        exec(repaired, namespace)
        self.assertEqual(namespace["round_amount"](12.3456), 12.35)

    def test_six_known_runtime_incidents_are_available(self) -> None:
        self.assertEqual(len(KNOWN_RUNTIME_INCIDENTS), 6)
        self.assertEqual(len(set(ids())), 6)
        for incident in KNOWN_RUNTIME_INCIDENTS:
            self.assertTrue(incident["label"])
            self.assertTrue(incident["ops"])
