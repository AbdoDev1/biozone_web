import frappe
from frappe import _
from frappe.utils import has_common


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


SYSTEM_USER_ROLES = ("System Manager", "Administrator")


def is_system_user(user=None):
	"""هل الحساب موظف/أدمن (System User)؟

	الاعتماد الأساسي على User.user_type — نفس المعيار اللي biozone كله
	بيستخدمه في require_staff_access وredirect_staff_away_from_store —
	مع احتياط بالأدوار (System Manager/Administrator) عشان أي حساب
	تايبه Website User بس اتنفذت له صلاحيات نظام يتحسب صح برضه.
	استخدمنا frappe.utils.has_common في فحص الأدوار بدل التخمين بالاسم.

	ملحوظة: الدالة دي بتفضل مستخدمة كحارس لصفحة /system-home (وكمان
	في require_staff_accsess) بس — أما قرار الصفحة اللي بيوصل ليها
	الموظف من "/" فبيبقى في get_home_route_for_system_user تحت (تمييز
	الموظف العادي عن صاحب صلاحية الـ Desk).
	"""
	user = user or frappe.session.user
	if not user or user == "Guest":
		return False

	user_type = frappe.db.get_value("User", user, "user_type")
	if user_type == "System User":
		return True

	return bool(has_common(frappe.get_roles(user), SYSTEM_USER_ROLES))


# الأدوار الإدارية اللي بتودي صاحبها للـ Desk بدل لوحة الموظف العادية.
# القرار مبني على نتيجة البحث في قاعدة البيانات (راجع التقرير): مفيش
# أي Role مخصص (is_custom=1) للمحاسبة/الإدارة في `tabRole` — كل اللي
# اتلاقى زي "Accounts Manager" و"Accounts User" أدوار ERPNext قياسية
# (is_custom=0). بناءً على طلب العميل، بنستعمل الـ fallback المتفق
# عليه: Administrator + System Manager فقط. لو اتطلب لاحقًا إن أي دور
# محاسبة قياسي (مثلًا "Accounts Manager") يودّي للـ Desk برضه، يكفي
# إضافته لقائمة الـ tuple دي.
ADMIN_DESK_ROLES = ("Administrator", "System Manager")


def get_home_route_for_system_user(user):
	"""يفرّق بين نوعين من حسابات System User حسب خطة الدومينات الأصلية:

	- موظف عادي (System User من غير دور إداري): /staff/dashboard
	- Administrator أو صاحب دور إداري (ADMIN_DESK_ROLES): /desk

	التمييز بـ frappe.get_roles + frappe.utils.has_common وليس بالتخمين
	بالأسماء، وكمان فحص صريح لاسم المستخدم Administrator.
	"""
	user = user or frappe.session.user
	if not user or user == "Guest":
		# الدالة دي بتتستدعى للـ System User بس — ده دفاع نظرًا لعدم وصول
		# الزائر هنا أصلًا.
		return "/biozone-home"

	roles = frappe.get_roles(user)
	if user == "Administrator" or has_common(roles, ADMIN_DESK_ROLES):
		return "/desk"

	return "/staff/dashboard"


def get_store_url(path="/biozone-home"):
	"""رابط المتجر العام (دومين biozone.pro أو ما يعادله في التطوير)
	عشان زر "الرجوع للمتجر" في /system-home يفتحه في **تبويب جديد**
	بجلسة منفصلة — التبويب الجديد على دومين مختلف مش بيحمل كوكيز/
	توكن جلسة الموظف أصلًا، فقاعدة عزل الدومينين
	(redirect_staff_away_from_store) تفضل شغالة من غير ما تتعدل.

	بيرجع رابط كامل بالـ scheme والـ port من الطلب الحالي عشان يشتغل
	على dev server (نفس البورت لو جوه التطوير) وعلى الإنتاج (https).
	"""
	store_domain = ""
	for domain in frappe.get_site_config().get("domains") or []:
		if domain and "staff" not in domain and "app" not in domain:
			store_domain = domain
			break

	if not store_domain:
		# مفيش دومين عام معرّف — نرجّع مسار نسبي (مش رابط كامل).
		return path

	scheme = "http"
	port = ""
	request = getattr(frappe.local, "request", None)
	if request:
		scheme = request.scheme or "http"
		host = getattr(request, "host", "") or ""
		if ":" in host:
			port = host.rsplit(":", 1)[1]

	if port.isdigit():
		return f"{scheme}://{store_domain}:{port}{path}"

	return f"{scheme}://{store_domain}{path}"


