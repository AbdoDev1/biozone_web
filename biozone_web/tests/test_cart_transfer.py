"""Characterization tests for transfer merge (plan §4.13 item 32).

Two layers:
1. `_merge_items_into_cart` on a fake cart — pins the exact behaviour that
   was inline in `transfer_apply` before the extraction (HEAD b98634f).
2. `transfer_apply`/`transfer_preview` end-to-end on real carts and
   `BZ Cart Merge` docs — pins status transitions, version checks, conflict
   mapping and idempotent replay.

Isolation: same policy as test_cart_snapshots — Administrator, customer gate
patched to None, one transaction rolled back in tearDown.
"""

import json
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from biozone_web.services import cart as cart_service

ITEM_A = "BZ-11929"
ITEM_B = "BZ-11811"


class FakeCart:
    def __init__(self, rows=None):
        self.items = [SimpleNamespace(**row) for row in (rows or [])]

    def append(self, field, row):
        assert field == "items"
        self.items.append(SimpleNamespace(**row))


def _line(code, qty, uom="Nos"):
    return {"item_code": code, "qty": qty, "uom": uom, "available": True, "issues": []}


class TestMergeItemsCharacterization(unittest.TestCase):
    """Pure-function tests: no DB, no transaction."""

    def _merge(self, target, rows):
        cart, conflicts, applied = cart_service._merge_items_into_cart(
            FakeCart(target), rows, None
        )
        return cart, conflicts, applied

    def test_empty_target_appends_lines(self):
        cart, conflicts, applied = self._merge([], [_line("A", 2), _line("B", 1)])
        self.assertEqual(conflicts, [])
        self.assertEqual(applied, 2)
        self.assertEqual(
            [(row.item_code, row.qty, row.uom) for row in cart.items],
            [("A", 2, "Nos"), ("B", 1, "Nos")],
        )

    def test_existing_line_sums_and_new_line_appends(self):
        cart, conflicts, applied = self._merge(
            [{"item_code": "A", "qty": 1, "uom": "Nos"}],
            [_line("A", 2), _line("B", 3)],
        )
        self.assertEqual(conflicts, [])
        self.assertEqual(applied, 2)
        self.assertEqual(
            [(row.item_code, row.qty) for row in cart.items],
            [("A", 3), ("B", 3)],
        )

    def test_uom_conflict_keeps_existing_qty(self):
        cart, conflicts, applied = self._merge(
            [{"item_code": "A", "qty": 1, "uom": "Nos"}],
            [_line("A", 2, "Box")],
        )
        self.assertEqual(applied, 0)
        self.assertEqual(conflicts[0]["code"], "TARGET_UOM_CONFLICT")
        self.assertEqual(conflicts[0]["item_code"], "A")
        self.assertEqual(cart.items[0].qty, 1)

    def test_qty_cap_reports_qty_limit(self):
        cart, conflicts, applied = self._merge(
            [{"item_code": "A", "qty": 99999.5, "uom": "Nos"}],
            [_line("A", 1)],
        )
        self.assertEqual(applied, 0)
        self.assertEqual(conflicts[0]["code"], "QTY_LIMIT")
        self.assertEqual(cart.items[0].qty, 99999.5)

    def test_line_cap_reports_line_limit(self):
        target = [
            {"item_code": f"L{i}", "qty": 1, "uom": "Nos"}
            for i in range(cart_service.MAX_CART_LINES)
        ]
        cart, conflicts, applied = self._merge(target, [_line("NEW", 1)])
        self.assertEqual(applied, 0)
        self.assertEqual(conflicts[0]["code"], "LINE_LIMIT")
        self.assertEqual(len(cart.items), cart_service.MAX_CART_LINES)

    def test_later_conflict_does_not_undo_earlier_in_memory_append(self):
        # Matches legacy behaviour: earlier lines are applied in memory, then
        # the request-level rollback discards them. The pure function does not
        # save, so the DB is untouched either way.
        cart, conflicts, applied = self._merge(
            [{"item_code": "A", "qty": 1, "uom": "Nos"}],
            [_line("B", 2), _line("A", 1, "Box")],
        )
        self.assertEqual(applied, 1)
        self.assertEqual([row.item_code for row in cart.items], ["A", "B"])
        self.assertEqual(conflicts[0]["code"], "TARGET_UOM_CONFLICT")


