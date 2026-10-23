import csv
import io

import frappe
from frappe import _

from biozone_web.request_id import get_request_id
from biozone_web.services.cart import cart_state
from biozone_web.utils import require_staff_access

VIEWER_ROLE = "BZ Cart Viewer"
ACCESS_LOG_DOCTYPE = "BZ Cart Access Log"
STATUSES = ("Active", "Converted", "Expired")
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_SEARCH = 100


def require_cart_viewer():
    require_staff_access()
    user = frappe.session.user
    if user == "Administrator":
        return user
    roles = set(frappe.get_roles(user))
    if "System Manager" in roles or VIEWER_ROLE in roles:
        return user
    frappe.throw(
        _("غير مصرح بعرض سلال العملاء"),
        frappe.PermissionError,
    )


def list_carts(*, status="Active", q="", page=1, page_size=DEFAULT_PAGE_SIZE):
    require_cart_viewer()
    status = status if status in STATUSES + ("All",) else "Active"
    if status == "All":
        status = None
    q = (q or "").strip()[:MAX_SEARCH]
    try:
        page = max(int(page or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(max(int(page_size or DEFAULT_PAGE_SIZE), 1), MAX_PAGE_SIZE)
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE

    params = {}
    search_where = ""
    if q:
        search_where = """
            and (
                c.name like %(q)s
                or c.user like %(q)s
                or c.customer like %(q)s
                or cu.customer_name like %(q)s
            )
        """
        params["q"] = f"%{q}%"
    where = f"where c.status = %(status)s {search_where}" if status else f"where 1=1 {search_where}"
    if status:
        params["status"] = status

    base_from = """
        from `tabBZ Cart` c
        left join `tabCustomer` cu on cu.name = c.customer
    """

    total = frappe.db.sql(
        f"select count(*) {base_from} {where}",
        params,
    )[0][0]

    count_params = {key: value for key, value in params.items() if key != "status"}
    count_rows = frappe.db.sql(
        f"select c.status as status, count(*) as total {base_from} where 1=1 {search_where} group by c.status",
        count_params,
    )
    counts = {row[0]: int(row[1]) for row in count_rows}
    counts["All"] = sum(counts.values())

    rows = frappe.db.sql(
        f"""
        select
            c.name,
            c.user,
            c.customer,
            coalesce(cu.customer_name, c.customer) as customer_name,
            c.status,
            c.version,
            c.last_activity_on,
            c.expires_on,
            c.converted_on,
            c.converted_sales_order,
            (
                select count(*)
                from `tabBZ Cart Item` i
                where i.parent = c.name
            ) as item_count,
            (
                select coalesce(sum(i.qty), 0)
                from `tabBZ Cart Item` i
                where i.parent = c.name
            ) as total_units,
            (
                select min(pu.user)
                from `tabPortal User` pu
                where pu.parenttype = 'Customer' and pu.parent = c.customer
            ) as customer_email
        {base_from}
        {where}
        order by coalesce(c.last_activity_on, c.modified) desc, c.modified desc
        limit %(limit)s offset %(offset)s
        """,
        {**params, "limit": page_size, "offset": (page - 1) * page_size},
        as_dict=True,
    )

    for row in rows:
        row["item_count"] = int(row.get("item_count") or 0)
        row["total_units"] = float(row.get("total_units") or 0)

    return {
        "carts": rows,
        "counts": counts,
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "pages": max((int(total) + page_size - 1) // page_size, 1),
        "status": status,
        "q": q,
    }


def get_cart_detail(name):
    require_cart_viewer()
    name = _required_cart_name(name)
    cart = frappe.get_doc("BZ Cart", name)
    state = cart_state(cart, customer=cart.customer or None)
    customer = _customer_info(cart.customer)
    state["customer"] = customer
    _log_access(cart, "View")
    state["access_log"] = _access_log(cart.name)
    return state


def recheck_cart(name):
    require_cart_viewer()
    name = _required_cart_name(name)
    cart = frappe.get_doc("BZ Cart", name)
    state = cart_state(cart, customer=cart.customer or None)
    _log_access(cart, "Recheck", f"fingerprint={state.get('quote_fingerprint') or ''}")
    return {
        "name": cart.name,
        "checked_on": frappe.utils.now(),
        "quote": state["quote"],
        "quote_fingerprint": state["quote_fingerprint"],
    }


def copy_cart(name, *, include_customer=False, include_prices=False):
    require_cart_viewer()
    name = _required_cart_name(name)
    cart = frappe.get_doc("BZ Cart", name)
    state = cart_state(cart, customer=cart.customer or None)
    customer = _customer_info(cart.customer)
    lines = _copy_lines(state, include_customer=include_customer, include_prices=include_prices)
    text = "\n".join(lines) + "\n"
    _log_access(
        cart,
        "Copy",
        f"include_customer={int(bool(include_customer))};include_prices={int(bool(include_prices))}",
    )
    return {
        "name": cart.name,
        "text": text,
        "filename": f"cart-{cart.name}.txt",
    }


def csv_cart(name, *, include_customer=False, include_prices=False):
    require_cart_viewer()
    name = _required_cart_name(name)
    cart = frappe.get_doc("BZ Cart", name)
    state = cart_state(cart, customer=cart.customer or None)
    customer = _customer_info(cart.customer)
    columns = ["item_code", "item_name", "qty", "uom", "available", "issues"]
    if include_customer:
        columns.extend(["customer", "customer_name", "customer_email"])
    if include_prices:
        columns.extend(["price", "line_total"])
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for item in (state.get("quote") or {}).get("items") or []:
        row = [
            item.get("item_code"),
            item.get("item_name"),
            item.get("qty"),
            item.get("uom"),
            "1" if item.get("available") else "0",
            " | ".join(_issue_label(issue) for issue in item.get("issues") or []),
        ]
        if include_customer:
            row.extend(
                [
                    cart.customer or "",
                    customer.get("customer_name") or "",
                    customer.get("email") or "",
                ]
            )
        if include_prices:
            row.extend([_number(item.get("price")), _number(item.get("line_total"))])
        writer.writerow([_csv_safe(value) for value in row])
    _log_access(
        cart,
        "CSV",
        f"include_customer={int(bool(include_customer))};include_prices={int(bool(include_prices))}",
    )
    return {
        "name": cart.name,
        "csv": "\ufeff" + buffer.getvalue(),
        "filename": f"cart-{cart.name}.csv",
    }


def _required_cart_name(name):
    name = (name or "").strip()
    if not name or not frappe.db.exists("BZ Cart", name):
        frappe.throw(_("السلة غير موجودة"), frappe.DoesNotExistError)
    return name


def _customer_info(customer):
    if not customer:
        return {"name": "", "customer_name": "", "customer_group": "", "email": ""}
    row = frappe.db.get_value(
        "Customer",
        customer,
        ["customer_name", "customer_group"],
        as_dict=True,
    ) or {}
    email = frappe.db.get_value(
        "Portal User",
        {"parenttype": "Customer", "parent": customer},
        "user",
    ) or ""
    return {
        "name": customer,
        "customer_name": row.get("customer_name") or customer,
        "customer_group": row.get("customer_group") or "",
        "email": email,
    }


def _copy_lines(state, *, include_customer, include_prices):
    cart = state.get("cart") or {}
    quote = state.get("quote") or {}
    lines = [
        f"السلة: {cart.get('name') or ''}",
        f"الحالة: {cart.get('status') or ''}",
        f"آخر نشاط: {cart.get('last_activity_on') or ''}",
    ]
    customer = state.get("customer") or {}
    if include_customer:
        lines.append(
            "العميل: "
            + (customer.get("customer_name") or cart.get("customer") or "")
            + (f" ({customer.get('email')})" if customer.get("email") else "")
        )
    lines.append("")
    lines.append("الأصناف:")
    for item in quote.get("items") or []:
        line = (
            f"- {item.get('item_code') or ''} | {item.get('item_name') or ''}"
            f" | {item.get('qty')} {item.get('uom') or ''}"
        )
        if not item.get("available"):
            line += " | غير متاح"
        issues = " | ".join(_issue_label(issue) for issue in item.get("issues") or [])
        if issues:
            line += f" | {issues}"
        if include_prices:
            line += f" | السعر: {_number(item.get('price'))} | الإجمالي: {_number(item.get('line_total'))}"
        lines.append(line)
    if include_prices:
        lines.append("")
        lines.append(f"الإجمالي: {_number(quote.get('net_total'))} {quote.get('currency') or ''}")
    return lines


def _issue_label(issue):
    if not isinstance(issue, dict):
        return str(issue)
    return issue.get("message") or issue.get("code") or ""


def _number(value):
    if value is None:
        return ""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _csv_safe(value):
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


def _log_table_ready():
    return frappe.db.table_exists(ACCESS_LOG_DOCTYPE)


def _log_access(cart, action, details=""):
    if not _log_table_ready():
        return
    frappe.get_doc(
        {
            "doctype": ACCESS_LOG_DOCTYPE,
            "staff_user": frappe.session.user,
            "action": action,
            "cart": cart.name,
            "customer": cart.customer or None,
            "cart_user": cart.user or None,
            "item_count": len(cart.items or []),
            "request_id": get_request_id(),
            "details": (details or "")[:500],
        }
    ).insert(ignore_permissions=True)


def _access_log(cart_name):
    if not _log_table_ready():
        return []
    rows = frappe.get_all(
        ACCESS_LOG_DOCTYPE,
        filters={"cart": cart_name},
        fields=[
            "action",
            "staff_user",
            "item_count",
            "details",
            "creation",
        ],
        order_by="creation desc",
        limit=20,
    )
    for row in rows:
        row["creation"] = str(row.get("creation") or "")
    return rows
