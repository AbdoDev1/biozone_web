"""Plan §4.13 remaining rules: 10, 25 (second half), 26, 28, 29, 30, 31.

Isolation: Administrator, customer gate patched to None, one transaction
rolled back in tearDown (same policy as test_cart_snapshots).
"""

import hashlib
import inspect
import json
import unittest
from unittest.mock import patch

import frappe

from biozone_web.services import cart as cart_service

ITEM_A = "BZ-11929"
ITEM_B = "BZ-11811"
ITEM_C = "BZ-11010"
TOKEN_10 = "a" * 64
TOKEN_25 = "b" * 64
NO_CUSTOMER_USER = "test-no-customer@example.com"


class _SnapshotBase(unittest.TestCase):
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


class TestUndoCartScope(_SnapshotBase):
    """Test 10: a Cart snapshot never blind-replaces a non-empty cart."""

    def test_undo_cart_replace_requires_empty_cart(self):
        snap, _ = self._snapshot_of([(ITEM_A, 2)])
        state = self._cart_with([(ITEM_B, 1)])

        result = cart_service.undo(
            snap, TOKEN_10, cart_version=state["cart"]["version"]
        )

        self.assertEqual(result["applied_mode"], "Merge")
        rows = {row["item_code"]: row for row in result["cart"]["items"]}
        self.assertEqual(set(rows), {ITEM_A, ITEM_B})
        self.assertEqual(rows[ITEM_A]["qty"], 2)
        self.assertEqual(rows[ITEM_B]["qty"], 1)


class TestPurgeSnapshots(_SnapshotBase):
    """Test 25 (second half): purge keeps recent and Restored snapshots."""

    def test_purge_keeps_recent_and_restored_snapshots(self):
        from biozone_web import tasks

        old_available, _ = self._snapshot_of([(ITEM_A, 1)])
        restored, restored_version = self._snapshot_of([(ITEM_B, 1)])
        cart_service.undo(restored, TOKEN_25, cart_version=restored_version)
        recent, _ = self._snapshot_of([(ITEM_C, 1)])

        past = frappe.utils.add_days(frappe.utils.now_datetime(), -1)
        for name in (old_available, restored):
            frappe.db.set_value(
                "BZ Cart Snapshot", name, "expires_on", past, update_modified=False
            )

        deleted = tasks.purge_cart_snapshots()

        self.assertGreaterEqual(deleted, 1)
        self.assertFalse(frappe.db.exists("BZ Cart Snapshot", old_available))
        self.assertTrue(frappe.db.exists("BZ Cart Snapshot", restored))
        self.assertEqual(
            frappe.db.get_value("BZ Cart Snapshot", restored, "status"), "Restored"
        )
        self.assertTrue(frappe.db.exists("BZ Cart Snapshot", recent))


class TestListAccessLog(_SnapshotBase):
    """Test 26: one Access Log row per list request, with the filters."""

    def test_list_carts_logs_one_entry_with_filters(self):
        from biozone_web.services import cart_staff

        state = self._cart_with([(ITEM_A, 1)])
        cart_name = state["cart"]["name"]
        with patch.object(cart_staff, "get_current_domain_role", lambda: "app"):
            before = frappe.db.count("BZ Cart Access Log")
            data = cart_staff.list_carts(status="Active", q=cart_name)
            after = frappe.db.count("BZ Cart Access Log")

        self.assertEqual(data["total"], 1)
        self.assertEqual(after, before + 1)
        row = frappe.get_all(
            "BZ Cart Access Log",
            filters={"staff_user": "Administrator"},
            fields=["action", "cart", "details", "request_id"],
            order_by="creation desc",
            limit=1,
        )[0]
        self.assertEqual(row.action, "View")
        self.assertIsNone(row.cart)
        self.assertIn("list;", row.details)
        self.assertIn("status=Active", row.details)
        self.assertIn(f"q={cart_name}", row.details)
        self.assertIn("page=1", row.details)


class TestSnapshotSchema(unittest.TestCase):
    """Test 28: no Link fields, read-only System Manager perm, no prices."""

    def test_snapshot_schema_has_no_link_fields_and_readonly_perms(self):
        path = frappe.get_app_path(
            "biozone_web",
            "biozone_web/doctype/bz_cart_snapshot/bz_cart_snapshot.json",
        )
        with open(path, encoding="utf-8") as handle:
            schema = json.load(handle)

        for field in schema["fields"]:
            self.assertNotIn(
                field["fieldtype"],
                ("Link", "Dynamic Link", "Table", "Table MultiSelect"),
                field["fieldname"],
            )

        perms = schema.get("permissions") or []
        self.assertEqual([perm["role"] for perm in perms], ["System Manager"])
        perm = perms[0]
        self.assertEqual(perm.get("read"), 1)
        for action in ("write", "create", "delete", "submit", "cancel", "amend"):
            self.assertFalse(perm.get(action), action)


class TestSnapshotItemsNoPrices(_SnapshotBase):
    def test_snapshot_items_json_has_no_prices(self):
        # Covered end-to-end in test_cart_undo_merge (item 20); this is the
        # schema-level guarantee: only three keys are ever written.
        snap, _ = self._snapshot_of([(ITEM_A, 2)])
        items = json.loads(
            frappe.db.get_value("BZ Cart Snapshot", snap, "items_json")
        )
        self.assertEqual(set(items[0]), {"item_code", "qty", "uom"})


