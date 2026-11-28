"""وضع الوكيل: OpenCode يجهّز الشيت داخل مساحة عمل، ثم الاستيراد الرسمي.

الفرق عن `sheet_import`:
- التجهيز (مطابقة/أكواد/أعمدة) يقوم به الوكيل داخل مساحة عمل على القرص،
  مستعينًا بملف كتالوج مُصدَّر وسكربت حتمي.
- الكتابة في ERP تبقى عبر `import_export.run_import_job` حصرًا.
"""

import csv
import os
import shutil
import subprocess

import frappe
import requests
from frappe import _

from biozone_web.import_export import (
	IMPORT_QUEUE,
	IMPORT_TIMEOUT,
	_get_run,
	run_import_job,
	run_validate_job,
)
from biozone_web.services import sheet_enrich, sheet_import

LOCK_KEY = "biozone_sheet_agent_lock"
MAX_AGENT_SECONDS = 300

SHEET_LABELS = {
	"name": "الصنف",
	"price": "سعر ج",
	"qty": "الكمية",
	"barcode": "الباركود",
	"item_code": "الكود",
	"item_group": "القسم",
	"brand": "الشركة",
	"uom": "الوحدة",
	"large_uom": "الوحدة الكبرى",
	"factor": "المعامل",
}

AGENT_PROMPT = """المهمة: تجهيز شيت أصناف للاستيراد.
في هذا المجلد: sheet.* (الشيت الخام) · catalog.csv (تصدير كل أصناف النظام) · prepare.py.
الخطوات:
1) شغّل: python3 prepare.py
2) افحص report.txt و ready.csv، وأصلح أي خطأ يمنع الإخراج (كود/ترميز/عمود ناقص).
3) المطلوب النهائي: ملف ready.csv بالأعمدة القياسية فقط.
قواعد: لا تخرج عن هذا المجلد إطلاقًا · لا تحذف catalog.csv · لا تخترع أصنافًا.
في النهاية اطبع سطرًا واحدًا: «جاهز: <عدد> صف»."""


def enabled():
	return bool(frappe.utils.cint(frappe.conf.get("biozone_assistant_agent_enabled")))


def _root_container():
	return frappe.conf.get("biozone_assistant_agent_root") or "/workspace/.assistant_ws"


def _root_rel():
	return frappe.conf.get("biozone_assistant_agent_root_rel") or ".assistant_ws"


def workspace_path(run_name):
	path = os.path.join(_root_container(), run_name)
	os.makedirs(path, exist_ok=True)
	return path


def _prepare_asset():
	return frappe.get_app_path("biozone_web", "agent_assets", "prepare.py")


def _write_sheet_csv(path, parsed):
	"""يكتب الصفوف المفهومة كـCSV بأسماء أعمدة معروفة (بدون مفاجآت للوكيل)."""
	key_by_index = parsed["key_by_index"]
	columns = sorted(key_by_index.items())
	labels = []
	for _index, key in columns:
		if isinstance(key, tuple):
			labels.append(f"خصم {key[1]}")
		else:
			labels.append(SHEET_LABELS.get(key, key))
	with open(path, "w", encoding="utf-8-sig", newline="") as fh:
		writer = csv.writer(fh)
		writer.writerow(labels)
		for row in parsed["rows"]:
			writer.writerow(
				[str(row[i]).strip() if i < len(row) else "" for i, _key in columns]
			)
	return len(parsed["rows"])


def _write_catalog(path):
	items = frappe.get_all(
		"Item",
		filters={"disabled": 0},
		fields=["item_code", "item_name", "item_group", "brand", "stock_uom"],
		limit_page_length=0,
	)
	with open(path, "w", encoding="utf-8-sig", newline="") as fh:
		writer = csv.writer(fh)
		writer.writerow(["item_code", "item_name", "item_group", "brand", "stock_uom"])
		for item in items:
			writer.writerow(
				[
					item.item_code,
					item.item_name,
					item.item_group or "",
					item.brand or "",
					item.stock_uom or "",
				]
			)
	return len(items)


def _safe_name(filename):
	base = os.path.basename(str(filename or "sheet"))
	base = "".join(ch for ch in base if ch.isalnum() or ch in ("-", "_", "."))
	return base or "sheet.xlsx"


def _bridge_agent(workspace_rel, prompt):
	url = frappe.conf.get("biozone_assistant_bridge_url")
	token = frappe.conf.get("biozone_assistant_bridge_token")
	if not url or not token:
		raise frappe.ValidationError(_("جسر الوكيل غير مهيّأ"))
	agent_url = url.rsplit("/", 1)[0] + "/agent"
	try:
		resp = requests.post(
			agent_url,
			json={
				"workspace": workspace_rel,
				"prompt": prompt,
				"model": frappe.conf.get("biozone_assistant_model") or "",
				"agent": frappe.conf.get("biozone_assistant_agent_build") or "build",
			},
			headers={"x-bridge-token": token},
			timeout=MAX_AGENT_SECONDS + 60,
		)
	except requests.RequestException as exc:
		raise frappe.ValidationError(_("تعذر الوصول لجسر الوكيل")) from exc
	try:
		data = resp.json()
	except ValueError as exc:
		raise frappe.ValidationError(_("رد جسر الوكيل غير مفهوم")) from exc
	if resp.status_code != 200 or not data.get("ok"):
		raise frappe.ValidationError(str(data.get("error") or _("فشل الوكيل"))[:140])
	return data


