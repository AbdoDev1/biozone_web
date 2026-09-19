import uuid

import frappe
from frappe import _


STORE_LOGIN_FAILED_MESSAGE = "هذا الحساب غير مصرح له بالدخول إلى المتجر"


def _fail_store_login():
	"""رفض دخول المتجر — إرجاع نظيف بلا مغلّف استثناء (نفس نمط
	 _fail_staff_login): الجسم {"message": النص الواضح} حصرًا مع 401.
	 يُستخدم لمساري الرفض الجديدين فقط (فارغ/نوع مرفوض) — مسارات الفشل
	 القائمة (كلمة خاطئة/غير موجود) تبقى على سلوك المحرك كما هي.
	"""
	frappe.clear_messages()
	frappe.local.response["http_status_code"] = 401
	return STORE_LOGIN_FAILED_MESSAGE


@frappe.whitelist(allow_guest=True)
def biozone_login(usr=None, pwd=None, remember_me: int = 0):
	# بوابة نوع المتجر (قرار فصل النطاقات): حسابات العملاء (Website User)
	# فقط. أي System User (موظف/أدمن) يُرفض هنا — بعد نجاح المصادقة لكن
	# قبل post_login — فلا تُنشأ أي جلسة ولا يُصدَر أي session cookie.
	# الرفض برسالة المتجر الواضحة نفسها، بلا redirect لأي نطاق آخر.
	if not isinstance(usr, str) or not usr.strip() or not isinstance(pwd, str) or not pwd:
		return _fail_store_login()
	login_manager = frappe.local.login_manager
	login_manager.authenticate(user=usr, pwd=pwd)
	if frappe.db.get_value("User", login_manager.user, "user_type") != "Website User":
		return _fail_store_login()
	login_manager.post_login()

	if frappe.utils.cint(remember_me):
		frappe.local.cookie_manager.set_cookie(
			"sid",
			frappe.session.sid,
			max_age=60 * 60 * 24 * 30,
			httponly=True,
		)

	return {
		"message": "Logged In",
		"home_page": "/biozone-home",
	}


STAFF_LOGIN_FAILED_MESSAGE = "البريد الإلكتروني أو كلمة المرور غير صحيحة"


def _fail_staff_login():
	"""فشل موحد لدخول الستاف — يمنع user enumeration حتى على مستوى الـAPI.

	إرجاع طبيعي (بلا throw) مع http_status_code=401: معالج Frappe يبني مفاتيح
	 exc/exception/exc_type/_server_messages فقط عند انتشار استثناء، فتبقى
	 الاستجابة الخام {"message": النص العام} حصرًا. القيم القديمة
	 (home_page/full_name من post_login سابق) تُحذف قبل الإرجاع، والعميل
	 (staff/login.html) يعرض ثابتًا محليًا دائمًا ولا يقرأ الجسم إطلاقًا.
	"""
	frappe.clear_messages()
	frappe.local.response.pop("home_page", None)
	frappe.local.response.pop("full_name", None)
	frappe.local.response["http_status_code"] = 401
	return STAFF_LOGIN_FAILED_MESSAGE


@frappe.whitelist(allow_guest=True, methods=["POST"])
def staff_login(usr=None, pwd=None):
	"""دخول الستاف عبر نطاق staff فقط — System Users دون غيرهم.

	- نفس آلية المصادقة القائمة في biozone_login (login_manager.authenticate
	  ثم post_login) — بلا آلية مخصصة، فيرث حد المعدل الداخلي تلقائيًا
	  (allow_consecutive_login_attempts / allow_login_after_fail).
	- ملحوظة 2FA مؤقتة وموثقة: مثل biozone_login تمامًا، يستدعي
	  authenticate+post_login مباشرة دون مسار LoginManager.login()، أي لا
	  يمر بفحص should_run_2fa/OTP. لا يُدَّعى أن دخول الستاف محمي بـ2FA —
	  بند مستقل لاحقًا لنقله للمسار القياسي.
	- رسالة فشل واحدة موحدة لكل الحالات (فارغ/مفقود/غير موجود/كلمة خاطئة/
	  معطل/Website User/قفل مؤقت) — بلا تحليل لنص الخطأ، وبلا مغلّف
	  استثناء في الجسم الخام (إرجاع + 401 بدل throw).
	- بلا remember_me عمدًا: جلسات الستاف قصيرة فقط.
	"""
	if not isinstance(usr, str) or not usr.strip() or not isinstance(pwd, str) or not pwd:
		return _fail_staff_login()
	login_manager = frappe.local.login_manager
	try:
		login_manager.authenticate(user=usr, pwd=pwd)
	except (frappe.AuthenticationError, frappe.SecurityException):
		return _fail_staff_login()
	login_manager.post_login()

	user_type = frappe.db.get_value("User", login_manager.user, "user_type")
	if user_type != "System User":
		login_manager.logout()
		return _fail_staff_login()

	return {
		"message": "Logged In",
		"home_page": "/staff/dashboard",
	}


@frappe.whitelist(allow_guest=True)
def biozone_sign_up(email: str, full_name: str, phone: str, pwd: str):
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

	# User.get_roles() مش موجودة على مستند Document (الخطأ اللي
	# ظهر فعليًا). الطريقة الصح: نقرا جدول roles الفرعي (Has Role)
	# على المستند مباشرة، مش عن طريق دالة مش موجودة.
	existing_roles = {r.role for r in user.get("roles", [])}

	if "Biozone Storefront Customer" not in existing_roles:
		user.add_roles("Biozone Storefront Customer")

	# إنشاء Customer فور التسجيل (بنفس قيم المسار الكسول عبر
	# create_customer_for_user: نفس ربط Portal User الذي يجده
	# find_customer_for_current_user، ونفس الفئة الافتراضية، مع
	# staff_category_reviewed = 0 ليظهر فورًا في /staff/customers).
	# قبل commit عمدًا: سياسة ذرّية مطابقة لمسار التسجيل الحالي
	# (commit واحد أدناه) — أي فشل هنا يُلغي التسجيل كله بلا مستخدم
	# يتيم بلا عميل. الدالة آمنة للتكرار (تُرجع الربط الموجود).
	from biozone_web.utils import create_customer_for_user

	create_customer_for_user(email, full_name=full_name)

	frappe.db.commit()

	login_manager = frappe.local.login_manager
	login_manager.authenticate(user=email, pwd=pwd)
	login_manager.post_login()

	return {
		"message": "Account Created",
		"home_page": "/biozone-home",
	}



@frappe.whitelist(allow_guest=True)
def biozone_forgot_password(email: str):
	from frappe.core.doctype.user.user import reset_password

	email = (email or "").strip().lower()

	if not email:
		frappe.throw(_("من فضلك اكتب البريد الإلكتروني"))

	reset_password(user=email)
	frappe.clear_messages()

	return {
		"message": _(
			"إذا كان هذا البريد الإلكتروني مسجلًا لدينا، "
			"فسيصلك رابط لإعادة تعيين كلمة المرور. "
			"يُرجى التحقق من صندوق الوارد."
		)
	}


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def biozone_get_cart_prices(item_codes):
	"""أسعار السلة من السيرفر حسب فئة الطالب (البند 5).

	الزائر وأي عميل يستدعيها بحرية (تصفح/سلة مفتوحة للجميع)، وتُرجع لكل
	كود: السعر النهائي بعد خصم فئة الطالب فقط + السعر الأساسي + النسبة.
	الفئة تُشتق سيرفر-سايد دائمًا من ربط المستخدم — لا يُقبل أي
	customer_group من العميل، فلا سبيل لقراءة سعر فئة أخرى (اختبار 7).

	الأصناف بلا سعر أساسي تُرجع price=None لتُحذف من السلة.
	"""
	from biozone_web.utils import (
		get_effective_item_prices,
		get_storefront_price_context,
	)

	if isinstance(item_codes, str):
		item_codes = frappe.parse_json(item_codes)

	codes = []
	for code in item_codes or []:
		if isinstance(code, str):
			code = code.strip()
			if code and code not in codes:
				codes.append(code)

		if len(codes) >= 200:
			break

	ctx = get_storefront_price_context()

	return {
		"ok": True,
		"prices": get_effective_item_prices(
			codes,
			customer=ctx["customer"],
			customer_group=ctx["customer_group"],
		),
		"customer_group": ctx["customer_group"],
		"is_active": ctx["is_active"],
		"is_guest": ctx["is_guest"],
	}


