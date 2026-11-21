"""DB-free unit tests for the shared SEO meta (Option A).

Covers the single-source site name (Website Settings.app_name), title
composition, canonical/robots, dynamic item/staff-print pages and the
``update_website_context`` hook contract.
"""

import re
import unittest
from unittest.mock import patch
from urllib.parse import quote

import frappe
from markupsafe import Markup

from biozone_web.services import seo


def _settings(app_name):
    return frappe._dict(app_name=app_name)


class TestSiteName(unittest.TestCase):
	def test_site_name_from_website_settings(self):
		with patch.object(seo.frappe, "get_cached_doc", return_value=_settings("Biozone")):
			self.assertEqual(seo.get_site_name(), "Biozone")

	def test_blank_app_name_falls_back_to_default(self):
		with patch.object(seo.frappe, "get_cached_doc", return_value=_settings("  ")):
			self.assertEqual(seo.get_site_name(), seo.DEFAULT_SITE_NAME)

	def test_missing_settings_falls_back_to_default(self):
		with patch.object(seo.frappe, "get_cached_doc", side_effect=Exception("no site")):
			self.assertEqual(seo.get_site_name(), seo.DEFAULT_SITE_NAME)


class TestStoreTitle(unittest.TestCase):
	def test_site_first(self):
		self.assertEqual(seo.store_title("المتجر", "Biozone"), "Biozone - المتجر")

	def test_site_last(self):
		self.assertEqual(
			seo.store_title("طلباتي", "Biozone", site_first=False), "طلباتي - Biozone"
		)

	def test_empty_page_title_is_site_name(self):
		self.assertEqual(seo.store_title("", "Biozone"), "Biozone")

	def test_site_name_drift_is_reflected_everywhere(self):
		self.assertEqual(seo.store_title("المتجر", "شركة أخرى"), "شركة أخرى - المتجر")


class TestBuildSeo(unittest.TestCase):
	def _build(self, route, **kwargs):
		with patch.object(seo, "get_url", side_effect=lambda p: f"https://biozone.pro{p}"):
			return seo.build_seo(route, **kwargs)

	def test_canonical_is_absolute_and_normalized(self):
		built = self._build("catalog", title="Biozone - المتجر")
		self.assertEqual(built.canonical, "https://biozone.pro/catalog")

	def test_route_without_leading_slash_is_normalized(self):
		built = self._build("catalog", title="t")
		self.assertEqual(built.canonical, "https://biozone.pro/catalog")

	def test_noindex_sets_robots(self):
		self.assertEqual(self._build("cart", title="t", noindex=True).robots, "noindex, nofollow")

	def test_indexable_has_no_robots(self):
		self.assertEqual(self._build("catalog", title="t").robots, "")

	def test_relative_image_becomes_absolute(self):
		built = self._build("item", title="t", image="/files/a.png")
		self.assertEqual(built.image, "https://biozone.pro/files/a.png")

	def test_absolute_image_is_kept(self):
		built = self._build("item", title="t", image="https://cdn.example/a.png")
		self.assertEqual(built.image, "https://cdn.example/a.png")

	def test_missing_title_falls_back_to_site_name(self):
		with patch.object(seo.frappe, "get_cached_doc", return_value=_settings("Biozone")):
			built = self._build("catalog", title=None)
		self.assertEqual(built.title, "Biozone")


