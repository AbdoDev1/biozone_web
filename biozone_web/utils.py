import frappe
from frappe import _
from frappe.utils import has_common


def get_current_domain_role():
	"""دور النطاق الحالي من Host header — أساس الفصل بين النطاقات الثلاثة.

	- `staff.*` → "staff" (بوابة الموظفين فقط).
	- `app.*` → "app" (Desk فقط).
	- أي شيء آخر (biozone.pro/www/store/test/مضيفات التطوير/IPs) → "store".

	المضيفات المجهولة تمامًا يرفضها Frappe قبل الوصول لأي كود (404 على
	تحليل الموقع)، فالافتراضي "store" آمن للتطوير المحلي فقط وموثق هنا.
	كل القرارات downstream نسبية same-host — لا يُبنى أي redirect مطلق
	عابر للنطاقات من هذه الدالة.
	"""
	request = getattr(frappe.local, "request", None)
	host = (getattr(request, "host", "") or "").lower().split(":")[0]
	if host.startswith("staff."):
		return "staff"
	if host.startswith("app."):
		return "app"
	return "store"


def guard_domain_routes():
	"""حارس before_request لمسارات Desk (قاعدة نهائية).

	- يمنع /desk و/app (بأي عمق فرعي) على دوري store وstaff — لكل الحالات
	  (ضيف/عميل/موظف/أدمن) وبلا استثناء صلاحيات: المنع حسب المضيف+المسار.
	  app.biozone.pro وحده يعرض Desk (يُمرَّر دون مساس).
	- إعادة كتابة داخلية لصفحة الرفض المحلية /not-available (403 نظيفة
	  بلا traceback، same-host) — لا redirect ولا عبور نطاقات.
	- لا يمس /api ولا الأصول ولا أي مسار آخر.
	"""
	request = getattr(frappe.local, "request", None)
	path = (getattr(request, "path", "") or "").lower()
	if get_current_domain_role() not in ("store", "staff"):
		return
	if path == "/desk" or path.startswith("/desk/") or path == "/app" or path.startswith("/app/"):
		# إعادة كتابة المسار داخليًا لصفحة الرفض المحلية /not-available
		# (403 نظيفة بلا traceback) — رمي استثناء من before_request يعرض
		# صفحة خطأ الإطار بمحتوى تتبع داخلي، وRedirect هنا مكسور (301 بلا
		# Location). لا redirect ولا عبور نطاقات: نفس الطلب نفس المضيف.
		frappe.local.request.path = "/not-available"
		return


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

	فصل النطاقات: صفحات /staff/* لا تُعرض إلا تحت مضيف staff — أي مضيف
	آخر يرفض محليًا بـ403 (بلا تحويل). الضيف على مضيف staff يُحوَّل نسبيًا
	لـ/staff/login (نفس المضيف). العميل/المعطّل على مضيف staff يُرفض
	محليًا بـ403 بدل تحويله لمسار المتجر.
	"""
	if get_current_domain_role() != "staff":
		frappe.throw(_("غير متاح على هذا النطاق"), frappe.PermissionError)

	user = frappe.session.user

	if user == "Guest":
		frappe.local.flags.redirect_location = "/staff/login"
		raise frappe.Redirect

	user_type, enabled = frappe.db.get_value("User", user, ["user_type", "enabled"])
	if user_type != "System User" or not enabled:
		# حساب عميل، أو حساب موظف اتقفل (enabled=0) بعد ما ساب الشركة
		# مثلًا لكن جلسته القديمة لسه شغالة — رفض محلي بلا نقل لأي مسار.
		frappe.throw(_("غير متاح على هذا النطاق"), frappe.PermissionError)


def require_customer_access():
	"""بوابة نهايات جرس العميل: Website User مسجّل ومفعّل فقط.

	الضيف وحسابات System User (بما فيها المعطلة) مرفوضون محليًا بـ403 —
	بلا تحويل (النهايات API لا صفحات).
	"""
	user = frappe.session.user
	if user == "Guest":
		raise frappe.PermissionError(_("غير متاح على هذا النطاق"))
	user_type, enabled = frappe.db.get_value("User", user, ["user_type", "enabled"])
	if user_type != "Website User" or not enabled:
		raise frappe.PermissionError(_("غير متاح على هذا النطاق"))


def redirect_staff_away_from_store():
	"""حارس نطاق صفحات المتجر/العميل (الاسم تاريخي — لم يعد يحوّل إطلاقًا).

	قرار فصل النطاقات: لا يوجد أي تحويل هنا، نسبيًا كان أو مطلقًا.
	- تحت مضيف staff/app: صفحات المتجر مرفوضة محليًا بـ403.
	- تحت مضيف store: الموظف (System User) مرفوض محليًا بـ403 — بوابته
	  مضيف staff حصرًا، ولا يُحوَّل إليه تلقائيًا حتى لا ينشئ حلقة أو
	  يعرض صفحة غير مناسبة على مضيف المتجر.
	- الضيف والحسابات غير الموظفة على مضيف store: يمرون كالمعتاد.
	"""
	role = get_current_domain_role()
	if role in ("staff", "app"):
		frappe.throw(_("غير متاح على هذا النطاق"), frappe.PermissionError)
		return

	user = frappe.session.user
	if user == "Guest":
		return

	user_type = frappe.db.get_value("User", user, "user_type")
	if user_type == "System User":
		frappe.throw(_("غير متاح على هذا النطاق"), frappe.PermissionError)


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
	"""رابط المتجر الأساسي (biozone.pro — يُلتقط تلقائيًا كأول دومين عام
	 في site_config بعد استبعاد staff/app) عشان زر "الرجوع للمتجر"
	 في /system-home يفتحه في **تبويب جديد** بجلسة منفصلة — التبويب الجديد
	 على دومين مختلف مش بيحمل كوكيز/توكن جلسة الموظف أصلًا (كوكيز host-only
	 بلا Domain)، فلا مشاركة جلسة مع staff/app ولا تحويل تلقائي من أي نطاق:
	 مجرد رابط يفتحه المستخدم بيده. لا يُستخدم داخل منطق الفصل المركزي
	 كاستثناء يربط النطاقات — مجرد عنوان عرض.

	بيرجع رابط كامل بالـ scheme والـ port من الطلب الحالي عشان يشتغل
	على dev server (نفس البورت لو جوه التطوير) وعلى الإنتاج (https).
	"""
	store_domain = ""
	fallback_domain = ""
	for domain in frappe.get_site_config().get("domains") or []:
		if not domain or "staff" in domain or "app" in domain:
			continue
		if "store." in domain and not store_domain:
			store_domain = domain
		if not fallback_domain:
			fallback_domain = domain
	store_domain = store_domain or fallback_domain

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
	"""يحدد الصفحة اللي بيوصل ليها اللي بيفتح "/" حسب نوع الحساب **ودور النطاق**:

	- Guest: مضيف staff → /staff/login (نسبي، نفس المضيف)؛ مضيف app → /desk
	  (وضيفه يهبط على دخول Desk القياسي)؛ غير ذلك → /biozone-home.
	- Website User: /biozone-home (صفحة المتجر نفسها ترفضه محليًا بـ403 تحت
	  staff/app عبر redirect_staff_away_from_store — بلا مغادرة للمضيف).
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
		role = get_current_domain_role()
		if role == "staff":
			return "/staff/login"
		if role == "app":
			# "/app" لا يُحسم كصفحة موقع (404)، فالمضيف يُوجَّه لـ/desk
			# وضيفه يهبط على دخول Desk القياسي — كله نفس المضيف.
			return "/desk"
		return "/biozone-home"

	if is_system_user(user):
		# مضيف app يعرض Desk حصرًا: أي System User (موظف أو إداري) يهبط
		# على /desk، ولا يرث /staff/dashboard من مسار الستاف — هذا الـhook
		# يغذي أيضًا home_page لدخول الإطار (/api/method/login) على app.
		if get_current_domain_role() == "app":
			return "/desk"
		return get_home_route_for_system_user(user)

	return "/biozone-home"


def set_host_aware_home_page():
	"""تثبيت home_page الواعي بالمضيف قبل التوجيه (before_request).

	السبب: إطار Frappe يخزّن home_page في الكاش لكل مستخدم بلا اعتبار
	للمضيف — فزيارة ضيف لمضيف app (نتيجتها /desk) كانت تسمم ضيوف مضيف
	المتجر (نتيجته /biozone-home) والعكس. هنا نضبط
	frappe.local.flags.home_page الذي يفحصه الإطار *قبل* الكاش
	(frappe/website/utils.py) — والـ flags نطاق-طلب (thread-local)
	فلا تسريب بين الطلبات ولا state مشترك. (تحققنا: لا متغيرات
	module-level للمضيف في التطبيق — المصدر الوحيد كان كاش الإطار.)

	- الجلسة محلولة قبل before_request (app.py: HTTPRequest ثم الخطافات)
	  فيُعتمد frappe.session.user بأمان.
	- الضيف: خريطة ثابتة صريحة للمضيفات المتبقية فقط — بلا DB وبلا أي
	  state خارج frappe.local (مضيف مجهول/تطوير → /biozone-home الافتراضي
	  الآمن، فيتجاوز قراءة خانة Guest المشتركة التي قد تكون قديمة).
	- www.biozone.pro يُعامل معاملة الجذر (يخدم المتجر عند الوصول
	  المباشر للأصل) — التحويل canonical الـ301 يتم على حافة Cloudflare
	  (قاعدة www-to-root)، لا raise تحويلية هنا أبدًا: معالج الاستثناءات
	  العام لا ينتج Location من before_request.
	- المسجلون: المنطق الواعي الكامل عبر get_website_user_home_page
	  (يحتاج الأدوار = قراءة DB — مقبول لمسار "/" فقط؛ البديل هو كاش
	  الإطار المسموم عبر المضيفات لنفس المستخدم).
	- روابط نسبية same-host (و301 الجذر لمضيفه المعلن) — لا عبور نطاقات.
	"""
	request = getattr(frappe.local, "request", None)
	if not request:
		return
	# P1-variant: request.host وحده هوية المضيف (nginx يعيد كتابة Host؛
	# X-Forwarded-Host قابل للتزوير من العميل عبر السلسلة — لا يُقرأ أبدًا).
	host = (getattr(request, "host", "") or "").split(":")[0].lower()
	path = getattr(request, "path", "") or ""
	if path.strip("/") != "":
		return
	user = getattr(getattr(frappe, "session", None), "user", None) or "Guest"
	if user == "Guest":
		if host in ("biozone.pro", "www.biozone.pro"):
			home = "/biozone-home"
		elif host == "staff.biozone.pro":
			home = "/staff/login"
		elif host == "app.biozone.pro":
			home = "/desk"
		else:
			# مضيف مجهول (تطوير/IP/alias): الافتراضي الآمن للمتجر. يكتفي
			# بتجاوز قراءة خانة Guest المشتركة — بلا كتابة كاش، بلا رفض،
			# بلا تحويل.
			home = "/biozone-home"
		frappe.local.flags.home_page = home
		return
	frappe.local.flags.home_page = get_website_user_home_page()


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


def get_csrf_token_safe() -> str:
	"""رمز CSRF للصفحات التي تستدعي APIs عبر fetch.

	في طلبات الويب الحقيقية يُرجع الرمز الفعلي. في سياقات بلا جلسة
	(bench console/اختبارات) يُرجع سلسلة فارغة بدل رمي استثناء، حتى لا
	تتعطل معاينة الصفحة — نماذج fetch لن تُستخدم هناك أصلًا.
	"""
	try:
		return frappe.sessions.get_csrf_token()
	except Exception:
		return ""


def get_header_context():
	"""Shared header state for any www page that includes site_header.html.
	Call this from the page's get_context() and merge the result in, e.g.:

		context.update(get_header_context())

	- is_logged_in: any authenticated user
	- user_full_name: shown in the header instead of the account icon
	- notif_unread_count: customer bell counter — Website Users only and
	  only when the notifications flag is on. Guests, staff and flag-off
	  cost zero notification queries.
	"""
	user = frappe.session.user
	is_logged_in = user != "Guest"

	user_full_name = None
	notif_unread_count = 0
	if is_logged_in:
		info = frappe.db.get_value("User", user,
		                           ["full_name", "user_type", "enabled"],
		                           as_dict=True)
		if info:
			user_full_name = info.full_name or user
			if (info.user_type == "Website User" and info.enabled
					and _notifications_flag_on()):
				from biozone_web.hooks import _BZ_NOTIFICATION_TYPES
				notif_unread_count = frappe.db.count(
					"Notification Log",
					{"for_user": user, "read": 0,
					 "type": ("in", list(_BZ_NOTIFICATION_TYPES))})

	return {
		"is_logged_in": is_logged_in,
		"user_full_name": user_full_name,
		"notif_unread_count": notif_unread_count,
	}


def _notifications_flag_on():
	"""Read-only flag check without importing the service (no cycles)."""
	try:
		val = frappe.get_conf().get("biozone_notifications_enabled", 0)
	except Exception:
		return False
	if isinstance(val, bool):
		return val
	if isinstance(val, (int, float)):
		return bool(val)
	if isinstance(val, str):
		return val.strip().lower() in ("1", "true", "yes", "on")
	return False


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


def _customer_has_field(fieldname):
	"""حارس نشر عام: هل حقل معيَّن موجود على Customer؟

	الكود قد يُنشر قبل تشغيل bench migrate الذي ينشئ الحقل. الفحص هنا
	يتيح للكود العمل بأمان في الفترة البينية (يتجاهل الحقل) بدل رمي
	استثناء.
	"""
	try:
		return bool(frappe.get_meta("Customer").has_field(fieldname))
	except Exception:
		return False


def customer_has_staff_review_field():
	"""هل حقل مراجعة الفئة موجود على Customer؟"""
	return _customer_has_field("staff_category_reviewed")


def customer_has_category_assigned_field():
	"""هل حقل التعيين الإداري للفئة موجود على Customer؟"""
	return _customer_has_field("category_assigned_by_staff")


def create_customer_for_user(user_email, full_name=None):
	"""ينشئ Customer مرتبطًا ببريد صريح، بنفس قيم وحماية المسار الكسول.

	الربط يتم عبر جدول Portal User الفرعي على Customer — نفس الآلية التي
	يتوقعها find_customer_for_current_user (البحث في Portal User بشرط
	user + parenttype = Customer)، فيجده get_or_create لاحقًا بلا تكرار.

	القيم مطابقة لمسار الإنشاء الكسول: customer_name من الاسم الكامل،
	customer_type = Individual، customer_group = الفئة الافتراضية
	(الجمهور حاليًا)، territory = الافتراضية، category_assigned_by_staff
	= 0 (غير معيَّن إداريًا — يظهر في قائمة انتظار الموظف).

	آمنة للتكرار: لو ربط موجود بالفعل تُرجع اسمه بلا إنشاء جديد (نفس
	حماية التحقق البعدي من سباق التزامن المستخدمة في المسار الكسول).
	"""
	user_email = (user_email or "").strip().lower()

	if not user_email or user_email == "Guest":
		frappe.throw(_("يجب تسجيل الدخول أولًا"))

	existing = frappe.db.get_value(
		"Portal User", {"user": user_email, "parenttype": "Customer"}, "parent"
	)
	if existing:
		return existing

	full_name = full_name or frappe.db.get_value("User", user_email, "full_name") or user_email

	new_customer = {
		"doctype": "Customer",
		"customer_name": full_name,
		"customer_type": "Individual",
		"customer_group": get_default_customer_group(),
		"territory": get_default_territory(),
		"portal_users": [{"user": user_email}],
	}
	if customer_has_staff_review_field():
		new_customer["staff_category_reviewed"] = 0
	if customer_has_category_assigned_field():
		new_customer["category_assigned_by_staff"] = 0

	customer = frappe.get_doc(new_customer)
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

	return create_customer_for_user(user_email)


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


# ---------- البند 5 (B5+B6d): عرض السعر حسب الفئة — سيرفر-سايد بالكامل ----------
#
# مصدر الحقيقة الوحيد للسعر هو محرك تسعير ERPNext نفسه
# (apply_pricing_rule على Standard Selling)، بنفس المدخلات التي تُستخدم
# عند إنشاء Sales Order. أي سطح عرض (كتالوج/رئيسية/سلة) يمر من هنا،
# فيتطابق السعر بالضرورة مع الطلب النهائي.

# قائمة البيع الوحيدة المعتمدة — لا توجد قوائم بيع أخرى للفئات (اتأكد حيًا:
# جدول Price List فيه Standard Selling وStandard Buying فقط)، فالتمييز بين
# الفئات يتم حصريًا عبر Pricing Rule (خصم %) فوق نفس السعر الأساسي.
PUBLIC_PRICE_LIST = "Standard Selling"

# الفئة العامة الاحتياطية لو Selling Settings بلا قيمة (دفاع فقط — القيمة
# الحية الحالية هي "الجمهور"، وهي نفسها فئة العميل الجديد الافتراضية).
PUBLIC_CUSTOMER_GROUP_FALLBACK = "الجمهور"


def get_public_customer_group():
	"""اسم فئة الجمهور (العامة) من الإعداد الحي، لا تخمين ثابت."""
	return (
		frappe.db.get_single_value("Selling Settings", "customer_group")
		or PUBLIC_CUSTOMER_GROUP_FALLBACK
	)


def get_customer_price_group(customer=None):
	"""الفئة السعرية الفعّالة لعميل — بلا أي fallback لفئة بديلة.

	- زائر/بلا ربط Customer → فئة الجمهور.
	- عميل على فئة الجمهور نفسها (أي حساب جديد قبل التفعيل) → الجمهور.
	- عميل على فئة غير موجودة/مجمّعة (is_group)/معطّلة → الجمهور مباشرة،
	  وليس لأي فئة بديلة (قاعدة "ممنوع fallback" في التصميم).
	- غير ذلك → اسم فئته المفعّلة كما هي.

	ملحوظة: المحرك نفسه لا يفحص disabled على Customer Group، لذلك الفحص
	هنا صريح وإلزامي قبل تمرير الفئة للمحرك أو للطلب.
	"""
	public_group = get_public_customer_group()

	if customer is None:
		customer = find_customer_for_current_user()

	if not customer:
		return public_group

	group = frappe.db.get_value("Customer", customer, "customer_group")

	if not group or group == public_group:
		return public_group

	group_row = frappe.db.get_value(
		"Customer Group", group, ["is_group", "disabled"], as_dict=True
	)

	if not group_row or group_row.is_group or group_row.disabled:
		return public_group

	return group


def is_active_price_customer(customer=None):
	"""هل العميل مؤهّل سعريًا (فئة مفعّلة غير عامة)؟"""
	return get_customer_price_group(customer) != get_public_customer_group()


def get_storefront_price_context(customer=None):
	"""سياق التسعير الكامل للطلب الحالي — تُشتق الفئة سيرفر-سايد دائمًا،
	ولا يُقبل أي customer_group من العميل (قرار اختبار 7)."""
	if customer is None:
		customer = find_customer_for_current_user()

	public_group = get_public_customer_group()
	group = get_customer_price_group(customer)

	return {
		"customer": customer,
		"customer_group": group,
		"is_active": bool(group and group != public_group),
		"is_guest": frappe.session.user == "Guest",
	}


def require_active_price_customer():
	"""بوابة تأكيد الطلب (البند 5): ترفض أي حساب بلا فئة مفعّلة.

	تستخدم find فقط (بلا إنشاء Customer) حتى لا تُنشأ صفوف يتيمة من
	محاولات مرفوضة. تُرجع (customer, customer_group) المؤكدين للاستخدام
	المباشر في Sales Order — بلا إعادة قراءة منفصلة.
	"""
	customer = find_customer_for_current_user()
	group = get_customer_price_group(customer)

	if group == get_public_customer_group():
		frappe.throw(
			_("يتعذّر تأكيد الطلب قبل تفعيل فئة حسابك. يُرجى التواصل مع إدارة المتجر لتفعيل الحساب."),
			frappe.PermissionError,
		)

	return customer, group


def get_effective_item_prices(item_codes, customer=None, customer_group=None):
	"""السعر النهائي (بعد خصم الفئة) لمجموعة أصناف، دفعة واحدة عبر محرك
	ERPNext الحقيقي — نفس المحرك الذي يسعّر صفوف Sales Order.

	تُرجع dict لكل كود مطلوب: {"price", "base_price", "discount_percentage"}.
	الأصناف بلا سعر أساسي تُرجع price=None (تُعامل كغير متاحة، كما قبل).

	ملحوظتان موثّقتان:
	- تُحسب بسعر الوحدة (qty=1). قواعد بحد أدنى للكمية (min_qty) لا يمكن
	  إنشاؤها من شاشة /staff/pricing أصلًا (دائمًا 0)، فإن وُجدت من Desk
	  يدويًا فقد يختلف سطر الطلب عن العرض — المحرك واحد والمدخلات واحدة
	  عدا الكمية.
	- أي عطل في المحرك يُترك ليُرمى (fail loud) — لا عرض لسعر بديل صامت.
	"""
	codes = []
	for code in item_codes or []:
		code = (code or "").strip() if isinstance(code, str) else ""
		if code and code not in codes:
			codes.append(code)

	result = {
		code: {"price": None, "base_price": None, "discount_percentage": 0.0}
		for code in codes
	}

	if not codes:
		return result

	base_rows = frappe.get_all(
		"Item Price",
		fields=["item_code", "price_list_rate", "uom"],
		filters={"price_list": PUBLIC_PRICE_LIST, "item_code": ["in", codes]},
	)

	meta_rows = frappe.get_all(
		"Item",
		fields=["item_code", "item_group", "stock_uom"],
		filters={"item_code": ["in", codes]},
	)
	meta_map = {r["item_code"]: r for r in meta_rows}

	# سد دفاعي (المرحلة 0): السعر المرجعي واحد بوحدة الصغرى. عند وجود
	# أكثر من سعر للصنف يُفضَّل المطابق لوحدة المخزون (أو بلا وحدة)،
	# وإلا الأول — السلوك القديم محفوظ عند السعر الوحيد.
	by_code = {}
	for r in base_rows:
		by_code.setdefault(r["item_code"], []).append(r)
	base_map = {}
	for code, rows in by_code.items():
		want = (meta_map.get(code) or {}).get("stock_uom") or ""
		pick = next(
			(r for r in rows if not (r.get("uom") or "").strip() or (r.get("uom") or "").strip() == want),
			rows[0],
		)
		base_map[code] = pick["price_list_rate"]

	pricable = [c for c in codes if base_map.get(c)]
	if not pricable:
		return result

	if customer_group is None:
		customer_group = get_customer_price_group(customer)

	if customer is None:
		customer = find_customer_for_current_user()

	company = get_default_company()
	currency = frappe.db.get_value("Company", company, "default_currency")

	from erpnext.accounts.doctype.pricing_rule.pricing_rule import apply_pricing_rule

	# pricing-guest-compat: narrow Administrator impersonation window around
	# the engine call only. ERPNext >= 16.36 gates apply_pricing_rule behind
	# Sales Order permission, but guests browse public prices (16.33 had no
	# gate). Direct session assignment — never frappe.set_user here, it
	# rewrites sid too. Restored in finally whatever happens. No writes,
	# no commit, no order creation inside: the engine path is read-only.
	previous_user = frappe.session.user
	frappe.session.user = "Administrator"
	try:
		engine_out = apply_pricing_rule(
			{
				"doctype": "Sales Order",
				"transaction_type": "selling",
				"selling_price_list": PUBLIC_PRICE_LIST,
				"price_list": PUBLIC_PRICE_LIST,
				"company": company,
				"currency": currency,
				"transaction_date": frappe.utils.today(),
				"ignore_pricing_rule": 0,
				"customer": customer,
				"customer_group": customer_group,
				"items": [
					{
						"doctype": "Sales Order Item",
						"item_code": code,
						"item_group": (meta_map.get(code) or {}).get("item_group"),
						"qty": 1,
						"stock_qty": 1,
						"uom": (meta_map.get(code) or {}).get("stock_uom"),
						"price_list_rate": base_map[code],
					}
					for code in pricable
				],
			}
		)
	finally:
		frappe.session.user = previous_user

	for code, row in zip(pricable, engine_out):
		base = base_map[code]
		price_list_rate = frappe.utils.flt(row.get("price_list_rate")) or base
		discount_amount = frappe.utils.flt(row.get("discount_amount"))
		result[code] = {
			"price": price_list_rate - discount_amount,
			"base_price": base,
			"discount_percentage": frappe.utils.flt(row.get("discount_percentage")),
		}

	return result


def render_state_page(
	context,
	title,
	message,
	hint="",
	http_status_code=None,
	order_name="",
	back_url="/staff/orders",
	back_label=None,
):
	"""صفحة حالة/رفض ناعمة بدل exception عام — القالب يعرضها عبر `error_state`.

	`http_status_code=404` للحالات المرفوضة (سجل غير موجود / فاتورة غير صالحة)،
	وبلا كود (200) لصفحات الحالة (بلا فاتورة / ملغى). لا traceback في أي حالة
	لأن لا استثناء يُرمى أصلًا. `back_url/back_label` يخصصان زر العودة
	(صفحات المتجر تستخدم مساراتها).
	"""
	context.error_state = True
	context.error_title = title
	context.error_message = message
	context.error_hint = hint or ""
	context.error_order = order_name or ""
	context.error_back_url = back_url or "/staff/orders"
	context.error_back_label = back_label or _("العودة إلى قائمة الطلبات")
	if http_status_code:
		context.http_status_code = http_status_code
	return context


def invoice_linked_to_order(si_name, order_name):
	"""ارتباط مثبت بين الفاتورة والطلب: مباشر عبر Sales Invoice Item،
	أو عبر Delivery Note معتمد (docstatus=1).

	أي فاتورة تمرَّر صراحةً لطلب لا تخصه يجب رفضها — لا fallback ولا عرض
	متبادل (خلل سلامة بيانات مثبت في order-delivery).
	"""
	if frappe.db.exists(
		"Sales Invoice Item", {"parent": si_name, "sales_order": order_name, "docstatus": ["!=", 2]}
	):
		return True
	dn_names = frappe.db.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": order_name, "docstatus": ["!=", 2]},
		pluck="parent",
	)
	for dn in {d for d in dn_names if d}:
		if frappe.db.get_value("Delivery Note", dn, "docstatus") != 1:
			continue
		if frappe.db.exists(
			"Sales Invoice Item", {"parent": si_name, "delivery_note": dn, "docstatus": ["!=", 2]}
		):
			return True
	return False