def get_website_user_home_page(user=None):
	"""يحدد الصفحة اللي بيوصل ليها اللي بيفتح "/" حسب نوع الحساب:

	- Guest: بيفتح المتجر مباشرة /biozone-home — تعديل 3 (الزائر
	  بيتصفح الكاتالوج فعليًا، بدل ما يقع على صفحة تسجيل الدخول).
	- Website User: يروح مباشرة لصفحة المتجر (/biozone-home) بدل صفحة
	  Settings الافتراضية (Edit Profile / Reset Password / 3rd party apps).
	- System User:
	    * موظف عادي → /staff/dashboard
	    * Administrator/صاحب دور إداري → /desk
	  (تعديل 1 — تمييز صريح عبر get_home_route_for_system_user، مش
	  تجميع كل الـ System Users في مسار واحد.)

	ملحوظة الأولوية: الدالة دي بتتستدعى من get_home_page() (عن طريق
	hook get_website_user_home_page) قبل Website Settings.home_page —
	بس بعد Role.home_page وPortal Settings.default_portal_home (لو
	اتعييّنوا لأي مستخدم هيبقى ليهم الأولوية عليها).
	"""
	user = user or frappe.session.user
	if not user or user == "Guest":
		return "/biozone-home"

	if is_system_user(user):
		return get_home_route_for_system_user(user)

	return "/biozone-home"


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


def get_portal_users_for_customer(customer):
	"""يرجع قائمة User المرتبطين بـ Customer عبر جدول Portal User."""
	if not customer:
		return []

	rows = frappe.get_all(
		"Portal User",
		filters={"parent": customer, "parenttype": "Customer"},
		fields=["user"],
	)
	return [r.user for r in rows if r.user]


def is_unique_customer_binding(customer, user):
	"""يتحقق أن Customer مرتبط بالمستخدم الحالي فقط ولا يشاركه أحد.

	يرجع True فقط إذا كان المستخدم الحالي هو الـ Portal User الوحيد
	لهذا الـ Customer. أي Customer مشترك بين عدة مستخدمين يرجع False
	لمنع تسريب الطلبات بين المستخدمين.
	"""
	if not customer or not user or user == "Guest":
		return False

	users = get_portal_users_for_customer(customer)
	return users == [user]


def can_current_user_view_sales_order(so, user=None):
	"""يحدد هل المستخدم الحالي مسموح له بعرض Sales Order أم لا.

	القاعدة:
	1. الطلبات الجديدة: owner الحقيقي يطابق المستخدم الحالي.
	2. الطلبات القديمة (owner = Administrator بسبب hack الإنشاء المحذوف):
	   يُقبل fallback عبر Customer فقط إذا كان الربط فريدًا — أي أن
	   المستخدم الحالي هو الـ Portal User الوحيد لهذا الـ Customer.
	   أي Customer مشترك يُرفض لمنع تسريب الطلبات.
	"""
	user = user or frappe.session.user

	if not user or user == "Guest":
		return False

	if so.owner == user:
		return True

	# Fallback للطلبات القديمة فقط: مالكها Administrator والطلب مرتبط
	# بنفس Customer الفريد للمستخدم الحالي.
	if so.owner == "Administrator" and so.customer:
		customer = find_customer_for_current_user()
		if customer and so.customer == customer and is_unique_customer_binding(customer, user):
			return True

	return False


def assert_can_view_sales_order(so, user=None):
	"""يرمي PermissionError إذا لم يكن مسموحًا للمستخدم عرض الطلب."""
	if not can_current_user_view_sales_order(so, user=user):
		frappe.throw(
			_("لا تملك صلاحية عرض هذا الطلب"),
			frappe.PermissionError,
		)


def get_or_create_customer_for_current_user():
	"""يرجع اسم الـCustomer المرتبط بالمستخدم الحالي، وينشئ واحد جديد لو
	مفيش. الربط دلوقتي عن طريق جدول Portal User الفرعي على Customer
	(بدل تسمية الـCustomer صراحة ببريد المستخدم — القديم، اتأكد إنه مكسور).

	ملحوظة تاريخية: الافتراض القديم `Customer.name == email` كان بيخلي
	أي عميل عنده أكتر من طلب يتفرّق على Customers متعددة (باج بيانات
	حقيقي). الدالة دي اتصلحت تستخدم Portal User بدل تطابق الاسم بتاريخ
	8 سبتمبر 2026، ومسار إنشاء Customer جديد فضل متقفل مؤقتًا (P0) لحد
	ما اتاختبر حيًا بالكامل تحت سباق تزامن فعلي (طلبات متزامنة حقيقية) —
	الاختبار نجح 100%، والقفل اتشال نهائيًا من الكود ومن الإنتاج بتاريخ
	11 سبتمبر 2026. حماية التكرار الموضحة تحت (تحقق بعدي بعد الإنشاء،
	مش lock حقيقي زي MySQL GET_LOCK) هي الحماية الدائمة المعتمدة الآن،
	مش إجراء مؤقت.
	"""
	user_email = frappe.session.user

	if user_email == "Guest":
		frappe.throw(_("يجب تسجيل الدخول أولًا"))

	customer_name = find_customer_for_current_user()
	if customer_name:
		return customer_name

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