class TestPageMeta(unittest.TestCase):
	def setUp(self):
		patcher = patch.object(
			seo.frappe, "get_cached_doc", return_value=_settings("Biozone")
		)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_home_and_root_share_definition(self):
		home = seo.get_page_meta("biozone-home")
		root = seo.get_page_meta("")
		self.assertEqual(home["title"], root["title"])
		self.assertFalse(home.get("noindex"))

	def test_catalog_is_indexable_with_description(self):
		meta = seo.get_page_meta("catalog")
		self.assertIn("Biozone", meta["title"])
		self.assertTrue(meta["description"])
		self.assertFalse(meta.get("noindex"))

	def test_item_uses_item_name(self):
		meta = seo.get_page_meta("item", {"item": {"item_name": "باراسيتامول"}})
		self.assertEqual(meta["title"], "Biozone - باراسيتامول")
		self.assertIn("باراسيتامول", meta["description"])

	def test_item_without_item_uses_fallback(self):
		meta = seo.get_page_meta("item", {})
		self.assertEqual(meta["title"], f"Biozone - {seo.ITEM_FALLBACK_TITLE}")

	def test_private_pages_are_noindexed(self):
		for path in (
			"cart",
			"login",
			"my-orders",
			"order-confirmed",
			"system-home",
			"not-available",
			"account",
			"account/orders",
			"account/order-detail",
			"account/return-detail",
			"staff/login",
			"staff/dashboard",
		):
			with self.subTest(path=path):
				self.assertTrue(seo.get_page_meta(path, {}).get("noindex"))

	def test_staff_print_title_depends_on_kind(self):
		credit = seo.get_page_meta("staff/print", {"print_kind": "credit"})
		invoice = seo.get_page_meta("staff/print", {"print_kind": "invoice"})
		self.assertEqual(credit["title"], "إشعار مرتجع - بيوزون")
		self.assertEqual(invoice["title"], "فاتورة - بيوزون")

	def test_every_staff_page_is_managed(self):
		for path in seo.STAFF_TITLES:
			with self.subTest(path=path):
				self.assertTrue(seo.get_page_meta(path, {}).get("noindex"))

	def test_unknown_path_is_not_managed(self):
		self.assertIsNone(seo.get_page_meta("unknown-route"))


class TestUpdateWebsiteContext(unittest.TestCase):
	def setUp(self):
		self.get_cached_doc = patch.object(
			seo.frappe, "get_cached_doc", return_value=_settings("Biozone")
		).start()
		self.get_url = patch.object(
			seo, "get_url", side_effect=lambda p: f"https://biozone.pro{p}"
		).start()
		self.addCleanup(patch.stopall)

	def test_hook_attaches_seo_for_managed_path(self):
		context = frappe._dict(path="catalog", item=None)
		self.assertIsNone(seo.update_website_context(context))
		self.assertEqual(context.seo.title, "Biozone - المتجر")
		self.assertEqual(context.seo.canonical, "https://biozone.pro/catalog")

	def test_hook_handles_root_path(self):
		context = frappe._dict(path="")
		seo.update_website_context(context)
		self.assertEqual(context.seo.canonical, "https://biozone.pro/")
		self.assertFalse(context.seo.robots)

	def test_hook_skips_unknown_path(self):
		context = frappe._dict(path="unknown-route")
		seo.update_website_context(context)
		self.assertNotIn("seo", context)


def _render_meta(built):
	"""Render the shared partial exactly as a page would."""
	return frappe.render_template(
		'{% include "templates/includes/seo_meta.html" %}', {"seo": built}
	)


class TestTitleEscaping(unittest.TestCase):
	"""The <title> must be HTML-escaped, including Markup input."""

	def test_title_escapes_script_and_closing_tag(self):
		with patch.object(seo, "get_url", side_effect=lambda p: "https://biozone.pro" + p):
			built = seo.build_seo(
				"/item", title=Markup("</title><script>alert(1)</script>")
			)
		html = _render_meta(built)
		self.assertIn("<title>&lt;/title&gt;&lt;script&gt;alert(1)&lt;/script&gt;</title>", html)
		self.assertNotIn("</title><script>", html)
		self.assertNotIn("<script>", html)

	def test_markup_value_is_downgraded_to_plain_text(self):
		built = seo.build_seo("/caption", title=Markup("<b>x</b>"))
		self.assertEqual(type(built.title), str)
		self.assertNotIsInstance(built.title, Markup)

	def test_arabic_title_renders_intact(self):
		built = seo.build_seo("/catalog", title="المتجر الطبي")
		self.assertIn("<title>المتجر الطبي</title>", _render_meta(built))

	def test_description_is_escaped(self):
		built = seo.build_seo("/x", title="t", description=Markup('<b id="d">x</b>'))
		html = _render_meta(built)
		self.assertIn('content="&lt;b id=&#34;d&#34;&gt;x&lt;/b&gt;"', html)
		self.assertNotIn('<b id="d">', html)


