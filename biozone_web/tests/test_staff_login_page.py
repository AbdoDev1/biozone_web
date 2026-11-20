"""عقد صفحة دخول الموظفين (DB-free).

يثبّت ما طُلب صراحةً:
- عين كلمة المرور موجودة وتُبدّل type + الأيقونة + aria (مثل صفحة العملاء).
- عند 429 تُعرض رسالة حد المحاولات مع **العدّاد الحقيقي** (retry_after)،
  وعند أي فشل آخر تبقى الرسالة موحّدة بلا تمييز للسبب.
- لا يُعرض أي نص خام من الخادم (error/exception) في أي مسار.

    python3 -m unittest biozone_web.tests.test_staff_login_page
"""

import os
import unittest


def _read(*parts):
	root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
	with open(os.path.normpath(os.path.join(root, *parts)), encoding="utf-8") as fh:
		return fh.read()


class TestStaffLoginPage(unittest.TestCase):
	def setUp(self):
		self.page = _read("biozone_web", "www", "staff", "login.html")

	def test_password_eye_toggle_exists(self):
		self.assertIn('id="staff-password-toggle"', self.page)
		self.assertIn('aria-pressed="false"', self.page)
		self.assertIn('id="staff-eye-open"', self.page)
		self.assertIn('id="staff-eye-off"', self.page)

	def test_eye_toggle_switches_type_and_aria(self):
		self.assertIn("pwdInput.type = show ? 'text' : 'password';", self.page)
		self.assertIn("setAttribute('aria-pressed'", self.page)
		self.assertIn("إخفاء كلمة المرور", self.page)
		self.assertIn("إظهار كلمة المرور", self.page)

	def test_rate_limit_message_uses_real_counter(self):
		self.assertIn("retryAfterText", self.page)
		self.assertIn("message.retry_after", self.page)
		self.assertIn("RATE_LIMITED_MESSAGE", self.page)
		self.assertIn("res.status === 429", self.page)

	def test_other_failures_stay_uniform(self):
		self.assertIn("LOGIN_FAILED_MESSAGE", self.page)
		# لا قراءة لأي نص خام من الخادم
		self.assertNotIn("message.error", self.page)
		self.assertNotIn("data.exception", self.page)

	def test_retry_formatter_units(self):
		# نفس منطق صفحة العملاء: ثانية/دقيقة/ساعة
		self.assertIn("seconds < 60", self.page)
		self.assertIn("seconds < 3600", self.page)


if __name__ == "__main__":
	unittest.main()
