"""Acceptance tests for catalog search (scope 1+2+3 only).

Covers: LIKE escaping (%, _, backslash), 128/129 length rule,
Arabic teh/alef expansion with literal-first ordering, exact barcode
search, empty/category/count/pagination/no-dupe behavior.

Convention: self-cleaning fixtures (ZZT- prefix), deleted in tearDown.
Live DB reads on dev; no template/HTTP layer (covered visually).
"""

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from biozone_web.www import catalog


def _tname(suffix):
    return "ZZT %s" % suffix


class TestSearchHelpers(unittest.TestCase):
    def test_escape_like(self):
        self.assertEqual(catalog.escape_like("a%b_c\\d"), "a\\%b\\_c\\\\d")
        self.assertEqual(catalog.escape_like("زولا"), "زولا")
        self.assertEqual(catalog.escape_like(""), "")

    def test_validate_length(self):
        ok, err = catalog.validate_search_term("x" * 128)
        self.assertTrue(ok)
        self.assertIsNone(err)
        ok, err = catalog.validate_search_term("x" * 129)
        self.assertFalse(ok)
        self.assertTrue(isinstance(err, str) and len(err) > 0)

    def test_variants_teh_both_ways(self):
        self.assertEqual(catalog.build_search_variants("ركبة"), ["ركبة", "ركبه"])
        self.assertEqual(catalog.build_search_variants("ركبه"), ["ركبه", "ركبة"])

    def test_variants_ya_both_ways(self):
        self.assertEqual(catalog.build_search_variants("هدى"), ["هدى", "هدي"])
        self.assertEqual(catalog.build_search_variants("هدي"), ["هدي", "هدى"])

    def test_variants_alef(self):
        v = catalog.build_search_variants("أطفال")
        self.assertIn("أطفال", v)
        self.assertIn("اطفال", v)
        self.assertEqual(v[0], "أطفال")

    def test_variants_plain_unchanged(self):
        self.assertEqual(catalog.build_search_variants("زولا"), ["زولا"])
        self.assertEqual(catalog.build_search_variants(""), [])