class TestItemCanonical(unittest.TestCase):
	"""Item canonical keeps the encoded code only — no other query params."""

	def _item_meta(self, code, name="صنف تجريبي"):
		with patch.object(seo, "get_site_name", return_value="Biozone"):
			return seo.get_page_meta("item", {"item": {"item_code": code, "item_name": name}})

	def test_code_is_url_encoded(self):
		meta = self._item_meta("BZ 11504/1")
		self.assertEqual(meta["canonical"], "/item?code=BZ%2011504%2F1")

	def test_two_items_get_distinct_canonicals(self):
		a = self._item_meta("BZ-11504")
		b = self._item_meta("TST-COMPAT-A-0726BA92")
		self.assertNotEqual(a["canonical"], b["canonical"])
		self.assertIn("BZ-11504", a["canonical"])
		self.assertIn("TST-COMPAT-A-0726BA92", b["canonical"])

	def test_canonical_uses_configured_domain_not_localhost(self):
		meta = self._item_meta("BZ-11504")
		with patch.object(seo, "get_url", side_effect=lambda p: "https://biozone.pro" + p):
			built = seo.build_seo("/item", canonical=meta["canonical"], title="t")
		self.assertEqual(built.canonical, "https://biozone.pro/item?code=BZ-11504")
		self.assertNotIn("localhost", built.canonical)

	def test_og_url_matches_canonical(self):
		built = seo.build_seo(
			"/item", canonical="/item?code=BZ-11504", title="t"
		)
		html = _render_meta(built)
		self.assertIn(
			'<meta property="og:url" content="%s">' % built.canonical, html
		)


