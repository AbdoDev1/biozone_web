import frappe
from frappe import _
from frappe.auth import LoginManager


@frappe.whitelist(allow_guest=True)
def biozone_login(usr: str, pwd: str, remember_me: int = 0):
	"""تسجيل دخول مباشر، مع دعم اختياري لتمديد مدة الجلسة (تذكرني)."""
	login_manager = LoginManager()
	login_manager.authenticate(user=usr, pwd=pwd)
	login_manager.post_login()

	if frappe.utils.cint(remember_me):
		# تمديد مدة صلاحية الجلسة لو المستخدم اختار "تذكرني"
		# (القيمة الافتراضية تُقرأ من System Settings > Session Expiry،
		#  هنا بس بنمدد الكوكي نفسها لمدة أطول صراحةً)
		frappe.local.cookie_manager.set_cookie(
			"sid", frappe.session.sid, max_age=60 * 60 * 24 * 30  # 30 يوم
		)

	return {"message": "Logged In", "home_page": "/biozone-home"}


@frappe.whitelist(allow_guest=True)
def biozone_sign_up(email: str, full_name: str, phone: str, pwd: str):
	"""تسجيل حساب جديد فوري ومباشر — بلا بريد تفعيل وبلا موافقة إدارية،
	مطابقةً لقرار المشروع (تسجيل فوري زي آلية mg الحالية)."""

	email = email.strip().lower()

	if not email or not full_name or not pwd:
		frappe.throw(_("من فضلك أكمل كل الحقول المطلوبة"))

	if frappe.db.exists("User", email):
		frappe.throw(_("هذا البريد الإلكتروني مسجل بالفعل"))

	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": full_name,
			"phone": phone,
			"send_welcome_email": 0,
			"enabled": 1,
			"user_type": "Website User",
			"new_password": pwd,
		}
	)
	user.insert(ignore_permissions=True)
	frappe.db.commit()

	# تسجيل الدخول فورًا بعد إنشاء الحساب مباشرة
	login_manager = LoginManager()
	login_manager.authenticate(user=email, pwd=pwd)
	login_manager.post_login()

	return {"message": "Account Created", "home_page": "/biozone-home"}
