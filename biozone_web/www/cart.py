import frappe

from biozone_web.utils import get_header_context


def get_context(context):
	context.no_cache = 1
	context.active_page = "cart"
	context.update(get_header_context())
	return context
