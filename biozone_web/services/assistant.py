"""مساعد الستاف — حلقة أدوات على مزوّد متوافق مع OpenAI.

الثوابت: الوكيل يخطّط فقط · الأدوات هي الوحيدة التي تلمس البيانات ·
كل أداة تُرجع JSON صغيرًا (≤ ~500 بايت) ولا تُرجع قوائم أصناف أبدًا.
"""

import json
import re

import frappe
import requests
from frappe import _

from biozone_web.services import sheet_import

DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash-free"
MAX_ROUNDS = 3
MAX_TOKENS = 700
TIMEOUT = 45
BRIDGE_TIMEOUT = 180
MAX_HISTORY = 8
MAX_CHARS = 600
RUN_NAME_RE = re.compile(r"^[A-Za-z0-9]{6,20}$")

# الشات: دور واحد فقط (قرار المالك 2026-11-27) — Administrator يبقى مستثنى.
# الإعدادات/المفتاح: MANAGE_ROLES أدناه.
ASSISTANT_ROLES = ("Biozone Assistant User",)

SYSTEM_PROMPT = """أنت مساعد ستاف متجر Biozone على ERPNext. تتكلم عربي قصير ومباشر.
قواعدك الثابتة:
- لا تخترع أي رقم أو اسم. كل رقم يصل إليك من أداة فقط.
- أقصى رد 5 أسطر. بلا تعداد طويل وبلا شرح تقني.
- مهمتك الآن: استيراد شيتات الأصناف وقراءة حالتها وتقرير المخزون.
- عند وصول شيت: استخدم import_status وقل النتيجة في سطر واحد + الرابط.
- لو المطلوب خارج أدواتك: قل «لا أستطيع هذا بعد».
- لا تطلب من الموظف تفاصيل تقنية (ورقة/ترويسة/ترميز/أعمدة)."""


class AssistantError(Exception):
	def __init__(self, message, code="ASSISTANT_UNAVAILABLE"):
		self.message = message
		self.code = code
		super().__init__(message)


# ---------------------------------------------------------------------------
# الصلاحية والإعداد
# ---------------------------------------------------------------------------


def assert_can_use_assistant():
	"""بوابة دور صريحة فوق require_staff_access (نمط item_images.py:63)."""
	from biozone_web.utils import require_staff_access

	require_staff_access()
	if frappe.session.user == "Administrator":
		return
	if set(ASSISTANT_ROLES).isdisjoint(frappe.get_roles(frappe.session.user)):
		frappe.throw(_("غير مصرح باستخدام المساعد"), frappe.PermissionError)


def is_enabled():
	return bool(frappe.utils.cint(frappe.conf.get("biozone_assistant_enabled")))


def _providers():
	"""قائمة مزوّدين بالترتيب: المجاني أولًا ثم البديل (بلا مفاتيح في الكود)."""
	raw = frappe.conf.get("biozone_assistant_providers")
	if raw:
		try:
			providers = json.loads(raw) if isinstance(raw, str) else raw
			if isinstance(providers, list) and providers:
				return providers
		except ValueError:
			frappe.log_error(title="assistant providers json", message=str(raw)[:200])
	return [
		{
			"base_url": frappe.conf.get("biozone_assistant_base_url") or DEFAULT_BASE_URL,
			"api_key": frappe.conf.get("biozone_assistant_api_key"),
			"model": frappe.conf.get("biozone_assistant_model") or DEFAULT_MODEL,
		}
	]


