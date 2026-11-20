"""CR-01..CR-08 hardening tests.

Unit tests cover the shared normalizer, the quote contract and the
post-insert comparator (CR-04) plus the batch unit resolution (CR-06).
The live class exercises the confirm flow on the site DB: the post-insert
price guard rolls the whole transaction back, a successful replay wins
before eligibility gates, and the status endpoint never leaks another
user's attempt.

Concurrency proofs that need two browser tabs / two DB connections
(CRT-01, CRT-02) are not simulated here — they stay manual, per the
review's acceptance rules.
"""

import hashlib
import types
import unittest
from unittest.mock import patch

import frappe

from biozone_web import api as biozone_api
from biozone_web.services import checkout

TEST_EMAIL = "test-checkout-hardening@example.com"
TEST_NAME = "Test Checkout Hardening"


class TestInputValidation(unittest.TestCase):
	def _invalid(self, items):
		with self.assertRaises(checkout.CheckoutError) as ctx:
			checkout.normalize_cart_rows(items)
		self.assertEqual(ctx.exception.error_code, "ITEM_DATA_INVALID")
		return ctx.exception

	def test_non_dict_rows_rejected(self):
		self._invalid([1])
		self._invalid([{}])
		self._invalid(["A"])

	def test_bad_item_code_and_uom_rejected(self):
		self._invalid([{"item_code": 1, "quantity": 1, "uom": "Nos"}])
		self._invalid([{"item_code": "", "quantity": 1, "uom": "Nos"}])
		self._invalid([{"item_code": "A" * 200, "quantity": 1, "uom": "Nos"}])
		self._invalid([{"item_code": "A", "quantity": 1, "uom": {}}])
		self._invalid([{"item_code": "A", "quantity": 1, "uom": "U" * 200}])

	def test_bad_quantities_rejected(self):
		for qty in (0, -1, True, "nan", "inf", 1e30, None, "x"):
			with self.subTest(qty=qty):
				self._invalid([{"item_code": "A", "quantity": qty, "uom": "Nos"}])

	def test_quantity_is_rounded_to_three_decimals(self):
		rows = checkout.normalize_cart_rows([{"item_code": "A", "quantity": "1.23456", "uom": "Nos"}])
		self.assertEqual(rows[0]["quantity"], 1.235)

	def test_malformed_json_string_rejected(self):
		self._invalid("{not json")

	def test_pricing_normalizer_does_not_query_items(self):
		with patch("biozone_web.services.checkout.frappe.get_all") as get_all:
			rows = checkout.normalize_cart_rows(
				[{"item_code": "A", "quantity": 1, "uom": "Nos"}]
			)
		self.assertEqual(len(rows), 1)
		self.assertFalse(get_all.called)


class TestCartPricesContract(unittest.TestCase):
	"""CR-05: the guest pricing endpoint answers 4xx payloads, not 500s."""

	def test_non_dict_row_returns_item_data_invalid(self):
		from biozone_web import api as biozone_api

		res = biozone_api.biozone_get_cart_prices(item_codes=[], items=[1])
		self.assertFalse(res["ok"])
		self.assertEqual(res["error_code"], "ITEM_DATA_INVALID")

	def test_malformed_items_json_returns_item_data_invalid(self):
		from biozone_web import api as biozone_api

		res = biozone_api.biozone_get_cart_prices(item_codes=[], items="{bad json")
		self.assertFalse(res["ok"])
		self.assertEqual(res["error_code"], "ITEM_DATA_INVALID")

	def test_huge_quantity_returns_item_data_invalid(self):
		from biozone_web import api as biozone_api

		res = biozone_api.biozone_get_cart_prices(
			item_codes=[], items=[{"item_code": "A", "quantity": 1e30, "uom": "Nos"}]
		)
		self.assertFalse(res["ok"])
		self.assertEqual(res["error_code"], "ITEM_DATA_INVALID")


