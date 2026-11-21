"""DB-free unit tests for the shared SEO meta (Option A).

Covers the single-source site name (Website Settings.app_name), title
composition, canonical/robots, dynamic item/staff-print pages and the
``update_website_context`` hook contract.
"""

import unittest
from unittest.mock import patch

import frappe

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


if __name__ == "__main__":
	unittest.main()
