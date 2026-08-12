"""Fast, offline checks used by the CI and self-healing gate."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from pipeline import etl
from scripts import demo_vulnerability
from governance import github_gov


class EtlSmokeTests(unittest.TestCase):
    def test_schema_aliases_produce_revenue(self) -> None:
        parsed = etl.parse_trades([{"product": "BTC-USD", "px": "100", "qty": "0.25"}])
        self.assertEqual(parsed[0]["amount"], 25.0)

    def test_duplicate_trades_are_not_double_counted(self) -> None:
        rows = [
            {"product": "BTC-USD", "price": "10", "size": "2", "trade_id": "same"},
            {"product": "BTC-USD", "price": "10", "size": "2", "trade_id": "same"},
        ]
        with patch.object(etl, "load", return_value={}) as load:
            result = etl.run_etl(rows)
        self.assertFalse(result["failed"])
        self.assertEqual(result["aggregate"]["total_revenue"], 20.0)
        load.assert_called_once()

    def test_all_invalid_rows_fail_closed(self) -> None:
        with patch.object(etl, "load") as load:
            result = etl.run_etl([{"product": "BTC-USD", "price": "bad", "size": "1"}])
        self.assertTrue(result["failed"])
        load.assert_not_called()


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
