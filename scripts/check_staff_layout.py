#!/usr/bin/env python3
"""Structural guard for staff pages: catch stray closing tags automatically.

WHY THIS EXISTS
---------------
This project's shell template (biozone_web/templates/includes/staff_shell_top.html)
intentionally opens a wrapper div (``.sidebar-main``) ONCE and leaves it open --
it is never closed in any page template; the browser auto-closes it at </body>.
That design makes a stray ``</div>`` in ANY page template fail SILENTLY: no
server error, no console error. The extra close tag just closes ``.sidebar-main``
early and ejects all following content out of <main>/.sidebar-main, so the page
renders underneath the fixed sidebar. This exact bug once hid in items.html and
dashboard.html and survived several rounds of CSS-level fixes (which could never
work -- correct CSS operating on a table that was structurally in the wrong part
of the page) before a manual multi-round investigation found it. This check
exists so the next stray tag is caught in seconds by a script, not by days of
debugging.

WHAT IT CHECKS (for every biozone_web/www/staff/*.html page)
------------------------------------------------------------
1. The shell's documented invariant: staff_shell_top.html leaves exactly one
   wrapper div (.sidebar-main) unclosed (plus <body>, closed by each page).
2. Each page, merged with its shell includes (the same merge the browser sees),
   has no stray/misnested structural tags (div/section/main/header/footer/
   aside/nav/form/table/...): a stack-based nesting check, NOT just equal
   open/close counts -- order and nesting matter.
3. Div-balance: after the merge, exactly the shell's one wrapper div remains
   open (auto-closed at </body>). A stray close makes the balance 0 (dashboard
   case) or triggers misnesting errors (items case); an unclosed added div
   makes it +2. All three fail loudly here.
4. Ancestor chain (static proxy): every <table> and <section> of a shell page
   must sit textually between <main> and </main> -- i.e. inside the content
   column, never as a direct child of <body>.
5. If html5lib is installed (``pip install html5lib``), the merged source is
   ALSO parsed with an HTML5-compliant parser and any "end-tag-too-early"
   error fails the check. Without html5lib this layer is skipped with a notice
   (the stack check above still runs -- it caught both real bugs on its own).

Raw template sources are checked WITHOUT rendering (no DB/app needed), so this
runs in pre-commit and in CI. Limitation, stated plainly: Jinja control tags
({% %}/{{ }}) are stripped before parsing, so a tag imbalance hidden INSIDE a
Jinja branch condition the strip can't see would be missed -- the rendered-page
browser check during development remains the authority for visual issues.

USAGE
-----
    # from the app root (biozone_web/):
    python3 scripts/check_staff_layout.py
    # against another checkout:
    python3 scripts/check_staff_layout.py --app-root /path/to/biozone_web

Exit status: 0 when every page passes, 1 with file:line details otherwise.
Pre-commit wiring: see the `check-staff-layout` local hook in
.pre-commit-config.yaml (runs this script when staff templates change).
"""

import argparse
import bisect
import pathlib
import re
import sys

TRACKED_TAGS = (
	"div",
	"section",
	"main",
	"header",
	"footer",
	"aside",
	"nav",
	"form",
	"table",
	"thead",
	"tbody",
	"tfoot",
	"tr",
	"body",
	"html",
	"head",
)
VOID_TAGS = frozenset(
	{
		"area",
		"base",
		"br",
		"col",
		"embed",
		"hr",
		"img",
		"input",
		"link",
		"meta",
		"param",
		"source",
		"track",
		"wbr",
	}
)
TAG_RE = re.compile(
	r"<(/?)\s*([a-zA-Z][a-zA-Z0-9]*)((?:\"[^\"]*\"|'[^']*'|[^'\">])*)>",
)
INCLUDE_RE = re.compile(r'{%\s*include\s*["\']([^"\']+)["\']\s*%}')


def strip_noise(text):
	"""Remove HTML comments, <style>/<script> bodies, and Jinja tags/text.

	All three would otherwise produce false tag matches (CSS selectors like
	#items-management-table, JS innerHTML strings, explanatory comments that
	mention tag names, multi-line Jinja expressions).
	"""
	text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
	text = re.sub(
		r"(<style\b[^>]*>).*?(</style\s*>)",
		lambda m: m.group(1) + m.group(2),
		text,
		flags=re.S | re.I,
	)
	text = re.sub(
		r"(<script\b[^>]*>).*?(</script\s*>)",
		lambda m: m.group(1) + m.group(2),
		text,
		flags=re.S | re.I,
	)
	text = re.sub(r"{#.*?#}", "", text, flags=re.S)
	text = re.sub(r"{%.*?%}", "", text, flags=re.S)
	text = re.sub(r"{{.*?}}", "x", text, flags=re.S)
	return text


