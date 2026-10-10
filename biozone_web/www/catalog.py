import re
import unicodedata

import frappe

from biozone_web.utils import (
    get_header_context,
    redirect_staff_away_from_store,
)

PAGE_SIZE = 20
ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

# النطاق 1+2+3 (بحث الكتالوج): تهريب LIKE + حد طول + توسعة عربية.
# بلا ضبابية ولا ترتيب كلمات ولا وصف ولا FULLTEXT ولا تغيير واجهة
# (عدا سطر رسالة الرفض الذي يفرضه بند الطول). بلا تغيير مخطط/فهارس.
MAX_SEARCH_LENGTH = 128
SEARCH_TOO_LONG_MESSAGE = "البحث طويل جدًا — الحد الأقصى 128 حرفًا"
_ALEF_HAMZAS = "أإآٱ"


def escape_like(value):
    """تهريب محارف LIKE للبحث الحرفي — القيمة تبقى مُعامَلة."""
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def validate_search_term(term):
    """(مقبول, رسالة): الرفض فوق الحد بلا إرسال لقاعدة البيانات."""
    if len(term or "") > MAX_SEARCH_LENGTH:
        return False, SEARCH_TOO_LONG_MESSAGE
    return True, None


def _word_variants(word):
    """بدائل كلمة واحدة: الأصل + أ/ا + ة/ه + ى/ي (الأخيران نهاية الكلمة)."""
    import re as _re

    out = [word]
    folded = re_sub_alef(word)
    if folded != word:
        out.append(folded)
    for hamza in ("أ", "إ"):
        alt = _re.sub(r"(^|\s)ا", lambda m: m.group(1) + hamza, word)
        if alt != word and alt not in out:
            out.append(alt)
    for swapped in (_teh_swapped(word), _ya_swapped(word)):
        if swapped != word and swapped not in out:
            out.append(swapped)
    return out


def re_sub_alef(term):
    return "".join("ا" if ch in _ALEF_HAMZAS else ch for ch in term)


def _teh_swapped(term):
    """تبادل ة/ه في آخر الكلمة فقط (موضع التاء المربوطة) — بلا خردة وسطية."""
    if term.endswith("ة"):
        return term[:-1] + "ه"
    if term.endswith("ه"):
        return term[:-1] + "ة"
    return term


def _ya_swapped(term):
    """تبادل ى/ي في آخر الكلمة فقط — بلا خردة وسطية."""
    if term.endswith("ى"):
        return term[:-1] + "ي"
    if term.endswith("ي"):
        return term[:-1] + "ى"
    return term


def build_search_variants(term):
    """بدائل البحث بالترتيب: الحرفي أولًا، ثم توسعة كل كلمة على حدة.

    الفواصل الأصلية (بما فيها المسافات المتكررة) تُحفظ حرفيًا في كل
    بديل — بلا دمج مسافات وبلا ترتيب كلمات (خارج النطاق عمدًا).
    """
    import itertools as _it
    import re as _re

    term = (term or "").strip()
    if not term:
        return []
    parts = _re.split(r"(\s+)", term)
    option_lists = [[p] if not p.strip() else _word_variants(p) for p in parts]
    variants = []
    for combo in _it.product(*option_lists):
        v = "".join(combo)
        if v not in variants:
            variants.append(v)
    return variants


def _to_arabic_digits(number):
    return str(number).translate(ARABIC_DIGITS)


def _search_where(term, category):
    """WHERE + params للعدّ والصفحة معًا (تطابق حرفي مضمون).

    - LIKE مهرّب صريح — المحارف الخاصة حروف عادية.
    - بدائل أ/ا وة/ه وى/ي OR — بلا دمج سجلات وبلا تغيير بيانات.
    - باركود مطابق تمامًا (=) مع بقاء فلاتر الفئة والتعطيل.
    """
    term = (term or "").strip()
    where = "disabled = 0"
    params = {}
    if category:
        where += " and item_group = %(category)s"
        params["category"] = category
    lit = None
    if term:
        variants = build_search_variants(term)
        ors = []
        for i, v in enumerate(variants):
            ev = escape_like(v)
            ors.append(
                "(item_name like %%(t%d)s escape '\\\\' "
                "or item_code like %%(t%d)s escape '\\\\')" % (i, i)
            )
            params["t%d" % i] = "%%%s%%" % ev
        ors.append("item_code in (select parent from `tabItem Barcode` where barcode = %(exact)s)")
        params["exact"] = term
        where += " and (" + " or ".join(ors) + ")"
        lit = "%%%s%%" % escape_like(term)
    return where, params, lit


