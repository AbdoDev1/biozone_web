"""Integration tests (site DB) for the per-group minimum order limit.

Phase-B executable: requires migrate (``min_order_amount`` field present)
plus a site database. Conventions:

- Helper taxonomy + staff-save validation run under ``bench run-tests``
  (deterministic, hermetic; every mutation restored in tearDown — the
  endpoints commit explicitly, so runner rollback alone is NOT relied upon).
- Live confirm-flow tests (T-div / T-round-live / T-calc-fail / T-midflight /
  T-race / T-callbacks / R-confirm-core) use get-or-create fixtures with a
  FIXED test identity (``test-min-order@example.com``) so reruns collide
  with nothing and leave no per-run orphans; created Sales Orders and their
  Notification Log rows are deleted per test. Each live test skips loudly
  (``skipTest`` with reason) when its prerequisite is absent instead of
  failing on environment gaps.
- ``require_staff_access`` is host-bound (staff domain), so endpoint tests
  patch the guard to exercise validation branches only; guard behavior
  itself stays covered by the manual browser matrix (phase B).
"""

import unittest
from unittest.mock import patch

import frappe

from biozone_web import api as biozone_api
from biozone_web import utils as biozone_utils
from biozone_web.services.checkout import build_checkout_quote, quote_contract_fields
from biozone_web.utils import (
    MinOrderConfigError,
    customer_group_has_min_order_field,
    get_group_min_order_amount,
    get_public_customer_group,
)

TEST_EMAIL = "test-min-order@example.com"
TEST_NAME = "Test Min Order"


def _field_present():
    return customer_group_has_min_order_field() is True


def _active_leaf_groups(exclude_public=True):
    public = get_public_customer_group()
    groups = frappe.get_all(
        "Customer Group",
        filters={"is_group": 0, "disabled": 0},
        fields=["name"],
        order_by="name asc",
    )
    names = [g.name for g in groups]
    if exclude_public:
        names = [n for n in names if n != public]
    return names


class MinOrderDBCase(unittest.TestCase):
    """Base: field-guard skip + mutation restore registry."""

    @classmethod
    def setUpClass(cls):
        if not _field_present():
            raise unittest.SkipTest("min_order_amount field absent — run migrate first")

    def setUp(self):
        self._restore = []

    def _set_limit(self, group, value):
        original = frappe.db.get_value("Customer Group", group, "min_order_amount")
        if group not in [g for g, _ in self._restore]:
            self._restore.append((group, original))
        frappe.db.set_value("Customer Group", group, "min_order_amount", value)
        frappe.db.commit()

    def tearDown(self):
        for group, original in self._restore:
            frappe.db.set_value("Customer Group", group, "min_order_amount", original)
        frappe.db.commit()


class TestLimitHelperTaxonomy(MinOrderDBCase):
    """T-gates / T-dbread at helper level (deterministic)."""

    def test_public_group_always_zero(self):
        self.assertEqual(get_group_min_order_amount(get_public_customer_group()), 0.0)

    def test_unknown_and_empty_group_zero(self):
        self.assertEqual(get_group_min_order_amount("NO-SUCH-GROUP-XYZ"), 0.0)
        self.assertEqual(get_group_min_order_amount(""), 0.0)
        self.assertEqual(get_group_min_order_amount(None), 0.0)

    def test_disabled_group_zero(self):
        rows = frappe.get_all(
            "Customer Group",
            filters={"is_group": 0, "disabled": 1},
            fields=["name"],
            limit_page_length=1,
        )
        if not rows:
            self.skipTest("no disabled leaf group in this environment")
        self._set_limit(rows[0].name, 5000)
        self.assertEqual(get_group_min_order_amount(rows[0].name), 0.0)

    def test_roundtrip_active_group(self):
        groups = _active_leaf_groups()
        if not groups:
            self.skipTest("no active non-public leaf group in this environment")
        group = groups[0]
        self._set_limit(group, 5000)
        self.assertEqual(get_group_min_order_amount(group), 5000.0)
        self._set_limit(group, 0)
        self.assertEqual(get_group_min_order_amount(group), 0.0)

    def test_stored_negative_is_config_error(self):
        groups = _active_leaf_groups()
        if not groups:
            self.skipTest("no active non-public leaf group in this environment")
        group = groups[0]
        self._set_limit(group, -5)
        with self.assertRaises(MinOrderConfigError):
            get_group_min_order_amount(group)

    # ملحوظة: فرع القيمة NULL غير قابل للاختبار — العمود NOT NULL مثبت
    # حيًا (IntegrityError 1048 عند محاولة الكتابة)، فيبقى الفرع دفاعيًا
    # في الكود بلا تغطية آلية.