def _post_chat(provider, payload):
	url = (provider.get("base_url") or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"
	key = provider.get("api_key")
	if not key:
		raise AssistantError(_("المساعد غير مهيّأ (لا يوجد مفتاح)"), "ASSISTANT_NOT_CONFIGURED")
	headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
	try:
		resp = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
	except requests.RequestException as exc:
		raise AssistantError(_("تعذر الوصول للنموذج"), "ASSISTANT_UNAVAILABLE") from exc
	if resp.status_code != 200:
		# 402/429 = حصة/رصيد — نجرّب المزوّد التالي بدل إفشال المحادثة.
		code = "ASSISTANT_QUOTA" if resp.status_code in (402, 429) else "ASSISTANT_ERROR"
		raise AssistantError(_("النموذج غير متاح الآن"), code)
	try:
		return resp.json()
	except ValueError as exc:
		raise AssistantError(_("رد النموذج غير مفهوم"), "ASSISTANT_BAD_RESPONSE") from exc


def _chat(messages, tools):
	"""أول مزوّد يرد بنجاح — والباقي بديل عند الحصة/الرصيد."""
	last = None
	cache = frappe.cache()
	for provider in _providers():
		model = provider.get("model") or DEFAULT_MODEL
		down_key = f"biozone_assistant_down::{model}"
		if cache.get(down_key):
			continue
		payload = {
			"model": model,
			"messages": messages,
			"tools": tools,
			"tool_choice": "auto",
			"max_tokens": MAX_TOKENS,
		}
		try:
			return _post_chat(provider, payload)
		except AssistantError as exc:
			last = exc
			if exc.code in ("ASSISTANT_QUOTA", "ASSISTANT_ERROR"):
				# لا نعيد اختبار مزوّد مستنفد في كل رسالة (10 دقائق كافية).
				cache.set(down_key, 1, ex=600)
				continue
			raise
	raise last or AssistantError(_("لا يوجد مزوّد متاح"))


# ---------------------------------------------------------------------------
# الأدوات — كل واحدة ≤ 500 بايت
# ---------------------------------------------------------------------------


def _owned_run(run_name):
	if not isinstance(run_name, str) or not RUN_NAME_RE.match(run_name):
		return None
	return frappe.db.get_value(
		"Biozone Item Import",
		{"name": run_name, "requested_by": frappe.session.user},
		"name",
	)


def tool_import_status(run_name=None):
	if run_name and not _owned_run(run_name):
		return {"error": "not_found"}
	if not run_name:
		last = sheet_import.last_import()
		if not last.get("found"):
			return {"found": False}
		return {
			"run_name": last["run_name"],
			"created": last["created"],
			"updated": last["updated"],
			"failed": last["failed"],
		}
	data = sheet_import.status(run_name)
	return {
		"status": data["status"],
		"total": data["total_rows"],
		"created": data["created"] or data["to_create"],
		"updated": data["updated"] or data["to_update"],
		"failed": data["failed"],
		"warnings": data["warnings"][:200],
		"error": data["error"][:120],
		"done": data["done"],
	}


def tool_last_import():
	data = sheet_import.last_import()
	if not data.get("found"):
		return {"found": False}
	return {
		"run_name": data["run_name"],
		"date": data["creation"][:10],
		"created": data["created"],
		"updated": data["updated"],
		"failed": data["failed"],
	}


def tool_stock_report_link():
	return {"url": "/staff/stock"}


def tool_help():
	return {
		"can": ["استيراد شيت", "حالة آخر استيراد", "رابط تقرير المخزون"],
		"cannot": ["تعديل أسعار", "حذف أصناف", "بيانات عملاء"],
	}


TOOL_IMPLS = {
	"import_status": tool_import_status,
	"last_import": tool_last_import,
	"stock_report_link": tool_stock_report_link,
	"help": tool_help,
}

TOOLS_SCHEMA = [
	{
		"type": "function",
		"function": {
			"name": "import_status",
			"description": "حالة شيت استيراد (أو آخر استيراد لو بلا معامل)",
			"parameters": {
				"type": "object",
				"properties": {
					"run_name": {"type": "string", "description": "معرّف التشغيل إن وُجد"}
				},
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "last_import",
			"description": "ملخص آخر استيراد مكتمل",
			"parameters": {"type": "object", "properties": {}},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "stock_report_link",
			"description": "رابط تقرير المخزون",
			"parameters": {"type": "object", "properties": {}},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "help",
			"description": "ما يستطيع وما لا يستطيع",
			"parameters": {"type": "object", "properties": {}},
		},
	},
]


# ---------------------------------------------------------------------------
# الحلقة
# ---------------------------------------------------------------------------


def _clean_history(history):
	out = []
	if not isinstance(history, list):
		return out
	for item in history[-MAX_HISTORY:]:
		if not isinstance(item, dict):
			continue
		role = item.get("role")
		content = str(item.get("content") or "").strip()[:MAX_CHARS]
		if role in ("user", "assistant") and content:
			out.append({"role": role, "content": content})
	return out


def _execute_tool(name, args):
	impl = TOOL_IMPLS.get(name)
	if not impl:
		return {"error": "unknown_tool"}
	if not isinstance(args, dict):
		args = {}
	try:
		return impl(**{k: v for k, v in args.items() if k in ("run_name",)})
	except TypeError:
		return impl()
	except Exception as exc:
		frappe.log_error(title="assistant tool failed", message=f"{name}: {exc}")
		return {"error": "tool_failed"}


def _backend():
	"""api = مزوّد OpenAI-compatible · bridge = OpenCode محلي (الموديل المجاني)."""
	return str(frappe.conf.get("biozone_assistant_backend") or "api").strip().lower()


def _context_block(run_name, history):
	lines = []
	if run_name and _owned_run(run_name):
		st = sheet_import.status(run_name)
		lines.append(
			"حالة الشيت الحالي: "
			+ json.dumps(
				{
					"status": st["status"],
					"new": st["created"] or st["to_create"],
					"updated": st["updated"] or st["to_update"],
					"failed": st["failed"],
					"warnings": st["warnings"][:150],
				},
				ensure_ascii=False,
			)
		)
	else:
		last = sheet_import.last_import()
		if last.get("found"):
			lines.append(
				"آخر استيراد: "
				+ json.dumps(
					{
						"date": last["creation"][:10],
						"new": last["created"],
						"updated": last["updated"],
						"failed": last["failed"],
					},
					ensure_ascii=False,
				)
			)
	lines.append("رابط تقرير المخزون: /staff/stock (اذكره حرفيًا كما هو).")
	recent = [
		f"{item['role']}: {item['content'][:120]}"
		for item in _clean_history(history or [])[-4:]
	]
	if recent:
		lines.append("آخر رسائل:\n" + "\n".join(recent))
	return "\n".join(lines)


def _bridge_reply(text, history, run_name, session=None, model=None):
	url = frappe.conf.get("biozone_assistant_bridge_url")
	token = frappe.conf.get("biozone_assistant_bridge_token")
	if not url or not token:
		raise AssistantError(_("المساعد المحلي غير مهيّأ"), "ASSISTANT_NOT_CONFIGURED")
	payload = {
		"message": text,
		"context": _context_block(run_name, history),
		"model": model or frappe.conf.get("biozone_assistant_model") or "",
		"agent": frappe.conf.get("biozone_assistant_agent") or "plan",
	}
	if session:
		payload["session"] = session
	try:
		resp = requests.post(
			url, json=payload, headers={"x-bridge-token": token}, timeout=BRIDGE_TIMEOUT
		)
	except requests.RequestException as exc:
		raise AssistantError(_("تعذر الوصول للمساعد المحلي"), "ASSISTANT_UNAVAILABLE") from exc
	try:
		data = resp.json()
	except ValueError as exc:
		raise AssistantError(_("رد المساعد غير مفهوم"), "ASSISTANT_BAD_RESPONSE") from exc
	if resp.status_code != 200 or not data.get("ok"):
		raise AssistantError(
			str(data.get("error") or _("المساعد المحلي غير متاح الآن"))[:160],
			"ASSISTANT_UNAVAILABLE",
		)
	return {"ok": True, "reply": data.get("text") or _("لا أستطيع هذا بعد"), "session_id": data.get("session_id")}


def reply(text, history=None, run_name=None, session=None, model=None):
	"""رسالة موظف → رد المساعد (مع تنفيذ الأدوات المطلوبة)."""
	if not is_enabled():
		raise AssistantError(_("المساعد غير مفعّل"), "ASSISTANT_DISABLED")
	text = str(text or "").strip()[:MAX_CHARS]
	if not text:
		raise AssistantError(_("اكتب رسالة أولًا"), "ASSISTANT_EMPTY")
	if _backend() == "bridge":
		return _bridge_reply(text, history, run_name, session=session, model=model)

	messages = [{"role": "system", "content": SYSTEM_PROMPT}]
	messages.extend(_clean_history(history))
	if run_name and _owned_run(run_name):
		messages.append(
			{
				"role": "system",
				"content": f"الشيت الحالي في المحادثة: run_name={run_name}. استخدمه في import_status.",
			}
		)
	messages.append({"role": "user", "content": text})

	for _round in range(MAX_ROUNDS):
		data = _chat(messages, TOOLS_SCHEMA)
		choices = data.get("choices") or []
		message = (choices[0].get("message") if choices else None) or {}
		calls = message.get("tool_calls") or []
		if not calls:
			content = (message.get("content") or "").strip()
			return {"ok": True, "reply": content or _("لا أستطيع هذا بعد")}
		messages.append(
			{
				"role": "assistant",
				"content": message.get("content") or "",
				"tool_calls": calls,
			}
		)
		for call in calls:
			fn = (call.get("function") or {}).get("name")
			raw = (call.get("function") or {}).get("arguments") or "{}"
			try:
				args = json.loads(raw) if isinstance(raw, str) else (raw or {})
			except ValueError:
				args = {}
			result = _execute_tool(fn, args)
			messages.append(
				{
					"role": "tool",
					"tool_call_id": call.get("id"),
					"content": json.dumps(result, ensure_ascii=False, default=str),
				}
			)

	return {"ok": True, "reply": _("تم — راجع تقرير المخزون.")}


# ---------------------------------------------------------------------------
# الإعدادات (شاشة التحكم) — للمدير فقط
# ---------------------------------------------------------------------------

MANAGE_ROLES = (
	"System Manager",
	"Biozone Assistant Admin",
	"Biozone Assistant Manager",
)
MAX_PROVIDERS = 5


def assert_can_manage():
	from biozone_web.utils import require_staff_access

	require_staff_access()
	if frappe.session.user == "Administrator":
		return
	if set(MANAGE_ROLES).isdisjoint(frappe.get_roles(frappe.session.user)):
		frappe.throw(_("غير مصرح بإدارة إعدادات المساعد"), frappe.PermissionError)


def can_manage():
	try:
		assert_can_manage()
		return True
	except Exception:
		return False


def _mask(key):
	key = str(key or "")
	return ("••••" + key[-4:]) if key else ""


def _bridge_config():
	url = frappe.conf.get("biozone_assistant_bridge_url")
	token = frappe.conf.get("biozone_assistant_bridge_token")
	if not url or not token:
		return None
	try:
		resp = requests.get(
			url.rsplit("/", 1)[0] + "/config",
			headers={"x-bridge-token": token},
			timeout=15,
		)
		return resp.json()
	except (requests.RequestException, ValueError):
		return None


def get_settings():
	providers = []
	for provider in _providers():
		providers.append(
			{
				"base_url": provider.get("base_url") or "",
				"model": provider.get("model") or "",
				"api_key_masked": _mask(provider.get("api_key")),
				"has_key": bool(provider.get("api_key")),
			}
		)
	if _backend() == "bridge":
		bridge = _bridge_config()
		if bridge and bridge.get("ok"):
			cfg = bridge.get("config") or {}
			return {
				"enabled": is_enabled(),
				"backend": "bridge",
				"model": cfg.get("model") or DEFAULT_MODEL,
				"agent": cfg.get("agent_chat") or "plan",
				"fallback": cfg.get("fallback") or [],
				"providers": providers,
			}
	return {
		"enabled": is_enabled(),
		"backend": _backend(),
		"model": frappe.conf.get("biozone_assistant_model") or DEFAULT_MODEL,
		"agent": frappe.conf.get("biozone_assistant_agent") or "plan",
		"providers": providers,
	}


def _clear_down_cache(models=None):
	cache = frappe.cache()
	if models is None:
		models = [p.get("model") for p in _providers()] + [
			frappe.conf.get("biozone_assistant_model") or DEFAULT_MODEL
		]
	for model in {m for m in models if m}:
		cache.delete(f"biozone_assistant_down::{model}")


def save_settings(providers, enabled=1, backend=None, model=None, agent=None, fallback=None):
	"""يكتب المزوّدين في site_config. المفتاح الفارغ = أبقِ القديم بنفس الترتيب."""
	if not isinstance(providers, list):
		raise AssistantError(_("صيغة الإعدادات غير صحيحة"), "ASSISTANT_BAD_SETTINGS")
	current = _providers()
	cleaned = []
	for index, provider in enumerate(providers[:MAX_PROVIDERS]):
		if not isinstance(provider, dict):
			continue
		base_url = str(provider.get("base_url") or "").strip().rstrip("/")
		model = str(provider.get("model") or "").strip()
		api_key = str(provider.get("api_key") or "").strip()
		if not base_url and not model:
			continue
		if not base_url.startswith("https://"):
			raise AssistantError(_("رابط المزوّد يجب أن يبدأ بـ https://"), "ASSISTANT_BAD_SETTINGS")
		if not model:
			raise AssistantError(_("اسم الموديل مطلوب"), "ASSISTANT_BAD_SETTINGS")
		if not api_key and index < len(current):
			api_key = current[index].get("api_key") or ""
		if not api_key:
			raise AssistantError(_("مفتاح المزوّد مطلوب"), "ASSISTANT_BAD_SETTINGS")
		cleaned.append({"base_url": base_url, "model": model, "api_key": api_key})
	backend = str(backend or _backend()).strip().lower()
	if backend not in ("bridge", "api"):
		raise AssistantError(_("وضع تشغيل غير معروف"), "ASSISTANT_BAD_SETTINGS")
	if backend == "api" and not cleaned:
		raise AssistantError(_("أضف مزوّدًا واحدًا على الأقل"), "ASSISTANT_BAD_SETTINGS")

	from frappe.installer import update_site_config

	if _backend() == "bridge" or backend == "bridge":
		# الموديل يملكه الجسر — نكتبه هناك مباشرة
		url = frappe.conf.get("biozone_assistant_bridge_url")
		token = frappe.conf.get("biozone_assistant_bridge_token")
		if url and token:
			try:
				requests.post(
					url.rsplit("/", 1)[0] + "/config",
					json={"model": model, "fallback": fallback, "agent_chat": agent or "plan"} if (model is not None or fallback is not None) else {},
					headers={"x-bridge-token": token},
					timeout=15,
				)
			except requests.RequestException:
				pass
		update_site_config("biozone_assistant_backend", "bridge")
		update_site_config("biozone_assistant_enabled", 1 if frappe.utils.cint(enabled) else 0)
		frappe.conf["biozone_assistant_backend"] = "bridge"
		frappe.conf["biozone_assistant_enabled"] = 1 if frappe.utils.cint(enabled) else 0
		if model is not None:
			frappe.conf["biozone_assistant_model"] = str(model).strip()
		return get_settings()

	if cleaned:
		update_site_config("biozone_assistant_providers", cleaned)
	update_site_config("biozone_assistant_backend", backend)
	update_site_config("biozone_assistant_enabled", 1 if frappe.utils.cint(enabled) else 0)
	if model is not None:
		update_site_config("biozone_assistant_model", str(model).strip())
	if agent:
		update_site_config("biozone_assistant_agent", str(agent).strip())
	# يسري فورًا في العملية الحالية بلا إعادة تشغيل.
	frappe.conf.update(
		{
			"biozone_assistant_backend": backend,
			"biozone_assistant_enabled": 1 if frappe.utils.cint(enabled) else 0,
		}
	)
	if model is not None:
		frappe.conf["biozone_assistant_model"] = str(model).strip()
	if agent:
		frappe.conf["biozone_assistant_agent"] = str(agent).strip()
	if cleaned:
		frappe.conf["biozone_assistant_providers"] = cleaned
	_clear_down_cache([p["model"] for p in cleaned])
	return get_settings()


def list_models():
	"""قائمة الموديلات من OpenCode المحلي (لشاشة الإعدادات)."""
	url = frappe.conf.get("biozone_assistant_bridge_url")
	token = frappe.conf.get("biozone_assistant_bridge_token")
	if not url or not token:
		return {"ok": False, "models": [], "error": _("الجسر غير مهيّأ")}
	try:
		resp = requests.post(
			url.rsplit("/", 1)[0] + "/models",
			json={},
			headers={"x-bridge-token": token},
			timeout=60,
		)
		data = resp.json()
	except (requests.RequestException, ValueError):
		return {"ok": False, "models": [], "error": _("تعذر جلب الموديلات")}
	return {"ok": bool(data.get("ok")), "models": data.get("models") or []}


def test_provider(index=0):
	"""اختبار اتصال حقيقي بمزوّد واحد (بلا أدوات) — يعيد النتيجة فقط."""
	if _backend() == "bridge":
		started = frappe.utils.now_datetime()
		result = _bridge_reply("قل كلمة واحدة: تم", [], None)
		seconds = (frappe.utils.now_datetime() - started).total_seconds()
		return {
			"ok": True,
			"model": "opencode (local bridge)",
			"seconds": round(seconds, 2),
			"reply": (result.get("reply") or "")[:60],
		}

	providers = _providers()
	try:
		provider = providers[int(index)]
	except (IndexError, ValueError, TypeError):
		raise AssistantError(_("مزوّد غير موجود"), "ASSISTANT_BAD_SETTINGS")
	payload = {
		"model": provider.get("model") or DEFAULT_MODEL,
		"messages": [{"role": "user", "content": "قل: تم"}],
		"max_tokens": 8,
	}
	started = frappe.utils.now_datetime()
	data = _post_chat(provider, payload)
	seconds = (frappe.utils.now_datetime() - started).total_seconds()
	choices = data.get("choices") or []
	reply = ((choices[0].get("message") or {}).get("content") if choices else "") or ""
	return {
		"ok": True,
		"model": payload["model"],
		"seconds": round(seconds, 2),
		"reply": reply.strip()[:60],
	}
