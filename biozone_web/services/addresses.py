"""Customer shipping address service (single address, staff-written).

- One Address per Customer, linked via Dynamic Link, with forced
  address_type="Shipping", country="Egypt", is_shipping_address=1 and
  is_primary_address=1. The framework itself unsets the same flags on
  any older address of the same party (validate_preferred_address).
- Save = create-or-update: a second save updates the same doc and
  disables any stray extras, never duplicates. Delete = disable, so
  history is preserved.
- The customer never writes: no customer-facing endpoint exists; the
  account page renders server-side read-only. Staff writes through a
  single whitelisted wrapper guarded by require_staff_access.
- Any customer/link identity from the request is ignored. The Customer
  always comes from the caller's explicit argument (staff passes the
  target name; the account page passes the session-derived one).
- No commit inside — the caller keeps its single commit.
"""

import re

import frappe
from frappe import _

from biozone_web.utils import (
	find_customer_for_current_user,
	is_unique_customer_binding,
)

ADDRESS_COUNTRY = "Egypt"
ADDRESS_TYPE = "Shipping"

PHONE_PATTERN = re.compile(r"^[+\d][\d\s\-]{5,19}$")

EMPTY_MESSAGE = _("بيانات العنوان غير مكتملة")
UNKNOWN_CUSTOMER_MESSAGE = _("العميل غير موجود")
PHONE_MESSAGE = _("رقم الهاتف غير صالح")


def _clean(value, label):
	"""Stripped text, throwing when empty. Landmark stays optional."""
	text = (value or "").strip()
	if not text:
		frappe.throw(_("{0} مطلوب").format(label))
	return text


def _clean_phone(value):
	phone = (value or "").strip()
	if not phone or not PHONE_PATTERN.match(phone):
		frappe.throw(PHONE_MESSAGE)
	return phone


def _linked_addresses(customer):
	"""Non-disabled addresses linked to the customer, preferred first."""
	return frappe.get_all(
		"Address",
		filters=[
			["Dynamic Link", "link_doctype", "=", "Customer"],
			["Dynamic Link", "link_name", "=", customer],
			["Dynamic Link", "parenttype", "=", "Address"],
			["disabled", "=", 0],
		],
		fields=[
			"name",
			"address_title",
			"address_type",
			"address_line1",
			"address_line2",
			"city",
			"state",
			"country",
			"phone",
			"is_primary_address",
			"is_shipping_address",
		],
		order_by="is_primary_address DESC, creation ASC",
	)


def get_customer_shipping_address(customer):
	"""Single linked shipping address as a dict, or None.

	No session check here — the caller owns authorization (staff page
	passes any target; the account page passes the session-derived
	customer via get_my_shipping_address).
	"""
	if not customer or not frappe.db.exists("Customer", customer):
		return None
	rows = _linked_addresses(customer)
	return rows[0] if rows else None


def get_my_shipping_address():
	"""Session customer's address, or None.

	Same guards as the orders list: session-derived customer only, and
	nothing at all for a shared Customer binding (my_orders.py).
	"""
	customer = find_customer_for_current_user()
	if not customer or not is_unique_customer_binding(customer, frappe.session.user):
		return None
	return get_customer_shipping_address(customer)


def build_brief_display(address):
	"""Customer-facing abbreviated form: street + landmark only.

	No governorate, city, or country — ever. Phone stays a separate
	line in the template, not part of this string.
	"""
	if not address:
		return ""
	street = (address.get("address_line1") or "").strip()
	landmark = (address.get("address_line2") or "").strip()
	if street and landmark:
		return _("شارع {0} — {1}").format(street, landmark)
	return street or landmark


def staff_save_customer_address(customer, governorate, city, street, landmark, phone):
	"""Create-or-update the customer's single shipping address.

	Staff-only: the whitelisted wrapper enforces require_staff_access;
	writes run elevated (ignore_permissions) because authorization is
	the explicit staff guard plus the explicit customer argument —
	never DocPerm, never anything from the request body.
	"""
	if not customer or not frappe.db.exists("Customer", customer):
		frappe.throw(UNKNOWN_CUSTOMER_MESSAGE)

	governorate = _clean(governorate, _("المحافظة"))
	city = _clean(city, _("المدينة"))
	street = _clean(street, _("الشارع"))
	phone = _clean_phone(phone)
	landmark = (landmark or "").strip()
	if not (street or landmark):
		frappe.throw(EMPTY_MESSAGE)

	title = frappe.db.get_value("Customer", customer, "customer_name") or customer
	fields = {
		"address_title": title,
		"address_type": ADDRESS_TYPE,
		"address_line1": street,
		"address_line2": landmark,
		"city": city,
		"state": governorate,
		"country": ADDRESS_COUNTRY,
		"phone": phone,
		"is_shipping_address": 1,
		"is_primary_address": 1,
		"links": [{"link_doctype": "Customer", "link_name": customer}],
	}

	existing = _linked_addresses(customer)
	if existing:
		name = existing[0]["name"]
		doc = frappe.get_doc("Address", name)
		doc.update(fields)
		doc.save(ignore_permissions=True)
		created = False
		for extra in existing[1:]:
			frappe.db.set_value("Address", extra["name"], "disabled", 1)
	else:
		doc = frappe.get_doc({"doctype": "Address", **fields})
		doc.insert(ignore_permissions=True)
		name = doc.name
		created = True

	frappe.db.set_value("Customer", customer, "customer_primary_address", name)
	return {"ok": True, "address": name, "created": created}
