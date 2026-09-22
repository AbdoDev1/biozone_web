"""Display-unit resolution for the two-unit phase (read-only + settings).

Single source of truth for "which unit does this customer see for this
item": item flag (inherit/small_only/large_only) first, then the customer
group's display_unit, then the default (small). Prices stay single
(small-unit public price); the large price is always computed.

No writes here except the staff settings endpoint. No Frappe Data Import,
no bulk SQL writes — one ORM save per settings row like the rest.
"""

from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe import _


def money2(value):
	"""Round half up to 2 decimals (currency display + large-unit price)."""
	return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def get_group_display_unit(customer_group):
	"""Return 'large' or 'small' for a customer group (default 'small').

	Inactive/disabled/unknown groups fall back to 'small' — display must
	never break on a missing setting.
	"""
	if not (customer_group or "").strip():
		return "small"
	group = frappe.db.get_value(
		"Customer Group", customer_group, ["disabled", "display_unit"], as_dict=True
	)
	if not group or group.disabled:
		return "small"
	mode = (group.display_unit or "").strip()
	return "large" if mode == "large" else "small"


def _item_unit_data(codes):
	"""Batch-read stock_uom/override/conversions for items (read-only)."""
	codes = [c for c in dict.fromkeys((c or "").strip() for c in (codes or [])) if c]
	out = {}
	if not codes:
		return out
	for r in frappe.get_all(
		"Item",
		filters={"item_code": ["in", codes]},
		fields=["item_code", "stock_uom", "display_override", "disabled"],
	):
		out[r.item_code] = {
			"stock_uom": r.stock_uom or "Nos",
			"override": (r.display_override or "inherit").strip() or "inherit",
			"disabled": bool(r.disabled),
			"conversions": [],
		}
	if not out:
		return out
	for r in frappe.get_all(
		"UOM Conversion Detail",
		filters={"parent": ["in", list(out)], "parenttype": "Item"},
		fields=["parent", "uom", "conversion_factor", "min_qty"],
		order_by="idx asc",
	):
		if r.uom and frappe.utils.flt(r.conversion_factor) > 0:
			out[r.parent]["conversions"].append(
				{
					"uom": r.uom,
					"factor": frappe.utils.flt(r.conversion_factor),
					"min_qty": frappe.utils.flt(r.min_qty)
					if r.min_qty not in (None, "")
					else 1.0,
				}
			)
	return out


def resolve_item_display(item_code, customer_group=None, _cache=None):
	"""Resolve the display unit for one item + group.

	Returns {ok, uom, factor, min_qty, kind, displayable, reason}.
	displayable=False means: configured large but no valid conversion —
	the item must be hidden with a setup message, never priced by guess.
	"""
	data = (_cache or {}).get(item_code)
	if data is None:
		data = _item_unit_data([item_code]).get(item_code)
	if not data:
		return {"ok": False, "reason": _("الصنف غير موجود")}
	override = data["override"]
	if override == "small_only":
		kind = "small"
	elif override == "large_only":
		kind = "large"
	else:
		from biozone_web.utils import get_customer_price_group

		group = customer_group
		if group is None:
			try:
				group = get_customer_price_group()
			except Exception:
				group = None
		kind = get_group_display_unit(group)
	if kind == "large":
		# A genuine large unit must differ from the stock unit: ERPNext
		# auto-creates a trivial (stock_uom, 1) row for every item, which
		# is NOT a sellable large unit. Exception: an explicit large_only
		# override on a single-unit item sells its stock unit as large.
		stock = data["stock_uom"]
		distinct = [c for c in (data["conversions"] or []) if (c["uom"] or "").strip() != stock]
		if not distinct:
			if override == "large_only":
				return {
					"ok": True,
					"displayable": True,
					"kind": "large",
					"uom": stock,
					"factor": 1.0,
					"min_qty": 1.0,
					"reason": "",
				}
			return {
				"ok": True,
				"displayable": False,
				"kind": "large",
				"uom": stock,
				"factor": 1.0,
				"min_qty": 1.0,
				"reason": _("لا توجد وحدة كبرى صالحة لهذا الصنف"),
			}
		conv = distinct[0]
		return {
			"ok": True,
			"displayable": True,
			"kind": "large",
			"uom": conv["uom"],
			"factor": conv["factor"],
			"min_qty": conv["min_qty"] if conv["min_qty"] > 0 else 1.0,
			"reason": "",
		}
	return {
		"ok": True,
		"displayable": True,
		"kind": "small",
		"uom": data["stock_uom"],
		"factor": 1.0,
		"min_qty": 1.0,
		"reason": "",
	}


def resolve_items_display(codes, customer_group=None):
	"""Batch version (one batched read for all items)."""
	data = _item_unit_data(codes)
	return {
		code: resolve_item_display(code, customer_group, _cache=data) for code in (codes or [])
	}


@frappe.whitelist()
def front_display_info(item_codes):
	"""Storefront: display unit per item for the current customer's group."""
	from biozone_web.utils import get_customer_price_group

	if isinstance(item_codes, str):
		try:
			item_codes = frappe.parse_json(item_codes)
		except Exception:
			item_codes = []
	try:
		group = get_customer_price_group()
	except Exception:
		group = None
	return {"ok": True, "group": group, "items": resolve_items_display(item_codes or [], group)}


@frappe.whitelist()
def staff_get_display_settings():
	"""Staff: every leaf group with its display unit (for the settings page)."""
	from biozone_web.utils import require_staff_access

	require_staff_access()
	groups = frappe.get_all(
		"Customer Group",
		filters={"is_group": 0},
		fields=["name", "disabled", "display_unit"],
		order_by="name asc",
	)
	return {
		"ok": True,
		"groups": [
			{
				"name": g.name,
				"disabled": bool(g.disabled),
				"display_unit": (g.display_unit or "small").strip() or "small",
			}
			for g in groups
		],
	}


@frappe.whitelist(methods=["POST"])
def staff_save_display_setting(customer_group, display_unit):
	"""Staff: set one group's display unit (small/large). ORM save only."""
	from biozone_web.utils import require_staff_access

	require_staff_access()
	customer_group = (customer_group or "").strip()
	display_unit = (display_unit or "").strip()
	if not customer_group or not frappe.db.exists("Customer Group", customer_group):
		return {"ok": False, "error": _("فئة العميل غير موجودة")}
	if display_unit not in ("small", "large"):
		return {"ok": False, "error": _("وحدة العرض يجب أن تكون صغرى أو كبرى")}
	frappe.db.set_value("Customer Group", customer_group, "display_unit", display_unit)
	frappe.db.commit()
	return {"ok": True, "group": customer_group, "display_unit": display_unit}
