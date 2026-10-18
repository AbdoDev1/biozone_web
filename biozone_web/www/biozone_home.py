from urllib.parse import quote

import frappe

from biozone_web.services.brands import brand_logo_url
from biozone_web.utils import (
    get_header_context,
    redirect_staff_away_from_store,
)

# Home sections come from Biozone Home Settings (owner-managed rows: group +
# optional display label + "show products" flag + order) — rename/delete-safe,
# no code change needed to swap sections. When the settings table is empty the
# home falls back to the legacy dynamic behavior: top real categories by active
# item count, plus a top-category rail and a curated personal-care collection.
SETTINGS_PARENT = "Biozone Home Settings"
SECTION_DOCTYPE = "Biozone Home Section"
PERFUME_GROUP = "عطور"
CARE_GROUPS = [
    "شامبو",
    "كريم شعر",
    "زيت شعر",
    "جل شعر",
    "بلسم شعر",
    "شاور جل",
    "صابون",
    "صابون سائل",
    "كريمات جسم",
    "غسول وجه",
    "عناية بالبشرة",
    "صنفرة جسم",
    "ليف استحمام",
    "مزيل عرق",
    "عناية بالشفاه",
]
RAIL_SIZE = 4


def _rail_items(filters, limit=RAIL_SIZE, diversify=False, candidates=60):
    """Minimal item fetch, same shape used by the rest of the store pages.

    diversify=True picks at most one item per Item Group (scanning a wider
    candidate window) so a merged collection doesn't end up with four items
    from the same group.
    """
    rows = frappe.get_all(
        "Item",
        fields=["item_code", "item_name", "item_group"],
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


def _fallback_tiles():
    """Legacy dynamic tiles: top real item groups by active item count."""
    return [
        {
            "item_group": r.item_group,
            "label": r.item_group,
            "url": "/catalog?category=" + quote(r.item_group, safe=""),
        }
        for r in frappe.db.sql(
            """
            select item_group, count(*) as item_count
            from `tabItem`
            where disabled = 0
            group by item_group
            order by item_count desc
            limit 4
            """,
            as_dict=True,
        )
    ]


def get_context(context):
    redirect_staff_away_from_store()

    context.active_page = "home"
    context.no_cache = 1
    context.update(get_header_context())

    from biozone_web.services.store_items import enrich_store_items

    rows = _settings_rows()
    existing = _existing_groups()

    if rows:
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
    else:
        context.top_categories = _fallback_tiles()
        context.home_rails = [
            {
                "title": PERFUME_GROUP,
                "url": "/catalog?category=" + quote(PERFUME_GROUP),
                "products": enrich_store_items(
                    _rail_items({"disabled": 0, "item_group": PERFUME_GROUP})
                ),
            },
            {
                "title": "العناية الشخصية",
                "url": "/catalog",
                "products": enrich_store_items(
                    _rail_items(
                        {"disabled": 0, "item_group": ["in", CARE_GROUPS]}, diversify=True
                    )
                ),
            },
        ]

    # Product brands: real ERPNext `Brand` values, ranked by how many active
    # items carry them. Dynamic on purpose — the home never hardcodes a brand
    # list (no Brand master / no items with a brand => the section hides).
    # Carries the Brand master's `image` (if any) so the card can show a logo;
    # otherwise it falls back to the app-shipped wordmark (services.brands).
    context.top_brands = frappe.db.sql(
        """
        select i.brand as brand, b.image as image, count(*) as item_count
        from `tabItem` i
        left join `tabBrand` b on b.name = i.brand
        where i.disabled = 0 and ifnull(i.brand, '') != ''
        group by i.brand, b.image
        order by item_count desc, i.brand asc
        limit 6
        """,
        as_dict=True,
    )
    for brand in context.top_brands:
        brand["logo_url"] = brand_logo_url(brand.brand)

    return context
