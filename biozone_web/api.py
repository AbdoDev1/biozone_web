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


@frappe.whitelist(methods=["POST"])
def biozone_confirm_order(items):
	"""يحوّل محتوى السلة (localStorage عند العميل) إلى Sales Order حقيقي
	في ERPNext باسم العميل الحالي — B4.

	قرارين مهمين اتاخدوا هنا:
	1) **السعر بيتحدد من السيرفر دايمًا، مش من اللي بعته العميل.** بنبعت
	   item_code + qty بس لـERPNext، وهو اللي بيجيب الـrate الحقيقي من
	   Item Price ويطبّق أي Pricing Rule (خصم حسب نوع الحساب) وقت
	   الحفظ — عشان محدش يقدر يلاعب في السعر من متصفحه.
	2) **الطلب بيتحفظ Draft (مش submit).** التأكيد النهائي (submit)
	   هيبقى من واجهة الموظف (B6c) بعد المراجعة، مش أوتوماتيك لحظة ما
	   العميل يضغط الزرار.
	"""
	if frappe.session.user == "Guest":
		frappe.throw(_("يجب تسجيل الدخول أولًا لتأكيد الطلب"), frappe.PermissionError)

	if isinstance(items, str):
		items = frappe.parse_json(items)

	if not items:
		frappe.throw(_("السلة فارغة"))

	from biozone_web.utils import get_default_company, get_or_create_customer_for_current_user

	so_items = []
	for it in items:
		item_code = (it.get("item_code") or "").strip()
		qty = frappe.utils.flt(it.get("quantity"))

		if not item_code or qty <= 0:
			frappe.throw(_("بيانات صنف غير صحيحة في السلة"))

		item = frappe.db.get_value("Item", item_code, ["disabled"], as_dict=True)
		if not item or item.disabled:
			frappe.throw(_("أحد الأصناف في السلة لم يعد متاحًا، يرجى تحديث السلة"))

		so_items.append({"item_code": item_code, "qty": qty})

	customer = get_or_create_customer_for_current_user()

	so = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"customer": customer,
			"company": get_default_company(),
			"selling_price_list": "Standard Selling",
			"delivery_date": frappe.utils.add_days(frappe.utils.nowdate(), 3),
			"items": so_items,
		}
	)
	so.insert(ignore_permissions=True)
	frappe.db.commit()

	return {"message": {"redirect": f"/order-confirmed?name={so.name}"}}


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
):
	"""إضافة/تعديل صنف من واجهة الموظف (`/staff/items`).

	ملحوظة صلاحيات مهمة: الفحص هنا لسه بسيط (require_staff_access = أي
	System User)، مش الـRole المخصص لإدارة التسعير اللي اتفقنا عليه في
	الخطة (قسم 7، قرار 3 سبتمبر). لازم يتضاف فحص `frappe.has_permission`
	أدق قبل ما الصفحة دي تتفتح لكل الموظفين فعليًا في الإنتاج.
	"""
	from biozone_web.utils import require_staff_access

	require_staff_access()

	new_item_code = (new_item_code or "").strip()
	item_name = (item_name or "").strip()

	if not new_item_code or not item_name or not item_group:
		return {"ok": False, "error": _("من فضلك أكمل اسم الصنف والكود والمجموعة")}

	if item_code:
		# تعديل صنف موجود
		if not frappe.db.exists("Item", item_code):
			return {"ok": False, "error": _("الصنف غير موجود")}

		if new_item_code != item_code:
			# تغيير الكود = إعادة تسمية الـdoc نفسه، مش مجرد تحديث حقل
			frappe.rename_doc("Item", item_code, new_item_code, force=True)

		doc = frappe.get_doc("Item", new_item_code)
		doc.item_name = item_name
		doc.item_group = item_group
		doc.brand = brand or None
		if stock_uom:
			doc.stock_uom = stock_uom
		doc.disabled = frappe.utils.cint(disabled)
		doc.save(ignore_permissions=True)
	else:
		# صنف جديد
		if frappe.db.exists("Item", new_item_code):
			return {"ok": False, "error": _("الكود ده مستخدم بالفعل لصنف تاني")}

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
			}
		)
		doc.insert(ignore_permissions=True)

	# السعر (سعر الجمهور) — سطر واحد بس في Item Price لقائمة Standard
	# Selling، مطابقةً للقرار المصحح (سعر واحد + خصومات متعددة منفصلة)
	if price not in (None, ""):
		price_value = frappe.utils.flt(price)
		existing_price = frappe.db.get_value(
			"Item Price",
			{"item_code": doc.item_code, "price_list": "Standard Selling"},
			"name",
		)
		if existing_price:
			frappe.db.set_value("Item Price", existing_price, "price_list_rate", price_value)
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
	return {"ok": True, "item_code": doc.item_code}


@frappe.whitelist(methods=["POST"])
def staff_disable_item(item_code: str):
	"""تعطيل صنف (مش حذف) — نفس القرار المتفق عليه: مفيش زرار حذف في
	واجهة الموظف خالص."""
	from biozone_web.utils import require_staff_access

	require_staff_access()

	if not frappe.db.exists("Item", item_code):
		return {"ok": False, "error": _("الصنف غير موجود")}

	frappe.db.set_value("Item", item_code, "disabled", 1)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
