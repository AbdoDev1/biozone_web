"""Regression tests: SO pricing must survive DN/SI creation and pricing changes.

The flow under test is the staff delivery path: a submitted Sales Order is
turned into a Delivery Note and then a Sales Invoice. ERPNext recalculates
item rates on every save/submit, so these tests assert the SO-linked rate and
unit discount at each stage, including after the matching Pricing Rule changes.

Runs against the site DB (``bench --site <site> execute
biozone_web.tests.test_delivery_invoice_pricing.run``). All mutations are
rolled back in tearDown; no committed test data.
"""

import unittest

import frappe

from biozone_web import api as biozone_api
from biozone_web import utils as biozone_utils

COMPANY = "Biozone"
WAREHOUSE = "Stores - BIO"
COST_CENTER = "Main - BIO"
TAX_ACCOUNT = "GST - BIO"
PRICE_LIST = "Standard Selling"
CURRENCY = "EGP"
TERRITORY = "Rest Of The World"
DEFAULT_TERRITORY = "Egypt"
BASE_RATE = 100.0
SO_QTY = 5.0


def _site_ready():
	try:
		return bool(
			frappe.db.get_value("Company", COMPANY, "name")
			and frappe.db.get_value("Warehouse", WAREHOUSE, "name")
			and frappe.db.get_value("Customer Group", "All Customer Groups", "name")
		)
	except Exception:
		return False


