'use strict';

const assert = require('node:assert');
const guard = require('../public/js/cart_checkout_guard.js');

function fakeStorage() {
  const data = new Map();
  return {
    getItem(key) {
      return data.has(key) ? data.get(key) : null;
    },
    setItem(key, value) {
      data.set(key, String(value));
    },
    removeItem(key) {
      data.delete(key);
    },
    _data: data,
  };
}

const items = [
  { item_code: 'A', quantity: 1, uom: 'Nos' },
  { item_code: 'B', quantity: 2, uom: 'Box' },
];

assert.strictEqual(guard.signature(items), guard.signature(JSON.parse(JSON.stringify(items))));
assert.notStrictEqual(guard.signature(items), guard.signature([{ item_code: 'A', quantity: 2, uom: 'Nos' }]));
assert.notStrictEqual(guard.signature(items), guard.signature([{ item_code: 'A', quantity: 1, uom: 'Box' }]));

const storage = fakeStorage();
let counter = 0;
const attempts = guard.createAttemptStore(storage, 'cart:test', () => 'token-' + (++counter));

const first = attempts.get(items, 'f'.repeat(64));
assert.strictEqual(first, 'token-1');
assert.strictEqual(attempts.get(items, 'f'.repeat(64)), first, 'same snapshot+fingerprint must reuse token');
assert.strictEqual(attempts.get(items, 'e'.repeat(64)), first, 'a price-only change must keep the same attempt token');
assert.strictEqual(attempts.get([{ item_code: 'A', quantity: 5, uom: 'Nos' }], 'e'.repeat(64)), 'token-2', 'new snapshot must rotate token');
assert.strictEqual(attempts.get(items, 'f'.repeat(64)), first, 'returning to an earlier snapshot must reuse its token');

attempts.clear();
assert.strictEqual(attempts.get(items, 'e'.repeat(64)), 'token-3', 'clear must force a new token');

// CR-02: persistence is reported, never swallowed.
const durable = guard.createAttemptStore(fakeStorage(), 'cart:durable', () => 'tok-durable');
const durableAttempt = durable.getAttempt(items, 'a'.repeat(64));
assert.strictEqual(durableAttempt.persisted, true, 'a stored token must be reported as persisted');
assert.strictEqual(durableAttempt.reused, false);
assert.strictEqual(durable.getAttempt(items, 'a'.repeat(64)).persisted, true, 'a reused token stays persisted');
assert.strictEqual(durable.getAttempt(items, 'a'.repeat(64)).reused, true);

const brokenStorage = {
  getItem() { return null; },
  setItem() { throw new Error('quota'); },
  removeItem() { throw new Error('quota'); },
};
const volatile = guard.createAttemptStore(brokenStorage, 'cart:broken', () => 'tok-volatile');
const volatileAttempt = volatile.getAttempt(items, 'b'.repeat(64));
assert.strictEqual(volatileAttempt.persisted, false, 'a token that only lives in memory is not persisted');
assert.strictEqual(volatile.getAttempt(items, 'b'.repeat(64)).token, volatileAttempt.token, 'memory fallback still reuses its token');

// CR-02: a stored token stays persisted even when the cosmetic fingerprint
// refresh write fails.
const flaky = fakeStorage();
let failWrites = false;
const flakyStore = {
  getItem(key) { return flaky.getItem(key); },
  setItem(key, value) { if (failWrites) throw new Error('quota'); flaky.setItem(key, value); },
  removeItem(key) { flaky.removeItem(key); },
};
const flakyAttempts = guard.createAttemptStore(flakyStore, 'cart:flaky', () => 'tok-flaky');
assert.strictEqual(flakyAttempts.getAttempt(items, 'c'.repeat(64)).persisted, true);
failWrites = true;
const reusedAfterFailure = flakyAttempts.getAttempt(items, 'd'.repeat(64));
assert.strictEqual(reusedAfterFailure.token, 'tok-flaky');
assert.strictEqual(reusedAfterFailure.persisted, true, 'an already-stored token survives a failed refresh write');

