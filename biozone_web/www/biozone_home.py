from urllib.parse import quote

import frappe

from biozone_web.services.brands import brand_logo_url
from biozone_web.utils import (
    get_header_context,
    redirect_staff_away_from_store,
)

# Home product rails: the top real category + one curated personal-care
# collection (several care Item Groups merged into a single rail).
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


def get_context(context):
    redirect_staff_away_from_store()

    context.active_page = "home"
    context.no_cache = 1
    context.update(get_header_context())

    # Top categories: real item groups, ranked by how many active items
    # they contain. Dynamic on purpose — avoids hardcoding category names
    # that could drift from the actual Item Group data.
    context.top_categories = frappe.db.sql(
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

    # Two product rails replacing the old single "featured" strip: one real
    # top category + one curated care collection. Both reuse the exact same
    # enrichment as /catalog (server-side pricing + image derivatives), so the
    # cards stay commercially and visually identical to the catalog.
    from biozone_web.services.store_items import enrich_store_items

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

    return context
