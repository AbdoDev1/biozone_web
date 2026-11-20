"""Regression guard: every icon name used must exist in the shipped subset font.

The storefront uses a small subset of Material Symbols
(``public/fonts/material-symbols-outlined.woff2``). Using a name that is not
in that subset renders the raw text (e.g. ``ARROW_DOWNWARD``) and breaks the
button layout — the cart jump button hit exactly this. This test diffs every
icon name referenced in templates/JS against the font's ligature set, so the
next added icon fails here instead of in production.
"""

import os
import re
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_PATH = os.path.join(APP_DIR, "public", "fonts", "material-symbols-outlined.woff2")


def _font_icon_names():
	try:
		from fontTools.ttLib import TTFont
	except ImportError:  # pragma: no cover - environment guard
		raise unittest.SkipTest("fontTools is not available")

	font = TTFont(FONT_PATH)
	code_to_glyph = {glyph: code for code, glyph in font.getBestCmap().items()}

	def text(glyph):
		code = code_to_glyph.get(glyph)
		return chr(code) if code else "?"

	names = set()
	for lookup in font["GSUB"].table.LookupList.Lookup:
		for subtable in lookup.SubTable:
			inner = getattr(subtable, "ExtSubTable", subtable)
			for first, ligatures in getattr(inner, "ligatures", {}).items():
				for ligature in ligatures:
					names.add(text(first) + "".join(text(c) for c in ligature.Component))
	return names


def _used_icon_names():
	"""Collect literal icon names from templates/JS that reference the font."""
	span = re.compile(r"material-symbols-outlined[^>]*>\s*([a-z0-9_]+)\s*<", re.S)
	assign = re.compile(r"(?:textContent|innerHTML)\s*=\s*'([a-z][a-z0-9_]*)'")
	icon_kv = re.compile(r"['\"]icon['\"]\s*:\s*['\"]([a-z][a-z0-9_]*)['\"]")

	used = {}
	for root, dirs, files in os.walk(APP_DIR):
		dirs[:] = [d for d in dirs if d != "__pycache__"]
		for filename in files:
			if not filename.endswith((".html", ".js", ".py")):
				continue
			path = os.path.join(root, filename)
			try:
				text = open(path, encoding="utf-8").read()
			except Exception:
				continue
			if "material-symbols-outlined" not in text:
				continue
			for pattern in (span, assign, icon_kv):
				for match in pattern.finditer(text):
					used.setdefault(match.group(1), set()).add(
						os.path.relpath(path, APP_DIR)
					)
	return used


class TestIconFontCoverage(unittest.TestCase):
	def test_font_subset_is_shipped(self):
		self.assertTrue(os.path.exists(FONT_PATH), "icon subset font is missing")
		self.assertGreater(os.path.getsize(FONT_PATH), 0)

	def test_every_used_icon_exists_in_the_subset(self):
		available = _font_icon_names()
		self.assertTrue(available, "the shipped font exposes no icon ligatures")
		used = _used_icon_names()
		self.assertTrue(used, "icon scan found no usages — check the scanner")

		missing = {name: sorted(files) for name, files in used.items() if name not in available}
		self.assertEqual(
			missing,
			{},
			"icon names missing from the shipped subset font (they will render as raw "
			f"text): {sorted(missing)}",
		)
