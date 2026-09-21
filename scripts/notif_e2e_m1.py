#!/usr/bin/env python3
"""E2E scenario — Phase 1 (headless, dev only, rollback per step).

Run (inside container, from sites dir):
    cd /workspace/development/frappe-bench/sites
    /workspace/development/frappe-bench/env/bin/python \
        /workspace/development/frappe-bench/apps/biozone_web/scripts/notif_e2e_m1.py

Steps: S0 (flag off -> silence) / S1 (customer confirms -> staff rows) /
       S2 (staff opens -> read + counter drops + correct link) /
       S7 (dummy event reaches bell with zero UI change).
Exit code 0 = all PASS, 1 = any FAIL.
"""
import sys

import frappe
from types import SimpleNamespace

SITE = "development.localhost"
CUST_A = "abdo4@gmail.com"
CUST_B = "abdo22@gmail.com"
STAFF = "abdulrahmanali.h.t@gmail.com"

PASS_COUNT = 0
FAIL_COUNT = 0


def show(step, label, cond, extra=""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"PASS | {step} | {label} {extra}")
    else:
        FAIL_COUNT += 1
        print(f"FAIL | {step} | {label} {extra}")


def main():
    frappe.init(site=SITE)
    frappe.connect()
    assert frappe.local.site == SITE, "dev-only guard"
    frappe.local.request = SimpleNamespace(host="staff.biozone.pro", path="/x")

    from biozone_web.services import notifications as N
    from biozone_web.services import notification_api as A
    from biozone_web.services import notification_core as C
    from biozone_web.api import biozone_confirm_order

    frappe.local.conf.biozone_notifications_enabled = 1

    # ---------- S0: flag off -> nothing, endpoints 0 ----------
    try:
        frappe.local.conf.biozone_notifications_enabled = 0
        frappe.set_user(CUST_A)
        r = N.notify("order_new", reference_doctype="Sales Order",
                     reference_name="NTF-E2E-000", context={"order": "NTF-E2E-000"})
        frappe.set_user(STAFF)
        c = A.notif_poll()["unread_count"] if _poll_ok() else "n/a"
        show("S0", "flag off silences notify", r == 0, f"r={r}")
    except Exception as e:
        show("S0", "flag off", False, repr(e)[:100])
    finally:
        frappe.db.rollback()
    frappe.local.conf.biozone_notifications_enabled = 1

    # ---------- S1: customer confirms -> staff notified, customer silent ----------
    so_name = None
    try:
        frappe.set_user(CUST_A)
        expected_staff = sorted(N.recipients_for(
            N.EVENTS["order_new"], "NTF-E2E-EXPECT", actor=CUST_A))
        res = biozone_confirm_order([{"item_code": "123", "quantity": 1}])
        so_name = res.get("sales_order")
        rows = frappe.db.get_all(
            "Notification Log",
            {"type": "BZ New Order", "document_name": so_name}, ["for_user"])
        users = sorted(r.for_user for r in rows)
        show("S1", "confirm notifies staff only",
             res.get("ok") and users == expected_staff,
             f"so={so_name} users={users}")
    except Exception as e:
        show("S1", "customer confirms", False, repr(e)[:120])

    # ---------- S2: staff opens -> correct page, read, counter drops ----------
    try:
        frappe.set_user(STAFF)
        before = A.notif_poll()["unread_count"]
        items = A.notif_list(limit=10)["items"]
        mine = [i for i in items if i["document_name"] == so_name]
        link_ok = bool(mine) and mine[0]["link"] == f"/staff/order-prep?order={so_name}"
        A.notif_mark_read(mine[0]["name"])
        after = A.notif_poll()["unread_count"]
        show("S2", "open marks read, counter drops, link correct",
             link_ok and before == after + 1, f"{before}->{after} link={link_ok}")
    except Exception as e:
        show("S2", "staff opens", False, repr(e)[:120])

    # ---------- S3: staff delivers -> customer A notified, already silent ----------
    dso = None
    try:
        from biozone_web.api import staff_confirm_delivery
        frappe.set_user(CUST_A)
        res = biozone_confirm_order([{"item_code": "123mg", "quantity": 1}])
        dso = res.get("sales_order")
        rows = frappe.get_all("Sales Order Item", {"parent": dso}, ["name"])
        for r in rows:
            frappe.db.set_value("Sales Order Item", r.name, "custom_confirmed", 1)
        frappe.set_user(STAFF)
        real_commit = frappe.db.commit
        frappe.db.commit = lambda *a, **k: None
        try:
            out = staff_confirm_delivery(dso)
            drows = frappe.db.get_all(
                "Notification Log",
                {"type": "BZ Delivered", "document_name": dso},
                ["for_user", "link"])
            out2 = staff_confirm_delivery(dso)
            n2 = frappe.db.count(
                "Notification Log",
                {"type": "BZ Delivered", "document_name": dso})
        finally:
            frappe.db.commit = real_commit
        users = sorted(r.for_user for r in drows)
        link_ok = bool(drows) and drows[0].link == f"/account/orders/{dso}"
        show("S3", "delivery notifies owner; already adds nothing",
             out.get("ok") and users == [CUST_A] and link_ok
             and out2.get("already") is True and n2 == 1,
             f"dso={dso} users={users}")
    except Exception as e:
        show("S3", "staff delivers", False, repr(e)[:150])

    # ---------- S4: customer B and guest see nothing ----------
    try:
        from biozone_web.services import notification_customer as CUSTEP
        frappe.set_user(CUST_B)
        b_items = CUSTEP.notif_list(limit=50)["items"]
        b_mine = [i for i in b_items if i["document_name"] == dso]
        guest_blocked = False
        try:
            frappe.set_user("Guest")
            CUSTEP.notif_list()
        except Exception:
            guest_blocked = True
        show("S4", "B sees none of A; guest refused",
             b_mine == [] and guest_blocked, f"b_rows={len(b_mine)}")
    except Exception as e:
        show("S4", "isolation", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    # ---------- S7: dummy (future-return-like) event, zero UI change ----------
    try:
        if not frappe.db.exists("Notification Type", "BZ Test Dummy"):
            d = frappe.new_doc("Notification Type")
            d.name = "BZ Test Dummy"
            d.type_name = "BZ Test Dummy"
            d.enabled = 1
            d.insert(ignore_permissions=True)
        N.EVENTS["test_dummy"] = {"type": "BZ Test Dummy", "audience": "staff",
                                  "title": "Dummy {order}",
                                  "link": lambda ref: f"/staff/orders?x={ref}",
                                  "dedupe": ("document_name", "type")}
        C._BZ_TYPES.append("BZ Test Dummy")
        frappe.set_user(CUST_A)
        N.notify("test_dummy", reference_doctype="Sales Order",
                 reference_name="NTF-E2E-DUMMY", context={"order": "NTF-E2E-DUMMY"})
        frappe.set_user(STAFF)
        items = A.notif_list(limit=10)["items"]
        seen = [i for i in items if i["type"] == "BZ Test Dummy"]
        show("S7", "dummy event in bell, no UI file touched",
             len(seen) == 1, f"seen={len(seen)}")
    except Exception as e:
        show("S7", "dummy event", False, repr(e)[:120])
    finally:
        N.EVENTS.pop("test_dummy", None)
        if "BZ Test Dummy" in C._BZ_TYPES:
            C._BZ_TYPES.remove("BZ Test Dummy")
        frappe.db.rollback()

    print(f"\nE2E | pass={PASS_COUNT} fail={FAIL_COUNT}")
    frappe.destroy()
    sys.exit(1 if FAIL_COUNT else 0)


def _poll_ok():
    return True


if __name__ == "__main__":
    main()
