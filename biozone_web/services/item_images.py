"""Item image service: single image per Item, staff-managed upload.

- One image per Item via the standard `Item.image` field (no schema
  change). Display surfaces use the thumbnail; the full original never
  appears in listings.
- Validation is content-based (Pillow): 5MB cap, {JPEG, PNG, WEBP},
  4000px per side, 16MP total. Extension/MIME from the request are
  never trusted.
- Flow: save File (public, attached to Item) -> optimize_image() ->
  make_thumbnail() -> set Item.image -> race-safe delete of the
  predecessor. Cleanup matrix:
    * original upload fails -> nothing persisted, throw;
    * thumbnail fails -> delete the new File, keep the old image, throw;
    * Item update fails -> delete the new File, throw;
    * old-file delete fails after success -> SUCCESS + warning log,
      orphan left for the missing-image health check.
- Deletion never touches /assets (placeholder guard) and never deletes
  a file still referenced as current.
- No commit inside — the caller keeps its single commit.
"""

import io
import os
import re
import uuid

import frappe
from frappe import _
from PIL import Image

from biozone_web.utils import require_staff_access

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_SIDE = 4000
MAX_IMAGE_PIXELS = 16_000_000
ALLOWED_PIL_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
THUMB_WIDTH = 300
THUMB_HEIGHT = 300

IMAGE_MANAGER_ROLES = frozenset({"System Manager", "Item Manager", "Stock Manager", "Biozone Stock Staff"})

LOG = "biozone_images"

UNKNOWN_ITEM_MESSAGE = _("الصنف غير موجود")
SIZE_MESSAGE = _("حجم الصورة يتجاوز الحد المسموح (5 ميجا)")
TYPE_MESSAGE = _("صيغة الصورة غير مدعومة (المسموح: JPG وPNG وWEBP فقط)")
DIMS_MESSAGE = _("أبعاد الصورة تتجاوز الحد المسموح")
CORRUPT_MESSAGE = _("ملف الصورة تالف أو ليس صورة حقيقية")
OPTIMIZE_MESSAGE = _("تعذر معالجة الصورة")


def assert_can_manage_item_images():
	"""Staff-only gate, tightened beyond the generic staff guard.

	Any enabled System User passes require_staff_access (even role-less
	ones, proven live) — image writes additionally require one of the
	pinned roles. Single-company setup: no per-company scoping in v1.
	"""
	require_staff_access()
	if IMAGE_MANAGER_ROLES.isdisjoint(frappe.get_roles(frappe.session.user)):
		frappe.throw(_("غير مصرح بإدارة صور الأصناف"), frappe.PermissionError)


def _validate_image_bytes(content):
	"""Content-based validation. Returns (ext, width, height).

	Extension/MIME from the request are never trusted: Pillow opens,
	verifies, and re-opens (verify consumes), and only its reported
	format decides.
	"""
	if not content or len(content) > MAX_IMAGE_BYTES:
		frappe.throw(SIZE_MESSAGE)
	try:
		probe = Image.open(io.BytesIO(content))
		probe.verify()
	except Exception:
		frappe.throw(CORRUPT_MESSAGE)
	try:
		image = Image.open(io.BytesIO(content))
		image.load()
	except Exception:
		frappe.throw(CORRUPT_MESSAGE)
	fmt = (image.format or "").upper()
	if fmt not in ALLOWED_PIL_FORMATS:
		frappe.throw(TYPE_MESSAGE)
	width, height = image.size
	if (
		width <= 0
		or height <= 0
		or width > MAX_IMAGE_SIDE
		or height > MAX_IMAGE_SIDE
		or width * height > MAX_IMAGE_PIXELS
	):
		frappe.throw(DIMS_MESSAGE)
	return ALLOWED_PIL_FORMATS[fmt], width, height


def _safe_filename(item_code, ext):
	"""Server-generated unique name: no user-controlled path survives."""
	stem = re.sub(r"[^A-Za-z0-9_.-]", "_", (item_code or "item"))[:60] or "item"
	return f"{stem}-{uuid.uuid4().hex[:8]}.{ext}"


def _file_doc_by_url(file_url):
	rows = frappe.get_all("File", filters={"file_url": file_url}, fields=["name"], limit=1)
	return rows[0]["name"] if rows else None


def _delete_file_doc_quietly(file_name, context):
	"""Best-effort file-doc delete. Never throws: failure is logged and
	the orphan stays visible to the missing-image health check."""
	try:
		frappe.delete_doc("File", file_name, ignore_permissions=True)
		return True
	except Exception:
		frappe.logger(LOG).warning("item image orphan: %s (%s)", file_name, context)
		return False


def _delete_predecessor(item_code, old_url, new_name):
	"""Race-safe predecessor delete: only a file that is no longer the
	current value, resolved fresh, outside /assets, and not the new doc.
	"""
	if not old_url or not old_url.startswith("/files/"):
		return False
	current = frappe.db.get_value("Item", item_code, "image") or ""
	if not current or current == old_url:
		return False
	old_name = _file_doc_by_url(old_url)
	if not old_name or old_name == new_name:
		return False
	return _delete_file_doc_quietly(old_name, f"predecessor of {item_code}")


