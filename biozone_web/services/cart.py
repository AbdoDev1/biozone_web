import hashlib
import json
import math
import re
import uuid

import frappe
from frappe import _

from biozone_web.request_id import get_request_id


MAX_CART_LINES = 200
MAX_LINE_QTY = 100000.0
MAX_UOM_LENGTH = 140
MAX_SOURCE_KEY_LENGTH = 160
SOURCE_UID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
PRICE_LIST = "Standard Selling"
EXPIRY_DAYS = 30
SNAPSHOT_TTL_DAYS = 7
SNAPSHOT_FLAG = "biozone_cart_snapshots_enabled"
UNDO_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,64}$")

STATUS_ACTIVE = "Active"
STATUS_CONVERTED = "Converted"
STATUS_EXPIRED = "Expired"

SNAPSHOT_AVAILABLE = "Available"
SNAPSHOT_RESTORED = "Restored"
SNAPSHOT_EXPIRED = "Expired"

DUPLICATE_ERRORS = tuple(
    exc
    for exc in (
        getattr(frappe, "DuplicateEntryError", None),
        getattr(frappe, "UniqueValidationError", None),
        getattr(getattr(frappe, "db", None), "IntegrityError", None),
    )
    if exc
)


class CartError(Exception):
    def __init__(self, error_code, message, *, status=422, item_code=None, extra=None):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status = status
        self.item_code = item_code
        self.extra = extra or {}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _parse_json(value, default):
    if not value:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value)
    except Exception:
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _money(value):
    from biozone_web.units import money2

    return money2(value)


def _now():
    return frappe.utils.now_datetime()


def _expiry(now):
    return frappe.utils.add_to_date(now, days=EXPIRY_DAYS)


def _require_text(value, field, *, max_length=None, required=True):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CartError("CART_INPUT_INVALID", _("بيانات غير صحيحة في الطلب"), status=422)
    value = value.strip()
    if required and not value:
        raise CartError("CART_INPUT_INVALID", _("بيانات غير صحيحة في الطلب"), status=422)
    if max_length and len(value) > max_length:
        raise CartError("CART_INPUT_INVALID", _("بيانات غير صحيحة في الطلب"), status=422)
    return value


def _optional_text(value, field, *, max_length=None):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CartError("CART_INPUT_INVALID", _("بيانات غير صحيحة في الطلب"), status=422)
    value = value.strip()
    if max_length and len(value) > max_length:
        raise CartError("CART_INPUT_INVALID", _("بيانات غير صحيحة في الطلب"), status=422)
    return value


def _validate_qty(value):
    if isinstance(value, bool):
        raise CartError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"), status=422)
    try:
        qty = float(value)
    except (TypeError, ValueError):
        raise CartError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"), status=422)
    if not math.isfinite(qty) or qty <= 0 or qty > MAX_LINE_QTY:
        raise CartError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"), status=422)
    qty = round(qty, 3)
    if qty <= 0:
        raise CartError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"), status=422)
    return qty


def _validate_cart_version(value):
    if value is None or isinstance(value, bool):
        raise CartError("CART_VERSION_REQUIRED", _("نسخة السلة مطلوبة — حدّث السلة"), status=422)
    if isinstance(value, float) and not value.is_integer():
        raise CartError("CART_VERSION_REQUIRED", _("نسخة السلة مطلوبة — حدّث السلة"), status=422)
    try:
        version = int(value)
    except (TypeError, ValueError):
        raise CartError("CART_VERSION_REQUIRED", _("نسخة السلة مطلوبة — حدّث السلة"), status=422)
    if version < 0:
        raise CartError("CART_VERSION_REQUIRED", _("نسخة السلة مطلوبة — حدّث السلة"), status=422)
    return version


def _validate_source_uid(value):
    value = _require_text(value, "source_uid", max_length=64)
    if not SOURCE_UID_RE.fullmatch(value):
        raise CartError("MERGE_SOURCE_INVALID", _("معرّف مصدر السلة غير صالح"), status=422)
    return value


def _validate_token(value):
    value = _require_text(value, "token", max_length=64)
    if not TOKEN_RE.fullmatch(value):
        raise CartError("CHECKOUT_TOKEN_INVALID", _("معرّف التأكيد غير صالح"), status=422)
    return value


def _validate_fingerprint(value):
    value = _require_text(value, "fingerprint", max_length=64)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise CartError("CART_QUOTE_INVALID", _("بصمة السعر غير صالحة — حدّث السلة"), status=422)
    return value.lower()


def snapshots_enabled():
    """Feature flag: absent/0/"" means disabled (fail-safe)."""
    return bool(frappe.utils.cint(frappe.conf.get(SNAPSHOT_FLAG, 0)))


def _restore_token_hash(user, token):
    return hashlib.sha256(f"{user}|{token}".encode("utf-8")).hexdigest()


