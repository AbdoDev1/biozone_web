#!/usr/bin/env python3
"""T1 acceptance matrix — Phase 1 (headless, dev only, rollback after each case).

Run (inside container, from sites dir):
    cd /workspace/development/frappe-bench/sites
    /workspace/development/frappe-bench/env/bin/python \
        /workspace/development/frappe-bench/apps/biozone_web/scripts/notif_t1_matrix.py

Covers the H (headless) subset of T1-01..T1-22. L/visual cases (T1-07, T1-13
timing, T1-15, T1-19) run separately against the live server + browser.
Exit code 0 = all PASS, 1 = any FAIL.
"""
import sys

import frappe
from types import SimpleNamespace

SITE = "development.localhost"
STAFF_REAL = "abdulrahmanali.h.t@gmail.com"
STAFF_TEST = "zzz_agent_staff@example.com"
CUST_A = "abdo4@gmail.com"
CUST_B = "abdo22@gmail.com"

# Error Log titles this run may create (T1-05, T1-20, T1-09). The teardown
# deletes only rows carrying these titles that did not exist at start,
# then commits — §6.3-1 must read delta zero.
OWN_ERROR_TITLES = [
	"Biozone unknown notification event: NOPE-BAD-EVENT",
	"Biozone notification: no recipients",
	"Biozone notification bad link: __t1_09",
]

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


def err_count():
    return frappe.db.count("Error Log")


def bz_count(docname=None, ntype=None):
    f = {}
    if docname:
        f["document_name"] = docname
    if ntype:
        f["type"] = ntype
    return frappe.db.count("Notification Log", f)


