"""Plan §4.13 tests 21 and 22: real two-connection races on dev.

Gated by BIOZONE_RACE_TESTS=1 because these tests commit (threads use their
own DB connections) and create/delete a dedicated user. Run with:

    BIOZONE_RACE_TESTS=1 bench --site development.localhost run-tests \
        --app biozone_web --module biozone_web.tests.test_cart_snapshot_race

22: two threads call Undo with the same token; exactly one applies, the other
    replays the committed result (already_restored=True), cart version +1.
21: thread A holds the cart row lock (the lock checkout_context takes) and
    converts the cart; thread B's Undo blocks on that lock, then returns 404
    SNAPSHOT_NOT_AVAILABLE ("converted to an order") without consuming the
    snapshot. Limit: this proves serialization/post-success behaviour, not a
    separate "Processing" receipt check — Undo has no such check; it relies on
    the cart row lock.
"""

import os
import threading
import time
import unittest
import uuid

import frappe

from biozone_web.services import cart as cart_service

ITEM_A = "BZ-11929"
USER = "test-cart-race@example.com"
TOKEN_22 = "1" * 64
TOKEN_21 = "2" * 64
SKIP_REASON = "set BIOZONE_RACE_TESTS=1 to run committed two-connection races"


def _connect(site, sites_path):
    frappe.init(site=site, sites_path=sites_path)
    frappe.connect()
    frappe.set_user(USER)
    frappe.db.sql("SET SESSION innodb_lock_wait_timeout = 10")


def _cleanup():
    frappe.db.rollback()
    frappe.db.sql(
        "delete from `tabBZ Cart Item` where parent in "
        "(select name from `tabBZ Cart` where user = %s)",
        (USER,),
    )
    frappe.db.delete("BZ Cart Snapshot", {"source_user": USER})
    for doctype in (
        "BZ Cart",
        "BZ Cart Merge",
        "BZ Checkout Receipt",
        "Sessions",
    ):
        frappe.db.delete(doctype, {"user": USER})
    frappe.db.delete("User", {"name": USER})
    frappe.db.commit()