class TestQuoteContract(unittest.TestCase):
	def _build(self, **extra):
		rows = [{"item_code": "A", "quantity": 1, "uom": "Nos"}]
		with (
			patch(
				"biozone_web.utils.get_effective_item_prices",
				return_value={"A": {"price": 10.0, "base_price": 10.0, "discount_percentage": 0.0}},
			),
			patch(
				"biozone_web.units.resolve_items_display",
				return_value={"A": {"ok": True, "displayable": True, "uom": "Nos", "factor": 1.0}},
			),
		):
			return checkout.build_checkout_quote(
				rows, customer="C", customer_group="G", **extra
			)

	def test_contract_fields_are_part_of_the_fingerprint(self):
		base = self._build(company="C1", selling_price_list="Standard Selling", currency="EGP")
		other_company = self._build(
			company="C2", selling_price_list="Standard Selling", currency="EGP"
		)
		other_list = self._build(
			company="C1", selling_price_list="Other List", currency="EGP"
		)
		other_currency = self._build(
			company="C1", selling_price_list="Standard Selling", currency="USD"
		)
		self.assertNotEqual(base["fingerprint"], other_company["fingerprint"])
		self.assertNotEqual(base["fingerprint"], other_list["fingerprint"])
		self.assertNotEqual(base["fingerprint"], other_currency["fingerprint"])
		self.assertEqual(base["snapshot"]["company"], "C1")
		self.assertEqual(base["snapshot"]["currency"], "EGP")
		self.assertEqual(base["snapshot"]["v"], 2)


class TestQuoteMismatch(unittest.TestCase):
	def _quote(self, **overrides):
		row = {
			"item_code": "A",
			"uom": "Nos",
			"qty": 2.0,
			"price": 10.0,
			"line_total": 20.0,
			"available": True,
			"conversion_factor": 1.0,
		}
		row.update(overrides.pop("row", {}))
		quote = {
			"items": [row],
			"net_total": 20.0,
			"company": "C1",
			"selling_price_list": "Standard Selling",
			"currency": "EGP",
		}
		quote.update(overrides)
		return quote

	def _so(self, **overrides):
		row = {
			"item_code": "A",
			"uom": "Nos",
			"qty": 2.0,
			"conversion_factor": 1.0,
			"rate": 10.0,
			"amount": 20.0,
		}
		row.update(overrides.pop("row", {}))
		# SimpleNamespace, not frappe._dict: ``.items`` must be the child
		# table, not dict.items().
		so = types.SimpleNamespace(
			items=[types.SimpleNamespace(**row)],
			company="C1",
			selling_price_list="Standard Selling",
			currency="EGP",
		)
		for key, value in overrides.items():
			setattr(so, key, value)
		return so

	def test_matching_order_passes(self):
		self.assertIsNone(checkout.quote_mismatch(self._so(), self._quote()))

	def test_rate_change_is_detected(self):
		self.assertIn("rate changed", checkout.quote_mismatch(self._so(row={"rate": 11.0}), self._quote()))

	def test_qty_change_is_detected(self):
		self.assertIn("qty changed", checkout.quote_mismatch(self._so(row={"qty": 3.0}), self._quote()))

	def test_uom_change_is_detected(self):
		self.assertIn("line set changed", checkout.quote_mismatch(self._so(row={"uom": "Box"}), self._quote()))

	def test_factor_change_is_detected(self):
		self.assertIn(
			"uom factor changed",
			checkout.quote_mismatch(self._so(row={"conversion_factor": 12.0}), self._quote()),
		)

	def test_line_total_change_is_detected(self):
		self.assertIn(
			"line total changed",
			checkout.quote_mismatch(self._so(row={"amount": 25.0}), self._quote()),
		)

	def test_net_total_change_is_detected(self):
		self.assertIn(
			"net total changed",
			checkout.quote_mismatch(self._so(), self._quote(net_total=21.0)),
		)

	def test_pricing_input_changes_are_detected(self):
		self.assertIn("company changed", checkout.quote_mismatch(self._so(company="C2"), self._quote()))
		self.assertIn(
			"price list changed",
			checkout.quote_mismatch(self._so(selling_price_list="Other"), self._quote()),
		)
		self.assertIn("currency changed", checkout.quote_mismatch(self._so(currency="USD"), self._quote()))

	def test_duplicate_lines_are_compared_as_a_multiset(self):
		quote = self._quote()
		quote["items"] = quote["items"] * 2
		quote["net_total"] = 40.0
		so = self._so()
		so.items = so.items * 2
		self.assertIsNone(checkout.quote_mismatch(so, quote))


