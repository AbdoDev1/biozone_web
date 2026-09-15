import frappe


def execute():
	"""تعليم العملاء القدامى بفئة غير عامة كمراجَعين فئويًا ضمنيًا.

	كل عميل حالي فئته غير فئة الجمهور (الحية عبر
	get_public_customer_group، لا نص ثابت) يُعتبر مراجَعًا ضمنيًا
	(category_assigned_by_staff = 1) فلا يفيض قائمة الانتظار الجديدة.
	كل عميل حالي على فئة الجمهور بالضبط يُترك على القيمة الافتراضية 0
	فيدخل قائمة الانتظار تلقائيًا بانتظار "اعتماد كجمهور" أو تغيير فئة
	حقيقي. لا يمس هذا الـpatch حقل customer_group ولا أي بيانات أخرى.

	ملحوظة تشغيل: Frappe يسجّل الـpatches المنفذة في Patch Log فلا يُعاد
	تنفيذه تلقائيًا. إعادة التشغيل اليدوية آمنة (idempotent): الصفوف التي
	صارت 1 تُتجاوَز بالشرط أدناه، وصفوف الجمهور تُتجاوَز دائمًا — لذلك
	يُشغَّل مرة واحدة فقط عبر bench migrate مباشرة بعد patch إنشاء الحقل.
	"""
	if not frappe.get_meta("Customer").has_field("category_assigned_by_staff"):
		frappe.log_error(
			title="Category assigned flag missing during backfill",
			message=(
				'الحقل "category_assigned_by_staff" غير موجود على Customer وقت '
				"تشغيل هذا الـpatch. تأكد من تنفيذ patch إنشاء الحقل "
				"(add_category_assigned_by_staff_to_customer) أولًا ثم أعد "
				"تشغيل bench migrate."
			),
		)
		return

	from biozone_web.utils import get_public_customer_group

	public_group = get_public_customer_group()

	updated = 0
	start = 0
	while True:
		rows = frappe.get_all(
			"Customer",
			fields=["name", "customer_group", "category_assigned_by_staff"],
			limit_page_length=500,
			limit_start=start,
		)
		if not rows:
			break
		for row in rows:
			if (row.get("customer_group") or "") == public_group:
				continue
			if row.get("category_assigned_by_staff"):
				continue
			frappe.db.set_value(
				"Customer", row.name, "category_assigned_by_staff", 1, update_modified=False
			)
			updated += 1
		if len(rows) < 500:
			break
		start += 500

	frappe.db.commit()

	frappe.logger().info(f"backfill_category_assigned_by_staff: marked {updated} customers as reviewed")