@unittest.skipUnless(os.environ.get("BIOZONE_RACE_TESTS") == "1", SKIP_REASON)
class TestCartSnapshotRace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not frappe.db.table_exists("BZ Cart Snapshot"):
            raise unittest.SkipTest("BZ Cart Snapshot table absent — run migrate first")
        cls.site = frappe.local.site
        cls.sites_path = frappe.get_site_path().rsplit("/", 1)[0]

    def setUp(self):
        from unittest.mock import patch

        self._prev_user = frappe.session.user
        self._customer_patch = patch.object(
            cart_service, "require_customer", return_value=None
        )
        self._customer_patch.start()
        self._flag_patch = patch.object(
            cart_service, "snapshots_enabled", return_value=True
        )
        self._flag_patch.start()
        _cleanup()
        frappe.get_doc(
            {
                "doctype": "User",
                "email": USER,
                "first_name": "Cart",
                "last_name": "Race",
                "send_welcome_email": 0,
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()

    def tearDown(self):
        _cleanup()
        self._flag_patch.stop()
        self._customer_patch.stop()
        frappe.set_user(self._prev_user)
        frappe.db.rollback()

    def _snapshot(self):
        frappe.set_user(USER)
        state = cart_service.add_item(ITEM_A, 1, cart_version=0)
        cleared = cart_service.clear_cart(cart_version=state["cart"]["version"])
        frappe.db.commit()
        return (
            cleared["undo"]["snapshot"],
            cleared["cart"]["version"],
            cleared["cart"]["name"],
        )

    def _run(self, workers, timeout=20):
        threads = [threading.Thread(target=worker) for worker in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout)
        self.assertTrue(all(not thread.is_alive() for thread in threads), "thread hung")

    def test_concurrent_undo_same_token_applies_once(self):
        snap, version, cart_name = self._snapshot()
        results = []
        barrier = threading.Barrier(2)

        def worker():
            _connect(self.site, self.sites_path)
            try:
                barrier.wait(timeout=10)
                result = cart_service.undo(snap, TOKEN_22, cart_version=version)
                frappe.db.commit()
                results.append(
                    (
                        "ok",
                        bool(result.get("already_restored")),
                        int(result.get("restored_count") or 0),
                    )
                )
            except Exception as exc:
                frappe.db.rollback()
                results.append(
                    ("err", type(exc).__name__, getattr(exc, "error_code", None), str(exc))
                )
            finally:
                frappe.destroy()

        self._run([worker, worker])

        self.assertEqual(len(results), 2, results)
        self.assertTrue(all(row[0] == "ok" for row in results), results)
        self.assertEqual(sorted(row[1] for row in results), [False, True], results)
        self.assertEqual([row[2] for row in results], [1, 1], results)
        doc = frappe.get_doc("BZ Cart Snapshot", snap)
        self.assertEqual(doc.status, "Restored")
        self.assertEqual(int(doc.restored_count or 0), 1)
        cart = frappe.get_doc("BZ Cart", cart_name)
        self.assertEqual(int(cart.version or 0), version + 1)

    def test_concurrent_undo_different_tokens_restores_once(self):
        snap, version, cart_name = self._snapshot()
        results = []
        barrier = threading.Barrier(2)
        tokens = ["3" * 64, "4" * 64]

        def worker(token):
            _connect(self.site, self.sites_path)
            try:
                barrier.wait(timeout=10)
                result = cart_service.undo(snap, token, cart_version=version)
                frappe.db.commit()
                results.append(
                    (
                        "ok",
                        bool(result.get("already_restored")),
                        int(result.get("restored_count") or 0),
                    )
                )
            except Exception as exc:
                frappe.db.rollback()
                results.append(
                    ("err", type(exc).__name__, getattr(exc, "error_code", None), str(exc))
                )
            finally:
                frappe.destroy()

        self._run([lambda: worker(tokens[0]), lambda: worker(tokens[1])])

        self.assertEqual(len(results), 2, results)
        oks = [row for row in results if row[0] == "ok"]
        errs = [row for row in results if row[0] == "err"]
        self.assertEqual(len(oks), 1, results)
        self.assertEqual(len(errs), 1, results)
        self.assertEqual(errs[0][2], "SNAPSHOT_ALREADY_RESTORED", results)
        doc = frappe.get_doc("BZ Cart Snapshot", snap)
        self.assertEqual(doc.status, "Restored")
        self.assertEqual(int(doc.restored_count or 0), 1)
        cart = frappe.get_doc("BZ Cart", cart_name)
        self.assertEqual(int(cart.version or 0), version + 1)
        self.assertEqual([(row.item_code, row.qty) for row in cart.items], [(ITEM_A, 1)])

    def test_undo_blocks_on_checkout_lock_then_404_after_conversion(self):
        snap, version, cart_name = self._snapshot()
        locked = threading.Event()
        results = []

        def checkout_worker():
            _connect(self.site, self.sites_path)
            try:
                cart = cart_service.get_active_cart(USER, for_update=True)
                locked.set()
                time.sleep(1.0)
                frappe.db.set_value(
                    "BZ Cart",
                    cart.name,
                    {
                        "status": "Converted",
                        "active_key": None,
                        "version": int(cart.version or 0) + 1,
                    },
                    update_modified=True,
                )
                frappe.db.commit()
                results.append(("checkout", "ok"))
            except Exception as exc:
                frappe.db.rollback()
                results.append(("checkout", "err", type(exc).__name__, str(exc)))
            finally:
                frappe.destroy()

        def undo_worker():
            _connect(self.site, self.sites_path)
            try:
                self.assertTrue(locked.wait(timeout=10), "checkout never locked")
                started = time.monotonic()
                try:
                    cart_service.undo(snap, TOKEN_21, cart_version=version)
                    results.append(("undo", "unexpected-success"))
                except cart_service.CartError as exc:
                    elapsed = time.monotonic() - started
                    results.append(
                        (
                            "undo",
                            exc.error_code,
                            exc.status,
                            exc.message,
                            round(elapsed, 2),
                        )
                    )
                frappe.db.rollback()
            except Exception as exc:
                frappe.db.rollback()
                results.append(("undo", "err", type(exc).__name__, str(exc)))
            finally:
                frappe.destroy()

        self._run([checkout_worker, undo_worker])

        self.assertIn(("checkout", "ok"), results, results)
        undo_rows = [row for row in results if row[0] == "undo"]
        self.assertEqual(len(undo_rows), 1, results)
        _, code, status, message, elapsed = undo_rows[0]
        self.assertEqual(code, "SNAPSHOT_NOT_AVAILABLE")
        self.assertEqual(status, 404)
        self.assertIn("تحوّلت إلى طلب", message)
        self.assertGreaterEqual(elapsed, 0.8, "undo did not block on the cart lock")
        doc = frappe.get_doc("BZ Cart Snapshot", snap)
        self.assertEqual(doc.status, "Available")
        cart = frappe.get_doc("BZ Cart", cart_name)
        self.assertEqual(cart.status, "Converted")


if __name__ == "__main__":
    unittest.main()