def line_of(offsets, pos):
	return bisect.bisect_right(offsets, pos) + 1


def parse_tags(text, shell_mode=False):
	"""Stack-based nesting check. Returns (errors, leftover_stack).

	shell_mode=True encodes this project's documented shell design: the
	shell's .sidebar-main wrapper div is intentionally left open and the
	parser auto-closes it at </body>. So a closing </body>/</html> seen while
	the wrapper div is still open is NOT an error -- the body/html frame is
	retired silently and the wrapper stays on the stack for the caller to
	assert on. Any other misnesting is still reported.
	"""
	clean = strip_noise(text)
	offsets = [m.start() + 1 for m in re.finditer(r"\n", clean)]
	errors = []
	stack = []  # (tag, lineno)
	for match in TAG_RE.finditer(clean):
		closing, name = match.group(1), match.group(2).lower()
		rest = match.group(3) or ""
		lineno = line_of(offsets, match.start())
		if name not in TRACKED_TAGS:
			continue
		if name in VOID_TAGS:
			continue
		if not closing and rest.rstrip().endswith("/"):
			continue  # self-closing <div ... />
		if not closing:
			stack.append((name, lineno))
		elif shell_mode and name in ("body", "html") and stack and stack[-1][0] == "div":
			# Documented auto-close: retire the body/html frame, keep wrapper.
			idx = next((j for j in range(len(stack) - 1, -1, -1) if stack[j][0] == name), None)
			if idx is None:
				errors.append((lineno, "stray </%s>: no open tag on the stack" % name))
			else:
				del stack[idx]
		elif stack and stack[-1][0] == name:
			stack.pop()
		elif not stack:
			errors.append((lineno, "stray </%s>: no open tag on the stack" % name))
		else:
			idx = next((j for j in range(len(stack) - 1, -1, -1) if stack[j][0] == name), None)
			if idx is None:
				errors.append(
					(
						lineno,
						"stray </%s>: closes nothing (open: %s)"
						% (name, " > ".join(t for t, _ in stack[-3:])),
					)
				)
			else:
				errors.append(
					(
						lineno,
						"</%s> misnested: %s still open above it (opened at lines %s)"
						% (
							name,
							", ".join("<%s>" % t for t, _ in stack[idx + 1 :]) or "nothing",
							", ".join(str(n) for _, n in stack[idx:]),
						),
					)
				)
				del stack[idx]
	return errors, stack


def resolve_merged(page_path, app_root):
	"""Inline {% include %} directives so the check sees shell+page as one doc."""
	text = page_path.read_text(encoding="utf-8")

	def repl(match):
		inc = app_root / match.group(1)
		if inc.is_file():
			return inc.read_text(encoding="utf-8")
		return "<!-- check_staff_layout: include not found: %s -->" % match.group(1)

	merged = INCLUDE_RE.sub(repl, text)
	uses_shell = "staff_shell_top.html" in text
	return merged, uses_shell


