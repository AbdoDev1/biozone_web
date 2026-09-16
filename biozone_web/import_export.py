"""Bulk item Import/Export for the staff panel (Arabic UI).

Same conventions as the existing staff_* functions in api.py:
- @frappe.whitelist (+ methods=["POST"] for writes), require_staff_access()
  as the first statement, {ok: True/False, ...} response shape.
- Writes only via frappe.new_doc/insert/save/set_value — never bulk_insert,
  never direct SQL writes, never Frappe's internal Data Import/Export
  whitelisted methods.
- System fields (owner/creation/modified/modified_by/docstatus/permissions)
  are never accepted as input and never written directly.
- Large files run as Background Jobs on the "long" queue; progress and the
  final result live on the Biozone Item Import run record, so they survive
  a closed browser.

Locked product decisions implemented here (section 12.6 of the plan):
1. Quantity cells are ADDED deltas posted via submitted Material Receipt to
   the single resolved default warehouse.
2. Public price overwrites Item Price / Standard Selling (site currency).
3. One simple Pricing Rule per (Item, Customer Group); no priority/min_qty.
4. item_code alone is a valid key; barcode+code must agree or the row fails.
5. Blank cell in ANY column = leave unchanged (no clearing via blanks).
6. Discount columns keyed by Customer Group docname; unknown/inactive/
   renamed/duplicate headers are detected and reported, never guessed.
7. Export gated by require_staff_access() only.
"""

import csv
import hashlib
import io

import frappe
from frappe import _

# ---------------------------------------------------------------------------
# Constants (implementation parameters, not product decisions)
# ---------------------------------------------------------------------------

IMPORT_QUEUE = "long"
IMPORT_TIMEOUT = 3600
IMPORT_BATCH_SIZE = 50
IMPORT_LOCK_TTL = 3600
IMPORT_LOCK_KEY = "biozone_item_import_lock"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 2000
PREVIEW_ROWS = 25
PUBLIC_PRICE_LIST = "Standard Selling"

# Internal column keys -> Arabic headers (template order).
BASE_COLUMNS = [
	("name", "الاسم"),
	("barcode", "الباركود"),
	("item_code", "الكود"),
	("price", "سعر الجمهور"),
	("qty", "الكمية"),
	("uom", "الوحدة"),
	("item_group", "القسم"),
]
BASE_KEY_BY_HEADER = {label: key for key, label in BASE_COLUMNS}
DISCOUNT_PREFIX = "خصم:"

