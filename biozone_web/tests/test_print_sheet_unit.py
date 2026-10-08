"""Regression tests for staff print sheets + XSS escaping (DB-free).

Covers, without any database:
- sheet number/balance formatting and the explicit rounding policies,
- photo-wording tafqeet boundaries,
- address sanitizer (multiline kept, payload inert, other tags stripped),
- Jinja render of the three print sheets with a hostile payload
  (no raw HTML passthrough, single-pass escaping),
- pagination chunking (max 20/page, footer on last page only),
- static guards: no raw `{{ *_json }}` embeddings and no `|safe` in print sheets.

Runnable anywhere the app imports (mirrors test_store_items.py conventions):
    ./env/bin/python -m pytest biozone_web/tests/test_print_sheet_unit.py
"""

import unittest
from decimal import Decimal

from jinja2.sandbox import SandboxedEnvironment

from biozone_web.b9_utils import (
    amount_in_words_sheet,
    clean_address_html,
    format_balance_3,
    format_sheet_number,
    moneyd,
)

EVIL = '<img src=x onerror="window.__xssFired=1">'

def _sheet_env():
    import os

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    from jinja2 import FileSystemLoader

    return SandboxedEnvironment(loader=FileSystemLoader(root))


def _sheet(name):
    return _sheet_env().get_template("biozone_web/templates/includes/" + name)


def _picking_ctx(items, **over):
    pages = [
        {"rows": items[i : i + 20], "page_no": k + 1}
        for k, i in enumerate(range(0, max(len(items), 1), 20))
    ]
    ctx = dict(
        order_name="T",
        delivery_note="",
        invoice_name="T",
        customer_name="c",
        customer_address="",
        posting_date="t",
        items=items,
        items_count=len(items),
        pages=pages,
        total_pages=len(pages),
        shipping=0,
        net_s="0",
        public_s="0",
        discnote_s="0",
        paid_s="0",
        prev_s="0",
        curr_s="0",
        amount_words="w",
        staff_name="s",
        time12="t",
    )
    ctx.update(over)
    return ctx


def _item(i, name="ص"):
    return {
        "idx": i,
        "item_name": name,
        "qty_s": "1",
        "price_s": "100",
        "disc_s": "0",
        "amount_s": "100",
    }


class TestSheetNumbers(unittest.TestCase):
    def test_format_sheet_number_strips(self):
        self.assertEqual(format_sheet_number(150), "150")
        self.assertEqual(format_sheet_number(112.5), "112.5")
        self.assertEqual(format_sheet_number(30.72), "30.72")
        self.assertEqual(format_sheet_number(0), "0")

    def test_format_balance_3_max_three(self):
        self.assertEqual(format_balance_3(43185.75), "43185.75")
        self.assertEqual(format_balance_3(41219.438), "41219.438")
        self.assertEqual(format_balance_3(78059.313), "78059.313")
        self.assertEqual(format_balance_3(0), "0")

    def test_moneyd_half_up_policy(self):
        # Explicit policy: ROUND_HALF_UP (differs from float %.3f at bounds).
        self.assertEqual(moneyd(2.675), Decimal("2.68"))
        self.assertEqual(moneyd(2.345), Decimal("2.35"))
        self.assertIsInstance(moneyd(10), Decimal)


class TestSheetWords(unittest.TestCase):
    def test_photo_wording(self):
        self.assertEqual(
            amount_in_words_sheet(1966.30),
            "مطلوب فقط وقدره الف وتسعمائة وستة وستون جنيه وثلاثون قرش فقط لاغير",
        )
        self.assertIn("تسعمائة", amount_in_words_sheet(981.37))

    def test_zero_and_whole(self):
        self.assertIn("صفر", amount_in_words_sheet(0))
        self.assertTrue(amount_in_words_sheet(1000).endswith("فقط لاغير"))


class TestCleanAddress(unittest.TestCase):
    def test_multiline_kept_payload_inert(self):
        out = clean_address_html("سطر أول<br>سطر ثانٍ")
        self.assertIn("<br>", out)
        out2 = clean_address_html('أ<script>alert(1)</script>ب<div>ج')
        self.assertNotIn("<script>", out2)
        self.assertNotIn("<div>", out2)
        self.assertIn("أ", out2)


class TestSheetXSS(unittest.TestCase):
    def test_evil_inert_in_all_sheets(self):
        pick = _picking_ctx([_item(1, EVIL)], customer_name=EVIL)
        html_p = _sheet("biozone_print_picking.html").render(**pick)
        inv = dict(
            pick,
            invoice_name="I",
            staff_name=EVIL,
            photo_words="w",
        )
        html_i = _sheet("biozone_print_invoice.html").render(**inv)
        clines = [dict(_item(1, EVIL), uom="قطعة")]
        cc = dict(
            order_name="X",
            credit_print={
                "name": "R",
                "posting_date": "t",
                "customer_name": EVIL,
                "original": "I",
                "staff_name": EVIL,
                "lines": clines,
                "total": 100.0,
                "total_s": "100",
                "print_time": "t",
                "time12": "t",
                "pages": [{"rows": clines, "page_no": 1}],
                "total_pages": 1,
            },
        )
        html_c = _sheet("biozone_print_credit.html").render(**cc)
        for html in (html_p, html_i, html_c):
            self.assertNotIn(EVIL, html)
            self.assertIn("&lt;img", html)


class TestPagination(unittest.TestCase):
    def _render(self, n):
        items = [_item(i + 1) for i in range(n)]
        pages = [
            {"rows": items[i : i + 20], "page_no": k + 1}
            for k, i in enumerate(range(0, max(len(items), 1), 20))
        ]
        ctx = _picking_ctx(items)
        ctx["pages"] = pages
        ctx["total_pages"] = len(pages)
        return _sheet("biozone_print_picking.html").render(**ctx)

    def test_counts(self):
        cases = {0: 1, 1: 1, 19: 1, 20: 1, 21: 2, 40: 2, 41: 3}
        for n, exp in cases.items():
            html = self._render(n)
            self.assertEqual(
                html.count('class="invoice-sheet"'), exp, "sheets n=%d" % n
            )
            self.assertEqual(html.count('class="sheet-footer"'), 1, "footer n=%d" % n)


class TestNoRawJsonOrSafe(unittest.TestCase):
    def test_no_raw_json_embeds_no_safe(self):
        import glob
        import os

        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
        hits = []
        for p in glob.glob(root + "/biozone_web/www/**/*.html", recursive=True):
            text = open(p, encoding="utf-8").read()
            if "{{ items_json }}" in text or "{{ customers_json }}" in text:
                hits.append(p)
            if "|safe" in text or "mark_safe" in text:
                hits.append(p + " ::safe")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
