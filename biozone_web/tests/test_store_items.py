"""Automated tests for services.store_items (fail-closed enrichment).

DB-free by design: every external dependency (pricing engine, display
resolver, thumbnails) is monkeypatched, so these run anywhere the app
imports — via ``bench --site <site> execute`` or Frappe's test runner.
No writes, no fixtures, no network.
"""

import unittest
from unittest.mock import patch

import biozone_web.services.store_items as store_items


def _row(code="BZ-T1"):
    return {"item_code": code, "item_name": "صنف اختبار", "item_group": "مجموعة اختبار"}


def _boom(*args, **kwargs):
    raise RuntimeError("display resolver exploded")


class TestFailClosed(unittest.TestCase):
    def test_display_resolution_failure_fails_closed(self):
        """Total resolver failure must yield [] — never factor-1.0 prices."""
        with patch.object(store_items, "resolve_items_display", _boom):
            out = store_items.enrich_store_items([_row()], customer_group="صيدلية")
        self.assertEqual(out, [])

    def test_non_displayable_item_is_excluded(self):
        """displayable=False rows drop; price=None passes through untouched."""
        display = {
            "BZ-T1": {"ok": True, "displayable": False, "uom": "قطعة", "factor": 1.0},
            "BZ-T2": {"ok": True, "displayable": True, "uom": "قطعة", "factor": 1.0},
        }
        with (
            patch.object(
                store_items, "resolve_items_display", lambda codes, group: display
            ),
            patch.object(store_items, "get_effective_item_prices", lambda codes: {}),
            patch.object(store_items, "resolve_item_thumbnails", lambda codes: {}),
        ):
            out = store_items.enrich_store_items([_row("BZ-T1"), _row("BZ-T2")])
        self.assertEqual([r["item_code"] for r in out], ["BZ-T2"])
        self.assertIsNone(out[0]["price"])
        self.assertEqual(out[0]["thumbnail"], "")


class TestPricingEnrichment(unittest.TestCase):
    def test_base_price_uses_same_display_factor(self):
        """Base and payable prices must speak in the same display unit."""
        display = {
            "BZ-T1": {"ok": True, "displayable": True, "uom": "باكت", "factor": 2.0}
        }
        pricing = {
            "BZ-T1": {
                "price": 58.5,
                "base_price": 75.0,
                "discount_percentage": 22.0,
            }
        }
        with (
            patch.object(store_items, "resolve_items_display", lambda codes, group: display),
            patch.object(store_items, "get_effective_item_prices", lambda codes: pricing),
            patch.object(store_items, "resolve_item_thumbnails", lambda codes: {}),
        ):
            out = store_items.enrich_store_items([_row("BZ-T1")])
        self.assertEqual(out[0]["price"], 117.0)
        self.assertEqual(out[0]["base_price"], 150.0)
        self.assertEqual(out[0]["discount_percentage"], 22.0)

    def test_no_discount_keeps_base_equal_to_price(self):
        """No rule means no strike: base and payable stay equal, pct zero."""
        display = {
            "BZ-T1": {"ok": True, "displayable": True, "uom": "قطعة", "factor": 1.0}
        }
        pricing = {
            "BZ-T1": {
                "price": 75.0,
                "base_price": 75.0,
                "discount_percentage": 0.0,
            }
        }
        with (
            patch.object(store_items, "resolve_items_display", lambda codes, group: display),
            patch.object(store_items, "get_effective_item_prices", lambda codes: pricing),
            patch.object(store_items, "resolve_item_thumbnails", lambda codes: {}),
        ):
            out = store_items.enrich_store_items([_row("BZ-T1")])
        self.assertEqual(out[0]["price"], 75.0)
        self.assertEqual(out[0]["base_price"], 75.0)
        self.assertEqual(out[0]["discount_percentage"], 0.0)

    def test_base_and_price_are_rounded_to_two_decimals(self):
        """Both displayed prices go through money2 after the unit factor."""
        display = {
            "BZ-T1": {"ok": True, "displayable": True, "uom": "قطعة", "factor": 1.0}
        }
        pricing = {
            "BZ-T1": {
                "price": 58.333,
                "base_price": 75.555,
                "discount_percentage": 22.8,
            }
        }
        with (
            patch.object(store_items, "resolve_items_display", lambda codes, group: display),
            patch.object(store_items, "get_effective_item_prices", lambda codes: pricing),
            patch.object(store_items, "resolve_item_thumbnails", lambda codes: {}),
        ):
            out = store_items.enrich_store_items([_row("BZ-T1")])
        self.assertEqual(out[0]["price"], 58.33)
        self.assertEqual(out[0]["base_price"], 75.56)


class TestActualStock(unittest.TestCase):
    def test_reads_actual_qty_from_store_warehouse(self):
        with (
            patch.object(store_items, "get_default_warehouse", return_value="Stores - BIO"),
            patch("frappe.db.get_value", return_value=51) as get_value,
        ):
            qty = store_items.get_item_actual_qty("BZ-T1")
        self.assertEqual(qty, 51.0)
        get_value.assert_called_once_with(
            "Bin", {"item_code": "BZ-T1", "warehouse": "Stores - BIO"}, "actual_qty"
        )

    def test_missing_bin_row_is_zero(self):
        with (
            patch.object(store_items, "get_default_warehouse", return_value="Stores - BIO"),
            patch("frappe.db.get_value", return_value=None),
        ):
            self.assertEqual(store_items.get_item_actual_qty("BZ-T1"), 0.0)

    def test_empty_code_returns_none_without_lookup(self):
        with patch("frappe.db.get_value") as get_value:
            self.assertIsNone(store_items.get_item_actual_qty(""))
        get_value.assert_not_called()


def run():
    suite = unittest.TestLoader().loadTestsFromName(__name__)
    return unittest.TextTestRunner(verbosity=2).run(suite)
