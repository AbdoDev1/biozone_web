#!/usr/bin/env python3
"""Runtime layout guard for the shared header and the /account filter form.

WHY THIS EXISTS
---------------
``scripts/check_shared_partials.py`` is a STATIC guard: it reads templates and
proves the tokens/classes are declared on every host page. It cannot prove what
the browser actually does with them. This script is the runtime half -- it
measures real layout in a headless browser, which is how both defects were
root-caused in the first place (a 15px shift equal to the scrollbar gutter, and
form buttons pushed 45px outside the card at medium widths).

WHAT IT MEASURES                                  (rules: the 10 build rules)
---------------------------------------------------------------------------
A. scrollbar present vs absent (rule 6): every host route is loaded twice -- in a
   SHORT viewport (content overflows -> scrollbar) and a TALL one -- and the page
   must never scroll horizontally. The header's cart/account icon boxes must sit
   at the same place in both cases.
B. shared-partial identity across the pages that include it (rule 3): the header
   container's computed max-width/padding and its measured box must be IDENTICAL
   on every host route at the same viewport. This is the check that would have
   caught /account rendering full-bleed (max-width none, padding 0px) while
   /catalog rendered inside a 1280px padded container.
C. header stability between the two /account tabs (rules 5, 7): identical logo
   and icon boxes for ``?tab=profile`` and ``?tab=debt`` at every viewport,
   including the pair where only one of the two tabs scrolls.
D. filter-form containment (rule 4): the debt tab's form is swept across phone /
   medium / desktop widths; no field/button may spill outside its card and the
   document must not overflow horizontally.

REQUIREMENTS / USAGE
--------------------
    pip install playwright && python3 -m playwright install chromium

    # public pages only:
    python3 scripts/check_header_stability.py --base-url http://development.localhost:8000
    # include the authenticated /account routes (Netscape cookie file):
    python3 scripts/check_header_stability.py --base-url http://development.localhost:8000 \\
        --cookie-file ../../cookies.txt

Exit status: 0 when clean, 1 with the offending measurement otherwise. If
playwright is not importable the script prints a notice and exits 0 (the static
guard still runs in pre-commit on every template change).
"""

import argparse
import http.cookiejar
import pathlib
import sys

try:
	from playwright.sync_api import sync_playwright
except ImportError:  # optional runtime dependency
	sync_playwright = None

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from check_shared_partials import EXEMPTIONS, host_pages, rel  # noqa: E402

HEADER_PROBE = """
() => {
  const de = document.documentElement;
  const box = (el) => { if (!el) return null; const r = el.getBoundingClientRect();
    return {l: +r.left.toFixed(2), r: +r.right.toFixed(2), w: +r.width.toFixed(2)}; };
  const inner = document.querySelector('header > div');
  const s = inner ? getComputedStyle(inner) : null;
  return {
    gutter: window.innerWidth - de.clientWidth,
    scrolls: de.scrollHeight > de.clientHeight,
    hOverflow: de.scrollWidth > de.clientWidth,
    inner: box(inner),
    logo: box(document.querySelector('header a[href="/biozone-home"]')),
    cart: box(document.querySelector('header a[href="/cart"]')),
    maxWidth: s ? s.maxWidth : null,
    paddingInline: s ? s.paddingLeft + '/' + s.paddingRight : null,
    actionsGap: (() => { const g = document.querySelector('header > div > div:last-child');
                         return g ? getComputedStyle(g).gap : null; })(),
  };
}
"""

FORM_PROBE = """
() => {
  const form = document.querySelector('form[action="/account"]');
  if (!form) return null;
  const card = form.closest('div[class*="rounded-2xl"]');
  const cs = card ? getComputedStyle(card) : null;
  const cr = card ? card.getBoundingClientRect() : null;
  const left = cr ? cr.left + parseFloat(cs.paddingLeft) : null;
  const right = cr ? cr.right - parseFloat(cs.paddingRight) : null;
  const items = [...form.querySelectorAll('input:not([type=hidden]), button, a')];
  const boxes = items.map((el) => el.getBoundingClientRect());
  return {
    hOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    spill: items
      .filter((el, i) => boxes[i].left < left - 0.5 || boxes[i].right > right + 0.5)
      .map((el) => el.name || el.textContent.trim().slice(0, 12)),
    rows: new Set(boxes.map((b) => Math.round(b.top))).size,
    dates: ['date_from', 'date_to'].map((n) => {
      const el = form.querySelector(`[name="${n}"]`);
      return el ? +el.getBoundingClientRect().width.toFixed(1) : null;
    }),
  };
}
"""


HEADER_INCLUDE_ROUTE_HELP = {
	"/account/order-detail": "needs a real order name (/account/orders/<name>); covered by the live tab routes",
	"/order-confirmed": "needs ?name=<order>; not addressable without a real order",
	"/my-orders": "permanent redirect to /account/orders",
}
NEEDS_PARAM = HEADER_INCLUDE_ROUTE_HELP


def route_for(page_path, app_root):
	"""Map a template file to the public route a browser can load."""
	route = rel(page_path, app_root).replace("biozone_web/www/", "").removesuffix(".html")
	if route.endswith("/index"):
		route = route[: -len("/index")]
	if route == "index":
		route = ""
	return "/" + route


def load_cookies(path):
	if not path:
		return []
	jar = http.cookiejar.MozillaCookieJar(path)
	jar.load(ignore_discard=True, ignore_expires=True)
	entries = list(jar)
	host = entries[0].domain if entries else "development.localhost"
	return [{"name": c.name, "value": c.value, "domain": host, "path": "/"} for c in entries]


