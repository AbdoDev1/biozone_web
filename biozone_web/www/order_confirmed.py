import frappe

from frappe import _
from biozone_web.utils import (
    assert_can_view_sales_order,
    get_header_context,
    redirect_staff_away_from_store,
)


def get_context(context):
    redirect_staff_away_from_store()

    context.no_cache = 1
    context.active_page = None

    context.update(get_header_context())

    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login"
        raise frappe.Redirect

    order_name = frappe.form_dict.get("name") or frappe.form_dict.get("order_name")

    if not order_name:
        frappe.throw(
            _("رقم الطلب غير موجود"),
            frappe.DoesNotExistError,
        )

    if not frappe.db.exists("Sales Order", order_name):
        frappe.throw(
            _("الطلب غير موجود"),
            frappe.DoesNotExistError,
        )

    so = frappe.get_doc("Sales Order", order_name)

    # الطلبات الجديدة: owner الحقيقي. الطلبات القديمة (owner =
    # Administrator): fallback عبر Customer الفريد فقط.
    assert_can_view_sales_order(so)

    context.order_number = str(so.name)
    context.item_count = len(so.items or [])

    context.items = [
        {
            "item_name": item.item_name or "",
            "qty": float(item.qty or 0),
            "uom": item.uom or "",
            "rate": float(item.rate or 0),
            "rate_display": f"{float(item.rate or 0):,.2f}",
            "amount": float(item.amount or 0),
            "amount_display": f"{float(item.amount or 0):,.2f}",
        }
        for item in (so.items or [])
    ]

    net_total = float(so.net_total or 0)
    context.net_total = net_total
    context.net_total_display = f"{net_total:,.2f}"

    context.taxes = [
        {
            "description": tax.description or "",
            "tax_amount": float(tax.tax_amount or 0),
            "tax_amount_display": f"{float(tax.tax_amount or 0):,.2f}",
        }
        for tax in (so.taxes or [])
    ]

    grand_total = float(so.grand_total or 0)
    context.grand_total = grand_total
    context.grand_total_display = f"{grand_total:,.2f}"

    return context