QTY_HELP_COPY = (
	"الكمية = كمية مضافة جديدة إلى المخزن الافتراضي، وليست الرصيد الكلي الحالي."
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _discount_header(group_name):
	"""Header text for a per-Customer-Group discount column (decision 6)."""
	return f"{DISCOUNT_PREFIX} {group_name}"


def _parse_discount_header(header):
	"""Return the Customer Group docname encoded in a discount header, or
	None when the header is not a discount column."""
	text = (header or "").strip()
	if text.startswith(DISCOUNT_PREFIX):
		return text[len(DISCOUNT_PREFIX):].strip() or None
	return None


def _active_customer_groups():
	"""Live active leaf Customer Groups, ordered by name."""
	return frappe.get_all(
		"Customer Group",
		filters={"is_group": 0, "disabled": 0},
		fields=["name"],
		order_by="name asc",
	)


def _resolve_warehouse():
	"""Single fixed default warehouse (locked decision 1 + task A).

	Raises a clear setup error instead of guessing when none resolves.
	"""
	from biozone_web.utils import get_default_warehouse

	return get_default_warehouse()


def _item_group_item_rules_conflict():
	"""Read-only conflict check (task B): active Item-Group-level rules.

	Warning-only by design — never changes the simple per-pair rule design.
	Returns a list of {name, customer_group, item_group, discount_percentage}.
	"""
	rows = frappe.get_all(
		"Pricing Rule",
		filters={"disable": 0, "apply_on": "Item Group"},
		fields=["name", "customer_group", "discount_percentage"],
	)
	out = []
	for r in rows:
		groups = frappe.get_all(
			"Pricing Rule Item Group",
			filters={"parent": r.name},
			fields=["item_group"],
		)
		for g in groups:
			out.append(
				{
					"name": r.name,
					"customer_group": r.customer_group,
					"item_group": g.item_group,
					"discount_percentage": r.discount_percentage,
				}
			)
	return out


def _conflict_warning_text(conflicts):
	if not conflicts:
		return ""
	names = ", ".join(
		f"{c['name']} ({c['customer_group'] or '—'} / {c['item_group']} "
		f"{c['discount_percentage']}%)" for c in conflicts[:10]
	)
	more = f" +{len(conflicts) - 10} أخرى" if len(conflicts) > 10 else ""
	return (
		"تنبيه: توجد قواعد خصم على مستوى مجموعة الأصناف قد تتداخل مع "
		f"خصومات الأصناف المستوردة: {names}{more}. تصميم الاستيراد ينشئ "
		"قواعد بسيطة لكل (صنف، فئة) فقط، دون تغيير هذه القواعد."
	)


# ---------------------------------------------------------------------------
# File parsing (pure: bytes in, rows out — no DB)
# ---------------------------------------------------------------------------


def _read_xlsx_rows(content):
	"""Return (headers, data_rows, meta). data_rows are lists of raw cells."""
	import openpyxl

	wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
	ws = wb["الأصناف"] if "الأصناف" in wb.sheetnames else wb.active
	rows = list(ws.iter_rows(values_only=True))
	if not rows:
		return [], [], {}
	headers = [(str(c).strip() if c is not None else "") for c in rows[0]]
	data = []
	for r in rows[1:]:
		cells = [(str(c).strip() if c is not None else "") for c in r]
		if any(cells):
			data.append(cells)
	meta = {}
	if "_meta" in wb.sheetnames:
		for r in list(wb["_meta"].iter_rows(values_only=True))[1:]:
			if r and r[0]:
				meta[str(r[0]).strip()] = str(r[1]).strip() if len(r) > 1 and r[1] is not None else ""
	return headers, data, meta


def _read_csv_rows(content):
	"""Return (headers, data_rows, {}). Tries UTF-8-SIG then Windows-1256."""
	last_error = None
	for encoding in ("utf-8-sig", "cp1256", "windows-1252"):
		try:
			text = content.decode(encoding)
			break
		except (UnicodeDecodeError, LookupError) as e:
			last_error = e
			continue
	else:
		raise ValueError(f"تعذر قراءة ترميز الملف: {last_error}")
	reader = csv.reader(io.StringIO(text))
	rows = [[(c or "").strip() for c in r] for r in reader]
	rows = [r for r in rows if any(r)]
	if not rows:
		return [], [], {}
	return rows[0], rows[1:], {}


def parse_upload_bytes(content, filename):
	"""Parse uploaded bytes. Returns dict with headers/rows/meta or raises
	ValueError with an Arabic message. No DB access."""
	name = (filename or "").lower()
	if name.endswith(".xlsx"):
		headers, data, meta = _read_xlsx_rows(content)
	elif name.endswith(".csv"):
		headers, data, meta = _read_csv_rows(content)
	else:
		raise ValueError("صيغة الملف غير مدعومة — المسموح: xlsx أو csv فقط")
	return {"headers": headers, "rows": data, "meta": meta}


def map_headers(headers):
	"""Map header texts to internal keys.

	Returns (key_by_index, missing_base, unknown_headers, duplicate_errors,
	discount_groups_in_file). Never guesses a group from label text: the
	group part after 'خصم:' must equal a group docname check done later
	against live data (decision 6).
	"""
	key_by_index = {}
	seen = {}
	duplicate_errors = []
	unknown_headers = []
	discount_groups_in_file = []
	for i, h in enumerate(headers):
		if not h:
			continue
		if h in BASE_KEY_BY_HEADER:
			key = BASE_KEY_BY_HEADER[h]
		else:
			group = _parse_discount_header(h)
			if group is not None:
				if not group:
					duplicate_errors.append(f"عمود خصم بلا اسم فئة (العمود {i + 1})")
					continue
				key = ("discount", group)
				discount_groups_in_file.append(group)
			else:
				unknown_headers.append(h)
				continue
		if key in seen:
			duplicate_errors.append(f"رأس مكرر: {h}")
			continue
		seen[key] = True
		key_by_index[i] = key
	present = set(key_by_index.values())
	missing = [label for key, label in BASE_COLUMNS if key not in present]
	return key_by_index, missing, unknown_headers, duplicate_errors, discount_groups_in_file


# ---------------------------------------------------------------------------
# Row key resolution + validation (reads only)
# ---------------------------------------------------------------------------


def _strict_num(raw):
	"""Strict numeric parse: returns (ok_bool, float_value).

	frappe.utils.flt() silently coerces garbage ('abc' -> 0.0), so range
	checks alone can never catch invalid numbers. Every numeric import
	cell goes through here instead.
	"""
	text = (str(raw) if raw is not None else "").strip().replace(",", "")
	if not text:
		return False, 0.0
	try:
		return True, float(text)
	except (ValueError, TypeError):
		return False, 0.0


def _resolve_item_key(barcode, code):
	"""Resolve barcode/code cells to an Item, mirroring B6a + decision 4.

	Returns (item_code_or_None, error_or_None). Never writes.
	"""
	barcode = (barcode or "").strip()
	code = (code or "").strip()
	by_barcode = None
	by_code = None
	if barcode:
		parent = frappe.db.get_value("Item Barcode", {"barcode": barcode}, "parent")
		if parent:
			by_barcode = parent
		elif frappe.db.exists("Item", barcode):
			by_barcode = barcode
	if code and frappe.db.exists("Item", code):
		by_code = code
	if by_barcode and by_code and by_barcode != by_code:
		return None, _(
			"تعارض الباركود والكود: الباركود يخص {0} والكود يخص {1}"
		).format(by_barcode, by_code)
	return (by_barcode or by_code), None


def validate_rows(raw_rows, key_by_index, active_groups):
	"""Full read-only validation of every row.

	Returns (staged, counts, file_errors). staged items carry action
	create|update, resolved item_code, normalized values, and errors[].
	Blank in ANY column = leave unchanged (decision 5).
	"""
	active_set = {g.name for g in active_groups}
	staged = []
	for idx, cells in enumerate(raw_rows):
		n = idx + 2  # header is row 1
		values = {}
		for i, key in key_by_index.items():
			v = cells[i] if i < len(cells) else ""
			if isinstance(key, tuple):
				values.setdefault("discounts", {})[key[1]] = v
			else:
				values[key] = v
		row = {
			"n": n,
			"values": values,
			"action": None,
			"item_code": None,
			"errors": [],
		}
		barcode = values.get("barcode", "")
		code = values.get("item_code", "")
		target, key_error = _resolve_item_key(barcode, code)
		if key_error:
			row["errors"].append(key_error)
			staged.append(row)
			continue
		if target:
			row["action"] = "update"
			row["item_code"] = target
		else:
			# Create path: needs name + group; code = code or barcode.
			new_code = code or barcode
			if not new_code:
				row["errors"].append(_("الصف يحتاج باركود أو كودًا لتحديد الصنف"))
				staged.append(row)
				continue
			if frappe.db.exists("Item", new_code):
				# Re-resolved between upload and now — treat as update.
				row["action"] = "update"
				row["item_code"] = new_code
			else:
				if not (values.get("name") or "").strip():
					row["errors"].append(_("الاسم مطلوب للصنف الجديد"))
				if not (values.get("item_group") or "").strip():
					row["errors"].append(_("القسم مطلوب للصنف الجديد"))
				row["action"] = "create"
				row["item_code"] = new_code
		# Field-level checks (present cells only; blanks are always OK).
		group = (values.get("item_group") or "").strip()
		if group:
			if not frappe.db.exists("Item Group", {"name": group, "is_group": 0}):
				row["errors"].append(_("القسم غير موجود أو ليس قسمًا فرعيًا: {0}").format(group))
		uom = (values.get("uom") or "").strip()
		if uom and not frappe.db.exists("UOM", uom):
			row["errors"].append(_("الوحدة غير معرّفة: {0}").format(uom))
		price = (values.get("price") or "").strip()
		if price:
			ok, amount = _strict_num(price)
			if not ok:
				row["errors"].append(_("سعر الجمهور ليس رقمًا صالحًا"))
			elif amount < 0:
				row["errors"].append(_("سعر الجمهور يجب ألا يكون سالبًا"))
		qty = (values.get("qty") or "").strip()
		if qty:
			ok, amount = _strict_num(qty)
			if not ok:
				row["errors"].append(_("الكمية ليست رقمًا صالحًا"))
			elif amount <= 0:
				row["errors"].append(_("الكمية المضافة يجب أن تكون أكبر من صفر"))
		for gname, raw in (values.get("discounts") or {}).items():
			v = (raw or "").strip()
			if not v:
				continue
			if gname not in active_set:
				row["errors"].append(
					_("فئة الخصم غير معروفة أو غير مفعّلة: {0}").format(gname)
				)
				continue
			ok, p = _strict_num(v)
			if not ok:
				row["errors"].append(_("خصم {0} ليس رقمًا صالحًا").format(gname))
				continue
			if p < 0 or p > 100:
				row["errors"].append(_("خصم {0} يجب أن يكون بين 0 و100").format(gname))
		staged.append(row)
	counts = {
		"to_create": sum(1 for r in staged if r["action"] == "create" and not r["errors"]),
		"to_update": sum(1 for r in staged if r["action"] == "update" and not r["errors"]),
		"failed": sum(1 for r in staged if r["errors"] or not r["action"]),
	}
	return staged, counts, []


# ---------------------------------------------------------------------------
# Write helpers (mirror staff_save_item / staff_set_item_discount /
# staff_log_stock_movement semantics; ORM only)
# ---------------------------------------------------------------------------


def _import_apply_price(item_code, price_cell):
	"""Overwrite Standard Selling rate when the cell is non-blank.

	Returns (changed_bool, rule_name_or_None). Blank = leave unchanged.
	"""
	price = (price_cell or "").strip()
	if not price:
		return False, None
	ok, price_value = _strict_num(price)
	if not ok:
		raise frappe.ValidationError(_("سعر الجمهور ليس رقمًا صالحًا"))
	existing = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": PUBLIC_PRICE_LIST},
		"name",
	)
	if existing:
		current = frappe.db.get_value("Item Price", existing, "price_list_rate")
		if frappe.utils.flt(current) == price_value:
			return False, existing
		frappe.db.set_value("Item Price", existing, "price_list_rate", price_value)
		return True, existing
	frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": item_code,
			"price_list": PUBLIC_PRICE_LIST,
			"price_list_rate": price_value,
		}
	).insert(ignore_permissions=True)
	return True, None


