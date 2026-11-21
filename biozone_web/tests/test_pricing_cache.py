"""اختبارات كاش التسعير (خيار أ) — A..E + رسالة PRICE_CHANGED.

تُشغَّل على موقع تطوير (تحتاج بيانات Item Price / Pricing Rule):

    bench --site development.localhost run-tests \
      --module biozone_web.tests.test_pricing_cache --case TestPricingCache

الكاش يُفعَّل داخل الاختبار عبر frappe.conf بغض النظر عن إعداد الموقع.
"""

import unittest
from contextlib import contextmanager
from unittest.mock import patch

import frappe

from biozone_web.services import pricing_cache
from biozone_web.utils import get_effective_item_prices

PRICE_LIST = "Standard Selling"


@contextmanager
def cache_enabled(on=True, ttl=None):
	conf = frappe.get_conf()
	old_flag = conf.get("biozone_pricing_cache_enabled")
	old_ttl = conf.get("biozone_pricing_cache_ttl")
	conf["biozone_pricing_cache_enabled"] = 1 if on else 0
	if ttl is not None:
		conf["biozone_pricing_cache_ttl"] = ttl
	try:
		yield
	finally:
		if old_flag is None:
			conf.pop("biozone_pricing_cache_enabled", None)
		else:
			conf["biozone_pricing_cache_enabled"] = old_flag
		if old_ttl is None:
			conf.pop("biozone_pricing_cache_ttl", None)
		else:
			conf["biozone_pricing_cache_ttl"] = old_ttl


def _priced_row():
	rows = frappe.get_all(
		"Item Price",
		filters={"price_list": PRICE_LIST, "price_list_rate": [">", 0]},
		fields=["name", "item_code", "price_list_rate", "uom"],
		order_by="modified desc",
		limit_page_length=50,
	)
	for row in rows:
		stock_uom = frappe.db.get_value("Item", row["item_code"], "stock_uom")
		if not (row.get("uom") or "") or row["uom"] == stock_uom:
			if not frappe.db.get_value("Item", row["item_code"], "disabled"):
				return row
	return None


