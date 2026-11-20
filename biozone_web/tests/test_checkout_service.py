"""Unit tests for checkout quote/idempotency helpers (no DB writes).

Covers LCH-1 metadata validation, LCH-2 fingerprint sensitivity,
LCH-4 no 200-item truncation, and LCH-6 one Item query for many lines.
"""

import unittest
from unittest.mock import patch

import frappe

from biozone_web import api
from biozone_web.services import checkout


class TestCheckoutMetadata(unittest.TestCase):
	def test_valid_metadata_is_normalized(self):
		token = "a" * 32
		fingerprint = "b" * 64
		self.assertEqual(
			checkout.validate_checkout_metadata(f" {token} ", f" {fingerprint} "),
			(token, fingerprint),
		)

	def test_invalid_token_rejected(self):
		with self.assertRaises(checkout.CheckoutError) as ctx:
			checkout.validate_checkout_metadata("short", "b" * 64)
		self.assertEqual(ctx.exception.error_code, "CHECKOUT_TOKEN_INVALID")
		self.assertEqual(ctx.exception.status, 422)

	def test_invalid_fingerprint_rejected(self):
		with self.assertRaises(checkout.CheckoutError) as ctx:
			checkout.validate_checkout_metadata("a" * 32, "not-hex")
		self.assertEqual(ctx.exception.error_code, "CART_QUOTE_INVALID")
		self.assertEqual(ctx.exception.status, 422)


class TestNormalizeItems(unittest.TestCase):
	def _rows(self, codes):
		return [frappe._dict(item_code=code, disabled=0) for code in codes]

	def test_one_item_query_for_max_lines(self):
		codes = [f"ITEM-{i:03d}" for i in range(checkout.MAX_CART_LINES)]
		items = [{"item_code": code, "quantity": 1, "uom": "Nos"} for code in codes]
		with patch(
			"biozone_web.services.checkout.frappe.get_all",
			return_value=self._rows(codes),
		) as get_all:
			normalized = checkout.normalize_checkout_items(items)

		self.assertEqual(len(normalized), checkout.MAX_CART_LINES)
		self.assertEqual(get_all.call_count, 1)
		_, kwargs = get_all.call_args
		self.assertEqual(kwargs["filters"]["item_code"], ["in", codes])
		self.assertEqual(kwargs["limit_page_length"], checkout.MAX_CART_LINES)

	def test_disabled_item_rejected(self):
		rows = [frappe._dict(item_code="A", disabled=1)]
		with patch("biozone_web.services.checkout.frappe.get_all", return_value=rows):
			with self.assertRaises(checkout.CheckoutError) as ctx:
				checkout.normalize_checkout_items(
					[{"item_code": "A", "quantity": 1, "uom": "Nos"}]
				)
		self.assertEqual(ctx.exception.error_code, "ITEM_UNAVAILABLE")

	def test_invalid_quantity_rejected(self):
		for qty in (0, -1, "nan", True, None):
			with self.assertRaises(checkout.CheckoutError) as ctx:
				checkout.normalize_checkout_items(
					[{"item_code": "A", "quantity": qty, "uom": "Nos"}]
				)
			self.assertEqual(ctx.exception.error_code, "ITEM_DATA_INVALID")

	def test_more_than_max_lines_rejected_without_query(self):
		items = [
			{"item_code": f"ITEM-{i}", "quantity": 1, "uom": "Nos"}
			for i in range(checkout.MAX_CART_LINES + 1)
		]
		with patch("biozone_web.services.checkout.frappe.get_all") as get_all:
			with self.assertRaises(checkout.CheckoutError) as ctx:
				checkout.normalize_checkout_items(items)
		self.assertEqual(ctx.exception.error_code, "CART_TOO_LARGE")
		self.assertFalse(get_all.called)


