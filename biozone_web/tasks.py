import frappe

from biozone_web.services.cart import expire_cart

RETENTION_DAYS = 7
EXPIRE_BATCH = 5000
PURGE_BATCH = 500


def expire_carts():
    """إنهاء السلال النشطة التي تجاوزت مدة الـ30 يومًا."""
    now = frappe.utils.now_datetime()
    names = frappe.get_all(
        "BZ Cart",
        filters={
            "status": "Active",
            "expires_on": ["<=", now],
        },
        pluck="name",
        order_by="expires_on asc",
        limit=EXPIRE_BATCH,
    )
    expired = 0
    for name in names:
        if expire_cart(name):
            expired += 1
    frappe.logger("biozone").info("expire_carts: expired=%s", expired)
    return expired


def purge_retained_records():
    """حذف سجل الوصول والسلال المحوّلة الأقدم من 7 أيام.

    Sales Order والتأكيد (Receipt) يبقيان؛ يُفرَّغ رابط السلة فقط.
    """
    cutoff = frappe.utils.add_days(frappe.utils.now_datetime(), -RETENTION_DAYS)
    deleted_logs = _purge_access_logs(cutoff)
    deleted_carts = _purge_converted_carts(cutoff)
    frappe.logger("biozone").info(
        "purge_retained_records: logs=%s carts=%s cutoff=%s",
        deleted_logs,
        deleted_carts,
        cutoff,
    )
    return {"logs": deleted_logs, "carts": deleted_carts}


def _purge_access_logs(cutoff):
    if not frappe.db.table_exists("BZ Cart Access Log"):
        return 0
    names = frappe.get_all(
        "BZ Cart Access Log",
        filters={"creation": ["<", cutoff]},
        pluck="name",
        order_by="creation asc",
        limit=PURGE_BATCH,
    )
    if not names:
        return 0
    frappe.db.delete("BZ Cart Access Log", {"name": ["in", names]})
    return len(names)


def _purge_converted_carts(cutoff):
    names = frappe.db.sql(
        """
        select name
        from `tabBZ Cart`
        where status = %s
          and coalesce(converted_on, modified) < %s
        order by coalesce(converted_on, modified) asc
        limit %s
        """,
        ("Converted", cutoff, PURGE_BATCH),
        pluck=True,
    )
    deleted = 0
    for name in names:
        try:
            frappe.db.sql("update `tabBZ Checkout Receipt` set cart = null where cart = %s", (name,))
            if frappe.db.table_exists("BZ Cart Access Log"):
                frappe.db.sql("update `tabBZ Cart Access Log` set cart = null where cart = %s", (name,))
            frappe.db.delete("BZ Cart Merge", {"cart": name})
            frappe.delete_doc(
                "BZ Cart",
                name,
                ignore_permissions=True,
                ignore_missing=True,
            )
            deleted += 1
        except Exception:
            frappe.log_error(
                title="BZ cart retention delete failed",
                message=f"cart={name}\n{frappe.get_traceback()}",
            )
    return deleted
