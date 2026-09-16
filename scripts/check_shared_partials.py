#!/usr/bin/env python3
"""Static guard for the shared customer-facing partials (header / footer).

WHY THIS EXISTS
---------------
Two measured, root-caused defects made this check necessary:

1. The shared header moved 15px when switching between ``/account?tab=profile``
   and ``/account?tab=debt``. Root cause (measured live in a real browser, not
   assumed): the header container is ``w-full ... mx-auto``, so it follows the
   viewport edge; page content height differs per tab, so one tab had a vertical
   scrollbar and the other did not, and the layout shrank by exactly the
   scrollbar gutter (15px). The same defect existed between ``/catalog``
   (scrollbar) and ``/cart`` (no scrollbar) -> 7.5px, because the cause is
   shared, not page-specific. It was amplified in /account to a full 15px
   because ``px-md`` / ``py-sm`` / ``max-w-container-max`` / ``gap-sm`` were
   UNDEFINED there (no ``spacing`` block in those pages' tailwind config), so
   the header rendered full-bleed with zero padding instead of 16px padding
   inside a 1280px centred container like every other host page.

2. The debt-tab filter form pushed its buttons 45px outside the card (and off
   the viewport: ``document.scrollWidth`` 670 for a 640px viewport) at medium
   widths, because flex items keep their intrinsic min-content sizes (native
   ``input[type=date]`` = 143px, button group = 152px) while only the search
   field was ``flex-1``.

So: a shared partial may only use classes/tokens that exist in EVERY page
hosting it, the page-width stabilisation rule must live in the shared partial
itself, and multi-field forms must declare their layout per breakpoint. This
script enforces that, so the next regression is caught in seconds.

WHAT IT CHECKS              (rule numbers refer to the plan's 10 build rules)
---------------------------------------------------------------------------
R1  every host page defines every named token the shared partials use   (rule 1)
R2  no customer-facing page carries a hand-copied header/footer         (rule 2)
R3  the scrollbar-stability rule lives in the shared partial only       (rule 6)
R4  every >=3-field GET form declares base + two breakpoints            (rules 4, 10)
R5  the header's explicit geometry markers are pinned                   (rule 7)
R6  no negative margins/offsets used as compensation in /account        (rule 5)
R7  documented exemptions stay explicit (or the check fails)         (rules 8, 9)

Limitation, stated plainly: this is a STATIC check over template sources. Rules
3, 5 and 6 also have a runtime counterpart that measures real layout --
``scripts/check_header_stability.py`` (headless browser, optional dependency).

USAGE
-----
    # from the app root (biozone_web/):
    python3 scripts/check_shared_partials.py
    # against another checkout:
    python3 scripts/check_shared_partials.py --app-root /path/to/biozone_web

Exit status: 0 when clean, 1 with file:line details otherwise.
Pre-commit wiring: see the `check-shared-partials` local hook in
.pre-commit-config.yaml.
"""

import argparse
import pathlib
import re
import sys
HEADER_INCLUDE = '{% include "biozone_web/templates/includes/site_header.html" %}'
HEADER_PARTIAL = "biozone_web/templates/includes/site_header.html"
FOOTER_PARTIAL = "biozone_web/templates/includes/site_footer.html"

# Named tokens the shared partials use: <prefix>-<key>. Every key must exist in
# the `spacing` block of any page that includes one of the partials. Verified
# empirically (probe against the Tailwind CDN + runtime check): the named scale
# keys resolve through spacing[sm|md|lg|xl], and `max-w-container-max` resolves
# through spacing["container-max"] -- undefined keys silently render as no-op.
TOKEN_RE = re.compile(
	r"\b(?:px|py|p|mx|my|mt|mb|ml|mr|gap|gap-x|gap-y|w|h|max-w|min-w|"
	r"space-x|space-y)-(?P<key>xs|sm|md|lg|xl|gutter|container-max)\b"
)

# Tokens that pin the header geometry so a future edit cannot shift it silently.
PINNED_HEADER_MARKERS = [
	"h-[72px]",  # bar height
	"px-md",  # inner container padding (16px)
	"py-sm",  # inner container padding (8px)
	"max-w-container-max",  # inner container width cap (1280px)
	"text-2xl",  # logo size (24px)
	"gap-sm",  # icon group gap (8px)
	"w-6 h-6",  # icon box (24px) inside p-2 -> 40x40 buttons
]

SCROLLBAR_RULE = "scrollbar-gutter"

# A hand-copied header/footer is detectable by these sentinels: they are the
# partial's own opening markup, so any occurrence OUTSIDE the partial is a copy.
COPY_SENTINELS = [
	"sticky top-0 z-50 w-full shadow-sm bg-surface",
	"justify-between items-center max-w-container-max mx-auto border-t",
]

