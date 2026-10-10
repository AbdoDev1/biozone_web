"""Acceptance tests for spelling suggestions (v1, no commit yet).

Scope: single word, zero normal results, active names only, dist<=1,
min length 4, max 3 stable-ranked, category/disabled respected.
Self-cleaning ZSG- fixtures on dev MariaDB.
"""

import unittest

import frappe
from frappe.tests.utils import FrappeTestCase

from biozone_web.www import catalog


def _tname(suffix):
    return "ZZT %s" % suffix


class TestSuggestHelpers(unittest.TestCase):
    def test_levenshtein(self):
        self.assertEqual(catalog.levenshtein("سوفي", "صوفي"), 1)
        self.assertEqual(catalog.levenshtein("شانبو", "شامبو"), 1)
        self.assertEqual(catalog.levenshtein("افركانا", "افريكانا"), 1)
        self.assertGreater(catalog.levenshtein("سوفي", "شامبو"), 1)
        self.assertEqual(catalog.levenshtein("abc", "abc"), 0)

    def test_fold(self):
        self.assertEqual(catalog.fold_for_distance("أطفال"), "اطفال")
        self.assertEqual(catalog.fold_for_distance("ركبة"), "ركبه")
        self.assertEqual(catalog.fold_for_distance("هدى"), "هدي")


