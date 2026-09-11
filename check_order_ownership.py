"""فحص ملكية طلبات المتجر — سكربت يدوي للتوثيق والتشغيل من bench console.

لا يُنفذ تلقائيًا. يحتاج مستخدمين حقيقيين وبيانات متجر.

التشغيل (من مجلد bench):
    bench --site development.localhost console
    >>> exec(open("apps/biozone_web/check_order_ownership.py").read())
    >>> check_legacy_admin_count()
    >>> check_new_order_owner("SAL-ORD-2026-00001", "user@example.com")
    >>> check_cross_user_denied("SAL-ORD-2026-00001", "other@example.com")
"""
import frappe

from biozone_web.utils import can_current_user_view_sales_order


def check_new_order_owner(order_name, expected_user):
    """يثبت أن الطلب الجديد owner = المستخدم الحقيقي وليس Administrator."""
    owner = frappe.db.get_value("Sales Order", order_name, "owner")
    assert owner == expected_user, f"owner={owner!r} expected={expected_user!r}"
    assert owner != "Administrator" or expected_user == "Administrator", (
        "طلب مستخدم عادي حُفظ بمالك Administrator"
    )
    print(f"OK: {order_name} owner={owner}")
    return owner


def check_cross_user_denied(order_name, other_user):
    """يثبت أن مستخدمًا آخر لا يستطيع عرض الطلب."""
    so = frappe.get_doc("Sales Order", order_name)
    allowed = can_current_user_view_sales_order(so, user=other_user)
    assert allowed is False, f"تسريب: {other_user} يستطيع عرض {order_name}"
    print(f"OK: {other_user} مرفوض من {order_name}")


def check_legacy_admin_count():
    """يعدد الطلبات القديمة التي مالكها Administrator (للترحيل اليدوي)."""
    count = frappe.db.count("Sales Order", {"owner": "Administrator"})
    print(f"طلبات بمالك Administrator: {count}")
    return count
