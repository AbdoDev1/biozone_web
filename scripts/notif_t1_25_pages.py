#!/usr/bin/env python3
"""T1-25 (permanent guard): nine staff pages load with a real session.

Each page: first bell pulse is HTTP 200 and the console has no errors.
Would have caught the cold-load tokenless-400 bug automatically.

Dev-only: REFUSES unless the target site is development.localhost.
Creates a temporary staff user and deletes it — with its sessions,
its Notification Settings row and any password-email error rows it
caused — in ``finally``. Runs sequentially (one page at a time).

Run (host):  python3 development/frappe-bench/apps/biozone_web/scripts/notif_t1_25_pages.py
Needs: bench serve on 127.0.0.1:8000 with current code, docker access.
Exit: 0 = 9/9 pass, 1 = any fail, 2 = refused/unreachable.
"""
import base64
import http.client
import json
import re
import secrets
import subprocess
import sys
import urllib.parse

SITE = "development.localhost"
HOST = "127.0.0.1"
PORT = 8000
STAFF_HOST = "staff.biozone.pro"
USER = "ntf_t25@example.com"
ROLE = "Biozone Sales Staff"

PAGES = ["dashboard", "items", "stock", "orders", "pricing", "customers",
         "customer-detail?customer_name=abdo4",
         "order-delivery?order=SAL-ORD-2026-00059&invoice=ACC-SINV-2026-00015",
         "order-prep?order=SAL-ORD-2026-00061"]

CREATE_USER_PY = r"""
import frappe
frappe.init(site='development.localhost', sites_path='/workspace/development/frappe-bench/sites')
frappe.connect()
import sys
pwd = sys.argv[1]
name = 'ntf_t25@example.com'
pw_q = {'method': 'Unable to send new password notification'}
before = set(r.name for r in frappe.db.get_all('Error Log', pw_q, ['name']))
if frappe.db.exists('User', name):
    frappe.delete_doc('User', name, force=True)
u = frappe.new_doc('User')
u.email = name
u.first_name = 'ntf25'
u.user_type = 'System User'
u.enabled = 1
u.send_welcome_email = 0
u.new_password = pwd
u.insert(ignore_permissions=True)
u = frappe.get_doc('User', name)
u.append('roles', {'role': 'Biozone Sales Staff'})
u.save(ignore_permissions=True)
frappe.db.commit()
after = set(r.name for r in frappe.db.get_all('Error Log', pw_q, ['name']))
print('created:', frappe.db.get_value('User', name, 'user_type'))
print('CREATED_PW:' + repr(sorted(after - before)))
frappe.destroy()
"""

CLEANUP_USER_PY = r"""
import frappe
frappe.init(site='development.localhost', sites_path='/workspace/development/frappe-bench/sites')
frappe.connect()
import json, sys
name = 'ntf_t25@example.com'
created_arg = sys.argv[1]
start_arg = sys.argv[2]
if created_arg == "SKIP" or start_arg == "SKIP":
    created, start = None, None
else:
    import base64 as _b64
    created = set(json.loads(_b64.b64decode(created_arg).decode()))
    start = set(json.loads(_b64.b64decode(start_arg).decode()))
if frappe.db.exists('User', name):
    frappe.delete_doc('User', name, force=True)
frappe.db.delete('Notification Settings', {'name': name})
frappe.db.delete('Sessions', {'user': name})
leak = "n/a"
if created is not None:
    for n in sorted(created):
        if frappe.db.exists('Error Log', n):
            frappe.db.delete('Error Log', n)
    frappe.db.commit()
    current = set(r.name for r in frappe.db.get_all('Error Log', {}, ['name']))
    leak = len(current - start - created)
    print('cleaned: user+sessions+settings+%d recorded rows; LEAK=%d'
          % (len(created), leak))
else:
    frappe.db.commit()
    print('cleaned: user+sessions+settings (error rows SKIPPED)')
frappe.destroy()
"""


def bench_python(code, *args):
    cmd = ["docker", "exec", "-i", "devcontainer-frappe-1", "bash", "-c",
           "cd /workspace/development/frappe-bench/sites && "
           "/workspace/development/frappe-bench/env/bin/python - %s" % " ".join(args)]
    r = subprocess.run(cmd, input=code.encode(), capture_output=True, timeout=120)
    return r.returncode, r.stdout.decode(), r.stderr.decode()[-300:]


