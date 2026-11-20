'use strict';

// اختبارات وحدة لنقل سلة الزائر إلى سلة الحساب (بلا متصفح):
//   node biozone_web/tests/test_guest_cart_merge.js
const assert = require('node:assert');
const merge = require('../public/js/guest_cart_merge.js');

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

const GUEST = merge.GUEST_KEY;
const ACCOUNT = merge.accountKey('Customer@Example.com');

function seed(storage, key, items) {
  storage.setItem(key, JSON.stringify(items));
}

function read(storage, key) {
  const raw = storage.getItem(key);
  return raw ? JSON.parse(raw) : [];
}

// ---- مفاتيح وسلة الزائر ----

assert.strictEqual(GUEST, 'biozone_cart_items:__guest__');
assert.strictEqual(ACCOUNT, 'biozone_cart_items:customer@example.com');
assert.notStrictEqual(GUEST, ACCOUNT, 'سلة الزائر لا تشارك مفتاح أي حساب');

// ---- priceLines: الخادم وحده يحدد السعر والوحدة ----

{
  const lines = [
    { item_code: 'A', item_name: 'صنف أ', quantity: 2, uom: 'Nos', price: 999 },
    { item_code: 'B', item_name: 'صنف ب', quantity: 1, uom: 'Nos', price: 5 },
    { item_code: 'C', item_name: 'صنف ج', quantity: 0, uom: 'Nos' },
  ];
  const prices = {
    A: { price: 10, display_uom: 'Box', thumbnail: '/a.png' },
    B: { price: null, display_uom: 'Nos' },
  };
  const priced = merge.priceLines(lines, prices);
  assert.strictEqual(priced.lines.length, 1);
  assert.strictEqual(priced.lines[0].price, 10, 'السعر من الخادم وليس من المتصفح');
  assert.strictEqual(priced.lines[0].wasPrice, 999, 'سعر المتصفح محفوظ للعرض فقط');
  assert.strictEqual(priced.lines[0].uom, 'Box', 'الوحدة من الخادم');
  assert.strictEqual(priced.lines[0].image, '/a.png');
  assert.deepStrictEqual(priced.unavailable.map((x) => x.item_code), ['B']);
  assert.deepStrictEqual(priced.invalid.map((x) => x.item_code), ['C']);
}

// ---- buildPlan: إضافة / جمع كميات / تعارض وحدة بلا قرار افتراضي ----

{
  const accountItems = [
    { item_code: 'A', item_name: 'صنف أ', quantity: 3, uom: 'Box', price: 10 },
    { item_code: 'B', item_name: 'صنف ب', quantity: 1, uom: 'Nos', price: 5 },
  ];
  const guestLines = [
    { item_code: 'A', item_name: 'صنف أ', quantity: 2, uom: 'Box', price: 10 },
    { item_code: 'B', item_name: 'صنف ب', quantity: 4, uom: 'Kg', price: 5 },
    { item_code: 'C', item_name: 'صنف ج', quantity: 1, uom: 'Nos', price: 7 },
  ];
  const plan = merge.buildPlan({
    guestLines,
    accountItems,
    unavailable: [{ item_code: 'D', item_name: 'صنف د' }],
  });
  assert.strictEqual(plan.stats.additions, 1);
  assert.strictEqual(plan.stats.matches, 1);
  assert.strictEqual(plan.stats.conflicts, 1);
  assert.strictEqual(plan.stats.unavailableCount, 1);
  assert.strictEqual(plan.block, null);

  const qtyRow = plan.rows.find((r) => r.kind === 'qty');
  assert.strictEqual(qtyRow.action, 'sum', 'الجمع هو الافتراضي المعروض صراحةً');
  const uomRow = plan.rows.find((r) => r.kind === 'uom');
  assert.strictEqual(uomRow.action, '', 'تعارض الوحدة لا يُحسم تلقائيًا');
  assert.strictEqual(uomRow.accountUom, 'Nos');
  assert.strictEqual(uomRow.guestUom, 'Kg');
}

// ---- applyPlan: كل القرارات محترمة + لا تغيير في الأصل ----

