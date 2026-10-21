from urllib.parse import quote

import frappe

from biozone_web.services.brands import brand_logo_url
from biozone_web.utils import (
    get_header_context,
    redirect_staff_away_from_store,
)

# Home sections and the brand strip come from Biozone Home Settings
# (owner-managed rows: group/brand + optional display label + "show products"
# flag + order) — rename/delete-safe, no code change needed to swap content.
# Empty settings mean an empty homepage area: there is no dynamic fallback.
SETTINGS_PARENT = "Biozone Home Settings"
SECTION_DOCTYPE = "Biozone Home Section"
BRAND_DOCTYPE = "Biozone Home Brand"
RAIL_SIZE = 4


def _rail_items(filters, limit=RAIL_SIZE, diversify=False, candidates=60):
    """Minimal item fetch, same shape used by the rest of the store pages.

    diversify=True picks at most one item per Item Group (scanning a wider
    candidate window) so a merged collection doesn't end up with four items
    from the same group.
    """
    rows = frappe.get_all(
        "Item",
        fields=["item_code", "item_name", "item_group", "brand"],
        filters=filters,
        order_by="creation asc",
        limit_page_length=candidates if diversify else limit,
    )
    if not diversify:
        return rows
    picked, seen = [], set()
    for row in rows:
        if row["item_group"] in seen:
            continue
        seen.add(row["item_group"])
        picked.append(row)
        if len(picked) >= limit:
            break
    return picked


def _settings_rows():
    """Owner-typed section rows in display order (read as plain table data:
    the storefront runs as Guest and this content is public by design)."""
    return frappe.db.get_all(
        SECTION_DOCTYPE,
        filters={"parent": SETTINGS_PARENT, "parenttype": SETTINGS_PARENT},
        fields=["item_group", "label", "show_products", "sort_order"],
        order_by="sort_order asc, idx asc",
    )


def _settings_brand_rows():
    """Owner-typed brand rows in display order (same plain-table read)."""
    return frappe.db.get_all(
        BRAND_DOCTYPE,
        filters={"parent": SETTINGS_PARENT, "parenttype": SETTINGS_PARENT},
        fields=["brand", "label", "sort_order"],
        order_by="sort_order asc, idx asc",
    )


def _existing_brands(rows):
    """{brand: image} for the rows' Brand values that still exist."""
    names = {(row.get("brand") or "").strip() for row in rows or []} - {""}
    if not names:
        return {}
    return {
        b.name: b.image
        for b in frappe.db.get_all(
            "Brand", filters={"name": ["in", sorted(names)]}, fields=["name", "image"]
        )
    }


def _existing_groups():
    return set(frappe.db.get_all("Item Group", pluck="name"))


def _display_label(row):
    return (row.get("label") or "").strip() or row["item_group"]


def build_tiles(rows, existing_groups):
    """Tiles from settings rows; rows pointing at deleted groups are dropped."""
    tiles = []
    for row in rows or []:
        group = (row.get("item_group") or "").strip()
        if not group or group not in existing_groups:
            continue
        tiles.append(
            {
                "item_group": group,
                "label": _display_label(row),
                "url": "/catalog?category=" + quote(group, safe=""),
            }
        )
    return tiles


def build_rail_defs(rows, existing_groups):
    """Rail definitions (group/title/url) for rows flagged "show products"."""
    defs = []
    for row in rows or []:
        group = (row.get("item_group") or "").strip()
        if not group or group not in existing_groups or not row.get("show_products"):
            continue
        defs.append(
            {
                "group": group,
                "title": _display_label(row),
                "url": "/catalog?category=" + quote(group, safe=""),
            }
        )
    return defs


def build_top_brands(rows, existing_brands):
    """Brand cards from settings rows; rows pointing at deleted brands are
    dropped and a brand listed twice keeps its first row (no cap)."""
    cards, seen = [], set()
    for row in rows or []:
        brand = (row.get("brand") or "").strip()
        if not brand or brand in seen or brand not in existing_brands:
            continue
        seen.add(brand)
        cards.append(
            {
                "brand": brand,
                "image": existing_brands[brand],
                "label": (row.get("label") or "").strip() or brand,
                "logo_url": brand_logo_url(brand),
            }
        )
    return cards


def get_context(context):
    redirect_staff_away_from_store()

    context.active_page = "home"
    context.no_cache = 1
    context.update(get_header_context())

    from biozone_web.services.store_items import enrich_store_items

    rows = _settings_rows()
    existing = _existing_groups()

    context.top_categories = build_tiles(rows, existing)
    context.home_rails = [
        {
            "title": d["title"],
            "url": d["url"],
            "products": enrich_store_items(
                _rail_items({"disabled": 0, "item_group": d["group"]})
            ),
        }
        for d in build_rail_defs(rows, existing)
    ]

    # Product brands: owner rows (Biozone Home Brand) in display order.
    # No fallback: an empty table hides the brand strip.
    brand_rows = _settings_brand_rows()
    context.top_brands = build_top_brands(brand_rows, _existing_brands(brand_rows))

    return context
