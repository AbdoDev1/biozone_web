"""P1 — استيراد الشيت تلقائيًا: مُثري الشيت ← مسار الاستيراد الرسمي.

الرفع = موافقة (قرار المالك 2026-11-27): لا معاينة مسبقة، ولا شيت يعود
للمستخدم. المخرَج: تشغيل `Biozone Item Import` + عدّادات + سبب لكل صف مستبعد.

الكتابة تمرّ عبر `import_export.run_import_job` حصرًا — لا مسار كتابة ثانٍ.
"""

import hashlib
import re

import frappe
from frappe import _

from biozone_web.import_export import (
	IMPORT_LOCK_TTL,
	IMPORT_QUEUE,
	IMPORT_TIMEOUT,
	MAX_UPLOAD_BYTES,
	_build_workbook,
	_get_run,
	_prior_completed_runs,
	run_import_job,
	run_validate_job,
)
from biozone_web.services import sheet_enrich

LOCK_KEY = "biozone_sheet_import_lock"
DONE_STATUSES = ("Completed", "Partial")


# ---------------------------------------------------------------------------
# المساعدة
# ---------------------------------------------------------------------------


def _file_hash(content):
	return hashlib.sha256(content).hexdigest()


def _attached_file(run_name):
	rows = frappe.get_all(
		"File",
		filters={
			"attached_to_doctype": "Biozone Item Import",
			"attached_to_name": run_name,
		},
		fields=["name"],
		order_by="creation desc",
		limit_page_length=1,
	)
	return rows[0]["name"] if rows else None


