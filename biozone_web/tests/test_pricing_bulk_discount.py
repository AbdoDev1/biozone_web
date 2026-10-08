"""Direct tests for bulk item discounts + order-to-staff flow (site DB).

Requires a bench site database (``bench --site <site> run-tests
--app biozone_web``). Loud skips when prerequisites are absent; every
mutation restored in tearDown with fixed ``TST-`` / example.com
identities — no production data, no orphans.

Covers: single path unchanged, bulk multi-ok, bulk partial failure,
zero disables, guard wiring, no cross-group leakage, and a customer
order reaching staff prep as a draft.
"""

import unittest
from unittest.mock import patch

import frappe

from biozone_web import api as biozone_api
from biozone_web import utils as biozone_utils

TEST_DOMAIN = "example.com"


def _mail(tag):
    return f"test-disc-{tag}@{TEST_DOMAIN}"


def _site_ready():
    try:
        frappe.db.get_value("Customer Group", "x", "name")
        return True
    except Exception:
        return False


def _public_group():
    return biozone_utils.get_public_customer_group()


class DiscountBulkSiteCase(unittest.TestCase):
    def setUp(self):
        if not _site_ready():
            self.skipTest("no site database available")
        self._rules = []
        self._items = []
        self._groups = []
        self._customers = []
        self._users = []
        self._orders = []

    def tearDown(self):
        for name in self._orders:
            try:
                if frappe.db.exists("Sales Order", name):
                    frappe.delete_doc("Sales Order", name, ignore_permissions=True, force=True)
            except Exception:
                pass
        for name in self._customers:
            try:
                if frappe.db.exists("Customer", name):
                    frappe.delete_doc("Customer", name, ignore_permissions=True, force=True)
            except Exception:
                pass
        for email in self._users:
            try:
                if frappe.db.exists("User", email):
                    frappe.delete_doc("User", email, ignore_permissions=True)
            except Exception:
                pass
        for name in self._rules:
            try:
                if frappe.db.exists("Pricing Rule", name):
                    frappe.delete_doc("Pricing Rule", name, ignore_permissions=True, force=True)
            except Exception:
                pass
        for name in self._items:
            try:
                if frappe.db.exists("Item", name):
                    frappe.delete_doc("Item", name, ignore_permissions=True, force=True)
            except Exception:
                pass
        for name in self._groups:
            try:
                if frappe.db.exists("Customer Group", name):
                    frappe.delete_doc("Customer Group", name, ignore_permissions=True)
            except Exception:
                pass
        frappe.db.commit()

    # -- helpers ------------------------------------------------------
    def _make_group(self, tag):
        name = f"TST-DISC-{tag}"
        if not frappe.db.exists("Customer Group", name):
            doc = frappe.get_doc(
                {"doctype": "Customer Group", "customer_group_name": name, "is_group": 0}
            )
            doc.insert(ignore_permissions=True)
            self._groups.append(name)
        return name

    def _make_item(self, tag):
        code = f"TST-DISC-ITEM-{tag}"
        if not frappe.db.exists("Item", code):
            stock_uom = frappe.db.get_value(
                "UOM", {"enabled": 1}, "name"
            ) or frappe.db.get_value("UOM", {}, "name")
            if not stock_uom:
                self.skipTest("no UOM available")
            item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
            if not item_group:
                self.skipTest("no leaf Item Group available")
            doc = frappe.get_doc(
                {
                    "doctype": "Item",
                    "item_code": code,
                    "item_name": f"TST item {tag}",
                    "item_group": item_group,
                    "stock_uom": stock_uom,
                    "is_stock_item": 0,
                }
            )
            doc.insert(ignore_permissions=True)
            self._items.append(code)
        return code

    def _make_customer(self, tag, group):
        email = _mail(tag)
        if not frappe.db.exists("User", email):
            user = frappe.get_doc(
                {
                    "doctype": "User",
                    "email": email,
                    "first_name": "TST",
                    "last_name": tag,
                    "send_welcome_email": 0,
                }
            )
            user.insert(ignore_permissions=True)
            self._users.append(email)
            frappe.db.commit()
        name = biozone_utils.create_customer_for_user(email, full_name=f"TST {tag}")
        self._customers.append(name)
        frappe.db.set_value("Customer", name, "customer_group", group)
        frappe.db.commit()
        return name, email

    def _guard_off(self):
        return patch.object(biozone_utils, "require_staff_access", lambda: None)

    # -- single path unchanged -----------------------------------------
    def test_single_still_works(self):
        group = self._make_group("G1")
        code = self._make_item("I1")
        with self._guard_off():
            res = biozone_api.staff_set_item_discount(code, group, 12.5)
        self.assertTrue(res.get("ok"))
        self._rules.append(res["pricing_rule"])
        self.assertEqual(float(res["discount_percent"]), 12.5)

    # -- bulk multi-ok ---------------------------------------------------
    def test_bulk_multi_ok(self):
        group = self._make_group("G2")
        codes = [self._make_item("B1"), self._make_item("B2")]
        with self._guard_off():
            res = biozone_api.staff_set_item_discount(
                customer_group=group,
                updates=[
                    {"item_code": codes[0], "discount_percent": 10},
                    {"item_code": codes[1], "discount_percent": 20},
                ],
            )
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("updated"), 2)
        self.assertEqual(res.get("failed"), 0)
        for r in res["results"]:
            self._rules.append(r["pricing_rule"])
        got = {}
        for r in res["results"]:
            doc = frappe.get_doc("Pricing Rule", r["pricing_rule"])
            self.assertEqual(doc.customer_group, group)
            self.assertFalse(doc.disable)
            self.assertIn(
                r["item_code"], [row.item_code for row in (doc.items or [])]
            )
            got[r["item_code"]] = float(doc.discount_percentage)
        self.assertEqual(got.get(codes[0]), 10.0)
        self.assertEqual(got.get(codes[1]), 20.0)

    # -- bulk partial failure ---------------------------------------------
    def test_bulk_partial_failure(self):
        group = self._make_group("G3")
        code = self._make_item("P1")
        with self._guard_off():
            res = biozone_api.staff_set_item_discount(
                customer_group=group,
                updates=[
                    {"item_code": code, "discount_percent": 5},
                    {"item_code": "__no_such_item__", "discount_percent": 5},
                    {"item_code": code, "discount_percent": 150},
                ],
            )
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("updated"), 1)
        self.assertEqual(res.get("failed"), 2)
        for r in res["results"]:
            if r.get("ok"):
                self._rules.append(r["pricing_rule"])
        errs = [r.get("error") for r in res["results"] if not r.get("ok")]
        self.assertTrue(all(errs))

    # -- zero disables ------------------------------------------------------
    def test_bulk_zero_disables(self):
        group = self._make_group("G4")
        code = self._make_item("Z1")
        with self._guard_off():
            first = biozone_api.staff_set_item_discount(code, group, 15)
            self.assertTrue(first.get("ok"))
            self._rules.append(first["pricing_rule"])
            res = biozone_api.staff_set_item_discount(
                customer_group=group, updates=[{"item_code": code, "discount_percent": 0}]
            )
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("updated"), 1)
        self.assertEqual(
            frappe.db.get_value("Pricing Rule", first["pricing_rule"], "disable"), 1
        )

    # -- guard wiring ---------------------------------------------------------
    def test_bulk_unauthorized_rejected(self):
        group = self._make_group("G5")
        code = self._make_item("U1")
        with patch.object(
            biozone_utils,
            "require_staff_access",
            side_effect=frappe.PermissionError("denied"),
        ):
            with self.assertRaises(frappe.PermissionError):
                biozone_api.staff_set_item_discount(
                    customer_group=group,
                    updates=[{"item_code": code, "discount_percent": 5}],
                )
        self.assertEqual(
            frappe.db.count(
                "Pricing Rule",
                {"title": ["like", f"{code} - {group}"]},
            ),
            0,
        )

    # -- no cross-group leakage -----------------------------------------------
    def test_no_leak_across_groups(self):
        group_a = self._make_group("GA")
        group_b = self._make_group("GB")
        code = self._make_item("L1")
        with self._guard_off():
            res = biozone_api.staff_set_item_discount(code, group_a, 25)
        self.assertTrue(res.get("ok"))
        self._rules.append(res["pricing_rule"])
        doc = frappe.get_doc("Pricing Rule", res["pricing_rule"])
        self.assertEqual(doc.customer_group, group_a)
        self.assertEqual(doc.applicable_for, "Customer Group")
        # No blank-group rule may carry our title pattern.
        self.assertEqual(
            frappe.db.count(
                "Pricing Rule",
                {"title": ["like", f"{code} - %"], "customer_group": ["in", ["", None]]},
            ),
            0,
        )
        # Group B sees no discount for the same item.
        rows = frappe.db.sql(
            """
            select pr.discount_percentage
            from `tabPricing Rule Item Code` pri
            inner join `tabPricing Rule` pr on pr.name = pri.parent
            where pr.apply_on = 'Item Code' and pr.disable = 0
                and pr.customer_group = %(g)s and pri.item_code = %(c)s
            """,
            {"g": group_b, "c": code},
            as_dict=True,
        )
        self.assertEqual(rows, [])

    # -- toggle untouched ----------------------------------------------------
    def test_toggle_still_works(self):
        group = self._make_group("TG")
        with self._guard_off():
            off = biozone_api.staff_toggle_customer_group(group, 1)
            self.assertTrue(off.get("ok"))
            self.assertEqual(frappe.db.get_value("Customer Group", group, "disabled"), 1)
            on = biozone_api.staff_toggle_customer_group(group, 0)
            self.assertTrue(on.get("ok"))
            self.assertEqual(frappe.db.get_value("Customer Group", group, "disabled"), 0)

    # -- order from customer reaches staff --------------------------------------
    def test_customer_order_reaches_staff_prep(self):
        public = _public_group()
        if not public:
            self.skipTest("no public customer group configured")
        from biozone_web.units import resolve_items_display

        codes = [
            r.item_code
            for r in frappe.get_all(
                "Item", filters={"disabled": 0}, fields=["item_code"], limit_page_length=50
            )
        ]
        display = resolve_items_display(codes, public)
        usable = [c for c in codes if (display.get(c) or {}).get("ok")]
        if not usable:
            self.skipTest("no displayable item for public group")
        prices = biozone_utils.get_effective_item_prices(
            usable, customer=None, customer_group=public
        )
        priced = [c for c in usable if (prices.get(c) or {}).get("price")]
        if not priced:
            self.skipTest("no priced item for public group")
        item_code = priced[0]

        _name, email = self._make_customer("ord", public)
        prev_user = frappe.session.user
        try:
            frappe.set_user(email)
            res = biozone_api.biozone_confirm_order(items=[{"item_code": item_code, "quantity": 1}])
        finally:
            frappe.set_user(prev_user)
        self.assertTrue(res.get("ok"), res)
        so_name = res["sales_order"]
        self._orders.append(so_name)
        so = frappe.get_doc("Sales Order", so_name)
        self.assertEqual(so.docstatus, 0)
        self.assertEqual(so.customer_group, public)
        # Staff-visible as a draft for prep.
        with self._guard_off():
            got, err = biozone_api._b9_get_draft_order(so_name)
        self.assertIsNone(err)
        self.assertEqual(got.name, so_name)


if __name__ == "__main__":
    unittest.main()
