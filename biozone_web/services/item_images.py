"""Item image service: single image per Item, staff-managed upload.

- One image per Item via the standard `Item.image` field (no schema
  change). The item page uses the original; catalog cards use the
  trimmed card pair when present, else the thumbnail.
- Validation is content-based (Pillow): 5MB cap, {JPEG, PNG, WEBP},
  4000px per side, 16MP total. Extension/MIME from the request are
  never trusted.
- Flow: save File (public, attached to Item) -> optimize_image() ->
  make_thumbnail() -> recompress thumbnail (JPEG/WEBP q82) ->
  make card pair (trimmed, from the original, best-effort) ->
  set Item.image -> race-safe delete of the
  predecessor. Cleanup matrix:
    * original upload fails -> nothing persisted, throw;
    * thumbnail fails -> delete the new File, keep the old image, throw;
    * Item update fails -> delete derivatives + the new File, throw;
    * old-file delete fails after success -> SUCCESS + warning log,
      orphan left for the missing-image health check.
    * card-pair build fails -> warning log only, thumbnail chain covers.
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
from PIL import Image, ImageChops

from biozone_web.utils import require_staff_access

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_SIDE = 4000
MAX_IMAGE_PIXELS = 16_000_000
ALLOWED_PIL_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
THUMB_WIDTH = 300
THUMB_HEIGHT = 300
THUMB_JPEG_QUALITY = 82
CARD_LONG_2X = 600
CARD_LONG_1X = 300
CARD_TRIM_THRESHOLD = 12
CARD_TRIM_PAD = 0.03
CARD_SUFFIX_1X = "_card"
CARD_SUFFIX_2X = "_card2x"
MEDIUM_SUFFIX = "_medium"

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
	_remove_derivatives(old_url)
	return _delete_file_doc_quietly(old_name, f"predecessor of {item_code}")


def _recompress_file(thumb_path):
	"""Pure core: re-save one thumbnail file with catalog settings.

	Returns True/False, never throws — the strict wrapper below and
	the batch backfill both build on this.
	"""
	try:
		with Image.open(thumb_path) as image:
			image.load()
			fmt = (image.format or "").upper()
			if fmt == "JPEG":
				if image.mode in ("RGBA", "P"):
					image = image.convert("RGB")
				image.save(thumb_path, quality=THUMB_JPEG_QUALITY, optimize=True)
			elif fmt == "WEBP":
				image.save(thumb_path, quality=THUMB_JPEG_QUALITY, method=6)
			elif fmt == "PNG":
				image.save(thumb_path, optimize=True)
		return True
	except Exception:
		return False


def _recompress_thumbnail(thumbnail_url, item_code, file_name):
	"""Re-save the framework thumbnail with explicit catalog quality.

	Framework make_thumbnail saves with Pillow defaults (~q75 for
	JPEG): this pass applies JPEG/WEBP q82 (PNG optimize only).
	Failure behaves exactly like a thumbnail failure — the caller
	deletes the new File and keeps the old image.
	"""
	try:
		path = os.path.abspath(frappe.get_site_path("public", thumbnail_url.lstrip("/")))
		if not _recompress_file(path):
			raise IOError("thumbnail recompress failed")
	except Exception:
		_delete_file_doc_quietly(file_name, f"thumbnail recompress failed for {item_code}")
		frappe.throw(_("تعذر توليد المصغرة — احتُفظ بالصورة القديمة"))


def _derived_paths(file_url):
	"""Derivative relative URLs by convention (the _small file itself
	is tracked by the File doc; these are ours to create and clean)."""
	stem, dot, ext = (file_url or "").rpartition(".")
	if not (file_url or "").startswith("/files/") or not dot:
		return {}
	return {
		"medium": f"{stem}{MEDIUM_SUFFIX}.{ext}",
		"card": f"{stem}{CARD_SUFFIX_1X}.{ext}",
		"card2x": f"{stem}{CARD_SUFFIX_2X}.{ext}",
	}


def derived_url(file_url, key):
	"""Exists-checked derivative URL or "". Never a broken link:
	items without the file simply get no candidate."""
	candidate = _derived_paths(file_url).get(key) or ""
	if candidate and os.path.exists(os.path.abspath(frappe.get_site_path("public", candidate.lstrip("/")))):
		return candidate
	return ""


def medium_url_for(file_url):
	return derived_url(file_url, "medium")


def _remove_derivatives(file_url):
	"""Best-effort removal of our derivative files for one original.
	Used on replace/delete/failure paths so no orphan stays behind."""
	for _key, rel in _derived_paths(file_url).items():
		try:
			p = os.path.abspath(frappe.get_site_path("public", rel.lstrip("/")))
			if os.path.exists(p):
				os.remove(p)
		except Exception:
			frappe.logger(LOG).warning("derivative orphan: %s", rel)


def _trim_content_box(image, threshold=CARD_TRIM_THRESHOLD, pad=CARD_TRIM_PAD):
	"""Near-white margin bbox or None when trimming is pointless.

	Padded crop only — content is never cut. Returns None for blank
	images or when the content already fills >=95% of the frame.
	"""
	rgb = image.convert("RGB") if image.mode != "RGB" else image
	bg = Image.new("RGB", rgb.size, (255, 255, 255))
	mask = ImageChops.difference(rgb, bg).point(lambda v: 255 if v > threshold else 0)
	bbox = mask.getbbox()
	if not bbox:
		return None
	x0, y0, x1, y1 = bbox
	pad_x = int((x1 - x0) * pad)
	pad_y = int((y1 - y0) * pad)
	x0 = max(0, x0 - pad_x)
	y0 = max(0, y0 - pad_y)
	x1 = min(rgb.size[0], x1 + pad_x)
	y1 = min(rgb.size[1], y1 + pad_y)
	if (x1 - x0) * (y1 - y0) >= rgb.size[0] * rgb.size[1] * 0.95:
		return None
	return (x0, y0, x1, y1)


def _build_card_pair(image):
	"""(img_1x, img_2x|None) catalog pair from an opened original.

	Pure Pillow — no frappe, no disk. Trims first, then fits the long
	side to 600/300 with LANCZOS. Sources under 300 long side yield a
	single native-size file: never upscaled, never called an improvement.
	"""
	canvas = image.copy()
	box = _trim_content_box(canvas)
	if box:
		canvas = canvas.crop(box)
	long_side = max(canvas.size)
	if long_side <= CARD_LONG_1X:
		return (canvas, None)
	scale2 = min(CARD_LONG_2X, long_side) / long_side
	img2x = canvas.resize(
		(max(1, round(canvas.size[0] * scale2)), max(1, round(canvas.size[1] * scale2))),
		Image.Resampling.LANCZOS,
	)
	img1x = img2x.resize(
		(max(1, round(img2x.size[0] / 2)), max(1, round(img2x.size[1] / 2))),
		Image.Resampling.LANCZOS,
	)
	return (img1x, img2x)


def _save_card_image(image, path, fmt):
	"""Same-format save with catalog clarity settings. No conversion."""
	if fmt == "JPEG":
		if image.mode in ("RGBA", "P"):
			image = image.convert("RGB")
		image.save(path, quality=THUMB_JPEG_QUALITY, optimize=True)
	elif fmt == "WEBP":
		image.save(path, quality=THUMB_JPEG_QUALITY, method=6)
	elif fmt == "PNG":
		image.save(path, optimize=True)
	else:
		image.save(path)


def _make_card_pair(file_url, item_code):
	"""Best-effort catalog pair from the stored original. Never throws:
	on any error logs + returns ("", "") and display falls back to the
	thumbnail chain. The original file is only read, never rewritten."""
	try:
		src = os.path.abspath(frappe.get_site_path("public", file_url.lstrip("/")))
		with Image.open(src) as image:
			image.load()
			fmt = (image.format or "").upper()
			img1x, img2x = _build_card_pair(image)
		paths = _derived_paths(file_url)
		base = os.path.abspath(frappe.get_site_path("public"))
		p1 = os.path.join(base, paths["card"].lstrip("/"))
		_save_card_image(img1x, p1, fmt)
		out2 = ""
		if img2x is not None:
			p2 = os.path.join(base, paths["card2x"].lstrip("/"))
			_save_card_image(img2x, p2, fmt)
			out2 = paths["card2x"]
		return (paths["card"], out2)
	except Exception:
		frappe.logger(LOG).warning("card pair skipped for %s", item_code)
		return ("", "")


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
	_recompress_thumbnail(thumbnail_url, item_code, new_doc.name)
	_make_card_pair(file_url, item_code)

	item_doc = frappe.get_doc("Item", item_code)
	try:
		item_doc.image = file_url
		item_doc.save(ignore_permissions=True)
	except Exception:
		_remove_derivatives(file_url)
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
		_remove_derivatives(current)
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


def rebuild_derivatives(dry_run=True, limit=None):
	"""Batch re-apply catalog settings to existing item images.

	dry_run=True lists what WOULD change without writing anything.
	dry_run=False rewrites thumbnails in place (same URLs — expect
	short-lived browser/CDN staleness) and (re)builds card pairs from
	originals. Per-item try/except: one bad file is recorded, never
	aborts the batch. Writes files only — no DB writes, no commit.
	Returns {"total","rebuilt","skipped","failed","failures"}.
	"""
	kwargs = {"filters": {"image": ["!=", ""]}, "fields": ["item_code", "image"]}
	if limit:
		kwargs["limit_page_length"] = int(limit)
	rows = frappe.get_all("Item", **kwargs) or []
	summary = {"total": len(rows), "rebuilt": 0, "skipped": 0, "failed": 0, "failures": []}
	for r in rows:
		code = r["item_code"]
		url = r["image"] or ""
		if not url.startswith("/files/"):
			summary["skipped"] += 1
			continue
		public = os.path.abspath(frappe.get_site_path("public"))
		orig = os.path.join(public, url.lstrip("/"))
		if not os.path.exists(orig):
			summary["failed"] += 1
			summary["failures"].append((code, "disk"))
			continue
		doc = frappe.db.get_value("File", {"file_url": url}, ["name", "thumbnail_url"], as_dict=True) or {}
		thumb = doc.get("thumbnail_url") or ""
		thumb_path = os.path.join(public, thumb.lstrip("/")) if thumb.startswith("/files/") else ""
		if dry_run:
			summary["rebuilt"] += 1
			continue
		ok = True
		if thumb_path and os.path.exists(thumb_path):
			ok = _recompress_file(thumb_path)
		else:
			ok = False
		card, _card2x = _make_card_pair(url, code)
		if ok and (card or not thumb_path):
			summary["rebuilt"] += 1
		else:
			summary["failed"] += 1
			summary["failures"].append((code, "recompress" if not ok else "card"))
	frappe.logger(LOG).info("rebuild_derivatives dry_run=%s %s", dry_run, summary)
	return summary


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
