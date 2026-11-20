import frappe

from biozone_web.utils import (
	get_header_context,
	get_storefront_free_shipping_threshold,
	redirect_staff_away_from_store,
)


def get_context(context):
	redirect_staff_away_from_store()

	# سلة محلية للزائر أيضًا: العرض والتعديل متاحان هنا، وإتمام الطلب
	# يبقى محميًا ببوابة التأكيد (biozone_confirm_order ترفض Guest).
	# نقل سلة الزائر إلى الحساب لا يحدث في هذه الصفحة تلقائيًا — بل
	# بمعاينة وموافقة صريحة في الواجهة (public/js/guest_cart_merge.js).
	context.no_cache = 1
	context.active_page = "cart"
	context.update(get_header_context())
	context.is_guest = frappe.session.user == "Guest"
	context.free_shipping_threshold = get_storefront_free_shipping_threshold()
	context.csrf_token = frappe.sessions.get_csrf_token()
	return context
