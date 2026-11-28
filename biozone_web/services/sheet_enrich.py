"""مُثري شيت الأصناف — طبقة التوافق (P1).

شيت خام بأي صيغة (xlsx · xls · csv · HTML مُعاد تسميته) → صفوف بالأعمدة
القياسية حرفيًا (`import_export.BASE_COLUMNS`) جاهزة لمسار الاستيراد الرسمي.

قراءة فقط: لا يكتب في القاعدة. الكتابة تتم في `import_export.run_import_job`.
"""

import csv
import io
import re
from collections import Counter, defaultdict

import frappe
from frappe import _

from biozone_web.import_export import BASE_COLUMNS, DISCOUNT_PREFIX

# ---------------------------------------------------------------------------
# ثوابت قابلة للمعايرة (سياسة — تحتاج نقاش المالك قبل التغيير)
# ---------------------------------------------------------------------------

MAX_SCAN_ROWS = 10  # صفوف تُفحص لكشف صف الترويسة (عنوان فوق الترويسة)
MIN_KNOWN_HEADERS = 2  # أقل عدد أعمدة معروفة لاعتبار الصف ترويسة
MIN_HEADER_RATIO = 0.5  # أو نصف أعمدة الصف معروفة
NEIGHBOR_K = 5  # عدد الجيران المستخدمين للتنبؤ
CONFIDENT = 0.66  # عتبة اعتبار تصويت الجيران «واثقًا»
FALLBACK_ITEM_GROUP = "مستلزمات عامة"
FALLBACK_UOM = "قطعة"
MAX_ROWS = 2000  # مطابق لأداة الاستيراد
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# ---------------------------------------------------------------------------
# تطبيع عربي
# ---------------------------------------------------------------------------

_TRANSLATE = str.maketrans(
	{
		"أ": "ا",
		"إ": "ا",
		"آ": "ا",
		"ٱ": "ا",
		"ة": "ه",
		"ى": "ي",
		"ؤ": "و",
		"ئ": "ي",
		"ﻻ": "لا",
	}
)


def normalize_ar(text):
	"""تطبيع عربي للمقارنة: تشكيل · همزات · ة/ه · ى/ي · مسافات الأرقام."""
	s = str(text or "").strip()
	s = re.sub(r"[\u064B-\u0652\u0640]", "", s)
	s = s.translate(_TRANSLATE)
	# «175مل» → «175 مل» · «ب12» → «ب 12»
	s = re.sub(r"(\d)([^\d\s])", r"\1 \2", s)
	s = re.sub(r"([^\d\s])(\d)", r"\1 \2", s)
	s = re.sub(r"[^\w\s]", " ", s)
	return re.sub(r"\s+", " ", s).strip().lower()


def tokens(text):
	return {t for t in normalize_ar(text).split() if len(t) >= 2}


# ---------------------------------------------------------------------------
# جدول المرادفات (ينمو بسطر واحد لكل تسمية جديدة — قرار 20)
# ---------------------------------------------------------------------------

_ALIASES = {
	"name": [
		"الاسم",
		"الصنف",
		"اسم الصنف",
		"البيان",
		"الوصف",
		"name",
		"item",
		"item name",
	],
	"barcode": ["الباركود", "باركود", "barcode"],
	"item_code": [
		"الكود الموحد",
		"الكود",
		"كود الصنف",
		"كود",
		"code",
		"item code",
		"item_code",
	],
	"price": [
		"سعر الجمهور",
		"سعر ج",
		"السعر",
		"سعر",
		"سعر البيع",
		"price",
	],
	"qty": ["الكمية", "كمية", "العدد", "qty", "quantity"],
	"large_uom": ["الوحدة الكبرى", "الوحده الكبري", "large uom"],
	"uom": ["الوحدة", "وحده", "uom", "unit"],
	"factor": ["عدد الوحدات في الكبرى", "المعامل", "factor"],
	"item_group": [
		"القسم",
		"قسم",
		"المجموعة",
		"group",
		"item group",
		"item_group",
	],
	"brand": [
		"البراند",
		"الماركة",
		"ماركة",
		"الشركة",
		"الشركه",
		"brand",
		"company",
	],
}

