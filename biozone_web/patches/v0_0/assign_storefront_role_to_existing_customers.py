import frappe


def execute():
	"""إسناد Role "Biozone Storefront Customer" لكل حساب Website User
	موجود بالفعل (اتسجّل قبل ما الدور ده يتضاف تلقائيًا في
	biozone_sign_up). حسابات Website User الجديدة بتاخد الدور ده
	تلقائيًا وقت التسجيل، فمحتاجين ده كإجراء لمرة واحدة بس للحسابات
	القديمة (زي abdo4).

	ده patch رسمي (مش endpoint عام ولا سكربت يدوي محفوظ بره النظام)
	عشان يتنفذ تلقائيًا وبشكل قابل للتكرار على أي بيئة (تطوير، staging،
	إنتاج) عبر bench migrate، بدل الاعتماد على تنفيذ يدوي مرة واحدة على
	السيرفر بس.

	ملحوظة: الـRole نفسه ("Biozone Storefront Customer") وصلاحية Read
	على Item، وإعداد permlevel=1 على الحقول الحساسة، لازم تتعمل يدويًا
	من Frappe Desk قبل تشغيل الـpatch ده (أو تُصدَّر كـfixtures منفصلة
	لاحقًا)، لأن الـpatch ده بيفترض إن الـRole نفسه موجود بالفعل — لو
	مش موجود، الـpatch هيتخطى الإسناد بأمان (يسجّل تحذير) بدل ما يفشل.
	"""
	role_name = "Biozone Storefront Customer"

	if not frappe.db.exists("Role", role_name):
		frappe.log_error(
			title="Storefront role missing during patch",
			message=(
				f'Role "{role_name}" غير موجود وقت تشغيل هذا الـpatch. '
				"من فضلك أنشئ الـRole ده يدويًا من Frappe Desk (Role "
				"Permission Manager) ثم أعد تشغيل bench migrate."
			),
		)
		return

	website_users = frappe.get_all(
		"User",
		filters={"user_type": "Website User", "enabled": 1},
		pluck="name",
	)

	for user_email in website_users:
		user = frappe.get_doc("User", user_email)

		if role_name not in user.get_roles():
			user.add_roles(role_name)

	frappe.db.commit()
