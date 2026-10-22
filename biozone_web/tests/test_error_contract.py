import json
import unittest
from unittest.mock import patch

import frappe
import sentry_sdk
from werkzeug.wrappers import Response

from biozone_web import api
from biozone_web.request_id import finalize_response, get_request_id, set_request_id
from biozone_web.sentry_scrubber import ensure_sentry_scrubber, sanitize_event

GENERIC_500 = "تعذر إتمام الطلب، يرجى المحاولة مرة أخرى"


class TestErrorContract(unittest.TestCase):
	def setUp(self):
		self.prev_user = frappe.session.user
		self.prev_response = getattr(frappe.local, "response", None)
		self.prev_headers = getattr(frappe.local, "response_headers", None)
		self.prev_request_id = getattr(frappe.local, "biozone_request_id", None)
		frappe.local.response = {}
		frappe.local.response_headers = {}

	def tearDown(self):
		frappe.set_user(self.prev_user)
		frappe.local.response = self.prev_response
		frappe.local.response_headers = self.prev_headers
		if self.prev_request_id is None:
			if hasattr(frappe.local, "biozone_request_id"):
				del frappe.local.biozone_request_id
		else:
			frappe.local.biozone_request_id = self.prev_request_id
		frappe.db.rollback()

	def test_api_fail_is_422_and_rolls_back(self):
		frappe.set_user("Administrator")
		todo = frappe.get_doc({"doctype": "ToDo", "description": "zz error contract rollback"}).insert(
			ignore_permissions=True
		)
		self.assertTrue(frappe.db.exists("ToDo", todo.name))

		response = api._api_fail("TEST_ERROR", "رسالة اختبار")

		self.assertEqual(frappe.local.response["http_status_code"], 422)
		self.assertFalse(response["ok"])
		self.assertEqual(response["error_code"], "TEST_ERROR")
		self.assertEqual(response["error"], "رسالة اختبار")
		self.assertTrue(response["request_id"])
		self.assertFalse(frappe.db.exists("ToDo", todo.name))

	def test_confirm_empty_cart_is_422(self):
		frappe.set_user("Administrator")
		response = api.biozone_confirm_order([])
		self.assertEqual(frappe.local.response["http_status_code"], 422)
		self.assertEqual(response["error_code"], "CART_EMPTY")
		self.assertTrue(response["request_id"])

	def _first_item_cart(self):
		item = frappe.get_all("Item", filters={"disabled": 0}, pluck="name", limit=1)
		if not item:
			self.skipTest("no enabled Item for confirm validation")
		return [{"item_code": item[0], "quantity": 1, "uom": ""}]

	def test_missing_customer_is_422(self):
		frappe.set_user("Administrator")
		with patch("biozone_web.utils.find_customer_for_current_user", return_value=None):
			response = api.biozone_confirm_order(self._first_item_cart())
		self.assertEqual(frappe.local.response["http_status_code"], 422)
		self.assertFalse(response["ok"])
		self.assertEqual(response["error_code"], "CUSTOMER_MISSING")
		self.assertTrue(response["request_id"])

	def test_ineligible_customer_is_422(self):
		frappe.set_user("Administrator")
		with (
			patch("biozone_web.utils.find_customer_for_current_user", return_value="__test_customer__"),
			patch("biozone_web.utils.is_customer_order_eligible", return_value=False),
		):
			response = api.biozone_confirm_order(self._first_item_cart())
		self.assertEqual(frappe.local.response["http_status_code"], 422)
		self.assertFalse(response["ok"])
		self.assertEqual(response["error_code"], "CUSTOMER_GROUP_INELIGIBLE")
		self.assertTrue(response["request_id"])

	def test_pilot_helpers_use_same_contract(self):
		frappe.set_user("Administrator")
		stock = api._pilot_stock_fail("STOCK_ERROR", "رسالة")
		self.assertEqual(frappe.local.response["http_status_code"], 422)
		self.assertEqual(stock["error_code"], "STOCK_ERROR")
		self.assertTrue(stock["request_id"])

		frappe.local.response = {}
		barcode = api._pilot_barcode_fail("BARCODE_ERROR", "رسالة", {"out_of_order": True})
		self.assertEqual(frappe.local.response["http_status_code"], 422)
		self.assertEqual(barcode["error_code"], "BARCODE_ERROR")
		self.assertTrue(barcode["out_of_order"])
		self.assertTrue(barcode["request_id"])

	def test_500_response_is_generic_with_request_id(self):
		response = Response(
			json.dumps({"exc_type": "Raw", "_server_messages": "secret"}),
			status=500,
			content_type="application/json",
		)
		finalize_response(response)
		body = json.loads(response.get_data(as_text=True))
		self.assertEqual(body, {"message": GENERIC_500, "request_id": get_request_id()})
		self.assertEqual(response.headers["X-Request-Id"], body["request_id"])

	def test_403_response_gets_request_id(self):
		response = Response(
			json.dumps({"exc_type": "PermissionError", "message": "denied"}),
			status=403,
			content_type="application/json",
		)
		finalize_response(response)
		body = json.loads(response.get_data(as_text=True))
		self.assertEqual(body["request_id"], get_request_id())
		self.assertEqual(response.headers["X-Request-Id"], body["request_id"])

	def test_request_id_is_stable_and_in_headers(self):
		frappe.local.response = {}
		frappe.local.response_headers = {}
		set_request_id()
		first = get_request_id()
		set_request_id()
		self.assertEqual(first, get_request_id())
		self.assertEqual(frappe.local.response["request_id"], first)
		self.assertEqual(frappe.local.response_headers["X-Request-Id"], first)


