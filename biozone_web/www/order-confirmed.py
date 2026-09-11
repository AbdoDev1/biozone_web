import frappe

from frappe import _
from biozone_web.utils import (
    get_header_context,
    redirect_staff_away_from_store,
)


def get_context(context):
    redirect_staff_away_from_store()

    context.no_cache = 1
    context.active_page = None

    context.update(get_header_context())

    order_name = (
        frappe.form_dict.get("name")
        or frappe.form_dict.get("order_name")
    )

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

    if so.owner != frappe.session.user:
        frappe.throw(
            _("لا تملك صلاحية عرض هذا الطلب"),
            frappe.PermissionError,
        )

    context.order_number = str(so.name)

    context.item_count = len(so.items)

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
        for item in so.items
    ]

    context.net_total = float(so.net_total or 0)
    context.net_total_display = f"{context.net_total:,.2f}"

    context.taxes = [
        {
            "description": tax.description or "",
            "tax_amount": float(tax.tax_amount or 0),
            "tax_amount_display": f"{float(tax.tax_amount or 0):,.2f}",
        }
        for tax in so.taxes
    ]

    context.grand_total = float(so.grand_total or 0)
    context.grand_total_display = f"{context.grand_total:,.2f}"

    return context