# Negative margins / horizontal offsets used as alignment compensation are
# forbidden (they were explicitly ruled out as a fix for the header shift).
# `-translate-y-1/2` is a transform, not a layout offset, so it is not matched.
NEGATIVE_OFFSET_RE = re.compile(
	r"(?<![\w-])-(?:m|mx|my|mt|mb|ml|mr|space-x|space-y|left|right|inset|start|end)-"
)
NEGATIVE_INLINE_MARGIN_RE = re.compile(r"margin(?:-left|-right|-top|-bottom)?\s*:\s*-\d")

# Documented exemptions: pages known to violate a rule and left untouched on
# purpose (out of the current scope), each with its reason. The check FAILS if
# an exemption becomes stale (i.e. the page gets fixed), so the list cannot rot
# silently.
EXEMPTIONS = {
	"biozone_web/www/order-confirmed.html": (
		"includes the shared header/footer but has no `spacing` block (its header "
		"renders full-bleed: max-width none, padding 0px). Same root cause as "
		"/account, deliberately NOT touched by the /account-scoped fix; remove "
		"this exemption when it is unified."
	),
}


def host_pages(app_root):
	"""Every template that includes the shared header (customer-facing pages)."""
	pages = []
	for path in sorted((app_root / "biozone_web/www").rglob("*.html")):
		if HEADER_INCLUDE in path.read_text(encoding="utf-8"):
			pages.append(path)
	return pages


def rel(path, app_root):
	return str(path.relative_to(app_root))


def spacing_keys(text):
	"""Keys declared in the page's `spacing: { ... }` tailwind config block."""
	found = re.search(r"spacing\s*:\s*\{(?P<body>[^}]*)\}", text)
	if not found:
		return set()
	return {raw.strip() for raw in re.findall(r"[\"']?([\w-]+)[\"']?\s*:", found.group("body"))}


def required_token_keys(app_root):
	"""Named token keys used by the shared partials (must exist on host pages)."""
	keys = set()
	for name in (HEADER_PARTIAL, FOOTER_PARTIAL):
		text = (app_root / name).read_text(encoding="utf-8")
		keys |= {m.group("key") for m in TOKEN_RE.finditer(text)}
	return keys



def check_token_coverage(app_root, pages):
	"""R1: host pages must define every named token the partials rely on."""
	problems = []
	needed = required_token_keys(app_root)
	for page in pages:
		text = page.read_text(encoding="utf-8")
		missing = sorted(needed - spacing_keys(text))
		exempt = rel(page, app_root) in EXEMPTIONS
		if missing and not exempt:
			problems.append(
				f"{rel(page, app_root)}: shared partials use tokens this page does not define "
				f"(missing spacing keys: {missing}) -- the partial would render unstyled there"
			)
		elif missing and exempt:
			print(f"[KNOWN] {rel(page, app_root)}: exempted -- missing spacing keys {missing}")
		elif exempt:
			problems.append(
				f"{rel(page, app_root)}: exemption is STALE -- the page now defines every token; "
				f"delete it from EXEMPTIONS in {__file__}"
			)
	return problems


def check_no_manual_copies(app_root, pages):
	"""R2: no page may carry its own copy of the shared header/footer markup."""
	problems = []
	for page in pages:
		for lineno, line in enumerate(page.read_text(encoding="utf-8").splitlines(), start=1):
			for sentinel in COPY_SENTINELS:
				if sentinel in line:
					problems.append(
						f"{rel(page, app_root)}:{lineno}: hand-copied shared header/footer markup "
						f"({sentinel[:36]}...) -- include the partial instead"
					)
	return problems


def check_scrollbar_rule_location(app_root):
	"""R3: the stable-gutter rule must exist, and only in the shared partial."""
	problems = []
	partial = app_root / HEADER_PARTIAL
	if SCROLLBAR_RULE not in partial.read_text(encoding="utf-8"):
		problems.append(
			f"{HEADER_PARTIAL}: missing the `{SCROLLBAR_RULE}` page-width stabilisation rule "
			f"(without it the shared header shifts by the scrollbar width whenever page height "
			f"crosses the viewport height)"
		)
	for path in sorted((app_root / "biozone_web").rglob("*.html")):
		if path == partial:
			continue
		text = path.read_text(encoding="utf-8")
		if SCROLLBAR_RULE in text:
			line = next(i for i, ln in enumerate(text.splitlines(), 1) if SCROLLBAR_RULE in ln)
			problems.append(
				f"{rel(path, app_root)}:{line}: `{SCROLLBAR_RULE}` must live in the shared partial "
				f"only, never per page (a per-page rule re-creates the inconsistency)"
			)
	return problems


