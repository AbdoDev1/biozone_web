#!/usr/bin/env python3
"""T2-13/T2-15 (permanent guard): eight store pages as a real customer.

Per page: zero console errors and zero automatic ``notif_*`` requests
(no customer polling). Plus: customer A (one unread row) sees badge 1,
customer B sees none, and guests get no bell element at all (T2-15:
no counter leaks across users).

Dev-only: refuses unless the target is development.localhost with a
live server. Creates two temporary customers (welcome mail off) and one
display row for A, and deletes users, sessions, settings, the row and
its own error rows in ``finally``. Sequential. Exit 0/1/2.
"""
import base64
import http.client
import http.cookiejar
import json
import re
import secrets
import subprocess
import sys
import urllib.parse

SITE = "development.localhost"
HOST = "127.0.0.1"
PORT = 8000
STORE_HOST = "biozone.pro"
USER_A = "ntf_t2a@example.com"
USER_B = "ntf_t2b@example.com"

PAGES = ["/", "/catalog", "/cart", "/biozone-home", "/account",
         "/account/orders",
         "/order-confirmed?name=NTF-NOPE-ORDER",
         "/account/orders/NTF-NOPE-ORDER"]

SETUP_PY = r"""
import frappe
frappe.init(site='development.localhost', sites_path='/workspace/development/frappe-bench/sites')
frappe.connect()
import sys
pa, pb = sys.argv[1], sys.argv[2]
pw_q = {'method': 'Unable to send new password notification'}
before = set(r.name for r in frappe.db.get_all('Error Log', pw_q, ['name']))
for name in ('ntf_t2a@example.com', 'ntf_t2b@example.com'):
    if frappe.db.exists('User', name):
        frappe.delete_doc('User', name, force=True)
    u = frappe.new_doc('User')
    u.email = name
    u.first_name = 't2'
    u.user_type = 'Website User'
    u.enabled = 1
    u.send_welcome_email = 0
    u.new_password = pa if 't2a' in name else pb
    u.insert(ignore_permissions=True)
d = frappe.new_doc('Notification Log')
d.type = 'BZ Delivered'
d.title = 'T2-15 display row'
d.subject = 'T2-15 display row'
d.document_type = 'Sales Order'
d.document_name = 'SAL-ORD-2026-00061'
d.for_user = 'ntf_t2a@example.com'
d.from_user = 'abdulrahmanali.h.t@gmail.com'
d.link = '/account/orders/SAL-ORD-2026-00061'
d.insert(ignore_permissions=True)
frappe.db.commit()
after = set(r.name for r in frappe.db.get_all('Error Log', pw_q, ['name']))
print('CREATED_PW:' + repr(sorted(after - before)))
frappe.destroy()
"""

CLEANUP_PY = r"""
import frappe
frappe.init(site='development.localhost', sites_path='/workspace/development/frappe-bench/sites')
frappe.connect()
import base64 as _b64, json, sys
created = set(json.loads(_b64.b64decode(sys.argv[1]).decode()))
start = set(json.loads(_b64.b64decode(sys.argv[2]).decode()))
for name in ('ntf_t2a@example.com', 'ntf_t2b@example.com'):
    if frappe.db.exists('User', name):
        frappe.delete_doc('User', name, force=True)
    frappe.db.delete('Notification Settings', {'name': name})
    frappe.db.delete('Sessions', {'user': name})
for n in sorted(created):
    if frappe.db.exists('Error Log', n):
        frappe.db.delete('Error Log', n)
rows = frappe.db.get_all('Notification Log',
                         {'for_user': 'ntf_t2a@example.com',
                          'title': 'T2-15 display row'}, ['name'])
for r in rows:
    frappe.db.delete('Notification Log', r.name)
frappe.db.commit()
current = set(r.name for r in frappe.db.get_all('Error Log', {}, ['name']))
leak = sorted(current - start - created)
print('cleaned; LEAK=%d %s' % (len(leak), leak[:3]))
frappe.destroy()
"""


def bench_python(code, *args):
    cmd = ["docker", "exec", "-i", "devcontainer-frappe-1", "bash", "-c",
           "cd /workspace/development/frappe-bench/sites && "
           "/workspace/development/frappe-bench/env/bin/python - %s" % " ".join(args)]
    r = subprocess.run(cmd, input=code.encode(), capture_output=True, timeout=120)
    return r.returncode, r.stdout.decode(), r.stderr.decode()[-300:]


def login(user, pwd):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=30)
    conn.request("POST", "/api/method/login",
                 body=json.dumps({"usr": user, "pwd": pwd}).encode(),
                 headers={"Host": STORE_HOST, "Content-Type": "application/json"})
    resp = conn.getresponse()
    body = resp.read().decode()[:150]
    if resp.status != 200:
        return None
    return resp.getheader("Set-Cookie", "")


