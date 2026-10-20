import frappe

LOGO = "/assets/biozone_web/images/biozone-favicon.svg"
BANNER = "/assets/biozone_web/images/hero/hero-pharmacy.jpg"
BRAND_HTML = (
	'<span style="display:inline-flex;align-items:center;gap:6px;">'
	f'<img src="{LOGO}" alt="Biozone" style="width:22px;height:22px;">'
	"<span>Biozone</span></span>"
)


def execute():
	"""توحيد هوية Biozone في Website Settings والشركة وLetter Head.

	يستخدم شعار التطبيق الثابت الموجود أصلًا (biozone-favicon.svg) بدل
	اختراع شعار جديد. لا يغيّر أي منطق أو صلاحيات؛ مجرد إعدادات عرض
	وعلامة تجارية، ويُعاد تنفيذه بأمان.
	"""
	ws = frappe.get_single("Website Settings")
	ws.app_name = "Biozone"
	ws.brand_html = BRAND_HTML
	ws.favicon = LOGO
	ws.banner_image = BANNER
	ws.save(ignore_permissions=True)

	for company in frappe.get_all("Company", pluck="name"):
		frappe.db.set_value("Company", company, "company_logo", LOGO)

	for letter_head in frappe.get_all("Letter Head", pluck="name"):
		frappe.db.set_value("Letter Head", letter_head, "image", LOGO)

	frappe.clear_cache()
	frappe.db.commit()