class TestSnapshotFlag(unittest.TestCase):
    """Test 29: absent/off flag disables; on applies without a restart."""

    def test_flag_missing_disables_snapshots(self):
        with patch.object(frappe, "conf", {}):
            self.assertFalse(cart_service.snapshots_enabled())

    def test_flag_off_values_disable_and_on_enables_without_restart(self):
        for value in ("0", "", None, 0):
            with patch.object(frappe, "conf", {"biozone_cart_snapshots_enabled": value}):
                self.assertFalse(cart_service.snapshots_enabled(), value)
        with patch.object(frappe, "conf", {"biozone_cart_snapshots_enabled": 1}):
            self.assertTrue(cart_service.snapshots_enabled())
        # Same process, second read: the value is not cached across calls.
        with patch.object(frappe, "conf", {"biozone_cart_snapshots_enabled": "1"}):
            self.assertTrue(cart_service.snapshots_enabled())


class TestUndoEndpointGuards(unittest.TestCase):
    """Test 30: 429 rate limit, Guest denied, customer missing 422."""

    def setUp(self):
        self._prev_user = frappe.session.user
        self._prev_response = getattr(frappe.local, "response", None)
        if self._prev_response is None:
            frappe.local.response = frappe._dict()

    def tearDown(self):
        frappe.set_user(self._prev_user)
        if self._prev_response is None:
            frappe.local.response = frappe._dict()
        else:
            frappe.local.response = self._prev_response
        frappe.db.rollback()

    def test_undo_rate_limit_returns_429(self):
        from biozone_web import api

        digest = hashlib.sha256(b"Administrator").hexdigest()[:32]
        captured = {}

        def fake_hit(key, ttl):
            captured["key"] = key
            captured["ttl"] = ttl
            return 31  # limit for cart_undo is 30

        with patch("biozone_web.services.rate_limit._hit", side_effect=fake_hit):
            response = api.biozone_cart_undo(
                snapshot="whatever", restore_token="a" * 64, cart_version=0
            )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "RATE_LIMITED")
        self.assertEqual(response["retry_after"], 60)
        self.assertEqual(frappe.local.response["http_status_code"], 429)
        self.assertEqual(captured["key"], f"rl:cart:cart_undo:user:{digest}")
        self.assertEqual(captured["ttl"], 60)

    def test_undo_guest_denied(self):
        from biozone_web import api

        frappe.set_user("Guest")
        with self.assertRaises(frappe.PermissionError):
            frappe.is_whitelisted(api.biozone_cart_undo)
        with self.assertRaises(cart_service.CartError) as ctx:
            cart_service.undo("whatever", "a" * 64, cart_version=0)
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.error_code, "CUSTOMER_MISSING")

    def test_undo_customer_missing_422(self):
        if not frappe.db.exists("User", NO_CUSTOMER_USER):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": NO_CUSTOMER_USER,
                    "first_name": "No",
                    "last_name": "Customer",
                    "send_welcome_email": 0,
                }
            ).insert(ignore_permissions=True)
        frappe.set_user(NO_CUSTOMER_USER)

        with self.assertRaises(cart_service.CartError) as ctx:
            cart_service.undo("whatever", "a" * 64, cart_version=0)
        self.assertEqual(ctx.exception.status, 422)
        self.assertEqual(ctx.exception.error_code, "CUSTOMER_MISSING")


class TestNoIntermediateCommit(unittest.TestCase):
    """Test 31 (second half): destructive paths never commit mid-way."""

    def test_clear_remove_undo_no_intermediate_commit(self):
        for fn in (
            cart_service.clear_cart,
            cart_service.remove_item,
            cart_service.undo,
            cart_service._insert_snapshot,
        ):
            self.assertNotIn(
                "frappe.db.commit",
                inspect.getsource(fn),
                fn.__name__,
            )


class TestUndoToastUI(unittest.TestCase):
    """Decision 2026-11-07: immediate-only undo toast, 12s, no load call."""

    def test_cart_page_has_no_load_undo_and_uses_12s_toast(self):
        path = frappe.get_app_path("biozone_web", "www/cart.html")
        with open(path, encoding="utf-8") as handle:
            html = handle.read()

        self.assertNotIn("includeUndo", html)
        self.assertNotIn("يمكنك التراجع عن آخر تغيير", html)
        self.assertIn("UNDO_TOAST_MS = 12000", html)
        self.assertIn("setTimeout(hideUndoToast, UNDO_TOAST_MS)", html)
        self.assertNotIn("biozone_cart_undo_dismissed", html)
        self.assertIn("#cart-undo-toast { bottom: calc(env(safe-area-inset-bottom) + 88px); }", html)

    def test_undo_result_keeps_restored_count(self):
        """The toast reads restored_count from the store result.

        `_setServerState` rebuilds the public state and drops response-only
        fields, so `CartStore.undo` must copy the counters back before
        returning. Browser check 2026-11-07 showed "تمت استعادة 0 أصناف"
        while the server returned restored_count=1.
        """
        path = frappe.get_app_path("biozone_web", "public/js/cart_store.js")
        with open(path, encoding="utf-8") as handle:
            js = handle.read()

        self.assertIn("state.restored_count = Number(data.restored_count || 0)", js)
        self.assertIn("state.skipped_count = Number(data.skipped_count || 0)", js)


if __name__ == "__main__":
    unittest.main()
