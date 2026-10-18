"""Unit tests for the home brand card builder (Biozone Home Brand rows).

Pure-function coverage: owner-managed rows -> brand cards, with
rename/delete safety (rows pointing at missing brands are dropped, labels
fall back to the real brand name) and first-row-wins de-duplication.
No DB writes; no HTTP layer.
"""

import unittest

from biozone_web.www import biozone_home


def _row(brand, label="", sort_order=0):
    return {"brand": brand, "label": label, "sort_order": sort_order}


BRANDS = {"Apple Baby": "/files/apple-baby.png", "Vatika": None, "Fogg": "/files/fogg.png"}


class TestBuildTopBrands(unittest.TestCase):
    def test_order_label_and_image(self):
        rows = [_row("Fogg", sort_order=1), _row("Vatika", label="فاتيكا", sort_order=2)]
        cards = biozone_home.build_top_brands(rows, BRANDS)
        self.assertEqual([c["brand"] for c in cards], ["Fogg", "Vatika"])
        self.assertEqual(cards[0]["image"], "/files/fogg.png")
        self.assertEqual(cards[0]["label"], "Fogg")
        self.assertEqual(cards[1]["label"], "فاتيكا")

    def test_deleted_brand_is_dropped(self):
        rows = [_row("Fogg", sort_order=1), _row("محذوف", sort_order=2)]
        cards = biozone_home.build_top_brands(rows, BRANDS)
        self.assertEqual([c["brand"] for c in cards], ["Fogg"])

    def test_duplicate_brand_first_row_wins(self):
        rows = [
            _row("Apple Baby", label="أ", sort_order=1),
            _row("Apple Baby", label="ب", sort_order=2),
        ]
        cards = biozone_home.build_top_brands(rows, BRANDS)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["label"], "أ")

    def test_no_cap(self):
        existing = {f"B{i}": None for i in range(10)}
        rows = [_row(f"B{i}", sort_order=i) for i in range(10)]
        cards = biozone_home.build_top_brands(rows, existing)
        self.assertEqual(len(cards), 10)

    def test_empty_rows(self):
        self.assertEqual(biozone_home.build_top_brands([], BRANDS), [])
        self.assertEqual(biozone_home.build_top_brands(None, BRANDS), [])