_DISCOUNT_ALIASES = {
	normalize_ar(alias): group
	for alias, group in {
		"جملة": "جملة",
		"خصم جملة": "جملة",
		"صيدلية": "صيدلية",
		"خصم صيدلية": "صيدلية",
		"مندوب": "الجمهور",
		"خصم مندوب": "الجمهور",
		"خصم": "الجمهور",
		"جمهور": "الجمهور",
		"خصم الجمهور": "الجمهور",
	}.items()
}

_HEADER_MAP = {}
for _key, _labels in _ALIASES.items():
	for _label in _labels:
		_HEADER_MAP[normalize_ar(_label)] = _key


def resolve_header(raw):
	"""ترويسة خام → مفتاح داخلي أو ('discount', group) أو None."""
	h = normalize_ar(raw)
	if not h:
		return None
	if h in _HEADER_MAP:
		return _HEADER_MAP[h]
	group = _DISCOUNT_ALIASES.get(h)
	if group:
		return ("discount", group)
	return None


# ---------------------------------------------------------------------------
# كشف الصيغة وقراءتها بالمحتوى لا بالامتداد
# ---------------------------------------------------------------------------


def detect_format(content, filename=""):
	"""xlsx · xls · html · csv — من البايتات الأولى، لا من الامتداد."""
	head = content[:8]
	if head[:4] == b"PK\x03\x04":
		return "xlsx"
	if head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
		return "xls"
	text_head = content[:4096].lstrip().lower()
	if text_head.startswith(b"<") or b"<table" in text_head:
		return "html"
	return "csv"


def _cell_str(value):
	if value is None:
		return ""
	if isinstance(value, float) and value.is_integer():
		return str(int(value))
	if isinstance(value, bool):
		return str(value)
	return str(value).strip()


def _read_xlsx(content):
	import openpyxl

	wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
	sheets = []
	for name in wb.sheetnames:
		ws = wb[name]
		rows = [[_cell_str(c) for c in r] for r in ws.iter_rows(values_only=True)]
		sheets.append((name, rows))
	return sheets


def _read_xls(content):
	import xlrd

	wb = xlrd.open_workbook(file_contents=content)
	sheets = []
	for sheet in wb.sheets():
		rows = []
		for r in range(sheet.nrows):
			rows.append([_cell_str(v) for v in sheet.row_values(r)])
		sheets.append((sheet.name, rows))
	return sheets


def _decode_text(content):
	"""فك الترميز العربي القديم: UTF-8 ثم CP1256 ثم Windows-1252."""
	last_error = None
	for encoding in ("utf-8-sig", "utf-8", "cp1256", "windows-1252"):
		try:
			return content.decode(encoding)
		except (UnicodeDecodeError, LookupError) as exc:
			last_error = exc
	raise ValueError(_("تعذر قراءة ترميز الملف: {0}").format(last_error))


def _read_html(content):
	from lxml import html as lxml_html

	# فك الترميز أولًا — lxml يفترض Latin-1 على البايتات بلا <meta charset>.
	text = _decode_text(content)
	tree = lxml_html.fromstring(text)
	tables = tree.xpath("//table")
	sheets = []
	for i, table in enumerate(tables, 1):
		rows = []
		for tr in table.xpath(".//tr"):
			cells = tr.xpath("./td|./th")
			rows.append([" ".join(c.text_content().split()) for c in cells])
		rows = [r for r in rows if any(r)]
		if rows:
			sheets.append((f"table{i}", rows))
	return sheets


def _read_csv(content):
	text = _decode_text(content)
	sample = "\n".join(text.splitlines()[:5])
	delimiter = ","
	try:
		delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
	except csv.Error:
		counts = {d: sample.count(d) for d in (",", ";", "\t", "|")}
		if max(counts.values()):
			delimiter = max(counts, key=counts.get)
	rows = [
		[(c or "").strip() for c in r]
		for r in csv.reader(io.StringIO(text), delimiter=delimiter)
	]
	return [("csv", [r for r in rows if any(r)])]


