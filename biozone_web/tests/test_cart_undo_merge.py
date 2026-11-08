"""Plan §4.13 tests 16, 18 and 20: Undo merge on a non-empty cart.

- 16: merge sums quantities and respects QTY/LINE caps.
- 18: over MAX_CART_LINES is reported as LINE_CAP while the rest restores.
- 20: restore prices come from live pricing, never from the snapshot
  (the snapshot stores no prices at all).

Isolation: Administrator, customer gate patched to None, one transaction
rolled back in tearDown.
"""

import json
import unittest
from unittest.mock import patch

import frappe

from biozone_web.services import cart as cart_service

ITEM_A = "BZ-11929"
ITEM_B = "BZ-11811"
ITEM_C = "BZ-11010"
TOKEN_16 = "c" * 64
TOKEN_16B = "f" * 64
TOKEN_18 = "d" * 64
TOKEN_20 = "e" * 64


class TestUndoMerge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not frappe.db.table_exists("BZ Cart Snapshot"):
            raise unittest.SkipTest("BZ Cart Snapshot table absent — run migrate first")

    def setUp(self):
        self._prev_user = frappe.session.user
        frappe.set_user("Administrator")
        self._customer_patch = patch.object(
            cart_service, "require_customer", return_value=None
        )
        self._customer_patch.start()
        self._flag_patch = patch.object(
            cart_service, "snapshots_enabled", return_value=True
        )
        self._flag_patch.start()

    def tearDown(self):
        self._flag_patch.stop()
        self._customer_patch.stop()
        frappe.set_user(self._prev_user)
        frappe.db.rollback()

    def _cart_with(self, rows):
        cart = cart_service.get_active_cart("Administrator")
        version = int(cart.version or 0) if cart else 0
        state = None
        for code, qty in rows:
            state = cart_service.add_item(code, qty, cart_version=version)
            version = state["cart"]["version"]
        return state

    def _snapshot_of(self, rows):
        state = self._cart_with(rows)
        cleared = cart_service.clear_cart(cart_version=state["cart"]["version"])
        return cleared["undo"]["snapshot"], cleared["cart"]["version"]

    def test_merge_sums_qty_and_keeps_other_lines(self):
        snap, _ = self._snapshot_of([(ITEM_A, 2), (ITEM_B, 3)])
        state = self._cart_with([(ITEM_A, 1), (ITEM_C, 1)])

        result = cart_service.undo(
            snap, TOKEN_16, cart_version=state["cart"]["version"]
        )

        self.assertEqual(result["applied_mode"], "Merge")
        self.assertEqual(result["restored_count"], 2)
        self.assertEqual(result["skipped_count"], 0)
        rows = {row["item_code"]: row for row in result["cart"]["items"]}
        self.assertEqual(rows[ITEM_A]["qty"], 3)
        self.assertEqual(rows[ITEM_B]["qty"], 3)
        self.assertEqual(rows[ITEM_C]["qty"], 1)

    def test_merge_skips_qty_cap_and_restores_rest(self):
        snap, _ = self._snapshot_of([(ITEM_A, 2), (ITEM_B, 3)])
        state = self._cart_with([(ITEM_A, 99999.5)])

        result = cart_service.undo(
            snap, TOKEN_16B, cart_version=state["cart"]["version"]
        )

        self.assertEqual(result["applied_mode"], "Merge")
        self.assertEqual(result["restored_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        skipped = result["skipped"][0]
        self.assertEqual(skipped["item_code"], ITEM_A)
        self.assertEqual(skipped["reason"], "QTY_CAP")
        rows = {row["item_code"]: row for row in result["cart"]["items"]}
        self.assertEqual(rows[ITEM_A]["qty"], 99999.5)
        self.assertEqual(rows[ITEM_B]["qty"], 3)

    def test_over_max_lines_skips_and_reports_line_cap(self):
        snap, _ = self._snapshot_of([(ITEM_A, 2), (ITEM_B, 1)])
        state = self._cart_with([(ITEM_C, 1)])

        with patch.object(cart_service, "MAX_CART_LINES", 2):
            result = cart_service.undo(
                snap, TOKEN_18, cart_version=state["cart"]["version"]
            )

        self.assertEqual(result["applied_mode"], "Merge")
        self.assertEqual(result["restored_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        skipped = result["skipped"][0]
        self.assertEqual(skipped["item_code"], ITEM_B)
        self.assertEqual(skipped["reason"], "LINE_CAP")
        rows = {row["item_code"]: row for row in result["cart"]["items"]}
        self.assertEqual(set(rows), {ITEM_A, ITEM_C})
        self.assertEqual(rows[ITEM_A]["qty"], 2)

    def test_restore_uses_live_price_not_snapshot(self):
        state = self._cart_with([(ITEM_A, 1)])
        live_price = state["quote"]["items"][0]["price"]
        self.assertIsNotNone(live_price)
        cleared = cart_service.clear_cart(cart_version=state["cart"]["version"])
        snap = cleared["undo"]["snapshot"]

        snapshot_doc = frappe.get_doc("BZ Cart Snapshot", snap)
        self.assertNotIn("price", json.loads(snapshot_doc.items_json)[0])

        from biozone_web.utils import get_effective_item_prices as real_prices

        def doubled(codes, **kwargs):
            result = real_prices(codes, **kwargs)
            for code in result:
                if code == ITEM_A and result[code].get("price") is not None:
                    result[code]["price"] = result[code]["price"] * 2
            return result

        with patch("biozone_web.utils.get_effective_item_prices", side_effect=doubled):
            restored = cart_service.undo(
                snap, TOKEN_20, cart_version=cleared["cart"]["version"]
            )

        item = restored["quote"]["items"][0]
        self.assertEqual(item["item_code"], ITEM_A)
        self.assertAlmostEqual(item["price"], live_price * 2, places=2)


if __name__ == "__main__":
    unittest.main()