def staff_log_stock_movement(item_code: str, movement_type: str, uom: str, qty: float, note: str = ""):
	"""يسجل حركة مخزون حقيقية (Stock Entry) — B6b. يفترض مستودع واحد
	ضمني (get_default_warehouse) لأن اللوحة (تسجيل حركة) ما بتسألش عن
	مستودع، زي ما اتحدد في مواصفات الشاشة."""
	from biozone_web.utils import get_default_warehouse, require_staff_access

	require_staff_access()

	if movement_type not in ("in", "out"):
		frappe.throw(_("نوع الحركة غير صحيح"))

	qty = frappe.utils.flt(qty)
	if qty <= 0:
		frappe.throw(_("الكمية يجب أن تكون أكبر من صفر"))

	item = frappe.db.get_value("Item", item_code, ["item_name", "stock_uom", "disabled"], as_dict=True)
	if not item or item.disabled:
		frappe.throw(_("الصنف غير موجود أو غير مفعّل"))

	conversion_factor = 1.0
	if uom != item.stock_uom:
		conversion_factor = frappe.db.get_value(
			"UOM Conversion Detail",
			{"parent": item_code, "parenttype": "Item", "uom": uom},
			"conversion_factor",
		)
		if not conversion_factor:
			frappe.throw(_("الوحدة المختارة غير معرّفة لهذا الصنف"))

	warehouse = get_default_warehouse()
	purpose = "Material Receipt" if movement_type == "in" else "Material Issue"

	if not frappe.db.exists("Stock Entry Type", purpose):
		frappe.throw(
			_("نوع حركة المخزون \"{0}\" غير معرّف في النظام — يرجى إعداده أولًا").format(purpose)
		)

	stock_entry = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"stock_entry_type": purpose,
			"purpose": purpose,
			"to_warehouse": warehouse if movement_type == "in" else None,
			"from_warehouse": warehouse if movement_type == "out" else None,
			"remarks": note or None,
			"items": [
				{
					"item_code": item_code,
					"qty": qty,
					"uom": uom,
					"conversion_factor": conversion_factor,
					"t_warehouse": warehouse if movement_type == "in" else None,
					"s_warehouse": warehouse if movement_type == "out" else None,
				}
			],
		}
	)
	stock_entry.insert()
	stock_entry.submit()

	return {"ok": True, "stock_entry": stock_entry.name}

@frappe.whitelist(methods=["POST"])
def staff_confirm_order(order_name: str):
    """B6c — 'قبول/تأكيد الطلب' من واجهة الموظف.

    ب4 بيحفظ الطلبات كـ Draft (قرار: التأكيد النهائي متوقع من الموظف).
    هذه الخطوة بتقفل الطلب فعليًا: submit للـSales Order (بتتحول لـ
    To Deliver and Bill) + إنشاء وتسليم Delivery Note مرتبط بيه على
    نفس المستودع الافتراضي (بنفس منطق single-warehouse في المخزون).
    """
    from biozone_web.utils import get_default_warehouse, require_staff_access

    require_staff_access()

    order_name = (order_name or "").strip()
    if not order_name or not frappe.db.exists("Sales Order", order_name):
        return {"ok": False, "error": _("الطلب غير موجود")}

    so = frappe.get_doc("Sales Order", order_name)
    if so.docstatus != 0:
        return {"ok": False, "error": _("هذا الطلب مؤكَّد بالفعل")}

    # 1) تأكيد الطلب (submit) — لو فشل نرجع الخطأ قبل ما نعمل إشعار تسليم
    try:
        so.submit()
    except Exception as exc:
        frappe.db.rollback()
        return {"ok": False, "error": _("تعذر تأكيد الطلب: {0}").format(exc)}

    # 2) إنشاء وتسليم Delivery Note مرتبط بالطلب المنفّذ
    warehouse = get_default_warehouse()
    try:
        dn = _create_delivery_note_for_order(so, warehouse)
    except Exception as exc:
        frappe.db.rollback()
        return {
            "ok": False,
            "error": _("تم تأكيد الطلب لكن تعذر إنشاء إشعار التسليم: {0}").format(exc),
        }

    frappe.db.commit()
    return {"ok": True, "sales_order": so.name, "delivery_note": dn.name}


def _create_delivery_note_for_order(so, warehouse):
    """ينشئ ويقدّم Delivery Note مرتبط بالطلب — الأصناف بـ`against_sales_order`
    + `so_detail` عشان عند الـsubmit تنزل الكميات من المستودع (`warehouse`)
    ويتحدّث delivered_qty على الطلب الأصلي.

    (الحقول اتأكدنا منها فعليًا على الإصدار المثبّت: البند فيه `warehouse`
    + `target_warehouse` للمرتجع فقط — مفيش `from_warehouse` ولا
    `delivery_note_type` على المستوى الأب).
    """
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