_READERS = {
	"xlsx": _read_xlsx,
	"xls": _read_xls,
	"html": _read_html,
	"csv": _read_csv,
}


def read_sheets(content, filename=""):
	"""كل أوراق الملف كصفوف نصية (بدون أي قرار)."""
	fmt = detect_format(content, filename)
	try:
		sheets = _READERS[fmt](content)
	except ValueError:
		raise
	except Exception as exc:
		frappe.log_error(title="sheet_enrich read failed", message=f"{fmt}: {exc}")
		raise ValueError(_("الملف مش مقروء — أعد تصديره."))
	if not any(rows for _, rows in sheets):
		raise ValueError(_("الملف لا يحتوي على صفوف بيانات"))
	return fmt, sheets


# ---------------------------------------------------------------------------
# كشف صف الترويسة واختيار الورقة
# ---------------------------------------------------------------------------


def detect_header_index(rows):
	"""أول/أفضل صف ترويسة داخل أول MAX_SCAN_ROWS؛ None إن لم يوجد."""
	best_score, best_idx = -1, None
	for idx, row in enumerate(rows[:MAX_SCAN_ROWS]):
		keys = {resolve_header(c) for c in row}
		keys.discard(None)
		score = len(keys)
		if score < MIN_KNOWN_HEADERS:
			continue
		non_empty = sum(1 for c in row if str(c).strip()) or 1
		if score / non_empty < MIN_HEADER_RATIO and score < 3:
			continue
		if score > best_score:
			best_score, best_idx = score, idx
	return best_idx


def pick_sheet(sheets):
	"""أفضل ورقة تلقائيًا: أعلى عدد ترويسات معروفة ثم أكثر صفوفًا — بلا سؤال."""
	best = None
	for name, rows in sheets:
		idx = detect_header_index(rows)
		if idx is None:
			continue
		keys = {resolve_header(c) for c in rows[idx]}
		keys.discard(None)
		score = (len(keys), len(rows) - idx - 1)
		if best is None or score > best[0]:
			best = (score, name, rows, idx)
	if best is None:
		return None
	return best[1], best[2], best[3]


def map_columns(header_row):
	key_by_index = {}
	unknown = []
	for i, cell in enumerate(header_row):
		key = resolve_header(cell)
		if key is None:
			if str(cell).strip():
				unknown.append(str(cell).strip())
			continue
		if key in key_by_index.values():
			continue
		key_by_index[i] = key
	return key_by_index, unknown


def parse_any(content, filename=""):
	"""شيت خام → قاموس موحّد (صيغة · ورقة · ترويسة · صفوف · أعمدة)."""
	if not content:
		raise ValueError(_("الملف فارغ"))
	if len(content) > MAX_UPLOAD_BYTES:
		raise ValueError(_("حجم الملف يتجاوز الحد المسموح (5MB)"))
	fmt, sheets = read_sheets(content, filename)
	picked = pick_sheet(sheets)
	if picked is None:
		raise ValueError(_("لم أجد صف ترويسة معروفًا في الملف"))
	name, rows, header_index = picked
	key_by_index, unknown = map_columns(rows[header_index])
	data = []
	for cells in rows[header_index + 1 :]:
		if not any(str(c).strip() for c in cells):
			continue
		data.append(cells)
	if not data:
		raise ValueError(_("الملف لا يحتوي على صفوف بيانات"))
	if len(data) > MAX_ROWS:
		raise ValueError(_("عدد الصفوف يتجاوز الحد المسموح (2000 صف)"))
	return {
		"format": fmt,
		"sheet": name,
		"header_index": header_index,
		"key_by_index": key_by_index,
		"unknown_headers": unknown,
		"rows": data,
	}


def row_value(cells, key_by_index, key):
	for i, k in key_by_index.items():
		if k == key:
			return cells[i] if i < len(cells) else ""
	return ""


# ---------------------------------------------------------------------------
# الكتالوج: مطابقة 100% + جيران + براند
# ---------------------------------------------------------------------------