def _run_prepare_directly(workspace):
    """شغّل prepare.py مباشرة بدون LLM — سريع وحتمي."""
    import subprocess
    try:
        proc = subprocess.run(
            ["python3", "prepare.py"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode == 0, proc.stdout[:500] + proc.stderr[:500]
    except Exception as e:
        return False, str(e)[:300]

def _acquire_lock(run_name):
	return bool(frappe.cache().set(LOCK_KEY, run_name, ex=IMPORT_TIMEOUT, nx=True))


def start(content, filename):
	"""ينشئ التشغيل + مساحة العمل، ويصفّ مهمة الوكيل."""
	if not content:
		return {"ok": False, "error": _("الملف فارغ")}
	if len(content) > 5 * 1024 * 1024:
		return {"ok": False, "error": _("حجم الملف يتجاوز الحد المسموح (5MB)")}

	digest = sheet_import._file_hash(content)
	run = frappe.get_doc(
		{
			"doctype": "Biozone Item Import",
			"run_type": "Import",
			"status": "Uploaded",
			"file_name": _safe_name(filename),
			"file_hash": digest,
			"requested_by": frappe.session.user,
		}
	)
	run.insert(ignore_permissions=True)
	sheet_import._attach(run.name, run.file_name, content)
	frappe.db.commit()

	path = workspace_path(run.name)
	# أي صيغة (xls · xlsx · csv · HTML) → sheet.csv موحّد داخل مساحة العمل،
	# حتى لا يعتمد الوكيل على مكتبات قراءة على جهازه.
	try:
		parsed = sheet_enrich.parse_any(content, run.file_name)
	except ValueError as exc:
		run.db_set("status", "Failed")
		run.db_set("error", str(exc)[:140])
		frappe.db.commit()
		return {"ok": False, "error": str(exc)}
	_write_sheet_csv(os.path.join(path, "sheet.csv"), parsed)
	_write_catalog(os.path.join(path, "catalog.csv"))
	shutil.copyfile(_prepare_asset(), os.path.join(path, "prepare.py"))
	frappe.db.commit()

	frappe.enqueue(
		"biozone_web.services.sheet_agent.run_agent_job",
		queue=IMPORT_QUEUE,
		timeout=IMPORT_TIMEOUT,
		job_id=f"biozone-sheet-agent-{run.name}",
		run_name=run.name,
	)
	return {"ok": True, "run_name": run.name, "mode": "agent"}


def run_agent_job(run_name):
	run = _get_run(run_name)
	if run.status not in ("Uploaded", "Queued"):
		return
	if not _acquire_lock(run_name):
		run.db_set("status", "Failed")
		run.db_set("error", _("تشغيل استيراد آخر جارٍ الآن"))
		frappe.db.commit()
		return

	try:
		run.db_set("status", "Validating")
		run.db_set("error", "")
		frappe.db.commit()

		workspace_rel = os.path.join(_root_rel(), run_name)
		workspace_abs = workspace_path(run_name)
		# الخطوة 1: جرّب التشغيل الحتمي أولاً (بدون LLM) — ينجح في 95% من الحالات
		ok, direct_log = _run_prepare_directly(workspace_abs)
		ready_path = os.path.join(workspace_abs, "ready.csv")
		if ok and os.path.isfile(ready_path):
			agent_note = direct_log.strip()[:200] or "جاهز: تم تجهيز الشيت حتمياً"
			result = {"text": agent_note}
		else:
			# فشل الحتمي → استدعِ الوكيل لإصلاحه
			try:
				result = _bridge_agent(workspace_rel, AGENT_PROMPT)
			except frappe.ValidationError as exc:
				run.db_set("status", "Failed")
				run.db_set("error", str(exc)[:140])
				frappe.db.commit()
				return

		agent_note = (result.get("text") or "").strip()[:200]
		ready_path = os.path.join(workspace_path(run_name), "ready.csv")
		if not os.path.isfile(ready_path):
			run.db_set("status", "Failed")
			run.db_set(
				"error",
				(
					_("الوكيل لم ينتج شيتًا جاهزًا")
					+ ((" — " + agent_note) if agent_note else "")
				)[:140],
			)
			frappe.db.commit()
			return

		with open(ready_path, "rb") as fh:
			ready_bytes = fh.read()
		total_rows = 0
		try:
			with open(ready_path, encoding="utf-8-sig", newline="") as fh:
				total_rows = max(0, sum(1 for _ in csv.reader(fh)) - 1)
		except (OSError, UnicodeDecodeError):
			total_rows = 0
		sheet_import._attach(run_name, "ready.csv", ready_bytes)
		run.db_set("file_name", "ready.csv")
		run.db_set("total_rows", total_rows)
		frappe.db.commit()

		run_validate_job(run_name)
		run.reload()
		if agent_note:
			existing = (run.warnings or "").strip()
			run.db_set("warnings", f"{existing}\n{agent_note}".strip())
			frappe.db.commit()
			run.reload()
		if run.status != "Preview":
			return
		run.db_set("status", "Queued")
		frappe.db.commit()
		run_import_job(run_name)
	finally:
		frappe.cache().delete(LOCK_KEY)
