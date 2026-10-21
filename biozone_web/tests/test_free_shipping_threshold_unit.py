"""Unit tests (DB-free) for the display-only free-shipping threshold."""

import unittest
from unittest.mock import patch

from biozone_web import utils


class TestFreeShippingThreshold(unittest.TestCase):
	def setUp(self):
		self.get_meta = patch.object(utils.frappe, "get_meta").start()
		self.get_single_value = patch.object(utils.frappe.db, "get_single_value").start()
		self.get_all = patch.object(utils.frappe.db, "get_all").start()
		self.addCleanup(patch.stopall)
		self.get_meta.return_value.has_field.return_value = True
		self.get_single_value.return_value = 1000

	def test_global_fallback_without_customer_group(self):
		self.assertEqual(utils.get_free_shipping_threshold(), 1000.0)

	def test_global_fallback_when_group_has_no_row(self):
		self.get_all.return_value = []
		self.assertEqual(utils.get_free_shipping_threshold("Group A"), 1000.0)

	def test_group_row_overrides_global(self):
		self.get_all.return_value = [{"free_shipping_threshold": 750}]
		self.assertEqual(utils.get_free_shipping_threshold("Group A"), 750.0)

	def test_zero_group_row_hides_sentence(self):
		self.get_all.return_value = [{"free_shipping_threshold": 0}]
		self.assertEqual(utils.get_free_shipping_threshold("Group A"), 0.0)

	def test_missing_table_field_falls_back_to_global(self):
		self.get_meta.return_value.has_field.side_effect = (
			lambda field: field != utils.FREE_SHIPPING_TABLE_FIELD
		)
		self.assertEqual(utils.get_free_shipping_threshold("Group A"), 1000.0)

	def test_invalid_group_value_hides_sentence(self):
		self.get_all.return_value = [{"free_shipping_threshold": "invalid"}]
		self.assertEqual(utils.get_free_shipping_threshold("Group A"), 0.0)

	def test_storefront_uses_current_customer_group(self):
		with (
			patch.object(utils, "get_customer_price_group", return_value="Group A"),
			patch.object(utils, "get_free_shipping_threshold", return_value=750.0) as get_threshold,
		):
			self.assertEqual(utils.get_storefront_free_shipping_threshold(), 750.0)
			get_threshold.assert_called_once_with("Group A")


if __name__ == "__main__":
	unittest.main()
