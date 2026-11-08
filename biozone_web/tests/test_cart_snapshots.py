"""P0 snapshot tests: create/replay, Undo (Replace/Merge), expiry, purge.

Each test runs inside one transaction and rolls it back in tearDown. The
customer gate is patched to `None` so the tests stay on Administrator and do
not create Users/Customers (same isolation policy as test_cart_expiry).
"""

import json
import unittest
from unittest.mock import patch

import frappe

from biozone_web.services import cart as cart_service

ITEM_A = "BZ-11929"
ITEM_B = "BZ-11811"


class TestCartSnapshots(unittest.TestCase):
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

    def _cart_with(self, code=ITEM_A, qty=1):
        state = cart_service.add_item(code, qty, cart_version=0)
        self.assertTrue(state["cart"]["name"])
        return state

    def _snapshot_count(self):
        return frappe.db.count("BZ Cart Snapshot", {"source_user": "Administrator"})

    def test_snapshot_created_before_clear(self):
        state = self._cart_with(ITEM_A, 2)
        version = state["cart"]["version"]
        uom = state["cart"]["items"][0]["uom"]

        result = cart_service.clear_cart(cart_version=version)

        self.assertEqual(result["cart"]["item_count"], 0)
        self.assertIn("undo", result)
        snapshot = frappe.get_doc("BZ Cart Snapshot", result["undo"]["snapshot"])
        self.assertEqual(snapshot.reason, "Clear")
        self.assertEqual(snapshot.scope, "Cart")
        self.assertEqual(snapshot.source_version, version)
        self.assertEqual(snapshot.status, "Available")
        self.assertEqual(snapshot.item_count, 1)
        self.assertEqual(float(snapshot.total_units), 2.0)
        rows = json.loads(snapshot.items_json)
        self.assertEqual(
            rows,
            [{"item_code": ITEM_A, "qty": 2.0, "uom": uom}],
        )
        for row in rows:
            self.assertEqual(set(row), {"item_code", "qty", "uom"})

    def test_snapshot_created_before_remove_line(self):
        state = self._cart_with(ITEM_A, 1)
        cart_service.add_item(ITEM_B, 1, cart_version=state["cart"]["version"])
        state = cart_service.get_cart()
        version = state["cart"]["version"]
        uom = next(
            item["uom"] for item in state["cart"]["items"] if item["item_code"] == ITEM_A
        )

        result = cart_service.remove_item(ITEM_A, cart_version=version)

        snapshot = frappe.get_doc("BZ Cart Snapshot", result["undo"]["snapshot"])
        self.assertEqual(snapshot.reason, "Remove")
        self.assertEqual(snapshot.scope, "Line")
        self.assertEqual(snapshot.source_version, version)
        self.assertEqual(snapshot.item_count, 1)
        self.assertEqual(
            json.loads(snapshot.items_json),
            [{"item_code": ITEM_A, "qty": 1.0, "uom": uom}],
        )
        self.assertEqual(
            [item["item_code"] for item in result["cart"]["items"]], [ITEM_B]
        )

    def test_clear_empty_cart_creates_no_snapshot(self):
        now = frappe.utils.now_datetime()
        cart = frappe.get_doc(
            {
                "doctype": "BZ Cart",
                "user": "Administrator",
                "status": "Active",
                "version": 1,
                "active_key": "Administrator",
                "last_activity_on": now,
                "expires_on": frappe.utils.add_days(now, 30),
                "items": [],
            }
        ).insert(ignore_permissions=True)

        result = cart_service.clear_cart(cart_version=cart.version)

        self.assertNotIn("undo", result)
        self.assertEqual(self._snapshot_count(), 0)

    def test_snapshot_failure_aborts_clear_and_remove(self):
        state = self._cart_with(ITEM_A, 2)
        version = state["cart"]["version"]
        cart_name = state["cart"]["name"]

        with patch.object(
            cart_service, "_insert_snapshot", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                cart_service.clear_cart(cart_version=version)
        cart = frappe.get_doc("BZ Cart", cart_name)
        self.assertEqual([row.item_code for row in cart.items], [ITEM_A])
        self.assertEqual(cart.version, version)

        with patch.object(
            cart_service, "_insert_snapshot", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                cart_service.remove_item(ITEM_A, cart_version=version)
        cart = frappe.get_doc("BZ Cart", cart_name)
        self.assertEqual([row.item_code for row in cart.items], [ITEM_A])
        self.assertEqual(cart.version, version)

    def test_snapshot_key_idempotent_on_retry(self):
        state = self._cart_with(ITEM_A, 2)
        version = state["cart"]["version"]

        first = cart_service.clear_cart(cart_version=version)
        second = cart_service.clear_cart(cart_version=version)

        self.assertEqual(first["undo"]["snapshot"], second["undo"]["snapshot"])
        self.assertEqual(self._snapshot_count(), 1)

    def test_clear_retry_replays_instead_of_409(self):
        state = self._cart_with(ITEM_A, 1)
        version = state["cart"]["version"]

        first = cart_service.clear_cart(cart_version=version)
        # The retry uses the stale version; the replay branch must run before
        # _check_version, so it returns 200-equivalent data, not a conflict.
        second = cart_service.clear_cart(cart_version=version)

        self.assertEqual(first["undo"]["snapshot"], second["undo"]["snapshot"])
        self.assertEqual(second["cart"]["version"], version + 1)
        self.assertEqual(second["cart"]["item_count"], 0)

    def test_undo_restores_cleared_cart(self):
        state = self._cart_with(ITEM_A, 2)
        version = state["cart"]["version"]
        cleared = cart_service.clear_cart(cart_version=version)

        result = cart_service.undo(
            cleared["undo"]["snapshot"],
            "a" * 64,
            cart_version=cleared["cart"]["version"],
        )

        self.assertFalse(result["already_restored"])
        self.assertEqual(result["applied_mode"], "Replace")
        self.assertEqual(result["restored_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(
            [item["item_code"] for item in result["cart"]["items"]], [ITEM_A]
        )
        self.assertEqual(result["cart"]["version"], version + 2)
        snapshot = frappe.get_doc("BZ Cart Snapshot", cleared["undo"]["snapshot"])
        self.assertEqual(snapshot.status, "Restored")
        self.assertEqual(snapshot.restored_by, "Administrator")
        self.assertIsNotNone(snapshot.restored_on)
        self.assertEqual(snapshot.restored_count, 1)

    def test_undo_restores_removed_line(self):
        state = self._cart_with(ITEM_A, 1)
        cart_service.add_item(ITEM_B, 1, cart_version=state["cart"]["version"])
        state = cart_service.get_cart()
        version = state["cart"]["version"]
        removed = cart_service.remove_item(ITEM_A, cart_version=version)

        result = cart_service.undo(
            removed["undo"]["snapshot"],
            "b" * 64,
            cart_version=removed["cart"]["version"],
        )

        self.assertEqual(result["applied_mode"], "Merge")
        self.assertEqual(
            sorted(item["item_code"] for item in result["cart"]["items"]),
            sorted([ITEM_A, ITEM_B]),
        )

    def test_undo_line_never_wipes_other_items(self):
        state = self._cart_with(ITEM_A, 1)
        cart_service.add_item(ITEM_B, 1, cart_version=state["cart"]["version"])
        state = cart_service.get_cart()
        removed = cart_service.remove_item(
            ITEM_A, cart_version=state["cart"]["version"]
        )
        # Add a third item after the removal; undo must keep it.
        state = cart_service.get_cart()
        cart_service.add_item(ITEM_A, 1, cart_version=state["cart"]["version"])
        current = cart_service.get_cart()

        result = cart_service.undo(
            removed["undo"]["snapshot"],
            "c" * 64,
            cart_version=current["cart"]["version"],
        )

        codes = sorted(item["item_code"] for item in result["cart"]["items"])
        self.assertEqual(codes, sorted([ITEM_A, ITEM_B]))
        quantities = {
            item["item_code"]: item["qty"] for item in result["cart"]["items"]
        }
        self.assertEqual(quantities[ITEM_A], 2.0)

    def test_undo_expired_snapshot_returns_422(self):
        state = self._cart_with(ITEM_A, 1)
        cleared = cart_service.clear_cart(cart_version=state["cart"]["version"])
        frappe.db.set_value(
            "BZ Cart Snapshot",
            cleared["undo"]["snapshot"],
            "expires_on",
            frappe.utils.add_days(frappe.utils.now_datetime(), -1),
            update_modified=False,
        )

        with self.assertRaises(cart_service.CartError) as ctx:
            cart_service.undo(
                cleared["undo"]["snapshot"],
                "d" * 64,
                cart_version=cleared["cart"]["version"],
            )
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.error_code, "SNAPSHOT_EXPIRED")

    def test_undo_idempotent_same_token(self):
        state = self._cart_with(ITEM_A, 1)
        cleared = cart_service.clear_cart(cart_version=state["cart"]["version"])
        first = cart_service.undo(
            cleared["undo"]["snapshot"],
            "e" * 64,
            cart_version=cleared["cart"]["version"],
        )
        second = cart_service.undo(
            cleared["undo"]["snapshot"],
            "e" * 64,
            cart_version=cleared["cart"]["version"],
        )

        self.assertFalse(first["already_restored"])
        self.assertTrue(second["already_restored"])
        self.assertEqual(second["restored_count"], first["restored_count"])
        self.assertEqual(second["cart"]["version"], first["cart"]["version"])

    def test_undo_version_conflict_returns_merge_offer(self):
        state = self._cart_with(ITEM_A, 1)
        cleared = cart_service.clear_cart(cart_version=state["cart"]["version"])
        stale_version = cleared["cart"]["version"]
        cart_service.add_item(ITEM_B, 1, cart_version=stale_version)

        with self.assertRaises(cart_service.CartError) as ctx:
            cart_service.undo(
                cleared["undo"]["snapshot"],
                "f" * 64,
                cart_version=stale_version,
            )
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.error_code, "CART_VERSION_CONFLICT")
        self.assertTrue(ctx.exception.extra.get("merge_available"))

    def test_undo_rejects_processing_checkout_receipt(self):
        state = self._cart_with(ITEM_A, 1)
        cart_name = state["cart"]["name"]
        cleared = cart_service.clear_cart(cart_version=state["cart"]["version"])
        frappe.get_doc(
            {
                "doctype": "BZ Checkout Receipt",
                "checkout_token": "9" * 64,
                "user": "Administrator",
                "cart": cart_name,
                "status": "Processing",
                "cart_version": state["cart"]["version"],
            }
        ).insert(ignore_permissions=True)

        with self.assertRaises(cart_service.CartError) as ctx:
            cart_service.undo(
                cleared["undo"]["snapshot"],
                "9" * 64,
                cart_version=cleared["cart"]["version"],
            )

        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.error_code, "CHECKOUT_IN_PROGRESS")
        snapshot = frappe.get_doc("BZ Cart Snapshot", cleared["undo"]["snapshot"])
        self.assertEqual(snapshot.status, "Available")
        self.assertIsNone(snapshot.restore_token_hash)

    def test_undo_skips_disabled_item_and_restores_rest(self):
        state = self._cart_with(ITEM_A, 1)
        cart_service.add_item(ITEM_B, 1, cart_version=state["cart"]["version"])
        state = cart_service.get_cart()
        cleared = cart_service.clear_cart(cart_version=state["cart"]["version"])
        frappe.db.set_value(
            "Item", ITEM_B, "disabled", 1, update_modified=False
        )

        result = cart_service.undo(
            cleared["undo"]["snapshot"],
            "1" * 64,
            cart_version=cleared["cart"]["version"],
        )

        self.assertEqual(result["restored_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["skipped"][0]["item_code"], ITEM_B)
        self.assertEqual(result["skipped"][0]["reason"], "ITEM_DISABLED")
        self.assertEqual(
            [item["item_code"] for item in result["cart"]["items"]], [ITEM_A]
        )

    def test_undo_all_skipped_returns_422_keeps_snapshot_available(self):
        state = self._cart_with(ITEM_A, 1)
        cleared = cart_service.clear_cart(cart_version=state["cart"]["version"])
        frappe.db.set_value("Item", ITEM_A, "disabled", 1, update_modified=False)

        with self.assertRaises(cart_service.CartError) as ctx:
            cart_service.undo(
                cleared["undo"]["snapshot"],
                "2" * 64,
                cart_version=cleared["cart"]["version"],
            )
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.error_code, "UNDO_NOTHING_RESTORED")
        snapshot = frappe.get_doc("BZ Cart Snapshot", cleared["undo"]["snapshot"])
        self.assertEqual(snapshot.status, "Available")
        self.assertIsNone(snapshot.restore_token_hash)

    def test_cart_get_include_undo_flag_and_source_cart_rule(self):
        # Decision 2026-11-07: the backend flag/rule is kept for P2, but the
        # /cart page no longer requests include_undo and shows no load toast.
        state = self._cart_with(ITEM_A, 1)
        cleared = cart_service.clear_cart(cart_version=state["cart"]["version"])

        without = cart_service.get_cart(include_undo=False)
        with_undo = cart_service.get_cart(include_undo=True)

        self.assertNotIn("undo", without)
        self.assertEqual(with_undo["undo"]["snapshot"], cleared["undo"]["snapshot"])

    def test_flag_off_undo_404_and_no_undo_query(self):
        state = self._cart_with(ITEM_A, 1)
        cleared = cart_service.clear_cart(cart_version=state["cart"]["version"])

        with patch.object(cart_service, "snapshots_enabled", return_value=False):
            self.assertNotIn("undo", cart_service.get_cart(include_undo=True))
            with self.assertRaises(cart_service.CartError) as ctx:
                cart_service.undo(
                    cleared["undo"]["snapshot"],
                    "3" * 64,
                    cart_version=cleared["cart"]["version"],
                )
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.error_code, "FEATURE_DISABLED")

    def test_restore_token_unique_allows_null(self):
        state = self._cart_with(ITEM_A, 1)
        first = cart_service.clear_cart(cart_version=state["cart"]["version"])
        cart_service.add_item(
            ITEM_A, 1, cart_version=first["cart"]["version"]
        )
        second = cart_service.clear_cart(
            cart_version=cart_service.get_cart()["cart"]["version"]
        )

        self.assertNotEqual(
            first["undo"]["snapshot"], second["undo"]["snapshot"]
        )
        for name in (first["undo"]["snapshot"], second["undo"]["snapshot"]):
            snapshot = frappe.get_doc("BZ Cart Snapshot", name)
            self.assertIsNone(snapshot.restore_token_hash)

    def test_purge_snapshots_permanently_no_deleted_document(self):
        from biozone_web import tasks

        state = self._cart_with(ITEM_A, 1)
        cleared = cart_service.clear_cart(cart_version=state["cart"]["version"])
        name = cleared["undo"]["snapshot"]
        frappe.db.set_value(
            "BZ Cart Snapshot",
            name,
            "expires_on",
            frappe.utils.add_days(frappe.utils.now_datetime(), -1),
            update_modified=False,
        )
        deleted_documents = frappe.db.count(
            "Deleted Document", {"deleted_doctype": "BZ Cart Snapshot"}
        )

        deleted = tasks.purge_cart_snapshots()

        self.assertEqual(deleted, 1)
        self.assertFalse(frappe.db.exists("BZ Cart Snapshot", name))
        self.assertEqual(
            frappe.db.count(
                "Deleted Document", {"deleted_doctype": "BZ Cart Snapshot"}
            ),
            deleted_documents,
        )


if __name__ == "__main__":
    unittest.main()