class TestQuoteFingerprint(unittest.TestCase):
	def _quote(self, codes, price=10.0, uom="Nos", factor=1.0, qty=1, sent_uom=None):
		prices = {
			code: {
				"price": price,
				"base_price": price,
				"discount_percentage": 0.0,
			}
			for code in codes
		}
		display = {
			code: {
				"ok": True,
				"displayable": True,
				"uom": uom,
				"factor": factor,
			}
			for code in codes
		}
		line_uom = sent_uom if sent_uom is not None else uom
		items = [
			{"item_code": code, "quantity": qty, "uom": line_uom}
			for code in codes
		]
		with (
			patch("biozone_web.utils.get_effective_item_prices", return_value=prices),
			patch("biozone_web.units.resolve_items_display", return_value=display),
		):
			return checkout.build_checkout_quote(
				items,
				customer="CUST-TEST",
				customer_group="GROUP-TEST",
			)

	def test_same_inputs_same_fingerprint(self):
		first = self._quote(["A"])
		second = self._quote(["A"])
		self.assertEqual(first["fingerprint"], second["fingerprint"])
		self.assertEqual(first["issues"], [])
		self.assertEqual(len(first["so_items"]), 1)

	def test_price_change_changes_fingerprint(self):
		self.assertNotEqual(
			self._quote(["A"], price=10.0)["fingerprint"],
			self._quote(["A"], price=10.01)["fingerprint"],
		)

	def test_uom_change_changes_fingerprint_and_flags_issue(self):
		changed = self._quote(["A"], uom="Box", factor=12.0, sent_uom="Nos")
		self.assertNotEqual(self._quote(["A"])["fingerprint"], changed["fingerprint"])
		self.assertEqual(changed["issues"][0]["code"], "UOM_CHANGED")

	def test_quantity_change_changes_fingerprint(self):
		self.assertNotEqual(
			self._quote(["A"], qty=1)["fingerprint"],
			self._quote(["A"], qty=2)["fingerprint"],
		)

	def test_max_items_are_not_truncated(self):
		codes = [f"ITEM-{i:03d}" for i in range(checkout.MAX_CART_LINES)]
		quote = self._quote(codes)
		self.assertEqual(len(quote["items"]), checkout.MAX_CART_LINES)
		self.assertEqual(len(quote["prices"]), checkout.MAX_CART_LINES)
		self.assertEqual(len(quote["so_items"]), checkout.MAX_CART_LINES)

	def test_over_max_items_rejected(self):
		codes = [f"ITEM-{i:03d}" for i in range(checkout.MAX_CART_LINES + 1)]
		with self.assertRaises(checkout.CheckoutError) as ctx:
			self._quote(codes)
		self.assertEqual(ctx.exception.error_code, "CART_TOO_LARGE")


class TestCartPricesCap(unittest.TestCase):
	def test_max_codes_all_reach_quote_builder(self):
		codes = [f"ITEM-{i:03d}" for i in range(checkout.MAX_CART_LINES)]
		captured = {}

		def fake_quote(rows, **_kwargs):
			captured["rows"] = rows
			return {
				"prices": {row["item_code"]: {"price": 1.0} for row in rows},
				"fingerprint": "c" * 64,
				"net_total": float(len(rows)),
			}

		with (
			patch(
				"biozone_web.services.checkout.build_checkout_quote",
				side_effect=fake_quote,
			),
			patch(
				"biozone_web.utils.get_storefront_price_context",
				return_value={
					"customer": None,
					"customer_group": "Public",
					"is_active": True,
					"is_guest": True,
				},
			),
			patch(
				"biozone_web.services.item_images.resolve_item_thumbnails",
				return_value={},
			),
			patch("biozone_web.utils.get_group_min_order_amount", return_value=0.0),
		):
			result = api.biozone_get_cart_prices(codes)

		self.assertEqual(len(captured["rows"]), checkout.MAX_CART_LINES)
		self.assertEqual(len(result["prices"]), checkout.MAX_CART_LINES)
		self.assertEqual(result["quote_fingerprint"], "c" * 64)

	def test_over_cap_codes_rejected_before_pricing(self):
		codes = [f"ITEM-{i:03d}" for i in range(checkout.MAX_CART_LINES + 1)]
		frappe.local.response = {}
		with patch("biozone_web.utils.get_storefront_price_context") as context:
			result = api.biozone_get_cart_prices(codes)
		self.assertFalse(result.get("ok"))
		self.assertEqual(result.get("error_code"), "CART_TOO_LARGE")
		self.assertEqual(frappe.local.response.get("http_status_code"), 422)
		self.assertFalse(context.called)


if __name__ == "__main__":
	unittest.main()