def _import_apply_discount(item_code, group, percent_cell):
	"""One simple rule per (Item, Customer Group), mirrored from
	staff_set_item_discount (legacy-title healing + disable-on-0).

	Returns (changed_bool, rule_name_or_None). Blank = leave unchanged.
	"""
	raw = (percent_cell or "").strip()
	if not raw:
		return False, None
	ok, percent = _strict_num(raw)
	if not ok:
		raise frappe.ValidationError(_("نسبة الخصم ليست رقمًا صالحًا"))
	existing = frappe.db.sql(
		"""
		select pr.name
		from `tabPricing Rule Item Code` pri
		inner join `tabPricing Rule` pr on pr.name = pri.parent
		where pr.apply_on = 'Item Code'
			and pri.item_code = %(item_code)s
			and (
				pr.customer_group = %(customer_group)s
				or (
					ifnull(pr.customer_group, '') = ''
					and pr.title = %(legacy_title)s
				)
			)
		limit 1
		""",
		{
			"customer_group": group,
			"item_code": item_code,
			"legacy_title": f"{item_code} - {group}",
		},
		as_dict=True,
	)
	existing_name = existing[0].name if existing else None
	if percent <= 0:
		if existing_name:
			frappe.db.set_value("Pricing Rule", existing_name, "disable", 1)
			return True, existing_name
		return False, None
	if existing_name:
		doc = frappe.get_doc("Pricing Rule", existing_name)
		if (
			frappe.utils.flt(doc.discount_percentage) == percent
			and doc.applicable_for == "Customer Group"
			and doc.customer_group == group
			and not doc.disable
		):
			return False, existing_name
		doc.discount_percentage = percent
		doc.applicable_for = "Customer Group"
		doc.customer_group = group
		doc.disable = 0
		doc.save(ignore_permissions=True)
		return True, existing_name
	doc = frappe.get_doc(
		{
			"doctype": "Pricing Rule",
			"title": f"{item_code} - {group}",
			"apply_on": "Item Code",
			"price_or_product_discount": "Price",
			"selling": 1,
			"applicable_for": "Customer Group",
			"customer_group": group,
			"rate_or_discount": "Discount Percentage",
			"discount_percentage": percent,
			"items": [{"item_code": item_code}],
		}
	)
	doc.insert(ignore_permissions=True)
	return True, doc.name