@frappe.whitelist(methods=["POST"])
def biozone_confirm_order(items):
	"""ينشئ Sales Order من سلة المتجر للمستخدم الحالي.

	الملكية: الطلب يُنشأ داخل نافذة انتحال Administrator مؤقتة
	(تعيين مباشر لـ frappe.session.user — ممنوع frappe.set_user())
	لتغطية فحص Account الداخلي (account_perm_check عبر session.user)،
	ثم يُصحَّح owner فورًا بعد insert() عبر db_set إلى ordering_user
	(المستخدم الحقيقي المحفوظ قبل النافذة) ويُعاد تحميل المستند.
	الجلسة تُستعاد في finally. راجع التعليق حول so.insert().

	الصلاحيات: Website User لا يملك DocPerm لإنشاء Sales Order
	(لا يوجد أي تعديل من Desk ولا fixtures)، لذلك يُستخدم
	so.insert(ignore_permissions=True) لفحص الإنشاء فقط، مع بقاء
	owner = المستخدم الحقيقي. كل فحوصات الروابط والقيم الإلزامية
	تظل مفعّلة (ignore_links لم يُستخدم).

	ملحوظة مهمة: so.insert(ignore_permissions=True) بيتجاوز فحص
	صلاحية مستند الـSales Order نفسه بس. أي مستند تاني بتفتحه
	ERPNext من جوّه validate() (مثلًا Account لحساب الضريبة/الحساب
	الافتراضي) بييجي بفحص صلاحية منفصل خاص بيه، مش مغطّى بالـ
	ignore_permissions بتاع الـSales Order. عشان كده بنستخدم
	frappe.flags.ignore_permissions مؤقتًا — ده flag خاص بالـrequest
	الحالي بس (مش الـsession، مش الـcookie، مش صلاحية دايمة على أي
	Role)، بيتصفّر تلقائيًا في أول كل request جديد. ملحوظة محدّثة:
	انتحال Administrator المؤقت هنا (تعيين مباشر، بلا set_user)
	ضيّق على so.insert() + تصحيح owner فقط مع استعادة في finally —
	وليس الحيلة القديمة الواسعة التي سببت تسجيل الخروج التلقائي. الفرق عن منح Role العميل صلاحية Account: Read: ده مش
	بيغيّر أي Role، فالعميل لسه ميقدرش يعمل curl مباشر لـ
	/api/resource/Account وينجح — البوابة بتتفتح جوّه كودنا احنا بس.

	نطاق الـflag: يغطي كامل كتلة try بما فيها قراءة/تأكيد الفئة وإنشاء
	الـSales Order — أي مستند تفتحه ERPNext من جوّه validate() (مثلًا
	Account) يخضع لفحص صلاحية منفصل غير مغطّى بـignore_permissions
	الخاص بالـSales Order. (ملحوظة البند 5: إنشاء Customer جديد لم يعد
	يحدث في هذا المسار أصلًا — البوابة ترفض قبل الإنشاء — لكن الـflag
	يبقى لازمًا لفحوصات Account الداخلية أثناء so.insert()).

	البند 5 (عرض السعر حسب الفئة): (1) البوابة require_active_price_customer
	ترفض الزائر (بالفحص فوق) وأي مسجّل بلا فئة مفعّلة — التأكيد للفئة
	المفعّلة فقط. (2) الفئة المؤكدة تُمرَّر صراحة في customer_group وإلا
	ملأها النظام بالافتراضي العام وضاعت خصومات الفئة. (3) أسعار الطلب
	يحسبها محرك ERPNext من (الفئة، Standard Selling) — العميل يرسل
	الأكواد والكميات فقط، فلا أثر لأي سعر يُعدَّل يدويًا في السلة.
	"""
	if frappe.session.user == "Guest":
		frappe.throw(
			_("يجب تسجيل الدخول أولًا لتأكيد الطلب"),
			frappe.PermissionError,
		)

	if isinstance(items, str):
		items = frappe.parse_json(items)

	if not items:
		frappe.throw(_("السلة فارغة"))

	from biozone_web.utils import (
		get_default_company,
		require_active_price_customer,
	)

	so_items = []

	for it in items:
		item_code = (it.get("item_code") or "").strip()
		qty = frappe.utils.flt(it.get("quantity"))

		if not item_code or qty <= 0:
			frappe.throw(_("بيانات صنف غير صحيحة في السلة"))

		item = frappe.db.get_value(
			"Item",
			item_code,
			["disabled"],
			as_dict=True,
		)

		if not item or item.disabled:
			frappe.throw(
				_("أحد الأصناف في السلة لم يعد متاحًا، يرجى تحديث السلة")
			)

		so_items.append(
			{
				"item_code": item_code,
				"qty": qty,
			}
		)

	# الـflag بيتفعّل هنا، قبل إنشاء/جلب الـCustomer، مش بعده —
	# راجع الملحوظة في docstring الدالة. بنحفظ القيمة السابقة (مش
	# بنفترض إنها False) ونرجّعها في finally زي ما هي، عشان لو
	# الدالة دي اتنادت يومًا من سياق فيه ignore_permissions شغّال
	# بالفعل، ميتلغيش من تحته.
	previous_ignore_permissions = getattr(
		frappe.flags,
		"ignore_permissions",
		False,
	)
	frappe.flags.ignore_permissions = True

	try:
		# البند 5: تأكيد الطلب للفئة المفعّلة فقط — الزائر مرفوض بالفحص
		# فوق، وهنا يُرفض أي مسجّل بلا فئة مفعّلة (الجمهور/بلا ربط/معطّلة)
		# قبل أي كتابة. البوابة تستخدم find فقط (بلا إنشاء Customer) حتى
		# لا تُنشأ صفوف يتيمة من محاولات مرفوضة، وتُرجع الفئة المؤكدة
		# لتمريرها صراحة للطلب أدناه.
		customer, customer_group = require_active_price_customer()

		# الملكية: ordering_user هو الجلسة الحقيقية قبل أي انتحال.
		# نافذة الانتحال أدناه تضبط owner مؤقتًا على Administrator،
		# لذلك يُصحَّح فورًا بعد insert() عبر db_set (المسار الثاني).
		ordering_user = frappe.session.user
		max_attempts = 3
		so = None

		for attempt in range(max_attempts):
			try:
				so = frappe.get_doc(
					{
						"doctype": "Sales Order",
						"customer": customer,
						# البند 5: تمرير صريح إلزامي — مستند Sales Order الجديد
						# يملأ customer_group تلقائيًا بالقيمة الافتراضية العامة
						# (الجمهور) ما لم تُمرَّر صراحة (مُثبت حيًا)، فتُمرَّر هنا
						# الفئة المؤكدة من البوابة ليعمل محرك الخصم عليها.
						"customer_group": customer_group,
						"company": get_default_company(),
						"selling_price_list": "Standard Selling",
						"delivery_date": frappe.utils.add_days(
							frappe.utils.nowdate(),
							3,
						),
						"items": so_items,
					}
				)

				# نافذة انتحال Administrator مؤقتة (تعيين مباشر فقط —
				# ممنوع frappe.set_user() في أي اتجاه): فحص Account الداخلي
				# (account_perm_check في party.py) يعتمد على frappe.session.user
				# ولا يتأثر بـ frappe.flags.ignore_permissions. ordering_user
				# محفوظ مسبقًا من الجلسة الحقيقية، والنافذة ضيقة على
				# so.insert() + تصحيح owner فقط، بلا commit أو حفظ آخر.
				original_session_user = frappe.session.user
				try:
					frappe.session.user = "Administrator"
					so.insert(ignore_permissions=True)
					# تصحيح الملكية بعد الإدراج (المسار الآمن الثاني):
					# set_user_and_timestamp() عيّن owner/modified_by =
					# Administrator على الأب وكل صف فرعي وقت insert()،
					# فنرجعها للمستخدم الحقيقي في DB والذاكرة معًا.
					# ملاحظة: modified_by للأب أيضًا لم يُصحَّح سابقًا
					# (كان التصحيح السابق owner فقط) — يُصحَّح هنا معه.
					# الجداول الفرعية (items/payment_schedule/pricing_rules/...)
					# تُكتشف من meta.get_table_fields() بدل تعداد items يدويًا،
					# حتى لا يبقى جدول منسي (payment_schedule تُبنى أثناء
					# validate() وقت الانتحال بالضبط).
					if so.owner != ordering_user:
						so.db_set("owner", ordering_user, update_modified=False)
					if so.modified_by != ordering_user:
						so.db_set("modified_by", ordering_user, update_modified=False)

					for df in so.meta.get_table_fields():
						for child_row in (so.get(df.fieldname) or []):
							try:
								needs_fix = (
									child_row.owner != ordering_user
									or child_row.modified_by != ordering_user
								)
							except AttributeError:
								continue
							if needs_fix:
								child_row.db_set("owner", ordering_user, update_modified=False)
								child_row.db_set("modified_by", ordering_user, update_modified=False)

					so.reload()
				finally:
					frappe.session.user = original_session_user

				# ضمان صريح: owner يجب أن يكون المستخدم الذي أرسل
				# الطلب. الفحص هنا (داخل نفس المحاولة، قبل أي commit)
				# عشان أي rollback لاحق في الـ except الخارجي يلغي
				# الإدراج بالكامل من غير ما يترك أثر جزئي.
				if so.owner != ordering_user:
					frappe.log_error(
						title="Sales Order owner mismatch",
						message=(
							f"Expected owner: {ordering_user}\n"
							f"Actual owner: {so.owner}\n"
							f"Sales Order: {so.name}"
						),
					)

					try:
						frappe.delete_doc(
							"Sales Order",
							so.name,
							ignore_permissions=True,
							force=True,
						)
					except Exception:
						frappe.log_error(
							frappe.get_traceback(),
							"Failed to delete Sales Order after owner mismatch",
						)

					frappe.throw(_("تعذر تأكيد الطلب بشكل آمن، يرجى المحاولة مرة أخرى"))

				# لا commit يدوي هنا: النجاح يُترك لـ Frappe يثبّته
				# تلقائيًا في نهاية الـ request. الـ commit الوحيد
				# اليدوي في هذه الدالة هو ثبات Customer فوق (مقصود
				# ومستقل عن مصير Sales Order).
				break

			except frappe.QueryDeadlockError:
				frappe.db.rollback()

				if attempt == max_attempts - 1:
					raise

				import time

				time.sleep(0.1 * (attempt + 1))

		if so is None:
			frappe.throw(_("تعذر تأكيد الطلب، يرجى المحاولة مرة أخرى"))

	except Exception:
		frappe.db.rollback()
		raise

	finally:
		# يرجّع القيمة اللي كانت شغّالة قبل الدالة دي بالظبط، مش False
		# فرضًا — نفس السبب اللي فوق.
		frappe.flags.ignore_permissions = previous_ignore_permissions

	return {
		"ok": True,
		"sales_order": so.name,
		"redirect": f"/order-confirmed?name={so.name}",
	}


