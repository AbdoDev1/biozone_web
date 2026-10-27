"""قفل إنهاء السلة وعزل فشل كل سلة في المهمة الدورية.

هذان الاختباران يوثّقان العقد المطلوب قبل الإصلاح:
- `expire_cart` يقرأ الصف بقفل (`FOR UPDATE`) فلا يكتب فوق تعديل متزامن.
- `expire_carts` يستمر بعد فشل سلة واحدة ويسجّل الخطأ.

كل الاختبارات تعمل داخل معاملة واحدة وتُرجعها في `tearDown`، ولا
تنشئ مستخدمًا أو عميلًا (السلة تستخدم `Administrator` و`customer` فارغًا).
"""

import unittest
from unittest.mock import patch

import frappe

from biozone_web import tasks
from biozone_web.services import cart as cart_service


class TestCartExpiry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not frappe.db.table_exists("BZ Cart"):
            raise unittest.SkipTest("BZ Cart table absent — run migrate first")

    def setUp(self):
        self._prev_user = frappe.session.user
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.set_user(self._prev_user)
        frappe.db.rollback()

    def _make_expired_cart(self, *, version=3):
        now = frappe.utils.now_datetime()
        return frappe.get_doc(
            {
                "doctype": "BZ Cart",
                "user": "Administrator",
                "status": "Active",
                "version": version,
                "active_key": None,
                "last_activity_on": frappe.utils.add_days(now, -2),
                "expires_on": frappe.utils.add_days(now, -1),
            }
        ).insert(ignore_permissions=True)

    def test_expire_cart_for_update_does_not_overwrite_concurrent_edit(self):
        """تعديل متزامن (version=9) يجب ألا يضيع عند إنهاء السلة."""
        stale = self._make_expired_cart(version=3)
        concurrent_activity = frappe.utils.add_days(frappe.utils.now_datetime(), -1)
        frappe.db.set_value(
            "BZ Cart",
            stale.name,
            {"version": 9, "last_activity_on": concurrent_activity},
            update_modified=False,
        )

        self.assertTrue(cart_service.expire_cart(stale))

        row = frappe.db.get_value(
            "BZ Cart",
            stale.name,
            ["status", "version", "active_key", "last_activity_on"],
            as_dict=True,
        )
        self.assertEqual(row.status, "Expired")
        self.assertIsNone(row.active_key)
        self.assertEqual(row.version, 10)
        self.assertEqual(
            frappe.utils.get_datetime(row.last_activity_on),
            frappe.utils.get_datetime(concurrent_activity),
        )

    def test_expire_carts_continues_after_one_failure(self):
        """فشل سلة واحدة لا يوقف الدفعة، ويُسجَّل الخطأ باسمها."""
        calls = []

        def fake_expire(name):
            calls.append(name)
            if name == "CART-A":
                raise RuntimeError("boom")
            return True

        with (
            patch.object(tasks.frappe, "get_all", return_value=["CART-A", "CART-B"]),
            patch.object(tasks, "expire_cart", side_effect=fake_expire),
            patch.object(tasks.frappe, "log_error") as log_error,
        ):
            expired = tasks.expire_carts()

        self.assertEqual(expired, 1)
        self.assertEqual(calls, ["CART-A", "CART-B"])
        log_error.assert_called_once()
        self.assertIn("CART-A", str(log_error.call_args))