def _import_apply_qty(item_code, qty_cell, uom_cell, warehouse, run_name, row_n):
	"""Post an ADDED delta via submitted Material Receipt (decision 1).

	Returns the Stock Entry name. Blank = no movement (None).
	Raises frappe.ValidationError with an Arabic message on failure.
	"""
	qty_raw = (qty_cell or "").strip()
	if not qty_raw:
		return None
	ok, qty = _strict_num(qty_raw)
	if not ok or qty <= 0:
		raise frappe.ValidationError(_("الكمية المضافة يجب أن تكون رقمًا أكبر من صفر"))
	item = frappe.db.get_value(
		"Item", item_code, ["stock_uom"], as_dict=True
	)
	uom = (uom_cell or "").strip() or item.stock_uom
	conversion_factor = 1.0
	if uom != item.stock_uom:
		conversion_factor = frappe.db.get_value(
			"UOM Conversion Detail",
			{"parent": item_code, "parenttype": "Item", "uom": uom},
			"conversion_factor",
		)
		if not conversion_factor:
			frappe.throw(_("الوحدة {0} غير معرّفة للصنف {1}").format(uom, item_code))
	valuation = frappe.utils.flt(
		frappe.db.get_value(
			"Bin", {"item_code": item_code, "warehouse": warehouse}, "valuation_rate"
		)
	)
	stock_entry = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": "Material Receipt",
			"purpose": "Material Receipt",
			"to_warehouse": warehouse,
			"remarks": f"استيراد الأصناف {run_name} — صف {row_n}",
			"items": [
				{
					"item_code": item_code,
					"qty": qty,
					"uom": uom,
					"conversion_factor": conversion_factor,
					"t_warehouse": warehouse,
					"basic_rate": valuation or 0,
				}
			],
		}
	)
	stock_entry.insert()
	stock_entry.submit()
	return stock_entry.name


def _process_staged_row(row, warehouse, run_name):
	"""Create/update one staged row. Returns (item_outcome, pricing_outcomes).

	Raises on unexpected errors; validation errors are pre-caught at staging
	and re-checked lightly here (final re-validation per B6a).
	"""
	values = row["values"]
	barcode = (values.get("barcode") or "").strip()
	code = (values.get("item_code") or "").strip()
	target, key_error = _resolve_item_key(barcode, code)
	if key_error:
		raise frappe.ValidationError(key_error)
	name = (values.get("name") or "").strip()
	group = (values.get("item_group") or "").strip()
	uom = (values.get("uom") or "").strip()
	if target:
		if not frappe.db.exists("Item", target):
			raise frappe.ValidationError(_("الصنف {0} لم يعد موجودًا").format(target))
		doc = frappe.get_doc("Item", target)
		if name:
			doc.item_name = name
		if group:
			if not frappe.db.exists("Item Group", {"name": group, "is_group": 0}):
				raise frappe.ValidationError(_("القسم غير موجود: {0}").format(group))
			doc.item_group = group
		if uom and uom != doc.stock_uom:
			if not frappe.db.exists("UOM", uom):
				raise frappe.ValidationError(_("الوحدة غير معرّفة: {0}").format(uom))
			doc.stock_uom = uom
		if barcode:
			existing_bcs = {
				(r.barcode or "").strip()
				for r in (doc.get("barcodes") or [])
				if (r.barcode or "").strip()
			}
			if barcode not in existing_bcs:
				conflict = frappe.db.get_value("Item Barcode", {"barcode": barcode}, "parent")
				if conflict and conflict != doc.name:
					raise frappe.ValidationError(
						_(
							"BARCODE_CONFLICT_WITH_OTHER_ITEM: الباركود {0} مسجل بالفعل للصنف {1}"
						).format(barcode, conflict)
					)
				if barcode != doc.name and frappe.db.exists("Item", barcode):
					raise frappe.ValidationError(
						_(
							"BARCODE_CONFLICT_WITH_OTHER_ITEM: الباركود {0} يطابق كود صنف آخر موجود"
						).format(barcode)
					)
				doc.append("barcodes", {"barcode": barcode})
		doc.save(ignore_permissions=True)
		action = "update"
	else:
		if not name:
			raise frappe.ValidationError(_("الاسم مطلوب للصنف الجديد"))
		if not group:
			raise frappe.ValidationError(_("القسم مطلوب للصنف الجديد"))
		if not frappe.db.exists("Item Group", {"name": group, "is_group": 0}):
			raise frappe.ValidationError(_("القسم غير موجود: {0}").format(group))
		if uom and not frappe.db.exists("UOM", uom):
			raise frappe.ValidationError(_("الوحدة غير معرّفة: {0}").format(uom))
		new_barcodes = [barcode] if barcode else []
		for bc in new_barcodes:
			conflict = frappe.db.get_value("Item Barcode", {"barcode": bc}, "parent")
			if conflict:
				raise frappe.ValidationError(
					_(
						"BARCODE_CONFLICT_WITH_OTHER_ITEM: الباركود {0} مسجل بالفعل للصنف {1}"
					).format(bc, conflict)
				)
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code or barcode,
				"item_name": name,
				"item_group": group,
				"stock_uom": uom or "Nos",
				"is_stock_item": 1,
				"barcodes": [{"barcode": bc} for bc in new_barcodes],
			}
		)
		doc.insert(ignore_permissions=True)
		action = "create"
	item_outcome = {"row": row["n"], "item_code": doc.name, "action": action}
	price_changed, _ = _import_apply_price(doc.name, values.get("price", ""))
	if price_changed:
		item_outcome["price_updated"] = True
	pricing_outcomes = []
	for gname, raw in (values.get("discounts") or {}).items():
		if not (raw or "").strip():
			continue
		changed, rule = _import_apply_discount(doc.name, gname, raw)
		if changed:
			pricing_outcomes.append(
				{"row": row["n"], "item_code": doc.name, "group": gname, "rule": rule}
			)
	se_name = _import_apply_qty(
		doc.name, values.get("qty", ""), uom, warehouse, run_name, row["n"]
	)
	if se_name:
		item_outcome["stock_entry"] = se_name
	return item_outcome, pricing_outcomes