def api(path, *, cookies=None, body=None, headers=None):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=30)
    h = {"Host": STAFF_HOST}
    if headers:
        h.update(headers)
    data = json.dumps(body or {}).encode()
    if body is not None or path.endswith("login"):
        h["Content-Type"] = "application/json"
    conn.request("POST" if body is not None or path.endswith("login") else "GET",
                 path, body=data if body is not None or path.endswith("login") else None,
                 headers=h if cookies is None else {**h, "Cookie": cookies})
    resp = conn.getresponse()
    payload = resp.read().decode()[:200]
    set_cookie = resp.getheader("Set-Cookie", "")
    return resp.status, payload, set_cookie


def main():
    if SITE != "development.localhost":
        print("REFUSED: not a dev target")
        return 2
    try:
        conn = http.client.HTTPConnection(HOST, PORT, timeout=8)
        conn.request("GET", "/api/method/frappe.ping",
                     headers={"Host": STAFF_HOST})
        if conn.getresponse().status != 200:
            raise RuntimeError("ping")
    except Exception:
        print("REFUSED: bench serve unreachable on 127.0.0.1:8000")
        return 2

    pwd = secrets.token_hex(12)
    while ("abc" in pwd or "6543" in pwd
           or re.search(r"(.)\1\1", pwd)):
        pwd = secrets.token_hex(12)  # Frappe rejects guessable sequences
    fails = 0
    created_pw, err_start = None, None
    try:
        rc, out, err = bench_python("import frappe\n"
            "frappe.init(site='development.localhost',"
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
        rc, out, err = bench_python(CREATE_USER_PY, pwd)
        for _line in out.strip().splitlines():
            if _line.startswith("CREATED_PW:"):
                try:
                    created_pw = set(eval(_line.split(":", 1)[1]))
                except Exception:
                    created_pw = None
                break
        if rc != 0 or "System User" not in out or created_pw is None:
            print("FAIL | user creation", out[-100:], err[-100:])
            return 1

        st, payload, set_cookie = api("/api/method/login",
                                      body={"usr": USER, "pwd": pwd})
        sid = ""
        for part in set_cookie.split(";"):
            if part.strip().startswith("sid="):
                sid = part.strip()
                break
        if st != 200 or not sid:
            print("FAIL | login", st, payload[:80])
            return 1
        cookies = sid

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(args=[
                "--host-resolver-rules=MAP staff.biozone.pro 127.0.0.1"])
            ctx = b.new_context(viewport={"width": 1440, "height": 900})
            ctx.add_cookies([{"name": "sid", "value": sid.split("=", 1)[1],
                              "domain": STAFF_HOST, "path": "/"}])
            for slug in PAGES:
                errs, polls = [], []
                pg = ctx.new_page()
                pg.on("console", lambda m, e=errs:
                      e.append(m.text[:100]) if m.type == "error" else None)
                pg.on("response", lambda r, pl=polls:
                      pl.append(r.status) if "notif_poll" in r.url else None)
                label = slug.split("?")[0]
                try:
                    pg.goto(f"http://{STAFF_HOST}:{PORT}/staff/{slug}",
                            wait_until="domcontentloaded", timeout=60000)
                    pg.wait_for_timeout(4000)
                    first = polls[0] if polls else "NO-PULSE"
                    ok = first == 200 and not errs
                    print(f"{'PASS' if ok else 'FAIL'} | {label} | "
                          f"first-pulse={first} polls={len(polls)} "
                          f"console-errors={len(errs)}")
                    for e in errs[:2]:
                        print("   !! " + e)
                    fails += 0 if ok else 1
                except Exception as ex:
                    print(f"FAIL | {label} | {str(ex)[:100]}")
                    fails += 1
                finally:
                    pg.close()
            b.close()
    finally:
        c_arg = ("SKIP" if created_pw is None
                 else base64.b64encode(
                     json.dumps(sorted(created_pw)).encode()).decode())
        s_arg = ("SKIP" if err_start is None
                 else base64.b64encode(
                     json.dumps(sorted(err_start)).encode()).decode())
        rc, out, err = bench_python(CLEANUP_USER_PY, c_arg, s_arg)
        print("cleanup:", out.strip().splitlines()[-1] if out.strip() else err[-100:])
    print(f"T1-25 | pages={len(PAGES)} fails={fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
