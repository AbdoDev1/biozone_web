"""Plan §4.13 tests 11 and 14: snapshot ownership and token scoping.

Two real users are inserted inside the test transaction (no commit), so the
`user` Link on BZ Cart validates and `frappe.set_user` can switch identity.
Everything is rolled back in tearDown.
"""

import unittest
from unittest.mock import patch

import frappe

from biozone_web.services import cart as cart_service

ITEM_A = "BZ-11929"
ITEM_B = "BZ-11811"
USER_A = "test-undo-a@example.com"
USER_B = "test-undo-b@example.com"
TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


class TestSnapshotOwnership(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not frappe.db.table_exists("BZ Cart Snapshot"):
            raise unittest.SkipTest("BZ Cart Snapshot table absent — run migrate first")

    def setUp(self):
        self._prev_user = frappe.session.user
        self._customer_patch = patch.object(
            cart_service, "require_customer", return_value=None
        )
        self._customer_patch.start()
        self._flag_patch = patch.object(
            cart_service, "snapshots_enabled", return_value=True
        )
        self._flag_patch.start()
        self._ensure_user(USER_A)
        self._ensure_user(USER_B)

    def tearDown(self):
        self._flag_patch.stop()
        self._customer_patch.stop()
        frappe.set_user(self._prev_user)
        frappe.db.rollback()

    def _ensure_user(self, email):
        if not frappe.db.exists("User", email):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": email,
                    "first_name": "Test",
                    "last_name": "Undo",
                    "send_welcome_email": 0,
                }
            ).insert(ignore_permissions=True)

    def _clear_for(self, user, code, qty):
        frappe.set_user(user)
        state = cart_service.add_item(code, qty, cart_version=0)
        cleared = cart_service.clear_cart(cart_version=state["cart"]["version"])
        return cleared["undo"]["snapshot"], cleared["cart"]["version"]

    def test_owner_only_returns_uniform_404(self):
        snap_a, _ = self._clear_for(USER_A, ITEM_A, 2)
        frappe.set_user(USER_B)

        with self.assertRaises(cart_service.CartError) as foreign:
            cart_service.undo(snap_a, TOKEN_B, cart_version=0)
        with self.assertRaises(cart_service.CartError) as missing:
            cart_service.undo("doesnotexist", TOKEN_B, cart_version=0)

        self.assertEqual(foreign.exception.status, 404)
        self.assertEqual(foreign.exception.error_code, "SNAPSHOT_NOT_AVAILABLE")
        self.assertEqual(foreign.exception.status, missing.exception.status)
        self.assertEqual(foreign.exception.error_code, missing.exception.error_code)
        self.assertEqual(foreign.exception.message, missing.exception.message)

    def test_foreign_user_cannot_read_restored_result_with_same_token(self):
        snap_a, version_a = self._clear_for(USER_A, ITEM_A, 2)
        frappe.set_user(USER_A)
        restored = cart_service.undo(snap_a, TOKEN_A, cart_version=version_a)
        self.assertFalse(restored["already_restored"])
        self.assertEqual(restored["restored_count"], 1)

        frappe.set_user(USER_B)
        with self.assertRaises(cart_service.CartError) as ctx:
            cart_service.undo(snap_a, TOKEN_A, cart_version=0)
        self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(ctx.exception.error_code, "SNAPSHOT_NOT_AVAILABLE")
        self.assertNotIn("cart", getattr(ctx.exception, "extra", {}) or {})

    def test_same_token_value_is_scoped_per_user(self):
        snap_a, version_a = self._clear_for(USER_A, ITEM_A, 2)
        frappe.set_user(USER_A)
        cart_service.undo(snap_a, TOKEN_A, cart_version=version_a)

        snap_b, version_b = self._clear_for(USER_B, ITEM_B, 1)
        frappe.set_user(USER_B)
        restored = cart_service.undo(snap_b, TOKEN_A, cart_version=version_b)

        self.assertFalse(restored["already_restored"])
        self.assertEqual(restored["restored_count"], 1)
        self.assertEqual(
            [row["item_code"] for row in restored["cart"]["items"]], [ITEM_B]
        )


if __name__ == "__main__":
    unittest.main()