# ---------------------------------------------------------------------------
# Redis batch lock (the agreed B6a simple batch lock — one import commits
# at a time; additive deltas must never race)
# ---------------------------------------------------------------------------


def _acquire_import_lock(run_name, ttl=IMPORT_LOCK_TTL):
	"""Atomic NX lock. Returns True when acquired.

	The value is pickled to match frappe.cache().get_value(), which
	unpickles on read — a raw-bytes value would break every later
	get_value() with an unpickling error and wedge the lock until TTL.
	"""
	import pickle

	try:
		cache = frappe.cache()
		key = cache.make_key(IMPORT_LOCK_KEY)
		acquired = cache.set(
			name=key, value=pickle.dumps(run_name), ex=ttl, nx=True
		)
		return bool(acquired)
	except Exception:
		if frappe.cache().get_value(IMPORT_LOCK_KEY) == run_name:
			return True
		if frappe.cache().get_value(IMPORT_LOCK_KEY):
			return False
		frappe.cache().set_value(IMPORT_LOCK_KEY, run_name, expires_in_sec=ttl)
		return True


def _release_import_lock(run_name):
	try:
		if frappe.cache().get_value(IMPORT_LOCK_KEY) == run_name:
			frappe.cache().delete_value(IMPORT_LOCK_KEY)
	except Exception:
		pass


# ---------------------------------------------------------------------------
# Run-record helpers
# ---------------------------------------------------------------------------


def _get_run(run_name):
	if not frappe.db.exists("Biozone Item Import", run_name):
		frappe.throw(_("جلسة الاستيراد غير موجودة"))
	return frappe.get_doc("Biozone Item Import", run_name)


def _prior_completed_runs(file_hash, exclude):
	if not file_hash:
		return []
	rows = frappe.get_all(
		"Biozone Item Import",
		filters={
			"name": ["!=", exclude],
			"file_hash": file_hash,
			"status": ["in", ["Completed", "Partial"]],
			"run_type": "Import",
		},
		fields=["name", "creation", "requested_by", "done_created", "done_updated"],
		order_by="creation desc",
		limit_page_length=5,
	)
	return rows


# ---------------------------------------------------------------------------
# Whitelisted API — upload / validate-status / confirm / result / export
# ---------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def staff_upload_import_file():
	"""Upload + parse only (fast, in-memory), then queue background validation.

	Stages: uploading (here) -> Validating (job) -> Preview (user confirms).
	"""
	from biozone_web.utils import require_staff_access

	require_staff_access()

	upload = (frappe.request.files or {}).get("file")
	if upload is None:
		return {"ok": False, "error": _("اختر ملفًا أولًا (xlsx أو csv)")}
	filename = (getattr(upload, "filename", "") or "").strip()
	content = upload.read() or b""
	if not filename.lower().endswith((".xlsx", ".csv")):
		return {"ok": False, "error": _("صيغة الملف غير مدعومة — المسموح: xlsx أو csv فقط")}
	if len(content) > MAX_UPLOAD_BYTES:
		return {"ok": False, "error": _("حجم الملف يتجاوز الحد المسموح (5MB)")}
	if not content:
		return {"ok": False, "error": _("الملف فارغ")}
	file_hash = hashlib.sha256(content).hexdigest()
	try:
		parsed = parse_upload_bytes(content, filename)
	except ValueError as e:
		return {"ok": False, "error": str(e)}
	if len(parsed["rows"]) > MAX_ROWS:
		return {
			"ok": False,
			"error": _("عدد الصفوف يتجاوز الحد المسموح (2000 صف)"),
		}
	if not parsed["rows"]:
		return {"ok": False, "error": _("الملف لا يحتوي على صفوف بيانات")}
	try:
		warehouse = _resolve_warehouse()
	except Exception as e:
		return {"ok": False, "error": str(e), "setup_error": True}
	run = frappe.get_doc(
		{
			"doctype": "Biozone Item Import",
			"run_type": "Import",
			"status": "Uploaded",
			"file_name": filename,
			"file_hash": file_hash,
			"warehouse": warehouse,
			"total_rows": len(parsed["rows"]),
			"requested_by": frappe.session.user,
		}
	)
	run.insert(ignore_permissions=True)
	frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{run.name}-{filename}",
			"content": content,
			"is_private": 1,
			"attached_to_doctype": "Biozone Item Import",
			"attached_to_name": run.name,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.enqueue(
		"biozone_web.import_export.run_validate_job",
		queue=IMPORT_QUEUE,
		timeout=IMPORT_TIMEOUT,
		job_id=f"biozone-import-validate-{run.name}",
		enqueue_after_commit=True,
		run_name=run.name,
	)
	return {"ok": True, "run_name": run.name, "total_rows": len(parsed["rows"])}


