"""اختبارات حرس فئات CSS (DB-free).

الهدف: منع ارتداد خطأ التحليل الذي جعل الحرس يبلّغ عن فئات موجودة
(هروب سداسي `\\2c` للفاصلة داخل القيم الاعتباطية).

    python3 -m unittest biozone_web.tests.test_css_class_guard
"""

import os
import sys
import unittest


def _root():
	return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_guard():
	sys.path.insert(0, os.path.join(os.path.dirname(_root()), "scripts"))
	import check_css_classes  # noqa: WPS433

	return check_css_classes


class TestCssClassGuard(unittest.TestCase):
	def setUp(self):
		self.guard = _load_guard()

	def test_hex_escaped_comma_is_decoded(self):
		# الشكل الفعلي في app.<hash>.css لصنف ظل بقيمة اعتباطية
		raw = r"shadow-\[0_4px_24px_rgba\(0\2c 0\2c 0\2c 0\.05\)\]"
		self.assertEqual(
			self.guard._unescape(raw),
			"shadow-[0_4px_24px_rgba(0,0,0,0.05)]",
		)

	def test_escaped_variant_separator_is_decoded(self):
		self.assertEqual(self.guard._unescape(r"hover\:bg-primary"), "hover:bg-primary")

	def test_real_css_contains_arbitrary_shadow_and_bracket_grid(self):
		defined, _files = self.guard.defined_classes(self.guard.Path(_root()))
		for token in (
			"shadow-[0_4px_24px_rgba(0,0,0,0.05)]",
			"shadow-[0_2px_8px_rgba(11,28,48,0.04)]",
			"lg:grid-cols-[minmax(0,1fr)_10rem_10rem_auto]",
			"pl-12",
		):
			self.assertIn(token, defined, token)

	def test_known_missing_utilities_are_reported(self):
		defined, _files = self.guard.defined_classes(self.guard.Path(_root()))
		for token in ("pl-20", "left-10", "h-4.5", "w-4.5", "shadow-2xs"):
			self.assertNotIn(token, defined, token)


if __name__ == "__main__":
	unittest.main()