class TestStaffSaveLimit(MinOrderDBCase):
    """staff_save_order_limit validation branches (guard patched — see module docstring)."""

    def _call(self, **kwargs):
        with patch.object(biozone_utils, "require_staff_access", lambda: None):
            return biozone_api.staff_save_order_limit(**kwargs)

    def test_invalid_group_rejected(self):
        res = self._call(customer_group="NO-SUCH-GROUP-XYZ", min_order_amount="100")
        self.assertFalse(res["ok"])

    def test_public_group_refused(self):
        res = self._call(
            customer_group=get_public_customer_group(), min_order_amount="100"
        )
        self.assertFalse(res["ok"])

    def test_negative_rejected(self):
        groups = _active_leaf_groups()
        if not groups:
            self.skipTest("no active non-public leaf group in this environment")
        res = self._call(customer_group=groups[0], min_order_amount="-1")
        self.assertFalse(res["ok"])

    def test_garbage_rejected(self):
        groups = _active_leaf_groups()
        if not groups:
            self.skipTest("no active non-public leaf group in this environment")
        for raw in ("abc", "nan", "inf", "100000001"):
            res = self._call(customer_group=groups[0], min_order_amount=raw)
            self.assertFalse(res["ok"], raw)

    def test_arabic_digits_accepted(self):
        groups = _active_leaf_groups()
        if not groups:
            self.skipTest("no active non-public leaf group in this environment")
        group = groups[0]
        self._set_limit(group, 0)
        res = self._call(customer_group=group, min_order_amount="٥٠٠٠")
        self.assertTrue(res["ok"])
        self.assertEqual(res["min_limit"], 5000.0)
        self.assertEqual(
            frappe.db.get_value("Customer Group", group, "min_order_amount"), 5000.0
        )

    def test_empty_disables(self):
        groups = _active_leaf_groups()
        if not groups:
            self.skipTest("no active non-public leaf group in this environment")
        group = groups[0]
        self._set_limit(group, 5000)
        res = self._call(customer_group=group, min_order_amount="")
        self.assertTrue(res["ok"])
        self.assertEqual(res["min_limit"], 0.0)