def main():
    if SITE != "development.localhost":
        print("REFUSED: not a dev target")
        return 2
    try:
        conn = http.client.HTTPConnection(HOST, PORT, timeout=8)
        conn.request("GET", "/api/method/frappe.ping", headers={"Host": STORE_HOST})
        if conn.getresponse().status != 200:
            raise RuntimeError("ping")
    except Exception:
        print("REFUSED: bench serve unreachable")
        return 2

    def gen_pwd():
        while True:
            p = secrets.token_hex(12)
            if "abc" not in p and "6543" not in p and not re.search(r"(.)\1\1", p):
                return p

    pa, pb = gen_pwd(), gen_pwd()
    fails = 0
    created_pw, err_start = set(), None
    try:
        rc, out, err = bench_python(
            "import frappe\nfrappe.init(site='development.localhost',"
            " sites_path='/workspace/development/frappe-bench/sites')\n"
            "frappe.connect()\n"
            "print('ERR_ALL:' + repr([r.name for r in frappe.db.get_all("
            "'Error Log', {}, ['name'])]))\n"
            "frappe.destroy()\n")
        for _line in out.strip().splitlines():
            if _line.startswith("ERR_ALL:"):
                try:
                    err_start = set(eval(_line.split(":", 1)[1]))
                except Exception:
                    err_start = None
                break
        rc, out, err = bench_python(SETUP_PY, pa, pb)
        for _line in out.strip().splitlines():
            if _line.startswith("CREATED_PW:"):
                try:
                    created_pw = set(eval(_line.split(":", 1)[1]))
                except Exception:
                    created_pw = None
                break
        if rc != 0 or created_pw is None or err_start is None:
            print("FAIL | setup", out[-150:], err[-150:])
            return 1
        cookie_a = login(USER_A, pa)
        cookie_b = login(USER_B, pb)
        if not cookie_a or not cookie_b:
            print("FAIL | login")
            return 1

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(args=[
                "--host-resolver-rules=MAP biozone.pro 127.0.0.1"])
            # --- 8 pages as A: clean console, zero auto notif_* ---
            ctx = b.new_context(viewport={"width": 1366, "height": 900})
            ctx.add_cookies([{"name": "sid",
                              "value": _sid(cookie_a),
                              "domain": STORE_HOST, "path": "/"}])
            for route in PAGES:
                errs, polls = [], []
                pg = ctx.new_page()
                pg.on("console", lambda m, e=errs:
                      e.append(m.text[:120]) if m.type == "error" else None)
                pg.on("response", lambda r, pl=polls:
                      pl.append(r.status) if "notif_" in r.url else None)
                try:
                    pg.goto(f"http://{STORE_HOST}:{PORT}{route}",
                            wait_until="domcontentloaded", timeout=60000)
                    pg.wait_for_timeout(3500)
                    real_errs = [e for e in errs if "favicon" not in e.lower()]
                    if "NOPE" in route:
                        # Soft-404 fixture: the shell must render (header
                        # intact) while the page's own data fetch 404s.
                        data_404 = [e for e in real_errs if "404" in e]
                        other = [e for e in real_errs if "404" not in e]
                        shell_ok = pg.evaluate(
                            "() => !!document.getElementById('bz-bell-btn') "
                            "|| !!document.querySelector('header')")
                        ok = not polls and not other and shell_ok
                        extra = f"soft404 shell={shell_ok}"
                    else:
                        ok = not real_errs and not polls
                        extra = ""
                    print(f"{'PASS' if ok else 'FAIL'} | A {route} | "
                          f"auto-notif={len(polls)} console-errors={len(real_errs)} "
                          f"{extra}")
                    for e in real_errs[:2]:
                        print("   !! " + e)
                    fails += 0 if ok else 1
                except Exception as ex:
                    print(f"FAIL | A {route} | {str(ex)[:100]}")
                    fails += 1
                finally:
                    pg.close()
            # --- A badge shows 1 ---
            pg = ctx.new_page()
            pg.goto(f"http://{STORE_HOST}:{PORT}/account/orders",
                    wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(2500)
            badge = pg.evaluate("""() => {
              const b = document.getElementById('bz-bell-badge');
              return b ? (b.classList.contains('hidden') ? 'hidden' : b.textContent) : 'NO-BELL';
            }""")
            ok = badge == "1"
            print(f"{'PASS' if ok else 'FAIL'} | A badge | {badge}")
            fails += 0 if ok else 1
            pg.close()
            ctx.close()
            # --- B badge hidden; guest: no bell element ---
            ctx2 = b.new_context(viewport={"width": 1366, "height": 900})
            ctx2.add_cookies([{"name": "sid",
                               "value": _sid(cookie_b),
                               "domain": STORE_HOST, "path": "/"}])
            pg = ctx2.new_page()
            pg.goto(f"http://{STORE_HOST}:{PORT}/account/orders",
                    wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(2500)
            badge_b = pg.evaluate("""() => {
              const x = document.getElementById('bz-bell-badge');
              return x ? (x.classList.contains('hidden') ? 'hidden' : x.textContent) : 'NO-BELL';
            }""")
            ok = badge_b == "hidden"
            print(f"{'PASS' if ok else 'FAIL'} | B badge hidden | {badge_b}")
            fails += 0 if ok else 1
            pg.close()
            ctx2.close()
            ctx3 = b.new_context(viewport={"width": 1366, "height": 900})
            pg = ctx3.new_page()
            pg.goto(f"http://{STORE_HOST}:{PORT}/catalog",
                    wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(2000)
            has_bell = pg.evaluate("() => !!document.getElementById('bz-bell-btn')")
            ok = not has_bell
            print(f"{'PASS' if ok else 'FAIL'} | guest has no bell | {has_bell}")
            fails += 0 if ok else 1
            pg.close()
            ctx3.close()
            b.close()
    finally:
        rc, out, err = bench_python(CLEANUP_PY, _b64(created_pw), _b64(err_start))
        print("cleanup:", out.strip().splitlines()[-1] if out.strip() else err[-100:])
    print(f"T2-13 | checks fail={fails}")
    return 1 if fails else 0


def _sid(set_cookie):
    for part in set_cookie.split(";"):
        if part.strip().startswith("sid="):
            return part.strip()[4:]
    return ""


def _b64(items):
    import base64
    return base64.b64encode(json.dumps(sorted(items)).encode()).decode()


if __name__ == "__main__":
    sys.exit(main())