def _snapshot_key(user, cart_name, source_version, reason, item_code=None):
    raw = "|".join(
        [
            str(user or ""),
            str(cart_name or ""),
            str(source_version),
            str(reason or ""),
            str(item_code or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _snapshot_lines(cart, reason, item_code=None):
    if reason == "Remove":
        line = _find_line(cart, item_code)
        rows = [line] if line else []
    else:
        rows = list(cart.items or [])
    return [
        {
            "item_code": (row.item_code or "").strip(),
            "qty": round(frappe.utils.flt(row.qty), 3),
            "uom": (row.uom or "").strip(),
        }
        for row in rows
        if (row.item_code or "").strip()
    ]


def _snapshot_payload(snapshot):
    if not snapshot:
        return None
    return {
        "snapshot": snapshot.name,
        "reason": snapshot.reason,
        "scope": snapshot.scope,
        "expires_on": str(snapshot.expires_on) if snapshot.expires_on else None,
        "item_count": int(snapshot.item_count or 0),
        "total_units": float(snapshot.total_units or 0),
    }


def _with_undo(state, snapshot):
    if snapshot:
        state["undo"] = _snapshot_payload(snapshot)
    return state


def _get_snapshot_by_key(snapshot_key):
    name = frappe.db.get_value(
        "BZ Cart Snapshot", {"snapshot_key": snapshot_key}, "name"
    )
    return frappe.get_doc("BZ Cart Snapshot", name) if name else None


def _latest_snapshot(user, cart_name, *, now=None):
    now = now or _now()
    rows = frappe.get_all(
        "BZ Cart Snapshot",
        filters={
            "source_user": user,
            "source_cart": cart_name,
            "status": SNAPSHOT_AVAILABLE,
            "expires_on": [">", now],
        },
        fields=["name", "reason", "scope", "expires_on", "item_count", "total_units"],
        order_by="created_on desc",
        limit=1,
    )
    return frappe._dict(rows[0]) if rows else None


def _insert_snapshot(cart, customer, reason, scope, source_version, item_code=None):
    lines = _snapshot_lines(cart, reason, item_code)
    if not lines:
        return None
    now = _now()
    doc = frappe.get_doc(
        {
            "doctype": "BZ Cart Snapshot",
            "snapshot_key": _snapshot_key(
                frappe.session.user, cart.name, source_version, reason, item_code
            ),
            "status": SNAPSHOT_AVAILABLE,
            "reason": reason,
            "scope": scope,
            "source_user": frappe.session.user,
            "source_customer": customer,
            "source_cart": cart.name,
            "source_version": source_version,
            "items_json": _json(lines),
            "item_count": len(lines),
            "total_units": round(sum(row["qty"] for row in lines), 3),
            "actor": frappe.session.user,
            "request_id": get_request_id(),
            "created_on": now,
            "expires_on": frappe.utils.add_to_date(now, days=SNAPSHOT_TTL_DAYS),
            "restore_token_hash": None,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc


def _merge_items_into_cart(cart, rows, customer):
    """Merge normalized lines into a locked cart.

    No save, no commit, no response shaping. Conflicts are collected (not
    raised) so transfer_apply can turn the first one into a 409 while undo
    can report the rest as skipped lines.
    """
    now = _now()
    conflicts = []
    applied = 0
    for line in rows:
        code = line["item_code"]
        uom = (line.get("uom") or "").strip()
        qty = round(frappe.utils.flt(line.get("qty")), 3)
        existing = _find_line(cart, code)
        if existing:
            if (existing.uom or "").strip() != uom:
                conflicts.append(
                    {
                        "item_code": code,
                        "code": "TARGET_UOM_CONFLICT",
                        "message": _("الصنف موجود في سلة الحساب بوحدة مختلفة"),
                    }
                )
                continue
            new_qty = round(frappe.utils.flt(existing.qty) + qty, 3)
            if new_qty > MAX_LINE_QTY:
                conflicts.append(
                    {
                        "item_code": code,
                        "code": "QTY_LIMIT",
                        "message": _("الكمية أكبر من الحد المسموح"),
                    }
                )
                continue
            existing.qty = new_qty
            existing.updated_on = now
        else:
            if len(cart.items or []) >= MAX_CART_LINES:
                conflicts.append(
                    {
                        "item_code": code,
                        "code": "LINE_LIMIT",
                        "message": _("عدد أصناف السلة أكبر من الحد المسموح"),
                    }
                )
                continue
            cart.append(
                "items",
                {
                    "item_code": code,
                    "qty": qty,
                    "uom": uom,
                    "added_on": now,
                    "updated_on": now,
                },
            )
        applied += 1
    return cart, conflicts, applied


_SKIP_REASONS = {
    "ITEM_NOT_FOUND": "ITEM_NOT_FOUND",
    "ITEM_DISABLED": "ITEM_DISABLED",
    "NO_PRICE": "NO_PRICE",
    "UOM_UNAVAILABLE": "UOM_CONFLICT",
    "UOM_CHANGED": "UOM_CONFLICT",
    "SOURCE_UOM_CONFLICT": "UOM_CONFLICT",
    "QTY_LIMIT": "QTY_CAP",
    "LINE_LIMIT": "LINE_CAP",
}


def _undo_skips(conflicts):
    skipped = []
    seen = set()
    for conflict in conflicts or []:
        code = conflict.get("item_code") or ""
        reason = _SKIP_REASONS.get(
            conflict.get("code"), conflict.get("code") or "UNAVAILABLE"
        )
        key = (code, reason)
        if key in seen:
            continue
        seen.add(key)
        skipped.append(
            {
                "item_code": code,
                "reason": reason,
                "message": conflict.get("message") or "",
            }
        )
    return skipped


def _cart_was_converted(cart_name):
    if not cart_name:
        return False
    return frappe.db.get_value("BZ Cart", cart_name, "status") == STATUS_CONVERTED


def _undo_replay(snapshot, cart, customer):
    state = cart_state(cart, customer=customer) if cart else {}
    state.update(
        {
            "ok": True,
            "already_restored": True,
            "applied_mode": snapshot.restore_mode,
            "restored_count": int(snapshot.restored_count or 0),
            "skipped_count": int(snapshot.skipped_count or 0),
            "skipped": _parse_json(snapshot.skipped_json, []),
        }
    )
    return state


def undo(snapshot_name, restore_token, cart_version=None):
    """Restore a Clear/Remove snapshot into the current Active cart.

    `restore_token` is a client-generated idempotency token (32-64 hex).
    Its hash is scoped to the user, so a different user replaying the same
    token cannot read another user's committed result.
    """
    customer = require_customer()
    user = frappe.session.user
    if not snapshots_enabled():
        raise CartError("FEATURE_DISABLED", _("التراجع غير متاح حاليًا"), status=404)
    name = _require_text(snapshot_name, "snapshot", max_length=140)
    token = _require_text(restore_token, "restore_token", max_length=64)
    if not UNDO_TOKEN_RE.fullmatch(token):
        raise CartError("CART_INPUT_INVALID", _("بيانات غير صحيحة في الطلب"), status=422)
    token_hash = _restore_token_hash(user, token)

    rows = frappe.db.sql(
        "select name from `tabBZ Cart Snapshot` where name = %s for update",
        (name,),
        as_dict=True,
    )
    if not rows:
        raise CartError("SNAPSHOT_NOT_AVAILABLE", _("النسخة غير متاحة للاستعادة"), status=404)
    snapshot = frappe.get_doc("BZ Cart Snapshot", name)
    if snapshot.source_user != user:
        raise CartError("SNAPSHOT_NOT_AVAILABLE", _("النسخة غير متاحة للاستعادة"), status=404)

    cart = get_active_cart(user, for_update=True)
    if cart is not None and cart.name == snapshot.source_cart:
        if frappe.db.exists(
            "BZ Checkout Receipt",
            {"cart": cart.name, "status": "Processing"},
        ):
            raise CartError(
                "CHECKOUT_IN_PROGRESS",
                _("طلبك قيد المعالجة — انتظر قليلًا ثم حدّث الصفحة"),
                status=409,
            )
    if snapshot.status == SNAPSHOT_RESTORED:
        if (snapshot.restore_token_hash or "") == token_hash:
            return _undo_replay(snapshot, cart, customer)
        raise CartError(
            "SNAPSHOT_ALREADY_RESTORED",
            _("تمت استعادة هذه النسخة بالفعل"),
            status=409,
        )
    if snapshot.status != SNAPSHOT_AVAILABLE:
        raise CartError("SNAPSHOT_NOT_AVAILABLE", _("النسخة غير متاحة للاستعادة"), status=404)
    if snapshot.expires_on and frappe.utils.get_datetime(snapshot.expires_on) <= _now():
        raise CartError("SNAPSHOT_EXPIRED", _("انتهت صلاحية النسخة"), status=422)
    if (snapshot.source_customer or None) != (customer or None):
        raise CartError("SNAPSHOT_NOT_AVAILABLE", _("النسخة غير متاحة للاستعادة"), status=404)
    if cart is None or cart.name != snapshot.source_cart:
        if cart is None:
            # Undo may have waited on the checkout row lock, so this
            # transaction's REPEATABLE-READ view can predate the conversion.
            # Nothing was written on this path: drop the read-only view and
            # re-read the status in a fresh transaction for the message.
            frappe.db.rollback()
        converted = _cart_was_converted(snapshot.source_cart)
        raise CartError(
            "SNAPSHOT_NOT_AVAILABLE",
            _("هذه السلة تحوّلت إلى طلب ولا يمكن استعادتها.")
            if converted
            else _("النسخة غير متاحة للاستعادة"),
            status=404,
        )

    if snapshot.scope == "Cart":
        expected = _validate_cart_version(cart_version)
        if expected != int(cart.version or 0):
            error = _conflict_current(
                cart,
                customer,
                _("تغيّرت السلة من جهاز آخر — حدّثها ثم أعد المحاولة"),
            )
            error.extra["merge_available"] = True
            raise error
    # Line scope ignores cart_version by design: adding a line is safe and
    # the restore token prevents a double-apply.

    items = _parse_json(snapshot.items_json, [])
    normalized, conflicts, ctx = _normalize_source_items(items, customer)
    skipped = _undo_skips(conflicts)
    available = [line for line in normalized if line.get("available", True)]

    now = _now()
    if snapshot.scope == "Cart" and not (cart.items or []):
        applied_mode = "Replace"
        rows = []
        for line in available:
            if len(rows) >= MAX_CART_LINES:
                skipped.append(
                    {
                        "item_code": line["item_code"],
                        "reason": "LINE_CAP",
                        "message": _("عدد أصناف السلة أكبر من الحد المسموح"),
                    }
                )
                continue
            rows.append(
                {
                    "item_code": line["item_code"],
                    "qty": line["qty"],
                    "uom": line["uom"],
                    "added_on": now,
                    "updated_on": now,
                }
            )
        cart.items = []
        for row in rows:
            cart.append("items", row)
        restored = len(rows)
    else:
        applied_mode = "Merge"
        cart, merge_conflicts, restored = _merge_items_into_cart(cart, available, customer)
        skipped.extend(_undo_skips(merge_conflicts))

    if not restored:
        raise CartError(
            "UNDO_NOTHING_RESTORED",
            _("تعذّر استعادة أي صنف من هذه النسخة"),
            status=422,
        )

    _touch(cart, customer)
    snapshot.status = SNAPSHOT_RESTORED
    snapshot.restored_on = now
    snapshot.restored_by = user
    snapshot.restore_request_id = get_request_id()
    snapshot.restore_token_hash = token_hash
    snapshot.restore_mode = applied_mode
    snapshot.restored_count = restored
    snapshot.skipped_count = len(skipped)
    snapshot.skipped_json = _json(skipped)
    try:
        snapshot.save(ignore_permissions=True)
    except DUPLICATE_ERRORS:
        frappe.db.rollback()
        locked = frappe.db.sql(
            "select name from `tabBZ Cart Snapshot` where name = %s for update",
            (name,),
            as_dict=True,
        )
        snapshot = frappe.get_doc("BZ Cart Snapshot", name) if locked else None
        if snapshot and (snapshot.restore_token_hash or "") == token_hash:
            cart = get_active_cart(user, for_update=True)
            return _undo_replay(snapshot, cart, customer)
        raise CartError(
            "UNDO_IN_PROGRESS",
            _("الاستعادة قيد التنفيذ — أعد المحاولة"),
            status=409,
        )

    state = cart_state(cart, customer=customer)
    state.update(
        {
            "ok": True,
            "already_restored": False,
            "applied_mode": applied_mode,
            "restored_count": restored,
            "skipped_count": len(skipped),
            "skipped": skipped,
        }
    )
    return state


def require_customer():
    from biozone_web.utils import find_customer_for_current_user, is_customer_order_eligible

    customer = find_customer_for_current_user()
    if not customer:
        raise CartError(
            "CUSTOMER_MISSING",
            _("لا يوجد حساب عميل مرتبط بهذا المستخدم — تواصل مع إدارة المتجر."),
            status=422,
        )
    row = frappe.db.get_value("Customer", customer, ["disabled"], as_dict=True)
    if not row:
        raise CartError(
            "CUSTOMER_MISSING",
            _("لا يوجد حساب عميل مرتبط بهذا المستخدم — تواصل مع إدارة المتجر."),
            status=422,
        )
    if row.disabled:
        raise CartError(
            "CUSTOMER_GROUP_INELIGIBLE",
            _("فئة حسابك غير مفعّلة حاليًا — تواصل مع إدارة المتجر."),
            status=422,
        )
    if not is_customer_order_eligible(customer):
        raise CartError(
            "CUSTOMER_GROUP_INELIGIBLE",
            _("فئة حسابك غير مفعّلة حاليًا — تواصل مع إدارة المتجر."),
            status=422,
        )
    return customer


def get_active_cart(user=None, *, for_update=False):
    user = user or frappe.session.user
    if not user or user == "Guest":
        return None
    if for_update:
        rows = frappe.db.sql(
            """
            select name
            from `tabBZ Cart`
            where user = %s and status = %s and active_key = %s
            for update
            """,
            (user, STATUS_ACTIVE, user),
            as_dict=True,
        )
        if not rows:
            return None
        name = rows[0].name
    else:
        name = frappe.db.get_value(
            "BZ Cart",
            {"user": user, "status": STATUS_ACTIVE, "active_key": user},
            "name",
        )
    if not name:
        return None
    cart = frappe.get_doc("BZ Cart", name)
    if cart_is_expired(cart):
        expire_cart(cart)
        return None
    return cart


def cart_is_expired(cart, now=None):
    """السلة المنتهية لا تُعاد حتى لو تأخر عامل الجدولة اليومي."""
    if not cart or cart.status != STATUS_ACTIVE or not cart.expires_on:
        return False
    now = now or _now()
    try:
        return frappe.utils.get_datetime(cart.expires_on) <= now
    except Exception:
        return False


def expire_cart(cart):
    """إنهاء السلة مرة واحدة فقط، مع تفريغ active_key ورفع الإصدار.

    يُقفل صف السلة بالاسم (المفتاح الأساسي) ويُعاد قراءته قبل الكتابة،
    حتى لا يكتب إصدار قديم فوق تعديل متزامن (كمية/إضافة صنف/تعديل نشاط).
    القفل بالاسم فقط: لا فهارس غير فريدة ولا gap locks.
    """
    if not cart:
        return False
    name = cart if isinstance(cart, str) else cart.name
    if not name:
        return False
    locked = frappe.db.sql(
        "select name from `tabBZ Cart` where name = %s for update",
        (name,),
        as_dict=True,
    )
    if not locked:
        return False
    cart = frappe.get_doc("BZ Cart", name)
    if cart.status != STATUS_ACTIVE:
        return False
    cart.status = STATUS_EXPIRED
    cart.active_key = None
    cart.version = int(cart.version or 0) + 1
    cart.save(ignore_permissions=True)
    return True


def _item_meta(codes):
    codes = [c for c in dict.fromkeys((c or "").strip() for c in (codes or [])) if c]
    if not codes:
        return {}
    rows = frappe.get_all(
        "Item",
        filters={"item_code": ["in", codes]},
        fields=["item_code", "item_name", "item_group", "stock_uom", "disabled"],
    )
    return {row.item_code: row for row in rows}


def _price_context(customer=None):
    from biozone_web.utils import get_default_company, get_storefront_price_context

    ctx = get_storefront_price_context(customer)
    company = get_default_company()
    currency = frappe.db.get_value("Company", company, "default_currency") or "EGP"
    return {
        "customer": ctx["customer"],
        "customer_group": ctx["customer_group"],
        "is_active": ctx["is_active"],
        "company": company,
        "currency": currency,
        "price_list": PRICE_LIST,
    }


def _min_limit(customer_group):
    from biozone_web.utils import MinOrderConfigError, get_group_min_order_amount

    try:
        return get_group_min_order_amount(customer_group)
    except MinOrderConfigError:
        return None


def _resolve_line(code, qty, uom, meta, display, prices, ctx, thumbnails=None):
    from biozone_web.units import resolve_item_uom_factor

    issues = []
    meta_row = meta.get(code)
    if not meta_row:
        issues.append({"code": "ITEM_NOT_FOUND", "message": _("الصنف غير موجود")})
    elif frappe.utils.cint(meta_row.disabled):
        issues.append({"code": "ITEM_DISABLED", "message": _("الصنف غير متاح حاليًا")})

    d = display.get(code) or {}
    factor = None
    line_uom = (uom or "").strip()
    if not d.get("ok") or not d.get("displayable", True):
        issues.append({"code": "UOM_UNAVAILABLE", "message": _("الصنف غير متاح بوحدة عرض صالحة")})
    else:
        display_uom = (d.get("uom") or "").strip()
        if not line_uom:
            line_uom = display_uom
            factor = d.get("factor") or 1.0
        elif line_uom == display_uom:
            factor = d.get("factor") or 1.0
        else:
            factor = resolve_item_uom_factor(code, line_uom)
            if factor is None:
                issues.append({"code": "UOM_CHANGED", "message": _("وحدة الصنف تغيّرت — حدّث السلة")})

    price_row = prices.get(code) or {}
    small_price = price_row.get("price")
    base_price = price_row.get("base_price")
    if small_price is None:
        issues.append({"code": "NO_PRICE", "message": _("لا يوجد سعر متاح لهذا الصنف")})

    unit_price = None
    line_total = None
    if small_price is not None and factor is not None:
        unit_price_raw = frappe.utils.flt(small_price) * factor
        unit_price = _money(unit_price_raw)
        line_total = _money(unit_price * qty)

    thumb = (thumbnails or {}).get(code) or {}

    return {
        "item_code": code,
        "item_name": (getattr(meta_row, "item_name", None) or code),
        "item_group": (getattr(meta_row, "item_group", None) or ""),
        "qty": qty,
        "uom": line_uom,
        "factor": factor,
        "price": unit_price,
        "base_price": _money(frappe.utils.flt(base_price)) if base_price is not None else None,
        "discount_percentage": frappe.utils.flt(price_row.get("discount_percentage")),
        "line_total": line_total,
        "thumbnail": thumb.get("thumbnail") or "",
        "available": not issues,
        "issues": issues,
    }


def _empty_quote(ctx):
    quote = {
        "valid": True,
        "issues": [],
        "items": [],
        "net_total": 0.0,
        "total_units": 0.0,
        "item_count": 0,
        "customer": ctx.get("customer"),
        "customer_group": ctx.get("customer_group"),
        "company": ctx.get("company"),
        "currency": ctx.get("currency"),
        "price_list": ctx.get("price_list") or PRICE_LIST,
        "min_limit": _min_limit(ctx.get("customer_group")),
    }
    quote["min_applies"] = bool(ctx.get("is_active") and quote["min_limit"])
    quote["fingerprint"] = _quote_fingerprint(quote)
    return quote


def build_quote(cart, customer=None):
    ctx = _price_context(customer)
    if cart is None:
        return _empty_quote(ctx)

    from biozone_web.units import resolve_items_display
    from biozone_web.utils import get_effective_item_prices

    rows = list(cart.items or [])
    codes = list(dict.fromkeys((row.item_code or "").strip() for row in rows if row.item_code))
    meta = _item_meta(codes)
    prices = get_effective_item_prices(
        codes,
        customer=ctx["customer"],
        customer_group=ctx["customer_group"],
    )
    display = resolve_items_display(codes, ctx["customer_group"]) if codes else {}
    from biozone_web.services.item_images import resolve_item_thumbnails

    thumbnails = resolve_item_thumbnails(codes) if codes else {}

    items = []
    issues = []
    net_total = 0.0
    total_units = 0.0
    for row in rows:
        code = (row.item_code or "").strip()
        try:
            qty = _validate_qty(row.qty)
        except CartError:
            qty = 0.0
            issue = {"code": "ITEM_DATA_INVALID", "message": _("بيانات صنف غير صحيحة في السلة")}
            items.append(
                {
                    "item_code": code,
                    "item_name": code,
                    "item_group": "",
                    "qty": 0.0,
                    "uom": (row.uom or "").strip(),
                    "price": None,
                    "line_total": None,
                    "thumbnail": "",
                    "available": False,
                    "issues": [issue],
                }
            )
            issues.append({"item_code": code, **issue})
            continue
        line = _resolve_line(
            code, qty, row.uom, meta, display, prices, ctx, thumbnails
        )
        items.append(line)
        for issue in line["issues"]:
            issues.append({"item_code": code, **issue})
        if line["available"]:
            net_total += line["line_total"] or 0
            total_units += line["qty"]

    quote = {
        "valid": not issues,
        "issues": issues,
        "items": items,
        "net_total": _money(net_total),
        "total_units": round(total_units, 3),
        "item_count": len(items),
        "customer": ctx["customer"],
        "customer_group": ctx["customer_group"],
        "company": ctx["company"],
        "currency": ctx["currency"],
        "price_list": ctx["price_list"],
        "min_limit": _min_limit(ctx["customer_group"]),
    }
    quote["min_applies"] = bool(ctx["is_active"] and quote["min_limit"])
    quote["fingerprint"] = _quote_fingerprint(quote)
    return quote


def _quote_fingerprint(quote):
    payload = {
        "v": 1,
        "customer": quote.get("customer"),
        "customer_group": quote.get("customer_group"),
        "company": quote.get("company"),
        "currency": quote.get("currency"),
        "price_list": quote.get("price_list"),
        "min_limit": quote.get("min_limit"),
        "min_applies": quote.get("min_applies"),
        "valid": quote.get("valid"),
        "net_total": quote.get("net_total"),
        "items": [
            {
                "item_code": item.get("item_code"),
                "qty": item.get("qty"),
                "uom": item.get("uom"),
                "factor": item.get("factor"),
                "price": item.get("price"),
                "line_total": item.get("line_total"),
                "available": item.get("available"),
                "issues": [issue.get("code") for issue in item.get("issues") or []],
            }
            for item in quote.get("items") or []
        ],
    }
    return _fingerprint(payload)


def cart_state(cart, customer=None):
    quote = build_quote(cart, customer=customer)
    if cart is None:
        cart_info = {
            "name": None,
            "status": "Empty",
            "version": 0,
            "customer": quote.get("customer"),
            "last_activity_on": None,
            "expires_on": None,
            "converted_on": None,
            "converted_sales_order": None,
            "items": [],
        }
    else:
        cart_info = {
            "name": cart.name,
            "status": cart.status,
            "version": int(cart.version or 0),
            "customer": cart.customer,
            "last_activity_on": str(cart.last_activity_on) if cart.last_activity_on else None,
            "expires_on": str(cart.expires_on) if cart.expires_on else None,
            "converted_on": str(cart.converted_on) if cart.converted_on else None,
            "converted_sales_order": cart.converted_sales_order,
            "items": quote["items"],
        }
    cart_info["item_count"] = quote["item_count"]
    cart_info["total_units"] = quote["total_units"]
    cart_info["net_total"] = quote["net_total"]
    return {
        "cart": cart_info,
        "quote": quote,
        "quote_fingerprint": quote["fingerprint"],
    }


def _conflict_current(cart, customer, message):
    state = cart_state(cart, customer=customer)
    return CartError(
        "CART_VERSION_CONFLICT",
        message,
        status=409,
        extra={
            "cart": state["cart"],
            "quote": state["quote"],
            "quote_fingerprint": state["quote_fingerprint"],
        },
    )


def _check_version(cart, expected, customer):
    version = _validate_cart_version(expected)
    current = int(cart.version or 0) if cart else 0
    if version != current:
        raise _conflict_current(
            cart,
            customer,
            _("تغيّرت السلة من جهاز آخر — حدّثها ثم أعد المحاولة"),
        )
    return version


def _create_cart(user, customer, items):
    now = _now()
    doc = frappe.get_doc(
        {
            "doctype": "BZ Cart",
            "user": user,
            "customer": customer,
            "status": STATUS_ACTIVE,
            "version": 1,
            "active_key": user,
            "last_activity_on": now,
            "expires_on": _expiry(now),
            "items": items,
        }
    )
    try:
        doc.insert(ignore_permissions=True)
        return doc
    except DUPLICATE_ERRORS:
        frappe.db.rollback()
        existing = get_active_cart(user, for_update=True)
        if existing:
            raise _conflict_current(
                existing,
                customer,
                _("تغيّرت السلة من جهاز آخر — حدّثها ثم أعد المحاولة"),
            )
        raise


def _touch(cart, customer):
    now = _now()
    cart.customer = customer
    cart.last_activity_on = now
    cart.expires_on = _expiry(now)
    cart.version = int(cart.version or 0) + 1
    cart.save(ignore_permissions=True)


def _find_line(cart, item_code):
    for row in cart.items or []:
        if (row.item_code or "").strip() == item_code:
            return row
    return None


def _validate_new_item(item_code, uom, customer):
    ctx = _price_context(customer)
    code = _require_text(item_code, "item_code", max_length=140)
    meta = _item_meta([code])
    if code not in meta:
        raise CartError(
            "ITEM_UNAVAILABLE",
            _("أحد الأصناف في السلة لم يعد متاحًا، يرجى تحديث السلة"),
            item_code=code,
        )
    if frappe.utils.cint(meta[code].disabled):
        raise CartError(
            "ITEM_UNAVAILABLE",
            _("أحد الأصناف في السلة لم يعد متاحًا، يرجى تحديث السلة"),
            item_code=code,
        )
    from biozone_web.units import resolve_items_display

    display = resolve_items_display([code], ctx["customer_group"])
    d = display.get(code) or {}
    if not d.get("ok") or not d.get("displayable", True):
        raise CartError(
            "UOM_UNAVAILABLE",
            _("الصنف {0} غير متاح بوحدة عرض صالحة حاليًا — حدّث السلة").format(code),
            item_code=code,
        )
    line_uom = _optional_text(uom, "uom", max_length=MAX_UOM_LENGTH) or (
        d.get("uom") or ""
    ).strip()
    if not line_uom or len(line_uom) > MAX_UOM_LENGTH:
        raise CartError("UOM_UNAVAILABLE", _("وحدة الصنف غير صالحة"), item_code=code)
    if line_uom != (d.get("uom") or "").strip():
        from biozone_web.units import resolve_item_uom_factor

        if resolve_item_uom_factor(code, line_uom) is None:
            raise CartError(
                "UOM_CHANGED",
                _("الوحدة المطلوبة للصنف {0} لم تعد صالحة — حدّث السلة").format(code),
                item_code=code,
            )
    from biozone_web.utils import get_effective_item_prices

    price = get_effective_item_prices(
        [code],
        customer=customer,
        customer_group=ctx["customer_group"],
    ).get(code, {})
    if price.get("price") is None:
        raise CartError(
            "ITEM_UNAVAILABLE",
            _("أحد الأصناف في السلة لم يعد متاحًا، يرجى تحديث السلة"),
            item_code=code,
        )
    return code, line_uom, ctx


def get_cart(include_undo=False):
    customer = require_customer()
    cart = get_active_cart()
    state = cart_state(cart, customer=customer)
    if include_undo and snapshots_enabled() and cart:
        snapshot = _latest_snapshot(frappe.session.user, cart.name)
        if snapshot:
            state["undo"] = _snapshot_payload(snapshot)
    return state


def add_item(item_code, qty, uom=None, cart_version=None):
    customer = require_customer()
    user = frappe.session.user
    code, line_uom, ctx = _validate_new_item(item_code, uom, customer)
    line_qty = _validate_qty(qty)
    expected = _validate_cart_version(cart_version)

    cart = get_active_cart(user, for_update=True)
    if cart is None:
        if expected != 0:
            raise _conflict_current(None, customer, _("لا توجد سلة نشطة — حدّث السلة"))
        now = _now()
        doc = _create_cart(
            user,
            customer,
            [
                {
                    "item_code": code,
                    "qty": line_qty,
                    "uom": line_uom,
                    "added_on": now,
                    "updated_on": now,
                }
            ],
        )
        return cart_state(doc, customer=customer)

    _check_version(cart, expected, customer)
    line = _find_line(cart, code)
    now = _now()
    if line:
        if (line.uom or "").strip() != line_uom:
            state = cart_state(cart, customer=customer)
            raise CartError(
                "CART_UOM_CONFLICT",
                _("الصنف موجود في السلة بوحدة مختلفة — أزله ثم أضفه بالوحدة الجديدة"),
                status=409,
                item_code=code,
                extra={"cart": state["cart"], "quote_fingerprint": state["quote_fingerprint"]},
            )
        new_qty = round(frappe.utils.flt(line.qty) + line_qty, 3)
        if new_qty > MAX_LINE_QTY:
            raise CartError("QTY_LIMIT", _("الكمية أكبر من الحد المسموح"), item_code=code)
        line.qty = new_qty
        line.updated_on = now
    else:
        if len(cart.items or []) >= MAX_CART_LINES:
            raise CartError("CART_LINE_LIMIT", _("عدد أصناف السلة أكبر من الحد المسموح"))
        cart.append(
            "items",
            {
                "item_code": code,
                "qty": line_qty,
                "uom": line_uom,
                "added_on": now,
                "updated_on": now,
            },
        )
    _touch(cart, customer)
    return cart_state(cart, customer=customer)


def set_qty(item_code, qty, cart_version=None):
    customer = require_customer()
    user = frappe.session.user
    code = _require_text(item_code, "item_code", max_length=140)
    line_qty = _validate_qty(qty)
    cart = get_active_cart(user, for_update=True)
    if cart is None:
        raise CartError("CART_EMPTY", _("السلة فارغة"), status=422)
    _check_version(cart, cart_version, customer)
    line = _find_line(cart, code)
    if not line:
        raise CartError("ITEM_NOT_IN_CART", _("الصنف غير موجود في السلة"), item_code=code)
    if round(frappe.utils.flt(line.qty), 3) == line_qty:
        return cart_state(cart, customer=customer)
    line.qty = line_qty
    line.updated_on = _now()
    _touch(cart, customer)
    return cart_state(cart, customer=customer)


def remove_item(item_code, cart_version=None):
    customer = require_customer()
    user = frappe.session.user
    code = _require_text(item_code, "item_code", max_length=140)
    expected = _validate_cart_version(cart_version)
    cart = get_active_cart(user, for_update=True)
    if cart is None:
        raise CartError("CART_EMPTY", _("السلة فارغة"), status=422)
    snapshot = None
    if snapshots_enabled():
        snapshot = _get_snapshot_by_key(
            _snapshot_key(user, cart.name, expected, "Remove", code)
        )
        if snapshot:
            return _with_undo(cart_state(cart, customer=customer), snapshot)
    _check_version(cart, expected, customer)
    line = _find_line(cart, code)
    if not line:
        raise CartError("ITEM_NOT_IN_CART", _("الصنف غير موجود في السلة"), item_code=code)
    if snapshots_enabled():
        snapshot = _insert_snapshot(
            cart, customer, "Remove", "Line", expected, item_code=code
        )
    cart.remove(line)
    _touch(cart, customer)
    return _with_undo(cart_state(cart, customer=customer), snapshot)


def clear_cart(cart_version=None):
    customer = require_customer()
    user = frappe.session.user
    cart = get_active_cart(user, for_update=True)
    if cart is None:
        return cart_state(None, customer=customer)
    expected = _validate_cart_version(cart_version)
    snapshot = None
    if snapshots_enabled():
        snapshot = _get_snapshot_by_key(
            _snapshot_key(user, cart.name, expected, "Clear")
        )
        if snapshot:
            return _with_undo(cart_state(cart, customer=customer), snapshot)
    _check_version(cart, expected, customer)
    if not cart.items:
        return cart_state(cart, customer=customer)
    if snapshots_enabled():
        snapshot = _insert_snapshot(cart, customer, "Clear", "Cart", expected)
    cart.items = []
    _touch(cart, customer)
    return _with_undo(cart_state(cart, customer=customer), snapshot)


def _normalize_source_items(items, customer, *, validate_price=True):
    if isinstance(items, str):
        items = _parse_json(items, [])
    if not isinstance(items, list) or not items:
        raise CartError("CART_EMPTY", _("سلة المصدر فارغة"), status=422)
    if len(items) > MAX_CART_LINES:
        raise CartError("CART_LINE_LIMIT", _("عدد أصناف السلة أكبر من الحد المسموح"))
    ctx = _price_context(customer)
    from biozone_web.units import resolve_items_display
    from biozone_web.utils import get_effective_item_prices

    raw = []
    for entry in items:
        if not isinstance(entry, dict):
            raise CartError("ITEM_DATA_INVALID", _("بيانات صنف غير صحيحة في السلة"))
        code = _require_text(entry.get("item_code"), "item_code", max_length=140)
        qty = _validate_qty(entry.get("qty"))
        uom = _optional_text(entry.get("uom"), "uom", max_length=MAX_UOM_LENGTH)
        raw.append({"item_code": code, "qty": qty, "uom": uom})

    codes = list(dict.fromkeys(row["item_code"] for row in raw))
    meta = _item_meta(codes)
    display = resolve_items_display(codes, ctx["customer_group"])
    from biozone_web.services.item_images import resolve_item_thumbnails

    thumbnails = resolve_item_thumbnails(codes)
    prices = get_effective_item_prices(
        codes,
        customer=ctx["customer"],
        customer_group=ctx["customer_group"],
    )
    normalized = []
    conflicts = []
    by_code = {}
    for row in raw:
        line = _resolve_line(
            row["item_code"],
            row["qty"],
            row["uom"],
            meta,
            display,
            prices,
            ctx,
            thumbnails,
        )
        if not line["available"]:
            for issue in line["issues"]:
                conflicts.append(
                    {
                        "item_code": row["item_code"],
                        "code": issue["code"],
                        "message": issue["message"],
                    }
                )
        if not validate_price and any(i["code"] == "NO_PRICE" for i in line["issues"]):
            line["issues"] = [i for i in line["issues"] if i["code"] != "NO_PRICE"]
            line["available"] = not line["issues"]
        existing = by_code.get(row["item_code"])
        if existing:
            if (existing["uom"] or "") != (line["uom"] or ""):
                conflicts.append(
                    {
                        "item_code": row["item_code"],
                        "code": "SOURCE_UOM_CONFLICT",
                        "message": _("الصنف مكرر بوحدتين مختلفتين في سلة المصدر"),
                    }
                )
                continue
            existing["qty"] = round(existing["qty"] + line["qty"], 3)
            if existing["qty"] > MAX_LINE_QTY:
                conflicts.append(
                    {
                        "item_code": row["item_code"],
                        "code": "QTY_LIMIT",
                        "message": _("الكمية أكبر من الحد المسموح"),
                    }
                )
            continue
        by_code[row["item_code"]] = line
        normalized.append(line)

    unique_conflicts = []
    seen = set()
    for conflict in conflicts:
        key = (conflict.get("item_code"), conflict.get("code"))
        if key in seen:
            continue
        seen.add(key)
        unique_conflicts.append(conflict)
    return normalized, unique_conflicts, ctx


def _source_fingerprint(lines):
    payload = {
        "items": [
            {
                "item_code": line.get("item_code"),
                "qty": line.get("qty"),
                "uom": line.get("uom"),
            }
            for line in lines
        ]
    }
    return _fingerprint(payload)


def _get_merge_by_source(source_uid, *, for_update=False):
    if for_update:
        rows = frappe.db.sql(
            "select name from `tabBZ Cart Merge` where source_uid = %s for update",
            (source_uid,),
            as_dict=True,
        )
        if not rows:
            return None
        return frappe.get_doc("BZ Cart Merge", rows[0].name)
    name = frappe.db.get_value("BZ Cart Merge", {"source_uid": source_uid}, "name")
    return frappe.get_doc("BZ Cart Merge", name) if name else None


def _get_merge_by_token(token, *, for_update=False):
    if for_update:
        rows = frappe.db.sql(
            "select name from `tabBZ Cart Merge` where merge_token = %s for update",
            (token,),
            as_dict=True,
        )
        if not rows:
            return None
        return frappe.get_doc("BZ Cart Merge", rows[0].name)
    name = frappe.db.get_value("BZ Cart Merge", {"merge_token": token}, "name")
    return frappe.get_doc("BZ Cart Merge", name) if name else None


def _merge_preview_payload(normalized, conflicts, target, auto_apply):
    target_items = []
    if target:
        for row in target.items or []:
            target_items.append(
                {
                    "item_code": row.item_code,
                    "qty": frappe.utils.flt(row.qty),
                    "uom": row.uom,
                }
            )
    target_map = {row["item_code"]: row for row in target_items}
    merged = [dict(row) for row in target_items]
    merged_map = {row["item_code"]: row for row in merged}
    for line in normalized:
        existing = target_map.get(line["item_code"])
        if existing:
            if (existing.get("uom") or "") != (line.get("uom") or ""):
                conflicts.append(
                    {
                        "item_code": line["item_code"],
                        "code": "TARGET_UOM_CONFLICT",
                        "message": _("الصنف موجود في سلة الحساب بوحدة مختلفة"),
                    }
                )
                continue
            new_qty = round(existing["qty"] + line["qty"], 3)
            if new_qty > MAX_LINE_QTY:
                conflicts.append(
                    {
                        "item_code": line["item_code"],
                        "code": "QTY_LIMIT",
                        "message": _("الكمية أكبر من الحد المسموح"),
                    }
                )
                continue
            merged_map[line["item_code"]]["qty"] = new_qty
        else:
            merged.append(
                {
                    "item_code": line["item_code"],
                    "qty": line["qty"],
                    "uom": line["uom"],
                }
            )
            merged_map[line["item_code"]] = merged[-1]
    unique_conflicts = []
    seen = set()
    for conflict in conflicts:
        key = (conflict.get("item_code"), conflict.get("code"))
        if key in seen:
            continue
        seen.add(key)
        unique_conflicts.append(conflict)
    can_apply = bool(normalized) and not unique_conflicts
    if not can_apply:
        auto_apply = False
    return {
        "target_items": target_items,
        "source_items": [
            {
                "item_code": line["item_code"],
                "item_name": line.get("item_name"),
                "qty": line["qty"],
                "uom": line["uom"],
                "available": line.get("available", True),
                "issues": line.get("issues") or [],
            }
            for line in normalized
        ],
        "merged_items": merged,
        "conflicts": unique_conflicts,
        "can_apply": can_apply,
        "auto_apply": auto_apply,
    }


def transfer_preview(
    source_uid,
    source_kind,
    source_key=None,
    items=None,
    target_version=None,
):
    customer = require_customer()
    user = frappe.session.user
    uid = _validate_source_uid(source_uid)
    kind = _require_text(source_kind, "source_kind", max_length=32)
    if kind not in ("Guest", "Local Account"):
        raise CartError("MERGE_SOURCE_INVALID", _("نوع مصدر السلة غير صالح"), status=422)
    key = _require_text(source_key, "source_key", max_length=MAX_SOURCE_KEY_LENGTH, required=False)
    target = get_active_cart(user)
    current_version = int(target.version or 0) if target else 0
    if target_version is not None:
        expected = _validate_cart_version(target_version)
        if expected != current_version:
            raise _conflict_current(
                target,
                customer,
                _("تغيّرت السلة من جهاز آخر — حدّثها ثم أعد المحاولة"),
            )

    normalized, conflicts, ctx = _normalize_source_items(items, customer)
    fingerprint = _source_fingerprint(normalized)
    target_items = list(target.items or []) if target else []
    auto_apply = kind == "Local Account" and not target_items
    preview = _merge_preview_payload(normalized, conflicts, target, auto_apply)
    now = _now()
    token = uuid.uuid4().hex
    merge = _get_merge_by_source(uid, for_update=True)
    if merge:
        if merge.user != user:
            raise CartError("MERGE_SOURCE_OWNED", _("سلة المصدر مرتبطة بحساب آخر"), status=409)
        if merge.status == "Applied":
            if (merge.source_fingerprint or "") != fingerprint:
                raise CartError("MERGE_SOURCE_CONFLICT", _("تغيّر محتوى سلة المصدر"), status=409)
            return {
                "ok": True,
                "status": "Applied",
                "already_applied": True,
                "merge_token": merge.merge_token,
                "source_fingerprint": merge.source_fingerprint,
                "result": _parse_json(merge.result_snapshot, {}),
            }
        merge.merge_token = token
        merge.customer = customer
        merge.cart = target.name if target else None
        merge.status = "Previewed" if preview["can_apply"] else "Conflict"
        merge.source_kind = kind
        merge.source_key = key
        merge.source_fingerprint = fingerprint
        merge.target_version_before = current_version
        merge.result_version = 0
        merge.request_id = get_request_id()
        merge.applied_on = None
        merge.source_items = _json(
            [
                {"item_code": line["item_code"], "qty": line["qty"], "uom": line["uom"]}
                for line in normalized
            ]
        )
        merge.preview = _json(preview)
        merge.conflicts = _json(preview["conflicts"])
        merge.result_snapshot = ""
        merge.save(ignore_permissions=True)
    else:
        merge = frappe.get_doc(
            {
                "doctype": "BZ Cart Merge",
                "user": user,
                "customer": customer,
                "cart": target.name if target else None,
                "status": "Previewed" if preview["can_apply"] else "Conflict",
                "merge_token": token,
                "source_uid": uid,
                "source_kind": kind,
                "source_key": key,
                "source_fingerprint": fingerprint,
                "target_version_before": current_version,
                "request_id": get_request_id(),
                "source_items": _json(
                    [
                        {"item_code": line["item_code"], "qty": line["qty"], "uom": line["uom"]}
                        for line in normalized
                    ]
                ),
                "preview": _json(preview),
                "conflicts": _json(preview["conflicts"]),
            }
        )
        try:
            merge.insert(ignore_permissions=True)
        except DUPLICATE_ERRORS:
            frappe.db.rollback()
            raise CartError(
                "MERGE_IN_PROGRESS",
                _("جلسة الدمج قيد الإنشاء — أعد المحاولة"),
                status=409,
            )

    state = cart_state(target, customer=customer)
    return {
        "ok": True,
        "status": "Conflict" if preview["conflicts"] else "Previewed",
        "already_applied": False,
        "merge_token": token,
        "source_fingerprint": fingerprint,
        "target_version": current_version,
        "target": state["cart"],
        "target_quote": state["quote"],
        "target_quote_fingerprint": state["quote_fingerprint"],
        "source": preview["source_items"],
        "merged": preview["merged_items"],
        "conflicts": preview["conflicts"],
        "can_apply": preview["can_apply"],
        "auto_apply": preview["auto_apply"],
    }


def transfer_apply(merge_token, *, source_fingerprint=None, cart_version=None):
    customer = require_customer()
    user = frappe.session.user
    token = _validate_token(merge_token)
    source_fp = _validate_fingerprint(source_fingerprint) if source_fingerprint else None
    merge = _get_merge_by_token(token, for_update=True)
    if not merge or merge.user != user:
        raise CartError("MERGE_NOT_FOUND", _("جلسة الدمج غير موجودة — أعد المعاينة"), status=409)
    if merge.status == "Applied":
        if source_fp and source_fp != (merge.source_fingerprint or "").lower():
            raise CartError("MERGE_SOURCE_CONFLICT", _("تغيّر محتوى سلة المصدر"), status=409)
        return {
            "ok": True,
            "status": "Applied",
            "already_applied": True,
            "merge_token": merge.merge_token,
            "result": _parse_json(merge.result_snapshot, {}),
        }
    if merge.status == "Expired":
        raise CartError("MERGE_EXPIRED", _("انتهت جلسة الدمج — أعد المعاينة"), status=409)
    if source_fp and source_fp != (merge.source_fingerprint or "").lower():
        raise CartError("MERGE_SOURCE_CHANGED", _("تغيّر محتوى سلة المصدر — أعد المعاينة"), status=409)

    cart = get_active_cart(user, for_update=True)
    if (merge.cart or None) != (cart.name if cart else None):
        raise _conflict_current(
            cart,
            customer,
            _("تغيّرت السلة من جهاز آخر — أعد المعاينة"),
        )
    current_version = int(cart.version or 0) if cart else 0
    if cart_version is not None:
        expected = _validate_cart_version(cart_version)
        if expected != current_version:
            raise _conflict_current(cart, customer, _("تغيّرت السلة من جهاز آخر — أعد المعاينة"))
    if current_version != int(merge.target_version_before or 0):
        raise _conflict_current(cart, customer, _("تغيّرت السلة من جهاز آخر — أعد المعاينة"))

    source_items = _parse_json(merge.source_items, [])
    normalized, conflicts, ctx = _normalize_source_items(source_items, customer)
    if conflicts:
        raise CartError(
            "MERGE_CONFLICT",
            _("تعذّر دمج السلة لوجود تعارض — راجع السلة وأعد المحاولة"),
            status=409,
            extra={"conflicts": conflicts},
        )

    now = _now()
    if cart is None:
        rows = [
            {
                "item_code": line["item_code"],
                "qty": line["qty"],
                "uom": line["uom"],
                "added_on": now,
                "updated_on": now,
            }
            for line in normalized
        ]
        cart = _create_cart(user, customer, rows)
    else:
        cart, merge_conflicts, _applied = _merge_items_into_cart(
            cart, normalized, customer
        )
        if merge_conflicts:
            first = merge_conflicts[0]
            code = first.get("code")
            if code == "TARGET_UOM_CONFLICT":
                raise CartError(
                    "MERGE_CONFLICT",
                    _("تعذّر دمج السلة لوجود تعارض في الوحدة"),
                    status=409,
                    item_code=first.get("item_code"),
                )
            if code == "QTY_LIMIT":
                raise CartError(
                    "MERGE_CONFLICT",
                    _("تعذّر دمج السلة: الكمية أكبر من الحد المسموح"),
                    status=409,
                    item_code=first.get("item_code"),
                )
            raise CartError(
                "MERGE_CONFLICT",
                _("تعذّر دمج السلة: عدد الأصناف أكبر من الحد المسموح"),
                status=409,
            )
        _touch(cart, customer)

    state = cart_state(cart, customer=customer)
    merge.status = "Applied"
    merge.cart = cart.name
    merge.result_version = int(cart.version or 0)
    merge.applied_on = now
    merge.result_snapshot = _json(state)
    merge.save(ignore_permissions=True)
    return {
        "ok": True,
        "status": "Applied",
        "already_applied": False,
        "merge_token": merge.merge_token,
        "result": state,
    }


def get_checkout_receipt(token, *, for_update=False):
    if for_update:
        rows = frappe.db.sql(
            "select name from `tabBZ Checkout Receipt` where checkout_token = %s for update",
            (token,),
            as_dict=True,
        )
        if not rows:
            return None
        return frappe.get_doc("BZ Checkout Receipt", rows[0].name)
    name = frappe.db.get_value("BZ Checkout Receipt", {"checkout_token": token}, "name")
    return frappe.get_doc("BZ Checkout Receipt", name) if name else None


def checkout_context(checkout_token, cart_version, quote_fingerprint):
    customer = require_customer()
    user = frappe.session.user
    token = _validate_token(checkout_token)
    expected_version = _validate_cart_version(cart_version)
    fingerprint = _validate_fingerprint(quote_fingerprint)

    receipt = get_checkout_receipt(token)
    if receipt:
        if receipt.user != user:
            raise CartError("CHECKOUT_TOKEN_CONFLICT", _("معرّف التأكيد مستخدم بالفعل"), status=409)
        if receipt.status == "Success":
            return {
                "already_completed": True,
                "receipt": receipt,
                "cart": None,
                "state": None,
                "customer": customer,
            }
        if receipt.status == "Processing":
            raise CartError(
                "CHECKOUT_IN_PROGRESS",
                _("طلبك قيد المعالجة — انتظر قليلًا ثم حدّث الصفحة"),
                status=409,
            )

    cart = get_active_cart(user, for_update=True)
    if cart is None or not cart.items:
        raise CartError("CART_EMPTY", _("السلة فارغة"), status=422)
    if int(cart.version or 0) != expected_version:
        raise _conflict_current(
            cart,
            customer,
            _("تغيّرت السلة من جهاز آخر — حدّثها ثم أعد المحاولة"),
        )
    state = cart_state(cart, customer=customer)
    if state["quote_fingerprint"] != fingerprint:
        raise CartError(
            "PRICE_CHANGED",
            _("تغيّر سعر أو توفر أحد الأصناف — راجع السلة ثم أعد التأكيد"),
            status=409,
            extra={
                "cart": state["cart"],
                "quote": state["quote"],
                "quote_fingerprint": state["quote_fingerprint"],
            },
        )
    if not state["quote"]["valid"]:
        first = (state["quote"]["issues"] or [{}])[0]
        code = first.get("item_code")
        issue_code = first.get("code") or "ITEM_UNAVAILABLE"
        message = first.get("message") or _("أحد الأصناف في السلة لم يعد متاحًا، يرجى تحديث السلة")
        raise CartError(issue_code, message, status=422, item_code=code, extra={"quote": state["quote"]})

    if not receipt:
        receipt = frappe.get_doc(
            {
                "doctype": "BZ Checkout Receipt",
                "checkout_token": token,
                "user": user,
                "cart": cart.name,
                "status": "Processing",
                "cart_version": expected_version,
                "quote_fingerprint": fingerprint,
                "request_id": get_request_id(),
                "quote_snapshot": _json(state["quote"]),
            }
        )
        try:
            receipt.insert(ignore_permissions=True)
        except DUPLICATE_ERRORS:
            frappe.db.rollback()
            existing = get_checkout_receipt(token, for_update=True)
            if existing and existing.user == user and existing.status == "Success":
                return {
                    "already_completed": True,
                    "receipt": existing,
                    "cart": None,
                    "state": None,
                    "customer": customer,
                }
            raise CartError(
                "CHECKOUT_IN_PROGRESS",
                _("طلبك قيد المعالجة — انتظر قليلًا ثم حدّث الصفحة"),
                status=409,
            )

    return {
        "already_completed": False,
        "receipt": receipt,
        "cart": cart,
        "state": state,
        "customer": customer,
    }


def mark_checkout_success(receipt, cart, sales_order, response):
    now = _now()
    version = int(cart.version or 0) + 1
    frappe.db.set_value(
        "BZ Cart",
        cart.name,
        {
            "status": STATUS_CONVERTED,
            "active_key": None,
            "converted_sales_order": sales_order,
            "converted_on": now,
            "last_activity_on": now,
            "version": version,
        },
        update_modified=True,
    )
    receipt.status = "Success"
    receipt.sales_order = sales_order
    receipt.completed_on = now
    receipt.response_snapshot = _json(response)
    receipt.save(ignore_permissions=True)
    return version


def checkout_status(checkout_token):
    user = frappe.session.user
    token = _validate_token(checkout_token)
    receipt = get_checkout_receipt(token)
    if not receipt or receipt.user != user:
        return {"ok": True, "status": "NotFound"}
    response = _parse_json(receipt.response_snapshot, {})
    return {
        "ok": True,
        "status": receipt.status,
        "sales_order": receipt.sales_order,
        "redirect": response.get("redirect") if receipt.status == "Success" else None,
        "request_id": receipt.request_id,
        "quote_fingerprint": receipt.quote_fingerprint,
        "cart_version": receipt.cart_version,
    }