class LiveConfirmCase(MinOrderDBCase):
    """Live confirm-flow matrix (T-div / T-round-live / T-calc-fail /
    T-midflight / T-race / T-callbacks / R-confirm-core).

    Fixtures are get-or-create with a fixed identity; everything created
    per test (Sales Orders + their Notification Log rows) is deleted in
    tearDown. Tests skip loudly when prerequisites are absent.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from biozone_web.units import resolve_items_display
        from biozone_web.utils import (
            create_customer_for_user,
            get_effective_item_prices,
        )

        groups = _active_leaf_groups()
        if not groups:
            raise unittest.SkipTest("no active non-public leaf group")
        cls.group = groups[0]

        if not frappe.db.exists("User", TEST_EMAIL):
            user = frappe.get_doc(
                {
                    "doctype": "User",
                    "email": TEST_EMAIL,
                    "first_name": "Test",
                    "last_name": "Min Order",
                    "send_welcome_email": 0,
                }
            )
            user.insert(ignore_permissions=True)
            frappe.db.commit()
        customer = create_customer_for_user(TEST_EMAIL, TEST_NAME)
        biozone_api._set_customer_account_type(customer, cls.group)
        cls.customer = customer

        codes = [
            r.item_code
            for r in frappe.get_all(
                "Item", filters={"disabled": 0}, fields=["item_code"], limit_page_length=50
            )
        ]
        display = resolve_items_display(codes, cls.group)
        usable = [c for c in codes if (display.get(c) or {}).get("ok")]
        if not usable:
            raise unittest.SkipTest("no displayable item for group")
        prices = get_effective_item_prices(usable, customer=customer, customer_group=cls.group)
        priced = [c for c in usable if (prices.get(c) or {}).get("price")]
        if not priced:
            raise unittest.SkipTest("no priced item for group")
        cls.item_code = priced[0]
        cls.unit_price = float(prices[cls.item_code]["price"])
        cls.uom = (display[cls.item_code] or {}).get("uom") or "Nos"

        # Live accept/reject paths require the price-write gate to be
        # closed (factory default 0). Snapshot dev values, zero them for
        # the run, restore in tearDownClass — environment left as found.
        # Done LAST so an earlier SkipTest never leaves flags modified.
        cls._flag_auto_orig = frappe.db.get_single_value(
            "Stock Settings", "auto_insert_price_list_rate_if_missing"
        )
        cls._flag_upd_orig = frappe.db.get_single_value(
            "Stock Settings", "update_existing_price_list_rate"
        )
        frappe.db.set_single_value("Stock Settings", "auto_insert_price_list_rate_if_missing", 0)
        frappe.db.set_single_value("Stock Settings", "update_existing_price_list_rate", 0)
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "_flag_auto_orig"):
            frappe.db.set_single_value(
                "Stock Settings", "auto_insert_price_list_rate_if_missing",
                cls._flag_auto_orig or 0,
            )
        if hasattr(cls, "_flag_upd_orig"):
            frappe.db.set_single_value(
                "Stock Settings", "update_existing_price_list_rate",
                cls._flag_upd_orig or 0,
            )
        frappe.db.commit()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self._orders = []
        self._receipts = []
        self._prev_user = frappe.session.user

    def tearDown(self):
        for token in self._receipts:
            name = frappe.db.get_value(
                "BZ Checkout Receipt", {"checkout_token": token}, "name"
            )
            if name:
                frappe.delete_doc(
                    "BZ Checkout Receipt", name, force=True, ignore_permissions=True
                )
        for name in self._orders:
            if frappe.db.exists("Sales Order", name):
                frappe.delete_doc("Sales Order", name, force=True)
        if self._orders:
            for row in frappe.get_all(
                "Notification Log",
                filters={"document_name": ["in", self._orders]},
                fields=["name"],
            ):
                frappe.delete_doc("Notification Log", row.name, force=True)
        frappe.db.commit()
        frappe.set_user(self._prev_user)
        super().tearDown()

    def _confirm_as_customer(self, items):
        frappe.set_user(TEST_EMAIL)
        try:
            return biozone_api.biozone_confirm_order(items)
        finally:
            frappe.set_user(self._prev_user)

    def _cart(self, qty):
        return [{"item_code": self.item_code, "quantity": qty, "uom": self.uom}]

    def _new_min_order_errors(self):
        # Error Log carries the title in `method` (there is no `title` column).
        rows = frappe.get_all(
            "Error Log",
            filters={"creation": [">", frappe.utils.now_datetime()]},
            fields=["method"],
            limit_page_length=200,
        )
        return [r.method for r in rows if "min-order" in (r.method or "")]

    def _checkout_quote(self, qty):
        items = self._cart(qty)
        quote = build_checkout_quote(
            [
                {
                    "item_code": it["item_code"],
                    "quantity": it["quantity"],
                    "uom": it["uom"],
                }
                for it in items
            ],
            customer=self.customer,
            customer_group=self.group,
            **quote_contract_fields(),
        )
        return items, quote

    def _confirm_with_meta(self, items, token, fingerprint):
        frappe.set_user(TEST_EMAIL)
        try:
            return biozone_api.biozone_confirm_order(
                items,
                checkout_token=token,
                quote_fingerprint=fingerprint,
            )
        finally:
            frappe.set_user(self._prev_user)

    def test_idempotent_same_token_replays_same_order(self):
        """LCH-1: same token+fingerprint creates one SO and replays it."""
        self._set_limit(self.group, 0)
        items, quote = self._checkout_quote(1)
        self.assertFalse(quote["issues"])
        token = frappe.generate_hash(length=32)
        self._receipts.append(token)
        before = frappe.db.count("Sales Order", {"customer": self.customer})

        frappe.local.response = {}
        first = self._confirm_with_meta(items, token, quote["fingerprint"])
        self.assertTrue(first.get("ok"), first)
        self._orders.append(first["sales_order"])
        frappe.db.commit()
        self.assertEqual(
            frappe.db.count("Sales Order", {"customer": self.customer}), before + 1
        )

        frappe.local.response = {}
        second = self._confirm_with_meta(items, token, quote["fingerprint"])
        self.assertTrue(second.get("ok"), second)
        self.assertEqual(second["sales_order"], first["sales_order"])
        self.assertEqual(
            frappe.db.count("Sales Order", {"customer": self.customer}), before + 1
        )

        receipt_name = frappe.db.get_value(
            "BZ Checkout Receipt", {"checkout_token": token}, "name"
        )
        receipt = frappe.get_doc("BZ Checkout Receipt", receipt_name)
        self.assertEqual(receipt.status, "Success")
        self.assertEqual(receipt.sales_order, first["sales_order"])

    def test_idempotent_same_token_different_payload_conflicts(self):
        """LCH-1: reusing a token for a different snapshot is rejected."""
        self._set_limit(self.group, 0)
        items_one, quote_one = self._checkout_quote(1)
        token = frappe.generate_hash(length=32)
        self._receipts.append(token)

        frappe.local.response = {}
        first = self._confirm_with_meta(items_one, token, quote_one["fingerprint"])
        self.assertTrue(first.get("ok"), first)
        self._orders.append(first["sales_order"])
        frappe.db.commit()

        items_two, quote_two = self._checkout_quote(2)
        self.assertNotEqual(quote_one["fingerprint"], quote_two["fingerprint"])
        before = frappe.db.count("Sales Order", {"customer": self.customer})

        frappe.local.response = {}
        second = self._confirm_with_meta(items_two, token, quote_two["fingerprint"])
        self.assertFalse(second.get("ok"))
        self.assertEqual(second.get("error_code"), "CHECKOUT_TOKEN_CONFLICT")
        self.assertEqual(frappe.local.response.get("http_status_code"), 409)
        self.assertTrue(second.get("already_created"))
        self.assertEqual(second.get("sales_order"), first["sales_order"])
        self.assertEqual(
            second.get("redirect"), f"/order-confirmed?name={first['sales_order']}"
        )
        self.assertEqual(
            frappe.db.count("Sales Order", {"customer": self.customer}), before
        )

    def test_failed_receipt_reused_with_new_fingerprint(self):
        """A failed receipt must not block a retry at the current price."""
        self._set_limit(self.group, 0)
        items, quote = self._checkout_quote(1)
        token = frappe.generate_hash(length=32)
        self._receipts.append(token)
        frappe.get_doc(
            {
                "doctype": "BZ Checkout Receipt",
                "checkout_token": token,
                "user": TEST_EMAIL,
                "status": "Failed",
                "quote_fingerprint": "0" * 64,
                "error_code": "TEST",
            }
        ).insert(ignore_permissions=True)

        frappe.local.response = {}
        result = self._confirm_with_meta(items, token, quote["fingerprint"])
        self.assertTrue(result.get("ok"), result)
        self._orders.append(result["sales_order"])
        receipt = frappe.get_doc("BZ Checkout Receipt", {"checkout_token": token})
        self.assertEqual(receipt.status, "Success")
        self.assertEqual(receipt.quote_fingerprint, quote["fingerprint"])
        self.assertEqual(receipt.sales_order, result["sales_order"])

    def test_idempotent_wrong_fingerprint_rejected_before_insert(self):
        """LCH-2: a stale quote fingerprint never creates an order."""
        self._set_limit(self.group, 0)
        items, _quote = self._checkout_quote(1)
        token = frappe.generate_hash(length=32)
        self._receipts.append(token)
        before = frappe.db.count("Sales Order", {"customer": self.customer})

        frappe.local.response = {}
        result = self._confirm_with_meta(items, token, "0" * 64)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_code"), "PRICE_CHANGED")
        self.assertEqual(frappe.local.response.get("http_status_code"), 409)
        self.assertEqual(
            frappe.db.count("Sales Order", {"customer": self.customer}), before
        )
        self.assertFalse(
            frappe.db.exists("BZ Checkout Receipt", {"checkout_token": token})
        )

    def test_t_div_accept_path_no_false_positive(self):
        """T-div: tiny limit + real carts must accept (precalc matched insert)."""
        self._set_limit(self.group, 1)
        for qty in (1, 3):
            res = self._confirm_as_customer(self._cart(qty))
            self.assertTrue(res.get("ok"), res)
            self._orders.append(res["sales_order"])
        self.assertEqual(self._new_min_order_errors(), [])

    def test_t_round_live_boundary(self):
        """T-round (live values): limit == net accepts, limit == net+0.01 rejects."""
        self._set_limit(self.group, 1)
        res = self._confirm_as_customer(self._cart(2))
        self.assertTrue(res.get("ok"), res)
        self._orders.append(res["sales_order"])
        net = float(frappe.db.get_value("Sales Order", res["sales_order"], "net_total"))
        self._set_limit(self.group, net)
        res2 = self._confirm_as_customer(self._cart(2))
        self.assertTrue(res2.get("ok"), res2)
        self._orders.append(res2["sales_order"])
        self._set_limit(self.group, net + 0.01)
        frappe.local.response = {}
        res3 = self._confirm_as_customer(self._cart(2))
        self.assertFalse(res3.get("ok"))
        self.assertEqual(res3.get("error_code"), "MIN_ORDER_BELOW_MINIMUM")
        self.assertEqual(frappe.local.response.get("http_status_code"), 422)

    def test_t_midflight_limit_change(self):
        """T-midflight: same cart rejected then accepted after live limit change."""
        self._set_limit(self.group, 10**9)
        frappe.local.response = {}
        res = self._confirm_as_customer(self._cart(1))
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error_code"), "MIN_ORDER_BELOW_MINIMUM")
        self.assertEqual(frappe.local.response.get("http_status_code"), 422)
        self.assertIn("shortfall", res)
        self._set_limit(self.group, 0)
        res2 = self._confirm_as_customer(self._cart(1))
        self.assertTrue(res2.get("ok"), res2)
        self._orders.append(res2["sales_order"])

    def test_t_callbacks_reject_leaves_nothing(self):
        """T-callbacks: business reject = zero SO / notification / error rows."""
        self._set_limit(self.group, 10**9)
        so_before = frappe.db.count("Sales Order", {"customer": self.customer})
        ntf_before = frappe.db.count("Notification Log", {"for_user": TEST_EMAIL})
        frappe.local.response = {}
        res = self._confirm_as_customer(self._cart(1))
        self.assertFalse(res.get("ok"))
        self.assertEqual(frappe.local.response.get("http_status_code"), 422)
        self.assertEqual(frappe.db.count("Sales Order", {"customer": self.customer}), so_before)
        self.assertEqual(frappe.db.count("Notification Log", {"for_user": TEST_EMAIL}), ntf_before)
        self.assertEqual(self._new_min_order_errors(), [])

    def test_t_calc_fail_on_price_write_gate(self):
        """T-calc-fail: enabled price-write path refuses before any calc/insert."""
        flag = "auto_insert_price_list_rate_if_missing"
        original = frappe.db.get_single_value("Stock Settings", flag)
        so_before = frappe.db.count("Sales Order", {"customer": self.customer})
        try:
            self._set_limit(self.group, 1)
            frappe.db.set_single_value("Stock Settings", flag, 1)
            frappe.db.commit()
            with self.assertRaises(biozone_api.BiozoneConfirmInternalError):
                self._confirm_as_customer(self._cart(1))
            self.assertEqual(
                frappe.db.count("Sales Order", {"customer": self.customer}), so_before
            )
        finally:
            frappe.db.set_single_value("Stock Settings", flag, original or 0)
            frappe.db.commit()

    def test_t_race_sequential_accepts(self):
        """T-race (smoke): repeated accepts stay green with zero mismatch logs."""
        self._set_limit(self.group, 1)
        for _ in range(3):
            res = self._confirm_as_customer(self._cart(1))
            self.assertTrue(res.get("ok"), res)
            self._orders.append(res["sales_order"])
        self.assertEqual(self._new_min_order_errors(), [])

    def test_t_ctx_cart_limit_matches_confirm(self):
        """T-ctx: cart meta limit uses the same server-side group value."""
        self._set_limit(self.group, 4321)
        frappe.set_user(TEST_EMAIL)
        try:
            meta = biozone_api.biozone_get_cart_prices([self.item_code])
        finally:
            frappe.set_user(self._prev_user)
        self.assertEqual(meta.get("customer_group"), self.group)
        self.assertEqual(meta.get("min_limit"), 4321.0)
        self.assertTrue(meta.get("min_applies"))

    def test_r_confirm_core_rejects(self):
        """R-confirm-core: guest يبقى PermissionError، والسلة الفارغة 422."""
        frappe.set_user("Guest")
        try:
            with self.assertRaises(frappe.PermissionError):
                biozone_api.biozone_confirm_order(self._cart(1))
        finally:
            frappe.set_user(self._prev_user)
        frappe.local.response = {}
        response = biozone_api.biozone_confirm_order([])
        self.assertEqual(frappe.local.response.get("http_status_code"), 422)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error_code"], "CART_EMPTY")
        self.assertTrue(response["request_id"])


class TestDisplayUntouched(MinOrderDBCase):
    """R-display: display-unit APIs unaffected by the limit feature."""

    def test_display_roundtrip_with_restore(self):
        from biozone_web.units import get_group_display_unit

        groups = _active_leaf_groups()
        if not groups:
            self.skipTest("no active non-public leaf group in this environment")
        group = groups[0]
        original = (frappe.db.get_value("Customer Group", group, "display_unit") or "small").strip()
        try:
            frappe.db.set_value("Customer Group", group, "display_unit", "large")
            frappe.db.commit()
            self.assertEqual(get_group_display_unit(group), "large")
        finally:
            frappe.db.set_value("Customer Group", group, "display_unit", original)
            frappe.db.commit()
        self.assertEqual(get_group_display_unit(group), original or "small")


def run():
    suite = unittest.TestLoader().loadTestsFromName(__name__)
    return unittest.TextTestRunner(verbosity=2).run(suite)