def _attach(run_name, filename, content):
	return (
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"{run_name}-{filename}",
				"content": content,
				"is_private": 1,
				"attached_to_doctype": "Biozone Item Import",
				"attached_to_name": run_name,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _excluded_summary(excluded):
	"""ملخص أسباب قصير: {السبب: العدد}."""
	reasons = {}
	for row in excluded:
		reason = row.get("reason") or "غير معروف"
		reasons[reason] = reasons.get(reason, 0) + 1
	return reasons


def _excluded_line(reasons, without_brand):
	"""سطر واحد للشات: «استبعدت 3: اسم فارغ (2) · سعر غير صالح (1)»."""
	parts = [f"{reason} ({count})" for reason, count in reasons.items()]
	text = ""
	if parts:
		text = f"استبعدت {sum(reasons.values())}: " + " · ".join(parts)
	if without_brand:
		text = (text + " · " if text else "") + f"{without_brand} بلا براند"
	return text


def _acquire_lock(run_name):
	return bool(
		frappe.cache().set(LOCK_KEY, run_name, ex=IMPORT_LOCK_TTL, nx=True)
	)


def _release_lock():
	frappe.cache().delete(LOCK_KEY)


# ---------------------------------------------------------------------------
# واجهة الخدمة
# ---------------------------------------------------------------------------


def check_repeat(content):
	"""بصمة الملف ← هل سبق استيراده بنجاح؟ (سؤال واحد في الشات)."""
	digest = _file_hash(content)
	prior = _prior_completed_runs(digest, None)
	return {
		"file_hash": digest,
		"repeat": bool(prior),
		"prior_runs": [
			{
				"name": p.name,
				"creation": str(p.creation),
				"requested_by": p.requested_by,
				"created": p.done_created,
				"updated": p.done_updated,
			}
			for p in prior
		],
	}


def start(content, filename, confirm_rerun=0):
	"""ينشئ التشغيل ويصفّ المهمة الخلفية. لا يكتب أي صنف هنا."""
	if not content:
		return {"ok": False, "error": _("الملف فارغ")}
	if len(content) > MAX_UPLOAD_BYTES:
		return {"ok": False, "error": _("حجم الملف يتجاوز الحد المسموح (5MB)")}

	digest = _file_hash(content)
	prior = _prior_completed_runs(digest, None)
	if prior and not frappe.utils.cint(confirm_rerun):
		return {
			"ok": False,
			"need_rerun_confirm": True,
			"file_hash": digest,
			"prior_runs": [
				{
					"name": p.name,
					"creation": str(p.creation),
					"created": p.done_created,
					"updated": p.done_updated,
				}
				for p in prior
			],
		}

	run = frappe.get_doc(
		{
			"doctype": "Biozone Item Import",
			"run_type": "Import",
			"status": "Uploaded",
			"file_name": filename or "sheet.xlsx",
			"file_hash": digest,
			"requested_by": frappe.session.user,
			"confirmed_rerun": 1 if frappe.utils.cint(confirm_rerun) else 0,
		}
	)
	run.insert(ignore_permissions=True)
	_attach(run.name, run.file_name, content)
	frappe.db.commit()

	frappe.enqueue(
		"biozone_web.services.sheet_import.run_sheet_job",
		queue=IMPORT_QUEUE,
		timeout=IMPORT_TIMEOUT,
		job_id=f"biozone-sheet-import-{run.name}",
		run_name=run.name,
	)
	return {"ok": True, "run_name": run.name}


def run_sheet_job(run_name):
	"""المهمة الخلفية: إثراء ← تحقق ← استيراد (كلها بالمسار الرسمي)."""
	run = _get_run(run_name)
	if run.status not in ("Uploaded", "Queued"):
		return

	if not _acquire_lock(run_name):
		run.db_set("status", "Failed")
		run.db_set("error", _("تشغيل استيراد آخر جارٍ الآن — أعد المحاولة بعد انتهائه"))
		frappe.db.commit()
		return

	try:
		run.db_set("status", "Validating")
		run.db_set("error", "")
		frappe.db.commit()

		file_name = _attached_file(run_name)
		if not file_name:
			raise frappe.ValidationError(_("ملف الشيت غير مرفق بهذا التشغيل"))
		content = frappe.get_doc("File", file_name).get_content()
		if isinstance(content, str):
			content = content.encode("utf-8")

		parsed = sheet_enrich.parse_any(content, run.file_name)
		index = sheet_enrich.load_catalog()
		enriched = sheet_enrich.enrich(parsed, index)

		if not enriched["rows"]:
			run.db_set("status", "Failed")
			run.db_set("total_rows", len(parsed["rows"]))
			run.db_set("error", _("لم أستورد شيئًا: كل الصفوف مستبعدة"))
			run.db_set(
				"warnings",
				_excluded_line(_excluded_summary(enriched["excluded"]), 0),
			)
			frappe.db.commit()
			return

		headers = sheet_enrich.standard_headers(enriched["discount_groups"])
		data_rows = [
			sheet_enrich.standard_row(row, enriched["discount_groups"])
			for row in enriched["rows"]
		]
		workbook = _build_workbook(
			headers,
			data_rows,
			[{"name": group} for group in enriched["discount_groups"]],
		)

		# استبدال الملف الخام بالشيت القياسي (داخلي فقط — لا يعود للمستخدم).
		# الامتداد يجب أن يكون xlsx: التحقق الرسمي يختار القارئ من الامتداد.
		base_name = re.sub(r"\.[^.]+$", "", run.file_name or "sheet")
		standard_name = _attach(run_name, f"{base_name}.xlsx", workbook)
		# التحقق الرسمي يقرأ الامتداد من file_name — يجب أن يطابق المحتوى.
		run.db_set("file_name", f"{base_name}.xlsx")
		if file_name and file_name != standard_name:
			frappe.delete_doc("File", file_name, ignore_permissions=True, force=True)

		reasons = _excluded_summary(enriched["excluded"])
		run.db_set("total_rows", len(parsed["rows"]))
		run.db_set("rows_to_create", enriched["stats"].get("created", 0))
		run.db_set("rows_to_update", enriched["stats"].get("updated", 0))
		frappe.db.commit()

		run_validate_job(run_name)

		# تحقق الاستيراد يعيد كتابة warnings — نضيف سطر الاستبعاد بعده.
		run.reload()
		line = _excluded_line(reasons, enriched["stats"].get("without_brand", 0))
		if line:
			existing = (run.warnings or "").strip()
			run.db_set("warnings", f"{existing}\n{line}".strip())
			frappe.db.commit()
			run.reload()

		if run.status != "Preview":
			return

		run.db_set("status", "Queued")
		frappe.db.commit()
		run_import_job(run_name)
	finally:
		_release_lock()


def status(run_name):
	"""حالة التشغيل للاستقصاء من الشات."""
	run = _get_run(run_name)
	return {
		"ok": True,
		"run_name": run.name,
		"status": run.status,
		"file_name": run.file_name,
		"total_rows": run.total_rows or 0,
		"to_create": run.rows_to_create or 0,
		"to_update": run.rows_to_update or 0,
		"created": run.done_created or 0,
		"updated": run.done_updated or 0,
		"failed": run.done_failed or 0,
		"warnings": run.warnings or "",
		"error": run.error or "",
		"done": run.status in DONE_STATUSES or run.status == "Failed",
	}


def last_import():
	"""ملخص آخر استيراد مكتمل (أمر «آخر استيراد»)."""
	rows = frappe.get_all(
		"Biozone Item Import",
		filters={"run_type": "Import", "status": ("in", DONE_STATUSES)},
		fields=[
			"name",
			"creation",
			"done_created",
			"done_updated",
			"done_failed",
			"requested_by",
		],
		order_by="creation desc",
		limit_page_length=1,
	)
	if not rows:
		return {"ok": True, "found": False}
	run = rows[0]
	return {
		"ok": True,
		"found": True,
		"run_name": run.name,
		"creation": str(run.creation),
		"created": run.done_created,
		"updated": run.done_updated,
		"failed": run.done_failed,
		"requested_by": run.requested_by,
	}
