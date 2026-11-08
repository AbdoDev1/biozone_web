import frappe

from biozone_web.services.cart import expire_cart

RETENTION_DAYS = 7
EXPIRE_BATCH = 5000
PURGE_BATCH = 500
EXPIRE_SAVEPOINT = "bz_expire_cart"


def expire_carts():
    """إنهاء السلال النشطة التي تجاوزت مدة الـ30 يومًا.

    عزل لكل سلة: savepoint + try/except + log_error، فلا يوقف فشل سلة
    واحدة بقية الدفعة. `expire_cart` نفسه يقفل الصف بالاسم.
    """
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
    failed = 0
    for name in names:
        frappe.db.savepoint(EXPIRE_SAVEPOINT)
        try:
            if expire_cart(name):
                expired += 1
        except Exception:
            failed += 1
            try:
                frappe.db.rollback(save_point=EXPIRE_SAVEPOINT)
            except Exception:
                frappe.logger("biozone").exception(
                    "expire_carts: savepoint rollback failed for %s", name
                )
            try:
                frappe.log_error(
                    title=f"expire_carts failed: {name}",
                    message=frappe.get_traceback(),
                )
            except Exception:
                frappe.logger("biozone").exception(
                    "expire_carts: log_error failed for %s", name
                )
    frappe.logger("biozone").info(
        "expire_carts: expired=%s failed=%s", expired, failed
    )
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


def purge_cart_snapshots():
    """Hard-delete expired Clear/Remove snapshots.

    Restored snapshots are kept (audit trail). Recent snapshots stay even if
    their status is Expired. `frappe.db.delete` bypasses the recycle bin, so
    no Deleted Document row is created for these short-lived recovery copies.
    """
    if not frappe.db.table_exists("BZ Cart Snapshot"):
        return 0
    now = frappe.utils.now_datetime()
    names = frappe.get_all(
        "BZ Cart Snapshot",
        filters={
            "expires_on": ["<=", now],
            "status": ["!=", "Restored"],
        },
        pluck="name",
        order_by="expires_on asc",
        limit=PURGE_BATCH,
    )
    if not names:
        return 0
    frappe.db.delete("BZ Cart Snapshot", {"name": ["in", names]})
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