class TestItemPageRendered(unittest.TestCase):
	"""End-to-end: a real item page, hostile name, real HTTP response.

	Writes only to an explicitly named isolated test site: both
	``BZ_TEST_SITE`` and ``BZ_TEST_BASE_URL`` are required, the process must
	run on that site, and the known dev/prod sites are refused. A unique
	canary item (per run) is fetched before anything else so a 200 from the
	home page cannot masquerade as a database match.

	Every run uses unique codes and registers ``_cleanup`` *before* the first
	write. Cleanup deletes exactly the tracked records (plus a run-scoped
	sweep for a crash between commit and tracking), verifies there are no
	leftovers, and raises instead of swallowing a failure.
	"""

	HOSTILE = "</title><b id=\"xss\">pwn</b> سويت"
	ARABIC = "سويت ايزى 50جرام"
	_NON_TEST_SITES = {
		"development.localhost",
		"biozone.pro",
		"store.biozone.pro",
		"staff.biozone.pro",
		"app.biozone.pro",
	}

	@classmethod
	def setUpClass(cls):
		import os
		import secrets
		import urllib.request

		cls._urllib = urllib.request
		cls.BASE = (os.environ.get("BZ_TEST_BASE_URL") or "").rstrip("/")
		cls.SITE = (os.environ.get("BZ_TEST_SITE") or "").strip()
		if not cls.BASE or not cls.SITE:
			raise unittest.SkipTest(
				"set BZ_TEST_BASE_URL and BZ_TEST_SITE to an isolated test site"
			)
		if cls.SITE != frappe.local.site:
			raise RuntimeError(
				"BZ_TEST_SITE=%s but run-tests is on %s — refusing to write"
				% (cls.SITE, frappe.local.site)
			)
		if cls.SITE in cls._NON_TEST_SITES or frappe.local.site in cls._NON_TEST_SITES:
			raise RuntimeError(
				"refusing to write integration-test data to %s" % frappe.local.site
			)

		cls.RUN_ID = secrets.token_hex(4)
		cls.CODE_CANARY = "BZ-SEO-T-%s-CANARY" % cls.RUN_ID
		cls.CODE_XSS = "BZ-SEO-T-%s-XSS" % cls.RUN_ID
		cls.CODE_AR = "BZ-SEO-T-%s-AR" % cls.RUN_ID
		cls.codes = (cls.CODE_CANARY, cls.CODE_XSS, cls.CODE_AR)
		cls.created = []
		cls._cleanup_done = False
		# قبل أول كتابة: أي فشل لاحق في setUpClass أو الاختبارات يمر هنا.
		# الاستدعاء الصريح في except يجعل الضمان محليًا لا معتمدًا على
		# سلوك unittest وحده (والحارس _cleanup_done يمنع التنفيذ مرتين).
		cls.addClassCleanup(cls._cleanup)

		try:
			cls._check_server()
			src = cls._pick_source_item()
			cls.group, cls.uom = src.item_group, src.stock_uom
			cls._make_item(cls.CODE_CANARY, "SEO canary %s" % cls.RUN_ID)
			cls._assert_same_database()
			cls._make_item(cls.CODE_XSS, cls.HOSTILE)
			cls._make_item(cls.CODE_AR, cls.ARABIC)
		except Exception:
			cls._cleanup()
			raise

	@classmethod
	def _open(cls, url, timeout=15):
		# لا header يفرض الموقع: يجب أن يخدم BZ_TEST_BASE_URL موقع الاختبار
		# نفسه. الكناري يثبت أن قاعدة البيانات المقروءة هي قاعدة الموقع.
		return cls._urllib.urlopen(url, timeout=timeout)

	@classmethod
	def _check_server(cls):
		try:
			with cls._open(cls.BASE + "/", timeout=5) as resp:
				if resp.status != 200:
					raise unittest.SkipTest("test server answered %s" % resp.status)
		except unittest.SkipTest:
			raise
		except Exception as exc:
			raise unittest.SkipTest(
				"test server not reachable at %s: %s" % (cls.BASE, exc)
			)

	@classmethod
	def _pick_source_item(cls):
		base = frappe.get_all(
			"Item Price",
			filters={"price_list": "Standard Selling", "price_list_rate": [">", 0]},
			fields=["item_code"],
			limit=1,
		)
		if not base:
			raise unittest.SkipTest("no priced item to copy group/uom from")
		return frappe.get_doc("Item", base[0]["item_code"])

	@classmethod
	def _make_item(cls, code, name):
		# اسم الصنف معروف قبل الإدراج؛ يُسجَّل أولًا كي يصل إليه التنظيف
		# حتى لو رمى الإدراج بعد كتابة جزئية. الحذف يتحقق من الوجود.
		cls.created.append(("Item", code))
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": name,
				"item_group": cls.group,
				"stock_uom": cls.uom,
				"is_stock_item": 0,
			}
		).insert(ignore_permissions=True)
		# الاسم الفعلي قد يختلف إن غيّر الـcontroller الـautoname.
		cls.created[-1] = ("Item", item.name)
		frappe.db.commit()
		price = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": code,
				"price_list": "Standard Selling",
				"price_list_rate": 12,
				"uom": "",
			}
		).insert(ignore_permissions=True)
		cls.created.append(("Item Price", price.name))
		frappe.db.commit()

	@classmethod
	def _assert_same_database(cls):
		html = cls._fetch(cls.CODE_CANARY)
		if html is None:
			raise AssertionError(
				"BASE_URL %s did not return 200 for canary %s"
				% (cls.BASE, cls.CODE_CANARY)
			)
		if cls.RUN_ID not in html:
			raise AssertionError(
				"BASE_URL %s is not serving the database of %s: canary %s "
				"is missing (a 200 from / proves nothing)"
				% (cls.BASE, cls.SITE, cls.CODE_CANARY)
			)

	@classmethod
	def _fetch(cls, code, extra=""):
		url = cls.BASE + "/item?code=%s%s" % (quote(code, safe=""), extra)
		try:
			with cls._open(url, timeout=15) as resp:
				if resp.status != 200:
					return None
				return resp.read().decode("utf-8", "replace")
		except Exception:
			return None

	@classmethod
	def _cleanup(cls):
		"""Delete exactly this run's records; never swallow a failure."""
		if getattr(cls, "_cleanup_done", False):
			return
		cls._cleanup_done = True
		errors = []
		# الأحدث أولًا: Item Price قبل Item.
		for doctype, name in reversed(list(getattr(cls, "created", []))):
			try:
				if frappe.db.exists(doctype, name):
					frappe.delete_doc(
						doctype, name, force=True, ignore_permissions=True
					)
			except Exception as exc:
				errors.append("%s %s: %r" % (doctype, name, exc))
		# شبكة أمان لسجل لم يُسجَّل (رمى الإدراج بعد commit): الاستعلام
		# محصور بأكواد هذا التشغيل فقط، لا يحذف أي بيانات أخرى.
		for code in getattr(cls, "codes", ()):
			try:
				for price in frappe.get_all(
					"Item Price", filters={"item_code": code}, pluck="name"
				):
					frappe.delete_doc(
						"Item Price", price, force=True, ignore_permissions=True
					)
				if frappe.db.exists("Item", code):
					frappe.delete_doc(
						"Item", code, force=True, ignore_permissions=True
					)
			except Exception as exc:
				errors.append("sweep %s: %r" % (code, exc))
		try:
			frappe.db.commit()
		except Exception as exc:
			errors.append("commit: %r" % exc)

		leftovers = {}
		run_id = getattr(cls, "RUN_ID", None)
		if run_id:
			prefix = "BZ-SEO-T-%s-" % run_id
			leftovers["Item"] = frappe.get_all(
				"Item", filters={"item_code": ["like", prefix + "%"]}, pluck="item_code"
			)
			leftovers["Item Price"] = frappe.get_all(
				"Item Price",
				filters={"item_code": ["like", prefix + "%"]},
				pluck="name",
			)
		if errors or any(leftovers.values()):
			raise AssertionError(
				"SEO test cleanup failed: errors=%s leftovers=%s"
				% (errors, leftovers)
			)

	def test_hostile_title_is_text_not_markup(self):
		html = self._fetch(self.CODE_XSS)
		self.assertTrue(html, "item page should render")
		match = re.search(r"<title>(.*?)</title>", html, re.S)
		self.assertTrue(match, "title missing")
		title = match.group(1)
		# Frappe sanitises item_name on save, so the HTML-looking remainder
		# must still arrive as text — never as elements.
		self.assertIn("&lt;", title)
		self.assertNotIn("<b", title)
		self.assertNotIn("</title", title)
		self.assertIn("سويت", title)
		self.assertNotIn('<b id="xss">', html)

	def test_saved_name_is_sanitised_before_render(self):
		# Documents the second layer: the DB itself drops </title> from the name.
		stored = frappe.db.get_value("Item", self.CODE_XSS, "item_name") or ""
		self.assertNotIn("</title", stored)

	def test_arabic_title_is_intact(self):
		html = self._fetch(self.CODE_AR)
		self.assertIn(self.ARABIC, html)

	def test_each_item_has_its_own_canonical_and_no_extra_params(self):
		for code in (self.CODE_XSS, self.CODE_AR):
			html = self._fetch(code, "&utm_source=x&page=2")
			match = re.search(r'<link rel="canonical" href="([^"]+)"', html or "")
			self.assertTrue(match, "canonical missing for %s" % code)
			href = match.group(1)
			self.assertTrue(href.startswith(self.BASE), href)
			self.assertIn("code=" + quote(code, safe=""), href)
			self.assertNotIn("utm_source", href)
			self.assertNotIn("page=", href)
			og = re.search(r'<meta property="og:url" content="([^"]+)"', html)
			self.assertTrue(og)
			self.assertEqual(og.group(1), href)


if __name__ == "__main__":
	unittest.main()