{
  const accountItems = [
    { item_code: 'A', quantity: 3, uom: 'Box', price: 10, item_name: 'أ' },
    { item_code: 'B', quantity: 1, uom: 'Box', price: 5, item_name: 'ب' },
  ];
  const guestLines = [
    { item_code: 'A', quantity: 2, uom: 'Box', price: 10, item_name: 'أ' },
    { item_code: 'B', quantity: 4, uom: 'Kg', price: 5, item_name: 'ب' },
    { item_code: 'C', quantity: 1, uom: 'Nos', price: 7, item_name: 'ج' },
  ];
  const plan = merge.buildPlan({ guestLines, accountItems });

  const unresolved = merge.applyPlan(plan, {}, accountItems);
  assert.strictEqual(unresolved.ok, false);
  assert.strictEqual(unresolved.error, 'unresolved');
  assert.deepStrictEqual(unresolved.unresolved.map((x) => x.item_code), ['B']);

  const applied = merge.applyPlan(plan, { B: 'use_guest' }, accountItems);
  assert.strictEqual(applied.ok, true);
  const byCode = Object.fromEntries(applied.items.map((it) => [it.item_code, it]));
  assert.strictEqual(byCode.A.quantity, 5, 'جمع الكميات بعد الموافقة');
  assert.strictEqual(byCode.B.quantity, 4);
  assert.strictEqual(byCode.B.uom, 'Kg', 'اختيار وحدة الزائر');
  assert.strictEqual(byCode.C.quantity, 1);
  assert.strictEqual(applied.items.length, 3);

  assert.strictEqual(accountItems[0].quantity, 3, 'سلة الحساب الأصلية لا تُمس في الذاكرة');

  const keepAccount = merge.applyPlan(plan, { B: 'keep_account' }, accountItems);
  assert.strictEqual(keepAccount.ok, true);
  assert.strictEqual(keepAccount.items.find((it) => it.item_code === 'B').uom, 'Box');

  const keepQty = merge.applyPlan(plan, { A: 'keep_account', B: 'keep_account' }, accountItems);
  assert.strictEqual(keepQty.items.find((it) => it.item_code === 'A').quantity, 3);
  const useGuestQty = merge.applyPlan(plan, { A: 'use_guest', B: 'keep_account' }, accountItems);
  assert.strictEqual(useGuestQty.items.find((it) => it.item_code === 'A').quantity, 2);
}

// ---- سقف 200 سطر: يمنع بلا اقتطاع صامت ----

{
  const accountItems = [];
  for (let i = 0; i < 200; i += 1) {
    accountItems.push({ item_code: 'ACC' + i, quantity: 1, uom: 'Nos', price: 1 });
  }
  const guestLines = [{ item_code: 'NEW', quantity: 1, uom: 'Nos', price: 1 }];
  const plan = merge.buildPlan({ guestLines, accountItems, maxLines: 200 });
  assert.ok(plan.block, 'التجاوز يجب أن يوقف النقل');
  assert.strictEqual(plan.block.type, 'line_cap');
  assert.strictEqual(plan.block.projected, 201);
  const rejected = merge.applyPlan(plan, {}, accountItems);
  assert.strictEqual(rejected.ok, false);
  assert.strictEqual(rejected.error, 'line_cap');
  assert.strictEqual(accountItems.length, 200, 'لا اقتطاع ولا كتابة عند الرفض');
}

// ---- الملكية على جهاز مشترك ----

{
  const storage = fakeStorage();
  seed(storage, GUEST, [{ item_code: 'A', quantity: 1, uom: 'Nos', price: 10 }]);

  assert.strictEqual(merge.hasPendingGuestCart(storage, 'a@x.com'), true);
  assert.strictEqual(merge.bindOwner(storage, 'a@x.com'), true);
  assert.strictEqual(merge.readOwner(storage), 'a@x.com');
  assert.strictEqual(merge.bindOwner(storage, 'b@x.com'), false, 'حساب ثانٍ لا يملك سلة الزائر');
  assert.strictEqual(merge.ownerAllows(storage, 'b@x.com'), false);
  assert.strictEqual(merge.hasPendingGuestCart(storage, 'b@x.com'), false);
  assert.strictEqual(merge.hasPendingGuestCart(storage, 'a@x.com'), true);
  assert.strictEqual(merge.bindOwner(storage, 'A@X.com'), true, 'نفس الحساب بحالة أحرف مختلفة');
}

// ---- المسح: فقط بعد نجاح النقل، ويمسح حالة checkout الخاصة بالزائر ----

