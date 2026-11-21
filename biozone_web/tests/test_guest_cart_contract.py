"""عقد سلة الزائر ونقلها إلى الحساب (DB-free).

يثبّت القرار الحالي بدلًا من إعادة اكتشافه لاحقًا:
- الزائر يضيف ويعرض ويعدّل على مفتاح محلي ثابت (__guest__)، بلا سلة خادم.
- النقل إلى سلة الحساب لا يحدث إلا بمعاينة + موافقة صريحة، ولا يُكتب شيء
  في سلة الحساب قبلها.
- المسح (بيانات الزائر + حالة checkout) لا يحدث إلا بعد نجاح الكتابة.
- العودة إلى السلة بعد الدخول، ومنع نقل سلة الزائر لحساب آخر على جهاز
  مشترك (ربط الملكية).
- حد 200 سطر يمنع النقل بلا اقتطاع صامت.

لا frappe ولا قاعدة بيانات:

    python3 -m pytest biozone_web/tests/test_guest_cart_contract.py -q
"""

import os
import unittest

MAX_LINES = 200


def _repo_path(*parts):
	root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
	return os.path.normpath(os.path.join(root, *parts))


def _read(*parts):
	with open(_repo_path(*parts), encoding="utf-8") as fh:
		return fh.read()


class TestGuestCartKeys(unittest.TestCase):
	def setUp(self):
		self.header = _read("biozone_web", "templates", "includes", "site_header.html")
		self.helpers = _read("biozone_web", "templates", "includes", "cart_helpers.html")

	def test_guest_has_a_cart_key(self):
		self.assertIn("bzAccountKey('__guest__')", self.header)
		self.assertIn("window.BZ_CART_IS_GUEST", self.header)
		# لا مفتاح null للزائر مرة أخرى: هذا ما كان يمنع الإضافة.
		self.assertNotIn("? bzAccountKey(window.BZ_STORE_USER) : null", self.header)

	def test_add_to_cart_is_not_login_gated(self):
		self.assertNotIn("bzRequireLogin", self.helpers)
		self.assertIn("const CART_STORAGE_KEY = window.BZ_CART_KEY;", self.helpers)

	def test_line_cap_is_200(self):
		self.assertIn("const CART_MAX_LINES = 200;", self.helpers)


class TestCartPage(unittest.TestCase):
	def setUp(self):
		self.page = _read("biozone_web", "www", "cart.html")
		self.py = _read("biozone_web", "www", "cart.py")
		self.js = _read("biozone_web", "public", "js", "guest_cart_merge.js")

	def test_guest_can_open_and_edit_cart(self):
		self.assertNotIn('redirect_location = "/login"', self.py)
		self.assertIn("context.is_guest", self.py)
		self.assertNotIn("if (!window.BZ_CART_KEY) { window.location.href = '/login'; return; }", self.page)

	def test_merge_module_and_preview_markup(self):
		self.assertIn("/assets/biozone_web/js/guest_cart_merge.js", self.page)
		self.assertIn('id="guest-merge-modal"', self.page)
		self.assertIn("openGuestMergePreview", self.page)
		self.assertIn("applyGuestMerge", self.page)

	def test_guest_checkout_redirects_to_login_and_back(self):
		self.assertIn("biozone_login_return", self.page)
		self.assertIn("goToLoginForCart", self.page)

	def test_merge_runs_inside_a_cross_tab_lock(self):
		self.assertIn("commitMerge", self.page)
		self.assertIn("CART_KEY + ':merge'", self.page)
		self.assertIn("withCheckoutLock", self.page)
		self.assertIn("expected: {", self.page)

	def test_stale_preview_is_not_applied(self):
		self.assertIn("reason === 'changed'", self.page)
		self.assertIn("لم يُنقل شيء ولم يُمسح شيء", self.page)
		self.assertIn("mergeBusy", self.page)

	def test_merge_is_fail_closed_without_web_locks(self):
		self.assertIn("hasWebLocks", self.page)
		self.assertIn("mergeUnsupportedMessage", self.page)
		self.assertIn("MERGE_LOCK_UNAVAILABLE", self.page)
		self.assertIn("info.mode !== 'web-locks'", self.page)
		self.assertIn("سلة الزائر محفوظة كما هي ولم يُنقل شيء", self.js)
		self.assertIn("mergeUiState", self.js)

	def test_capability_is_resolved_before_any_network(self):
		self.assertIn("const MERGE_UI = window.BzGuestCart && window.BzGuestCart.mergeUiState", self.page)
		self.assertIn("initMergeCapability();", self.page)
		# التهيئة تأتي قبل أي جلب أسعار (التهيئة تُستدعى قبل render/reprice)
		cap_at = self.page.index("initMergeCapability();")
		reprice_at = self.page.index("repriceFromServer(loadCart()).then")
		self.assertLess(cap_at, reprice_at, "فحص الدعم يسبق أي نداء شبكة")
		# وبلا دعم لا تُفتح المعاينة تلقائيًا ولا يُجلب تسعير
		self.assertIn("if (!MERGE_UI.available) return;", self.page)

	def test_merge_result_notice_survives_other_tab_writes(self):
		self.assertIn("data-persist", self.page)
		self.assertIn("clearNotice({ fromStorage: true })", self.page)

	def test_merge_preview_is_paginated_not_dumped(self):
		self.assertIn("MERGE_PREVIEW_LIMIT", self.js)
		self.assertIn("paginate", self.js)
		self.assertIn("mergeState.page", self.page)
		self.assertIn("صفحة ' + paged.page + ' من ' + paged.pageCount", self.page)
		self.assertNotIn("mergeState.showAll", self.page)
		# سقف ارتفاع فعلي للمودال (الفئة max-h-[90vh] غير مولّدة في CSS المبني)
		self.assertIn("#guest-merge-modal > div", self.page)
		self.assertIn("max-height: 90vh", self.page)

	def test_line_cap_blocks_instead_of_truncating(self):
		self.assertIn("mergeState.plan.block", self.page)
		self.assertIn("لن يُحذف شيء تلقائيًا", self.page)


