import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.utils.jinja import get_jenv

from biozone_web.services import cart_staff, cart as cart_service
from biozone_web import tasks

TEST_EMAIL = "test-cart-staff@example.com"
TEST_NAME = "Test Cart Staff"
VIEWER_ROLE = "BZ Cart Viewer"


class TestCartStaffHelpers(unittest.TestCase):
    def test_csv_formula_injection_is_neutralized(self):
        for value in ("=1+1", "+1+1", "-1+1", "@SUM(A1)"):
            self.assertEqual(cart_staff._csv_safe(value), "'" + value)
        self.assertEqual(cart_staff._csv_safe("Biozone"), "Biozone")
        self.assertEqual(cart_staff._csv_safe(None), "")

    def test_copy_default_hides_customer_and_prices(self):
        state = {
            "cart": {"name": "C1", "status": "Active", "last_activity_on": "2026-01-01", "customer": "CUST-1"},
            "customer": {"customer_name": "Secret Customer", "email": "secret@example.com"},
            "quote": {
                "items": [
                    {
                        "item_code": "ITEM-1",
                        "item_name": "Item One",
                        "qty": 2,
                        "uom": "Nos",
                        "price": 10,
                        "line_total": 20,
                        "available": True,
                        "issues": [],
                    }
                ],
                "net_total": 20,
                "currency": "EGP",
            },
        }
        text = "\n".join(cart_staff._copy_lines(state, include_customer=False, include_prices=False))
        self.assertNotIn("Secret Customer", text)
        self.assertNotIn("secret@example.com", text)
        self.assertNotIn("10.00", text)
        self.assertIn("Item One", text)

        text = "\n".join(cart_staff._copy_lines(state, include_customer=True, include_prices=True))
        self.assertIn("Secret Customer", text)
        self.assertIn("10.00", text)


class CartStaffDBCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not frappe.db.table_exists("BZ Cart") or not frappe.db.table_exists("BZ Cart Access Log"):
            raise unittest.SkipTest("BZ Cart staff tables absent — run migrate first")

        if not frappe.db.exists("Role", VIEWER_ROLE):
            frappe.get_doc({"doctype": "Role", "role_name": VIEWER_ROLE, "desk_access": 1}).insert(
                ignore_permissions=True
            )
        if not frappe.db.exists("User", TEST_EMAIL):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": TEST_EMAIL,
                    "first_name": "Test",
                    "last_name": "Cart Staff",
                    "send_welcome_email": 0,
                }
            ).insert(ignore_permissions=True)
        user = frappe.get_doc("User", TEST_EMAIL)
        if VIEWER_ROLE not in [r.role for r in user.roles]:
            user.add_roles(VIEWER_ROLE)
        cls.customer = frappe.get_all("Customer", pluck="name", limit=1)
        if not cls.customer:
            raise unittest.SkipTest("no Customer for cart staff tests")
        cls.customer = cls.customer[0]
        frappe.db.commit()

    def setUp(self):
        self._prev_user = frappe.session.user
        self._prev_form_dict = getattr(frappe.local, "form_dict", None)
        frappe.set_user(TEST_EMAIL)
        self._staff_patch = patch.object(cart_staff, "require_staff_access", lambda: TEST_EMAIL)
        self._staff_patch.start()
        self.cart = self._make_cart()

    def tearDown(self):
        self._staff_patch.stop()
        frappe.set_user(self._prev_user)
        frappe.local.form_dict = self._prev_form_dict
        frappe.db.rollback()

    def _make_cart(self, *, status="Active", active_key=None, expires_on=None, converted_on=None):
        now = frappe.utils.now_datetime()
        doc = frappe.get_doc(
            {
                "doctype": "BZ Cart",
                "user": TEST_EMAIL,
                "customer": self.customer,
                "status": status,
                "version": 0,
                "active_key": active_key,
                "last_activity_on": now,
                "expires_on": expires_on or frappe.utils.add_days(now, 30),
                "converted_on": converted_on,
            }
        )
        doc.insert(ignore_permissions=True)
        return doc

    def test_viewer_role_required(self):
        frappe.set_user("Administrator")
        with patch.object(cart_staff, "require_staff_access", lambda: "Administrator"):
            self.assertEqual(cart_staff.require_cart_viewer(), "Administrator")

        frappe.set_user(TEST_EMAIL)
        with patch.object(cart_staff, "require_staff_access", lambda: None):
            with patch.object(cart_staff.frappe, "get_roles", lambda user=None: ["All"]):
                with self.assertRaises(frappe.PermissionError):
                    cart_staff.require_cart_viewer()

    def test_detail_logs_view_and_copy_csv_log_actions(self):
        state = cart_staff.get_cart_detail(self.cart.name)
        self.assertEqual(state["cart"]["name"], self.cart.name)
        self.assertEqual(state["customer"]["name"], self.customer)
        logs = frappe.get_all(
            "BZ Cart Access Log",
            filters={"cart": self.cart.name, "action": "View"},
            pluck="name",
        )
        self.assertEqual(len(logs), 1)

        copied = cart_staff.copy_cart(self.cart.name)
        self.assertIn(self.cart.name, copied["text"])
        self.assertNotIn(state["customer"]["customer_name"], copied["text"])
        self.assertEqual(
            len(frappe.get_all("BZ Cart Access Log", filters={"cart": self.cart.name, "action": "Copy"})),
            1,
        )

        exported = cart_staff.csv_cart(
            self.cart.name,
            include_customer=True,
            include_prices=True,
        )
        self.assertTrue(exported["csv"].startswith("\ufeff"))
        self.assertIn("customer_name", exported["csv"].splitlines()[0])

        copied_with_customer = cart_staff.copy_cart(self.cart.name, include_customer=True)
        self.assertIn(state["customer"]["customer_name"], copied_with_customer["text"])
        self.assertEqual(
            len(frappe.get_all("BZ Cart Access Log", filters={"cart": self.cart.name, "action": "CSV"})),
            1,
        )

    def test_recheck_is_read_only(self):
        before = frappe.get_doc("BZ Cart", self.cart.name)
        result = cart_staff.recheck_cart(self.cart.name)
        after = frappe.get_doc("BZ Cart", self.cart.name)
        self.assertTrue(result["quote_fingerprint"])
        self.assertEqual(int(before.version or 0), int(after.version or 0))
        self.assertEqual(before.modified, after.modified)

    def test_detail_page_template_renders(self):
        frappe.local.form_dict = frappe._dict({"cart_name": self.cart.name})
        path = frappe.get_app_path("biozone_web", "www", "staff", "cart_detail.py")
        spec = importlib.util.spec_from_file_location("bz_cart_detail_page_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        context = frappe._dict()
        module.get_context(context)
        template_path = (
            Path(frappe.get_app_path("biozone_web", "www", "staff")) / "cart-detail.html"
        )
        html = get_jenv().from_string(open(template_path).read()).render(**context)
        self.assertIn("تفاصيل السلة", html)
        self.assertIn(self.cart.name, html)

    def test_list_page_accepts_all_tab(self):
        frappe.local.form_dict = frappe._dict({"st": "All"})
        path = frappe.get_app_path("biozone_web", "www", "staff", "carts.py")
        spec = importlib.util.spec_from_file_location("bz_carts_page_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        context = frappe._dict()
        module.get_context(context)
        self.assertEqual(context.st, "All")
        self.assertEqual(context.counts["All"], context.total)

    def test_list_filters_status_and_search(self):
        data = cart_staff.list_carts(status="Active", q=self.cart.name)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["carts"][0]["name"], self.cart.name)
        self.assertEqual(data["counts"].get("Active"), 1)

    def test_expire_cart_clears_active_key_and_bumps_version(self):
        cart = self._make_cart(active_key=TEST_EMAIL, expires_on=frappe.utils.add_days(frappe.utils.now_datetime(), -1))
        self.assertTrue(cart_service.cart_is_expired(cart))
        self.assertTrue(cart_service.expire_cart(cart))
        reloaded = frappe.get_doc("BZ Cart", cart.name)
        self.assertEqual(reloaded.status, "Expired")
        self.assertFalse(reloaded.active_key)
        self.assertEqual(int(reloaded.version or 0), 1)

    def test_get_active_cart_lazily_expires(self):
        cart = self._make_cart(active_key=TEST_EMAIL, expires_on=frappe.utils.add_days(frappe.utils.now_datetime(), -1))
        self.assertIsNone(cart_service.get_active_cart(TEST_EMAIL))
        self.assertEqual(frappe.db.get_value("BZ Cart", cart.name, "status"), "Expired")

    def test_expire_carts_task(self):
        cart = self._make_cart(active_key=TEST_EMAIL, expires_on=frappe.utils.add_days(frappe.utils.now_datetime(), -1))
        expired = tasks.expire_carts()
        self.assertGreaterEqual(expired, 1)
        self.assertEqual(frappe.db.get_value("BZ Cart", cart.name, "status"), "Expired")

    def test_retention_purges_old_converted_cart_and_keeps_recent(self):
        old = self._make_cart(status="Converted", converted_on=frappe.utils.add_days(frappe.utils.now_datetime(), -8))
        recent = self._make_cart(status="Converted", converted_on=frappe.utils.add_days(frappe.utils.now_datetime(), -1))
        cutoff = frappe.utils.add_days(frappe.utils.now_datetime(), -tasks.RETENTION_DAYS)
        deleted = tasks._purge_converted_carts(cutoff)
        self.assertGreaterEqual(deleted, 1)
        self.assertFalse(frappe.db.exists("BZ Cart", old.name))
        self.assertTrue(frappe.db.exists("BZ Cart", recent.name))

    def test_retention_purges_old_access_log_only(self):
        old = frappe.get_doc(
            {
                "doctype": "BZ Cart Access Log",
                "staff_user": TEST_EMAIL,
                "action": "View",
                "cart": self.cart.name,
            }
        ).insert(ignore_permissions=True)
        frappe.db.set_value(
            "BZ Cart Access Log",
            old.name,
            "creation",
            frappe.utils.add_days(frappe.utils.now_datetime(), -8),
        )
        cutoff = frappe.utils.add_days(frappe.utils.now_datetime(), -tasks.RETENTION_DAYS)
        tasks._purge_access_logs(cutoff)
        self.assertFalse(frappe.db.exists("BZ Cart Access Log", old.name))


if __name__ == "__main__":
    unittest.main()