{
  const storage = fakeStorage();
  seed(storage, GUEST, [{ item_code: 'A', quantity: 1, uom: 'Nos', price: 10 }]);
  storage.setItem('biozone_checkout_pending:' + GUEST, '{"attempts":[]}');
  storage.setItem('biozone_cart_instance:' + GUEST, 'abcdefabcdefabcdef');
  storage.setItem('biozone_checkout_lock:' + GUEST, '{}');
  merge.bindOwner(storage, 'a@x.com');
  seed(storage, ACCOUNT, [{ item_code: 'Z', quantity: 1, uom: 'Nos', price: 3 }]);

  assert.strictEqual(merge.clearGuestState(storage), true);
  assert.strictEqual(storage.getItem(GUEST), null);
  assert.strictEqual(storage.getItem('biozone_checkout_pending:' + GUEST), null);
  assert.strictEqual(storage.getItem('biozone_cart_instance:' + GUEST), null);
  assert.strictEqual(storage.getItem('biozone_checkout_lock:' + GUEST), null);
  assert.strictEqual(storage.getItem(merge.OWNER_KEY), null);
  assert.deepStrictEqual(read(storage, ACCOUNT), [{ item_code: 'Z', quantity: 1, uom: 'Nos', price: 3 }], 'سلة الحساب لا تُمسح');
  assert.strictEqual(merge.hasPendingGuestCart(storage, 'a@x.com'), false);
}

// ---- علامة "استُهلكت": تمنع جمع الكميات مرتين لو فشل المسح ----

{
  const storage = fakeStorage();
  seed(storage, GUEST, [{ item_code: 'A', quantity: 2, uom: 'Nos', price: 10 }]);
  merge.bindOwner(storage, 'a@x.com');
  assert.strictEqual(merge.markConsumed(storage, 'a@x.com'), true);
  assert.strictEqual(merge.hasPendingGuestCart(storage, 'a@x.com'), false, 'لا نقلة ثانية بعد نجاح النقل');
  assert.strictEqual(merge.hasPendingGuestCart(storage, 'b@x.com'), false, 'ولا لحساب آخر');
  assert.strictEqual(merge.clearGuestState(storage), true);
  assert.strictEqual(storage.getItem(merge.GUEST_KEY), null);
}

// ---- التدفق الكامل: زائر يضيف → دخول → معاينة → موافقة → كتابة → مسح ----

{
  const storage = fakeStorage();
  seed(storage, GUEST, [
    { item_code: 'A', item_name: 'صنف أ', quantity: 1, uom: 'Nos', price: 111 },
    { item_code: 'B', item_name: 'صنف ب', quantity: 2, uom: 'Box', price: 222 },
  ]);
  seed(storage, ACCOUNT, [{ item_code: 'A', item_name: 'صنف أ', quantity: 2, uom: 'Nos', price: 111 }]);
  const email = 'customer@example.com';

  const prices = {
    A: { price: 12.5, display_uom: 'Nos' },
    B: { price: 30, display_uom: 'Box', thumbnail: '/b.png' },
  };
  const priced = merge.priceLines(merge.readItems(storage, GUEST), prices);
  assert.strictEqual(priced.lines.length, 2);
  assert.strictEqual(priced.lines[0].price, 12.5);

  const plan = merge.buildPlan({
    guestLines: priced.lines,
    unavailable: priced.unavailable,
    invalid: priced.invalid,
    accountItems: merge.readItems(storage, ACCOUNT),
  });
  assert.strictEqual(plan.stats.additions, 1);
  assert.strictEqual(plan.stats.matches, 1);

  const applied = merge.applyPlan(plan, {}, merge.readItems(storage, ACCOUNT));
  assert.strictEqual(applied.ok, true);
  assert.strictEqual(merge.bindOwner(storage, email), true);
  storage.setItem(ACCOUNT, JSON.stringify(applied.items));
  assert.strictEqual(merge.markConsumed(storage, email), true);
  assert.strictEqual(merge.clearGuestState(storage), true);

  const cart = read(storage, ACCOUNT);
  assert.strictEqual(cart.length, 2);
  assert.strictEqual(cart.find((it) => it.item_code === 'A').quantity, 3);
  assert.strictEqual(cart.find((it) => it.item_code === 'B').price, 30);
  assert.strictEqual(storage.getItem(GUEST), null);
  assert.strictEqual(merge.totalOf(cart), 12.5 * 3 + 30 * 2);
}

// ---- التزامن: لا تطبيق لمعاينة قديمة، ولا دمج مزدوج ----

const guard = require('../public/js/cart_checkout_guard.js');