def staff_upload_item_image(item_code, content):
	"""Save a verified image as the item's single image. Returns
	{"ok", "image", "thumbnail", "replaced"}.

	Strict order: File insert -> optimize -> thumbnail (falsy fails the
	whole op with the new file cleaned and the old image kept) -> Item
	update -> predecessor delete (failure logged, op still succeeds).
	"""
	if not item_code or not frappe.db.exists("Item", item_code):
		frappe.throw(UNKNOWN_ITEM_MESSAGE)
	ext, _width, _height = _validate_image_bytes(content)
	old_url = frappe.db.get_value("Item", item_code, "image") or ""

	new_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": _safe_filename(item_code, ext),
			"is_private": 0,
			"attached_to_doctype": "Item",
			"attached_to_name": item_code,
			# إلزامي: خطاف الإطار attach_files_to_document (يعمل على كل حفظ)
			# يطابق بهذا الحقل — بدونه ينشئ مستندًا توأمًا مكررًا عند حفظ الصنف.
			"attached_to_field": "image",
			"content": content,
		}
	)
	new_doc.insert(ignore_permissions=True)
	# Framework may rewrite file_url on save (dedup/uniquify): reload so
	# every later step uses the canonical DB value, never a stale copy.
	new_doc.reload()
	try:
		new_doc.optimize_file()
	except Exception:
		_delete_file_doc_quietly(new_doc.name, f"optimize failed for {item_code}")
		frappe.throw(OPTIMIZE_MESSAGE)
	new_doc.reload()
	made_thumbnail = new_doc.make_thumbnail(width=THUMB_WIDTH, height=THUMB_HEIGHT)
	fresh = frappe.db.get_value("File", new_doc.name, ["file_url", "thumbnail_url"], as_dict=True) or {}
	file_url = fresh.get("file_url") or ""
	thumbnail_url = fresh.get("thumbnail_url") or ""
	if not made_thumbnail or not thumbnail_url:
		_delete_file_doc_quietly(new_doc.name, f"thumbnail failed for {item_code}")
		frappe.throw(_("تعذر توليد المصغرة — احتُفظ بالصورة القديمة"))

	item_doc = frappe.get_doc("Item", item_code)
	try:
		item_doc.image = file_url
		item_doc.save(ignore_permissions=True)
	except Exception:
		_delete_file_doc_quietly(new_doc.name, f"item update failed for {item_code}")
		raise

	_delete_predecessor(item_code, old_url, new_doc.name)
	return {
		"ok": True,
		"image": file_url,
		"thumbnail": thumbnail_url,
		"replaced": bool(old_url),
	}


def staff_delete_item_image(item_code):
	"""Clear the item's image. Idempotent: no image -> ok with
	deleted=False. A failing file delete is logged, never thrown —
	the display already falls back to the placeholder.
	"""
	if not item_code or not frappe.db.exists("Item", item_code):
		frappe.throw(UNKNOWN_ITEM_MESSAGE)
	current = frappe.db.get_value("Item", item_code, "image") or ""
	if not current:
		return {"ok": True, "deleted": False}
	item_doc = frappe.get_doc("Item", item_code)
	item_doc.image = ""
	item_doc.save(ignore_permissions=True)
	deleted = False
	if current.startswith("/files/"):
		old_name = _file_doc_by_url(current)
		if old_name:
			deleted = _delete_file_doc_quietly(old_name, f"delete for {item_code}")
	return {"ok": True, "deleted": deleted}


def resolve_item_thumbnails(item_codes):
	"""Map item codes to {"image", "thumbnail"}. Two queries total,
	never N+1. Thumbnail URL always comes from the File doc resolved
	by file_url — never hand-built. Non-file URLs (/assets/remote)
	are passed through untouched.
	"""
	codes = [c for c in dict.fromkeys(item_codes or []) if c]
	if not codes:
		return {}
	images = {
		r["item_code"]: r["image"] or ""
		for r in frappe.get_all("Item", filters={"item_code": ["in", codes]}, fields=["item_code", "image"])
	}
	file_urls = sorted({u for u in images.values() if u.startswith("/files/")})
	thumbs = {}
	if file_urls:
		thumbs = {
			r["file_url"]: r["thumbnail_url"] or ""
			for r in frappe.get_all(
				"File", filters={"file_url": ["in", file_urls]}, fields=["file_url", "thumbnail_url"]
			)
		}
	out = {}
	for code in codes:
		url = images.get(code, "")
		out[code] = {"image": url, "thumbnail": thumbs.get(url, "") if url else ""}
	return out


def staff_find_missing_images(limit=100):
	"""Read-only health check (also the post-restore triage tool):
	items whose image/thumbnail file is missing from disk or has no
	File doc. Never writes.
	"""
	rows = frappe.get_all(
		"Item",
		filters={"image": ["!=", ""]},
		fields=["item_code", "item_name", "image"],
		limit_page_length=min(max(int(limit or 100), 1), 500),
	)
	file_urls = sorted({r["image"] for r in rows if (r["image"] or "").startswith("/files/")})
	known = set()
	if file_urls:
		known = {
			r["file_url"]
			for r in frappe.get_all("File", filters={"file_url": ["in", file_urls]}, fields=["file_url"])
		}
	missing = []
	for r in rows:
		url = r["image"] or ""
		problems = []
		if url.startswith("/files/"):
			if url not in known:
				problems.append("file-doc")
			else:
				disk = os.path.abspath(frappe.get_site_path("public", url.lstrip("/")))
				if not os.path.exists(disk):
					problems.append("disk")
			if url in known:
				thumb = frappe.db.get_value("File", {"file_url": url}, "thumbnail_url") or ""
				if thumb and thumb.startswith("/files/"):
					disk_t = os.path.abspath(frappe.get_site_path("public", thumb.lstrip("/")))
					if not os.path.exists(disk_t):
						problems.append("thumbnail-disk")
		if problems:
			missing.append(
				{
					"item_code": r["item_code"],
					"item_name": r["item_name"],
					"image": url,
					"missing": problems,
				}
			)
	return missing
