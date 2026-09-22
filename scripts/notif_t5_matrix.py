#!/usr/bin/env python3
"""T5-01 — customer notif_poll: own count only, no override, same guards.

Headless, dev only. Asserts the focus-refresh endpoint passes the
request to core.poll_count safely and returns the bell-path count
format, under the same auth/CSRF envelope as list/mark.
Exit code 0 = all PASS, 1 = any FAIL.
"""
import sys

import frappe
from types import SimpleNamespace

SITE = "development.localhost"
STAFF = "abdulrahmanali.h.t@gmail.com"
CUST_A = "abdo4@gmail.com"
CUST_B = "abdo22@gmail.com"

PASS_COUNT = 0
FAIL_COUNT = 0


def show(tid, label, cond, extra=""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"PASS | {tid} | {label} {extra}")
    else:
        FAIL_COUNT += 1
        print(f"FAIL | {tid} | {label} {extra}")


def main():
    global PASS_COUNT, FAIL_COUNT
    frappe.init(site=SITE)
    frappe.connect()
    assert frappe.local.site == SITE, "dev-only guard"
    frappe.local.request = SimpleNamespace(host="biozone.pro", path="/x")

    from biozone_web.services import notifications as N
    from biozone_web.services import notification_customer as CUST

    frappe.local.conf.biozone_notifications_enabled = 1

    # Seed one delivered row for A (rolled back at the end).
    try:
        frappe.set_user(STAFF)
        N.notify("order_delivered", reference_doctype="Sales Order",
                 reference_name="SAL-ORD-2026-00061",
                 context={"order": "SAL-ORD-2026-00061"}, actor=STAFF)
        frappe.set_user(CUST_A)
        r = CUST.notif_poll()
        a_count = frappe.db.count(
            "Notification Log",
            {"for_user": CUST_A, "read": 0, "type": "BZ Delivered"})
        frappe.set_user(CUST_B)
        rb = CUST.notif_poll()
        show("T5-01", "poll returns current user's own count only",
             r.get("ok") and r.get("unread_count") == a_count and a_count >= 1
             and rb.get("unread_count") == 0,
             f"a={r.get('unread_count')} b={rb.get('unread_count')}")
        frappe.set_user(CUST_A)
        _row = frappe.db.get_value(
            "Notification Log",
            {"for_user": CUST_A, "type": "BZ Delivered", "read": 0}, "name")
        CUST.notif_mark_read(_row)
        r2 = CUST.notif_poll()
        show("T5-01", "read rows excluded from count",
             r2.get("unread_count") == a_count - 1,
             f"{a_count}->{r2.get('unread_count')}")
    except Exception as e:
        show("T5-01", "own count", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    # No user parameter is accepted to switch whose counter is read.
    try:
        frappe.set_user(CUST_A)
        rejected = False
        try:
            CUST.notif_poll(for_user=CUST_B)
        except TypeError:
            rejected = True
        show("T5-01", "no user override parameter", rejected)
    except Exception as e:
        show("T5-01", "no override", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    # Administrator, other System users and guests are refused.
    try:
        blocked = 0
        for u in ("Administrator", STAFF, "Guest"):
            try:
                frappe.set_user(u)
                CUST.notif_poll()
            except Exception:
                blocked += 1
        show("T5-01", "admin/staff/guest refused (same envelope as list/mark)",
             blocked == 3, f"blocked={blocked}/3")
    except Exception as e:
        show("T5-01", "refusals", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    print(f"\nMATRIX-T5 | pass={PASS_COUNT} fail={FAIL_COUNT}")
    frappe.destroy()
    sys.exit(1 if FAIL_COUNT else 0)


if __name__ == "__main__":
    main()