def main():
    global PASS_COUNT, FAIL_COUNT
    frappe.init(site=SITE)
    frappe.connect()
    assert frappe.local.site == SITE, "dev-only guard"
    # headless requests carry no host; staff endpoints enforce domain guard
    frappe.local.request = SimpleNamespace(host="staff.biozone.pro", path="/x")

    from biozone_web.services import notifications as N
    from biozone_web.services import notification_api as A
    from biozone_web.services import notification_core as C
    from biozone_web.hooks import _BZ_NOTIFICATION_TYPES

    DOC = "NTF-TEST-ORDER-001"

    # §6.3-1 snapshot: only rows with our titles created after this point
    # are removed by the teardown (pre-existing rows are never touched).
    _err_before = frappe.db.count("Error Log")
    _err_own_before = set(
        r.name for r in frappe.db.get_all(
            "Error Log", {"method": ("in", OWN_ERROR_TITLES)}, ["name"]))

    # ---------- T1-21: single source of type names, hooks.py import-free ----------
    try:
        import biozone_web.hooks as _hooks_mod
        reg_types = sorted(e["type"] for e in N.EVENTS.values())
        no_imports = True
        with open(_hooks_mod.__file__, encoding="utf-8") as _f:
            for _line in _f:
                _s = _line.strip()
                if _s.startswith("import ") or _s.startswith("from "):
                    no_imports = False
        show("T1-21", "registry types == _BZ_NOTIFICATION_TYPES, hooks import-free",
             reg_types == sorted(_BZ_NOTIFICATION_TYPES) and no_imports)
    except Exception as e:
        show("T1-21", "registry/constant check", False, repr(e)[:100])

    # ---------- T1-18: starting state ----------
    try:
        old_patch = frappe.db.exists(
            "Patch Log", {"patch": "biozone_web.patches.v0_0.bz_notification_types"})
        n_new = frappe.db.exists("Notification Type", "BZ New Order")
        n_del = frappe.db.exists("Notification Type", "BZ Delivered")
        show("T1-18", "old patch logged; 2 legacy types skipped cleanly",
             bool(old_patch) and bool(n_new) and bool(n_del))
    except Exception as e:
        show("T1-18", "starting state", False, repr(e)[:100])

    # ---------- T1-17: hooks merge ----------
    try:
        hooks = frappe.get_hooks("notification_skip_email_types") or []
        show("T1-17", "skip list = Alert + our 4",
             hooks == ["Alert"] + list(_BZ_NOTIFICATION_TYPES), str(hooks))
    except Exception as e:
        show("T1-17", "hooks merge", False, repr(e)[:100])

    # ---------- T1-16: new-user seed has no BZ ----------
    try:
        from frappe.desk.doctype.notification_settings.notification_settings import (
            get_default_email_notification_types as gdef)
        defaults = gdef()
        show("T1-16", "default email types contain no BZ",
             not any("BZ" in t for t in defaults), str(sorted(defaults)))
    except Exception as e:
        show("T1-16", "default email types", False, repr(e)[:100])

    # ---------- T1-01: flag off ----------
    try:
        prev = frappe.local.conf.get("biozone_notifications_enabled", None)
        frappe.local.conf.biozone_notifications_enabled = 0
        e0 = err_count()
        r = N.notify("order_new", reference_doctype="Sales Order",
                     reference_name=DOC, context={"order": DOC})
        n = bz_count(DOC)
        frappe.local.conf.biozone_notifications_enabled = prev
        show("T1-01", "flag off -> notify 0, zero rows",
             r == 0 and n == 0, f"r={r} n={n}")
    except Exception as e:
        frappe.local.conf.biozone_notifications_enabled = prev
        show("T1-01", "flag off", False, repr(e)[:100])
    finally:
        frappe.db.rollback()

    # ensure flag on for the rest (in-memory only, file untouched)
    frappe.local.conf.biozone_notifications_enabled = 1

    # ---------- T1-03: recipients ----------
    try:
        frappe.set_user(STAFF_REAL)
        em = N.recipients_for(N.EVENTS["order_new"], DOC, actor=STAFF_REAL)
        adm = frappe.db.get_value("User", "Administrator", "email") or "Administrator"
        show("T1-03", "staff emails, Administrator + actor excluded",
             STAFF_TEST in em and STAFF_REAL not in em
             and adm not in em and "Administrator" not in em, str(sorted(em)))
    except Exception as e:
        show("T1-03", "recipients", False, repr(e)[:100])
    finally:
        frappe.db.rollback()

    # ---------- T1-02: real confirm -> exact staff rows, none for customer ----------
    try:
        from biozone_web.api import biozone_confirm_order
        frappe.set_user(CUST_A)
        expected_staff = sorted(N.recipients_for(
            N.EVENTS["order_new"], "NTF-EXPECT", actor=CUST_A))
        res = biozone_confirm_order([{"item_code": "123", "quantity": 1}])
        so_name = res.get("sales_order")
        staff_rows = frappe.db.get_all(
            "Notification Log",
            {"type": "BZ New Order", "document_name": so_name},
            ["for_user", "title", "link"])
        users = sorted(r.for_user for r in staff_rows)
        cust_rows = [r for r in staff_rows if r.for_user == CUST_A]
        link_ok = all(r.link == f"/staff/order-prep?order={so_name}" for r in staff_rows)
        show("T1-02", "confirm creates exact staff rows, none for customer",
             res.get("ok") and users == expected_staff and not cust_rows and link_ok,
             f"so={so_name} rows={len(staff_rows)}")
    except Exception as e:
        show("T1-02", "real confirm", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    # ---------- T1-04: dedupe ----------
    try:
        frappe.set_user(CUST_A)
        n_staff = len(N.recipients_for(N.EVENTS["order_new"], DOC, actor=CUST_A))
        r1 = N.notify("order_new", reference_doctype="Sales Order",
                      reference_name=DOC, context={"order": DOC}, actor=CUST_A)
        n1 = bz_count(DOC, "BZ New Order")
        r2 = N.notify("order_new", reference_doctype="Sales Order",
                      reference_name=DOC, context={"order": DOC}, actor=CUST_A)
        n2 = bz_count(DOC, "BZ New Order")
        show("T1-04", "repeat notify adds nothing (dedupe_on)",
             r1 == n_staff and n1 == n_staff and r2 == 0 and n2 == n_staff,
             f"r1={r1} n={n1} r2={r2} n={n2}")
    except Exception as e:
        show("T1-04", "dedupe", False, repr(e)[:100])
    finally:
        frappe.db.rollback()

    # ---------- T1-22: retry safety (deadlock-style rollback + re-notify) ----------
    try:
        frappe.set_user(CUST_A)
        n_staff = len(N.recipients_for(N.EVENTS["order_new"], DOC, actor=CUST_A))
        N.notify("order_new", reference_doctype="Sales Order",
                 reference_name=DOC, context={"order": DOC}, actor=CUST_A)
        frappe.db.rollback()  # simulated deadlock retry: first attempt discarded
        N.notify("order_new", reference_doctype="Sales Order",
                 reference_name=DOC, context={"order": DOC}, actor=CUST_A)
        n = bz_count(DOC, "BZ New Order")
        show("T1-22", "rollback + re-notify -> exactly one set",
             n == n_staff, f"n={n}")
    except Exception as e:
        show("T1-22", "retry safety", False, repr(e)[:100])
    finally:
        frappe.db.rollback()

    # ---------- T1-05: failure injection ----------
    try:
        frappe.set_user(CUST_A)
        e0 = err_count()
        r = N.notify("NOPE-BAD-EVENT", reference_doctype="Sales Order",
                     reference_name=DOC, context={"order": DOC})
        e1 = err_count()
        n = bz_count(DOC)
        show("T1-05", "unknown event: 0 rows, 1 error, no raise",
             r == 0 and n == 0 and (e1 - e0) == 1, f"r={r} errs={e1 - e0}")
    except Exception as e:
        show("T1-05", "failure injection", False, repr(e)[:100])
    finally:
        frappe.db.rollback()

    # ---------- T1-20: staff event, zero recipients -> Error Log line ----------
    try:
        N.EVENTS["__t1_20"] = {"type": "BZ New Order", "audience": lambda ctx: [],
                               "staff_event": True, "title": "t",
                               "link": lambda ref: f"/staff/order-prep?order={ref}",
                               "dedupe": ("document_name", "type")}
        e0 = err_count()
        r = N.notify("__t1_20", reference_doctype="Sales Order",
                     reference_name=DOC, context={"order": DOC})
        e1 = err_count()
        show("T1-20", "zero-recipient staff event logs once, business ok",
             r == 0 and (e1 - e0) == 1, f"r={r} errs={e1 - e0}")
    except Exception as e:
        show("T1-20", "zero recipients", False, repr(e)[:100])
    finally:
        N.EVENTS.pop("__t1_20", None)
        frappe.db.rollback()

    # ---------- T1-09: hostile links rejected ----------
    try:
        bad = ["https://evil.example/x", "//evil.example/x",
               "javascript:alert(1)", "data:text/html,hi"]
        ok = True
        for i, link in enumerate(bad):
            N.EVENTS["__t1_09"] = {"type": "BZ New Order", "audience": "staff",
                                   "title": "t", "link": lambda ref, _l=link: _l,
                                   "dedupe": ("document_name", "type")}
            r = N.notify("__t1_09", reference_doctype="Sales Order",
                         reference_name=f"{DOC}-{i}", context={"order": DOC})
            if r != 0 or bz_count(f"{DOC}-{i}") != 0:
                ok = False
        show("T1-09", "absolute/protocol-relative/js links rejected", ok)
    except Exception as e:
        show("T1-09", "link validation", False, repr(e)[:100])
    finally:
        N.EVENTS.pop("__t1_09", None)
        frappe.db.rollback()

    # ---------- T1-08: XSS stored literally ----------
    try:
        xname = "<img src=x onerror=alert(1)>"
        N.notify("order_new", reference_doctype="Sales Order",
                 reference_name=DOC, context={"order": xname}, actor=CUST_A)
        row = frappe.db.get_value("Notification Log",
                                  {"document_name": DOC, "type": "BZ New Order"},
                                  ["title"], as_dict=True)
        stored = (row.title or "") if row else ""
        show("T1-08", "xss inert (brackets literal, handler stripped; bell uses textContent)",
             "<img" in stored and "onerror" not in stored, stored[:60])
    except Exception as e:
        show("T1-08", "xss literal", False, repr(e)[:100])
    finally:
        frappe.db.rollback()

    # ---------- T1-06: isolation (endpoints are staff-only, session-scoped) ----------
    try:
        frappe.set_user(CUST_A)
        N.notify("order_new", reference_doctype="Sales Order",
                 reference_name=DOC, context={"order": DOC}, actor=CUST_A)
        own_row = frappe.db.get_value(
            "Notification Log",
            {"for_user": STAFF_REAL, "document_name": DOC}, "name")
        other_row = frappe.db.get_value(
            "Notification Log",
            {"for_user": STAFF_TEST, "document_name": DOC}, "name")
        frappe.set_user(STAFF_REAL)
        items = A.notif_list(limit=50)["items"]
        only_own = all(i["for_user"] == STAFF_REAL for i in items)
        A.notif_mark_read(other_row)  # cross-staff attempt: silent no-op
        other_still = frappe.db.get_value("Notification Log", other_row, "read")
        A.notif_mark_read(own_row)
        own_now = frappe.db.get_value("Notification Log", own_row, "read")
        guest_blocked = False
        try:
            frappe.set_user("Guest")
            A.notif_poll()
        except Exception:
            guest_blocked = True
        cust_blocked = False
        try:
            frappe.set_user(CUST_B)
            A.notif_poll()
        except Exception:
            cust_blocked = True
        show("T1-06", "own-only list; cross mark blocked; own works; guest/customer 403",
             only_own and int(other_still or 0) == 0 and int(own_now or 0) == 1
             and guest_blocked and cust_blocked,
             f"own_only={only_own} other={other_still} own={own_now}")
    except Exception as e:
        show("T1-06", "isolation", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    # ---------- T1-10: extensibility without UI change ----------
    # Mirrors the §8 recipe: dummy type registered in the bell allowlist
    # (like a future BZ Broadcast in _BZ_NOTIFICATION_TYPES) + registry.
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
        n_staff = len(N.recipients_for(N.EVENTS["order_new"], DOC, actor=CUST_A))
        r = N.notify("test_dummy", reference_doctype="Sales Order",
                     reference_name=DOC, context={"order": DOC}, actor=CUST_A)
        frappe.set_user(STAFF_TEST)
        items = A.notif_list(limit=10)["items"]
        seen = [i for i in items if i["document_name"] == DOC
                and i["type"] == "BZ Test Dummy"]
        show("T1-10", "dummy event reaches bell with zero UI change",
             r == n_staff and len(seen) == 1, f"r={r} seen={len(seen)}")
    except Exception as e:
        show("T1-10", "extensibility", False, repr(e)[:120])
    finally:
        N.EVENTS.pop("test_dummy", None)
        if "BZ Test Dummy" in C._BZ_TYPES:
            C._BZ_TYPES.remove("BZ Test Dummy")
        frappe.db.rollback()

    # ---------- T1-23: read rows never reappear (mark-all regression) ----------
    try:
        frappe.set_user(CUST_A)
        N.notify("order_new", reference_doctype="Sales Order",
                 reference_name=DOC, context={"order": DOC}, actor=CUST_A)
        frappe.set_user(STAFF_REAL)
        before = A.notif_list(limit=10)["items"]
        seen_before = [i for i in before if i["document_name"] == DOC]
        A.notif_mark_all_read()
        poll_after = A.notif_poll()["unread_count"]
        reopen1 = A.notif_list(limit=10)["items"]  # panel reopen
        reopen2 = A.notif_list(limit=10)["items"]  # page reload equivalent
        leaked = [i for i in reopen1 + reopen2 if i["document_name"] == DOC]
        db_unread = frappe.db.count(
            "Notification Log",
            {"for_user": STAFF_REAL, "document_name": DOC, "read": 0})
        show("T1-23", "mark-all empties list permanently (no read rows listed)",
             len(seen_before) >= 1 and poll_after == 0
             and reopen1 == [] and reopen2 == [] and leaked == []
             and db_unread == 0,
             f"before={len(seen_before)} poll={poll_after}")
    except Exception as e:
        show("T1-23", "mark-all regression", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    # ---------- T1-24: Desk types never enter our bell (D10) ----------
    try:
        frappe.set_user(STAFF_REAL)
        d = frappe.new_doc("Notification Log")
        d.type = "Alert"  # Desk type, never emails (skip list) -> side-effect free
        d.title = "ntf t24 desk"
        d.subject = "ntf t24 desk"
        d.for_user = STAFF_REAL
        d.from_user = CUST_A
        d.document_name = "NTF-T24"
        d.insert(ignore_permissions=True)
        poll_n = A.notif_poll()["unread_count"]
        items = A.notif_list(limit=50)["items"]
        leaked = [i for i in items if i["type"] not in list(_BZ_NOTIFICATION_TYPES)]
        bz_unread = frappe.db.count(
            "Notification Log", {"for_user": STAFF_REAL, "read": 0,
                                 "type": ("in", list(_BZ_NOTIFICATION_TYPES))})
        db_total = frappe.db.count(
            "Notification Log", {"for_user": STAFF_REAL, "read": 0})
        show("T1-24", "Desk row counted in DB but invisible in bell",
             poll_n == bz_unread and leaked == [] and db_total == bz_unread + 1,
             f"poll={poll_n} bz={bz_unread} leaked={len(leaked)}")
    except Exception as e:
        show("T1-24", "desk-type filter", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    # ---------- T1-11: patch idempotent ----------
    try:
        from biozone_web.patches.v0_0.bz_notification_types_v2 import execute
        execute()
        execute()
        ntypes = frappe.db.count("Notification Type", {"name": ("like", "BZ %")})
        logged = frappe.db.exists(
            "Patch Log",
            {"patch": "biozone_web.patches.v0_0.bz_notification_types_v2"})
        show("T1-11", "patch twice -> types stable, patch logged",
             ntypes >= 4 and bool(logged), f"bz_types={ntypes}")
    except Exception as e:
        show("T1-11", "patch idempotent", False, repr(e)[:120])
    finally:
        frappe.db.rollback()

    print(f"\nMATRIX | pass={PASS_COUNT} fail={FAIL_COUNT}")

    # ---------- teardown: remove Error Log rows created by this run ----------
    try:
        _new_rows = [
            r.name for r in frappe.db.get_all(
                "Error Log", {"method": ("in", OWN_ERROR_TITLES)}, ["name"])
            if r.name not in _err_own_before]
        for _n in _new_rows:
            frappe.db.delete("Error Log", _n)
        if _new_rows:
            frappe.db.commit()
        _delta = frappe.db.count("Error Log") - _err_before
        print(f"CLEANUP | error-log delta={_delta} (removed {len(_new_rows)})")
        if _delta != 0:
            FAIL_COUNT += 1
    except Exception as e:
        print(f"CLEANUP | failed: {e!r}")
        FAIL_COUNT += 1

    print(f"FINAL | pass={PASS_COUNT} fail={FAIL_COUNT}")
    frappe.destroy()
    sys.exit(1 if FAIL_COUNT else 0)


if __name__ == "__main__":
    main()