class TestSuggestDB(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.g1 = "ZZT مجموعة"
        if frappe.db.exists("Item Group", cls.g1):
            for code in frappe.get_all("Item", {"item_group": cls.g1}, pluck="name"):
                frappe.delete_doc("Item", code, force=True)
            frappe.delete_doc("Item Group", cls.g1, force=True)
        grp = frappe.get_doc({
            "doctype": "Item Group", "item_group_name": cls.g1,
            "parent_item_group": "All Item Groups", "is_group": 0,
        })
        grp.insert(ignore_permissions=True)
        cls.g_sub = "ZZT فرعية"
        if frappe.db.exists("Item Group", cls.g_sub):
            for code in frappe.get_all("Item", {"item_group": cls.g_sub}, pluck="name"):
                frappe.delete_doc("Item", code, force=True)
            frappe.delete_doc("Item Group", cls.g_sub, force=True)
        sub = frappe.get_doc({
            "doctype": "Item Group", "item_group_name": cls.g_sub,
            "parent_item_group": cls.g1, "is_group": 0,
        })
        sub.insert(ignore_permissions=True)
        groups = frappe.get_all("Item Group", {"is_group": 0}, pluck="name",
                                order_by="name asc", limit_page_length=10)
        others = [g for g in groups if g != cls.g1]
        assert others, "need another leaf group on dev"
        cls.g2 = others[0]
        cls.codes = []
        defs = [
            ("ZSG-0001", _tname("صوفي"), cls.g1, 0, []),
            ("ZSG-0002", _tname("افريكانا"), cls.g1, 0, []),
            ("ZSG-0003", _tname("شامبو"), cls.g1, 0, []),
            ("ZSG-0004", _tname("توفي"), cls.g1, 0, []),
            ("ZSG-0005", _tname("كوكب"), cls.g1, 1, []),
            ("ZSG-0006", _tname("تصنيفي"), cls.g1, 0, []),
            ("ZSG-0007", _tname("جرعة 2500"), cls.g1, 0, []),
            ("ZSG-0008", _tname("جرعة 2501"), cls.g1, 0, []),
            ("ZSG-0009", _tname("جرعة 500mg"), cls.g1, 0, []),
            ("ZSG-0010", _tname("جرعة 50mg"), cls.g1, 0, []),
            ("ZSG-0011", _tname("شامبو5"), cls.g1, 0, []),
            ("ZSG-0012", _tname("سوفي"), cls.g1, 0, []),
            ("ZSG-0013", _tname("Shampoo"), cls.g1, 0, []),
            ("ZSG-0014", _tname("توفي"), cls.g1, 0, []),
            ("ZSG-0015", _tname("تفاحة"), cls.g1, 0, []),
            ("ZSG-0016", _tname("تفاحه"), cls.g1, 0, []),
            ("ZSG-0017", _tname("أسد"), cls.g1, 0, []),
            ("ZSG-0018", _tname("اسد"), cls.g1, 0, []),
            ("ZSG-0019", _tname("هدى"), cls.g1, 0, []),
            ("ZSG-0020", _tname("هدي"), cls.g1, 0, []),
            ("ZSG-0021", _tname("مقبول"), cls.g1, 0, []),
            ("ZSG-0022", _tname("مرفوض"), cls.g1, 1, []),
            ("ZSG-0023", _tname("دخيل"), cls.g2, 0, []),
            ("ZSG-0024", _tname("فرعي"), cls.g_sub, 0, []),
            ("ZSG-0025", _tname("اسبراى"), cls.g1, 0, []),
            ("ZSG-0026", _tname("اسبراي"), cls.g1, 0, []),
            ("ZSG-0027", _tname("سبرات"), cls.g1, 0, []),
            ("ZSG-0028", _tname("سبراب"), cls.g1, 0, []),
            ("ZSG-0029", _tname("سبراح"), cls.g1, 0, []),
            ("ZSG-0030", _tname("سبراك"), cls.g1, 0, []),
            ("ZSG-0031", _tname("سبراج"), cls.g1, 0, []),
            ("ZSG-0032", _tname("شامبو، ريفيرا"), cls.g1, 0, []),
            ("ZSG-0033", _tname("شامبو-ريفيرا"), cls.g1, 0, []),
            ("ZSG-0034", _tname("(شامبو)"), cls.g1, 0, []),
            ("ZSG-0035", _tname("«شامبو»"), cls.g1, 0, []),
            ("ZSG-0036", _tname("شامبو"), cls.g1, 0, []),
            ("ZSG-0037", _tname("جرعة ٢٥٠٠"), cls.g1, 0, []),
            ("ZSG-0038", _tname("جرعة ٢٥٠١"), cls.g1, 0, []),
            ("ZSG-0039", _tname("جرعة ٥٠٠ملغ"), cls.g1, 0, []),
            ("ZSG-0040", _tname("شامبو, ريفيرا"), cls.g1, 0, []),
        ]
        for code, name, group, disabled, barcodes in defs:
            if frappe.db.exists("Item", code):
                frappe.delete_doc("Item", code, force=True)
            doc = frappe.get_doc({
                "doctype": "Item", "item_code": code, "item_name": name,
                "item_group": group, "stock_uom": "Nos", "disabled": disabled,
                "barcodes": [{"barcode": b} for b in barcodes],
            })
            doc.insert(ignore_permissions=True)
            cls.codes.append(code)
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        for code in cls.codes:
            if frappe.db.exists("Item", code):
                frappe.delete_doc("Item", code, force=True)
        if frappe.db.exists("Item Group", cls.g_sub):
            frappe.delete_doc("Item Group", cls.g_sub, force=True)
        if frappe.db.exists("Item Group", cls.g1):
            frappe.delete_doc("Item Group", cls.g1, force=True)
        frappe.db.commit()
        super().tearDownClass()

    def test_normal_results_no_suggest(self):
        rows, _ = catalog.search_items("ZZT صوفي", category=self.g1,
                                       page=1, page_size=100)
        self.assertTrue(rows)
        self.assertEqual(catalog.suggest_corrections("ZZT صوفي"), [])

    def test_examples(self):
        # افركانا/شانبو: zero normal results (mechanism + zero case).
        # سوفي: planted literally (four-letter test), so normal search hits;
        # suggestion membership is asserted without a zero claim.
        for typo, want in (("افركانا", "افريكانا"),
                           ("شانبو", "شامبو")):
            rows, total = catalog.search_items(typo, category=self.g1,
                                               page=1, page_size=100)
            self.assertEqual(total, 0)
            sug = catalog.suggest_corrections(typo, category=self.g1)
            self.assertIn(want, sug)
        self.assertIn("صوفي", catalog.suggest_corrections("سوفي", category=self.g1))

    def test_no_auto_results(self):
        rows, total = catalog.search_items("افركانا", category=self.g1,
                                           page=1, page_size=100)
        self.assertEqual(total, 0)
        self.assertEqual(rows, [])

    def test_short_words_zero_queries(self):
        from unittest.mock import patch
        for q in ("شام", "صوف", "ab"):
            with patch.object(frappe.db, "sql") as m:
                m.side_effect = AssertionError("db touched by %r" % q)
                self.assertEqual(catalog.suggest_corrections(q, category=self.g1), [])

    def test_guard_rejects_digits_and_symbols_before_db(self):
        from unittest.mock import patch
        for q in ("2500", "500mg", "ZZT-9999", "12345", "ab12cd",
                  "شامبو5", "شا_مبو", "شامبو!", "٢٥٠٠", "٥٠٠ملغ", "شانبو،"):
            with patch.object(frappe.db, "sql") as m:
                m.side_effect = AssertionError("db touched by %r" % q)
                self.assertEqual(catalog.suggest_corrections(q, category=self.g1), [])

    def test_numeric_neighbors_never_suggested(self):
        self.assertEqual(catalog.suggest_corrections("٢٥٠٠", category=self.g1), [])
        self.assertEqual(catalog.suggest_corrections("٢٥٠٢", category=self.g1), [])
        self.assertEqual(catalog.suggest_corrections("٥٠٠ملع", category=self.g1), [])

    def test_candidate_with_digit_never_suggested(self):
        sug = catalog.suggest_corrections("شامبوو", category=self.g1)
        self.assertNotIn("شامبو5", sug)
        self.assertIn("شامبو", sug)

    def test_four_letters_allowed(self):
        self.assertIn("سوفي", catalog.suggest_corrections("توفي", category=self.g1))

    def test_zero_distance_excluded(self):
        self.assertEqual(catalog.suggest_corrections("افريكانا", category=self.g1), [])

    def test_every_suggestion_is_searchable(self):
        prev_user = frappe.session.user
        prev_form = frappe.local.form_dict
        frappe.set_user("Guest")
        try:
            for q in ("افركانا", "شانبو", "توفي", "سبراي", "استجمام"):
                for s in catalog.suggest_corrections(q, category=self.g1):
                    rows, total, err = catalog.search_items_with_validation(
                        s, category=self.g1, page=1, page_size=100)
                    self.assertGreater(total, 0, (q, s))
                    frappe.local.form_dict = frappe._dict(
                        {"q": s, "category": self.g1})
                    ctx = catalog.get_context(frappe._dict())
                    self.assertTrue(ctx.items, (q, s))
        finally:
            frappe.set_user(prev_user)
            frappe.local.form_dict = prev_form

    def test_fold_matches_search_normalization(self):
        # لا دالة طيّ في مسار البحث العادي (التطبيع = توسعة OR + collation)؛
        # التكافؤ هنا: متساويا الطيّ ⇒ نفس مجموعة نتائج البحث.
        for a, b in (("تفاحة", "تفاحه"), ("أسد", "اسد"), ("هدى", "هدي")):
            self.assertEqual(catalog.fold_for_distance(a),
                             catalog.fold_for_distance(b))
            ra, _ = catalog.search_items(a, category=self.g1, page=1, page_size=100)
            rb, _ = catalog.search_items(b, category=self.g1, page=1, page_size=100)
            self.assertEqual({r["item_code"] for r in ra},
                             {r["item_code"] for r in rb})

    def test_latin_letters_policy(self):
        self.assertIn("Shampoo", catalog.suggest_corrections("Shanpoo", category=self.g1))

    def test_click_through_uses_normal_path(self):
        sug = catalog.suggest_corrections("سوفي", category=self.g1)
        self.assertTrue(sug)
        rows, total = catalog.search_items(sug[0], category=self.g1,
                                           page=1, page_size=100)
        self.assertGreater(total, 0)
        rows2, _ = catalog.search_items(sug[0], category=self.g1,
                                        page=1, page_size=100)
        self.assertGreater(len(rows2), 0)

    def test_suggested_implies_searchable(self):
        # A مفعّل (مقبول) / B معطّل (مرفوض) / C فئة أخرى (دخيل) /
        # D فرعية (فرعي — المساواة لا تشملها).
        self.assertIn("مقبول", catalog.suggest_corrections("مقبوز", category=self.g1))
        rows, total = catalog.search_items("مقبول", category=self.g1,
                                           page=1, page_size=100)
        self.assertGreater(total, 0)
        for w, cat in (("مرفوض", self.g1), ("دخيل", self.g1), ("فرعي", self.g1)):
            sug = catalog.suggest_corrections(
                {"مرفوض": "مرفوط", "دخيل": "دخيز", "فرعي": "فرعز"}[w], category=cat)
            self.assertNotIn(w, sug)
        self.assertIn("دخيل", catalog.suggest_corrections("دخيز", category=self.g2))
        self.assertIn("فرعي", catalog.suggest_corrections("فرعز", category=self.g_sub))

    def test_category_none_vs_selected(self):
        unscoped = catalog.suggest_corrections("مقبوز")
        self.assertIn("مقبول", unscoped)
        scoped = catalog.suggest_corrections("مقبوز", category=self.g2)
        self.assertNotIn("مقبول", scoped)

    def test_dedupe_by_folded_key(self):
        # نقطة 4: مفتاح واحد ⇒ عرض واحد، والخانات تُملأ بالمفاتيح التالية.
        self.assertEqual(
            catalog.suggest_corrections("سبراي", category=self.g1),
            ["اسبراى", "سبراب", "سبرات"])

    def test_dedupe_does_not_waste_slots(self):
        self.assertEqual(
            catalog.suggest_corrections("سبراي", category=self.g1),
            ["اسبراى", "سبراب", "سبرات"])

    def test_matches_bruteforce_reference(self):
        import random

        def dp_lev(a, b):
            prev = list(range(len(b) + 1))
            for i, ca in enumerate(a, 1):
                cur = [i]
                for j, cb in enumerate(b, 1):
                    cur.append(min(prev[j] + 1, cur[-1] + 1,
                                   prev[j - 1] + (ca != cb)))
                prev = cur
            return prev[-1]

        pool_letters = list("ابجدهوزحطكلمنسعفصقرشتثخذضظغف")
        rnd = random.Random(42)
        pool = ["".join(rnd.choice(pool_letters) for _ in range(rnd.randint(4, 6)))
                for _ in range(200)]
        queries = ["".join(rnd.choice(pool_letters) for _ in range(rnd.randint(4, 6)))
                   for _ in range(100)]

        def reference(q):
            fq = catalog.fold_for_distance(q)
            if len(fq) < 4 or not all(
                    (c.isalpha() and c != "ـ") for c in q):
                return []
            best = {}
            for w in dict.fromkeys(pool):
                if len(w) < 4:
                    continue
                fw = catalog.fold_for_distance(w)
                if fw == fq:
                    continue
                d = dp_lev(fq, fw)
                if d <= 1:
                    if fw not in best or (d, w) < (best[fw][0], best[fw][1]):
                        best[fw] = (d, w)
            return [w for _, _, w in sorted(
                ((d, fw, w) for fw, (d, w) in best.items()))[:3]]

        for q in queries:
            self.assertEqual(catalog.rank_suggestions(
                catalog.fold_for_distance(q), pool), reference(q), q)
        rows, total = catalog.search_items("كوكب", category=self.g1,
                                           page=1, page_size=100)
        self.assertEqual(total, 0)
        self.assertEqual(catalog.suggest_corrections("كوكب", category=self.g1), [])
        self.assertEqual(catalog.suggest_corrections("تصنيفي", category=self.g2), [])

    def test_short_code_barcode_none(self):
        self.assertEqual(catalog.suggest_corrections("abc"), [])
        self.assertEqual(catalog.suggest_corrections("ZZT-9999"), [])
        self.assertEqual(catalog.suggest_corrections("12345"), [])
        self.assertEqual(catalog.suggest_corrections("سوفيسال"), [])

    def test_multiword_none(self):
        self.assertEqual(catalog.suggest_corrections("سوفي شامبو"), [])

    def test_max3_unique_stable(self):
        # FINAL expectation (d==0 excluded from point 2 on): [توفي, صوفي].
        # Interim (point 1): سوفي self-match still included — known red.
        sug1 = catalog.suggest_corrections("سوفي", category=self.g1)
        sug2 = catalog.suggest_corrections("سوفي", category=self.g1)
        self.assertEqual(sug1, sug2)
        self.assertEqual(sug1, ["توفي", "صوفي"])
        self.assertEqual(len(sug1), len(set(sug1)))

    def test_arabic_search_unchanged(self):
        rows, total = catalog.search_items("ركبة", page=1, page_size=100)
        self.assertGreater(total, 0)

    # --- real-catalog pairs (dev-data-bound; mechanism proved on fixtures above) ---
    def test_real_pair_istijmam(self):
        rows_t, total_t = catalog.search_items("استحمام", page=1, page_size=5)
        self.assertGreater(total_t, 0)
        sug = catalog.suggest_corrections("استجمام")
        self.assertIn("استحمام", sug)

    def test_real_pair_sibray(self):
        sug = catalog.suggest_corrections("سبراي")
        self.assertIn("اسبراى", sug)
        self.assertNotIn("اسبراي", sug)

    def _render_catalog(self, **over):
        import jinja2
        import os
        base = os.path.join(os.path.dirname(catalog.__file__), "..", "..")
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(os.path.abspath(base)),
            autoescape=False)
        tpl = env.get_template("biozone_web/www/catalog.html")
        ctx = dict(active_page="catalog", is_logged_in=False,
                   user_full_name=None, store_user="Guest",
                   notif_unread_count=0, sb_new_orders=0, sb_unreviewed=0,
                   categories=[], selected_category="", search_term="",
                   search_error=None, did_you_mean=[], items=[],
                   total_count_ar="٠", page=1, has_prev=False, has_next=False,
                   prev_page=0, next_page=2, csrf_token="")
        ctx.update(over)
        return tpl.render(ctx)

    def test_only_existing_words(self):
        self.assertEqual(
            catalog.suggest_corrections("توفي", category=self.g1),
            ["سوفي", "صوفي"])

    def test_suggestions_subset_of_vocabulary(self):
        pool = set()
        for (name,) in frappe.db.sql(
                "select distinct item_name from `tabItem` "
                "where disabled=0 and item_group=%(g)s", {"g": self.g1}):
            pool.update(catalog._tokenize(name))
        for w in catalog.suggest_corrections("سوفي", category=self.g1):
            self.assertIn(w, pool)

    def test_cap_three(self):
        sug = catalog.suggest_corrections("سبراي", category=self.g1)
        self.assertEqual(len(sug), 3)
        self.assertEqual(sug, ["اسبراى", "سبراب", "سبرات"])

    def test_order_stable_under_insertion_order(self):
        import random
        words = ["سبراي", "سبراح", "اسبراى", "سبراب", "اسبراي", "سبرات"]
        fq = catalog.fold_for_distance("سبراي")
        first = catalog.rank_suggestions(fq, list(words))
        rnd = random.Random(7)
        for _ in range(5):
            shuffled = list(words)
            rnd.shuffle(shuffled)
            self.assertEqual(catalog.rank_suggestions(fq, shuffled), first)

    def test_no_candidate_returns_empty_and_no_suggestion_block(self):
        html = self._render_catalog(search_term="zzzzqq", did_you_mean=[])
        self.assertNotIn("هل تقصد؟", html)
        self.assertIn("لا توجد نتائج", html)

    def test_suggestion_block_inside_empty_state(self):
        html = self._render_catalog(search_term="zzzzqq", did_you_mean=["صوفي"])
        pos_msg = html.index("لا توجد نتائج")
        pos_ask = html.index("هل تقصد؟")
        pos_sug = html.index("صوفي", pos_ask)
        self.assertLess(pos_msg, pos_ask)
        self.assertLess(pos_ask, pos_sug)
        html2 = self._render_catalog(
            search_term="x", did_you_mean=["صوفي"],
            items=[{"item_code": "A", "item_name": "B", "item_group": "C",
                    "price": 1, "display_uom": "", "thumbnail": ""}])
        self.assertNotIn("هل تقصد؟", html2)

    def _guest_context(self, q, category=""):
        prev_user = frappe.session.user
        prev_form = frappe.local.form_dict
        frappe.set_user("Guest")
        try:
            frappe.local.form_dict = frappe._dict(
                {"q": q, "category": category})
            return catalog.get_context(frappe._dict())
        finally:
            frappe.set_user(prev_user)
            frappe.local.form_dict = prev_form

    def test_template_escapes_html(self):
        evil = ["<script>alert(1)</script>", 'a"b', "x'y", "a&b"]
        html = self._render_catalog(search_term="x", did_you_mean=evil)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("a&#34;b", html)
        self.assertIn("a&amp;b", html)
        self.assertIn("q=%3Cscript%3E", html)

    def test_link_keeps_category_no_page(self):
        html = self._render_catalog(search_term="سوفي", selected_category="قفازات",
                                    did_you_mean=["صوفي"])
        self.assertIn("q=%D8%B5%D9%88%D9%81%D9%8A", html)
        self.assertIn("category=", html)
        self.assertNotIn("page=", html.replace("الصفحات", ""))

    def test_selecting_suggestion_runs_ordinary_path(self):
        ctx = self._guest_context("سوفي", self.g1)
        self.assertEqual(ctx.did_you_mean, [])
        self.assertTrue(ctx.items)

    def test_no_suggestion_when_results_exist(self):
        ctx = self._guest_context("ZZT صوفي", self.g1)
        self.assertTrue(ctx.total_count_ar)
        self.assertEqual(ctx.did_you_mean, [])

    def test_page_beyond_range_no_suggestion(self):
        ctx = self._guest_context("ZZT", self.g1)
        prev_user = frappe.session.user
        prev_form = frappe.local.form_dict
        frappe.set_user("Guest")
        try:
            frappe.local.form_dict = frappe._dict(
                {"q": "ZZT", "category": self.g1, "page": "99"})
            ctx = catalog.get_context(frappe._dict())
        finally:
            frappe.set_user(prev_user)
            frappe.local.form_dict = prev_form
        self.assertNotEqual(ctx.total_count_ar, "٠")
        self.assertEqual(ctx.did_you_mean, [])

    def test_search_error_no_suggestion(self):
        ctx = self._guest_context("x" * 129, self.g1)
        self.assertTrue(ctx.search_error)
        self.assertEqual(ctx.did_you_mean, [])
        self.assertEqual(ctx["items"], [])

    def test_arabic_matrix_planted(self):
        for a, b in (("تفاحة", "تفاحه"), ("أسد", "اسد"), ("هدى", "هدي")):
            ra, _ = catalog.search_items(a, category=self.g1, page=1, page_size=100)
            rb, _ = catalog.search_items(b, category=self.g1, page=1, page_size=100)
            self.assertGreater(len(ra), 0)
            self.assertEqual({r["item_code"] for r in ra},
                             {r["item_code"] for r in rb})

    def test_no_writes_during_suggest(self):
        orig = frappe.db.sql
        writes = []

        def counting(query, *a, **k):
            q = str(query or "")
            if q.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE"):
                writes.append(q[:60])
            return orig(query, *a, **k)

        from unittest.mock import patch
        prev_user = frappe.session.user
        prev_form = frappe.local.form_dict
        frappe.set_user("Guest")
        try:
            frappe.local.form_dict = frappe._dict({"q": "zzzzqq"})
            with patch.object(frappe.db, "sql", counting):
                catalog.get_context(frappe._dict())
        finally:
            frappe.set_user(prev_user)
            frappe.local.form_dict = prev_form
        self.assertEqual(writes, [])

    def test_punctuation_tokenization(self):
        self.assertEqual(
            catalog.suggest_corrections("شانبو", category=self.g1)[:1], ["شامبو"])

    def test_latin_comma_tokenization(self):
        self.assertIn("شامبو", catalog._tokenize("ZZT شامبو, ريفيرا"))
        self.assertIn("ريفيرا", catalog._tokenize("ZZT شامبو, ريفيرا"))
        self.assertEqual(
            catalog.suggest_corrections("شانبو", category=self.g1)[:1], ["شامبو"])

    def test_suggestions_are_letters_only(self):
        import unicodedata as _ud
        for q in ("شانبو", "سوفي", "توفي"):
            for w in catalog.suggest_corrections(q, category=self.g1):
                self.assertTrue(all(
                    ch.isalpha() or _ud.combining(ch) for ch in w), w)

    def test_hyphenated_name_yields_both_words(self):
        self.assertIn("شامبو", catalog._tokenize("ZZT شامبو-ريفيرا"))
        self.assertIn("ريفيرا", catalog._tokenize("ZZT شامبو-ريفيرا"))
        self.assertIn("ريفيرا", catalog.suggest_corrections("ريفير", category=self.g1))

    def test_perf_single_query(self):
        orig = frappe.db.sql
        calls = []

        def counting(query, *a, **k):
            calls.append(str(query or "")[:80])
            return orig(query, *a, **k)

        from unittest.mock import patch
        import time
        with patch.object(frappe.db, "sql", counting):
            t0 = time.perf_counter()
            sug = catalog.suggest_corrections("سوفي")
            ms = (time.perf_counter() - t0) * 1000
        print("SUGGEST-PERF queries=%d ms=%.1f sug=%s" % (len(calls), ms, sug))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("tabItem Price", " ".join(calls))

    def test_query_count_independent_of_candidates(self):
        from unittest.mock import patch

        def count_for(q, **kw):
            orig = frappe.db.sql
            calls = []

            def counting(query, *a, **k):
                calls.append(1)
                return orig(query, *a, **k)

            with patch.object(frappe.db, "sql", counting):
                catalog.suggest_corrections(q, **kw)
            return len(calls)

        few = count_for("كوكب", category=self.g1)
        many = count_for("سبراي", category=self.g1)
        self.assertEqual(few, 1)
        self.assertEqual(many, 1)

    def test_not_run_when_results_exist(self):
        from unittest.mock import patch
        with patch.object(catalog, "suggest_corrections",
                          side_effect=AssertionError("called on results")):
            rows, total = catalog.search_items("ZZT صوفي", category=self.g1,
                                               page=1, page_size=100)
            self.assertGreater(total, 0)
        prev_user = frappe.session.user
        prev_form = frappe.local.form_dict
        frappe.set_user("Guest")
        try:
            frappe.local.form_dict = frappe._dict({"q": "ZZT صوفي"})
            with patch.object(catalog, "suggest_corrections",
                              side_effect=AssertionError("called on results")):
                ctx = catalog.get_context(frappe._dict())
            self.assertTrue(ctx.items)
            self.assertEqual(ctx.did_you_mean, [])
        finally:
            frappe.set_user(prev_user)
            frappe.local.form_dict = prev_form

    def test_suggest_exception_keeps_page(self):
        from unittest.mock import patch
        prev_user = frappe.session.user
        prev_form = frappe.local.form_dict
        frappe.set_user("Guest")
        try:
            frappe.local.form_dict = frappe._dict({"q": "zzzzqq"})
            with patch.object(catalog, "suggest_corrections",
                              side_effect=RuntimeError("suggest exploded")):
                ctx = catalog.get_context(frappe._dict())
            self.assertEqual(ctx.did_you_mean, [])
            self.assertEqual(ctx["items"], [])
            self.assertEqual(ctx.total_count_ar, "٠")
        finally:
            frappe.set_user(prev_user)
            frappe.local.form_dict = prev_form

    def test_fold_called_once_per_unique_word(self):
        calls = []
        orig_fold = catalog.fold_for_distance

        def counting_fold(text):
            calls.append(text)
            return orig_fold(text)

        from unittest.mock import patch
        words = ["مكرر"] * 50 + ["مكرر", "فريد"]
        with patch.object(catalog, "fold_for_distance", counting_fold):
            catalog.rank_suggestions(catalog.fold_for_distance("مكر"), words)
        self.assertLessEqual(len(calls), 4)