def _set_customer_account_type(customer, customer_group):
	if not frappe.db.exists("Customer", customer):
		frappe.throw(_("العميل غير موجود"))

	if not frappe.db.exists(
		"Customer Group",
		{
			"name": customer_group,
			"is_group": 0,
			"disabled": 0,
		},
	):
		frappe.throw(_("نوع الحساب المختار غير صالح"))

	doc = frappe.get_doc("Customer", customer)

	# كتابة غير مشروطة عند نجاح الفحوصات: أي استدعاء ناجح من الموظف —
	# تغيير فئة، أو اعتماد "الجمهور" صراحة بإرسالها كقيمة — يُسجَّل
	# كتعيين إداري (category_assigned_by_staff = 1) حتى لو الفئة المختارة
	# مطابقة للفئة الحالية. (حارسا الحقلين للفترة البينية قبل migrate فقط.)
	from biozone_web.utils import (
		customer_has_category_assigned_field,
		customer_has_staff_review_field,
	)

	doc.customer_group = customer_group
	if customer_has_category_assigned_field():
		doc.category_assigned_by_staff = 1
	if customer_has_staff_review_field():
		doc.staff_category_reviewed = 1
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"customer": doc.name,
		"customer_group": doc.customer_group,
		"category_assigned_by_staff": frappe.utils.cint(doc.get("category_assigned_by_staff")),
		"staff_category_reviewed": frappe.utils.cint(doc.get("staff_category_reviewed")),
	}


@frappe.whitelist()
def get_available_account_types():
	from biozone_web.utils import require_staff_access

	require_staff_access()

	return frappe.get_all(
		"Customer Group",
		filters={"is_group": 0, "disabled": 0},
		fields=["name"],
		order_by="name",
	)


@frappe.whitelist()
def staff_set_customer_account_type(customer=None, customer_group=None, updates=None):
	from biozone_web.utils import require_staff_access

	require_staff_access()

	if updates is not None:
		return _set_customer_account_types_batch(updates)

	return _set_customer_account_type(customer, customer_group)


def _set_customer_account_types_batch(updates):
	"""حفظ دفعي لفئات العملاء عبر نفس مسار الصف الواحد.

	يقبل قائمة عناصر [{customer, customer_group}] (أو نفسها كنص JSON).
	كل صف يُتحقق منه ويُحفظ مستقلًا عبر _set_customer_account_type —
	فشل صف لا يمنع باقي الصفوف (نتائج جزئية لكل صف، بلا معاملة ذرية
	عابرة للصفوف — نفس استقلالية الحفظ المتتابع المستخدمة في صفحات
	الموظف الأخرى). أي خطأ في صف يُلفّ معامله الخاص (rollback) وتُمسح
	رسائله حتى لا تتسرب لرد الصفوف التالية.
	"""
	if isinstance(updates, str):
		updates = frappe.parse_json(updates)

	if not isinstance(updates, (list, tuple)):
		frappe.throw(_("صيغة الدفعة غير صالحة"))

	results = []
	for item in updates:
		item = item or {}
		name = (item.get("customer") or "").strip()
		group = (item.get("customer_group") or "").strip()
		if not name or not group:
			results.append(
				{
					"ok": False,
					"customer": name or None,
					"error": _("بيانات الصف غير مكتملة"),
				}
			)
			continue
		try:
			saved = _set_customer_account_type(name, group)
			results.append({"ok": True, **saved})
		except Exception as e:
			frappe.db.rollback()
			frappe.clear_messages()
			results.append(
				{
					"ok": False,
					"customer": name,
					"error": str(e) or _("تعذر حفظ هذا الصف"),
				}
			)

	updated = sum(1 for r in results if r.get("ok"))
	return {
		"ok": True,
		"updated": updated,
		"failed": len(results) - updated,
		"results": results,
	}


def _sync_item_barcodes(doc, new_barcodes: list):
	"""يزامن جدول barcodes الفرعي بالفرق — إضافة/حذف فقط، بلا DB مباشرة."""
	existing = [(r.barcode or "").strip() for r in (doc.get("barcodes") or []) if (r.barcode or "").strip()]
	existing_set = set(existing)
	new_set = set(new_barcodes or [])
	# حذف المحذوفة
	for row in list(doc.get("barcodes") or []):
		bc = (row.barcode or "").strip()
		if bc and bc not in new_set:
			doc.remove(row)
	# إضافة الجديدة
	for bc in new_barcodes or []:
		if bc not in existing_set:
			doc.append("barcodes", {"barcode": bc})


@frappe.whitelist(methods=["POST"])
def staff_save_item(
	item_code: str | None,
	new_item_code: str,
	item_name: str,
	item_group: str,
	brand: str | None = None,
	stock_uom: str | None = None,
	price: str | float | None = None,
	disabled: int = 0,
	barcodes=None,
):
	"""حفظ صنف + سعره + باركوداته (جدول Item Barcode القياسي — بند 4).

	barcodes: قائمة نصوص (تُطبَّع وتُفرَّغ وتُزال تكراراتها). التحديث
	بالفرق: إضافة الصفوف الجديدة وحذف المحذوفة فقط عبر doc.append/
	إزالة الصفوف ثم save — بلا كتابة مباشرة في DB.
	مفتاح barcodes الغائب (None — واجهة قديمة مخزنة لم ترسله) يعني
	"لا تلمس الباركودات الموجودة"، بينما القائمة الفارغة الصريحة []
	تعني "امسح الكل عمدًا". لا يُخلط بين الحالتين أبدًا.
	التحقق: BARCODE_CONFLICT_WITH_OTHER_ITEM عند تسجيل باركود مسجّل
	لصنف مختلف (نفس قاعدة تصميم الاستيراد بالجملة).
	"""
	from biozone_web.utils import require_staff_access

	require_staff_access()

	new_item_code = (new_item_code or "").strip()
	item_name = (item_name or "").strip()

	if not new_item_code or not item_name or not item_group:
		return {
			"ok": False,
			"error": _("من فضلك أكمل اسم الصنف والكود والمجموعة"),
		}

	# تطبيع قائمة الباركودات: نصوص مقصوصة بلا فراغات ولا تكرار.
	# الغياب (None) ≠ الإفراغ الصريح ([]): المفتاح الغائب يعني أن الواجهة
	# لم ترسل أي بيانات باركود (نسخة قديمة مخزنة) فتُترك الصفوف الموجودة
	# كما هي؛ أما [] المرسلة صراحة فتعني مسح الكل عمدًا.
	barcodes_provided = barcodes is not None
	if barcodes is None:
		barcodes = []
	elif isinstance(barcodes, str):
		try:
			barcodes = frappe.parse_json(barcodes)
		except Exception:
			barcodes = [barcodes]
	if not isinstance(barcodes, (list, tuple)):
		barcodes = [barcodes]
	seen = set()
	new_barcodes = []
	for bc in barcodes or []:
		bc = (str(bc) if bc is not None else "").strip()
		if not bc or bc in seen:
			continue
		seen.add(bc)
		new_barcodes.append(bc)

	# الكود النهائي بعد إعادة التسمية المحتملة — يُستخدم في فحص التعارض.
	final_code = new_item_code if not item_code or new_item_code != item_code else item_code

	# فحص التعارض قبل أي كتابة: باركود مسجّل لصنف آخر، أو يطابق كود
	# صنف آخر مباشرة (غموض في مسح B9).
	for bc in new_barcodes:
		conflict = frappe.db.get_value("Item Barcode", {"barcode": bc}, "parent")
		if conflict and conflict != final_code:
			return {
				"ok": False,
				"error": _("BARCODE_CONFLICT_WITH_OTHER_ITEM: الباركود {0} مسجل بالفعل للصنف {1}").format(bc, conflict),
			}
		if bc != final_code and frappe.db.exists("Item", bc):
			return {
				"ok": False,
				"error": _("BARCODE_CONFLICT_WITH_OTHER_ITEM: الباركود {0} يطابق كود صنف آخر موجود").format(bc),
			}

	if item_code:
		if not frappe.db.exists("Item", item_code):
			return {
				"ok": False,
				"error": _("الصنف غير موجود"),
			}

		if new_item_code != item_code:
			frappe.rename_doc(
				"Item",
				item_code,
				new_item_code,
				force=True,
			)

		doc = frappe.get_doc("Item", new_item_code)
		doc.item_name = item_name
		doc.item_group = item_group
		doc.brand = brand or None

		doc.disabled = frappe.utils.cint(disabled)
		# حارس الوحدة: لا تُكتب إلا قيمة مرسلة فعلًا ومختلفة عن الحالية،
		# حتى لا يطلق كل حفظ فحص check_stock_uom_with_bin أو فحص الرابط
		# على قيمة قديمة/افتراضية من الواجهة (سبب الـ417 العام السابق).
		submitted_uom = (stock_uom or "").strip()
		if submitted_uom and submitted_uom != doc.stock_uom:
			doc.stock_uom = submitted_uom
		# المزامنة فقط عند إرسال المفتاح فعلًا — الغائب يترك الموجود.
		if barcodes_provided:
			_sync_item_barcodes(doc, new_barcodes)
		doc.save(ignore_permissions=True)

	else:
		if frappe.db.exists("Item", new_item_code):
			return {
				"ok": False,
				"error": _("الكود ده مستخدم بالفعل لصنف تاني"),
			}

		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": new_item_code,
				"item_name": item_name,
				"item_group": item_group,
				"brand": brand or None,
				"stock_uom": stock_uom or "Nos",
				"is_stock_item": 1,
				"disabled": frappe.utils.cint(disabled),
				"barcodes": [{"barcode": bc} for bc in new_barcodes],
			}
		)

		doc.insert(ignore_permissions=True)

	if price not in (None, ""):
		price_value = frappe.utils.flt(price)

		existing_price = frappe.db.get_value(
			"Item Price",
			{
				"item_code": doc.item_code,
				"price_list": "Standard Selling",
			},
			"name",
		)

		if existing_price:
			frappe.db.set_value(
				"Item Price",
				existing_price,
				"price_list_rate",
				price_value,
			)
		else:
			frappe.get_doc(
				{
					"doctype": "Item Price",
					"item_code": doc.item_code,
					"price_list": "Standard Selling",
					"price_list_rate": price_value,
				}
			).insert(ignore_permissions=True)

	frappe.db.commit()

	return {
		"ok": True,
		"item_code": doc.item_code,
		"barcodes": [(r.barcode or "").strip() for r in (doc.get("barcodes") or []) if (r.barcode or "").strip()],
	}


