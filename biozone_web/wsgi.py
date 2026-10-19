from frappe.app import application as frappe_application
from werkzeug.middleware.proxy_fix import ProxyFix

application = ProxyFix(frappe_application, x_for=1, x_proto=1)
