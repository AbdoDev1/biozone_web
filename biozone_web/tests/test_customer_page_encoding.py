"""Regression tests for staff customers page encoding (DB-free).

Pins the single-pass JSON contract that the 3043fad XSS sweep broke:
- customers.html must embed the Python list with ONE tojson pass
  (``{{ customers|tojson }}``), never a pre-serialized string passed
  through tojson again (``customers_json|tojson`` double-encoding made
  CUSTOMERS a string and killed every page interaction).
- Rendered output with a hostile payload must parse back to an array
  with values intact and no raw </script> passthrough.
- The manual "approve as public" UI workflow must stay removed while
  the staff edit API itself is untouched (covered by integration tests).

No frappe, no database, no network — stdlib + jinja2 only:

    python3 -m pytest biozone_web/tests/test_customer_page_encoding.py -q
"""

import json
import os
import re
import unittest
from datetime import date
from decimal import Decimal

from jinja2 import Environment

EVIL_NAME = 'عميل </script><img src=x onerror="window.__xssFired=1"> "مميز"'
EVIL_GROUP = "الجمهور"


def _repo_path(*parts):
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    return os.path.normpath(os.path.join(root, *parts))


def _read_file(*parts):
    with open(_repo_path(*parts), encoding="utf-8") as fh:
        return fh.read()


def _read_template():
    return _read_file("biozone_web", "www", "staff", "customers.html")


def _read_py():
    with open(
        _repo_path("biozone_web", "www", "staff", "customers.py"),
        encoding="utf-8",
    ) as fh:
        return fh.read()


def _load_real_jsonable():
    """نفّذ _jsonable الحقيقية من customers.py بلا frappe (دالة نقية)."""
    src = _read_py()
    match = re.search(
        r"(def _jsonable\(value\):.*?)(?=\ndef |\nclass |\Z)", src, re.DOTALL
    )
    assert match, "real _jsonable not found in customers.py"
    import datetime as datetime_module

    namespace = {"datetime": datetime_module, "Decimal": Decimal}
    exec(compile(match.group(1), "customers_jsonable", "exec"), namespace)
    return namespace["_jsonable"]


def _real_customers_expression():
    """سطر CUSTOMERS الفعلي من القالب كما سيستقبله المتصفح."""
    html = _read_template()
    match = re.search(r"\{\{\s*customers\|tojson\s*\}\}", html)
    assert match, "single-pass customers|tojson not found in template"
    return match.group(0)


def _sample_customers():
    return [
        {
            "name": "CUST-0001",
            "customer_name": EVIL_NAME,
            "customer_group": EVIL_GROUP,
            "reviewed": False,
            "email": "test-customer@example.com",
            "created_display": "01 أكتوبر 2026",
            "invoice_count": 0,
            "billed_total": 0.0,
            "outstanding_total": 0.0,
            "last_invoice": None,
            "invoices": [],
        }
    ]


class TestRealSanitizeAndRender(unittest.TestCase):
    def test_no_safe_bypass_and_sanitizer_wired(self):
        html = _read_template()
        self.assertNotIn("|safe", html)
        py = _read_py()
        self.assertIn("[_jsonable(c) for c in customers]", py)
        self.assertNotIn("save-failed", html)

    def test_double_save_counter_guard_present(self):
        # wasUnclassified تُحسب قبل التحديث، وreviewed يُضبط بعده،
        # والإنقاص مشروط — فالحفظ الثاني لنفس العميل لا ينقص العداد.
        html = _read_template()
        was = html.index("wasUnclassified")
        set_true = html.index("data.reviewed = true")
        gated = html.index("if (wasUnclassified) decrementUnclassified()")
        self.assertLess(was, set_true)
        self.assertLess(set_true, gated)

    def test_real_line_renders_array_with_decimal_date_xss(self):
        sanitize = _load_real_jsonable()
        raw = _sample_customers()
        raw[0]["billed_total"] = Decimal("1234.50")
        raw[0]["raw_date"] = date(2026, 10, 8)
        cleaned = sanitize(raw)
        env = Environment()
        out = env.from_string(_real_customers_expression()).render(
            customers=cleaned
        )
        parsed = json.loads(out)
        self.assertIsInstance(parsed, list)
        self.assertEqual(parsed[0]["billed_total"], "1234.50")
        self.assertEqual(parsed[0]["raw_date"], "2026-10-08")
        self.assertEqual(parsed[0]["customer_name"], EVIL_NAME)
        self.assertNotIn("</script>", out)