@frappe.whitelist(methods=["POST"])
def staff_disable_item(item_code: str):
	from biozone_web.utils import require_staff_access

	require_staff_access()

	if not frappe.db.exists("Item", item_code):
		return {
			"ok": False,
			"error": _("الصنف غير موجود"),
		}

	frappe.db.set_value("Item", item_code, "disabled", 1)
	frappe.db.commit()

	return {
		"ok": True,
	}


def _pilot_stock_fail(error_code: str, message: str):
	"""فشل منظم لمسار المخزون (Pilot عقد الأخطاء — الحالات الخمس فقط).

	يضبط 422 (تحقق أعمال: لا 417 الخاصة بشرط Expect في HTTP، ولا 200
	التي تخفي التمييز بين التحقق والصلاحيات وفشل الخادم) ويُرجع جسمًا
	نظيفًا {ok, error, error_code, request_id}. على مسار الإرجاع الطبيعي
	لا يحقن الإطار أي exception/_server_messages/exc_type (مثبت حيًا
	بنفس الآلية في _fail_staff_login). request_id فريد لكل استدعاء.
	"""
	frappe.local.response["http_status_code"] = 422
	return {
		"ok": False,
		"error": message,
		"error_code": error_code,
		"request_id": uuid.uuid4().hex,
	}


@frappe.whitelist(methods=["POST"])
def staff_log_stock_movement(
	item_code: str,
	movement_type: str,
	uom: str,
	qty: float,
	note: str = "",
	rate: str | float | None = None,
):
	from biozone_web.utils import (
		get_default_warehouse,
		require_staff_access,
	)

	require_staff_access()

	if movement_type not in ("in", "out"):
		return _pilot_stock_fail("STOCK_BAD_TYPE", _("نوع الحركة غير صحيح"))

	qty = frappe.utils.flt(qty)

	if qty <= 0:
		return _pilot_stock_fail("STOCK_BAD_QTY", _("الكمية يجب أن تكون أكبر من صفر"))

	item = frappe.db.get_value(
		"Item",
		item_code,
		["item_name", "stock_uom", "disabled"],
		as_dict=True,
	)

	if not item or item.disabled:
		return _pilot_stock_fail("STOCK_BAD_ITEM", _("الصنف غير موجود أو غير مفعّل"))

	conversion_factor = 1.0

	if uom != item.stock_uom:
		conversion_factor = frappe.db.get_value(
			"UOM Conversion Detail",
			{
				"parent": item_code,
				"parenttype": "Item",
				"uom": uom,
			},
			"conversion_factor",
		)

		if not conversion_factor:
			return _pilot_stock_fail("STOCK_BAD_UOM", _("الوحدة المختارة غير معرّفة لهذا الصنف"))

	warehouse = get_default_warehouse()
	purpose = (
		"Material Receipt"
		if movement_type == "in"
		else "Material Issue"
	)

	if not frappe.db.exists("Stock Entry Type", purpose):
		return _pilot_stock_fail(
			"STOCK_BAD_ENTRY_TYPE",
			_(
				'نوع حركة المخزون "{0}" غير معرّف في النظام — '
				"يرجى إعداده أولًا"
			).format(purpose),
		)

	current_valuation_rate = frappe.utils.flt(
		frappe.db.get_value(
			"Bin",
			{
				"item_code": item_code,
				"warehouse": warehouse,
			},
			"valuation_rate",
		)
	)

	item_row = {
		"item_code": item_code,
		"qty": qty,
		"uom": uom,
		"conversion_factor": conversion_factor,
		"t_warehouse": (
			warehouse if movement_type == "in" else None
		),
		"s_warehouse": (
			warehouse if movement_type == "out" else None
		),
	}

	if movement_type == "in":
		effective_rate = (
			frappe.utils.flt(rate) or current_valuation_rate
		)

		if effective_rate <= 0:
			frappe.throw(
				_(
					"هذا الصنف مفيش له سعر تكلفة مسجل من قبل — "
					"من فضلك أدخل سعر التكلفة للوحدة قبل تسجيل "
					"أول حركة وارد له"
				)
			)

		item_row["basic_rate"] = effective_rate

	else:
		if current_valuation_rate <= 0:
			frappe.throw(
				_(
					"لا يمكن تسجيل صرف لهذا الصنف لأنه ليس له سعر "
					"تكلفة مسجل بعد — سجّل حركة وارد أولًا وحدد سعر "
					"التكلفة"
				)
			)

	stock_entry = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": purpose,
			"purpose": purpose,
			"to_warehouse": (
				warehouse if movement_type == "in" else None
			),
			"from_warehouse": (
				warehouse if movement_type == "out" else None
			),
			"remarks": note or None,
			"items": [item_row],
		}
	)

	stock_entry.insert()
	stock_entry.submit()

	return {
		"ok": True,
		"stock_entry": stock_entry.name,
	}


@frappe.whitelist(methods=["POST"])
def staff_confirm_order(order_name: str):
	"""نقطة التوافق القديمة — تحوّل الآن إلى التدفق الجديد (تسليم + فاتورة).

	كانت هذه الدالة تعتمد الطلب وتنشئ Delivery Note فقط بلا فحص باركود
	وبلا فاتورة (تدفق ما قبل B9). منذ B9 صارت مجرد غلاف يستدعي
	staff_confirm_delivery الذي يفرض تأكيد كل الأصناف وينشئ الفاتورة
	المعتمَدة في نفس اللحظة، مع منع التكرار. تُبقى هنا حتى تُحدَّث كل
	الواجهات القديمة، ثم تُحذف.
	"""
	result = staff_confirm_delivery(order_name)
	if not result.get("ok"):
		return result
	return {
		"ok": True,
		"sales_order": result.get("sales_order"),
		"delivery_note": result.get("delivery_note"),
		"sales_invoice": result.get("sales_invoice"),
		"redirect": result.get("redirect"),
	}