class TestCatalogSearchDB(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
        assert group, "no leaf item group on dev"
        cls.group = group
        cls.codes = []
        defs = [
            ("ZZT-0001", _tname("ركبة"), [{"barcode": "ZZTBC001"}]),
            ("ZZT-0002", _tname("ركبه"), []),
            ("ZZT-0003", _tname("100% قطن"), []),
            ("ZZT-0004", _tname("_شرطي"), []),
            ("ZZT-0005", _tname("عادي"), []),
            ("ZZT-0006", _tname("معطل"), [{"barcode": "ZZTBC006"}]),
            ("ZZT-0007", _tname("هدى"), []),
            ("ZZT-0008", _tname("هدي"), []),
            ("ZZT-0009", _tname("على"), []),
            ("ZZT-0010", _tname("علي"), []),
            ("ZZT-0011", _tname("أهدى"), []),
            ("ZZT-0012", _tname("اهدى"), []),
            ("ZZT-0013", _tname("باك\\سلاش"), []),
            ("ZZT-0014", _tname("نور هدى"), []),
            ("ZZT-0015", _tname("نور هدي"), []),
        ]
        for code, name, barcodes in defs:
            if frappe.db.exists("Item", code):
                frappe.delete_doc("Item", code, force=True)
            doc = frappe.get_doc({
                "doctype": "Item",
                "item_code": code,
                "item_name": name,
                "item_group": group,
                "stock_uom": "Nos",
                "disabled": 1 if code == "ZZT-0006" else 0,
                "barcodes": [{"barcode": b["barcode"]} for b in barcodes],
            })
            doc.insert(ignore_permissions=True)
            cls.codes.append(code)
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        for code in cls.codes:
            if frappe.db.exists("Item", code):
                frappe.delete_doc("Item", code, force=True)
        frappe.db.commit()
        super().tearDownClass()

    def _names(self, term, **kw):
        rows, total = catalog.search_items(term, **kw)
        return [r["item_name"] for r in rows], total

    def test_percent_literal(self):
        names, total = self._names("ZZT 100%")
        self.assertIn(_tname("100% قطن"), names)
        self.assertLess(total, frappe.db.count("Item", {"disabled": 0}))

    def test_underscore_literal(self):
        names, total = self._names("ZZT _")
        self.assertIn(_tname("_شرطي"), names)
        self.assertLess(total, frappe.db.count("Item", {"disabled": 0}))

    def test_backslash_safe(self):
        names, total = self._names("ZZT\\")
        self.assertIsInstance(names, list)

    def test_length_129_rejected_no_search(self):
        rows, total, err = catalog.search_items_with_validation("x" * 129)
        self.assertEqual(rows, [])
        self.assertEqual(total, 0)
        self.assertTrue(isinstance(err, str) and len(err) > 0)

    def test_length_128_ok(self):
        rows, total, err = catalog.search_items_with_validation("x" * 128)
        self.assertIsNone(err)

    def test_teh_both_directions_literal_first(self):
        names_a, total_a = self._names("ZZT ركبة")
        self.assertIn(_tname("ركبة"), names_a)
        self.assertIn(_tname("ركبه"), names_a)
        self.assertLess(names_a.index(_tname("ركبة")), names_a.index(_tname("ركبه")))
        names_b, total_b = self._names("ZZT ركبه")
        self.assertIn(_tname("ركبة"), names_b)
        self.assertIn(_tname("ركبه"), names_b)
        self.assertLess(names_b.index(_tname("ركبه")), names_b.index(_tname("ركبة")))
        # same union both directions (dev-data-bound, self-consistent)
        rows_a, _ = catalog.search_items("ZZT ركبة", page=1, page_size=100)
        rows_b, _ = catalog.search_items("ZZT ركبه", page=1, page_size=100)
        self.assertEqual({r["item_code"] for r in rows_a},
                         {r["item_code"] for r in rows_b})

    def test_teh_union_real_data(self):
        rows_a, total_a = catalog.search_items("ركبة", page=1, page_size=100)
        rows_b, total_b = catalog.search_items("ركبه", page=1, page_size=100)
        codes_a = [r["item_code"] for r in rows_a]
        codes_b = [r["item_code"] for r in rows_b]
        # union identical both directions (fixtures included by design)
        self.assertEqual(total_a, total_b)
        self.assertEqual(set(codes_a), set(codes_b))
        self.assertEqual(len(codes_a), len(set(codes_a)))
        for expected in ("BZ-10342", "BZ-10346", "BZ-10348", "BZ-10350",
                         "BZ-10352", "BZ-10362", "BZ-10946"):
            self.assertIn(expected, set(codes_a))
        # literal matches first
        first_a = codes_a[0]
        self.assertIn("ركبة", dict((r["item_code"], r["item_name"]) for r in rows_a)[first_a])

    def test_sara_four(self):
        rows, total = catalog.search_items("سرة", page=1, page_size=100)
        self.assertEqual(total, 4)
        rows2, total2 = catalog.search_items("سره", page=1, page_size=100)
        self.assertEqual(total2, 4)
        self.assertEqual({r["item_code"] for r in rows},
                         {r["item_code"] for r in rows2})

    def test_alef_no_direction_drops(self):
        rows_h, total_h = catalog.search_items("أطفال", page=1, page_size=100)
        rows_b, total_b = catalog.search_items("اطفال", page=1, page_size=100)
        codes_h = {r["item_code"] for r in rows_h}
        codes_b = {r["item_code"] for r in rows_b}
        self.assertEqual(codes_h, codes_b)
        self.assertIn("BZ-10692", codes_h)
        self.assertGreater(total_h, 1)

    def test_ya_both_directions_literal_first(self):
        names_a, _ = self._names("ZZT هدى")
        self.assertIn(_tname("هدى"), names_a)
        self.assertIn(_tname("هدي"), names_a)
        self.assertLess(names_a.index(_tname("هدى")), names_a.index(_tname("هدي")))
        names_b, _ = self._names("ZZT هدي")
        self.assertIn(_tname("هدى"), names_b)
        self.assertIn(_tname("هدي"), names_b)
        self.assertLess(names_b.index(_tname("هدي")), names_b.index(_tname("هدى")))

    def test_no_cross_contamination(self):
        names, _ = self._names("ZZT ركبه")
        for n in names:
            self.assertTrue("ركبه" in n or "ركبة" in n)

    def test_barcode_exact(self):
        names, total = self._names("ZZTBC001")
        self.assertEqual(total, 1)
        self.assertEqual(names, [_tname("ركبة")])

    def test_barcode_disabled_never_shows(self):
        _, total = self._names("ZZTBC006")
        self.assertEqual(total, 0)

    def test_barcode_respects_real_category(self):
        names, total = self._names("ZZTBC001", category=self.group)
        self.assertEqual(total, 1)
        self.assertEqual(names, [_tname("ركبة")])

    def test_barcode_respects_category_and_disabled(self):
        _, total = self._names("ZZTBC001", category="مجموعة-غير-موجودة-ZZT")
        self.assertEqual(total, 0)

    def test_disabled_excluded(self):
        names, _ = self._names("ZZT معطل")
        self.assertNotIn(_tname("معطل"), names)

    def test_empty_category_count_pages_no_dupes(self):
        _, total_all = self._names("ZZT")
        self.assertGreaterEqual(total_all, 5)
        p1, t1 = catalog.search_items("ZZT", page=1, page_size=2)
        p2, t2 = catalog.search_items("ZZT", page=2, page_size=2)
        self.assertEqual(t1, t2)
        c1 = [r["item_code"] for r in p1]
        c2 = [r["item_code"] for r in p2]
        self.assertEqual(len(set(c1) | set(c2)), len(c1) + len(c2))
        rows_all, _ = catalog.search_items("ZZT", page=1, page_size=100)
        seen = [(r["item_code"]) for r in rows_all]
        self.assertEqual(len(seen), len(set(seen)))

    def test_ala_ali_literal_first_separate(self):
        rows_a, _ = catalog.search_items("ZZT على", page=1, page_size=100)
        codes_a = [r["item_code"] for r in rows_a]
        self.assertIn("ZZT-0009", codes_a)
        self.assertIn("ZZT-0010", codes_a)
        self.assertLess(codes_a.index("ZZT-0009"), codes_a.index("ZZT-0010"))
        rows_b, _ = catalog.search_items("ZZT علي", page=1, page_size=100)
        codes_b = [r["item_code"] for r in rows_b]
        self.assertIn("ZZT-0009", codes_b)
        self.assertIn("ZZT-0010", codes_b)
        self.assertLess(codes_b.index("ZZT-0010"), codes_b.index("ZZT-0009"))

    def test_multiword_per_word_expansion(self):
        self.assertIn("ZZT نور هدي", catalog.build_search_variants("ZZT نور هدى"))
        names, _ = self._names("ZZT نور هدى")
        self.assertIn(_tname("نور هدى"), names)
        self.assertIn(_tname("نور هدي"), names)
        names, _ = self._names("ZZT نور هدي")
        self.assertIn(_tname("نور هدى"), names)
        self.assertIn(_tname("نور هدي"), names)

    def test_positional_no_midword_junk(self):
        for v in catalog.build_search_variants("هدى عادي"):
            self.assertNotIn("ةد", v)
        self.assertNotIn("سىرة", catalog.build_search_variants("سيرة"))
        self.assertIn("وجة", catalog.build_search_variants("وجه"))

    def test_interaction_alef_ya(self):
        for q in ("ZZT أهدى", "ZZT اهدى"):
            rows, _ = catalog.search_items(q, page=1, page_size=100)
            codes = {r["item_code"] for r in rows}
            self.assertIn("ZZT-0011", codes)
            self.assertIn("ZZT-0012", codes)

    def test_stored_data_unchanged(self):
        self._names("ZZT ركبه")
        self._names("ZZT هدى")
        for code, name in (("ZZT-0001", _tname("ركبة")),
                           ("ZZT-0007", _tname("هدى"))):
            doc = frappe.get_doc("Item", code)
            self.assertEqual(doc.item_name, name)
            self.assertEqual(doc.item_code, code)

    def test_alef_literal_first_real(self):
        rows, _ = catalog.search_items("أطفال", page=1, page_size=100)
        self.assertEqual(rows[0]["item_code"], "BZ-10692")

    def test_stable_order_across_calls(self):
        r1, _ = catalog.search_items("ركبة", page=1, page_size=100)
        r2, _ = catalog.search_items("ركبة", page=1, page_size=100)
        self.assertEqual([r["item_code"] for r in r1],
                         [r["item_code"] for r in r2])

    def test_latin_case_and_code_stable(self):
        lo, _ = catalog.search_items("bz-10202", page=1, page_size=100)
        hi, _ = catalog.search_items("BZ-10202", page=1, page_size=100)
        self.assertEqual([r["item_code"] for r in lo],
                         [r["item_code"] for r in hi])
        rows, total = catalog.search_items("ZZT-0007", page=1, page_size=100)
        self.assertEqual([r["item_code"] for r in rows], ["ZZT-0007"])
        self.assertEqual(total, 1)

    def test_expanded_respects_other_category(self):
        other = frappe.db.get_value("Item Group", {"is_group": 0, "name": ["!=", self.group]}, "name")
        self.assertTrue(other)
        _, total = self._names("ZZT ركبة", category=other)
        self.assertEqual(total, 0)

    def test_empty_with_category(self):
        rows, total = catalog.search_items("", category=self.group, page=1, page_size=100)
        expect = frappe.db.count("Item", {"disabled": 0, "item_group": self.group})
        self.assertEqual(total, expect)
        self.assertTrue(all(r["item_group"] == self.group for r in rows))

    def test_trailing_spaces_stripped(self):
        a, _ = self._names("  ZZT ركبة  ")
        b, _ = self._names("ZZT ركبة")
        self.assertEqual([r for r in a], [r for r in b])

    def test_page_clamp_and_invalid(self):
        _, total = catalog.search_items("ZZT", page=1, page_size=100)
        rows_last, t_last = catalog.search_items("ZZT", page=999, page_size=2)
        self.assertEqual(t_last, total)
        self.assertLessEqual(len(rows_last), 2)
        for bad in (0, -3, "abc"):
            rows, t = catalog.search_items("ZZT", page=bad, page_size=2)
            first, _ = catalog.search_items("ZZT", page=1, page_size=2)
            self.assertEqual([r["item_code"] for r in rows],
                             [r["item_code"] for r in first])

    def test_page_size_default_20(self):
        self.assertEqual(catalog.PAGE_SIZE, 20)

    def test_backslash_fixture_literal(self):
        names, total = self._names("ZZT باك\\سلاش")
        self.assertIn(_tname("باك\\سلاش"), names)
        self.assertLess(total, 10)

    def test_combined_specials_no_blowup(self):
        _, total = self._names("%_")
        self.assertLess(total, frappe.db.count("Item", {"disabled": 0}))

    def test_arabic_128_129(self):
        rows, total, err = catalog.search_items_with_validation("أ" * 128)
        self.assertIsNone(err)
        rows, total, err = catalog.search_items_with_validation("أ" * 129)
        self.assertEqual(rows, [])
        self.assertEqual(total, 0)
        self.assertTrue(err)

    def test_rejected_runs_no_catalog_query(self):
        seen = []

        orig = frappe.db.sql

        def counting(query, *a, **k):
            if "tabItem" in str(query or ""):
                seen.append(str(query)[:60])
            return orig(query, *a, **k)

        from unittest.mock import patch
        with patch.object(frappe.db, "sql", counting):
            catalog.search_items_with_validation("x" * 200)
        self.assertEqual(seen, [])

    def test_sqli_like_no_error_no_writes(self):
        before = frappe.db.count("Item")
        for bad in ("' OR '1'='1", "'; DROP TABLE tabItem; --", "ZZT%' OR 1=1 --"):
            names, total = self._names(bad)
            self.assertIsInstance(names, list)
        self.assertEqual(frappe.db.count("Item"), before)

    def test_search_term_escaped_in_template(self):
        import os
        p = os.path.join(os.path.dirname(catalog.__file__), "catalog.html")
        html = open(p, encoding="utf-8").read()
        self.assertIn("{{ search_term|e }}", html)
        self.assertIn("{{ search_error|e }}", html)
