"""DB-free tests for the unit-price rounding mirror of ERPNext.

The quote must subtract the *rounded* discount amount, exactly like
ERPNext's calculate_item_rate (taxes_and_totals.py:194-204). Subtracting
the raw discount made the quote 0.01/unit higher on tie prices
(45.00 with 7.5% -> quote 41.63, Sales Order 41.62), and the confirm
guard rejected the order with PRICE_CHANGED.
"""

import unittest
from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import patch

from biozone_web import utils as biozone_utils


def _fake_flt(value, precision=None, rounding_method=None):
    """Deterministic stand-in for frappe.utils.flt (2dp half-up)."""
    if precision is None:
        return float(value or 0)
    return float(
        Decimal(str(value or 0)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


class TestEffectiveUnitPrice(unittest.TestCase):
    def test_rounds_discount_before_subtracting(self):
        with patch("frappe.utils.flt", side_effect=_fake_flt):
            # 45.00 - 7.5% = 41.625 -> discount 3.375 -> 3.38, rate 41.62
            self.assertEqual(
                biozone_utils._effective_unit_price(45.0, 3.375, 7.5), 41.62
            )
            # 41.00 - 7.5% = 37.925 -> discount 3.075 -> 3.08, rate 37.92
            self.assertEqual(
                biozone_utils._effective_unit_price(41.0, 3.075, 7.5), 37.92
            )

    def test_no_discount_keeps_price(self):
        with patch("frappe.utils.flt", side_effect=_fake_flt):
            self.assertEqual(
                biozone_utils._effective_unit_price(45.0, 0, 0), 45.0
            )

    def test_fixed_discount_uses_rounded_amount(self):
        with patch("frappe.utils.flt", side_effect=_fake_flt):
            # A fixed discount is a Currency (2dp) value in practice; the
            # two-step keeps it identical to the order engine.
            self.assertEqual(
                biozone_utils._effective_unit_price(45.0, 3.375, 0), 41.62
            )


if __name__ == "__main__":
    unittest.main()