def run_validate_job(run_name):
	"""Background phase 1: full validation + staging + preview data."""
	run = _get_run(run_name)
	run.db_set("status", "Validating")
	frappe.db.commit()
	try:
		attached = frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": "Biozone Item Import",
				"attached_to_name": run_name,
			},
			fields=["name"],
			order_by="creation desc",
			limit_page_length=1,
		)
		if not attached:
			raise frappe.ValidationError(_("ملف التشغيل غير موجود"))
		content = frappe.get_doc("File", attached[0].name).get_content()
		parsed = parse_upload_bytes(content, run.file_name)
		key_by_index, missing, unknown, duplicates, _ = map_headers(parsed["headers"])
		if duplicates:
			raise frappe.ValidationError(_("رؤوس مكررة: {0}").format("، ".join(duplicates)))
		active_groups = _active_customer_groups()
		active_set = {g.name for g in active_groups}
		unknown_group_headers = sorted(
			{g for g in _discount_group_headers(key_by_index) if g not in active_set}
		)
		staged, counts, _ = validate_rows(parsed["rows"], key_by_index, active_groups)
		warnings = []
		if unknown:
			warnings.append(
				"أعمدة غير معروفة تم تجاهلها: " + "، ".join(unknown)
			)
		if unknown_group_headers:
			meta_groups = _template_meta_groups(parsed["meta"])
			hint = ""
			if meta_groups and any(g in meta_groups for g in unknown_group_headers):
				hint = " (كانت موجودة عند تنزيل القالب — ربما أُعيدت تسميتها أو عُطّلت؛ الفئات النشطة الآن: " + (
					"، ".join(sorted(active_set)) or "لا يوجد"
				) + ")"
			warnings.append(
				"فئات خصم غير معروفة أو غير مفعّلة تم تجاهل قيمها: "
				+ "، ".join(unknown_group_headers)
				+ hint
			)
		conflicts = _item_group_item_rules_conflict()
		conflict_text = _conflict_warning_text(conflicts)
		if conflict_text:
			warnings.append(conflict_text)
		run.db_set("missing_columns", "\n".join(missing))
		run.db_set("warnings", "\n".join(warnings))
		run.db_set(
			"groups_snapshot",
			frappe.as_json([{"name": g.name} for g in active_groups]),
		)
		run.db_set("rows_to_create", counts["to_create"])
		run.db_set("rows_to_update", counts["to_update"])
		run.db_set("rows_failed", counts["failed"])
		run.db_set("result_json", frappe.as_json({"staged": staged}))
		run.db_set("status", "Preview")
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		run.db_set("status", "Failed")
		run.db_set("error", str(e)[:500])
		frappe.db.commit()


def _discount_group_headers(key_by_index):
	return [k[1] for k in key_by_index.values() if isinstance(k, tuple)]


def _template_meta_groups(meta):
	try:
		import json as _json

		groups = _json.loads(meta.get("groups", "[]") or "[]")
		return {g.get("name") for g in groups if isinstance(g, dict) and g.get("name")}
	except Exception:
		return set()


@frappe.whitelist()
def staff_import_status(run_name):
	"""Pollable status for every stage; terminal states include the summary."""
	from biozone_web.utils import require_staff_access

	require_staff_access()
	run = _get_run(run_name)
	out = {
		"ok": True,
		"status": run.status,
		"total_rows": run.total_rows or 0,
		"rows_to_create": run.rows_to_create or 0,
		"rows_to_update": run.rows_to_update or 0,
		"rows_failed": run.rows_failed or 0,
		"done_created": run.done_created or 0,
		"done_updated": run.done_updated or 0,
		"done_failed": run.done_failed or 0,
		"missing_columns": (run.missing_columns or "").split("\n") if run.missing_columns else [],
		"warnings": (run.warnings or "").split("\n") if run.warnings else [],
		"error": run.error or "",
	}
	if run.status in ("Completed", "Partial", "Failed"):
		try:
			out["summary"] = frappe.parse_json(run.result_json or "{}")
		except Exception:
			out["summary"] = {}
	return out


