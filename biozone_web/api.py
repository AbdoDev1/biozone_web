import frappe
from frappe import _


@frappe.whitelist(allow_guest=True)
def biozone_login(usr: str, pwd: str, remember_me: int = 0):
	"""تسجيل دخول مباشر، مع دعم اختياري لتمديد مدة الجلسة (تذكرني)."""
	# Reuse the current request's login_manager instead of creating a new
	# LoginManager(), since a fresh instance re-resumes the guest session
	# inside its own __init__ before authenticate() even runs.
	login_manager = frappe.local.login_manager
	login_manager.authenticate(user=usr, pwd=pwd)
	login_manager.post_login()

	if frappe.utils.cint(remember_me):
		# Extend session length if "remember me" was checked.
		# (Default expiry comes from System Settings > Session Expiry;
		#  here we just override the cookie's own max_age explicitly.)
		frappe.local.cookie_manager.set_cookie(
			"sid",
			frappe.session.sid,
			max_age=60 * 60 * 24 * 30,  # 30 days
			httponly=True,
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
	login_manager = frappe.local.login_manager
	login_manager.authenticate(user=email, pwd=pwd)
	login_manager.post_login()

	return {"message": "Account Created", "home_page": "/biozone-home"}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def biozone_forgot_password(email: str):
	"""Uses Frappe's built-in password-reset flow (sends a reset-link email)."""
	from frappe.core.doctype.user.user import reset_password

	email = (email or "").strip().lower()
	if not email:
		frappe.throw(_("من فضلك اكتب البريد الإلكتروني"))

	reset_password(user=email)
	frappe.clear_messages()

	return {
		"message": _(
			"إذا كان هذا البريد الإلكتروني مسجلًا لدينا، فسيصلك رابط لإعادة تعيين كلمة المرور. يُرجى التحقق من صندوق الوارد."
		)
	}