def _create_delivery_note_for_order(so, warehouse):
	dn = frappe.get_doc(
		{
			"doctype": "Delivery Note",
			"customer": so.customer,
			"company": so.company,
			"delivery_date": frappe.utils.today(),
			"set_warehouse": warehouse,
			"items": [
				{
					"item_code": it.item_code,
					"item_name": it.item_name,
					"description": it.description or it.item_name,
					"qty": it.qty,
					"uom": it.uom,
					"rate": it.rate,
					"amount": it.amount,
					"against_sales_order": so.name,
					"so_detail": it.name,
					"warehouse": warehouse,
				}
				for it in so.items
			],
		}
	)

	dn.insert(ignore_permissions=True)
	dn.submit()

	return dn


@frappe.whitelist(methods=["POST"])
def staff_toggle_customer_group(customer_group: str, disabled: int):
	from biozone_web.utils import require_staff_access

	require_staff_access()

	if not frappe.db.exists("Customer Group", customer_group):
		return {
			"ok": False,
			"error": _("الفئة غير موجودة"),
		}

	frappe.db.set_value(
		"Customer Group",
		customer_group,
		"disabled",
		frappe.utils.cint(disabled),
	)
	frappe.db.commit()

	return {
		"ok": True,
	}


@frappe.whitelist(methods=["POST"])
def staff_create_customer_group(customer_group_name: str):
	"""ينشئ فئة عميل جديدة (Customer Group) تظهر في تاب "الفئات" وتاب
	"الخصومات" بعد كده.

	Customer Group في Frappe شجرية (Tree/NestedSet)، يعني أي فئة جديدة
	لازم يكون ليها parent_customer_group. بدل افتراض اسم زي
	"All Customer Groups" (نفس نوع الافتراض العام اللي سبب مشكلة قبل
	كده في staff_set_item_discount)، بنجيب الأب بفحص حي فعلي: بنقرأ كل
	قيم parent_customer_group الفريدة للفئات (leaf) الموجودة بالفعل.
	لو القيمة مش واحدة (يا مفيش فئات أصلًا، يا فيه أكتر من أب — يعني
	الشجرة بقت متداخلة مش مسطّحة زي ما افترضنا)، بنرفض وبنطلب تدخل
	يدوي بدل ما نخمّن.
	"""
	from biozone_web.utils import require_staff_access

	require_staff_access()

	customer_group_name = (customer_group_name or "").strip()

	if not customer_group_name:
		return {
			"ok": False,
			"error": _("من فضلك أدخل اسم الفئة"),
		}

	if frappe.db.exists("Customer Group", customer_group_name):
		return {
			"ok": False,
			"error": _("فيه فئة بنفس الاسم موجودة بالفعل"),
		}

	existing_parents = frappe.db.sql(
		"""
		select distinct parent_customer_group
		from `tabCustomer Group`
		where is_group = 0
		""",
		as_dict=True,
	)
	distinct_parents = {
		row.parent_customer_group for row in existing_parents if row.parent_customer_group
	}

	if len(distinct_parents) != 1:
		return {
			"ok": False,
			"error": _(
				"تعذر تحديد الفئة الأب تلقائيًا (مفيش فئات حالية نقيس "
				"عليها، أو فيه أكتر من أب مختلف). راجع شجرة Customer "
				"Group يدويًا من /app قبل الإضافة."
			),
		}

	parent_customer_group = distinct_parents.pop()

	doc = frappe.get_doc(
		{
			"doctype": "Customer Group",
			"customer_group_name": customer_group_name,
			"parent_customer_group": parent_customer_group,
			"is_group": 0,
			"disabled": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"ok": True,
		"customer_group": doc.name,
	}


@frappe.whitelist(methods=["POST"])
def staff_set_item_discount(
	item_code: str,
	customer_group: str,
	discount_percent: str | float,
):
	"""يحدد/يحدّث/يعطّل خصم صنف واحد لفئة عميل واحدة، عبر Pricing Rule على
	مستوى الكود (apply_on = 'Item Code')، بنسبة خصم مستقلة لكل زوج
	(صنف، فئة) — مش على مستوى المجموعة (Item Group).

	تحقّق حي (bench على بيئة التطوير المحلية، سبتمبر 2026 — أُغلق به التحذير
	القديم): أسماء الحقول المستخدمة هنا موجودة فعلًا في
	frappe.get_meta("Pricing Rule") — price_or_product_discount (خياراتها
	Price/Product)، rate_or_discount (تشمل Discount Percentage)،
	discount_percentage، applicable_for، customer_group، apply_on (تشمل
	Item Code)، disable، selling — وجدول الأصناف الفرعي اسمه فعلًا items
	بنوع Pricing Rule Item Code (جدول tabPricing Rule Item Code موجود بأعمدة
	parent/item_code). الإنشاء الفعلي لقاعدة بهذه الحقول نجح حيًا وطبّقها
	المحرك على Sales Order بنفس القيم.

	كشف حرج مُثبت من كود ERPNext نفسه (cleanup_fields_value): حقل
	applicable_for ليس شكليًا — أي حفظ بapplicable_for فارغة يُصفّر
	customer_group تلقائيًا فتتسرّب القاعدة للجميع (حدث فعلًا في PRLE-0002
	وPRLE-0003 قبل الإصلاح). لذلك تُضبط applicable_for="Customer Group"
	إلزاميًا في مساري الإنشاء والتحديث، مع شفاء القواعد المسرّبة القديمة
	عند إعادة حفظها (مطابقة بصيغة العنوان الخاصة بنا فقط).

	تعطيل بدل حذف (زي باقي الشاشات في المشروع): تصفير النسبة بيعطّل
	الـPricing Rule الموجودة بدل ما يمسحها، عشان نحتفظ بتاريخ القرار.
	"""
	from biozone_web.utils import require_staff_access

	require_staff_access()

	if not frappe.db.exists("Item", item_code):
		return {
			"ok": False,
			"error": _("الصنف غير موجود"),
		}

	if not frappe.db.exists(
		"Customer Group",
		{
			"name": customer_group,
			"is_group": 0,
			"disabled": 0,
		},
	):
		return {
			"ok": False,
			"error": _("الفئة غير موجودة أو غير مفعّلة"),
		}

	discount_percent = frappe.utils.flt(discount_percent)

	if discount_percent < 0 or discount_percent > 100:
		return {
			"ok": False,
			"error": _("نسبة الخصم يجب أن تكون بين 0 و100"),
		}

	existing = frappe.db.sql(
		"""
		select pr.name
		from `tabPricing Rule Item Code` pri
		inner join `tabPricing Rule` pr on pr.name = pri.parent
		where pr.apply_on = 'Item Code'
			and pri.item_code = %(item_code)s
			and (
				pr.customer_group = %(customer_group)s
				-- قواعد قديمة مسرّبة: أُنشئت بهذه الدالة نفسها قبل إصلاح
				-- applicable_for فحُفظت بفئة فارغة (تطابق الجميع). نلتقطها
				-- بصيغة العنوان الخاصة بنا فقط (f"{item_code} - {group}")
				-- حتى لا نخطف قاعدة عامة مقصودة أُنشئت من Desk يدويًا،
				-- ومسار التحديث أدناه يشفيها (group + applicable_for).
				or (
					ifnull(pr.customer_group, '') = ''
					and pr.title = %(legacy_title)s
				)
			)
		limit 1
		""",
		{
			"customer_group": customer_group,
			"item_code": item_code,
			"legacy_title": f"{item_code} - {customer_group}",
		},
		as_dict=True,
	)
	existing_name = existing[0].name if existing else None

	if discount_percent <= 0:
		# مفيش نسبة خصم = تعطيل القاعدة الموجودة لو فيه، من غير حذفها.
		if existing_name:
			frappe.db.set_value("Pricing Rule", existing_name, "disable", 1)
			frappe.db.commit()

		return {
			"ok": True,
			"pricing_rule": existing_name,
			"discount_percent": 0,
		}

	if existing_name:
		doc = frappe.get_doc("Pricing Rule", existing_name)
		doc.discount_percentage = discount_percent
		# شفاء إلزامي: applicable_for فارغة تجعل validate()‎ (cleanup_fields_value)
		# تُصفّر customer_group عند كل حفظ — وهو سبب تسرّب خصومات الفئات للجميع
		# (مُثبت حيًا على PRLE-0002/PRLE-0003). تُضبط هنا قبل الحفظ مع الفئة نفسها.
		doc.applicable_for = "Customer Group"
		doc.customer_group = customer_group
		doc.disable = 0
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Pricing Rule",
				"title": f"{item_code} - {customer_group}",
				"apply_on": "Item Code",
				"price_or_product_discount": "Price",
				"selling": 1,
				# إلزامي — بدونه يُصفَّر customer_group تلقائيًا عند الحفظ
				# (cleanup_fields_value في ERPNext)، فتصبح القاعدة عامة للجميع
				# بدل فئتها (سبب التسرّب المُثبت حيًا). راجع التعليق فوق.
				"applicable_for": "Customer Group",
				"customer_group": customer_group,
				"rate_or_discount": "Discount Percentage",
				"discount_percentage": discount_percent,
				"items": [{"item_code": item_code}],
			}
		)
		doc.insert(ignore_permissions=True)

	frappe.db.commit()

	return {
		"ok": True,
		"pricing_rule": doc.name,
		"discount_percent": discount_percent,
	}


