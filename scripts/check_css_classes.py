#!/usr/bin/env python3
"""Static guard: every CSS class used in a template must exist in the CSS.

WHY THIS EXISTS
---------------
The store templates load a *pre-built* stylesheet (``public/css/app.<hash>.css``).
Tailwind only emits the utilities it saw at build time, so a class that is new
in a template simply does not exist at runtime — with no error anywhere. Three
measured cases already shipped like this:

* ``max-h-[90vh]`` on the guest-merge modal  -> no height cap, card overflowed.
* ``pl-20`` on the header search input      -> no left padding under the scan icon.
* ``left-10`` on the "clear search" link    -> the link overlapped the submit icon.

All three looked fine in the markup and broke only in the browser. This guard
turns that whole class of defect into a fast, deterministic failure.

WHAT IT CHECKS
--------------
1. Collects every class **defined** by the compiled CSS (``public/css/app*.css``)
   and ``fonts.css`` — including escaped variants such as ``hover\\:bg-primary``.
2. Adds classes defined by inline ``<style>`` blocks inside the templates
   themselves (page-local rules are legitimate).
3. Extracts every ``class="..."`` value from ``www/**/*.html`` and
   ``templates/**/*.html`` (Jinja expressions are stripped, never guessed).
4. Fails (exit 1) listing each used-but-undefined class with file:line.

USAGE
-----
    python3 scripts/check_css_classes.py            # from the app dir
    python3 scripts/check_css_classes.py --json     # machine-readable
    python3 scripts/check_css_classes.py --root /path/to/biozone_web

EXIT CODES
----------
0 = every used class exists
1 = at least one used class is not generated (list printed)
2 = guard could not run (missing CSS/templates) — treated as failure on purpose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

CLASS_ATTR = re.compile(r"""class\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.S)
SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
ALLOWLIST_FILE = "scripts/css_class_allowlist.txt"
JINJA = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.S)
# اسم الفئة في CSS قد يحتوي هروبًا سداسيًا مثل \2c (فاصلة) — لا بد من دعمه،
# وإلا انقطعت أسماء الفئات ذات القيم الاعتباطية (shadow-[...] وغيرها).
CSS_NAME_CHAR = r"(?:\\[0-9a-fA-F]{1,6}\s?|\\.|[A-Za-z0-9_-])"
CSS_CLASS = re.compile(r"(?<![\w.-])\.(" + CSS_NAME_CHAR + r"+)")
CSS_HEX_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?")
STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
# Tokens that are not CSS classes by design (JS hooks only, never styled).
ALLOWED_TOKENS = {
	"material-symbols-outlined",  # font class, defined in fonts.css as well
}

# classes that are only meaningful inside Jinja (e.g. "{{ x }}") never reach here
BAD_TOKEN = re.compile(r"[\s{}\"'<>]")


def _unescape(name: str) -> str:
	r"""يفكّ الهروب السداسي (\2c → ,) ثم أي هروب بسيط (\: → :)."""
	def hex_repl(match):
		try:
			return chr(int(match.group(1), 16))
		except ValueError:
			return ""

	text = CSS_HEX_ESCAPE.sub(hex_repl, name)
	return re.sub(r"\\(.)", r"\1", text)


def css_classes(text: str) -> set[str]:
	out = set()
	for match in CSS_CLASS.finditer(text):
		name = _unescape(match.group(1)).strip()
		if name and not name[0].isdigit():
			out.add(name)
	return out


def defined_classes(root: Path) -> tuple[set[str], list[str]]:
	css_dir = root / "public" / "css"
	files = sorted(css_dir.glob("app*.css")) + sorted(css_dir.glob("fonts.css"))
	if not files:
		raise SystemExit(f"no compiled CSS found under {css_dir}")
	defined: set[str] = set()
	for path in files:
		defined |= css_classes(path.read_text(encoding="utf-8", errors="ignore"))
	return defined, [str(p.relative_to(root)) for p in files]