@frappe.whitelist()
def staff_import_preview(run_name, limit=PREVIEW_ROWS):
	"""First N staged rows with per-row action/errors for the preview screen."""
	from biozone_web.utils import require_staff_access

	require_staff_access()
	run = _get_run(run_name)
	if run.status not in ("Preview", "Queued", "Running", "Completed", "Partial"):
		return {"ok": False, "error": _("المعاينة غير جاهزة بعد — الحالة الحالية: {0}").format(run.status)}
	try:
		staged = (frappe.parse_json(run.result_json or "{}") or {}).get("staged", [])
	except Exception:
		staged = []
	prior = _prior_completed_runs(run.file_hash, run.name)
	return {
		"ok": True,
		"status": run.status,
		"rows": staged[: int(limit or PREVIEW_ROWS)],
		"total": len(staged),
		"missing_columns": (run.missing_columns or "").split("\n") if run.missing_columns else [],
		"warnings": (run.warnings or "").split("\n") if run.warnings else [],
		"prior_runs": [
			{
				"name": p.name,
				"creation": str(p.creation),
				"requested_by": p.requested_by,
				"done_created": p.done_created,
				"done_updated": p.done_updated,
			}
			for p in prior
		],
	}


@frappe.whitelist(methods=["POST"])
def staff_confirm_import(run_name, confirm_rerun=0):
	"""User-confirmed commit gate: idempotency check, then queue commit job.

	Stages: Preview (here) -> Queued -> Running -> Completed/Partial/Failed.
	"""
	from biozone_web.utils import require_staff_access

	require_staff_access()
	run = _get_run(run_name)
	if run.status != "Preview":
		return {
			"ok": False,
			"error": _("لا يمكن التأكيد — الحالة الحالية: {0}").format(run.status),
		}
	prior = _prior_completed_runs(run.file_hash, run.name)
	if prior and not frappe.utils.cint(confirm_rerun):
		return {
			"ok": False,
			"need_rerun_confirm": True,
			"error": _(
				"هذا الملف نفسه سُجّل كتشغيل مكتمل من قبل ({0}) — إعادة التشغيل "
				"ستضيف الكميات مرة أخرى. أكّد صراحة للمتابعة."
			).format(prior[0].name),
			"prior_runs": [
				{
					"name": p.name,
					"creation": str(p.creation),
					"requested_by": p.requested_by,
				}
				for p in prior
			],
		}
	run.db_set("confirmed_rerun", 1 if frappe.utils.cint(confirm_rerun) else 0)
	run.db_set("status", "Queued")
	frappe.db.commit()
	frappe.enqueue(
		"biozone_web.import_export.run_import_job",
		queue=IMPORT_QUEUE,
		timeout=IMPORT_TIMEOUT,
		job_id=f"biozone-import-commit-{run.name}",
		enqueue_after_commit=True,
		run_name=run.name,
	)
	return {"ok": True, "status": "Queued"}


def run_import_job(run_name):
	"""Background phase 2: bounded batches + periodic commits + Redis lock."""
	run = _get_run(run_name)
	if run.status != "Queued":
		# A run that was cancelled (or already handled) after enqueueing
		# must never commit anything — leave its status untouched.
		return
	run.db_set("status", "Running")
	run.db_set("error", "")
	run.db_set("done_created", 0)
	run.db_set("done_updated", 0)
	run.db_set("done_failed", 0)
	frappe.db.commit()
	if not _acquire_import_lock(run_name):
		run.db_set("status", "Failed")
		run.db_set("error", _("تشغيل استيراد آخر جارٍ الآن — أعد المحاولة بعد انتهائه"))
		frappe.db.commit()
		return
	try:
		try:
			warehouse = _resolve_warehouse()
		except Exception as e:
			raise frappe.ValidationError(str(e))
		if not frappe.db.exists("Stock Entry Type", "Material Receipt"):
			raise frappe.ValidationError(
				_('نوع حركة المخزون "Material Receipt" غير معرّف في النظام')
			)
		try:
			staged = (frappe.parse_json(run.result_json or "{}") or {}).get("staged", [])
		except Exception:
			staged = []
		if not staged:
			raise frappe.ValidationError(_("لا توجد صفوف مرحّلة لهذا التشغيل"))
		items_out, pricing_out, errors = [], [], []
		created = updated = failed = 0
		for i, row in enumerate(staged):
			if row.get("errors"):
				failed += 1
				errors.append(
					{
						"row": row.get("n"),
						"item_code": row.get("item_code"),
						"error": "؛ ".join(row["errors"]),
					}
				)
				continue
			sp = f"import_row_{i}"
			frappe.db.savepoint(sp)
			try:
				item_outcome, pricing_outcomes = _process_staged_row(
					row, warehouse, run_name
				)
				frappe.db.release_savepoint(sp)
				items_out.append(item_outcome)
				pricing_out.extend(pricing_outcomes)
				if item_outcome["action"] == "create":
					created += 1
				else:
					updated += 1
			except Exception as e:
				orig = e
				frappe.db.rollback(save_point=sp)
				try:
					frappe.db.release_savepoint(sp)
				except Exception:
					pass
				frappe.clear_messages()
				failed += 1
				_raw = str(orig)
				_err = {
					"row": row.get("n"),
					"item_code": row.get("item_code"),
					"error": _raw[:500],
				}
				if "Valuation Rate" in _raw and "is required" in _raw:
					_err["error_type"] = "zero_valuation"
				errors.append(_err)
				continue
			if (i + 1) % IMPORT_BATCH_SIZE == 0:
				run.db_set("done_created", created)
				run.db_set("done_updated", updated)
				run.db_set("done_failed", failed)
				frappe.db.commit()
		run.db_set("done_created", created)
		run.db_set("done_updated", updated)
		run.db_set("done_failed", failed)
		run.db_set(
			"result_json",
			frappe.as_json(
				{
					"items": items_out,
					"pricing": pricing_out,
					"errors": errors,
					"missing_columns": (run.missing_columns or "").split("\n")
					if run.missing_columns
					else [],
					"warnings": (run.warnings or "").split("\n") if run.warnings else [],
				}
			),
		)
		run.db_set("status", "Completed" if failed == 0 else "Partial")
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		run.db_set("status", "Failed")
		run.db_set("error", str(e)[:500])
		frappe.db.commit()
	finally:
		_release_import_lock(run_name)