def check_page(page_path, app_root):
	"""Returns a list of error strings (empty == pass)."""
	merged, uses_shell = resolve_merged(page_path, app_root)
	errors, stack = parse_tags(merged, shell_mode=uses_shell)
	problems = ["%s -- %s" % (ln, msg) for ln, msg in errors]

	if uses_shell:
		# Documented design: exactly .sidebar-main may remain open (the
		# parser auto-closes the wrapper at </body>, handled in shell_mode).
		# Anything else left open -- or the wrapper closed early (leftover
		# != ['div'], e.g. [] after a dashboard-style stray close) -- fails.
		closable = [t for t, _ in stack]
		if closable == ["div"]:
			m = re.search(r'<div\b[^>]*class="[^"]*sidebar-main[^"]*"[^>]*>', merged)
			if not m:
				problems.append("shell wrapper div (.sidebar-main) not found")
		else:
			if not closable:
				problems.append(
					"shell wrapper .sidebar-main was closed EARLY (leftover "
					"open tags: none) -- a stray close tag in this page is "
					"closing the shell wrapper instead of its own element"
				)
			else:
				problems.append(
					"unexpected tags left open at </html>: %s (expected only "
					"the shell's .sidebar-main wrapper div)" % closable
				)
		# Ancestor-chain proxy: tables/sections must live inside <main>.
		clean_merged = strip_noise(merged)
		main_open = clean_merged.find("<main")
		main_close = clean_merged.find("</main>")
		if main_open == -1 or main_close == -1:
			problems.append("no <main>...</main> found in merged page")
		else:
			for tag in ("table", "section"):
				for m in re.finditer(r"<%s[\s>]" % tag, clean_merged):
					if not (main_open < m.start() < main_close):
						line = clean_merged.count("\n", 0, m.start()) + 1
						problems.append(
							"merged line %d: <%s> sits OUTSIDE <main> -- content "
							"would render as a direct child of <body> (under the "
							"fixed sidebar)" % (line, tag)
						)
						break
	else:
		# Standalone page (e.g. login.html): must be fully balanced itself.
		if stack:
			problems.append("unclosed tags at EOF (standalone page): %s" % stack)

	# Optional HTML5-parser layer: real parse errors, not auto-corrected ones.
	try:
		import html5lib
	except ImportError:
		return problems, "merged-static only (html5lib not installed)"
	clean = strip_noise(merged)
	parser = html5lib.HTMLParser(strict=False, namespaceHTMLElements=False)
	try:
		parser.parse(clean)
	except Exception as exc:  # a crashing parser is itself a signal
		problems.append("html5lib crashed on merged source: %r" % exc)
		return problems, "merged-static + html5lib(crashed)"
	for pos, code, data in parser.errors:
		if "end-tag-too-early" in str(code):
			line = pos[0] if isinstance(pos, tuple) else "?"
			problems.append("html5lib line %s: end-tag-too-early %s" % (line, data))
	return problems, "merged-static + html5lib"


def check_shell_invariant(app_root):
	"""The shell must leave exactly one wrapper div (.sidebar-main) open."""
	top = app_root / "biozone_web/templates/includes/staff_shell_top.html"
	if not top.is_file():
		return ["shell top include missing: %s" % top]
	errors, stack = parse_tags(top.read_text(encoding="utf-8"))
	problems = ["shell_top %s -- %s" % (ln, msg) for ln, msg in errors]
	if [t for t, _ in stack] != ["body", "div"]:
		problems.append(
			"shell invariant broken: staff_shell_top.html must leave exactly "
			"<body> + one wrapper <div> open, found %s" % stack
		)
	return problems


def main(argv=None):
	ap = argparse.ArgumentParser(description="Staff shell structural guard.")
	ap.add_argument("--app-root", default=None, help="biozone_web app dir (default: parent of scripts/)")
	args = ap.parse_args(argv)
	app_root = (
		pathlib.Path(args.app_root) if args.app_root else pathlib.Path(__file__).resolve().parent.parent
	)
	staff_dir = app_root / "biozone_web/www/staff"
	if not staff_dir.is_dir():
		print("FAIL: staff template dir not found: %s" % staff_dir)
		return 1

	print("check_staff_layout: app_root=%s" % app_root)
	failures = 0
	shell_problems = check_shell_invariant(app_root)
	print("[shell] staff_shell_top.html ... %s" % ("FAIL" if shell_problems else "OK"))
	for p in shell_problems:
		print("    - %s" % p)
	failures += len(shell_problems)

	pages = sorted(staff_dir.glob("*.html"))
	for page in pages:
		problems, method = check_page(page, app_root)
		status = "FAIL" if problems else "OK"
		print("[%s] %-20s (%s)" % (status, page.name, method))
		for p in problems:
			print("    - %s" % p)
		failures += len(problems) if problems else 0

	try:
		import html5lib
	except ImportError:
		print("note: html5lib not installed -- html5lib layer skipped (pip install html5lib to enable it)")
	print("-" * 60)
	if failures:
		print(
			"FAIL: %d problem(s). Fix the tags above, then re-run:\n"
			"  python3 scripts/check_staff_layout.py" % failures
		)
		return 1
	print(
		"PASS: %d page(s), shell invariant holds, no stray/misnested tags, "
		"content nested inside <main>." % len(pages)
	)
	return 0


if __name__ == "__main__":
	sys.exit(main())
