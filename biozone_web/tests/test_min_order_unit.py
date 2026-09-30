"""Unit tests (DB-free) for the per-group minimum order limit.

Covers the pure building blocks only: Decimal quantization boundaries
(T-round contract), Arabic-Indic digit normalization, and the strict
staff-input parser. No database, no fixtures, no network — runnable
anywhere the app imports, mirroring test_store_items.py conventions.
"""

import unittest
from decimal import Decimal

from biozone_web.utils import (
    normalize_arabic_digits,
    parse_min_order_amount,
    quantize_2dp,
)


class TestQuantize2dp(unittest.TestCase):
    def test_exact_values_unchanged(self):
        self.assertEqual(quantize_2dp(5000), Decimal("5000.00"))
        self.assertEqual(quantize_2dp(4350.5), Decimal("4350.50"))
        self.assertEqual(quantize_2dp(0), Decimal("0.00"))
        self.assertEqual(quantize_2dp(None), Decimal("0.00"))

    def test_half_up_rounding(self):
        # T-round contract: 4999.995 quantizes UP to the limit (accepted),
        # 4999.994 quantizes DOWN (rejected).
        self.assertEqual(quantize_2dp(4999.995), Decimal("5000.00"))
        self.assertEqual(quantize_2dp(4999.994), Decimal("4999.99"))
        self.assertEqual(quantize_2dp(2.675), Decimal("2.68"))

    def test_comparison_semantics(self):
        limit = quantize_2dp(5000)
        self.assertTrue(quantize_2dp(4999.995) >= limit)
        self.assertFalse(quantize_2dp(4999.994) >= limit)
        self.assertTrue(quantize_2dp(5000.00) >= limit)

    def test_returns_decimal_not_float(self):
        self.assertIsInstance(quantize_2dp(10.01), Decimal)


class TestNormalizeArabicDigits(unittest.TestCase):
    def test_eastern_arabic(self):
        self.assertEqual(normalize_arabic_digits("٥٠٠٠"), "5000")

    def test_persian(self):
        self.assertEqual(normalize_arabic_digits("۲۳"), "23")

    def test_mixed_and_latin_untouched(self):
        self.assertEqual(normalize_arabic_digits("12٣٤"), "1234")
        self.assertEqual(normalize_arabic_digits("5000"), "5000")

    def test_non_string_passthrough(self):
        self.assertEqual(normalize_arabic_digits(5000), 5000)
        self.assertIsNone(normalize_arabic_digits(None))


class TestParseMinOrderAmount(unittest.TestCase):
    def test_empty_means_disabled(self):
        for raw in (None, "", "   "):
            ok, value, err = parse_min_order_amount(raw)
            self.assertTrue(ok)
            self.assertEqual(value, 0.0)
            self.assertIsNone(err)

    def test_valid_numbers(self):
        ok, value, err = parse_min_order_amount("5000")
        self.assertTrue(ok)
        self.assertEqual(value, 5000.0)
        self.assertIsNone(err)
        ok, value, err = parse_min_order_amount(2500.75)
        self.assertTrue(ok)
        self.assertEqual(value, 2500.75)
        ok, value, err = parse_min_order_amount("1,234.5")
        self.assertTrue(ok)
        self.assertEqual(value, 1234.5)

    def test_arabic_digits_accepted(self):
        ok, value, err = parse_min_order_amount("٥٠٠٠")
        self.assertTrue(ok)
        self.assertEqual(value, 5000.0)
        self.assertIsNone(err)

    def test_negative_rejected(self):
        ok, value, err = parse_min_order_amount("-1")
        self.assertFalse(ok)
        self.assertEqual(value, 0.0)
        self.assertTrue(err)

    def test_garbage_rejected(self):
        for raw in ("abc", "12x", "--5", True):
            ok, value, err = parse_min_order_amount(raw)
            self.assertFalse(ok, raw)
            self.assertEqual(value, 0.0)
            self.assertTrue(err, raw)

    def test_non_finite_rejected(self):
        for raw in ("nan", "inf", "-inf", float("nan")):
            ok, value, err = parse_min_order_amount(raw)
            self.assertFalse(ok, raw)
            self.assertTrue(err, raw)

    def test_absurd_cap_rejected(self):
        ok, value, err = parse_min_order_amount("100000001")
        self.assertFalse(ok)
        self.assertTrue(err)
        ok, value, err = parse_min_order_amount("100000000")
        self.assertTrue(ok)
        self.assertEqual(value, 100000000.0)


def run():
    suite = unittest.TestLoader().loadTestsFromName(__name__)
    return unittest.TextTestRunner(verbosity=2).run(suite)
