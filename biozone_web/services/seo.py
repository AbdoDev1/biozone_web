"""SEO meta tags for the standalone storefront/staff pages (Option A).

Why a central map instead of per-page Python: every ``www/*.html`` page is a
standalone document (it does not extend ``templates/base.html``), so Frappe's
own ``MetaTags`` pipeline never runs on it. The tags are therefore rendered by
``templates/includes/seo_meta.html`` from ``context.seo``.

The page map below is the single place that owns each static page's title and
description. The site name is never hardcoded for store pages: it comes from
``Website Settings.app_name`` (single source), so renaming the site in Desk
updates every store page title and ``og:site_name`` at once.

Hook wiring: ``update_website_context`` runs for template pages *after* the
page's ``get_context`` (so ``context.item`` / ``context.print_kind`` are
already available) and *before* rendering.
"""

import frappe
from frappe.utils import get_url

DEFAULT_SITE_NAME = "Biozone"

STORE_DESCRIPTION = (
    "Biozone شريكك الموثوق للمستلزمات الطبية والعناية؛ "
    "منتجات أصلية، أسعار تنافسية، وتوصيل سريع."
)

ITEM_FALLBACK_TITLE = "الصنف غير متاح"
ITEM_FALLBACK_DESCRIPTION = "تعرف على المستلزمات الطبية والعناية من Biozone."

# Staff pages keep their existing Arabic titles verbatim: they are internal,
# index-blocked screens and the owner reviews them visually per deploy.
STAFF_TITLES = {
	"staff/login": "بيوزون | دخول الموظفين",
	"staff/dashboard": "بيوزون | لوحة التحكم",
	"staff/customers": "العملاء - بيوزون",
	"staff/customer-detail": "تفاصيل العميل - بيوزون",
	"staff/items": "الأصناف - بيوزون Biozone",
	"staff/orders": "بيوزون | تجهيز الطلبات",
	"staff/order-prep": "بيوزون | تجهيز وفحص الطلب بالباركود",
	"staff/order-delivery": "بيوزون | تسليم الطلب والفاتورة",
	"staff/pricing": "بيوزون | التسعير",
	"staff/stock": "بيوزون | حركة المخزون",
	"staff/picking": "ورقة تجهيز مخزن - بيوزون",
}


def get_site_name() -> str:
	"""Site name from its single source: Website Settings.app_name."""
	try:
		name = frappe.get_cached_doc("Website Settings").app_name
	except Exception:
		name = None
	return (name or "").strip() or DEFAULT_SITE_NAME


def store_title(page_title, site_name=None, site_first=True):
	"""Compose a store page title around the site name (never hardcoded)."""
	site = (site_name or get_site_name()).strip() or DEFAULT_SITE_NAME
	page_title = (page_title or "").strip()
	if not page_title:
		return site
	return f"{site} - {page_title}" if site_first else f"{page_title} - {site}"


def build_seo(
	route,
	*,
	title=None,
	description=None,
	image=None,
	noindex=False,
	og_type="website",
	site_name=None,
):
	"""Build the ``context.seo`` dict consumed by the shared partial."""
	site = (site_name or get_site_name()).strip() or DEFAULT_SITE_NAME
	page_title = (title or "").strip() or site
	seo = frappe._dict(
		site_name=site,
		title=page_title,
		description=(description or "").strip(),
		canonical=get_url(route if str(route).startswith("/") else "/" + str(route)),
		og_type=og_type,
		robots="noindex, nofollow" if noindex else "",
	)
	if image:
		seo.image = image if str(image).startswith(("http://", "https://")) else get_url(image)
	return seo


def get_page_meta(path, context=None):
	"""Return the meta definition for a rendered path, or None when unmanaged."""
	context = context or {}
	site = get_site_name()
	path = (path or "").strip("/ ")

	# "/" and the store home render the same template.
	if path in ("", "biozone-home"):
		return {
			"title": store_title("شريكك الموثوق للمستلزمات الطبية", site),
			"description": STORE_DESCRIPTION,
		}
	if path == "catalog":
		return {
			"title": store_title("المتجر", site),
			"description": (
				f"تسوّق المستلزمات الطبية ومستحضرات العناية من {site} "
				"بأسعار تنافسية وتوصيل سريع."
			),
		}
	if path == "item":
		item = context.get("item") or {}
		item_name = (item.get("item_name") or "").strip()
		if item_name:
			return {
				"title": store_title(item_name, site),
				"description": (
					f"اطلب {item_name} من {site} مع توصيل سريع وأسعار تنافسية."
				),
			}
		return {
			"title": store_title(ITEM_FALLBACK_TITLE, site),
			"description": ITEM_FALLBACK_DESCRIPTION,
		}

	# Noindexed functional / private pages.
	if path == "cart":
		return {"title": store_title("سلة المشتريات", site), "noindex": True}
	if path == "login":
		return {"title": store_title("تسجيل الدخول", site), "noindex": True}
	if path == "my-orders":
		return {"title": store_title("طلباتي", site, site_first=False), "noindex": True}
	if path == "order-confirmed":
		return {
			"title": store_title("تم استلام طلبك بنجاح", site, site_first=False),
			"noindex": True,
		}
	if path == "system-home":
		return {"title": store_title("مدخل الموظف", site), "noindex": True}
	if path == "not-available":
		return {"title": "غير متاح على هذا النطاق", "noindex": True}
	if path == "account":
		return {"title": store_title("حسابي", site, site_first=False), "noindex": True}
	if path == "account/orders":
		return {"title": store_title("طلباتي", site, site_first=False), "noindex": True}
	if path == "account/order-detail":
		return {
			"title": store_title("تفاصيل الطلب", site, site_first=False),
			"noindex": True,
		}
	if path == "account/return-detail":
		return {
			"title": store_title("تفاصيل المرتجع", site, site_first=False),
			"noindex": True,
		}

	if path == "staff/print":
		kind = (context.get("print_kind") or "").strip()
		label = "إشعار مرتجع" if kind == "credit" else "فاتورة"
		return {"title": f"{label} - بيوزون", "noindex": True}

	if path in STAFF_TITLES:
		return {"title": STAFF_TITLES[path], "noindex": True}

	return None


def update_website_context(context):
	"""Hook: attach ``context.seo`` for every managed static page."""
	path = (context.get("path") or "").strip("/ ")
	meta = get_page_meta(path, context)
	if not meta:
		return None

	context.seo = build_seo(
		"/" + path if path else "/",
		title=meta.get("title"),
		description=meta.get("description"),
		image=meta.get("image"),
		noindex=meta.get("noindex", False),
		og_type=meta.get("og_type", "website"),
	)
	return None
