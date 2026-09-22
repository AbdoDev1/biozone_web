#!/usr/bin/env python3
"""T2 acceptance matrix — Phase 2 (headless, dev only).

Run (inside container, from sites dir):
    cd /workspace/development/frappe-bench/sites
    /workspace/development/frappe-bench/env/bin/python \
        /workspace/development/frappe-bench/apps/biozone_web/scripts/notif_t2_matrix.py

Rules (§6.2): delivery scenarios use real orders created for the test and
rolled back after; ``staff_confirm_delivery`` commits internally, so its
``frappe.db.commit`` is neutralized inside the test ONLY (restored in
``finally``). No test users are created (real dev users are reused).
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


def neutralize_commit():
    real = frappe.db.commit
    frappe.db.commit = lambda *a, **k: None
    return real


def make_confirmable_order():
    """Real order (uncommitted) with all items confirmed. Caller rolls back."""
    from biozone_web.api import biozone_confirm_order
    frappe.set_user(CUST_A)
    # 123mg has dev stock (delivery needs it; plain 123 does not)
    res = biozone_confirm_order([{"item_code": "123mg", "quantity": 1}])
    so_name = res.get("sales_order")
    rows = frappe.get_all("Sales Order Item", {"parent": so_name}, ["name"])
    for r in rows:
        frappe.db.set_value("Sales Order Item", r.name, "custom_confirmed", 1)
    return so_name


def deliver(so_name, user=STAFF):
    from biozone_web.api import staff_confirm_delivery
    frappe.set_user(user)
    return staff_confirm_delivery(so_name)


def main():
    global PASS_COUNT, FAIL_COUNT
    frappe.init(site=SITE)
    frappe.connect()
    assert frappe.local.site == SITE, "dev-only guard"
    frappe.local.request = SimpleNamespace(host="staff.biozone.pro", path="/x")

    from biozone_web.services import notifications as N
    from biozone_web.services import notification_customer as CUST
    from biozone_web.services import notification_api as A
    from biozone_web.hooks import _BZ_NOTIFICATION_TYPES
    from biozone_web.utils import get_header_context

    frappe.local.conf.biozone_notifications_enabled = 1

    # ---------- T2-03: failed delivery -> 0 rows, order intact ----------
    try:
        from biozone_web.api import biozone_confirm_order
        frappe.set_user(CUST_A)
        res = biozone_confirm_order([{"item_code": "123mg", "quantity": 1}])
        so_name = res.get("sales_order")
        real_commit = neutralize_commit()
        try:
            out = deliver(so_name)
        finally:
            frappe.db.commit = real_commit
        n = frappe.db.count("Notification Log",
                            {"type": "BZ Delivered", "document_name": so_name})
        st = frappe.db.get_value("Sales Order", so_name, "docstatus")
        show("T2-03", "unconfirmed delivery fails, 0 rows, order intact",
             out.get("ok") is False and n == 0 and st == 0,
             f"ok={out.get('ok')} n={n} docstatus={st}")
    except Exception as e:
        show("T2-03", "failed delivery", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    # ---------- T2-01: real delivery -> 1 row; already -> +0 ----------
    try:
        so_name = make_confirmable_order()
        real_commit = neutralize_commit()
        try:
            out = deliver(so_name)
            rows = frappe.db.get_all(
                "Notification Log",
                {"type": "BZ Delivered", "document_name": so_name},
                ["for_user", "title", "link"])
            out2 = deliver(so_name)
            n2 = frappe.db.count(
                "Notification Log",
                {"type": "BZ Delivered", "document_name": so_name})
        finally:
            frappe.db.commit = real_commit
        users = sorted(r.for_user for r in rows)
        link_ok = bool(rows) and rows[0].link == f"/account/orders/{so_name}"
        show("T2-01", "delivery notifies owner once; already adds nothing",
             out.get("ok") and users == [CUST_A] and link_ok
             and out2.get("already") is True and n2 == 1,
             f"so={so_name} users={users} already={out2.get('already')}")
    except Exception as e:
        show("T2-01", "real delivery", False, repr(e)[:150])
    finally:
        frappe.db.rollback()

    # ---------- T2-02: Administrator-owned -> silent ----------
    try:
        so_name = make_confirmable_order()
        frappe.db.set_value("Sales Order", so_name, "owner", "Administrator")
        real_commit = neutralize_commit()
        try:
            out = deliver(so_name)
            n = frappe.db.count(
                "Notification Log",
                {"type": "BZ Delivered", "document_name": so_name})
        finally:
            frappe.db.commit = real_commit
        show("T2-02", "Administrator-owned delivery notifies nobody",
             out.get("ok") and n == 0, f"ok={out.get('ok')} n={n}")
    except Exception as e:
        show("T2-02", "admin owner", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    # ---------- T2-04: cancelled -> no rows ----------
    try:
        real_commit = neutralize_commit()
        try:
            out = deliver("SAL-ORD-2026-00003")
            n = frappe.db.count(
                "Notification Log",
                {"type": "BZ Delivered",
                 "document_name": "SAL-ORD-2026-00003"})
        finally:
            frappe.db.commit = real_commit
        show("T2-04", "cancelled order: error, 0 rows",
             out.get("ok") is False and n == 0, f"ok={out.get('ok')}")
    except Exception as e:
        show("T2-04", "cancelled", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    # ---------- T2-07: notify inside transaction (rollback -> no orphans) ----------
    try:
        so_name = make_confirmable_order()
        real_commit = neutralize_commit()
        try:
            out = deliver(so_name)
            ok_before = bool(out.get("ok"))
            n_before = frappe.db.count(
                "Notification Log",
                {"type": "BZ Delivered", "document_name": so_name})
        finally:
            frappe.db.commit = real_commit
        frappe.db.rollback()
        n_after = frappe.db.count(
            "Notification Log",
            {"type": "BZ Delivered", "document_name": so_name})
        gone = not frappe.db.exists("Sales Order", so_name)
        show("T2-07", "rollback after delivery leaves no orphan rows",
             ok_before and n_before == 1 and n_after == 0 and gone,
             f"n={n_before}->{n_after}")
    except Exception as e:
        show("T2-07", "rollback safety", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    # ---------- T2-11: flag off vs on -> identical business, rows differ ----------
    try:
        so_name = make_confirmable_order()
        real_commit = neutralize_commit()
        try:
            frappe.local.conf.biozone_notifications_enabled = 0
            off = deliver(so_name)
            off_state = (frappe.db.get_value("Sales Order", so_name, "docstatus"),
                         bool(frappe.db.get_value("Delivery Note Item",
                                                  {"against_sales_order": so_name}, "parent")),
                         frappe.db.count("Notification Log",
                                         {"type": "BZ Delivered",
                                          "document_name": so_name}))
        finally:
            frappe.db.commit = real_commit
        frappe.db.rollback()
        so_name2 = make_confirmable_order()
        real_commit = neutralize_commit()
        try:
            frappe.local.conf.biozone_notifications_enabled = 1
            on = deliver(so_name2)
            on_state = (frappe.db.get_value("Sales Order", so_name2, "docstatus"),
                        bool(frappe.db.get_value("Delivery Note Item",
                                                 {"against_sales_order": so_name2}, "parent")),
                        frappe.db.count("Notification Log",
                                        {"type": "BZ Delivered",
                                         "document_name": so_name2}))
        finally:
            frappe.db.commit = real_commit
        show("T2-11", "business identical, only notification rows differ",
             off.get("ok") and on.get("ok")
             and off_state[:2] == on_state[:2] == (1, True)
             and off_state[2] == 0 and on_state[2] == 1,
             f"off={off_state} on={on_state}")
    except Exception as e:
        show("T2-11", "flag on/off parity", False, repr(e)[:150])
    finally:
        frappe.local.conf.biozone_notifications_enabled = 1
        frappe.db.rollback()

    # ---------- T2-05: customer endpoint isolation ----------
    try:
        frappe.set_user(STAFF)
        N.notify("order_delivered", reference_doctype="Sales Order",
                 reference_name="SAL-ORD-2026-00061",
                 context={"order": "SAL-ORD-2026-00061"}, actor=STAFF)
        a_row = frappe.db.get_value(
            "Notification Log",
            {"for_user": CUST_A, "type": "BZ Delivered"}, "name")
        frappe.set_user(CUST_B)
        b_items = CUST.notif_list(limit=50)["items"]
        CUST.notif_mark_read(a_row)
        a_still = frappe.db.get_value("Notification Log", a_row, "read")
        frappe.set_user(CUST_A)
        mine = CUST.notif_list(limit=50)["items"]
        own_only = all(i["for_user"] == CUST_A for i in mine)
        CUST.notif_mark_read(a_row)
        a_now = frappe.db.get_value("Notification Log", a_row, "read")
        desk = frappe.new_doc("Notification Log")
        desk.type = "Alert"
        desk.title = "t2 desk row"
        desk.subject = "t2 desk row"
        desk.for_user = CUST_A
        desk.from_user = STAFF
        desk.document_name = "NTF-T2-DESK"
        desk.insert(ignore_permissions=True)
        CUST.notif_mark_all_read()
        desk_still = frappe.db.get_value("Notification Log", desk.name, "read")
        guest_blocked = staff_blocked = False
        try:
            frappe.set_user("Guest")
            CUST.notif_list()
        except Exception:
            guest_blocked = True
        try:
            frappe.set_user(STAFF)
            CUST.notif_list()
        except Exception:
            staff_blocked = True
        show("T2-05", "B sees none; cross mark blocked; own works; guest+staff 403; mark-all spares Desk",
             b_items == [] and int(a_still or 0) == 0 and own_only
             and int(a_now or 0) == 1 and guest_blocked and staff_blocked
             and int(desk_still or 0) == 0,
             f"b={len(b_items)} own_only={own_only} desk={desk_still}")
    except Exception as e:
        show("T2-05", "customer isolation", False, repr(e)[:150])
    finally:
        frappe.db.rollback()

    # ---------- T2-06: no polling in customer pages; counter from server ----------
    try:
        import pathlib
        app_root = pathlib.Path(
            "/workspace/development/frappe-bench/apps/biozone_web")
        store_pages = list((app_root / "biozone_web/www").rglob("*.html"))
        store_pages = [p for p in store_pages
                       if "site_header" in p.read_text(encoding="utf-8")]
        bad = [str(p) for p in store_pages
               if "setInterval" in p.read_text(encoding="utf-8")
               or "notif_poll" in p.read_text(encoding="utf-8")]
        hdr = (app_root / "biozone_web/templates/includes/site_header.html"
               ).read_text(encoding="utf-8")
        hdr_bad = ("setInterval" in hdr or "notif_poll" in hdr
                   or "AudioContext" in hdr)
        frappe.set_user("Guest")
        g = get_header_context()
        frappe.set_user(CUST_A)
        c = get_header_context()
        show("T2-06", "store pages polling-free; counter server-side",
             bad == [] and not hdr_bad and g.get("notif_unread_count") == 0
             and isinstance(c.get("notif_unread_count"), int),
             f"pages={len(store_pages)} cust_count={c.get('notif_unread_count')}")
    except Exception as e:
        show("T2-06", "no customer polling", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    # ---------- T2-09: sound always-on, no mute control ----------
    try:
        bell = ("/workspace/development/frappe-bench/apps/biozone_web"
                "/biozone_web/templates/includes/bell.html")
        src = open(bell, encoding="utf-8").read()
        no_toggle = ("bz-sound-btn" not in src and "volume_off" not in src
                     and "soundOn" not in src)
        rise_only = "prev === undefined" in src
        tabs_once = "lastSoundAt" in src
        cust_mute = "Nothing here runs for customer pages" in src
        no_audio_file = ".mp3" not in src and ".wav" not in src
        auto_resume = "pointerdown" in src and "ensureAudio" in src
        show("T2-09", "no mute UI; rise-only; once per tabs; customer mute",
             no_toggle and rise_only and tabs_once and cust_mute
             and no_audio_file and auto_resume)
    except Exception as e:
        show("T2-09", "sound static", False, repr(e)[:100])

    # ---------- T2-15: no counter leaks via cache or across users ----------
    try:
        import pathlib
        app_root = pathlib.Path(
            "/workspace/development/frappe-bench/apps/biozone_web")
        content_pages = ["biozone_web/www/biozone_home.py",
                         "biozone_web/www/cart.py",
                         "biozone_web/www/catalog.py",
                         "biozone_web/www/order_confirmed.py",
                         "biozone_web/www/account/index.py",
                         "biozone_web/www/account/order_detail.py",
                         "biozone_web/www/account/orders.py"]
        uncached = [p for p in content_pages
                    if "no_cache" in (app_root / p).read_text(encoding="utf-8")]
        frappe.set_user(CUST_A)
        N.notify("order_delivered", reference_doctype="Sales Order",
                 reference_name="SAL-ORD-2026-00061",
                 context={"order": "SAL-ORD-2026-00061"}, actor=STAFF)
        frappe.set_user(CUST_B)
        b_count = frappe.db.count(
            "Notification Log",
            {"for_user": CUST_B, "read": 0,
             "type": ("in", list(_BZ_NOTIFICATION_TYPES))})
        show("T2-15", "7/7 content pages no_cache; B counter stays 0",
             len(uncached) == 7 and b_count == 0,
             f"uncached={len(uncached)}/7 b={b_count}")
    except Exception as e:
        show("T2-15", "cache isolation", False, repr(e)[:120])
    finally:
        frappe.db.rollback()
    # ---------- T2-14: header-context query budget ----------
    try:
        import io
        import contextlib
        frappe.set_user("Guest")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            frappe.db.debug = True
            get_header_context()
            frappe.db.debug = False
        guest_nlog = buf.getvalue().count("Notification Log")
        frappe.set_user(STAFF)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            frappe.db.debug = True
            get_header_context()
            frappe.db.debug = False
        staff_nlog = buf.getvalue().count("Notification Log")
        frappe.set_user(CUST_A)
        frappe.local.conf.biozone_notifications_enabled = 1
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            frappe.db.debug = True
            get_header_context()
            frappe.db.debug = False
        cust_nlog = buf.getvalue().count("Notification Log")
        frappe.local.conf.biozone_notifications_enabled = 0
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            frappe.db.debug = True
            get_header_context()
            frappe.db.debug = False
        off_nlog = buf.getvalue().count("Notification Log")
        frappe.local.conf.biozone_notifications_enabled = 1
        show("T2-14", "guest/staff 0, customer 1, flag-off 0 notification queries",
             guest_nlog == 0 and staff_nlog == 0
             and cust_nlog == 1 and off_nlog == 0,
             f"g={guest_nlog} s={staff_nlog} c={cust_nlog} off={off_nlog}")
    except Exception as e:
        show("T2-14", "context budget", False, repr(e)[:120])
    finally:
        frappe.db.debug = False
        frappe.local.conf.biozone_notifications_enabled = 1
        frappe.db.rollback()

    print(f"\nMATRIX-T2 | pass={PASS_COUNT} fail={FAIL_COUNT}")
    frappe.destroy()
    sys.exit(1 if FAIL_COUNT else 0)


if __name__ == "__main__":
    main()