class TestPricingCache(unittest.TestCase):
	"""الحالات A1/A2/B1/B3/C/D/E + عدّادات + رسالة 409."""

	@classmethod
	def setUpClass(cls):
		cls.row = _priced_row()
		if not cls.row:
			raise unittest.SkipTest("no usable Standard Selling Item Price")
		cls.price_row = cls.row["name"]
		cls.item_code = cls.row["item_code"]
		cls.original_rate = float(cls.row["price_list_rate"])
		cls.customer = None
		cls.group = None
		groups = frappe.get_all(
			"Customer Group",
			filters={"is_group": 0, "disabled": 0},
			fields=["name"],
			limit_page_length=5,
		)
		cls.group = groups[0]["name"] if groups else None
		cls.other_group = groups[1]["name"] if len(groups) > 1 else cls.group

	@classmethod
	def tearDownClass(cls):
		_set_rate(cls.price_row, cls.original_rate)
		pricing_cache.clear()

	def setUp(self):
		pricing_cache.clear()

	def tearDown(self):
		pricing_cache.clear()

	def _price(self, *, group=None, customer=None, use_cache):
		group = group or self.group
		with cache_enabled(use_cache):
			return get_effective_item_prices(
				[self.item_code], customer=customer, customer_group=group
			)[self.item_code]

	# ----- A1: تغيير Item Price بمستند (هوك) يظهر فورًا -----
	def test_a1_item_price_doc_change_invalidates_immediately(self):
		base = self._price(use_cache=True)
		_set_rate(self.price_row, self.original_rate + 10)
		after_cached = self._price(use_cache=True)
		after_truth = self._price(use_cache=False)
		self.assertEqual(
			after_cached["price"],
			after_truth["price"],
			"بعد حفظ Item Price يجب أن يتغيّر السعر فورًا (إبطال بالهوك)",
		)
		self.assertNotEqual(after_cached["price"], base["price"])

	# ----- A2: إنشاء/تعطيل Pricing Rule يظهر فورًا -----
	def test_a2_pricing_rule_change_invalidates_immediately(self):
		base = self._price(use_cache=True)
		rule = _create_discount_rule(self.item_code, 10, customer=None)
		try:
			with_rule_cached = self._price(use_cache=True)
			with_rule_truth = self._price(use_cache=False)
			self.assertEqual(with_rule_cached["price"], with_rule_truth["price"])
			self.assertLess(with_rule_cached["price"], base["price"], "الخصم لم يظهر")

			frappe.db.set_value("Pricing Rule", rule, "disable", 1)
			frappe.get_doc("Pricing Rule", rule).save()  # يشغّل on_update
			after_disable_cached = self._price(use_cache=True)
			after_disable_truth = self._price(use_cache=False)
			self.assertEqual(after_disable_cached["price"], after_disable_truth["price"])
		finally:
			if frappe.db.exists("Pricing Rule", rule):
				frappe.delete_doc("Pricing Rule", rule, force=True, ignore_permissions=True)
			pricing_cache.clear()

	# ----- B1: تعديل SQL مباشر (بلا هوك) يبقى قديمًا داخل TTL -----
	def test_b1_direct_db_write_stays_stale_until_ttl(self):
		# نستخدم تعطيل قاعدة تسعير عبر SQL: لا يغيّر السعر الأساسي (فالمفتاح
		# لا يتغيّر) ولا يشغّل الهوك ⇒ السقف هو TTL.
		rule = _create_discount_rule(self.item_code, 20, customer=None)
		try:
			discounted = self._price(use_cache=True)
			truth_discounted = self._price(use_cache=False)
			self.assertLess(discounted["price"], truth_discounted["base_price"])

			frappe.db.set_value("Pricing Rule", rule, "disable", 1)  # بلا هوك
			stale = self._price(use_cache=True)
			fresh = self._price(use_cache=False)
			self.assertEqual(stale["price"], discounted["price"], "الكاش قديم كما هو متوقع داخل TTL")
			self.assertNotEqual(fresh["price"], stale["price"], "التسعير المباشر يرى القاعدة المعطّلة")

			# B3: المسح اليدوي يعيد الحقيقة فورًا (بدل انتظار TTL)
			pricing_cache.clear()
			after_clear = self._price(use_cache=True)
			self.assertEqual(after_clear["price"], fresh["price"], "المسح اليدوي لم يُحدّث السعر")
		finally:
			try:
				frappe.db.set_value("Pricing Rule", rule, "disable", 0)
				frappe.delete_doc("Pricing Rule", rule, force=True, ignore_permissions=True)
			except Exception:
				pass
			frappe.db.commit()
			pricing_cache.clear()

	# ----- C: تغيير فئة العميل = مفتاح مختلف (بلا تسريب) -----
	def test_c_customer_group_change_uses_new_key(self):
		a_cached = self._price(group=self.group, use_cache=True)
		b_cached = self._price(group=self.other_group, use_cache=True)
		a_truth = self._price(group=self.group, use_cache=False)
		b_truth = self._price(group=self.other_group, use_cache=False)
		self.assertEqual(a_cached["price"], a_truth["price"])
		self.assertEqual(b_cached["price"], b_truth["price"])

	# ----- D: قاعدة تسعير تخص عميلًا لا تتسرّب لعميل آخر -----
	def test_d_customer_specific_rule_does_not_leak(self):
		rule = _create_discount_rule(self.item_code, 15, customer="__CUSTOMER__")
		customer = frappe.db.get_value("Customer", {}, "name")
		try:
			if not customer:
				self.skipTest("no customer to target")
			_set_rule_customer(rule, customer)
			pricing_cache.clear()
			target_cached = self._price(customer=customer, use_cache=True)
			target_truth = self._price(customer=customer, use_cache=False)
			self.assertEqual(target_cached["price"], target_truth["price"])

			other = frappe.db.get_value("Customer", {"name": ["!=", customer]}, "name")
			if other:
				other_cached = self._price(customer=other, use_cache=True)
				other_truth = self._price(customer=other, use_cache=False)
				self.assertEqual(other_cached["price"], other_truth["price"])
		finally:
			if frappe.db.exists("Pricing Rule", rule):
				frappe.delete_doc("Pricing Rule", rule, force=True, ignore_permissions=True)
			pricing_cache.clear()

	# ----- E: كاش دافئ = استعلامات ثابتة تقريبًا -----
	def test_e_warm_cache_is_not_n_plus_1(self):
		rows = frappe.get_all(
			"Item Price",
			filters={"price_list": PRICE_LIST, "price_list_rate": [">", 0]},
			fields=["item_code"],
			limit_page_length=30,
		)
		codes = list(dict.fromkeys(r["item_code"] for r in rows))[:30]
		if len(codes) < 10:
			self.skipTest("need at least 10 priced items")

		with cache_enabled(True):
			get_effective_item_prices(codes, customer_group=self.group)  # تسخين
			state = {"n": 0}
			orig = frappe.db.sql

			def counting(*args, **kwargs):
				state["n"] += 1
				return orig(*args, **kwargs)

			frappe.db.sql = counting
			try:
				get_effective_item_prices(codes, customer_group=self.group)
			finally:
				frappe.db.sql = orig
		self.assertLessEqual(
			state["n"],
			25,
			"كاش دافئ لـ%d صنفًا يجب أن يبقى بعيدًا عن N+1 (was %d queries)" % (len(codes), state["n"]),
		)

	# ----- عدّادات hit/miss + 409 -----
	def test_stats_counters(self):
		with cache_enabled(True):
			self._price(use_cache=True)  # miss + store
			self._price(use_cache=True)  # hit
		stats = pricing_cache.get_stats()
		self.assertGreaterEqual(stats["miss"], 1)
		self.assertGreaterEqual(stats["hit"], 1)
		self.assertTrue(0.0 <= stats["hit_rate"] <= 1.0)


	# ----- رسالة PRICE_CHANGED واضحة للزبون -----
	def test_price_changed_message_asks_to_review_cart(self):
		import inspect

		from biozone_web import api

		source = inspect.getsource(api.biozone_confirm_order)
		self.assertIn("راجع السلة", source, "رسالة 409 يجب أن تطلب مراجعة السلة")
		self.assertEqual(source.count("راجع السلة"), 2, "الرسالتان (قبل/بعد الإدراج)")

		with open(_page_path("www", "cart.html"), encoding="utf-8") as fh:
			page = fh.read()
		self.assertIn("حدّث الصفحة لمراجعة السلة ثم أكّد الطلب", page)


