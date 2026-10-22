import unittest
from unittest.mock import patch

from biozone_web.units import resolve_item_uom_factor

DATA = {
	"BZ-T1": {
		"stock_uom": "قطعة",
		"override": "inherit",
		"disabled": False,
		"conversions": [
			{"uom": "كرتونة", "factor": 12.0, "min_qty": 1.0},
			{"uom": "باكت", "factor": 6.0, "min_qty": 1.0},
		],
	}
}


class TestResolveItemUomFactor(unittest.TestCase):
	def _factor(self, uom):
		with patch("biozone_web.units._item_unit_data", return_value=DATA):
			return resolve_item_uom_factor("BZ-T1", uom)

	def test_blank_uom_returns_none(self):
		self.assertIsNone(self._factor(""))
		self.assertIsNone(self._factor(None))
		self.assertIsNone(self._factor("   "))
		self.assertIsNone(self._factor(123))

	def test_stock_uom_returns_one(self):
		self.assertEqual(self._factor("قطعة"), 1.0)

	def test_valid_conversion_returns_server_factor(self):
		self.assertEqual(self._factor("كرتونة"), 12.0)
		self.assertEqual(self._factor("باكت"), 6.0)

	def test_unknown_uom_returns_none(self):
		self.assertIsNone(self._factor("علبة"))

	def test_unknown_item_returns_none(self):
		with patch("biozone_web.units._item_unit_data", return_value={}):
			self.assertIsNone(resolve_item_uom_factor("BZ-X", "قطعة"))


def run():
	suite = unittest.TestLoader().loadTestsFromName(__name__)
	return unittest.TextTestRunner(verbosity=2).run(suite)