class TestSinglePassEncoding(unittest.TestCase):
    def test_no_double_encoded_customers(self):
        html = _read_template()
        self.assertNotIn("customers_json|tojson", html)
        self.assertIn("{{ customers|tojson }}", html)

    def test_approve_workflow_removed_from_ui(self):
        html = _read_template()
        self.assertNotIn("approveAsPublic", html)
        self.assertNotIn("اعتماد كجمهور", html)

    def test_no_activation_leftovers(self):
        # D5: لا أثر تنشيط إطلاقًا — لا endpoint ولا زر ولا دالة؛ والبديل
        # هو زر تثبيت الفئة الحالية عبر مسار التعديل نفسه.
        html = _read_template()
        self.assertNotIn("activateAccount", html)
        self.assertNotIn("staff_activate_customer_account", html)
        self.assertNotIn("تفعيل الحساب", html)
        self.assertIn("pinCurrentGroup", html)
        self.assertIn("تثبيت الفئة الحالية", html)
        with open(
            _repo_path("biozone_web", "api.py"), encoding="utf-8"
        ) as fh:
            api_src = fh.read()
        self.assertNotIn("def staff_activate_customer_account", api_src)
        self.assertIn("def staff_set_customer_account_type", api_src)

    def test_rendered_customers_is_array_with_hostile_payload(self):
        env = Environment()
        out = env.from_string("{{ customers|tojson }}").render(
            customers=_sample_customers()
        )
        parsed = json.loads(out)
        self.assertIsInstance(parsed, list)
        self.assertEqual(parsed[0]["customer_name"], EVIL_NAME)
        self.assertNotIn("</script>", out)

    def test_old_double_encoding_yields_string_not_array(self):
        """Documents the bug: tojson over a pre-serialized string pins a str."""
        env = Environment()
        pre_serialized = json.dumps(_sample_customers(), ensure_ascii=False)
        out = env.from_string("{{ customers_json|tojson }}").render(
            customers_json=pre_serialized
        )
        self.assertIsInstance(json.loads(out), str)


class TestTojsonRecurrenceSweep(unittest.TestCase):
    """مسح فئة العطل عبر القوالب: ترميز أحادي + قيم JSON-native + سياق معرّف.

    كل assertion مربوط بسطر فعلي في القالب أو البايثون — أي انتكاس
    (عودة ترميز مزدوج، Decimal خام، متغير مشروط بلا default) يكسر
    الاختبار قبل المتصفح.
    """

    def test_pricing_group_never_undefined(self):
        html = _read_file("biozone_web", "www", "staff", "pricing.html")
        self.assertIn("selected_group|tojson if selected_group else 'null'", html)
        py = _read_file("biozone_web", "www", "staff", "pricing.py")
        self.assertIn("context.selected_group = None", py)

    def test_stock_single_pass_list(self):
        html = _read_file("biozone_web", "www", "staff", "stock.html")
        self.assertNotIn("items_json|tojson", html)
        self.assertIn("{{ picker_items|tojson }}", html)
        py = _read_file("biozone_web", "www", "staff", "stock.py")
        self.assertIn("context.picker_items = picker_items", py)
        env = Environment()
        out = env.from_string("{{ picker_items|tojson }}").render(
            picker_items=[
                {"item_code": "A", "item_name": EVIL_NAME, "units": ["Nos", "Box"]}
            ]
        )
        parsed = json.loads(out)
        self.assertIsInstance(parsed, list)
        self.assertEqual(parsed[0]["item_name"], EVIL_NAME)
        self.assertNotIn("</script>", out)

    def test_order_prep_single_pass_float_rates(self):
        html = _read_file("biozone_web", "www", "staff", "order-prep.html")
        self.assertNotIn("items_json|tojson", html)
        self.assertIn("{{ items|tojson }}", html)
        py = _read_file("biozone_web", "www", "staff", "order_prep.py")
        self.assertIn('"rate": float(r.price_list_rate or 0)', py)
        env = Environment()
        out = env.from_string("{{ items|tojson }}").render(
            items=[
                {
                    "idx": 1,
                    "name": "row1",
                    "item_code": "X",
                    "item_name": EVIL_NAME,
                    "barcodes": [],
                    "qty": 2.0,
                    "uom": "Nos",
                    "rate": 10.5,
                    "public_price": 12.0,
                    "confirmed": False,
                }
            ]
        )
        parsed = json.loads(out)
        self.assertIsInstance(parsed, list)
        self.assertEqual(parsed[0]["rate"], 10.5)
        self.assertNotIn("</script>", out)

    def test_items_panel_numeric_sanitized(self):
        py = _read_file("biozone_web", "www", "staff", "items.py")
        self.assertIn("float(price) if price is not None else None", py)
        self.assertIn('"factor": float(conv.get("factor") or 0)', py)
        html = _read_file("biozone_web", "www", "staff", "items.html")
        match = re.search(r"openItemPanel\(\{\{ .*?\|tojson \}\}\)", html)
        assert match, "panel tojson expression not found"
        expr = re.search(r"\{\{ .*?\|tojson \}\}", match.group(0)).group(0)
        env = Environment()
        out = env.from_string(expr).render(
            it={
                "item_code": "X",
                "item_name": EVIL_NAME,
                "item_group": "G",
                "brand": None,
                "stock_uom": "Nos",
                "price": 85.0,
                "disabled": 0,
                "barcodes": [],
                "display_override": "inherit",
                "conversion": {"uom": "Box", "factor": 12.0, "min_qty": 1.0},
                "image": "",
                "thumbnail": "",
            }
        )
        parsed = json.loads(out)
        self.assertEqual(parsed["price"], 85.0)
        self.assertEqual(parsed["item_name"], EVIL_NAME)
        self.assertNotIn("</script>", out)


if __name__ == "__main__":
    unittest.main()