def check_multifield_forms(app_root):
	"""R4: every >=3-field GET form declares base + two breakpoint layouts."""
	problems = []
	account_dir = app_root / "biozone_web/www/account"
	for path in sorted(account_dir.glob("*.html")):
		text = path.read_text(encoding="utf-8")
		for match in re.finditer(r"<form\b[^>]*>", text):
			tag = match.group(0)
			end = text.find("</form>", match.end())
			body = text[match.end(): end if end != -1 else len(text)]
			if len(re.findall(r"<(?:input(?![^>]*type=\"hidden\")|select|button|textarea)\b", body)) < 3:
				continue
			classes = re.search(r'class="([^"]*)"', tag)
			tokens = classes.group(1).split() if classes else []
			points = {bp for bp in ("sm", "md", "lg") for t in tokens if t.startswith(bp + ":")}
			if len(points) < 2:
				line = text.count("\n", 0, match.start()) + 1
				problems.append(
					f"{rel(path, app_root)}:{line}: multi-field form declares only {sorted(points)} "
					f"breakpoint layout(s) -- phone/medium/desktop must each be explicit (rule 4)"
				)
	return problems


def check_header_geometry_pinned(app_root):
	"""R5: the header's explicit geometry markers are all present."""
	text = (app_root / HEADER_PARTIAL).read_text(encoding="utf-8")
	return [
		f"{HEADER_PARTIAL}: pinned header marker `{marker}` is missing -- logo/icon size or the "
		f"container's width+alignment is no longer explicit (rule 7)"
		for marker in PINNED_HEADER_MARKERS
		if marker not in text
	]


def check_no_negative_offsets(app_root):
	"""R6: no negative margins/offsets as alignment compensation in /account."""
	problems = []
	targets = [app_root / HEADER_PARTIAL, app_root / FOOTER_PARTIAL]
	targets += sorted((app_root / "biozone_web/www/account").glob("*.html"))
	for path in targets:
		for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
			if NEGATIVE_OFFSET_RE.search(line):
				problems.append(
					f"{rel(path, app_root)}:{lineno}: negative margin/offset used as compensation "
					f"(forbidden: fix the container instead) -- {line.strip()[:70]}"
				)
			if NEGATIVE_INLINE_MARGIN_RE.search(line):
				problems.append(
					f"{rel(path, app_root)}:{lineno}: negative inline margin (forbidden) -- "
					f"{line.strip()[:70]}"
				)
	return problems
	return keys
def main(argv=None):
	ap = argparse.ArgumentParser(description="Shared partials static guard.")
	ap.add_argument("--app-root", default=None, help="biozone_web app dir (default: parent of scripts/)")
	args = ap.parse_args(argv)
	app_root = (
		pathlib.Path(args.app_root) if args.app_root else pathlib.Path(__file__).resolve().parent.parent
	)
	if not (app_root / "biozone_web").is_dir():
		print("FAIL: app dir not found under %s" % app_root)
		return 1

	pages = host_pages(app_root)
	print("check_shared_partials: app_root=%s" % app_root)
	print("host pages including the shared header: %d" % len(pages))
	print("named tokens required by the shared partials: %s" % sorted(required_token_keys(app_root)))

	checks = [
		("R1 named-token coverage on every host page", check_token_coverage(app_root, pages)),
		("R2 no hand-copied header/footer", check_no_manual_copies(app_root, pages)),
		("R3 scrollbar rule lives in the shared partial only", check_scrollbar_rule_location(app_root)),
		("R4 multi-field forms declare 3 layouts", check_multifield_forms(app_root)),
		("R5 header geometry markers pinned", check_header_geometry_pinned(app_root)),
		("R6 no negative margins/offsets in /account", check_no_negative_offsets(app_root)),
	]

	failures = 0
	for title, problems in checks:
		print("[%s] %s" % ("FAIL" if problems else "OK  ", title))
		for problem in problems:
			print("    - %s" % problem)
		failures += len(problems)

	print("-" * 68)
	if failures:
		print(
			"FAIL: %d problem(s). Fix them, then re-run:\n  python3 scripts/check_shared_partials.py"
			% failures
		)
		return 1
	print(
		"PASS: %d host page(s); shared partials use only tokens defined everywhere they are "
		"included; no hand-copied header/footer; scrollbar stability declared once in the shared "
		"partial; multi-field forms explicit per breakpoint; header geometry pinned; no negative "
		"offsets." % len(pages)
	)
	return 0


if __name__ == "__main__":
	sys.exit(main())