def _search_order(lit):
    if lit is None:
        return "item_name asc, item_code asc"
    return (
        "((item_name like %(lit)s escape '\\\\' "
        "or item_code like %(lit)s escape '\\\\')) desc, "
        "item_name asc, item_code asc"
    )


def search_items(term, category=None, page=1, page_size=PAGE_SIZE):
    """صفوف + إجمالي بترتيب حتمي (الحرفي أولًا ثم اسم ثم كود — بلا تكرار)."""
    term = (term or "").strip()
    try:
        page = max(int(page or 1), 1)
    except (TypeError, ValueError):
        page = 1
    where, params, lit = _search_where(term, category)
    order_params = dict(params)
    order = _search_order(lit)
    if lit is not None:
        order_params["lit"] = lit
    total = frappe.db.sql("select count(*) from `tabItem` where " + where, params)[0][0]
    total_pages = max((total + page_size - 1) // page_size, 1)
    page = min(page, total_pages)
    rows = frappe.db.sql(
        "select item_code, item_name, item_group from `tabItem` where " + where
        + " order by " + order + " limit %(start)s, %(size)s",
        dict(order_params, start=(page - 1) * page_size, size=page_size),
        as_dict=True,
    )
    return (
        [{"item_code": r.item_code, "item_name": r.item_name, "item_group": r.item_group} for r in rows],
        total,
    )


def search_items_with_validation(term, category=None, page=1, page_size=PAGE_SIZE):
    """(صفوف, إجمالي, رسالة): الطويل يُرفض بلا أي استعلام بحث."""
    term = (term or "").strip()
    ok, err = validate_search_term(term)
    if not ok:
        return [], 0, err
    rows, total = search_items(term, category, page, page_size)
    return rows, total, None


# اقتراح التصحيح «هل تقصد؟» (v1): كلمة واحدة فقط، بعد صفر نتائج عادية.
# بلا إثراء أسعار، واستعلام واحد لأسماء الأصناف، وبلا مساس بالبحث الحالي.
SUGGEST_MIN_LENGTH = 4
SUGGEST_MAX_COUNT = 3
SUGGEST_MAX_DISTANCE = 1


def fold_for_distance(text):
    """طيّ معياري للمقارنة فقط (لا يمس المخزن): همزات→ا، ة→ه، ى→ي،
    تشكيل/تطويل محذوف، لاتيني صغير."""

    s = unicodedata.normalize("NFC", text or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("ـ", "")
    s = "".join("ا" if ch in _ALEF_HAMZAS else ch for ch in s)
    s = s.replace("ة", "ه").replace("ى", "ي")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def levenshtein(a, b, limit=1):
    """مسافة تحرير بسقف مبكر (كافٍ لحد v1) — خالصة بلا DB."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = prev[j - 1] + (ca != cb)
            cur.append(min(prev[j] + 1, cur[-1] + 1, cost))
            if cur[-1] < row_min:
                row_min = cur[-1]
        prev = cur
        if row_min > limit:
            return limit + 1
    return prev[-1]


def _is_letters_only(text):
    """حروف عربية/لاتينية فقط بعد حذف التشكيل — قرار: اللاتينية مسموحة.

    الأرقام (ولو عربية) والرموز والشرطات والتطويل مرفوضة. تُستخدم
    للاستعلام وللكلمة المرشحة على حد سواء (إهمال كامل بلا تنظيف جزئي).
    """

    s = "".join(ch for ch in unicodedata.normalize("NFC", text or "") if not unicodedata.combining(ch))
    return bool(s) and all(ch.isalpha() and ch != "ـ" for ch in s)


def _tokenize(name):
    """تقسيم الاسم على الفراغات وعلامات الترقيم الشائعة — اللاحقة
    العربية والتشكيل لا يُحذفان من الرمز المعروض (الفاصل فقط)."""

    return [
        t for t in re.split(r"[\s،؛:\-_/()«»\"'.!؟?,;\[\]+&%*]+", name or "") if t
    ]


def rank_suggestions(folded_q, words):
    """ترتيب خالص: مفردات {مطوي: خام} تُبنى مرة واحدة — كل كلمة فريدة تُطوى مرة.

    العرض لكل مفتاح: الأقصر ثم الأبجدية الأولى. الترتيب: (مسافة، مفتاح، عرض).
    """
    vocab = {}
    checked = set()
    for w in words or []:
        if w in checked:
            continue
        checked.add(w)
        if len(w) < SUGGEST_MIN_LENGTH:
            continue
        if not _is_letters_only(w):
            continue
        vocab.setdefault(fold_for_distance(w), set()).add(w)
    ranked = []
    for fw, raws in vocab.items():
        if fw == folded_q:
            continue
        d = levenshtein(folded_q, fw, SUGGEST_MAX_DISTANCE)
        if d <= SUGGEST_MAX_DISTANCE:
            display = sorted(raws, key=lambda r: (len(r), r))[0]
            ranked.append((d, fw, display))
    ranked.sort(key=lambda t: (t[0], t[1], t[2]))
    return [display for _, _, display in ranked[:SUGGEST_MAX_COUNT]]


def suggest_corrections(term, category=None):
    """حتى 3 كلمات فريدة مرتبة — [] عند أي شرط مفقود (حروف/طول/كلمة واحدة)."""
    term = (term or "").strip()
    if not term or len(term) < SUGGEST_MIN_LENGTH:
        return []
    if len(term.split()) != 1:
        return []
    if not _is_letters_only(term):
        return []
    folded_q = fold_for_distance(term)
    if len(folded_q) < SUGGEST_MIN_LENGTH:
        return []
    where = "disabled = 0"
    params = {}
    if category:
        where += " and item_group = %(category)s"
        params["category"] = category
    names = frappe.db.sql(
        "select distinct item_name from `tabItem` where " + where, params
    )
    words = [w for (name,) in names for w in _tokenize(name)]
    return rank_suggestions(folded_q, words)


def get_context(context):
    redirect_staff_away_from_store()

    context.no_cache = 1
    context.active_page = "catalog"
    context.update(get_header_context())

    search_term = (frappe.form_dict.get("q") or "").strip()
    selected_category = (frappe.form_dict.get("category") or "").strip()

    try:
        page = int(frappe.form_dict.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    page = max(page, 1)

    search_error = None
    ok, err = validate_search_term(search_term)
    if not ok:
        search_error = err
        items, total_count = [], 0
    else:
        items, total_count = search_items(
            search_term, selected_category or None, page, PAGE_SIZE
        )
    total_pages = max((total_count + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, total_pages)

    # اقتراح التصحيح: فقط عند صفر نتائج عادية وبلا خطأ طول — لا يغيّر
    # الاستعلام ولا النتائج ولا العدّ ولا الأسعار. أي عطل فيه يُسجَّل
    # ويُتجاهل حتى لا يكسر الصفحة أبدًا.
    did_you_mean = []
    if total_count == 0 and search_error is None and search_term:
        try:
            did_you_mean = suggest_corrections(search_term, selected_category or None)
        except Exception:
            frappe.log_error(title="Biozone suggest failed", message=frappe.get_traceback())
            did_you_mean = []

    # البند 5: السعر النهائي حسب فئة الطالب (سيرفر-سايد عبر محرك ERPNext)،
    # لا السعر الأساسي الخام — الزائر/غير المفعّل يرى سعر الجمهور، والمفعّل
    # يرى سعر فئته فقط. الأصناف بلا سعر أساسي تبقى price=None (زر معطّل).
    # الإثراء الكامل (سعر/وحدة عرض/مصغرة) في services.store_items —
    # المصدر الوحيد المشترك مع /item. فشل حل الوحدات = إخفاء (fail-closed).
    from biozone_web.services.store_items import enrich_store_items

    items = enrich_store_items(items)
    context.items = items
    # Category chip list: every distinct item_group that has active items.
    categories = [
        d.item_group
        for d in frappe.get_all(
            "Item",
            fields=["item_group"],
            filters={"disabled": 0},
            group_by="item_group",
            order_by="item_group asc",
        )
    ]

    context.items = items
    context.categories = categories
    context.selected_category = selected_category
    context.search_term = search_term
    context.search_error = search_error
    context.did_you_mean = did_you_mean
    context.total_count_ar = _to_arabic_digits(total_count)
    context.page = page
    context.has_prev = page > 1
    context.has_next = page < total_pages
    context.prev_page = page - 1
    context.next_page = page + 1

    return context
