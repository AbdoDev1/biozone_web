import frappe

# دفعات صغيرة حتى لا تُحمَّل كل سجلات العملاء في الذاكرة دفعة واحدة
# عند تشغيل الـpatch على قاعدة كبيرة.
BATCH_SIZE = 500


def execute():
	"""تعليم كل العملاء الموجودين قبل هذا التغيير كمراجَعين فئويًا.

	العملاء الجدد (المنشأون عبر biozone_sign_up بعد إضافة الحقل) يبدؤون
 بـstaff_category_reviewed = 0 فيظهرون في قائمة الانتظار
	(/staff/customers)، بينما العملاء القدامى يُعاملون كمراجَعين حتى لا
	تفيض القائمة عند إطلاق الصفحة. لا يمس هذا الـpatch حقل
	customer_group ولا أي بيانات أخرى.

	ملحوظة تشغيل: Frappe يسجّل الـpatches المنفذة في Patch Log فلا يُعاد
	تنفيذه تلقائيًا. الفلتر أدناه (فقط الصفوف التي ليست 1) يجعل إعادة
	التشغيل اليدوية آمنة على الصفوف المراجَعة، لكن إعادته يدويًا بعد
	تسجيل عملاء جدد ستعلّمهم كمراجَعين أيضًا — لذلك يُشغَّل مرة واحدة
	فقط عبر bench migrate مباشرة بعد patch إنشاء الحقل.
	"""
	if not frappe.get_meta("Customer").has_field("staff_category_reviewed"):
		frappe.log_error(
			title="Staff review flag missing during backfill",
			message=(
				'الحقل "staff_category_reviewed" غير موجود على Customer وقت '
				"تشغيل هذا الـpatch. تأكد من تنفيذ patch إنشاء الحقل "
				"(add_staff_category_reviewed_to_customer) أولًا ثم أعد "
				"تشغيل bench migrate."
			),
		)
		return

	updated = 0
	start = 0
	while True:
		rows = frappe.get_all(
			"Customer",
			fields=["name", "staff_category_reviewed"],
			limit_page_length=BATCH_SIZE,
			limit_start=start,
		)
		if not rows:
			break
		for row in rows:
			if row.get("staff_category_reviewed"):
				continue
			frappe.db.set_value(
				"Customer", row.name, "staff_category_reviewed", 1, update_modified=False
			)
			updated += 1
		frappe.db.commit()
		if len(rows) < BATCH_SIZE:
			break
		start += BATCH_SIZE

	frappe.logger().info(f"mark_existing_customers_category_reviewed: marked {updated} customers as reviewed")