class TestLoginReturn(unittest.TestCase):
	def setUp(self):
		self.login = _read("biozone_web", "www", "login.html")

	def test_returns_to_cart_after_login(self):
		self.assertIn("postLoginTarget", self.login)
		self.assertIn("biozone_login_return", self.login)
		self.assertNotIn("window.location.href = result.home_page || '/biozone-home';", self.login)

	def test_return_path_is_local_only(self):
		self.assertIn("safeLocalPath", self.login)
		self.assertIn("value.charAt(1) === '/'", self.login)

	def test_pending_guest_cart_sends_user_to_cart(self):
		self.assertIn("hasPendingGuestCart", self.login)


class TestMergeModule(unittest.TestCase):
	def setUp(self):
		self.js = _read("biozone_web", "public", "js", "guest_cart_merge.js")

	def test_guest_key_and_cap(self):
		self.assertIn("var GUEST_KEY = ACCOUNT_PREFIX + GUEST_SUFFIX;", self.js)
		self.assertIn("var MAX_LINES = 200;", self.js)

	def test_owner_binding_blocks_other_accounts(self):
		self.assertIn("if (current && current !== who) return false;", self.js)
		self.assertIn("return !owner || (!!who && owner === who);", self.js)

	def test_clear_removes_guest_checkout_state(self):
		for prefix in (
			"biozone_checkout_pending:",
			"biozone_cart_instance:",
			"biozone_checkout_lock:",
		):
			self.assertIn(prefix, self.js)
		self.assertIn("function clearGuestState", self.js)

	def test_no_silent_default_for_uom_conflict(self):
		# تعارض الوحدة يولد بقرار فارغ ويُطلب من المستخدم اختياره.
		self.assertIn("action: '',", self.js)
		self.assertIn("if (action !== 'keep_account' && action !== 'use_guest')", self.js)

	def test_server_prices_replace_browser_prices(self):
		self.assertIn("base[index].price = Number(row.price || 0);", self.js)

	def test_commit_reads_state_again_inside_the_lock(self):
		body = self.js[self.js.index("async function commitMerge"):]
		read_at = body.index("readItems(storage, accountKey)")
		sig_at = body.index("signature(accountRaw) !== expected.account")
		intent_at = body.index("INTENT_KEY")
		write_at = body.index("writeItems(storage, accountKey, built.items)")
		self.assertLess(read_at, sig_at, "القراءة الفعلية قبل المقارنة")
		self.assertLess(sig_at, write_at, "المقارنة قبل الكتابة")
		self.assertLess(intent_at, write_at, "النيّة تُكتب قبل سلة الحساب")

	def test_commit_requires_a_lock(self):
		self.assertIn("function hasWebLocks", self.js)
		self.assertIn("reason: 'no_lock'", self.js)
		self.assertIn("typeof lock !== 'function'", self.js)

	def test_interrupted_merge_resumes_without_remixing(self):
		self.assertIn("function resumeMerge", self.js)
		self.assertIn("intent.accountAfter", self.js)
		self.assertIn("recovered: true", self.js)


if __name__ == "__main__":
	unittest.main()