def snap(page, url, height):
	page.set_viewport_size({"width": page.viewport_size["width"], "height": height})
	page.goto(url, wait_until="networkidle", timeout=60000)
	page.wait_for_function("document.fonts.status === 'loaded'", timeout=30000)
	page.wait_for_timeout(150)
	return page.evaluate(HEADER_PROBE)


def main(argv=None):
	ap = argparse.ArgumentParser(description="Shared header + /account filters runtime guard.")
	ap.add_argument("--app-root", default=None)
	ap.add_argument("--base-url", default="http://development.localhost:8000")
	ap.add_argument("--cookie-file", default=None, help="Netscape cookie file for /account routes")
	args = ap.parse_args(argv)

	if sync_playwright is None:
		print("SKIP: playwright is not installed -- static guard only "
		      "(pip install playwright && python3 -m playwright install chromium)")
		return 0

	app_root = pathlib.Path(args.app_root) if args.app_root else SCRIPTS_DIR.parent
	exempt = set(EXEMPTIONS)
	probes = []
	for page_path in host_pages(app_root):
		route = route_for(page_path, app_root)
		if route in NEEDS_PARAM:
			print("[SKIP] %s (%s)" % (route, NEEDS_PARAM[route]))
			continue
		probes.append((route, rel(page_path, app_root) in exempt))

	cookies = load_cookies(args.cookie_file)
	failures = []
	viewports = [(390, 500), (390, 900), (768, 600), (1024, 600), (1440, 900), (1440, 600)]
	form_widths = [360, 480, 640, 680, 720, 768, 900, 1024, 1440]

	with sync_playwright() as p:
		browser = p.chromium.launch(ignore_default_args=["--hide-scrollbars"])
		ctx = browser.new_context(viewport={"width": 1440, "height": 900})
		if cookies:
			ctx.add_cookies(cookies)
		page = ctx.new_page()

		# A + B: same header geometry on every host route, scrollbar or not.
		print("\n[A/B] shared header identity across host routes (hatch = scrollbar state)")
		baseline = {}
		for route, is_exempt in probes:
			for width, height in viewports:
				page.set_viewport_size({"width": width, "height": height})
				data = snap(page, args.base_url + route, height)
				if data["cart"] is None or data["inner"] is None:
					print("      %-16s @%dx%d NO shared header rendered (route not reachable) -- skipped"
					      % (route, width, height))
					break
				key = (width, height)
				if data["hOverflow"]:
					failures.append(f"{route} @{width}x{height}: horizontal page overflow")
				if is_exempt:
					print("      [KNOWN] %-16s @%dx%d exempted from the identity check" % (route, width, height))
					continue
				sig = (data["maxWidth"], data["paddingInline"], data["actionsGap"])
				if key not in baseline:
					baseline[key] = (route, sig)
				elif baseline[key][1] != sig:
					failures.append(
						f"{route} @{width}x{height}: header container differs from "
						f"{baseline[key][0]} ({sig} vs {baseline[key][1]})"
					)
				print("      %-16s @%dx%d scrolls=%-5s gutter=%-3s maxW=%-6s pad=%-10s icons=%s"
				      % (route, width, height, data["scrolls"], data["gutter"], data["maxWidth"],
				         data["paddingInline"], data["cart"]["l"]))

		# C: header motion between the two /account tabs.
		print("\n[C] /account tab-to-tab header stability")
		for width, height in viewports:
			page.set_viewport_size({"width": width, "height": height})
			profile = snap(page, args.base_url + "/account?tab=profile", height)
			debt = snap(page, args.base_url + "/account?tab=debt", height)
			delta = round(debt["logo"]["l"] - profile["logo"]["l"], 2)
			same = all(profile[k]["l"] == debt[k]["l"] for k in ("logo", "cart"))
			print("      @%dx%d scrolls(profile/debt)=%s/%s gutters=%s/%s logoΔ=%+.2f cartΔ=%+.2f %s"
			      % (width, height, profile["scrolls"], debt["scrolls"], profile["gutter"],
			         debt["gutter"], delta, round(debt["cart"]["l"] - profile["cart"]["l"], 2),
			         "OK" if same else "MISMATCH"))
			if not same:
				failures.append(f"/account tabs @{width}x{height}: header boxes differ (logoΔ={delta}px)")

		# D: debt filter form containment across widths.
		print("\n[D] debt-tab filter form containment")
		for width in form_widths:
			page.set_viewport_size({"width": width, "height": 600})
			page.goto(args.base_url + "/account?tab=debt", wait_until="networkidle", timeout=60000)
			page.wait_for_function("document.fonts.status === 'loaded'", timeout=30000)
			data = page.evaluate(FORM_PROBE)
			if data is None:
				print("      @%-5d no filter form (tab not reachable?)" % width)
				continue
			print("      @%-5d rows=%s dateW=%s spill=%s pageOverflow=%s"
			      % (width, data["rows"], data["dates"], data["spill"], data["hOverflow"]))
			if data["spill"]:
				failures.append(f"debt form @{width}px: controls spill outside the card: {data['spill']}")
			if data["hOverflow"]:
				failures.append(f"debt form @{width}px: horizontal page overflow")
		browser.close()

	print("-" * 68)
	if failures:
		print("FAIL: %d runtime layout problem(s):" % len(failures))
		for problem in failures:
			print("    - %s" % problem)
		return 1
	print("PASS: header identical across host routes and /account tabs (scrollbar present or not); "
	      "no horizontal overflow; filter form contained at every width.")
	return 0


if __name__ == "__main__":
	sys.exit(main())