// CR-03: clearAttempt removes one token only.
const multi = guard.createAttemptStore(fakeStorage(), 'cart:multi', (() => {
  let n = 0;
  return () => 'tok-multi-' + (++n);
})());
const tokenA = multi.get([{ item_code: 'A', quantity: 1, uom: 'Nos' }], '1'.repeat(64));
const tokenB = multi.get([{ item_code: 'B', quantity: 1, uom: 'Nos' }], '2'.repeat(64));
assert.notStrictEqual(tokenA, tokenB);
assert.strictEqual(multi.clearAttempt(tokenA), true);
assert.strictEqual(multi.get([{ item_code: 'A', quantity: 1, uom: 'Nos' }], '1'.repeat(64)), 'tok-multi-3', 'cleared attempt must rotate');
assert.strictEqual(multi.get([{ item_code: 'B', quantity: 1, uom: 'Nos' }], '2'.repeat(64)), tokenB, 'other attempts must survive');

const revision = guard.createRevision();
const start = revision.read();
assert.ok(revision.isCurrent(start));
revision.bump();
assert.ok(!revision.isCurrent(start), 'a local write must invalidate older async results');
assert.ok(revision.isCurrent(revision.read()));

// هوية دورة السلة: إنشاء/قراءة/إعادة استخدام/تدوير آمن.
const instStorage = fakeStorage();
const instA = 'ci' + 'a'.repeat(30);
const instB = 'ci' + 'b'.repeat(30);
assert.strictEqual(
  guard.ensureCartInstance(instStorage, 'cart:inst', () => instA),
  instA,
  'ensure creates the instance'
);
assert.strictEqual(
  guard.ensureCartInstance(instStorage, 'cart:inst', () => instB),
  instA,
  'ensure reuses the stored instance'
);
assert.strictEqual(guard.readCartInstance(instStorage, 'cart:inst'), instA);
assert.strictEqual(
  guard.rotateCartInstance(instStorage, 'cart:inst', instA, () => instB),
  instB,
  'rotate replaces the instance of the current cycle'
);
assert.strictEqual(
  guard.rotateCartInstance(instStorage, 'cart:inst', 'ci' + 'z'.repeat(30), () => instA),
  instB,
  'compare-and-rotate must not rotate when another tab already did'
);
const shortStorage = fakeStorage();
shortStorage.setItem('biozone_cart_instance:cart:short', 'short');
assert.strictEqual(
  guard.ensureCartInstance(shortStorage, 'cart:short', () => instA),
  instA,
  'an invalid stored instance is replaced'
);
assert.ok(
  /^[A-Za-z0-9_-]{16,64}$/.test(guard.createCartInstance()),
  'generated cart instances are URL-safe and long enough'
);

// تعارض دورة السلة: رابط الطلب القائم، تدوير، بلا مسح/إعادة إرسال.
const conflictPlan = guard.planInstanceConflict({
  redirect: '/order-confirmed?name=SO-1',
});
assert.strictEqual(conflictPlan.rotate, true);
assert.strictEqual(conflictPlan.keepCart, true);
assert.strictEqual(conflictPlan.resend, false, 'a conflict must never auto-resend');
assert.strictEqual(conflictPlan.redirect, '/order-confirmed?name=SO-1');
assert.ok(conflictPlan.message.length > 0, 'the conflict shows a message');
assert.strictEqual(guard.planInstanceConflict(null).redirect, '');

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// سلة مقسّمة: بحث + صفحات 20 صنفًا (CR-05 pagination/search).
const cartItems = Array.from({ length: 45 }, (_, i) => ({
  item_code: 'BZ-' + (1000 + i),
  item_name: 'صنف رقم ' + i,
}));

assert.strictEqual(guard.paginate(cartItems, 1, 20).items.length, 20, 'first page has 20 items');
assert.strictEqual(guard.paginate(cartItems, 2, 20).items[0].item_code, 'BZ-1020');
assert.strictEqual(guard.paginate(cartItems, 3, 20).items.length, 5, 'last page keeps the remainder');
assert.strictEqual(guard.paginate(cartItems, 3, 20).pageCount, 3);
assert.strictEqual(guard.paginate(cartItems, 99, 20).page, 3, 'page is clamped to the last page');
assert.strictEqual(guard.paginate(cartItems, 0, 20).page, 1, 'page is clamped to the first page');
assert.strictEqual(guard.paginate([], 5, 20).pageCount, 1, 'empty list still reports one page');

