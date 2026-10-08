"""Direct tests for category eligibility, creation paths, isolation,
audit-rollback and guard wiring (site DB).

Requires a bench site database (``bench --site <site> run-tests
--app biozone_web``). Every test skips loudly (``skipTest``) when its
prerequisite is absent, and every mutation is restored in tearDown —
created Customers / Customer Groups are deleted per test with fixed
test identities (``test-cat-elig-*@example.com``) so reruns collide
with nothing and leave no orphans.

Conventions mirror test_min_order_integration.py. No production data:
all fixtures use the ``TST-`` prefix and example.com identities.
"""

import unittest
from unittest.mock import patch

import frappe

from biozone_web import api as biozone_api
from biozone_web import utils as biozone_utils

TEST_DOMAIN = "example.com"


def _mail(tag):
    return f"test-cat-elig-{tag}@{TEST_DOMAIN}"


def _site_ready():
    try:
        frappe.db.get_value("Customer Group", "x", "name")
        return True
    except Exception:
        return False


def _public_group():
    return biozone_utils.get_public_customer_group()


class EligibilitySiteCase(unittest.TestCase):
    """Base: loud skips + created-doc cleanup registry."""

    def setUp(self):
        if not _site_ready():
            self.skipTest("no site database available")
        self._created_customers = []
        self._created_groups = []
        self._created_users = []

    def tearDown(self):
        for name in self._created_customers:
            try:
                if frappe.db.exists("Customer", name):
                    frappe.delete_doc(
                        "Customer", name, ignore_permissions=True, force=True
                    )
            except Exception:
                pass
        for email in self._created_users:
            try:
                if frappe.db.exists("User", email):
                    frappe.delete_doc("User", email, ignore_permissions=True)
            except Exception:
                pass
        for name in self._created_groups:
            try:
                if frappe.db.exists("Customer Group", name):
                    frappe.delete_doc(
                        "Customer Group", name, ignore_permissions=True
                    )
            except Exception:
                pass
        frappe.db.commit()

    # -- helpers ------------------------------------------------------
    def _ensure_user(self, email, tag):
        if not frappe.db.exists("User", email):
            user = frappe.get_doc(
                {
                    "doctype": "User",
                    "email": email,
                    "first_name": "TST",
                    "last_name": tag,
                    "send_welcome_email": 0,
                }
            )
            user.insert(ignore_permissions=True)
            self._created_users.append(email)
            frappe.db.commit()

    def _make_customer(self, tag, group, assigned=0):
        email = _mail(tag)
        self._ensure_user(email, tag)
        name = biozone_utils.create_customer_for_user(email, full_name=f"TST {tag}")
        self._created_customers.append(name)
        frappe.db.set_value("Customer", name, "customer_group", group)
        frappe.db.set_value("Customer", name, "category_assigned_by_staff", assigned)
        frappe.db.commit()
        return name, email

    def _make_group(self, tag, disabled=False, is_group=False):
        name = f"TST-{tag}"
        if not frappe.db.exists("Customer Group", name):
            doc = frappe.get_doc(
                {
                    "doctype": "Customer Group",
                    "customer_group_name": name,
                    "is_group": 1 if is_group else 0,
                }
            )
            doc.insert(ignore_permissions=True)
            self._created_groups.append(name)
        frappe.db.set_value("Customer Group", name, "disabled", 1 if disabled else 0)
        frappe.db.commit()
        return name

    # -- D2 eligibility matrix ----------------------------------------
    def test_public_unclassified_eligible(self):
        public = _public_group()
        if not public:
            self.skipTest("no public customer group configured")
        name, _email = self._make_customer("pub0", public, assigned=0)
        self.assertTrue(biozone_utils.is_customer_order_eligible(name))

    def test_public_classified_eligible(self):
        public = _public_group()
        if not public:
            self.skipTest("no public customer group configured")
        name, _email = self._make_customer("pub1", public, assigned=1)
        self.assertTrue(biozone_utils.is_customer_order_eligible(name))

    def test_disabled_group_rejected(self):
        group = self._make_group("disabled", disabled=True)
        name, _email = self._make_customer("dis", group, assigned=1)
        self.assertFalse(biozone_utils.is_customer_order_eligible(name))

    def test_group_node_rejected(self):
        group = self._make_group("node", is_group=True)
        name, _email = self._make_customer("node", group, assigned=1)
        self.assertFalse(biozone_utils.is_customer_order_eligible(name))

    def test_unlinked_rejected(self):
        self.assertFalse(biozone_utils.is_customer_order_eligible("__no_such_customer__"))

    # -- creation paths parity (D4) ------------------------------------
    def test_creation_paths_agree(self):
        public = _public_group()
        if not public:
            self.skipTest("no public customer group configured")
        email = _mail("parity")
        self._ensure_user(email, "parity")
        first = biozone_utils.create_customer_for_user(email, full_name="TST parity")
        self._created_customers.append(first)
        # Lazy path resolves the same binding without duplicating.
        again = biozone_utils.create_customer_for_user(email, full_name="TST parity")
        self.assertEqual(first, again)
        self.assertEqual(
            frappe.db.get_value("Customer", first, "customer_group"), public
        )
        self.assertEqual(
            frappe.utils.cint(
                frappe.db.get_value(
                    "Customer", first, "category_assigned_by_staff"
                )
            ),
            0,
        )

    # -- single-change isolation ----------------------------------------
    def test_single_change_touches_only_chosen(self):
        public = _public_group()
        if not public:
            self.skipTest("no public customer group configured")
        tier = self._make_group("tier")
        chosen, _e1 = self._make_customer("chosen", public, assigned=0)
        other, _e2 = self._make_customer("other", public, assigned=0)
        with patch.object(biozone_utils, "require_staff_access", lambda: None):
            saved = biozone_api._set_customer_account_type(chosen, tier)
        self.assertEqual(saved["customer_group"], tier)
        self.assertEqual(
            frappe.db.get_value("Customer", other, "customer_group"), public
        )
        self.assertEqual(
            frappe.utils.cint(
                frappe.db.get_value("Customer", other, "category_assigned_by_staff")
            ),
            0,
        )

    # -- D6 audit rollback -----------------------------------------------
    def test_audit_failure_rolls_back_change(self):
        public = _public_group()
        if not public:
            self.skipTest("no public customer group configured")
        tier = self._make_group("tier2")
        name, _email = self._make_customer("audit", public, assigned=0)
        with patch.object(biozone_utils, "require_staff_access", lambda: None):
            with patch(
                "frappe.model.document.Document.add_comment",
                side_effect=RuntimeError("audit store down"),
            ):
                with self.assertRaises(frappe.ValidationError):
                    biozone_api._set_customer_account_type(name, tier)
        self.assertEqual(
            frappe.db.get_value("Customer", name, "customer_group"), public
        )

    # -- guard wiring ------------------------------------------------------
    def test_unauthorized_set_rejected(self):
        public = _public_group()
        if not public:
            self.skipTest("no public customer group configured")
        name, _email = self._make_customer("guard", public, assigned=0)
        with patch.object(
            biozone_utils,
            "require_staff_access",
            side_effect=frappe.PermissionError("denied"),
        ):
            with self.assertRaises(frappe.PermissionError):
                biozone_api.staff_set_customer_account_type(name, public)
        self.assertEqual(
            frappe.db.get_value("Customer", name, "customer_group"), public
        )


if __name__ == "__main__":
    unittest.main()
