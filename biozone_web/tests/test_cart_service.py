import unittest
from types import SimpleNamespace

from biozone_web.services import cart as cart_service


class TestCartValidation(unittest.TestCase):
    def test_qty_accepts_positive_numbers(self):
        self.assertEqual(cart_service._validate_qty("2.3456"), 2.346)
        self.assertEqual(cart_service._validate_qty(1), 1.0)

    def test_qty_rejects_invalid_values(self):
        for value in (0, -1, "abc", None, True, float("nan"), float("inf"), 100001):
            with self.assertRaises(cart_service.CartError, msg=repr(value)):
                cart_service._validate_qty(value)

    def test_cart_version_requires_non_negative_integer(self):
        self.assertEqual(cart_service._validate_cart_version(0), 0)
        self.assertEqual(cart_service._validate_cart_version("4"), 4)
        for value in (None, True, -1, 1.5, "1.5", "x"):
            with self.assertRaises(cart_service.CartError, msg=repr(value)):
                cart_service._validate_cart_version(value)

    def test_token_and_fingerprint_contracts(self):
        self.assertEqual(cart_service._validate_token("a" * 32), "a" * 32)
        self.assertEqual(cart_service._validate_fingerprint("A" * 64), "a" * 64)
        for value in ("short", "x" * 65, "bad token!"):
            with self.assertRaises(cart_service.CartError):
                cart_service._validate_token(value)
        for value in ("x" * 64, "g" * 64, ""):
            with self.assertRaises(cart_service.CartError):
                cart_service._validate_fingerprint(value)

    def test_optional_text_rejects_non_strings(self):
        self.assertEqual(cart_service._optional_text(None, "uom"), "")
        self.assertEqual(cart_service._optional_text(" Nos ", "uom"), "Nos")
        for value in (1, True, [], {}):
            with self.assertRaises(cart_service.CartError, msg=repr(value)):
                cart_service._optional_text(value, "uom")

    def test_source_uid_contract(self):
        self.assertEqual(cart_service._validate_source_uid("abc_DEF-123"), "abc_DEF-123")
        for value in ("short", "with space", "x" * 65, "bad!"):
            with self.assertRaises(cart_service.CartError):
                cart_service._validate_source_uid(value)


class TestCartFingerprints(unittest.TestCase):
    def test_source_fingerprint_changes_with_qty_and_uom(self):
        base = [{"item_code": "ITEM-A", "qty": 1, "uom": "Nos"}]
        same = [{"item_code": "ITEM-A", "qty": 1, "uom": "Nos"}]
        changed_qty = [{"item_code": "ITEM-A", "qty": 2, "uom": "Nos"}]
        changed_uom = [{"item_code": "ITEM-A", "qty": 1, "uom": "Box"}]
        self.assertEqual(
            cart_service._source_fingerprint(base),
            cart_service._source_fingerprint(same),
        )
        self.assertNotEqual(
            cart_service._source_fingerprint(base),
            cart_service._source_fingerprint(changed_qty),
        )
        self.assertNotEqual(
            cart_service._source_fingerprint(base),
            cart_service._source_fingerprint(changed_uom),
        )

    def test_quote_fingerprint_changes_with_total(self):
        quote = {
            "customer": "C1",
            "customer_group": "G1",
            "company": "CO",
            "currency": "EGP",
            "price_list": "Standard Selling",
            "min_limit": 0,
            "min_applies": False,
            "valid": True,
            "net_total": 10.0,
            "items": [
                {
                    "item_code": "ITEM-A",
                    "qty": 1,
                    "uom": "Nos",
                    "factor": 1.0,
                    "price": 10.0,
                    "line_total": 10.0,
                    "available": True,
                    "issues": [],
                }
            ],
        }
        first = cart_service._quote_fingerprint(quote)
        quote["net_total"] = 11.0
        self.assertNotEqual(first, cart_service._quote_fingerprint(quote))


class TestLineCalculation(unittest.TestCase):
    def test_line_total_uses_rounded_unit_price(self):
        meta = {
            "ITEM-A": SimpleNamespace(
                item_name="Item A",
                item_group="Group",
                disabled=0,
            )
        }
        display = {
            "ITEM-A": {
                "ok": True,
                "displayable": True,
                "uom": "Nos",
                "factor": 1.0,
            }
        }
        prices = {
            "ITEM-A": {
                "price": "1.005",
                "base_price": "1.005",
                "discount_percentage": 0,
            }
        }
        ctx = {
            "customer": "C1",
            "customer_group": "G1",
            "company": "CO",
            "currency": "EGP",
            "price_list": "Standard Selling",
        }
        line = cart_service._resolve_line(
            "ITEM-A",
            3,
            "Nos",
            meta,
            display,
            prices,
            ctx,
            {},
        )
        self.assertEqual(line["price"], 1.01)
        self.assertEqual(line["line_total"], 3.03)
        self.assertTrue(line["available"])


class TestMergePreview(unittest.TestCase):
    def _line(self, code="ITEM-A", qty=1, uom="Nos", available=True, issues=None):
        return {
            "item_code": code,
            "qty": qty,
            "uom": uom,
            "available": available,
            "issues": issues or [],
        }

    def test_empty_target_auto_apply_for_local_account(self):
        payload = cart_service._merge_preview_payload(
            [self._line()], [], None, True
        )
        self.assertTrue(payload["auto_apply"])
        self.assertTrue(payload["can_apply"])
        self.assertEqual(payload["merged_items"][0]["qty"], 1)

    def test_guest_never_auto_applies(self):
        payload = cart_service._merge_preview_payload(
            [self._line()], [], None, False
        )
        self.assertFalse(payload["auto_apply"])
        self.assertTrue(payload["can_apply"])

    def test_conflict_blocks_apply_and_auto(self):
        target = SimpleNamespace(
            items=[SimpleNamespace(item_code="ITEM-A", qty=1, uom="Box")]
        )
        payload = cart_service._merge_preview_payload(
            [self._line(uom="Nos")], [], target, True
        )
        self.assertFalse(payload["can_apply"])
        self.assertFalse(payload["auto_apply"])
        self.assertEqual(payload["conflicts"][0]["code"], "TARGET_UOM_CONFLICT")

    def test_duplicate_conflicts_are_deduplicated(self):
        conflict = {"item_code": "ITEM-A", "code": "NO_PRICE", "message": "x"}
        payload = cart_service._merge_preview_payload(
            [self._line()], [conflict, dict(conflict)], None, False
        )
        self.assertEqual(len(payload["conflicts"]), 1)


if __name__ == "__main__":
    unittest.main()
