"""Unit tests for home section builders (Biozone Home Settings rows).

Pure-function coverage: owner-managed rows -> tiles / rail defs, with
rename/delete safety (rows pointing at missing groups are dropped, labels
fall back to the real group name). No DB writes; no HTTP layer.
"""

import unittest
from urllib.parse import quote

from biozone_web.www import biozone_home


def _row(group, label="", show_products=0, sort_order=0):
    return {
        "item_group": group,
        "label": label,
        "show_products": show_products,
        "sort_order": sort_order,
    }


GROUPS = {"كريم شعر", "شامبو", "مزيل عرق"}


class TestBuildTiles(unittest.TestCase):
    def test_order_and_label_fallback(self):
        rows = [_row("كريم شعر", sort_order=1), _row("شامبو", sort_order=2)]
        tiles = biozone_home.build_tiles(rows, GROUPS)
        self.assertEqual([t["item_group"] for t in tiles], ["كريم شعر", "شامبو"])
        self.assertEqual(tiles[0]["label"], "كريم شعر")
        self.assertEqual(tiles[1]["label"], "شامبو")

    def test_display_label_override(self):
        rows = [_row("مزيل عرق", label="مزيل عرق سبراي", sort_order=1)]
        tiles = biozone_home.build_tiles(rows, GROUPS)
        self.assertEqual(tiles[0]["label"], "مزيل عرق سبراي")

    def test_deleted_group_is_dropped(self):
        rows = [_row("كريم شعر", sort_order=1), _row("حمام كريم", sort_order=2)]
        tiles = biozone_home.build_tiles(rows, GROUPS)
        self.assertEqual([t["item_group"] for t in tiles], ["كريم شعر"])

    def test_url_is_encoded_filter_link(self):
        rows = [_row("شامبو", sort_order=1)]
        tiles = biozone_home.build_tiles(rows, GROUPS)
        self.assertEqual(tiles[0]["url"], "/catalog?category=" + quote("شامبو", safe=""))

    def test_empty_rows(self):
        self.assertEqual(biozone_home.build_tiles([], GROUPS), [])
        self.assertEqual(biozone_home.build_tiles(None, GROUPS), [])


class TestBuildRailDefs(unittest.TestCase):
    def test_only_flagged_rows(self):
        rows = [
            _row("كريم شعر", sort_order=1),
            _row("شامبو", show_products=1, sort_order=2),
            _row("مزيل عرق", label="مزيل عرق سبراي", show_products=1, sort_order=3),
        ]
        defs = biozone_home.build_rail_defs(rows, GROUPS)
        self.assertEqual([d["group"] for d in defs], ["شامبو", "مزيل عرق"])
        self.assertEqual(defs[0]["title"], "شامبو")
        self.assertEqual(defs[1]["title"], "مزيل عرق سبراي")
        self.assertEqual(defs[1]["url"], "/catalog?category=" + quote("مزيل عرق", safe=""))

    def test_deleted_group_is_dropped(self):
        rows = [_row("حمام كريم", show_products=1, sort_order=1)]
        self.assertEqual(biozone_home.build_rail_defs(rows, GROUPS), [])

    def test_empty_rows(self):
        self.assertEqual(biozone_home.build_rail_defs([], GROUPS), [])
