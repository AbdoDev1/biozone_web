import frappe
from frappe import _


def require_staff_access():
	"""يتأكد إن اللي بيفتح أي صفحة تحت /staff/* هو حساب موظف (System User)
	مفعّل (enabled)، مش حساب عميل (Website User اللي بيتعمل وقت التسجيل
	في /catalog) ومش حساب موظف اتقفل بعد ما سابه.

	ملحوظة مهمة: ده فحص أولي بس (نوع الحساب + حالة التفعيل) — مفيش لسه
	Role مخصص يفرّق بين موظف مخزن وموظف تسعير وموظف تجهيز طلبات (B6a/
	B6b/B6d كل واحد محتاج صلاحية مختلفة زي ما اتحدد في الخطة). لما B6d
	(الحسابات والخصومات) تتبنى فعليًا، لازم نضيف هنا (أو في كل صفحة/
	API لوحدها) فحص Role حقيقي (مثلًا frappe.has_role("Store Staff"))
	بدل الاكتفاء بـ"موظف عادي = يشوف كل حاجة" زي دلوقتي.
	"""
	user = frappe.session.user

	if user == "Guest":
		frappe.local.flags.redirect_location = "/staff/login"
		raise frappe.Redirect

	user_type, enabled = frappe.db.get_value("User", user, ["user_type", "enabled"])
	if user_type != "System User" or not enabled:
		# حساب عميل، أو حساب موظف اتقفل (enabled=0) بعد ما ساب الشركة
		# مثلًا لكن جلسته القديمة لسه شغالة — في الحالتين نرجّعه للمتجر
		# بدل ما يشوف صفحة خطأ صلاحيات خام.
		frappe.local.flags.redirect_location = "/biozone-home"
		raise frappe.Redirect


def redirect_staff_away_from_store():
	"""عكس require_staff_access تمامًا: لو حساب موظف (System User) حاول
	يفتح أي صفحة من صفحات المتجر/العميل (الرئيسية، المتجر، السلة،
	تأكيد الطلب، طلباتي، تسجيل الدخول)، بنرجّعه على لوحة تحكم الموظف
	بدل كده.

	فصل كامل بين المدخلين ده اتطلب عشان الاستضافة النهائية هتكون على
	دومينين منفصلين (staff.biozone.pro لواجهة الموظف، biozone.pro
	للمتجر) — فكل فئة توصل لمدخلها بس حتى لو حد جرّب يفتح رابط
	المدخل التاني يدويًا.
	"""
	user = frappe.session.user
	if user == "Guest":
		return

	user_type = frappe.db.get_value("User", user, "user_type")
	if user_type == "System User":
		frappe.local.flags.redirect_location = "/staff/dashboard"
		raise frappe.Redirect


def get_default_warehouse():
	"""Single-warehouse assumption: the stock-movement panel doesn't ask
	the user to pick a warehouse (per the agreed field list), so every
	movement logged from /staff/stock goes against one implicit
	warehouse. Uses Stock Settings' default_warehouse if set **and
	actually exists** (اتأكدنا عمليًا إن الإعداد ممكن يبقى فيه قيمة
	قديمة لمستودع محذوف زي "Stores - MG" — فحص الوجود إلزامي)، otherwise
	falls back to the first non-group Warehouse. Raises clearly if
	neither exists so the failure is obvious instead of a silent None.
	"""
	warehouse = frappe.db.get_single_value("Stock Settings", "default_warehouse")
	if warehouse and not frappe.db.exists("Warehouse", warehouse):
		warehouse = None
	if not warehouse:
		warehouse = frappe.db.get_value(
			"Warehouse", {"is_group": 0, "disabled": 0}, "name", order_by="creation asc"
		)
	if not warehouse:
		frappe.throw(_("لا يوجد مستودع معرّف في النظام — يرجى إعداد مستودع أولًا"))
	return warehouse


def get_header_context():
	"""Shared header state for any www page that includes site_header.html.
	Call this from the page's get_context() and merge the result in, e.g.:

		context.update(get_header_context())

	- is_logged_in: any authenticated user
	- user_full_name: shown in the header instead of the account icon
	"""
	user = frappe.session.user
	is_logged_in = user != "Guest"

	user_full_name = None
	if is_logged_in:
		user_full_name = frappe.db.get_value("User", user, "full_name") or user

	return {
		"is_logged_in": is_logged_in,
		"user_full_name": user_full_name,
	}


