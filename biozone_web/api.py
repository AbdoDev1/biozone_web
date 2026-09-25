import uuid

import frappe
from frappe import _

from biozone_web.services.rate_limit import rate_limited
from biozone_web.services.login_lock import (
	acquire_login_lock,
	login_busy_response,
	release_login_lock,
)


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


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limited("biozone_login")
def biozone_login(usr=None, pwd=None, remember_me: int = 0):
	# بوابة نوع المتجر (قرار فصل النطاقات): حسابات العملاء (Website User)
	# فقط. أي System User (موظف/أدمن) يُرفض هنا — بعد نجاح المصادقة لكن
	# قبل post_login — فلا تُنشأ أي جلسة ولا يُصدَر أي session cookie.
	# الرفض برسالة المتجر الواضحة نفسها، بلا redirect لأي نطاق آخر.
	if not isinstance(usr, str) or not usr.strip() or not isinstance(pwd, str) or not pwd:
		return _fail_store_login()
	# B4: serialize concurrent logins for the same account (deadlock on
	# tabUser inside the framework session start). Waiter timeouts return
	# the busy contract below -- authenticate itself is untouched.
	lock_state, lock_token = acquire_login_lock(usr)
	if lock_state == "busy":
		return login_busy_response()
	try:
		login_manager = frappe.local.login_manager
		login_manager.authenticate(user=usr, pwd=pwd)
	finally:
		release_login_lock(usr, lock_token)
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
	# B4: same per-account mutex as biozone_login (shared namespace -- the
	# same typed identifier maps to one key on both paths).
	lock_state, lock_token = acquire_login_lock(usr)
	if lock_state == "busy":
		return login_busy_response()
	login_manager = frappe.local.login_manager
	try:
		login_manager.authenticate(user=usr, pwd=pwd)
	except (frappe.AuthenticationError, frappe.SecurityException):
		return _fail_staff_login()
	finally:
		release_login_lock(usr, lock_token)
	login_manager.post_login()

	user_type = frappe.db.get_value("User", login_manager.user, "user_type")
	if user_type != "System User":
		login_manager.logout()
		return _fail_staff_login()

	return {
		"message": "Logged In",
		"home_page": "/staff/dashboard",
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limited("biozone_sign_up")
def biozone_sign_up(email: str, full_name: str, phone: str, pwd: str):
	email = email.strip().lower()

	if not email or not full_name or not pwd:
		frappe.throw(_("من فضلك أكمل كل الحقول المطلوبة"))

	if frappe.db.exists("User", email):
		frappe.throw(_("هذا البريد الإلكتروني مسجل بالفعل"))

	# Slice 1a: the atomic unit (User + role + Customer) and its deadlock
	# retry live in biozone_web.services.signup — no new logic here.
	from biozone_web.services.signup import create_signup_with_retry

	failure = create_signup_with_retry(email, full_name, phone, pwd)
	if failure:
		return failure

	frappe.db.commit()

	login_manager = frappe.local.login_manager
	login_manager.authenticate(user=email, pwd=pwd)
	login_manager.post_login()

	return {
		"message": "Account Created",
		"home_page": "/biozone-home",
	}



@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limited("biozone_forgot_password")
def biozone_forgot_password(email: str):
	# NOTE (A2): frappe's reset_password carries @rate_limit with no key=
	# (user.py:1148), i.e. keyed by request_ip -- a collective key behind
	# our Tunnel (T10), exhausted globally after password_reset_limit
	# (3/hr default). Our @rate_limited above replaces it with per-IP +
	# per-email counters and a 429 contract, so the Desk limiter must NOT
	# run on this path. The orchestration below mirrors reset_password's
	# semantics exactly (silent skip for missing/disabled/Administrator --
	# CWE-204 -- plus identical generic message) while calling the User
	# doc's own validate/_reset_password methods, so framework logic is
	# reused, not forked. A regression test asserts no Desk-style rl: key
	# is ever created here (fails loudly if upstream changes).
	email = (email or "").strip().lower()

	if not email:
		frappe.throw(_("من فضلك اكتب البريد الإلكتروني"))

	try:
		user_doc = frappe.get_doc("User", email)
		if user_doc.name != "Administrator" and user_doc.enabled:
			user_doc.validate_reset_password()
			user_doc._reset_password(send_email=True)
	except frappe.DoesNotExistError:
		frappe.clear_messages()
	except frappe.OutgoingEmailError:
		frappe.clear_messages()
		frappe.log_error(title="Password reset email could not be sent", message=frappe.get_traceback())
	except Exception:
		frappe.clear_messages()
		frappe.log_error(title="Password reset failed unexpectedly", message=frappe.get_traceback())
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

	prices = get_effective_item_prices(
		codes,
		customer=ctx["customer"],
		customer_group=ctx["customer_group"],
	)

	# Phase-2 display units: same small-unit engine price, converted for
	# the customer's group unit. Non-displayable items keep price=None so
	# the cart drops them like priceless items (setup error, never guess).
	try:
		from biozone_web.units import money2, resolve_items_display

		display = resolve_items_display(codes, ctx["customer_group"])
	except Exception:
		display = {}
	for code, entry in prices.items():
		d = (display.get(code) or {}) if isinstance(display, dict) else {}
		if not d.get("ok") or not d.get("displayable", True):
			entry["price"] = None
			entry["display_uom"] = None
			continue
		factor = d.get("factor") or 1.0
		entry["display_uom"] = d.get("uom")
		entry["display_factor"] = factor
		if entry.get("price") is not None:
			entry["price"] = money2(frappe.utils.flt(entry["price"]) * factor)

	return {
		"ok": True,
		"prices": prices,
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
		uom = (it.get("uom") or "").strip()

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
				"uom": uom,
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

		# Phase-2 unit gate (definitive; the store sends freely, staff
		# confirmation decides): every line must match the group's display
		# unit resolved server-side, meet its minimum, and carry the item's
		# own conversion factor (client values never trusted). The snapshot
		# (uom + factor) freezes into the order lines.
		from biozone_web.units import resolve_items_display

		display = resolve_items_display(
			[it["item_code"] for it in so_items], customer_group
		)
		final_items = []
		for it in so_items:
			d = display.get(it["item_code"]) or {}
			if not d.get("ok") or not d.get("displayable", True):
				frappe.throw(
					_("الصنف {0} غير متاح بوحدة عرض صالحة حاليًا — حدّث السلة").format(
						it["item_code"]
					)
				)
			want_uom = d.get("uom")
			if (it.get("uom") or "").strip() and (it.get("uom") or "").strip() != want_uom:
				frappe.throw(
					_("الوحدة المرسلة للصنف {0} لا تطابق وحدة العرض لفئتك — حدّث السلة").format(
						it["item_code"]
					)
				)
			final_items.append(
				{
					"item_code": it["item_code"],
					"qty": it["qty"],
					"uom": want_uom,
					"conversion_factor": d.get("factor") or 1.0,
				}
			)
		so_items = final_items

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

		# Phase 1 (ND16/X1): staff bell — last write inside the
		# transaction, outside the retry loop and the Administrator
		# window. Lazy import inside try/except so api.py survives a
		# half-deployed tree; safe no-op when the flag is off.
		try:
			from biozone_web.services.notifications import notify
			notify("order_new", reference_doctype="Sales Order",
			       reference_name=so.name, context={"order": so.name},
			       actor=ordering_user)
		except Exception:
			frappe.log_error(title="Biozone notification call failed: order_new",
			                 message=frappe.get_traceback())

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


def _strict_num_local(raw):
	"""Strict numeric parse (mirrors import_export._strict_num — no flt
	coercion of garbage). Returns (ok_bool, float_value)."""
	text = (str(raw) if raw is not None else "").strip().replace(",", "")
	if not text:
		return False, 0.0
	try:
		return True, float(text)
	except (ValueError, TypeError):
		return False, 0.0


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
	price_unit: str | None = None,
	large_uom: str | None = None,
	factor: str | float | None = None,
	display_override: str | None = None,
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

	# حارس الوحدة المبكر (المرحلة 0): رفض عربي واضح قبل أي كتابة،
	# بدل انفجار LinkValidationError الخام عند الحفظ. الفارغ مسموح
	# (الافتراضي Nos لاحقًا) — المرفوض فقط قيمة غير معرّفة/معطّلة.
	submitted_unit = (stock_uom or "").strip()
	if submitted_unit and not frappe.db.exists(
		"UOM", {"name": submitted_unit, "enabled": 1}
	):
		return {
			"ok": False,
			"error": _("الوحدة غير معرّفة أو معطّلة: {0}").format(submitted_unit),
		}

	# Phase-2 conversion fields (same rules as the item import).
	large = (large_uom or "").strip()
	factor_raw = (factor if factor not in (None, "") else "")
	factor_raw = str(factor_raw).strip()
	if large:
		if not frappe.db.exists("UOM", {"name": large, "enabled": 1}):
			return {
				"ok": False,
				"error": _("الوحدة الكبرى غير معرّفة أو معطّلة: {0}").format(large),
			}
		small_unit = submitted_unit or "Nos"
		if large == small_unit:
			return {
				"ok": False,
				"error": _("الوحدة الكبرى تطابق الصغرى — اترك الكبرى فارغة للصنف الوحيد"),
			}
		ok_factor, factor_value = _strict_num_local(factor_raw)
		if not ok_factor or factor_value <= 0:
			return {
				"ok": False,
				"error": _("معامل التحويل يجب أن يكون رقمًا أكبر من صفر"),
			}
	elif factor_raw:
		return {
			"ok": False,
			"error": _("معامل تحويل بلا وحدة كبرى"),
		}
	flag = (display_override or "").strip()
	if flag and flag not in ("inherit", "small_only", "large_only"):
		return {
			"ok": False,
			"error": _("إعداد العرض غير صالح"),
		}

	# Phase-2 price entry unit: the stored reference is always the SMALL
	# price. Entering the large price (staff screen only) computes small
	# = round_half_up(large / factor) and shows it before save (drawer).
	price_unit = (price_unit or "small").strip() or "small"
	if price_unit not in ("small", "large"):
		return {
			"ok": False,
			"error": _("وحدة إدخال السعر يجب أن تكون صغرى أو كبرى"),
		}
	if price not in (None, "") and price_unit == "large":
		if not large:
			return {
				"ok": False,
				"error": _("أدخل الوحدة الكبرى ومعاملها أولًا لحساب سعر الصغرى"),
			}
		ok_price, large_price = _strict_num_local(price)
		if not ok_price or large_price < 0:
			return {
				"ok": False,
				"error": _("سعر الجمهور يجب أن يكون رقمًا لا يقل عن صفر"),
			}
		from biozone_web.units import money2

		price = money2(large_price / factor_value)

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
		if flag:
			doc.display_override = flag
		if large:
			from biozone_web.import_export import _import_upsert_conversion

			_import_upsert_conversion(doc, large, factor_raw, "")
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
				"display_override": flag or "inherit",
				"barcodes": [{"barcode": bc} for bc in new_barcodes],
				"uoms": (
					[
						{
							"uom": large,
							"conversion_factor": factor_value,
							"min_qty": 1.0,
						}
					]
					if large
					else []
				),
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

	# نفس نمط كل نهايات الستاف في هذا الملف: البوابة require_staff_access
	# أعلى الدالة، والإدراج بتجاوز الصلاحيات — أدوار الستاف (عدا
	# Biozone Stock Staff) لا تملك Stock Entry أصلًا (مثبت حيًا)،
	# والصفحة متاحة لكل الستاف. submit يرث علم المستند.
	stock_entry.insert(ignore_permissions=True)
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
# نموذج الحالات (4 + فلتر مرتجع في القائمة): جارٍ التجهيز / جاهز للتسليم /
# تم التسليم / ملغى + فلاج needs_attention داخلي. "مرتجع" فلتر قائمة فقط
# (DN مرتجع معتمد) وليست حالة في get_order_prep_state.
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
def staff_cancel_order(order_name: str):
	"""إلغاء طلب مسودة من شاشة التجهيز (S1 — Contract v1.1 §3).

	المسودة فقط (docstatus 0) وبلا مستندات مرتبطة — فلا حركة مخزنية
	ولا مالية لعكسها. الطريق الأصيل للمسودة discard() (docstatus 2)
	مع تثبيت status = "Cancelled" للعرض (discard لا تشغّل on_cancel).
	المسلَّم يُرفض (422) ويُوجَّه للمرتجع. الملغى أصلًا = already:True.
	"""
	from biozone_web.utils import require_staff_access

	require_staff_access()
	order_name = (order_name or "").strip()
	if not order_name or not frappe.db.exists("Sales Order", order_name):
		return {"ok": False, "error": _("الطلب غير موجود")}
	so = frappe.get_doc("Sales Order", order_name)
	if so.docstatus == 2:
		return {"ok": True, "already": True}
	if so.docstatus != 0:
		return {"ok": False, "error": _("لا يمكن إلغاء هذا الطلب بعد تسليمه — استخدم المرتجع")}
	docs = _b9_existing_delivery_docs(order_name)
	if docs.get("delivery_note") or docs.get("sales_invoice"):
		return {"ok": False, "error": _("لا يمكن إلغاء هذا الطلب — له مستندات مرتبطة، استخدم المرتجع")}
	so.flags.ignore_permissions = True
	so.discard()
	so.db_set("status", "Cancelled")
	staff = frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user
	_b9_log(so, _("ألغى {0} الطلب قبل التأكيد").format(staff))
	# إشعار العميل: آخر كتابة قبل الـcommit (X1)، معزول بـsavepoint
	# داخله — فشله لا يفشل الإلغاء (A4). استيراد كسول (R-c).
	try:
		from biozone_web.services.notifications import notify

		notify(
			"order_cancelled",
			reference_doctype="Sales Order",
			reference_name=so.name,
			context={"order": so.name},
		)
	except Exception:
		frappe.log_error(title="Biozone notify order_cancelled failed")
	frappe.db.commit()
	return {"ok": True, "already": False, "order_name": so.name}


def _b9_single_delivery_pair(order_name: str):
	"""الزوج الأصلي الوحيد (DN + SI) غير المرتجع للطلب — أو (None, None, error).

	تدفقنا ينتج زوجًا واحدًا لكل طلب مسلَّم. التعدد أو الغياب = بنية
	غير متوقعة تُرفض صراحة بدل التخمين (Contract v1.1 §4).
	"""
	dn_parents = frappe.get_all(
		"Delivery Note Item",
		filters={"against_sales_order": order_name, "docstatus": ["!=", 2]},
		fields=["parent"],
	)
	dn_names = sorted(
		{
			r.parent
			for r in dn_parents
			if frappe.db.get_value("Delivery Note", r.parent, "docstatus") == 1
			and not frappe.db.get_value("Delivery Note", r.parent, "is_return")
		}
	)
	si_parents = frappe.get_all(
		"Sales Invoice Item",
		filters={"sales_order": order_name, "docstatus": ["!=", 2]},
		fields=["parent"],
	)
	si_names = sorted(
		{
			r.parent
			for r in si_parents
			if frappe.db.get_value("Sales Invoice", r.parent, "docstatus") == 1
			and not frappe.db.get_value("Sales Invoice", r.parent, "is_return")
		}
	)
	if len(dn_names) != 1 or len(si_names) != 1:
		return None, None, _("بنية مستندات غير متوقعة لهذا الطلب — راجع الدعم")
	return dn_names[0], si_names[0], None


def _b9_apply_partial(ret_doc, requested: dict):
	"""يضبط أسطر مسودة المرتجع على كميات الموظف (سالبة) ويُسقط الصفرية.

	الكميات الممررة موجبة بوحدة البيع — وهي نفس وحدة الأسطر (R1/R2)
	فلا تحويل هنا؛ `stock_qty` تُضبط بالمعامل المنسوخ من السطر الأصلي.
	التجاوز يُرفض برسالة عربية قبل الاعتماد (الحكم النهائي أصيلًا R4).
	"""
	for row in list(ret_doc.items or []):
		remaining = abs(frappe.utils.flt(row.qty))
		want = frappe.utils.flt(requested.get((row.so_detail or "").strip(), 0))
		if want <= 0:
			ret_doc.items.remove(row)
			continue
		if want - remaining > 1e-9:
			frappe.throw(
				_("الكمية المطلوبة للصنف {0} تتجاوز المتبقي القابل للمرتجع ({1})").format(
					row.item_code, remaining
				)
			)
		cf = frappe.utils.flt(row.conversion_factor) or 1
		row.qty = -1 * want
		row.stock_qty = -1 * want * cf
	if not ret_doc.items:
		frappe.throw(_("لا كمية قابلة للمرتجع في البنود المحددة"))


def _b9_check_return_stock(dn_ret):
	"""فحص مبكر عربي: المرتجع يجب ألا يُبقي رصيد المخزن سالبًا (F18).

	المحرك الأصيل يرفض أي رصيد نهائي سالب (`NegativeStockError` —
	`stock_ledger.py:1227`)، ولو كان المرتجع نفسه يحسّن الرصيد. يُفحص
	هنا قبل أي كتابة برسالة عربية تذكر الرصيد الحالي، ويبقى الفحص
	الأصيل هو الحكم النهائي عند الاعتماد (حماية السباق).
	"""
	for row in list(dn_ret.items or []):
		ret_stock = frappe.utils.flt(row.stock_qty)
		if ret_stock >= 0:
			continue
		wh = row.warehouse or dn_ret.set_warehouse or ""
		bin_qty = frappe.utils.flt(
			frappe.db.get_value(
				"Bin", {"item_code": row.item_code, "warehouse": wh}, "actual_qty"
			)
			if wh
			else 0
		)
		if bin_qty + ret_stock < -1e-9:
			frappe.throw(
				_("رصيد {0} الحالي ({1}) لا يستوعب هذا المرتجع — سيبقى سالبًا. راجع الجرد أو التسوية المخزنية أولًا.").format(
					row.item_code, bin_qty
				)
			)


@frappe.whitelist(methods=["POST"])
def staff_create_return(order_name: str, items=None):
	"""مرتجع كلي/جزئي لطلب مسلَّم (S2a — Contract v1.1 §4).

	الكلي = كل البنود بكامل المتبقي. الجزئي = بنود + كميات موجبة
	(`so_detail` + `qty` بوحدة البيع) ≤ المتبقي. يُنشأ مرتجع التسليم
	(يُرجع المخزون) ومرتجع الفاتورة (Credit Note تخفض المديونية)
	ويُعتمدان **داخل معاملة واحدة** — أي فشل = `rollback` كامل
	(A2): لا مخزون بلا مالية ولا العكس. الإشعار آخر كتابة قبل
	الـcommit ومعزول بـsavepoint داخله (A4).
	"""
	import json as _json

	from biozone_web.utils import require_staff_access

	require_staff_access()
	order_name = (order_name or "").strip()
	if not order_name or not frappe.db.exists("Sales Order", order_name):
		return {"ok": False, "error": _("الطلب غير موجود")}
	so = frappe.get_doc("Sales Order", order_name)
	if so.docstatus != 1:
		return {"ok": False, "error": _("المرتجع متاح للطلبات المسلَّمة فقط")}
	dn_name, si_name, err = _b9_single_delivery_pair(order_name)
	if err:
		return {"ok": False, "error": err}
	if isinstance(items, str):
		try:
			items = _json.loads(items)
		except Exception:
			return {"ok": False, "error": _("بيانات الأصناف غير صالحة")}
	requested = {}
	for row in items or []:
		try:
			key = ((row or {}).get("so_detail") or "").strip()
			qty = frappe.utils.flt((row or {}).get("qty"))
		except Exception:
			continue
		if key and qty > 0:
			requested[key] = requested.get(key, 0) + qty
	if not requested:
		return {"ok": False, "error": _("حدد كمية موجبة لبند واحد على الأقل")}
	try:
		from erpnext.controllers.sales_and_purchase_return import make_return_doc

		# نافذة انتحال للبناء فقط (تعيين مباشر — ممنوع frappe.set_user
		# صراحةً في هذا الملف): الـmapper يفحص create داخليًا ولا يحترم
		# أي علم (Document.has_permission يقرأ علم المستند وحده)،
		# وأدوار الستاف تملك إنشاء DN دون SI. التعيين يطال الذاكرة فقط
		# (الـmapper لا يكتب DB)، ويُستعاد في finally — ثم الإدراج
		# والاعتماد والملكية والتدقيق والإشعار باسم الموظف الفعلي.
		_staff_user = frappe.session.user
		frappe.session.user = "Administrator"
		try:
			dn_ret = make_return_doc("Delivery Note", dn_name)
			si_ret = make_return_doc("Sales Invoice", si_name)
		finally:
			frappe.session.user = _staff_user
		_b9_apply_partial(dn_ret, requested)
		_b9_apply_partial(si_ret, requested)
		_b9_check_return_stock(dn_ret)
		dn_ret.insert(ignore_permissions=True)
		dn_ret.submit()
		si_ret.insert(ignore_permissions=True)
		si_ret.submit()
	except frappe.ValidationError as e:
		frappe.db.rollback()
		frappe.log_error(title="Biozone staff_create_return rejected")
		msg = e.args[0] if e.args and isinstance(e.args[0], str) else ""
		if "already been returned" in msg or "Cannot return more" in msg:
			msg = _("تجاوزت الكمية المتبقية القابلة للمرتجع")
		elif "to complete this transaction" in msg or "NegativeStock" in type(e).__name__:
			msg = _("المخزون الحالي لا يستوعب هذا المرتجع — راجع الجرد أو التسوية المخزنية أولًا")
		return {"ok": False, "error": msg or _("تعذر إنشاء المرتجع")}
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Biozone staff_create_return failed", message=frappe.get_traceback())
		return {"ok": False, "error": _("تعذر إنشاء المرتجع")}
	staff = frappe.db.get_value("User", frappe.session.user, "full_name") or frappe.session.user
	_b9_log(
		so,
		_("أنشأ {0} مرتجعًا (تسليم {1} + إشعار دائن {2})").format(staff, dn_ret.name, si_ret.name),
	)
	try:
		from biozone_web.services.notifications import notify

		notify(
			"order_returned",
			reference_doctype="Sales Order",
			reference_name=so.name,
			context={"order": so.name, "credit": si_ret.name},
		)
	except Exception:
		frappe.log_error(title="Biozone notify order_returned failed")
	frappe.db.commit()
	# مكتمل؟ — من مستندات المرتجع نفسها (R3)، لا من حقول returned_qty
	# الأصيلة التي لا يحدّثها المحرك مع فواتير update_stock=0 (مثبت حيًا).
	fully = False
	try:
		from erpnext.controllers.sales_and_purchase_return import make_return_doc as _mrd

		_rem = _mrd("Delivery Note", dn_name)
		fully = not any(abs(frappe.utils.flt(r.qty)) > 1e-9 for r in (_rem.items or []))
	except Exception:
		frappe.log_error(title="Biozone return fully-check failed")
	return {
		"ok": True,
		"delivery_return": dn_ret.name,
		"credit_note": si_ret.name,
		"fully": bool(fully),
	}


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


# Phase-2 shipping: single Actual row on the draft order, copied to the
# invoice by the existing taxes copy. Never in store/cart — staff only.
SHIPPING_ACCOUNT = "مصاريف الشحن - BIO"


@frappe.whitelist(methods=["POST"])
def staff_set_shipping(order_name, amount):
	"""Set/replace (or remove with 0) the shipping charge on a DRAFT order.

	Strict numeric check (Arabic error), draft-only (post-confirm edits
	are rejected by the C4 rule), one row replaced never duplicated.
	Returns the saved shipping + recomputed grand total for display.
	"""
	from biozone_web.utils import require_staff_access

	require_staff_access()
	order_name = (order_name or "").strip()
	if not order_name or not frappe.db.exists("Sales Order", order_name):
		return {"ok": False, "error": _("الطلب غير موجود")}
	ok, value = _strict_num_local(amount)
	if not ok or value < 0:
		return {
			"ok": False,
			"error": _("مصاريف الشحن يجب أن تكون رقمًا لا يقل عن صفر"),
		}
	so = frappe.get_doc("Sales Order", order_name)
	if so.docstatus != 0:
		return {"ok": False, "error": _("لا يمكن تعديل الشحن بعد تأكيد الطلب")}
	if not frappe.db.exists("Account", SHIPPING_ACCOUNT):
		return {"ok": False, "error": _("حساب الشحن غير معرّف في الدليل")}
	so.set(
		"taxes",
		[
			t
			for t in (so.get("taxes") or [])
			if (t.account_head or "").strip() != SHIPPING_ACCOUNT
		],
	)
	if value > 0:
		so.append(
			"taxes",
			{
				"charge_type": "Actual",
				"account_head": SHIPPING_ACCOUNT,
				"description": _("مصاريف الشحن"),
				"tax_amount": value,
			},
		)
	so.save(ignore_permissions=True)
	frappe.db.commit()
	so.reload()
	return {
		"ok": True,
		"shipping": value if value > 0 else 0,
		"grand_total": so.grand_total,
	}


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

	# Phase 2 (ND17/X1): customer bell — last write inside the
	# transaction, on the success path only (never the already/partial/
	# error branches above). Lazy import inside try/except so delivery
	# never breaks on a half-deployed tree; safe no-op when off.
	try:
		from biozone_web.services.notifications import notify as _notify_delivered
		_notify_delivered("order_delivered", reference_doctype="Sales Order",
		                  reference_name=so.name, context={"order": so.name},
		                  actor=frappe.session.user)
	except Exception:
		frappe.log_error(title="Biozone notification call failed: order_delivered",
		                 message=frappe.get_traceback())

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
				"tax_amount": t.get("tax_amount"),
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
