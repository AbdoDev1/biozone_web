app_name = "biozone_web"
app_title = "Biozone Web"
app_publisher = "Abdulrahman"
app_description = "Biozone web store built on native Frappe/ERPNext"
app_email = "abdulrahmanali.h.t@gmail.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "biozone_web",
# 		"logo": "/assets/biozone_web/logo.png",
# 		"title": "Biozone Web",
# 		"route": "/biozone_web",
# 		"has_permission": "biozone_web.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/biozone_web/css/biozone_web.css"
# app_include_js = "/assets/biozone_web/js/biozone_web.js"

# include js, css files in header of web template
# web_include_css = "/assets/biozone_web/css/biozone_web.css"
# web_include_js = "/assets/biozone_web/js/biozone_web.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "biozone_web/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "biozone_web/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# ملحوظة: ده معطّل عمليًا — مفيش Role اسمه "System User" في القاعدة،
# فالشيك ده مش بيطابق حد. التحديد الفعلي بيحصل في
# get_website_user_home_page تحت.
role_home_page = {
	"System User": "/staff/dashboard",
}

# الصفحة الرئيسية حسب نوع الحساب لمسار "/" — التشخيص:
# من غير hook ده، Website User بيتحط على fallback "me" فبيشوف
# صفحة Settings الافتراضية (Edit Profile / Reset Password / 3rd party
# apps) جوّا استجابة 200 لـ "/". الدالة دي بتحدد الصفحة لكل نوع:
# - Guest: بيفتح المتجر مباشرة /biozone-home (يشوف الكاتالوج فعليًا).
# - Website User: بيوصل مباشرة لصفحة المتجر /biozone-home.
# - System User: /staff/dashboard لو موظف عادي، أو /desk لو
#   Administrator/صاحب دور إداري (راجع get_home_route_for_system_user).
# من غير المساس بصفحة /me نفسها لأنها ممكن تكون مطلوبة من أماكن تانية.
get_website_user_home_page = "biozone_web.utils.get_website_user_home_page"

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# مسار تفاصيل العميل في الستاف: /staff/customers/<customer_name> تُحسم إلى
# الصفحة الثابتة staff/customer-detail، والاسم المطابَق يصل تلقائيًا في
# frappe.form_dict (آلية website_route_rules القياسية في Frappe — لا
# DocType مولّد ولا Web Form مستخدمان هنا).
website_route_rules = [
	{"from_route": "/staff/customers/<customer_name>", "to_route": "staff/customer-detail"},
	# تفاصيل طلب العميل: /account/orders/<name> تُحسم إلى الصفحة الثابتة
	# account/order-detail، والاسم المطابَق يصل في frappe.form_dict.
	{"from_route": "/account/orders/<order_name>", "to_route": "account/order-detail"},
]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "biozone_web.utils.jinja_methods",
# 	"filters": "biozone_web.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "biozone_web.install.before_install"
# after_install = "biozone_web.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "biozone_web.uninstall.before_uninstall"
# after_uninstall = "biozone_web.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "biozone_web.utils.before_app_install"
# after_app_install = "biozone_web.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "biozone_web.utils.before_app_uninstall"
# after_app_uninstall = "biozone_web.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "biozone_web.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "biozone_web.notifications.get_notification_config"

# Biozone in-app notification types (single source: imported by the
# notifications service and the registration patch — X4). The leading
# underscore keeps the hooks loader from treating it as a hook.
_BZ_NOTIFICATION_TYPES = (
	"BZ New Order",
	"BZ Delivered",
	"BZ Escalation",
	"BZ Escalation Reminder",
	"BZ Order Cancelled",
	"BZ Order Returned",
)

# Our types never send email (in-app only). Mandatory: new users are
# seeded with every enabled non-skipped type, so without this line a
# new customer would get BZ emails.
notification_skip_email_types = list(_BZ_NOTIFICATION_TYPES)

# Awesome Bar
# -----------
# Extra search results: list of dicts with label, description, route, index.
# route: ["List", "ToDo"], "/desk/docs/some/page", or "https://example.com"
# awesomebar_search = ["biozone_web.search.awesomebar_results"]

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"biozone_web.tasks.all"
# 	],
# 	"daily": [
# 		"biozone_web.tasks.daily"
# 	],
# 	"hourly": [
# 		"biozone_web.tasks.hourly"
# 	],
# 	"weekly": [
# 		"biozone_web.tasks.weekly"
# 	],
# 	"monthly": [
# 		"biozone_web.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "biozone_web.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "biozone_web.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "biozone_web.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "biozone_web.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# حارس مسارات Desk على مضيفي المتجر والستاف (قرار فصل النطاقات — قاعدة
# نهائية: app.biozone.pro وحده يعرض Desk). المسارات محددة بدقة (^/desk ،^/app) حتى
# لا يمس أي /api أو أصول أو صفحات متجر — وتفاصيل الحارس في utils.
before_request = ["biozone_web.utils.guard_domain_routes"]
# after_request = ["biozone_web.utils.after_request"]

# Job Events
# ----------
# before_job = ["biozone_web.utils.before_job"]
# after_job = ["biozone_web.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"biozone_web.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