class TestPostInsertMinimumGate(unittest.TestCase):
	"""B: the inserted total decides the minimum gate, divergence never 500s."""

	def test_matching_totals_above_minimum(self):
		gate = checkout.evaluate_post_insert_minimum(120.0, 120.0, 50.0)
		self.assertFalse(gate["diverged"])
		self.assertTrue(gate["allowed"])
		self.assertEqual(gate["shortfall"], 0.0)

	def test_divergence_above_minimum_is_allowed(self):
		gate = checkout.evaluate_post_insert_minimum(12400.80, 12280.80, 1.0)
		self.assertTrue(gate["diverged"], "a differing precalc must be reported")
		self.assertTrue(gate["allowed"], "a valid order must not be failed by divergence")

	def test_divergence_below_minimum_is_rejected_with_inserted_totals(self):
		gate = checkout.evaluate_post_insert_minimum(40.0, 90.0, 50.0)
		self.assertTrue(gate["diverged"])
		self.assertFalse(gate["allowed"])
		self.assertEqual(gate["inserted_net"], 40.0)
		self.assertEqual(gate["shortfall"], 10.0)

	def test_rounding_noise_does_not_count_as_divergence(self):
		gate = checkout.evaluate_post_insert_minimum(10.004, 10.0, 0.0)
		self.assertFalse(gate["diverged"])
		self.assertTrue(gate["allowed"])


class TestRateLimitConfig(unittest.TestCase):
	def test_storefront_limits_configured(self):
		from biozone_web.services import rate_limit

		self.assertIn("biozone_confirm_order", rate_limit.LIMITS)
		self.assertIn("biozone_get_cart_prices", rate_limit.LIMITS)
		self.assertIn("user", rate_limit.LIMITS["biozone_confirm_order"])
		self.assertNotIn(
			"ip",
			rate_limit.LIMITS["biozone_confirm_order"],
			"confirm must be per-user, not per-IP (shared offices)",
		)

	def test_pricing_fallback_is_generous(self):
		from biozone_web.services import rate_limit

		self.assertGreater(
			rate_limit.FALLBACK_OVERRIDES["biozone_get_cart_prices"][0],
			rate_limit.FALLBACK_LIMIT[0],
		)

	def test_untrusted_ip_uses_endpoint_override(self):
		from biozone_web.services import rate_limit

		with (
			patch.object(rate_limit, "get_client_ip", return_value=(None, False)),
			patch.object(rate_limit, "_hit", return_value=1) as hit,
		):
			decision, _retry = rate_limit.check_rate_limit("biozone_get_cart_prices")
		self.assertEqual(decision, "allow")
		key, ttl = hit.call_args[0][0], hit.call_args[0][1]
		self.assertIn("untrusted", key)
		self.assertEqual(ttl, 3600)

		# 301 > the endpoint fallback (300), not the strict 5/h fallback
		with (
			patch.object(rate_limit, "get_client_ip", return_value=(None, False)),
			patch.object(rate_limit, "_hit", return_value=301),
		):
			decision, retry = rate_limit.check_rate_limit("biozone_get_cart_prices")
		self.assertEqual(decision, "deny")
		self.assertEqual(retry, 3600)

	def test_user_counter_denies_over_limit(self):
		from biozone_web.services import rate_limit

		with (
			patch.object(rate_limit, "get_client_ip", return_value=("1.2.3.4", True)),
			patch.object(rate_limit, "_hit", return_value=61),
		):
			decision, retry = rate_limit.check_rate_limit(
				"biozone_confirm_order", user="u@example.com"
			)
		self.assertEqual(decision, "deny")
		self.assertEqual(retry, 3600)


class TestUnitsBatchResolution(unittest.TestCase):
	def test_group_display_unit_is_resolved_once_per_batch(self):
		from biozone_web import units

		data = {
			"A": {
				"stock_uom": "Nos",
				"override": "inherit",
				"disabled": False,
				"conversions": [{"uom": "Box", "factor": 12.0, "min_qty": 1.0}],
			},
			"B": {
				"stock_uom": "Nos",
				"override": "inherit",
				"disabled": False,
				"conversions": [{"uom": "Box", "factor": 12.0, "min_qty": 1.0}],
			},
		}
		with (
			patch("biozone_web.units._item_unit_data", return_value=data),
			patch(
				"biozone_web.units.get_group_display_unit",
				return_value="large",
				create=True,
			) as group_mode,
		):
			resolved = units.resolve_items_display(["A", "B"], "G")
		self.assertEqual(group_mode.call_count, 1, "group mode must be read once, not per item")
		self.assertEqual(resolved["A"]["uom"], "Box")
		self.assertEqual(resolved["B"]["uom"], "Box")