class CatalogIndex:
	"""فهرس قراءة فقط لأصناف النظام (اسم مطبّع · توكنات · تصويت براند)."""

	def __init__(self, items):
		self.items = items
		self.by_name = {}
		self.by_code = {}
		self._tokens = {}
		self.brand_hint = defaultdict(Counter)
		for it in items:
			code = it.get("item_code")
			self.by_code[code] = it
			norm = normalize_ar(it.get("item_name"))
			if norm:
				self.by_name.setdefault(norm, it)
			toks = tokens(it.get("item_name"))
			self._tokens[code] = toks
			brand = it.get("brand")
			if brand:
				for t in toks:
					self.brand_hint[t][brand] += 1

	def exact(self, name):
		return self.by_name.get(normalize_ar(name))

	def neighbors(self, name, k=NEIGHBOR_K):
		ts = tokens(name)
		if not ts:
			return []
		scored = []
		for it in self.items:
			other = self._tokens.get(it.get("item_code")) or set()
			if not other:
				continue
			union = len(ts | other) or 1
			scored.append((len(ts & other) / union, it))
		scored.sort(key=lambda x: x[0], reverse=True)
		return [s for s in scored[:k] if s[0] > 0]

	def brand_for(self, company):
		"""براند من عمود الشركة عبر تصويت أسماء الأصناف — لا اختراع."""
		toks = tokens(company)
		if not toks:
			return ""
		votes = Counter()
		for t in toks:
			if t in self.brand_hint:
				votes.update(self.brand_hint[t])
		return votes.most_common(1)[0][0] if votes else ""

	def group_for(self, name):
		near = self.neighbors(name, k=1)
		if near and near[0][0] >= CONFIDENT:
			return near[0][1].get("item_group") or "", near[0][0]
		return "", (near[0][0] if near else 0.0)

	def uom_for(self, name):
		near = self.neighbors(name, k=1)
		if near and near[0][0] >= CONFIDENT:
			return near[0][1].get("stock_uom") or ""
		return ""


def load_catalog():
	items = frappe.get_all(
		"Item",
		filters={"disabled": 0},
		fields=["item_code", "item_name", "item_group", "brand", "stock_uom"],
		limit_page_length=0,
	)
	return CatalogIndex(items)


def next_bz_number():
	"""أعلى BZ- رقمي في القاعدة + 1 (لا أرقام مكتوبة نصًا)."""
	codes = frappe.get_all(
		"Item",
		filters={"item_code": ("like", "BZ-%")},
		pluck="item_code",
		limit_page_length=0,
	)
	top = 0
	for code in codes:
		suffix = str(code)[3:]
		if suffix.isdigit():
			top = max(top, int(suffix))
	return top + 1


# ---------------------------------------------------------------------------
# الإثراء: كل صف → قرار + سبب + قيمة ناقصة
# ---------------------------------------------------------------------------


def _strict_num(raw):
	"""رقم صارم — لا يُحوَّل النص العشوائي إلى صفر."""
	text = str(raw or "").strip().replace(",", "").replace("٬", "")
	if not text:
		return None, None
	try:
		return True, float(text)
	except ValueError:
		return False, None


def _clean_discount(raw):
	ok, num = _strict_num(raw)
	if ok is None:
		return ""
	if not ok:
		return None
	if num < 0 or num > 100:
		return None
	return ("%g" % num)


def _discount_columns(key_by_index):
	return sorted({k[1] for k in key_by_index.values() if isinstance(k, tuple)})