# ---------------------------------------------------------------------------
# المرحلة B9 — تجهيز الطلبات (فحص الباركود + الفاتورة + التسليم)
#
# نموذج الحالات (3 فقط): جارٍ التجهيز / جاهز للتسليم / تم التسليم + فلاج
# needs_attention داخلي. التأكيد ثنائي على مستوى الصنف فقط (مسحة واحدة =
# الصنف كاملًا، بلا تتبع كمية). التزامن: الاعتماد على modified المدمج، مع
# حماية إلزامية من تكرار DN/SI عند إعادة إرسال تأكيد التسليم.
#
# سياسة التشغيل الموصى بها عند إنشاء حساب موظف (C3 — توصية توثيقية وتشغيلية، وليست بوابة كودية):
# يُوصى عند إنشاء حساب موظف جديد بإسناد الأدوار الثلاثة معًا كوحدة واحدة:
# Sales User + Stock User + Accounts User — نفس نمط B6، ويغطي كل عمليات
# B9 بهامش أمان. الإنفاذ الفعلي يتم عبر صلاحيات ERPNext الأساسية لكل
# DocType على حدة (Item/Sales Order/Delivery Note/Sales Invoice) أثناء
# الحفظ والاعتماد، وليس عبر أي تحقق مركزي في B9 (لا يوجد فحص
# required_roles/missing_roles في B9 عمدًا حتى لا تُمنع سيناريوهات مشروعة
# مثل موظف مخزن بلا صلاحية Accounts) — والحساب بلا أي دور تشغيلي يُرفض من المحرك نفسه (403).
# ---------------------------------------------------------------------------


def _b9_get_draft_order(order_name: str):
	"""يجلب طلب بيع Draft للموظف، أو يُرجع (None, error)."""
	from biozone_web.utils import require_staff_access

	require_staff_access()

	order_name = (order_name or "").strip()
	if not order_name or not frappe.db.exists("Sales Order", order_name):
		return None, _("الطلب غير موجود")

	so = frappe.get_doc("Sales Order", order_name)
	if so.docstatus != 0:
		return None, _("هذا الطلب لم يعد قيد التجهيز")
	return so, None


def _b9_prep_payload(so) -> dict:
	"""حمولة صفحة التجهيز: البنود + الباركودات + التقدم + الحالة."""
	from biozone_web.b9_utils import get_item_barcodes, get_order_prep_state

	state = get_order_prep_state(so)
	items = []
	for it in so.items or []:
		items.append(
			{
				"name": it.name,
				"item_code": it.item_code,
				"item_name": it.item_name,
				"barcodes": get_item_barcodes(it.item_code),
				"qty": float(it.qty or 0),
				"uom": it.uom,
				"rate": float(it.rate or 0),
				"discount_percentage": float(it.discount_percentage or 0),
				"discount_amount": float(it.discount_amount or 0),
				"amount": float(it.amount or 0),
				"confirmed": bool(frappe.utils.cint(it.get("custom_confirmed"))),
				"confirmation_method": it.get("custom_confirmation_method") or "",
				"confirmed_by": it.get("custom_confirmed_by") or "",
				"confirm_time": str(it.get("custom_confirm_time") or ""),
			}
		)
	return {
		"ok": True,
		"name": so.name,
		"customer": so.customer,
		"customer_name": so.customer_name,
		"company": so.company,
		"transaction_date": str(so.transaction_date or ""),
		"grand_total": float(so.grand_total or 0),
		"net_total": float(so.net_total or 0),
		"state": state["state"],
		"total": state["total"],
		"confirmed": state["confirmed"],
		"percent": state["percent"],
		"progress_text": state["progress_text"],
		"needs_attention": state["needs_attention"],
		"attention_note": state["attention_note"],
		"items": items,
	}


def _b9_log(so, text: str):
	"""تسجيل تلقائي (اسم المستخدم + وقت) لتعديلات التجهيز — بلا سبب مكتوب."""
	try:
		so.add_comment("Info", text)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "B9 prep audit comment failed")


@frappe.whitelist(methods=["GET", "POST"])
def staff_get_order_prep(order_name: str):
	"""حالة التجهيز الكاملة لطلب (لصفحة الفحص بالباركود)."""
	so, err = _b9_get_draft_order(order_name)
	if err:
		# الطلب المسلَّم يُعرض عبر سياق التسليم، لا عبر صفحة التجهيز.
		if frappe.db.exists("Sales Order", (order_name or "").strip()):
			doc = frappe.get_doc("Sales Order", (order_name or "").strip())
			if doc.docstatus == 1:
				return {"ok": False, "error": _("هذا الطلب تم تسليمه بالفعل"), "delivered": True}
		return {"ok": False, "error": err}
	return _b9_prep_payload(so)


def _pilot_barcode_fail(error_code: str, message: str, extra: dict | None = None):
	"""فشل منظم لمسار تأكيد الباركود (Pilot-2 عقد الأخطاء).

	نفس عقد Pilot-1: ‏422 + {ok, error, error_code, request_id} بلا حقول
	خام. request_id فريد لكل استدعاء. extra مفاتيح توافقية إضافية
	(مثل out_of_order/item_code) تُحفَظ كما هي.
	"""
	frappe.local.response["http_status_code"] = 422
	out = {
		"ok": False,
		"error": message,
		"error_code": error_code,
		"request_id": uuid.uuid4().hex,
	}
	if extra:
		out.update(extra)
	return out


@frappe.whitelist(methods=["POST"])
def staff_confirm_item_barcode(order_name: str, barcode: str):
	"""تأكيد صنف بمسحة باركود واحدة = الصنف بأكمله مؤكَّد (بلا كمية).

	تبسيط B9: تأكيد واحد فقط بلا تمييز ماسح/يدوي — الهوية من الجلسة
	تلقائيًا. لا تُكتب custom_confirmation_method ولا custom_confirm_reason
	من الآن (تُتركان فارغتين؛ الحقول نفسها باقية في DB بلا patch إزالة).
	"""
	from biozone_web.b9_utils import find_item_code_by_barcode
	from biozone_web.utils import require_staff_access

	require_staff_access()
	so, err = _b9_get_draft_order(order_name)
	if err:
		# فصل الحالتين على الشرط الفعلي نفسه داخل _b9_get_draft_order
		# (غياب السجل مقابل خروجه من المسودة) — بلا تغيير ترتيب الفحوص
		# أو نصوصها؛ كل فرع بكوده الخاص.
		if not frappe.db.exists("Sales Order", (order_name or "").strip()):
			return _pilot_barcode_fail("BARCODE_ORDER_NOT_FOUND", err)
		return _pilot_barcode_fail("BARCODE_ORDER_NOT_DRAFT", err)

	barcode = (barcode or "").strip()
	if not barcode:
		return _pilot_barcode_fail("BARCODE_EMPTY", _("من فضلك أدخل رقم الباركود"))

	item_code = find_item_code_by_barcode(barcode)
	if not item_code:
		return _pilot_barcode_fail("BARCODE_UNKNOWN", _("الباركود غير مسجل في الدليل"))

	matched = [it for it in (so.items or []) if it.item_code == item_code]
	if not matched:
		return _pilot_barcode_fail(
			"BARCODE_NOT_IN_ORDER",
			_("هذا الصنف غير مدرج في هذا الطلب"),
			{"out_of_order": True, "item_code": item_code},
		)

	unconfirmed = [it for it in matched if not frappe.utils.cint(it.get("custom_confirmed"))]
	if not unconfirmed:
		return {"ok": True, "already": True, "item_code": item_code, **_b9_prep_payload(so)}

	now = frappe.utils.now()
	staff = frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user
	for it in unconfirmed:
		it.custom_confirmed = 1
		it.custom_confirmed_by = staff
		it.custom_confirm_time = now
	so.save(ignore_permissions=True)
	_b9_log(so, _("أكّد {0} الصنف {1}").format(staff, item_code))
	frappe.db.commit()
	payload = _b9_prep_payload(frappe.get_doc("Sales Order", so.name))
	payload["confirmed_code"] = item_code
	return payload


def _b9_confirm_row_direct(so, row):
	"""تأكيد مباشر لبند واحد — هوية الجلسة تلقائيًا، بلا اسم/سبب يدوي."""
	staff = frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user
	row.custom_confirmed = 1
	row.custom_confirmed_by = staff
	row.custom_confirm_time = frappe.utils.now()
	so.save(ignore_permissions=True)
	_b9_log(so, _("أكّد {0} الصنف {1}").format(staff, row.item_code))
	frappe.db.commit()
	return _b9_prep_payload(frappe.get_doc("Sales Order", so.name))