def _active_leaf_groups(exclude_public=True):
	from biozone_web.utils import get_public_customer_group

	public = get_public_customer_group()
	names = [
		g.name
		for g in frappe.get_all(
			"Customer Group",
			filters={"is_group": 0, "disabled": 0},
			fields=["name"],
			order_by="name asc",
		)
	]
	if exclude_public:
		names = [n for n in names if n != public]
	return names


class CheckoutHardeningLiveCase(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		from biozone_web.units import resolve_items_display
		from biozone_web.utils import create_customer_for_user, get_effective_item_prices

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
					"last_name": "Checkout Hardening",
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
		cls.uom = (display[cls.item_code] or {}).get("uom") or "Nos"

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
		self._orders = []
		self._receipts = []
		self._restore_limits = []
		self._prev_user = frappe.session.user

	def tearDown(self):
		for token in self._receipts:
			name = frappe.db.get_value("BZ Checkout Receipt", {"checkout_token": token}, "name")
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
		for group, original in self._restore_limits:
			frappe.db.set_value("Customer Group", group, "min_order_amount", original)
		for row in frappe.get_all(
			"BZ Checkout Claim", filters={"user": TEST_EMAIL}, fields=["name"]
		):
			frappe.delete_doc(
				"BZ Checkout Claim", row.name, force=True, ignore_permissions=True
			)
		frappe.db.commit()
		frappe.set_user(self._prev_user)
		super().tearDown()

	def _set_limit(self, value):
		original = frappe.db.get_value("Customer Group", self.group, "min_order_amount")
		if not self._restore_limits:
			self._restore_limits.append((self.group, original))
		frappe.db.set_value("Customer Group", self.group, "min_order_amount", value)
		frappe.db.commit()

	def _items(self, qty=1):
		return [{"item_code": self.item_code, "quantity": qty, "uom": self.uom}]

	def _quote(self, qty=1):
		items = self._items(qty)
		quote = checkout.build_checkout_quote(
			items,
			customer=self.customer,
			customer_group=self.group,
			**checkout.quote_contract_fields(),
		)
		self.assertFalse(quote["issues"])
		return items, quote

	def _confirm(self, items, token=None, fingerprint=None, cart_instance=None):
		frappe.set_user(TEST_EMAIL)
		try:
			frappe.local.response = {}
			return biozone_api.biozone_confirm_order(
				items,
				checkout_token=token,
				quote_fingerprint=fingerprint,
				cart_instance=cart_instance,
			)
		finally:
			frappe.set_user(self._prev_user)

	def _inflating_builder(self):
		real_build = checkout.build_checkout_quote

		def inflated(rows, **kwargs):
			quote = real_build(rows, **kwargs)
			for row in quote["items"]:
				if row["available"]:
					row["price"] = round(float(row["price"]) + 1.0, 2)
					row["line_total"] = round(row["price"] * float(row["qty"]), 2)
			quote["net_total"] = round(
				sum(float(r["line_total"]) for r in quote["items"] if r["available"]), 2
			)
			quote["snapshot"]["net_total"] = quote["net_total"]
			quote["fingerprint"] = hashlib.sha256(
				checkout._json(quote["snapshot"]).encode("utf-8")
			).hexdigest()
			return quote

		return real_build, inflated

	def _assert_guard_rolled_back(self, res, token, before_count):
		self.assertFalse(res.get("ok"), res)
		self.assertEqual(res.get("error_code"), "PRICE_CHANGED")
		self.assertEqual(
			frappe.db.count("Sales Order", {"customer": self.customer}),
			before_count,
			"the guard must roll back the inserted Sales Order",
		)
		self.assertFalse(
			frappe.db.exists("BZ Checkout Receipt", {"checkout_token": token}),
			"no Success receipt may survive the rollback",
		)

	def test_post_insert_price_guard_rolls_back(self):
		self._set_limit(0)
		items = self._items(1)
		real_build, inflated = self._inflating_builder()
		inflated_quote = inflated(
			items,
			customer=self.customer,
			customer_group=self.group,
			**checkout.quote_contract_fields(),
		)
		token = frappe.generate_hash(length=32)
		self._receipts.append(token)
		before = frappe.db.count("Sales Order", {"customer": self.customer})

		with patch.object(checkout, "build_checkout_quote", side_effect=inflated):
			res = self._confirm(items, token, inflated_quote["fingerprint"])

		self._assert_guard_rolled_back(res, token, before)

	def test_post_insert_price_guard_with_minimum_enabled(self):
		self._set_limit(1)
		items = self._items(1)
		real_build, inflated = self._inflating_builder()
		inflated_quote = inflated(
			items,
			customer=self.customer,
			customer_group=self.group,
			**checkout.quote_contract_fields(),
		)
		token = frappe.generate_hash(length=32)
		self._receipts.append(token)
		before = frappe.db.count("Sales Order", {"customer": self.customer})

		with patch.object(checkout, "build_checkout_quote", side_effect=inflated):
			res = self._confirm(items, token, inflated_quote["fingerprint"])

		self._assert_guard_rolled_back(res, token, before)

	def test_replay_wins_before_eligibility_gates(self):
		self._set_limit(0)
		items, quote = self._quote(1)
		token = frappe.generate_hash(length=32)
		self._receipts.append(token)

		first = self._confirm(items, token, quote["fingerprint"])
		self.assertTrue(first.get("ok"), first)
		self._orders.append(first["sales_order"])
		frappe.db.commit()

		notifications_before = frappe.db.count(
			"Notification Log", {"document_name": first["sales_order"]}
		)
		with patch("biozone_web.utils.is_customer_order_eligible", return_value=False):
			second = self._confirm(items, token, quote["fingerprint"])

		self.assertTrue(second.get("ok"), second)
		self.assertEqual(second["sales_order"], first["sales_order"])
		self.assertEqual(
			frappe.db.count("Notification Log", {"document_name": first["sales_order"]}),
			notifications_before,
			"replay must not re-notify",
		)

	def test_status_endpoint_is_owner_only(self):
		self._set_limit(0)
		items, quote = self._quote(1)
		token = frappe.generate_hash(length=32)
		self._receipts.append(token)

		first = self._confirm(items, token, quote["fingerprint"])
		self.assertTrue(first.get("ok"), first)
		self._orders.append(first["sales_order"])
		frappe.db.commit()

		frappe.set_user(TEST_EMAIL)
		try:
			own = biozone_api.biozone_checkout_status(token)
		finally:
			frappe.set_user(self._prev_user)
		self.assertEqual(own["status"], "Success")
		self.assertEqual(own["sales_order"], first["sales_order"])

		frappe.set_user("Administrator")
		try:
			other = biozone_api.biozone_checkout_status(token)
		finally:
			frappe.set_user(self._prev_user)
		self.assertEqual(other["status"], "NotFound")
		self.assertNotIn("sales_order", other)

		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				biozone_api.biozone_checkout_status(token)
		finally:
			frappe.set_user(self._prev_user)

	def _make_receipt(self, token, fingerprint, *, status="Processing", age_minutes=0):
		doc = frappe.get_doc(
			{
				"doctype": "BZ Checkout Receipt",
				"checkout_token": token,
				"user": TEST_EMAIL,
				"status": status,
				"quote_fingerprint": fingerprint,
				"quote_snapshot": "{}",
				"request_id": "test-fixture",
			}
		).insert(ignore_permissions=True)
		if age_minutes:
			old = frappe.utils.add_to_date(
				frappe.utils.now_datetime(), minutes=-int(age_minutes)
			)
			frappe.db.set_value(
				"BZ Checkout Receipt", doc.name, "creation", old, update_modified=False
			)
		frappe.db.commit()
		return doc

	def _instance(self):
		return "ci" + frappe.generate_hash(length=30)

	def test_same_cart_instance_replays_same_order(self):
		"""Same user + same cart_instance + same fingerprint -> replay."""
		self._set_limit(0)
		items, quote = self._quote(1)
		instance = self._instance()
		token1 = frappe.generate_hash(length=32)
		self._receipts.append(token1)
		first = self._confirm(items, token1, quote["fingerprint"], cart_instance=instance)
		self.assertTrue(first.get("ok"), first)
		self._orders.append(first["sales_order"])
		frappe.db.commit()

		before = frappe.db.count("Sales Order", {"customer": self.customer})
		token2 = frappe.generate_hash(length=32)
		self._receipts.append(token2)
		second = self._confirm(items, token2, quote["fingerprint"], cart_instance=instance)
		self.assertTrue(second.get("ok"), second)
		self.assertTrue(second.get("deduplicated"), second)
		self.assertEqual(second["sales_order"], first["sales_order"])
		self.assertEqual(
			frappe.db.count("Sales Order", {"customer": self.customer}),
			before,
			"one cart cycle must produce one order",
		)
		self.assertFalse(
			frappe.db.exists("BZ Checkout Receipt", {"checkout_token": token2}),
			"the replay must not create a second receipt",
		)

	def test_new_cart_instance_identical_cart_creates_new_order(self):
		"""A new cart cycle with an identical cart must be allowed."""
		self._set_limit(0)
		items, quote = self._quote(1)
		token1 = frappe.generate_hash(length=32)
		self._receipts.append(token1)
		first = self._confirm(
			items, token1, quote["fingerprint"], cart_instance=self._instance()
		)
		self.assertTrue(first.get("ok"), first)
		self._orders.append(first["sales_order"])
		frappe.db.commit()

		token2 = frappe.generate_hash(length=32)
		self._receipts.append(token2)
		second = self._confirm(
			items, token2, quote["fingerprint"], cart_instance=self._instance()
		)
		self.assertTrue(second.get("ok"), second)
		self.assertFalse(second.get("deduplicated"))
		self.assertNotEqual(
			second["sales_order"],
			first["sales_order"],
			"an intentional repeat order from a new cycle must create a new order",
		)
		self._orders.append(second["sales_order"])

	def test_same_cart_instance_different_fingerprint_conflicts(self):
		"""Same cycle with different content: 409, never a silent second order."""
		self._set_limit(0)
		items1, quote1 = self._quote(1)
		instance = self._instance()
		token1 = frappe.generate_hash(length=32)
		self._receipts.append(token1)
		first = self._confirm(items1, token1, quote1["fingerprint"], cart_instance=instance)
		self.assertTrue(first.get("ok"), first)
		self._orders.append(first["sales_order"])
		frappe.db.commit()

		items2, quote2 = self._quote(2)
		token2 = frappe.generate_hash(length=32)
		self._receipts.append(token2)
		before = frappe.db.count("Sales Order", {"customer": self.customer})
		res = self._confirm(items2, token2, quote2["fingerprint"], cart_instance=instance)
		self.assertFalse(res.get("ok"), res)
		self.assertEqual(res.get("error_code"), "CART_INSTANCE_CONFLICT")
		self.assertTrue(res.get("already_created"))
		self.assertEqual(res.get("sales_order"), first["sales_order"])
		self.assertEqual(
			frappe.db.count("Sales Order", {"customer": self.customer}), before
		)

	def test_missing_cart_instance_does_not_block_identical_order(self):
		"""Old clients: no fingerprint-only dedup; token guarantees remain."""
		self._set_limit(0)
		items, quote = self._quote(1)
		token1 = frappe.generate_hash(length=32)
		self._receipts.append(token1)
		first = self._confirm(items, token1, quote["fingerprint"])
		self.assertTrue(first.get("ok"), first)
		self._orders.append(first["sales_order"])
		frappe.db.commit()

		token2 = frappe.generate_hash(length=32)
		self._receipts.append(token2)
		second = self._confirm(items, token2, quote["fingerprint"])
		self.assertTrue(second.get("ok"), second)
		self.assertNotEqual(
			second["sales_order"],
			first["sales_order"],
			"without a cart instance the fingerprint must not block a new order",
		)
		self._orders.append(second["sales_order"])

	def test_invalid_cart_instance_rejected(self):
		self._set_limit(0)
		items, quote = self._quote(1)
		token = frappe.generate_hash(length=32)
		self._receipts.append(token)
		res = self._confirm(items, token, quote["fingerprint"], cart_instance="short")
		self.assertFalse(res.get("ok"), res)
		self.assertEqual(res.get("error_code"), "CART_INSTANCE_INVALID")

	def test_stale_processing_without_order_recovers(self):
		self._set_limit(0)
		items, quote = self._quote(1)
		token = frappe.generate_hash(length=32)
		self._receipts.append(token)
		self._make_receipt(token, quote["fingerprint"], age_minutes=15)

		res = self._confirm(items, token, quote["fingerprint"])
		self.assertTrue(res.get("ok"), res)
		self._orders.append(res["sales_order"])

	def test_stale_processing_with_existing_order_is_replayed(self):
		self._set_limit(0)
		items, quote = self._quote(1)
		token = frappe.generate_hash(length=32)
		self._receipts.append(token)
		first = self._confirm(items, token, quote["fingerprint"])
		self.assertTrue(first.get("ok"), first)
		self._orders.append(first["sales_order"])
		frappe.db.commit()

		name = frappe.db.get_value("BZ Checkout Receipt", {"checkout_token": token}, "name")
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-15)
		frappe.db.set_value(
			"BZ Checkout Receipt",
			name,
			{"status": "Processing", "creation": old},
			update_modified=False,
		)
		frappe.db.commit()

		before = frappe.db.count("Sales Order", {"customer": self.customer})
		again = self._confirm(items, token, quote["fingerprint"])
		self.assertTrue(again.get("ok"), again)
		self.assertEqual(again["sales_order"], first["sales_order"])
		self.assertEqual(
			frappe.db.count("Sales Order", {"customer": self.customer}), before
		)
		self.assertEqual(
			frappe.db.get_value("BZ Checkout Receipt", name, "status"), "Success"
		)

	def test_legacy_confirm_without_metadata_still_works(self):
		self._set_limit(0)
		items = self._items(1)
		before = frappe.db.count("Sales Order", {"customer": self.customer})
		res = self._confirm(items)
		self.assertTrue(res.get("ok"), res)
		self._orders.append(res["sales_order"])
		self.assertEqual(
			frappe.db.count("Sales Order", {"customer": self.customer}), before + 1
		)

	def test_future_pricing_rule_does_not_break_confirm(self):
		"""B root fix: the precalc must use the same transaction date as insert.

		The leftover TST-COMPAT fixtures carry a future-dated pricing rule
		(valid_from after today). Without an explicit ``transaction_date`` the
		precalc applied it and diverged from the inserted order, which the old
		exact-equality guard turned into a 500. Now the totals agree, the order
		succeeds, and no divergence is logged.
		"""
		from biozone_web.units import resolve_items_display
		from biozone_web.utils import get_effective_item_prices

		codes = [
			r.item_code
			for r in frappe.get_all(
				"Item",
				filters={"item_code": ["like", "TST-COMPAT-A-%"]},
				fields=["item_code"],
				limit_page_length=20,
			)
		]
		if not codes:
			self.skipTest("no TST-COMPAT fixture in this environment")
		display = resolve_items_display(codes, self.group)
		prices = get_effective_item_prices(codes, customer=self.customer, customer_group=self.group)
		usable = [
			c for c in codes
			if (display.get(c) or {}).get("ok")
			and (display.get(c) or {}).get("displayable", True)
			and (prices.get(c) or {}).get("price")
		]
		if not usable:
			self.skipTest("no usable TST-COMPAT fixture for this group")

		item_code = usable[0]
		items = [
			{
				"item_code": item_code,
				"quantity": 1,
				"uom": (display.get(item_code) or {}).get("uom") or "",
			}
		]
		self._set_limit(1)
		quote = checkout.build_checkout_quote(
			items,
			customer=self.customer,
			customer_group=self.group,
			**checkout.quote_contract_fields(),
		)
		token = frappe.generate_hash(length=32)
		self._receipts.append(token)
		started = frappe.utils.now_datetime()

		res = self._confirm(items, token, quote["fingerprint"])
		self.assertTrue(res.get("ok"), res)
		self._orders.append(res["sales_order"])
		so = frappe.get_doc("Sales Order", res["sales_order"])
		self.assertEqual(checkout.money2(so.net_total), quote["net_total"])

		logs = frappe.get_all(
			"Error Log",
			filters={
				"method": "Biozone min-order precalc/insert divergence",
				"creation": [">", started],
			},
			fields=["name"],
		)
		try:
			self.assertFalse(
				logs, "the precalc must now match the inserted order (no divergence)"
			)
		finally:
			for row in logs:
				frappe.delete_doc("Error Log", row.name, force=True, ignore_permissions=True)