class FakeSentryClient:
	def __init__(self):
		self.options = {"before_send": None}


class TestSentryScrubber(unittest.TestCase):
	def test_sanitize_event_removes_sensitive_data(self):
		event = {
			"user": {"email": "secret@example.com"},
			"breadcrumbs": [{"message": "token=secret"}],
			"extra": {"password": "secret"},
			"logentry": {"message": "SELECT password FROM tabUser"},
			"message": "secret",
			"request": {
				"url": "https://example.com/api?token=secret",
				"headers": {"Authorization": "Bearer secret"},
				"data": {"password": "secret"},
				"query_string": "token=secret",
			},
			"contexts": {
				"JSON Body": {"password": "secret"},
				"job": {"kwargs": {"password": "secret"}},
				"runtime": {"name": "python"},
			},
			"tags": {"frappe_user": "secret@example.com", "password": "secret", "request_id": "r1"},
			"exception": {
				"values": [
					{
						"type": "ValueError",
						"value": "secret",
						"stacktrace": {"frames": [{"vars": {"password": "secret"}}]},
					}
				]
			},
		}
		out = sanitize_event(event)
		blob = json.dumps(out, ensure_ascii=False)
		for secret in ("secret@example.com", "Bearer", "SELECT", "secret"):
			self.assertNotIn(secret, blob)
		self.assertEqual(out["request"]["url"], "https://example.com/api")
		self.assertEqual(out["tags"], {"password": "[Filtered]", "request_id": "r1"})
		self.assertNotIn("user", out)
		self.assertNotIn("breadcrumbs", out)
		self.assertNotIn("extra", out)
		self.assertNotIn("JSON Body", out["contexts"])
		self.assertIn("job", out["contexts"])
		self.assertEqual(out["exception"]["values"][0]["value"], "[Filtered]")
		self.assertNotIn("vars", out["exception"]["values"][0]["stacktrace"]["frames"][0])
		self.assertTrue(out["release"])

	def test_message_only_event_gets_safe_logentry(self):
		out = sanitize_event({"logentry": {"message": "secret"}, "message": "secret"})
		self.assertEqual(out["logentry"]["message"], "Biozone error (details filtered)")
		self.assertNotIn("secret", json.dumps(out, ensure_ascii=False))

	def test_ensure_sentry_scrubber_is_idempotent(self):
		hub = sentry_sdk.Hub.current
		old = hub.client
		fake = FakeSentryClient()
		hub.bind_client(fake)
		try:
			ensure_sentry_scrubber()
			first = fake.options["before_send"]
			self.assertTrue(getattr(first, "_biozone_scrubber", False))
			ensure_sentry_scrubber()
			self.assertIs(fake.options["before_send"], first)
		finally:
			hub.bind_client(old)


def run():
	suite = unittest.TestLoader().loadTestsFromName(__name__)
	return unittest.TextTestRunner(verbosity=2).run(suite)