@frappe.whitelist(methods=["POST"])
def staff_confirm_item_manual(order_name: str, so_detail: str, staff_name: str | None = None, reason: str | None = None, notes: str = ""):
	"""تأكيد مباشر بضغطة واحدة (بلا باركود) — بلا اسم/سبب يدوي.

	تبسيط B9: بارامترات staff_name/reason/notes القديمة مقبولة للتوافق
	الخلفي فقط وتُتجاهل بالكامل — الهوية من الجلسة كما في تعديل الكمية
	والحذف. لا تُكتب custom_confirmation_method ولا custom_confirm_reason.
	"""
	so, err = _b9_get_draft_order(order_name)
	if err:
		return {"ok": False, "error": err}

	row = next((it for it in (so.items or []) if it.name == so_detail), None)
	if row is None:
		return {"ok": False, "error": _("البند غير موجود في هذا الطلب")}

	if frappe.utils.cint(row.get("custom_confirmed")):
		return {"ok": True, "already": True, **_b9_prep_payload(so)}

	return _b9_confirm_row_direct(so, row)


@frappe.whitelist(methods=["POST"])
def staff_update_item_qty(order_name: str, so_detail: str, new_qty: str | float):
	"""تعديل كمية بند أثناء التجهيز — لأي موظف، والطلب Draft فقط.

	بلا سبب مكتوب وبلا إشعار للعميل. تعديل الكمية على صنف مؤكَّد لا يُلغي
	تأكيده (§4). يُسجَّل الفاعل والوقت تلقائيًا في سجل الطلب.
	"""
	so, err = _b9_get_draft_order(order_name)
	if err:
		return {"ok": False, "error": err}

	new_qty = frappe.utils.flt(new_qty)
	if new_qty <= 0:
		return {"ok": False, "error": _("الكمية يجب أن تكون أكبر من صفر")}

	row = next((it for it in (so.items or []) if it.name == so_detail), None)
	if row is None:
		return {"ok": False, "error": _("البند غير موجود في هذا الطلب")}

	old_qty = float(row.qty or 0)
	row.qty = new_qty
	so.save(ignore_permissions=True)
	staff = frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user
	_b9_log(so, _("عدّل {0} كمية {1} من {2} إلى {3}").format(staff, row.item_code, old_qty, new_qty))
	frappe.db.commit()
	return _b9_prep_payload(frappe.get_doc("Sales Order", so.name))


@frappe.whitelist(methods=["POST"])
def staff_delete_item(order_name: str, so_detail: str):
	"""حذف بند أثناء التجهيز — يحدّث عداد التقدم فورًا (§4)."""
	so, err = _b9_get_draft_order(order_name)
	if err:
		return {"ok": False, "error": err}

	rows = list(so.items or [])
	idx = next((i for i, it in enumerate(rows) if it.name == so_detail), None)
	if idx is None:
		return {"ok": False, "error": _("البند غير موجود في هذا الطلب")}

	removed = rows[idx]
	so.items.remove(removed)
	if not so.items:
		return {"ok": False, "error": _("لا يمكن حذف آخر بند في الطلب")}
	so.save(ignore_permissions=True)
	staff = frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user
	_b9_log(so, _("حذف {0} الصنف {1} من الطلب").format(staff, removed.item_code))
	frappe.db.commit()
	return _b9_prep_payload(frappe.get_doc("Sales Order", so.name))


@frappe.whitelist(methods=["POST"])
def staff_set_order_attention(order_name: str, needs_attention: int = 0, note: str = ""):
	"""ضبط فلاج الانتباه الداخلي (§2) — يبقى الطلب جارٍ التجهيز."""
	from biozone_web.utils import require_staff_access

	require_staff_access()
	order_name = (order_name or "").strip()
	if not frappe.db.exists("Sales Order", order_name):
		return {"ok": False, "error": _("الطلب غير موجود")}
	so = frappe.get_doc("Sales Order", order_name)
	if so.docstatus != 0:
		return {"ok": False, "error": _("هذا الطلب لم يعد قيد التجهيز")}
	so.db_set("custom_needs_attention", frappe.utils.cint(needs_attention), update_modified=True)
	so.db_set("custom_attention_note", (note or "").strip(), update_modified=False)
	staff = frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user
	_b9_log(so, _("حدّث {0} حالة الانتباه للطلب").format(staff))
	frappe.db.commit()
	return {"ok": True, "needs_attention": bool(frappe.utils.cint(needs_attention))}


def _b9_existing_delivery_docs(order_name: str) -> dict:
	"""أي DN/SI غير ملغاة مرتبطة بالطلب (لمنع التكرار §5)."""
	dn = frappe.db.get_value(
		"Delivery Note Item",
		{"against_sales_order": order_name, "docstatus": ["!=", 2]},
		"parent",
	)
	si = None
	if dn:
		si = frappe.db.get_value(
			"Sales Invoice Item",
			{"delivery_note": dn, "docstatus": ["!=", 2]},
			"parent",
		)
	if not si:
		si = frappe.db.get_value(
			"Sales Invoice Item",
			{"sales_order": order_name, "docstatus": ["!=", 2]},
			"parent",
		)
	return {"delivery_note": dn, "sales_invoice": si}


@frappe.whitelist(methods=["POST"])
def staff_confirm_delivery(order_name: str):
	"""التأكيد النهائي: تسليم + فاتورة معتمَدة سويًا في نفس اللحظة (§6).

	الشروط: كل الأصناف مؤكَّدة (ثنائي على مستوى الصنف). المنع من التكرار:
	تحقق قبلي من عدم وجود DN/SI غير ملغاة لنفس الطلب + إرجاع الموجود
	(idempotent) عند إعادة الإرسال، بدل إنشاء مكرر.

	C4: التسليم الجزئي (DN دون SI أو العكس) لا يُعلَن نجاحًا أبدًا — خطأ
	صريح يمنع اعتبار الطلب "تم التسليم"، بدل ok:true مع sales_invoice=null.
	"""
	from biozone_web.b9_utils import get_order_prep_state
	from biozone_web.utils import get_default_warehouse, require_staff_access

	require_staff_access()
	order_name = (order_name or "").strip()
	if not order_name or not frappe.db.exists("Sales Order", order_name):
		return {"ok": False, "error": _("الطلب غير موجود")}

	# منع التكرار (1): مستندات موجودة بالفعل — تُرجع كما هي بلا إنشاء جديد.
	# C4: الاكتمال فقط يُعاد كنجاح. الجزئي (DN دون SI أو العكس) خطأ صريح،
	# لا ok:true مع sales_invoice=null.
	existing = _b9_existing_delivery_docs(order_name)
	so_check = frappe.get_doc("Sales Order", order_name)
	if existing["delivery_note"] and existing["sales_invoice"]:
		return {
			"ok": True,
			"already": True,
			"sales_order": order_name,
			"delivery_note": existing["delivery_note"],
			"sales_invoice": existing["sales_invoice"],
		}
	if existing["delivery_note"] or existing["sales_invoice"]:
		return {
			"ok": False,
			"error": _("يوجد تسليم دون فاتورة مكتملة، ويلزم معالجة الطلب قبل إعادة المحاولة"),
			"delivery_note": existing["delivery_note"],
			"sales_invoice": existing["sales_invoice"],
			"incomplete": True,
		}

	so = so_check
	if so.docstatus != 0:
		# سُلّم من نافذة أخرى بين الفحص والتنفيذ — أعد فحص المستندات.
		# C4: لا نجاح صامت هنا أيضًا — المكتمل فقط يُعاد كنجاح.
		existing = _b9_existing_delivery_docs(order_name)
		if existing["delivery_note"] and existing["sales_invoice"]:
			return {
				"ok": True,
				"already": True,
				"sales_order": order_name,
				"delivery_note": existing["delivery_note"],
				"sales_invoice": existing["sales_invoice"],
			}
		if existing["delivery_note"] or existing["sales_invoice"]:
			return {
				"ok": False,
				"error": _("يوجد تسليم دون فاتورة مكتملة، ويلزم معالجة الطلب قبل إعادة المحاولة"),
				"delivery_note": existing["delivery_note"],
				"sales_invoice": existing["sales_invoice"],
				"incomplete": True,
			}
		return {
			"ok": False,
			"error": _("هذا الطلب معتمَد مسبقًا ولا يملك مستندات تسليم مكتملة، ويلزم معالجته يدويًا قبل إعادة المحاولة"),
			"delivery_note": existing["delivery_note"],
			"sales_invoice": existing["sales_invoice"],
			"incomplete": True,
		}

	state = get_order_prep_state(so)
	if state["total"] == 0:
		return {"ok": False, "error": _("الطلب بلا أصناف")}
	if state["confirmed"] != state["total"]:
		remaining = state["total"] - state["confirmed"]
		return {
			"ok": False,
			"error": _("لا يمكن التسليم قبل تأكيد كل الأصناف — المتبقي: {0}").format(remaining),
			"remaining": remaining,
		}

	try:
		so.submit()
	except Exception:
		frappe.db.rollback()
		# سباق محتمل: اعتُمد من جلسة أخرى — أعد فحص المستندات بدل الفشل.
		# C4: المكتمل فقط يُعاد كنجاح؛ الجزئي أو الغياب خطأ صريح.
		existing = _b9_existing_delivery_docs(order_name)
		if frappe.db.get_value("Sales Order", order_name, "docstatus") == 1 and (
			existing["delivery_note"] and existing["sales_invoice"]
		):
			return {
				"ok": True,
				"already": True,
				"sales_order": order_name,
				"delivery_note": existing["delivery_note"],
				"sales_invoice": existing["sales_invoice"],
			}
		if frappe.db.get_value("Sales Order", order_name, "docstatus") == 1 and (
			existing["delivery_note"] or existing["sales_invoice"]
		):
			return {
				"ok": False,
				"error": _("يوجد تسليم دون فاتورة مكتملة، ويلزم معالجة الطلب قبل إعادة المحاولة"),
				"delivery_note": existing["delivery_note"],
				"sales_invoice": existing["sales_invoice"],
				"incomplete": True,
			}
		return {"ok": False, "error": _("تعذر اعتماد الطلب")}

	warehouse = get_default_warehouse()
	try:
		so.reload()
		dn = _create_delivery_note_for_order(so, warehouse)
		si = _create_sales_invoice_for_delivery(so, dn)
	except Exception as exc:
		frappe.db.rollback()
		return {"ok": False, "error": _("تعذر إنشاء التسليم والفاتورة: {0}").format(exc)}

	frappe.db.commit()
	staff = frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user
	try:
		so.add_comment("Info", _("سلّم {0} الطلب وأنشأ الفاتورة {1}").format(staff, si.name))
		frappe.db.commit()
	except Exception:
		pass

	return {
		"ok": True,
		"sales_order": so.name,
		"delivery_note": dn.name,
		"sales_invoice": si.name,
		"redirect": f"/staff/order-delivery?order={so.name}&invoice={si.name}",
	}