def enrich(parsed, index, start_code=None):
	"""صفوف خام → صفوف قياسية + إحصاءات + مستبعدين بأسبابهم."""
	key_by_index = parsed["key_by_index"]
	discount_groups = _discount_columns(key_by_index)
	code_no = start_code if start_code is not None else next_bz_number()
	used_codes = set(index.by_code)
	seen_names = {}
	rows, excluded = [], []

	stats = Counter()

	for offset, cells in enumerate(parsed["rows"], 1):
		row_no = parsed["header_index"] + 1 + offset
		name = str(row_value(cells, key_by_index, "name")).strip()
		if not name:
			excluded.append({"row": row_no, "reason": "اسم فارغ"})
			continue

		norm = normalize_ar(name)
		if norm in seen_names:
			excluded.append({"row": row_no, "reason": "مكرر داخل الملف", "duplicate_of": seen_names[norm]})
			continue

		price_ok, price = _strict_num(row_value(cells, key_by_index, "price"))
		if price_ok is False:
			excluded.append({"row": row_no, "reason": "سعر غير صالح"})
			continue
		if price is not None and price < 0:
			excluded.append({"row": row_no, "reason": "سعر غير صالح"})
			continue

		discounts = {}
		bad_discount = False
		for group in discount_groups:
			raw = row_value(cells, key_by_index, ("discount", group))
			value = _clean_discount(raw)
			if value is None:
				bad_discount = True
				break
			if value != "":
				discounts[group] = value
		if bad_discount:
			excluded.append({"row": row_no, "reason": "خصم غير صالح"})
			continue

		seen_names[norm] = row_no

		existing = index.exact(name)
		if existing is None:
			code = row_value(cells, key_by_index, "item_code")
			code = str(code).strip() if code else ""
			if code and code in index.by_code:
				existing = index.by_code[code]

		if existing:
			action = "update"
			item_code = existing["item_code"]
			item_group = existing.get("item_group") or ""
			uom = existing.get("stock_uom") or ""
			brand = existing.get("brand") or ""
		else:
			action = "create"
			while True:
				item_code = f"BZ-{code_no}"
				code_no += 1
				if item_code not in used_codes and not frappe.db.exists("Item", item_code):
					break
			used_codes.add(item_code)
			item_group = _pick_group(cells, key_by_index)
			uom = str(row_value(cells, key_by_index, "uom")).strip()
			company = str(row_value(cells, key_by_index, "brand")).strip()
			brand = ""
			if company:
				brand = index.brand_for(company)
			if not brand:
				brand_from_sheet = company if _brand_exists(company) else ""
				brand = brand_from_sheet
			if not item_group:
				item_group, _confidence = index.group_for(name)
			if not item_group:
				item_group = FALLBACK_ITEM_GROUP
			if not uom:
				uom = index.uom_for(name) or FALLBACK_UOM

		if not brand:
			stats["without_brand"] += 1

		stats["created" if action == "create" else "updated"] += 1
		rows.append(
			{
				"row": row_no,
				"action": action,
				"name": name,
				"barcode": str(row_value(cells, key_by_index, "barcode")).strip(),
				"item_code": item_code,
				"price": "" if price is None else ("%g" % price),
				"qty": str(row_value(cells, key_by_index, "qty")).strip(),
				"large_uom": str(row_value(cells, key_by_index, "large_uom")).strip(),
				"uom": uom,
				"factor": str(row_value(cells, key_by_index, "factor")).strip(),
				"item_group": item_group,
				"brand": brand,
				"discounts": discounts,
			}
		)

	return {
		"rows": rows,
		"discount_groups": discount_groups,
		"excluded": excluded,
		"stats": stats,
		"next_code": code_no,
	}


def _pick_group(cells, key_by_index):
	return str(row_value(cells, key_by_index, "item_group")).strip()


_BRAND_CACHE = {}


def _brand_exists(name):
	if name not in _BRAND_CACHE:
		_BRAND_CACHE[name] = bool(frappe.db.exists("Brand", name))
	return _BRAND_CACHE[name]


def standard_headers(discount_groups):
	headers = [label for _key, label in BASE_COLUMNS]
	return headers + [f"{DISCOUNT_PREFIX}{g}" for g in discount_groups]


def standard_row(row, discount_groups=()):
	return [
		row["name"],
		row["barcode"],
		row["item_code"],
		row["price"],
		row["qty"],
		row["large_uom"],
		row["uom"],
		row["factor"],
		row["item_group"],
		row["brand"],
	] + [row["discounts"].get(g, "") for g in discount_groups]
