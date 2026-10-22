import copy
import os
import re

import frappe

from biozone_web import __version__

REDACTED = "[Filtered]"
SENSITIVE_KEY = re.compile(
	r"(pass(word|wd)?|secret|token|csrf|api[_-]?key|authorization|cookie|"
	r"session|sid|email|phone|mobile|address|card|iban|otp)",
	re.IGNORECASE,
)
DROP_CONTEXTS = {"json body", "form data", "request", "response"}


def _scrub(value, key=None, depth=0):
	if depth > 8:
		return REDACTED
	if key is not None and SENSITIVE_KEY.search(str(key)):
		return REDACTED
	if isinstance(value, dict):
		return {k: _scrub(v, k, depth + 1) for k, v in value.items()}
	if isinstance(value, (list, tuple)):
		return [_scrub(item, key, depth + 1) for item in value]
	return value


def _release() -> str:
	return os.getenv("BIOZONE_SENTRY_RELEASE") or f"biozone_web@{__version__}"


def sanitize_event(event: dict) -> dict:
	event = copy.deepcopy(event)
	event["release"] = _release()
	had_logentry = "logentry" in event or "message" in event
	event.pop("user", None)
	event.pop("breadcrumbs", None)
	event.pop("extra", None)
	event.pop("logentry", None)
	event.pop("message", None)

	request = event.get("request")
	if isinstance(request, dict):
		for key in ("data", "query_string", "cookies", "env", "headers"):
			request.pop(key, None)
		if isinstance(request.get("url"), str):
			request["url"] = request["url"].split("?", 1)[0]

	contexts = event.get("contexts")
	if isinstance(contexts, dict):
		for key in list(contexts):
			lower = str(key).lower()
			if lower in DROP_CONTEXTS or "body" in lower or "form" in lower:
				contexts.pop(key, None)
		job = contexts.get("job")
		if isinstance(job, dict):
			job.pop("kwargs", None)
		event["contexts"] = _scrub(contexts)

	tags = event.get("tags")
	if isinstance(tags, dict):
		tags.pop("frappe_user", None)
		event["tags"] = _scrub(tags)

	for exception in (event.get("exception") or {}).get("values") or []:
		exception["value"] = REDACTED
		for frame in (exception.get("stacktrace") or {}).get("frames") or []:
			frame.pop("vars", None)

	if had_logentry and not event.get("exception"):
		event["logentry"] = {"message": "Biozone error (details filtered)"}

	return event


def ensure_sentry_scrubber(*args, **kwargs) -> None:
	try:
		import sentry_sdk

		client = getattr(sentry_sdk.Hub.current, "client", None)
		if not client:
			return
		options = client.options
		current = options.get("before_send")
		if getattr(current, "_biozone_scrubber", False):
			return

		def before_send(event, hint):
			if current:
				event = current(event, hint)
			if event is None:
				return None
			return sanitize_event(event)

		before_send._biozone_scrubber = True
		options["before_send"] = before_send
	except Exception:
		frappe.logger().warning("Failed to install Sentry scrubber", exc_info=True)