class DeliveryInvoicePricingCase(unittest.TestCase):
	def setUp(self):
		if not _site_ready():
			self.skipTest("site fixtures unavailable")
		self.tag = frappe.generate_hash(length=8)
		self.public_group = biozone_utils.get_public_customer_group()
		if not self.public_group:
			self.skipTest("public customer group unavailable")

	def tearDown(self):
		frappe.db.rollback()
		frappe.clear_cache()

	def _make_group(self, label):
		name = f"TST-DP-{label}-{self.tag}"
		frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": name,
				"parent_customer_group": "All Customer Groups",
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
		return name

	def _make_item(self, label):
		stock_uom = frappe.db.get_value("UOM", {"enabled": 1}, "name")
		item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		if not stock_uom or not item_group:
			self.skipTest("UOM or leaf Item Group unavailable")
		code = f"TST-DP-ITEM-{label}-{self.tag}"
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": f"TST DP {label} {self.tag}",
				"item_group": item_group,
				"stock_uom": stock_uom,
				"is_stock_item": 0,
				"is_sales_item": 1,
			}
		).insert(ignore_permissions=True)
		return code, stock_uom

	def _make_customer(self, label, group):
		name = f"TST DP {label} {self.tag}"
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": name,
				"customer_type": "Company",
				"customer_group": group,
				"territory": TERRITORY,
			}
		).insert(ignore_permissions=True)
		return name

	def _make_pricing_rule(self, label, item_code, group, discount):
		rule = frappe.get_doc(
			{
				"doctype": "Pricing Rule",
				"title": f"TST DP {label} {self.tag}",
				"apply_on": "Item Code",
				"price_or_product_discount": "Price",
				"selling": 1,
				"applicable_for": "Customer Group",
				"customer_group": group,
				"rate_or_discount": "Discount Percentage",
				"discount_percentage": discount,
				"valid_from": frappe.utils.today(),
				"items": [{"item_code": item_code}],
			}
		)
		rule.insert(ignore_permissions=True)
		return rule

	def _make_order(self, label, discount, with_tax=False, extra_items=0):
		group = self._make_group(label)
		item_code, uom = self._make_item(label)
		customer = self._make_customer(label, group)
		rule = self._make_pricing_rule(label, item_code, group, discount)

		rate = BASE_RATE - (BASE_RATE * discount / 100.0)
		item = {
			"item_code": item_code,
			"item_name": item_code,
			"description": item_code,
			"qty": SO_QTY,
			"uom": uom,
			"conversion_factor": 1,
			"price_list_rate": BASE_RATE,
			"discount_percentage": discount,
			"discount_amount": BASE_RATE * discount / 100.0,
			"rate": rate,
			"delivery_date": frappe.utils.today(),
			"warehouse": WAREHOUSE,
		}
		items = [item]
		for extra_index in range(extra_items):
			extra_code, extra_uom = self._make_item(f"{label}-EXTRA-{extra_index}")
			items.append(
				{
					"item_code": extra_code,
					"item_name": extra_code,
					"description": extra_code,
					"qty": SO_QTY,
					"uom": extra_uom,
					"conversion_factor": 1,
					"price_list_rate": BASE_RATE,
					"rate": BASE_RATE,
					"delivery_date": frappe.utils.today(),
					"warehouse": WAREHOUSE,
				}
			)
		order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"customer": customer,
				"customer_group": group,
				"territory": TERRITORY,
				"company": COMPANY,
				"transaction_date": frappe.utils.today(),
				"delivery_date": frappe.utils.today(),
				"currency": CURRENCY,
				"conversion_rate": 1,
				"selling_price_list": PRICE_LIST,
				"set_warehouse": WAREHOUSE,
				"items": items,
				"taxes": (
					[
						{
							"charge_type": "On Net Total",
							"account_head": TAX_ACCOUNT,
							"description": "GST 14%",
							"rate": 14,
							"cost_center": COST_CENTER,
						}
					]
					if with_tax
					else []
				),
			}
		)
		order.insert(ignore_permissions=True)
		order.submit()
		order.reload()

		self.assertEqual(order.docstatus, 1)
		self.assertEqual(order.customer_group, group)
		self.assertEqual(order.territory, TERRITORY)
		self.assertEqual(float(order.items[0].rate), rate)
		self.assertEqual(float(order.items[0].discount_percentage), float(discount))

		frappe.defaults.set_user_default("customer_group", self.public_group)
		frappe.defaults.set_user_default("territory", DEFAULT_TERRITORY)
		frappe.db.set_value("Pricing Rule", rule.name, "discount_percentage", 55)
		frappe.clear_cache(doctype="Pricing Rule")

		return {
			"group": group,
			"customer": customer,
			"item_code": item_code,
			"rule": rule.name,
			"order": order,
			"order_item": order.items[0],
			"rate": rate,
			"discount": discount,
			"with_tax": with_tax,
		}

	def _assert_item(self, item, case, qty, check_amount=True):
		self.assertEqual(item.so_detail, case["order_item"].name)
		self.assertAlmostEqual(float(item.price_list_rate), BASE_RATE, places=2)
		self.assertAlmostEqual(float(item.discount_percentage), float(case["discount"]), places=2)
		self.assertAlmostEqual(
			float(item.discount_amount),
			BASE_RATE * case["discount"] / 100.0,
			places=2,
		)
		self.assertAlmostEqual(float(item.rate), case["rate"], places=2)
		if check_amount:
			self.assertAlmostEqual(float(item.amount), case["rate"] * qty, places=2)

	def _assert_flow(self, case, qty=SO_QTY):
		so = case["order"]
		order_item = case["order_item"]
		qty_map = {order_item.name: qty}

		dn = biozone_api._build_delivery_note_for_order(so, WAREHOUSE, qty_map)
		self.assertEqual(dn.customer_group, case["group"])
		self.assertEqual(dn.territory, TERRITORY)
		self.assertEqual(dn.ignore_pricing_rule, 1)
		self._assert_item(dn.items[0], case, qty, check_amount=False)

		dn.insert(ignore_permissions=True)
		dn.reload()
		self._assert_item(dn.items[0], case, qty)
		dn.save(ignore_permissions=True)
		dn.reload()
		self._assert_item(dn.items[0], case, qty)
		dn.run_method("validate")
		self._assert_item(dn.items[0], case, qty)
		dn.submit()
		dn.reload()
		self.assertEqual(dn.docstatus, 1)
		self._assert_item(dn.items[0], case, qty)

		si = biozone_api._build_sales_invoice_for_delivery(so, dn)
		self.assertEqual(si.customer_group, case["group"])
		self.assertEqual(si.territory, TERRITORY)
		self.assertEqual(si.ignore_pricing_rule, 1)
		self._assert_item(si.items[0], case, qty, check_amount=False)

		si.insert(ignore_permissions=True)
		si.reload()
		self._assert_item(si.items[0], case, qty)
		si.save(ignore_permissions=True)
		si.reload()
		self._assert_item(si.items[0], case, qty)
		si.run_method("validate")
		self._assert_item(si.items[0], case, qty)
		si.submit()
		si.reload()
		self.assertEqual(si.docstatus, 1)
		self._assert_item(si.items[0], case, qty)

		if case["with_tax"]:
			expected_tax = case["rate"] * qty * 0.14
			self.assertAlmostEqual(float(si.taxes[0].tax_amount), expected_tax, places=2)
			self.assertAlmostEqual(float(si.grand_total), case["rate"] * qty + expected_tax, places=2)

		so.reload()
		self.assertEqual(so.customer_group, case["group"])
		self.assertEqual(so.territory, TERRITORY)
		self.assertAlmostEqual(float(so.items[0].rate), case["rate"], places=2)
		self.assertAlmostEqual(float(so.items[0].discount_percentage), float(case["discount"]), places=2)

		return dn, si

	def test_pricing_rule_change_does_not_reprice_generated_documents(self):
		for label, discount in (("PUBLIC", 10), ("PHARMACY", 20), ("WHOLESALE", 30)):
			with self.subTest(label=label):
				self._assert_flow(self._make_order(f"{label}-{self.tag}", discount))

	def test_partial_delivery_uses_so_unit_price_and_dn_quantity(self):
		case = self._make_order(f"PARTIAL-{self.tag}", 30, with_tax=True)
		dn, si = self._assert_flow(case, qty=2)
		self.assertAlmostEqual(float(dn.items[0].qty), 2, places=2)
		self.assertAlmostEqual(float(si.items[0].qty), 2, places=2)
		self.assertAlmostEqual(float(si.taxes[0].tax_amount), 19.6, places=2)
		self.assertAlmostEqual(float(si.grand_total), 159.6, places=2)

	def test_partial_qty_map_omits_unlisted_order_items(self):
		case = self._make_order(f"QTYMAP-{self.tag}", 10, extra_items=1)
		so = case["order"]
		first, second = so.items

		partial = biozone_api._build_delivery_note_for_order(so, WAREHOUSE, {first.name: 2})
		self.assertEqual(len(partial.items), 1)
		self.assertEqual(partial.items[0].so_detail, first.name)
		self.assertAlmostEqual(float(partial.items[0].qty), 2, places=2)

		full = biozone_api._build_delivery_note_for_order(so, WAREHOUSE)
		self.assertEqual({d.so_detail for d in full.items}, {first.name, second.name})
		for row in full.items:
			self.assertAlmostEqual(float(row.qty), SO_QTY, places=2)

	def test_split_delivery_rows_are_all_invoiced(self):
		case = self._make_order(f"SPLIT-{self.tag}", 30)
		so = case["order"]
		order_item = case["order_item"]

		dn = biozone_api._build_delivery_note_for_order(so, WAREHOUSE, {order_item.name: 2})
		dn.append(
			"items",
			{
				"item_code": order_item.item_code,
				"item_name": order_item.item_name,
				"description": order_item.description or order_item.item_name,
				"qty": 1,
				"uom": order_item.uom,
				"against_sales_order": so.name,
				"so_detail": order_item.name,
				"warehouse": WAREHOUSE,
				"price_list_rate": order_item.price_list_rate,
				"discount_percentage": order_item.discount_percentage,
				"discount_amount": order_item.discount_amount,
				"rate": order_item.rate,
			},
		)
		dn.insert(ignore_permissions=True)
		dn.submit()
		dn.reload()
		self.assertEqual(len(dn.items), 2)

		si = biozone_api._build_sales_invoice_for_delivery(so, dn)
		self.assertEqual(len(si.items), 2)
		self.assertEqual([row.so_detail for row in si.items], [order_item.name] * 2)
		self.assertEqual([float(row.qty) for row in si.items], [2.0, 1.0])
		self.assertEqual(
			[row.dn_detail for row in si.items],
			[dn.items[0].name, dn.items[1].name],
		)
		self.assertEqual(
			[row.delivery_note for row in si.items],
			[dn.name, dn.name],
		)
		for row in si.items:
			self.assertAlmostEqual(float(row.rate), case["rate"], places=2)

		si.insert(ignore_permissions=True)
		si.submit()
		dn.reload()
		self.assertAlmostEqual(float(dn.items[0].billed_amt), case["rate"] * 2, places=2)
		self.assertAlmostEqual(float(dn.items[1].billed_amt), case["rate"] * 1, places=2)

	def test_unlinked_delivery_rows_are_rejected(self):
		case = self._make_order(f"UNLINKED-{self.tag}", 10)
		so = case["order"]
		dn = biozone_api._build_delivery_note_for_order(so, WAREHOUSE, {case["order_item"].name: 2})
		dn.items[0].so_detail = None
		dn.items[0].against_sales_order = None

		with self.assertRaises(frappe.ValidationError) as ctx:
			biozone_api._build_sales_invoice_for_delivery(so, dn)
		self.assertIn("غير مرتبط", str(ctx.exception))


def run():
	suite = unittest.TestLoader().loadTestsFromName(__name__)
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	return {
		"tests": result.testsRun,
		"errors": len(result.errors),
		"failures": len(result.failures),
	}


if __name__ == "__main__":
	unittest.main()