class TestTransferApply(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for doctype in ("BZ Cart", "BZ Cart Merge"):
            if not frappe.db.table_exists(doctype):
                raise unittest.SkipTest(f"{doctype} table absent — run migrate first")

    def setUp(self):
        self._prev_user = frappe.session.user
        frappe.set_user("Administrator")
        self._customer_patch = patch.object(
            cart_service, "require_customer", return_value=None
        )
        self._customer_patch.start()

    def tearDown(self):
        self._customer_patch.stop()
        frappe.set_user(self._prev_user)
        frappe.db.rollback()

    def _cart_with(self, code, qty):
        return cart_service.add_item(code, qty, cart_version=0)

    def _make_merge(self, cart, source_items, *, status="Previewed"):
        token = uuid.uuid4().hex
        doc = frappe.get_doc(
            {
                "doctype": "BZ Cart Merge",
                "user": "Administrator",
                "customer": None,
                "cart": cart.name if cart else None,
                "status": status,
                "merge_token": token,
                "source_uid": uuid.uuid4().hex,
                "source_kind": "Guest",
                "source_fingerprint": "a" * 64,
                "target_version_before": int(cart.version or 0) if cart else 0,
                "source_items": json.dumps(source_items),
            }
        ).insert(ignore_permissions=True)
        return doc, token

    def test_transfer_apply_merges_and_marks_applied(self):
        state = self._cart_with(ITEM_A, 1)
        cart_name = state["cart"]["name"]
        cart = frappe.get_doc("BZ Cart", cart_name)
        merge, token = self._make_merge(
            cart, [{"item_code": ITEM_B, "qty": 2}]
        )

        result = cart_service.transfer_apply(token, cart_version=cart.version)

        self.assertFalse(result["already_applied"])
        self.assertEqual(result["status"], "Applied")
        cart.reload()
        rows = {row.item_code: row for row in cart.items}
        self.assertEqual(set(rows), {ITEM_A, ITEM_B})
        self.assertEqual(rows[ITEM_B].qty, 2)
        merge.reload()
        self.assertEqual(merge.status, "Applied")
        self.assertEqual(merge.result_version, cart.version)
        self.assertTrue(merge.result_snapshot)

    def test_transfer_apply_replay_is_idempotent(self):
        state = self._cart_with(ITEM_A, 1)
        cart = frappe.get_doc("BZ Cart", state["cart"]["name"])
        merge, token = self._make_merge(
            cart, [{"item_code": ITEM_B, "qty": 1}]
        )
        first = cart_service.transfer_apply(token, cart_version=cart.version)
        cart.reload()
        version_after_first = cart.version

        second = cart_service.transfer_apply(token, cart_version=cart.version)

        self.assertTrue(second["already_applied"])
        self.assertEqual(second["result"], first["result"])
        cart.reload()
        self.assertEqual(cart.version, version_after_first)

    def test_transfer_apply_stale_version_returns_409(self):
        state = self._cart_with(ITEM_A, 1)
        cart = frappe.get_doc("BZ Cart", state["cart"]["name"])
        merge, token = self._make_merge(
            cart, [{"item_code": ITEM_B, "qty": 1}]
        )
        cart_service.add_item(ITEM_B, 1, cart_version=cart.version)
        cart.reload()

        with self.assertRaises(cart_service.CartError) as ctx:
            cart_service.transfer_apply(token, cart_version=cart.version - 1)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.error_code, "CART_VERSION_CONFLICT")
        merge.reload()
        self.assertEqual(merge.status, "Previewed")

    def test_transfer_apply_disabled_source_item_returns_merge_conflict(self):
        state = self._cart_with(ITEM_A, 1)
        cart = frappe.get_doc("BZ Cart", state["cart"]["name"])
        merge, token = self._make_merge(
            cart, [{"item_code": ITEM_B, "qty": 1}]
        )
        disabled_meta = {
            ITEM_B: SimpleNamespace(
                item_name="Disabled", item_group="Test", disabled=1
            )
        }
        with patch.object(cart_service, "_item_meta", return_value=disabled_meta):
            with self.assertRaises(cart_service.CartError) as ctx:
                cart_service.transfer_apply(token, cart_version=cart.version)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.error_code, "MERGE_CONFLICT")
        codes = {row["code"] for row in (ctx.exception.extra or {}).get("conflicts", [])}
        self.assertIn("ITEM_DISABLED", codes)
        cart.reload()
        self.assertEqual([row.item_code for row in cart.items], [ITEM_A])
        merge.reload()
        self.assertEqual(merge.status, "Previewed")

    def test_preview_guest_never_auto_applies(self):
        result = cart_service.transfer_preview(
            uuid.uuid4().hex,
            "Guest",
            items=[{"item_code": ITEM_A, "qty": 1}],
        )
        self.assertFalse(result["auto_apply"])
        self.assertTrue(result["can_apply"])

    def test_preview_local_account_empty_target_auto_applies(self):
        result = cart_service.transfer_preview(
            uuid.uuid4().hex,
            "Local Account",
            items=[{"item_code": ITEM_A, "qty": 1}],
        )
        self.assertTrue(result["auto_apply"])
        self.assertTrue(result["can_apply"])

    def test_preview_local_account_non_empty_target_does_not_auto_apply(self):
        state = self._cart_with(ITEM_A, 1)
        result = cart_service.transfer_preview(
            uuid.uuid4().hex,
            "Local Account",
            items=[{"item_code": ITEM_B, "qty": 1}],
            target_version=state["cart"]["version"],
        )
        self.assertFalse(result["auto_apply"])
        self.assertTrue(result["can_apply"])


if __name__ == "__main__":
    unittest.main()
