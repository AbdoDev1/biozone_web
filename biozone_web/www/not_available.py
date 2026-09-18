import frappe

from biozone_web.utils import get_current_domain_role


def get_context(context):
	# صفحة رفض محلية نظيفة (403) تُستخدم بدل صفحات الخطأ الخام للمسارات
	# الممنوعة حسب النطاق (مثل /desk على مضيف المتجر). بلا أي traceback
	# أو مسارات داخلية — قالب ثابت + رسالة فقط.
	# روابط العودة نسبية same-host حصرًا حسب النطاق الحالي — لا عبور نطاقات.
	context.no_cache = 1
	role = get_current_domain_role()
	if role == "staff":
		context.login_url = "/staff/login"
		context.login_label = "تسجيل دخول الموظفين"
	else:
		context.login_url = "/login"
		context.login_label = "تسجيل الدخول"
	context.http_status_code = 403
	return context