function accountStorage(guestItems, accountItems, email) {
  const storage = fakeStorage();
  seed(storage, GUEST, guestItems);
  seed(storage, ACCOUNT, accountItems);
  merge.bindOwner(storage, email);
  return storage;
}

function expectedOf(storage) {
  return {
    guest: merge.signature(merge.readItems(storage, GUEST)),
    account: merge.signature(merge.readItems(storage, ACCOUNT)),
    email: 'customer@example.com',
  };
}

function mergeOnce(storage, buildItems) {
  return merge.commitMerge({
    storage,
    guestKey: GUEST,
    accountKey: ACCOUNT,
    expected: expectedOf(storage),
    buildItems,
  });
}

(async () => {
  // (1) المسار السعيد
  {
    const storage = accountStorage(
      [{ item_code: 'A', quantity: 2, uom: 'Nos', price: 10 }],
      [{ item_code: 'A', quantity: 1, uom: 'Nos', price: 10 }],
      'customer@example.com'
    );
    const expected = expectedOf(storage);
    const result = await merge.commitMerge({
      storage,
      accountKey: ACCOUNT,
      lock: (cb) => Promise.resolve(cb()),
      expected,
      buildItems: () => merge.applyPlan(
        merge.buildPlan({
          guestLines: [{ item_code: 'A', quantity: 2, uom: 'Nos', price: 10 }],
          accountItems: merge.readItems(storage, ACCOUNT),
        }),
        {},
        merge.readItems(storage, ACCOUNT)
      ),
    });
    assert.strictEqual(result.ok, true);
    assert.strictEqual(result.recovered, false);
    assert.strictEqual(read(storage, ACCOUNT)[0].quantity, 3);
    assert.strictEqual(storage.getItem(GUEST), null);
    assert.strictEqual(storage.getItem(merge.INTENT_KEY), null);
  }

  // (2) تبويب ثانٍ غيّر سلة الحساب أثناء فتح المعاينة
  {
    const storage = accountStorage(
      [{ item_code: 'A', quantity: 2, uom: 'Nos', price: 10 }],
      [{ item_code: 'A', quantity: 1, uom: 'Nos', price: 10 }],
      'customer@example.com'
    );
    const expected = expectedOf(storage);
    // تبويب آخر أضاف صنفًا للحساب بعد فتح المودال
    seed(storage, ACCOUNT, [
      { item_code: 'A', quantity: 1, uom: 'Nos', price: 10 },
      { item_code: 'NEW', quantity: 4, uom: 'Nos', price: 7 },
    ]);
    const result = await merge.commitMerge({
      storage,
      accountKey: ACCOUNT,
      lock: (cb) => Promise.resolve(cb()),
      expected,
      buildItems: () => ({ ok: true, items: [{ item_code: 'A', quantity: 3, uom: 'Nos' }] }),
    });
    assert.strictEqual(result.ok, false);
    assert.strictEqual(result.reason, 'changed');
    assert.strictEqual(read(storage, ACCOUNT).length, 2, 'لا كتابة على سلة الحساب');
    assert.deepStrictEqual(read(storage, ACCOUNT)[1], { item_code: 'NEW', quantity: 4, uom: 'Nos', price: 7 });
    assert.strictEqual(read(storage, GUEST).length, 1, 'لا مسح لسلة الزائر');
    assert.strictEqual(storage.getItem(merge.INTENT_KEY), null, 'لا نيّة معلّقة بعد الرفض');
  }

  // (3) تبويب ثانٍ غيّر سلة الزائر أثناء فتح المعاينة
  {
    const storage = accountStorage(
      [{ item_code: 'A', quantity: 2, uom: 'Nos', price: 10 }],
      [],
      'customer@example.com'
    );
    const expected = expectedOf(storage);
    seed(storage, GUEST, [
      { item_code: 'A', quantity: 5, uom: 'Nos', price: 10 },
      { item_code: 'Z', quantity: 1, uom: 'Nos', price: 2 },
    ]);
    const result = await merge.commitMerge({
      storage,
      accountKey: ACCOUNT,
      lock: (cb) => Promise.resolve(cb()),
      expected,
      buildItems: () => ({ ok: true, items: [{ item_code: 'A', quantity: 2, uom: 'Nos' }] }),
    });
    assert.strictEqual(result.ok, false);
    assert.strictEqual(result.reason, 'changed');
    assert.strictEqual(read(storage, ACCOUNT).length, 0);
    assert.strictEqual(read(storage, GUEST).length, 2, 'سلة الزائر كما هي بلا مسح');
  }

  // (4) محاولتا نقل متزامنتان بقفل حقيقي: دمج واحد فقط بلا جمع مزدوج
  {
    const storage = accountStorage(
      [{ item_code: 'A', quantity: 2, uom: 'Nos', price: 10 }],
      [{ item_code: 'A', quantity: 1, uom: 'Nos', price: 10 }],
      'customer@example.com'
    );
    const expected = expectedOf(storage);

    const runOnce = () => merge.commitMerge({
      storage,
      accountKey: ACCOUNT,
      expected,
      buildItems: () => ({ ok: true, items: [{ item_code: 'A', quantity: 3, uom: 'Nos', price: 10 }] }),
      lock: (callback) => guard.withCheckoutLock(ACCOUNT + ':merge', callback, {
        storage,
        timeoutMs: 3000,
        leaseMs: 5000,
      }),
    });

    const results = await Promise.all([runOnce(), runOnce()]);
    const okCount = results.filter((r) => r.ok).length;
    assert.strictEqual(okCount, 1, 'نقلة واحدة ناجحة فقط');
    assert.strictEqual(results.filter((r) => r.reason === 'changed').length, 1, 'الثانية تُرفض كمعاينة قديمة');
    assert.strictEqual(read(storage, ACCOUNT)[0].quantity, 3, 'لا جمع مزدوج للكميات');
    assert.strictEqual(storage.getItem(GUEST), null);
    assert.strictEqual(storage.getItem(merge.INTENT_KEY), null);
  }

  // (5) انقطاع الصفحة بعد كتابة سلة الحساب وقبل المسح: لا دمج ثانٍ
  {
    const storage = accountStorage(
      [{ item_code: 'A', quantity: 2, uom: 'Nos', price: 10 }],
      [{ item_code: 'A', quantity: 1, uom: 'Nos', price: 10 }],
      'customer@example.com'
    );
    const expected = expectedOf(storage);
    const merged = [{ item_code: 'A', quantity: 3, uom: 'Nos', price: 10 }];
    // حالة "ما بعد الكتابة ثم انقطاع": الحساب مكتوب + النيّة موجودة + الزائر لم يُمسح
    seed(storage, ACCOUNT, merged);
    storage.setItem(merge.INTENT_KEY, JSON.stringify({
      email: 'customer@example.com',
      guestSig: expected.guest,
      accountBefore: expected.account,
      accountAfter: merge.signature(merged),
      at: new Date().toISOString(),
    }));

    const result = await merge.commitMerge({
      storage,
      accountKey: ACCOUNT,
      lock: (cb) => Promise.resolve(cb()),
      expected,
      buildItems: () => {
        throw new Error('يجب ألا يُعاد الدمج');
      },
    });
    assert.strictEqual(result.ok, true);
    assert.strictEqual(result.recovered, true, 'استكمال تنظيف فقط');
    assert.strictEqual(read(storage, ACCOUNT)[0].quantity, 3, 'لا جمع مزدوج بعد إعادة الفتح');
    assert.strictEqual(storage.getItem(GUEST), null);
    assert.strictEqual(storage.getItem(merge.INTENT_KEY), null);
    assert.strictEqual(merge.hasPendingGuestCart(storage, 'customer@example.com'), false);
  }

  // (6) نيّة معلّقة لكن سلة الحساب لم تُكتب (انقطاع أبكر): تُهمل ويُكمل النقل مرة واحدة
  {
    const storage = accountStorage(
      [{ item_code: 'A', quantity: 1, uom: 'Nos', price: 10 }],
      [],
      'customer@example.com'
    );
    const expected = expectedOf(storage);
    storage.setItem(merge.INTENT_KEY, JSON.stringify({
      email: 'customer@example.com',
      guestSig: expected.guest,
      accountBefore: expected.account,
      accountAfter: 'stale-signature',
      at: new Date().toISOString(),
    }));
    const result = await merge.commitMerge({
      storage,
      accountKey: ACCOUNT,
      lock: (cb) => Promise.resolve(cb()),
      expected,
      buildItems: () => ({ ok: true, items: [{ item_code: 'A', quantity: 1, uom: 'Nos', price: 10 }] }),
    });
    assert.strictEqual(result.ok, true);
    assert.strictEqual(result.recovered, false);
    assert.strictEqual(read(storage, ACCOUNT).length, 1);
    assert.strictEqual(storage.getItem(merge.INTENT_KEY), null);
  }

  // (7) نيّة قديمة لكن سلة الزائر تغيّرت (سلة جديدة): لا استكمال ولا مسح
  {
    const storage = accountStorage(
      [{ item_code: 'A', quantity: 1, uom: 'Nos', price: 10 }],
      [],
      'customer@example.com'
    );
    const expected = expectedOf(storage);
    storage.setItem(merge.INTENT_KEY, JSON.stringify({
      email: 'customer@example.com',
      guestSig: expected.guest,
      accountBefore: expected.account,
      accountAfter: merge.signature([{ item_code: 'A', quantity: 9, uom: 'Nos' }]),
      at: new Date().toISOString(),
    }));
    // سلة زائر جديدة مختلفة عن لقطة النيّة
    seed(storage, GUEST, [{ item_code: 'B', quantity: 2, uom: 'Nos', price: 3 }]);
    const result = await merge.commitMerge({
      storage,
      accountKey: ACCOUNT,
      lock: (cb) => Promise.resolve(cb()),
      expected,
      buildItems: () => ({ ok: true, items: [{ item_code: 'B', quantity: 2, uom: 'Nos' }] }),
    });
    assert.strictEqual(result.recovered, undefined, 'لا استكمال لنقل قديم على سلة جديدة');
    assert.strictEqual(result.ok, false);
    assert.strictEqual(result.reason, 'changed');
    assert.deepStrictEqual(
      read(storage, GUEST),
      [{ item_code: 'B', quantity: 2, uom: 'Nos', price: 3 }],
      'السلة الجديدة لا تُمسح'
    );
    assert.strictEqual(read(storage, ACCOUNT).length, 0);
  }
  // (8) fail-closed: بلا Web Locks / بلا قفل → لا كتابة ولا مسح
  {
    const storage = accountStorage(
      [{ item_code: 'A', quantity: 2, uom: 'Nos', price: 10 }],
      [{ item_code: 'A', quantity: 1, uom: 'Nos', price: 10 }],
      'customer@example.com'
    );
    const expected = expectedOf(storage);
    const result = await merge.commitMerge({
      storage,
      accountKey: ACCOUNT,
      expected,
      buildItems: () => ({ ok: true, items: [{ item_code: 'A', quantity: 3, uom: 'Nos' }] }),
    });
    assert.strictEqual(result.ok, false);
    assert.strictEqual(result.reason, 'no_lock');
    assert.strictEqual(read(storage, ACCOUNT)[0].quantity, 1, 'سلة الحساب لم تُكتب');
    assert.strictEqual(read(storage, GUEST)[0].quantity, 2, 'سلة الزائر لم تُمسح');
    assert.strictEqual(storage.getItem(merge.INTENT_KEY), null);
    assert.strictEqual(merge.hasPendingGuestCart(storage, 'customer@example.com'), true);
  }

  // (9) كشف قدرة Web Locks + الحالة الأولية للواجهة (قبل أي شبكة)
  {
    assert.strictEqual(merge.hasWebLocks({}), false, 'no locks');
    assert.strictEqual(merge.hasWebLocks({ locks: {} }), false, 'locks بلا request');
    assert.strictEqual(merge.hasWebLocks({ locks: { request: () => {} } }), true);
    assert.strictEqual(merge.hasWebLocks({ locks: { request: null } }), false);

    const off = merge.mergeUiState({});
    assert.strictEqual(off.available, false);
    assert.strictEqual(off.buttonDisabled, true);
    assert.match(off.message, /لا يدعم قفل التبويبات الآمن/);
    assert.match(off.message, /سلة الزائر محفوظة كما هي/);

    const on = merge.mergeUiState({ locks: { request: () => {} } });
    assert.strictEqual(on.available, true);
    assert.strictEqual(on.buttonDisabled, false);
    assert.strictEqual(on.message, '');

    const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
    Object.defineProperty(globalThis, 'navigator', { value: undefined, configurable: true });
    assert.strictEqual(merge.hasWebLocks(), false, 'بدون navigator = لا قفل');
    assert.strictEqual(merge.mergeUiState().buttonDisabled, true, 'الحالة الافتراضية بلا navigator = معطّل');
    if (originalNavigator) Object.defineProperty(globalThis, 'navigator', originalNavigator);
  }
})()
  .then(() => {
    console.log('guest_cart_merge: concurrency + recovery assertions passed');
  })
  .catch((err) => {
    console.error(err);
    process.exitCode = 1;
  });
