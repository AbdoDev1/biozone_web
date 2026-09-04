import json

import frappe

from biozone_web.utils import get_header_context, require_staff_access


def get_context(context):
    require_staff_access()
    context.no_cache = 1
    context.active_page = "orders"
    context.update(get_header_context())
    context.today_display = frappe.utils.format_date(frappe.utils.today(), "d MMMM yyyy")
    context.csrf_token = frappe.sessions.get_csrf_token()

    # الطلبات بانتظار التأكيد: كل Sales Order مفيهاش submit لسه (Draft) —
    # دي الطلبات اللي بتبعت من المتجر عبر biozone_confirm_order بدون تأكيد
    # نهائي (قرار B4)، والموظف هو اللي بيقفلها من هنا.
    pending = frappe.get_all(
        "Sales Order",
        filters={"docstatus": 0, "status": "Draft"},
        fields=[
            "name",
            "customer",
            "customer_name",
            "transaction_date",
            "delivery_date",
            "net_total",
            "grand_total",
            "owner",
            "creation",
        ],
        order_by="creation asc",
    )

    # تفاصيل كاملة لكل طلب (للجدول + للدرج الجانبي) — بيتحطوا كـJSON زي
    # items_json في صفحة المخزون عشان الدرج يفتح فورًا من غير fetch إضافي،
    # والكميات والأسعار كلها من السيرفر (مطمّنة بإن مصدر الحقيقة هو ERPNext).
    full_orders = []
    for so in pending:
        so["items_count"] = frappe.db.count("Sales Order Item", {"parent": so["name"]})
        so["created_display"] = frappe.utils.format_datetime(so["creation"], "dd MMM yyyy")

        so_doc = frappe.get_doc("Sales Order", so["name"])
        full_orders.append(
            {
                "name": so["name"],
                "customer_name": so_doc.customer_name,
                "transaction_date": so["transaction_date"],
                "delivery_date": so["delivery_date"],
                "net_total": so_doc.net_total,
                "grand_total": so_doc.grand_total,
                "items_count": so["items_count"],
                "items": [
                    {
                        "item_name": it.item_name,
                        "item_code": it.item_code,
                        "qty": it.qty,
                        "uom": it.uom,
                        "rate": it.rate,
                        "amount": it.amount,
                    }
                    for it in so_doc.items
                ],
                "taxes": [
                    {"description": t.description, "tax_amount": t.tax_amount}
                    for t in so_doc.taxes
                ],
            }
        )

    # إجمالي البنود في الطلبات المعلّقة (للكارت الثاني في الـKPIs)
    context.total_items = sum(so["items_count"] for so in pending)

    # آخر الطلبات المؤكَّدة (submitted Sales Orders) لتبويب "المؤكَّدة مؤخرًا"
    confirmed = frappe.get_all(
        "Sales Order",
        filters={"docstatus": 1},
        fields=["name", "customer_name", "grand_total", "modified"],
        order_by="modified desc",
        limit_page_length=10,
    )
    for so in confirmed:
        so["confirmed_display"] = frappe.utils.format_datetime(so["modified"], "dd MMM yyyy")
    context.confirmed_orders = confirmed

    context.pending_orders = pending
    context.pending_count = len(pending)
    context.orders_json = json.dumps(full_orders, ensure_ascii=False, default=str)
    return context