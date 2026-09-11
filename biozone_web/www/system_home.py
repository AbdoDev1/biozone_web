import frappe

from biozone_web.utils import (
	get_home_route_for_system_user,
	get_store_url,
	is_system_user,
)


def get_context(context):
	# الصفحة دي مدخل مخصص للموظف/الأدمن (System User) — مش نافذة للعموم.
	# أي حد تاني يجرب يفتح الرابط مباشرة (/system-home) يترجّع للمسار الصح:
	# Guest على تسجيل الدخول، وWebsite User على المتجر.
	#
	# ملحوظة (تعديل 1): من دلوقتي "/" بيوّرّي الـ System User مباشرة
	# لوجهته (لوحة الموظف للعادي أو الـ Desk للإداري) عن طريق
	# get_website_user_home_page → get_home_route_for_system_user — يعني
	# الصفحة دي بتفضل متاحة لو حد فتح /system-home بنفسه بس.
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login"
		raise frappe.Redirect

	if not is_system_user(frappe.session.user):
		frappe.local.flags.redirect_location = "/biozone-home"
		raise frappe.Redirect

	context.no_cache = 1

	# زر "الرئيسية" يودّي لنفس الوجهة اللي بتحسمها
	# get_home_route_for_system_user حسب نوع الحساب اللي فاتح الصفحة
	# (موظف عادي → /staff/dashboard، إداري → /desk) — مش قيمة ثابتة.
	context.home_route = get_home_route_for_system_user(frappe.session.user)

	# زر "الرجوع للمتجر" يفتح دومين المتجر العام في **تبويب جديد**
	# (target=_blank) بجلسة منفصلة — التبويب الجديد على دومين مختلف مش
	# بيحمل كوكيز/توكن جلسة الموظف أصلًا، فقاعدة عزل الدومينين
	# redirect_staff_away_from_store تفضل شغالة من غير أي تعديل عليها.
	context.store_url = get_store_url("/biozone-home")

	return context