assert.strictEqual(guard.filterCartItems(cartItems, '').length, 45, 'empty query keeps every line');
assert.strictEqual(guard.filterCartItems(cartItems, '   ').length, 45, 'blank query keeps every line');
assert.strictEqual(guard.filterCartItems(cartItems, 'BZ-1042')[0].item_code, 'BZ-1042');
assert.strictEqual(guard.filterCartItems(cartItems, 'bz-1001').length, 1, 'code search is case-insensitive');
assert.strictEqual(guard.filterCartItems(cartItems, 'لا يوجد').length, 0);
assert.strictEqual(
  guard.filterCartItems([{ item_code: 'X', item_name: 'ابل اكالة فواكه' }], 'اكالة').length,
  1,
  'Arabic item-name search works'
);

// حالة زر التأكيد (A): نص/أيقونة/تعطيل لكل مرحلة + عودة طبيعية.
assert.strictEqual(guard.confirmButtonContent('idle').text, 'تأكيد الطلب');
assert.strictEqual(guard.confirmButtonContent('idle').icon, 'check_circle');
assert.strictEqual(guard.confirmButtonContent('idle').busy, false);
assert.strictEqual(guard.confirmButtonContent('verifying').text, 'جارٍ التحقق من الأسعار...');
assert.strictEqual(guard.confirmButtonContent('verifying').icon, 'progress_activity');
assert.strictEqual(guard.confirmButtonContent('verifying').busy, true);
assert.strictEqual(guard.confirmButtonContent('submitting').text, 'جارٍ تأكيد الطلب...');
assert.strictEqual(guard.confirmButtonContent('submitting').busy, true);
assert.strictEqual(
  guard.confirmButtonContent('unknown').text,
  'تأكيد الطلب',
  'an unknown phase must fall back to the idle button'
);

async function testLockSerializes() {
  const lockStorage = fakeStorage();
  const order = [];
  const firstRun = guard.withCheckoutLock(
    'cart:lock',
    async () => {
      order.push('a-start');
      await sleep(80);
      order.push('a-end');
    },
    { storage: lockStorage, timeoutMs: 3000, leaseMs: 5000 }
  );
  const secondRun = guard.withCheckoutLock(
    'cart:lock',
    async () => {
      order.push('b-start');
      order.push('b-end');
    },
    { storage: lockStorage, timeoutMs: 3000, leaseMs: 5000 }
  );
  await Promise.all([firstRun, secondRun]);
  assert.deepStrictEqual(order, ['a-start', 'a-end', 'b-start', 'b-end'], 'lease fallback must serialize callers');
}

async function testLockTimeout() {
  const lockStorage = fakeStorage();
  lockStorage.setItem(
    'biozone_checkout_lock:cart:busy',
    JSON.stringify({ owner: 'other-tab', expires: Date.now() + 60000 })
  );
  let rejected = null;
  try {
    await guard.withCheckoutLock('cart:busy', async () => {}, {
      storage: lockStorage,
      timeoutMs: 150,
      leaseMs: 5000,
    });
  } catch (err) {
    rejected = err;
  }
  assert.ok(rejected, 'a held lock must not be entered');
  assert.strictEqual(rejected.code, 'CHECKOUT_LOCK_TIMEOUT');
}

async function testFetchTimeout() {
  const ok = await guard.fetchWithTimeout(
    () => Promise.resolve({ status: 200 }),
    '/x',
    { method: 'POST' },
    500
  );
  assert.strictEqual(ok.status, 200, 'a fast request resolves');

  let rejected = null;
  try {
    await guard.fetchWithTimeout(
      () => new Promise(() => {}),
      '/x',
      { method: 'POST' },
      50
    );
  } catch (err) {
    rejected = err;
  }
  assert.ok(rejected, 'a hung request must not wait forever');
  assert.strictEqual(rejected.code, 'REQUEST_TIMEOUT');
}

async function testLockReleasesOnError() {
  const lockStorage = fakeStorage();
  await assert.rejects(
    guard.withCheckoutLock(
      'cart:throws',
      async () => { throw new Error('boom'); },
      { storage: lockStorage, timeoutMs: 1000, leaseMs: 5000 }
    ),
    /boom/
  );
  assert.strictEqual(lockStorage.getItem('biozone_checkout_lock:cart:throws'), null, 'lock must be released after an error');
  let ran = false;
  await guard.withCheckoutLock(
    'cart:throws',
    async () => { ran = true; },
    { storage: lockStorage, timeoutMs: 1000, leaseMs: 5000 }
  );
  assert.strictEqual(ran, true, 'lock must be reusable after an error');
}

(async () => {
  await testLockSerializes();
  await testLockTimeout();
  await testLockReleasesOnError();
  await testFetchTimeout();
  console.log('cart_checkout_guard: all assertions passed');
})();