# ---------------------------------------------------------------------------
# Template + export (same column layout, round-trip friendly)
# ---------------------------------------------------------------------------


def _build_workbook(headers, data_rows, groups):
	"""Build an xlsx workbook: data sheet + hidden _meta sheet."""
	import openpyxl

	wb = openpyxl.Workbook()
	ws = wb.active
	ws.title = "الأصناف"
	ws.sheet_view.rightToLeft = True
	ws.append(headers)
	for row in data_rows:
		ws.append(row)
	meta = wb.create_sheet("_meta")
	meta.sheet_state = "hidden"
	meta.append(["key", "value"])
	meta.append(["downloaded_at", frappe.utils.now()])
	meta.append(["groups", frappe.as_json([{"name": g["name"]} for g in groups])])
	meta.append(["warehouse", _warehouse_for_sheet()])
	meta.append(["price_list", PUBLIC_PRICE_LIST])
	buf = io.BytesIO()
	wb.save(buf)
	return buf.getvalue()


def _warehouse_for_sheet():
	try:
		return _resolve_warehouse()
	except Exception:
		return ""


def _export_headers(groups):
	headers = [label for _, label in BASE_COLUMNS]
	headers.extend(_discount_header(g["name"]) for g in groups)
	return headers


@frappe.whitelist()
def staff_download_import_template():
	"""Empty template: Arabic headers + live discount columns + _meta sheet."""
	from biozone_web.utils import require_staff_access

	require_staff_access()
	groups = _active_customer_groups()
	content = _build_workbook(_export_headers(groups), [], groups)
	frappe.response["type"] = "binary"
	frappe.response["filecontent"] = content
	frappe.response["filename"] = "biozone-items-import-template.xlsx"


@frappe.whitelist()
def staff_export_items():
	"""Live-data export in the template layout (enabled items only).

	Enabled-only by design: the layout has no disabled flag, and disabled
	items would fail quantity rows on re-upload with a clear row error.
	Discount percents are per-Item rules only (never group-derived), so a
	re-upload never materializes group rules into item rules.
	"""
	from biozone_web.utils import require_staff_access

	require_staff_access()
	try:
		warehouse = _resolve_warehouse()
	except Exception as e:
		frappe.throw(str(e))
	groups = _active_customer_groups()
	items = frappe.get_all(
		"Item",
		filters={"disabled": 0},
		fields=["item_code", "item_name", "item_group", "stock_uom"],
		order_by="item_name asc",
	)
	codes = [i.item_code for i in items]
	stock_map, price_map, barcode_map, discount_map = {}, {}, {}, {}
	if codes:
		for r in frappe.db.sql(
			"select item_code, actual_qty from `tabBin` "
			"where item_code in %(codes)s and warehouse = %(wh)s",
			{"codes": codes, "wh": warehouse},
			as_dict=True,
		):
			stock_map[r.item_code] = r.actual_qty or 0
		for r in frappe.db.sql(
			"select item_code, price_list_rate from `tabItem Price` "
			"where price_list = 'Standard Selling' and item_code in %(codes)s",
			{"codes": codes},
			as_dict=True,
		):
			price_map[r.item_code] = r.price_list_rate
		for r in frappe.get_all(
			"Item Barcode",
			filters={"parent": ["in", codes], "parenttype": "Item"},
			fields=["parent", "barcode"],
			order_by="idx asc",
		):
			if r.barcode and r.parent not in barcode_map:
				barcode_map[r.parent] = r.barcode
		if groups:
			for r in frappe.db.sql(
				"""
				select pri.item_code, pr.customer_group, pr.discount_percentage
				from `tabPricing Rule Item Code` pri
				inner join `tabPricing Rule` pr on pr.name = pri.parent
				where pr.disable = 0 and pr.apply_on = 'Item Code'
					and pr.customer_group in %(groups)s
					and pri.item_code in %(codes)s
				""",
				{"groups": [g.name for g in groups], "codes": codes},
				as_dict=True,
			):
				discount_map[(r.item_code, r.customer_group)] = r.discount_percentage
	data_rows = []
	for it in items:
		row = [
			it.item_name or "",
			barcode_map.get(it.item_code, ""),
			it.item_code,
			price_map.get(it.item_code, ""),
			stock_map.get(it.item_code, 0),
			it.stock_uom or "",
			it.item_group or "",
		]
		for g in groups:
			v = discount_map.get((it.item_code, g.name))
			row.append("" if v is None else v)
		data_rows.append(row)
	content = _build_workbook(_export_headers(groups), data_rows, groups)
	run = frappe.get_doc(
		{
			"doctype": "Biozone Item Import",
			"run_type": "Export",
			"status": "Completed",
			"file_name": "export",
			"warehouse": warehouse,
			"total_rows": len(data_rows),
			"done_updated": len(data_rows),
			"requested_by": frappe.session.user,
		}
	)
	run.insert(ignore_permissions=True)
	frappe.db.commit()
	frappe.response["type"] = "binary"
	frappe.response["filecontent"] = content
	frappe.response["filename"] = "biozone-items-export.xlsx"