def strip_scripts(text: str) -> str:
	"""يستبدل محتوى <script> بمسافات (نفس عدد الأسطر) حتى لا تُقرأ سلاسل JS
	التي تحتوي class=\"...\" كأنها ترميز قوالب."""
	def blank(match):
		return re.sub(r"[^\n]", " ", match.group(0))
	return SCRIPT_BLOCK.sub(blank, text)


def load_allowlist(root: Path) -> set[str]:
	path = root.parent / "scripts" / "css_class_allowlist.txt"
	if not path.exists():
		return set()
	out = set()
	for line in path.read_text(encoding="utf-8").splitlines():
		line = line.strip()
		if line and not line.startswith("#"):
			out.add(line.split()[0])
	return out


def template_files(root: Path) -> list[Path]:
	files = sorted((root / "www").rglob("*.html"))
	files += sorted((root / "templates").rglob("*.html"))
	return [p for p in files if p.is_file()]


def inline_defined(files: list[Path]) -> set[str]:
	out: set[str] = set()
	for path in files:
		text = path.read_text(encoding="utf-8", errors="ignore")
		for block in STYLE_BLOCK.findall(text):
			out |= css_classes(block)
	return out


def used_classes(files: list[Path]) -> dict[str, list[tuple[str, int]]]:
	found: dict[str, list[tuple[str, int]]] = defaultdict(list)
	for path in files:
		text = strip_scripts(path.read_text(encoding="utf-8", errors="ignore"))
		for match in CLASS_ATTR.finditer(text):
			raw = match.group(1) if match.group(1) is not None else match.group(2) or ""
			clean = JINJA.sub(" ", raw)
			if not clean.strip():
				continue
			line = text.count("\n", 0, match.start()) + 1
			for token in clean.split():
				if BAD_TOKEN.search(token):
					continue
				token = token.strip()
				if token:
					found[token].append((str(path), line))
	return found


def main() -> int:
	parser = argparse.ArgumentParser(description="Fail when a template uses a class the CSS never generated.")
	parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1] / "biozone_web"))
	parser.add_argument("--json", action="store_true")
	args = parser.parse_args()

	root = Path(args.root).resolve()
	defined, css_files = defined_classes(root)
	files = template_files(root)
	if not files:
		print(f"FAIL: no templates found under {root}/www or {root}/templates")
		return 2
	defined |= inline_defined(files)

	used = used_classes(files)
	allowed = ALLOWED_TOKENS | load_allowlist(root)
	missing = {
		token: places
		for token, places in used.items()
		if token not in defined and token not in allowed
	}
	skipped = sorted(t for t in used if t not in defined and t in allowed)

	if args.json:
		print(json.dumps(
			{
				"css_files": css_files,
				"templates_scanned": len(files),
				"classes_used": len(used),
				"classes_defined": len(defined),
				"missing": {k: v for k, v in sorted(missing.items())},
			},
			ensure_ascii=False,
			indent=2,
		))
		return 1 if missing else 0

	print(f"css: {', '.join(css_files)}")
	print(f"templates: {len(files)} · used classes: {len(used)} · defined classes: {len(defined)}")
	if skipped:
		print(f"allowlisted non-CSS hooks: {len(skipped)}")
	if not missing:
		print("OK: every class used in templates exists in the compiled CSS")
		return 0

	print(f"\nFAIL: {len(missing)} class(es) used but never generated:\n")
	for token in sorted(missing):
		places = missing[token]
		first = places[0]
		rel = Path(first[0]).resolve().relative_to(root) if Path(first[0]).is_absolute() else first[0]
		print(f"  {token:<40} {rel}:{first[1]}  ({len(places)} use(s))")
	print("\nFix: add a page-local <style> rule (preferred) or regenerate the CSS build.")
	return 1


if __name__ == "__main__":
	sys.exit(main())