def _create_sales_invoice_for_delivery(so, dn):
	"""فاتورة مبيعات معتمَدة (submitted) مرتبطة بالطلب والتسليم معًا.

	تثبيت الرصيد السابق (C2): يُلتقط رصيد العميل الآجل من دفتر الأستاذ
	الفعلي هنا — قبل إنشاء مستند الفاتورة نفسه — ويُحفظ في
	`custom_previous_balance`، فلا ينجرف الرقم المطبوع مع الفواتير اللاحقة.
	"""
	from erpnext.selling.doctype.customer.customer import get_customer_outstanding

	previous_balance = frappe.utils.flt(
		get_customer_outstanding(so.customer, so.company, ignore_outstanding_sales_order=True)
	)

	dn_map = {}
	for dit in dn.items or []:
		if dit.get("so_detail"):
			dn_map[dit.so_detail] = {"delivery_note": dn.name, "dn_detail": dit.name}

	si_items = []
	for it in so.items or []:
		link = dn_map.get(it.name, {})
		si_items.append(
			{
				"item_code": it.item_code,
				"item_name": it.item_name,
				"description": it.description or it.item_name,
				"qty": it.qty,
				"uom": it.uom,
				"price_list_rate": it.price_list_rate,
				"discount_percentage": it.discount_percentage,
				"discount_amount": it.discount_amount,
				"rate": it.rate,
				"amount": it.amount,
				"warehouse": it.warehouse,
				"sales_order": so.name,
				"so_detail": it.name,
				"delivery_note": link.get("delivery_note"),
				"dn_detail": link.get("dn_detail"),
				"cost_center": it.get("cost_center"),
			}
		)

	due_date = frappe.utils.add_days(frappe.utils.today(), 30)
	try:
		sched = (so.get("payment_schedule") or [])
		if sched and sched[0].get("due_date"):
			sched_due = frappe.utils.getdate(sched[0].due_date)
			# طلب قديم بجدول سداد مضى استحقاقه قبل اليوم: نسخ التاريخ كما
			# هو يجعل الفاتورة مرفوضة (Due Date before Posting Date) بعد
			# اعتماد الطلب — أي تسليم بلا فاتورة. يُثبَّت على اليوم بدلًا
			# من ذلك (مستحق فورًا) بدل الفشل بعد الاعتماد.
			if sched_due and sched_due >= frappe.utils.getdate(frappe.utils.today()):
				due_date = sched[0].due_date
			elif sched_due:
				due_date = frappe.utils.today()
	except Exception:
		pass

	si = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"customer": so.customer,
			"company": so.company,
			"posting_date": frappe.utils.today(),
			"posting_time": frappe.utils.nowtime(),
			"due_date": due_date,
			"selling_price_list": so.selling_price_list,
			"currency": so.currency,
			"conversion_rate": so.conversion_rate,
			"update_stock": 0,
			"set_warehouse": so.get("set_warehouse"),
			"custom_previous_balance": previous_balance,
			"taxes_and_charges": so.get("taxes_and_charges"),
			"items": si_items,
			"taxes": [
				{
					"charge_type": t.charge_type,
					"account_head": t.account_head,
					"description": t.description,
					"rate": t.rate,
					"included_in_print_rate": t.get("included_in_print_rate"),
				}
				for t in (so.get("taxes") or [])
				if t.get("account_head")
			],
		}
	)
	si.insert(ignore_permissions=True)
	si.submit()
	return si


@frappe.whitelist(methods=["GET", "POST"])
def staff_get_delivery_context(order_name: str, sales_invoice: str | None = None):
	"""سياق صفحة التسليم والفاتورة: الطلب + التسليم + الفاتورة + الأرصدة.

	الحقول الإلزامية (§7): الحساب السابق المثبَّت لحظة إنشاء الفاتورة
	(`custom_previous_balance` — يُقرأ من الحقل المحفوظ مباشرة ولا يُعاد
	حسابه وقت العرض حتى لا ينجرف مع الفواتير اللاحقة)، والحالي =
	السابق + مستحق هذه الفاتورة، وخصم كل صنف كعمود منفصل، والإجمالي
	بالحروف، واسم الموظف المؤكِّد، ووقت الطباعة الفعلي. الفاتورة
	submitted دائمًا.
	"""
	from biozone_web.b9_utils import amount_in_arabic_words
	from biozone_web.utils import invoice_linked_to_order, require_staff_access

	require_staff_access()
	order_name = (order_name or "").strip()
	if not order_name or not frappe.db.exists("Sales Order", order_name):
		return {"ok": False, "error": _("الطلب غير موجود")}

	so = frappe.get_doc("Sales Order", order_name)
	if so.docstatus == 2:
		return {
			"ok": False,
			"error": _("هذا الطلب ملغى ولا يمكن إنشاء مستند تسليم له."),
			"cancelled": True,
		}
	existing = _b9_existing_delivery_docs(order_name)
	si_name = (sales_invoice or "").strip() or existing["sales_invoice"]
	if not si_name or not frappe.db.exists("Sales Invoice", si_name):
		return {"ok": False, "error": _("لا توجد فاتورة معتمَدة لهذا الطلب بعد")}

	si = frappe.get_doc("Sales Invoice", si_name)
	if si.docstatus != 1:
		return {"ok": False, "error": _("الفاتورة ليست معتمَدة")}
	if not invoice_linked_to_order(si.name, order_name):
		if (sales_invoice or "").strip():
			return {"ok": False, "error": _("الفاتورة ليست فاتورة معتمدة لهذا الطلب")}
		return {"ok": False, "error": _("لا توجد فاتورة معتمَدة لهذا الطلب بعد")}

	# السابق من الحقل المثبَّت لحظة الإنشاء (C2) — بلا أي حساب جديد هنا.
	# الحالي = السابق + مستحق هذه الفاتورة (حساب ثابت لا ينجرف).
	previous_balance = frappe.utils.flt(si.get("custom_previous_balance"))
	invoice_outstanding = frappe.utils.flt(si.outstanding_amount) or frappe.utils.flt(si.grand_total)
	current_balance = previous_balance + invoice_outstanding
	confirmer = frappe.db.get_value("User", si.owner, "full_name") or si.owner

	items = [
		{
			"idx": i + 1,
			"item_name": it.item_name,
			"item_code": it.item_code,
			"qty": float(it.qty or 0),
			"uom": it.uom,
			"rate": float(it.rate or 0),
			"discount_percentage": float(it.discount_percentage or 0),
			"discount_amount": float(it.discount_amount or 0),
			"amount": float(it.amount or 0),
		}
		for i, it in enumerate(si.items or [])
	]

	return {
		"ok": True,
		"order_name": so.name,
		"delivery_note": existing["delivery_note"],
		"invoice_name": si.name,
		"customer_name": si.customer_name,
		"customer": si.customer,
		"posting_date": str(si.posting_date or ""),
		"items": items,
		"items_count": len(items),
		"net_total": float(si.net_total or 0),
		"grand_total": float(si.grand_total or 0),
		"rounded_total": float(si.rounded_total or si.grand_total or 0),
		"previous_balance": previous_balance,
		"current_balance": current_balance,
		"amount_words": amount_in_arabic_words(float(si.rounded_total or si.grand_total or 0)),
		"confirmed_by": confirmer,
		"print_time": frappe.utils.now_datetime().strftime("%H:%M - %d/%m/%Y"),
		"company": si.company,
	}
