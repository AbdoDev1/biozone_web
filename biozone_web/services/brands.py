"""شعارات البراندات المحلية (Wordmark) — مصدر واحد مشترك.

تُستخدم في الرئيسية (شريط البراندات) وفي نتائج بحث البراندات بالكتالوج.
أولوية العرض دائمًا: `Brand.image` المرفوع في النظام ← ملف هذا المجلد.
"""

BRAND_LOGO_BASE = "/assets/biozone_web/images/brands"

BRAND_LOGOS = {
	"Africana": "africana",
	"Apple": "apple",
	"Eva": "eva",
	"Fogg": "fogg",
	"Nivea": "nivea",
	"Rexona": "rexona",
}


def brand_logo_url(brand_name):
	"""رابط Wordmark المحلي للبراند، أو None إن لم يكن له ملف مرسل بعد."""
	slug = BRAND_LOGOS.get((brand_name or "").strip())
	return "%s/%s.svg" % (BRAND_LOGO_BASE, slug) if slug else None