def find_customer_for_current_user():
	"""يدوّر عن Customer مرتبط رسميًا بالمستخدم الحالي عن طريق جدول Portal
	User الفرعي — بدل الاعتماد على Customer.name == email، اللي اتأكد
	إنه مكسور بسبب Customer.autoname() في ERPNext (بيتجاهل أي name
	صريح لما Customer Naming By = "Customer Name"، فبتتعمل نسخة Customer
	جديدة كل مرة بدل ما تتلاقى الموجودة). يرجع اسم Customer لو لقى ربط
	فعلي، أو None لو مفيش (يشمل حالة Guest).
	"""
	user_email = frappe.session.user

	if user_email == "Guest":
		return None

	return frappe.db.get_value(
		"Portal User", {"user": user_email, "parenttype": "Customer"}, "parent"
	)


def get_or_create_customer_for_current_user():
	"""يرجع اسم الـCustomer المرتبط بالمستخدم الحالي، وينشئ واحد جديد لو
	مفيش. الربط دلوقتي عن طريق جدول Portal User الفرعي على Customer
	(بدل تسمية الـCustomer صراحة ببريد المستخدم — القديم، اتأكد إنه مكسور).

	🛑 P0 مؤقت (8 سبتمبر 2026): الافتراض القديم `Customer.name == email`
	كان بيخلي أي عميل عنده أكتر من طلب يتفرّق على Customers متعددة (باج
	بيانات حقيقي، راجع ticket منفصل + خطة migration). الدالة دي اتصلحت
	تستخدم Portal User بدل تطابق الاسم، لكن **مسار إنشاء Customer جديد
	لسه متقفل مؤقتًا** لحد ما نختبره حي بالكامل (Customer + صف Portal
	User + استدعاء تاني بيرجع نفس الاسم بلا تكرار) — الشيل هيبقى في
	commit منفصل بعد الاختبار، مش هنا.
	"""
	user_email = frappe.session.user

	if user_email == "Guest":
		frappe.throw(_("يجب تسجيل الدخول أولًا"))

	customer_name = find_customer_for_current_user()
	if customer_name:
		return customer_name

	# 🛑 P0 مؤقت — مسار الإنشاء (تحت) لسه متقفل لحد ما يتاختبر حي بالكامل
	# ويتشال في commit منفصل. راجع ticket منفصل قبل الشيل.
	frappe.throw(_("عذرًا، الطلبات الجديدة متوقفة مؤقتًا لصيانة عاجلة. حاول لاحقًا."))

	full_name = frappe.db.get_value("User", user_email, "full_name") or user_email

	customer = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": full_name,
			"customer_type": "Individual",
			"customer_group": get_default_customer_group(),
			"territory": get_default_territory(),
			"portal_users": [{"user": user_email}],
		}
	)
	customer.insert(ignore_permissions=True)

	# حماية بسيطة من سباق التزامن (طلبين متزامنين لأول مرة لنفس
	# المستخدم قبل ما أي ربط يتعمل): تحقق بعدي، مش lock حقيقي — لو حصل
	# سباق فعلي، ناخد أقدم Customer اتعمل ونمسح الزيادة اللي إحنا
	# عملناها دلوقتي.
	all_matches = frappe.get_all(
		"Portal User",
		filters={"user": user_email, "parenttype": "Customer"},
		fields=["parent", "creation"],
		order_by="creation asc",
	)
	if len(all_matches) > 1:
		canonical = all_matches[0].parent
		if canonical != customer.name:
			frappe.delete_doc("Customer", customer.name, ignore_permissions=True, force=True)
			return canonical

	return customer.name


def get_default_customer_group():
	return frappe.db.get_single_value("Selling Settings", "customer_group") or frappe.db.get_value(
		"Customer Group", {"is_group": 0}, "name", order_by="creation asc"
	)


def get_default_territory():
	return frappe.db.get_single_value("Selling Settings", "territory") or frappe.db.get_value(
		"Territory", {"is_group": 0}, "name", order_by="creation asc"
	)


def get_default_company():
	return frappe.defaults.get_global_default("company") or frappe.db.get_single_value(
		"Global Defaults", "default_company"
	)
