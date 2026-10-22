import json
import uuid

import frappe


def get_request_id() -> str:
	request_id = getattr(frappe.local, "biozone_request_id", None)
	if not request_id:
		request_id = uuid.uuid4().hex
		frappe.local.biozone_request_id = request_id
	return request_id


def set_request_id() -> None:
	request_id = get_request_id()
	frappe.local.request_id = request_id
	if hasattr(frappe.local, "response"):
		frappe.local.response["request_id"] = request_id
	if hasattr(frappe.local, "response_headers"):
		frappe.local.response_headers["X-Request-Id"] = request_id
	try:
		import sentry_sdk

		if sentry_sdk.Hub.current.client:
			sentry_sdk.set_tag("request_id", request_id)
	except Exception:
		pass


def finalize_response(response=None, request=None) -> None:
	request_id = get_request_id()
	if response is None:
		return
	if hasattr(response, "headers"):
		response.headers["X-Request-Id"] = request_id
	if not getattr(response, "is_json", False) or getattr(response, "direct_passthrough", False):
		return
	try:
		payload = json.loads(response.get_data(as_text=True))
	except Exception:
		return
	if not isinstance(payload, dict):
		return
	if getattr(response, "status_code", 0) >= 500:
		payload = {
			"message": "تعذر إتمام الطلب، يرجى المحاولة مرة أخرى",
			"request_id": request_id,
		}
	elif "request_id" not in payload:
		payload["request_id"] = request_id
	response.set_data(json.dumps(payload, ensure_ascii=False))