class TestStaleCacheCannotCreateOrder(unittest.TestCase):
	"""B2: كاش قديم + تأكيد ⇒ 409 PRICE_CHANGED ولا Sales Order."""

	@classmethod
	def setUpClass(cls):
		from biozone_web import api as biozone_api
		from biozone_web.units import resolve_items_display
		from biozone_web.utils import create_customer_for_user

		cls.biozone_api = biozone_api
		row = _priced_row()
		if not row:
			raise unittest.SkipTest("no priced item")
		cls.price_row = row["name"]
		cls.item_code = row["item_code"]
		cls.original_rate = float(row["price_list_rate"])

		groups = frappe.get_all(
			"Customer Group", filters={"is_group": 0, "disabled": 0}, fields=["name"], limit_page_length=5
		)
		if not groups:
			raise unittest.SkipTest("no active customer group")
		cls.group = groups[0]["name"]

		email = "test-pricing-cache@example.com"
		if not frappe.db.exists("User", email):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Cache",
					"last_name": "Test",
					"send_welcome_email": 0,
				}
			)
			user.insert(ignore_permissions=True)
			frappe.db.commit()
		customer = create_customer_for_user(email, "Cache Test")
		try:
			biozone_api._set_customer_account_type(customer, cls.group)
		except Exception:
			pass
		cls.email = email
		cls.customer = customer

		display = resolve_items_display([cls.item_code], cls.group)
		cls.uom = (display.get(cls.item_code) or {}).get("uom") or "Nos"

	@classmethod
	def tearDownClass(cls):
		_set_rate(cls.price_row, cls.original_rate, use_sql=True)
		pricing_cache.clear()

	def tearDown(self):
		pricing_cache.clear()
		frappe.set_user("Administrator")

	def test_stale_cached_price_is_rejected_and_rolled_back(self):
		from biozone_web.api import biozone_confirm_order, biozone_get_cart_prices

		frappe.set_user(self.email)
		pricing_cache.clear()
		items = [{"item_code": self.item_code, "quantity": 1, "uom": self.uom}]

		with cache_enabled(True):
			stale = biozone_get_cart_prices(
				[self.item_code], items=items
			)
			self.assertTrue(stale.get("ok"))
			fingerprint = stale["quote_fingerprint"]

			# كتابة مباشرة تتجاوز الهوكات: الكاش يبقى قديمًا
			_set_rate(self.price_row, self.original_rate + 30, use_sql=True)

			so_before = frappe.db.count("Sales Order")
			frappe.local.response = frappe._dict()
			result = biozone_confirm_order(
				items=items,
				checkout_token="a" * 32,
				quote_fingerprint=fingerprint,
			)
			so_after = frappe.db.count("Sales Order")

		self.assertEqual(frappe.local.response.get("http_status_code"), 409)
		self.assertEqual(getattr(result, "get", lambda *_: None)("error_code"), "PRICE_CHANGED")
		self.assertIn("راجع السلة", result.get("error", ""))
		self.assertEqual(so_after, so_before, "لا يجب إنشاء Sales Order بسعر قديم")
		self.assertGreaterEqual(
			pricing_cache.get_stats()["price_changed_409"], 1, "عدّاد 409 لم يزد"
		)



def _page_path(*parts):
	import os

	root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
	return os.path.normpath(os.path.join(root, *parts))


def _set_rate(name, rate, use_sql=False):
	if use_sql:
		frappe.db.set_value("Item Price", name, "price_list_rate", rate)
	else:
		doc = frappe.get_doc("Item Price", name)
		doc.price_list_rate = rate
		doc.save(ignore_permissions=True)
	frappe.db.commit()


def _create_discount_rule(item_code, percent, customer=None):
	rule = frappe.get_doc(
		{
			"doctype": "Pricing Rule",
			"title": "BZ cache test %s" % frappe.utils.random_string(6),
			"apply_on": "Item Code",
			"items": [{"item_code": item_code}],
			"selling": 1,
			"price_or_product_discount": "Price",
			"rate_or_discount": "Discount Percentage",
			"discount_percentage": percent,
			"company": frappe.defaults.get_defaults().get("company") or frappe.db.get_value("Company", {}, "name"),
			"valid_from": frappe.utils.today(),
		}
	)
	if customer and customer != "__CUSTOMER__":
		rule.applicable_for = "Customer"
		rule.customer = customer
	rule.insert(ignore_permissions=True)
	frappe.db.commit()
	return rule.name


def _set_rule_customer(rule_name, customer):
	doc = frappe.get_doc("Pricing Rule", rule_name)
	doc.applicable_for = "Customer"
	doc.customer = customer
	doc.save(ignore_permissions=True)
	frappe.db.commit()


if __name__ == "__main__":
	unittest.main()
