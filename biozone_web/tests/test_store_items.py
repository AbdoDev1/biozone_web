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


def run():
    suite = unittest.TestLoader().loadTestsFromName(__name__)
    return unittest.TextTestRunner(verbosity=2).run(suite)
