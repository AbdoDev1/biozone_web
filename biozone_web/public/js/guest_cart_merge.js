(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.BzGuestCart = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  // نفس مفتاح سلة الحساب الموجود في site_header.html — الزائر يستخدم
  // لاحقة ثابتة داخل نفس المساحة، فلا DocType ولا سلة خادم.
  var ACCOUNT_PREFIX = 'biozone_cart_items:';
  var GUEST_SUFFIX = '__guest__';
  var GUEST_KEY = ACCOUNT_PREFIX + GUEST_SUFFIX;
  // ربط سلة الزائر بأول حساب دخل بعد إنشائها: يمنع نقلها لحساب آخر
  // على جهاز مشترك. لا يُحذف إلا بعد نجاح النقل.
  var OWNER_KEY = 'biozone_cart_guest_owner';
  // علامة أمان: النقل تم لكن تعذر مسح سلة الزائر (رفض تخزين) — تمنع
  // نقلة ثانية تجمع الكميات مرتين.
  var CONSUMED_KEY = 'biozone_cart_guest_consumed';
  // سجل نيّة النقل: يُكتب قبل كتابة سلة الحساب ويُحذف بعد اكتمال المسح.
  // وجوده بعد انقطاع الصفحة يعني أن الكتابة قد تكون تمت — فيُكمل التنظيف
  // بلا دمج ثانٍ (idempotent)، أو يُهمَل إن لم تحدث الكتابة.
  var INTENT_KEY = 'biozone_cart_guest_merge_intent';
  var MAX_LINES = 200;
  // في مودال النقل نعرض أول 5 صفوف فقط (تكسر الشاشة عند العدد الكبير)؛
  // صفوف تعارض وحدة البيع تُعرض دائمًا لأنها تحتاج قرارًا صريحًا.
  var MERGE_PREVIEW_LIMIT = 5;
  var GUEST_STATE_PREFIXES = [
    'biozone_checkout_pending:',
    'biozone_cart_instance:',
    'biozone_checkout_lock:'
  ];

  function accountKey(user) {
    return ACCOUNT_PREFIX + String(user || '').trim().toLowerCase();
  }

  // قدرة القفل الحقيقي بين التبويبات. النقل لا يُنفَّذ بدونها (fail-closed):
  // مسار الـlease في localStorage ليس قفلًا متبادلًا موثوقًا.
  function hasWebLocks(nav) {
    var target = nav || (typeof navigator !== 'undefined' ? navigator : null);
    return !!(target && target.locks && typeof target.locks.request === 'function');
  }

  var MERGE_LOCK_MESSAGE =
    'متصفحك لا يدعم قفل التبويبات الآمن (Web Locks) المطلوب لنقل السلة بين التبويبات. '
    + 'سلة الزائر محفوظة كما هي ولم يُنقل شيء ولم يُمسح شيء — يمكنك متابعة استخدام السلة وتسجيل الدخول كالمعتاد.';

  // الحالة الأولية للواجهة (قبل أي شبكة أو معاينة): عند غياب الدعم يظهر
  // زر النقل معطّلًا مع الرسالة فورًا، لا بعد محاولة فاشلة.
  function mergeUiState(nav) {
    var available = hasWebLocks(nav);
    return {
      available: available,
      buttonDisabled: !available,
      message: available ? '' : MERGE_LOCK_MESSAGE
    };
  }

  function normalizeEmail(value) {
    return String(value || '').trim().toLowerCase();
  }

  function readJson(storage, key) {
    try {
      var raw = storage && storage.getItem(key);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  }

  function writeJson(storage, key, value) {
    try {
      if (!storage) return false;
      var serialized = JSON.stringify(value);
      storage.setItem(key, serialized);
      return storage.getItem(key) === serialized;
    } catch (e) {
      return false;
    }
  }

  function readItems(storage, key) {
    var parsed = readJson(storage, key);
    return Array.isArray(parsed) ? parsed : [];
  }

  function readOwner(storage) {
    try {
      return normalizeEmail(storage && storage.getItem(OWNER_KEY));
    } catch (e) {
      return '';
    }
  }

  // يربط السلة بأول حساب يراها. حساب مختلف لا يستطيع إعادة الربط.
  function bindOwner(storage, email) {
    var who = normalizeEmail(email);
    if (!who) return false;
    var current = readOwner(storage);
    if (current && current !== who) return false;
    if (current === who) return true;
    try {
      storage.setItem(OWNER_KEY, who);
      return readOwner(storage) === who;
    } catch (e) {
      return false;
    }
  }

  function ownerAllows(storage, email) {
    var who = normalizeEmail(email);
    var owner = readOwner(storage);
    return !owner || (!!who && owner === who);
  }

  function isConsumed(storage, email) {
    try {
      var raw = storage && storage.getItem(CONSUMED_KEY);
      if (!raw) return false;
      var parsed = JSON.parse(raw);
      return !!parsed && normalizeEmail(parsed.email) === normalizeEmail(email);
    } catch (e) {
      return false;
    }
  }

  function markConsumed(storage, email) {
    return writeJson(storage, CONSUMED_KEY, {
      email: normalizeEmail(email),
      at: new Date().toISOString()
    });
  }

  // هل توجد سلة زائر جاهزة للنقل لهذا الحساب؟ (فارغة/محسومة/مملوكة
  // لحساب آخر = لا). لا يُحذف أي شيء هنا.
  function hasPendingGuestCart(storage, email) {
    if (!readItems(storage, GUEST_KEY).length) return false;
    if (isConsumed(storage, email)) return false;
    return ownerAllows(storage, email);
  }

  // مسح بيانات الزائر وحالة checkout الخاصة بها — بعد نجاح النقل فقط.
  function clearGuestState(storage) {
    var ok = true;
    try {
      storage.removeItem(GUEST_KEY);
      if (storage.getItem(GUEST_KEY) !== null) ok = false;
    } catch (e) {
      ok = false;
    }
    GUEST_STATE_PREFIXES.forEach(function (prefix) {
      try {
        storage.removeItem(prefix + GUEST_KEY);
      } catch (e) {
        ok = false;
      }
    });
    [OWNER_KEY, CONSUMED_KEY].forEach(function (key) {
      try {
        storage.removeItem(key);
      } catch (e) {
        ok = false;
      }
    });
    return ok;
  }

  // بصمة هوية السلة (صنف/كمية/وحدة) — نفس دلالة BzCartCheckout.signature.
  function signature(items) {
    return JSON.stringify((items || []).map(function (it) {
      return [
        (it && it.item_code) || '',
        Number((it && it.quantity) || 0),
        (it && it.uom) || ''
      ];
    }));
  }

  function readIntent(storage) {
    return readJson(storage, INTENT_KEY) || null;
  }

  function clearIntent(storage) {
    try {
      storage.removeItem(INTENT_KEY);
    } catch (e) {}
  }

  function writeItems(storage, key, items) {
    if (!writeJson(storage, key, items)) return false;
    return signature(readItems(storage, key)) === signature(items);
  }

  // يكمل تنظيف نقلة كُتبت ثم انقطعت الصفحة قبل المسح (بلا دمج ثانٍ).
  // يشترط تطابق الحالة الحالية مع النيّة (سلة الحساب = النتيجة، وسلة
  // الزائر = نفس اللقطة) — وإلا فلا استكمال ولا مسح. يُعيد null عند عدم
  // التطابق.
  function resumeMerge(storage, guestKey, accountKey, email) {
    var intent = readIntent(storage);
    if (!intent) return null;
    if (normalizeEmail(intent.email) !== normalizeEmail(email)) return null;
    if (signature(readItems(storage, accountKey)) !== intent.accountAfter) return null;
    if (signature(readItems(storage, guestKey)) !== intent.guestSig) return null;
    var cleared = clearGuestState(storage);
    clearIntent(storage);
    return { recovered: true, cleared: cleared };
  }

  // التنفيذ الآمن للنقل: قفل بين التبويبات (إن توفّر) + إعادة قراءة
  // المفتاحين ومقارنة البصمة **داخل القفل مباشرة قبل الكتابة**.
  // لا كتابة ولا مسح عند الاختلاف: تُعاد المعاينة بدل تطبيق لقطة قديمة.
  async function commitMerge(options) {
    var opts = options || {};
    var storage = opts.storage;
    var guestKey = opts.guestKey || GUEST_KEY;
    var accountKey = opts.accountKey;
    var expected = opts.expected || {};
    var buildItems = opts.buildItems;
    var lock = opts.lock;

    function run() {
      // (أ) نقلة سابقة انقطعت بعد كتابتها → أكمل التنظيف فقط.
      var resumed = resumeMerge(storage, guestKey, accountKey, expected.email);
      if (resumed) return { ok: true, recovered: true, cleared: resumed.cleared };

      // نيّة قديمة غير مطابقة (لم تُكتب سلة الحساب) → تُهمل.
      if (readIntent(storage)) clearIntent(storage);

      // (ب) الحالة الفعلية الآن — لا لقطة الفتح.
      var guestRaw = readItems(storage, guestKey);
      var accountRaw = readItems(storage, accountKey);
      if (!expected || signature(guestRaw) !== expected.guest
        || signature(accountRaw) !== expected.account) {
        return { ok: false, reason: 'changed' };
      }

      var built = buildItems ? buildItems() : null;
      if (!built || !built.ok || !Array.isArray(built.items)) {
        return { ok: false, reason: (built && built.error) || 'invalid' };
      }

      var accountAfter = signature(built.items);
      // (ج) نيّة قبل الكتابة: تسمح بإكمال التنظيف بعد انقطاع بلا دمج ثانٍ.
      if (!writeJson(storage, INTENT_KEY, {
        email: normalizeEmail(expected.email),
        guestSig: expected.guest,
        accountBefore: expected.account,
        accountAfter: accountAfter,
        at: new Date().toISOString()
      })) {
        return { ok: false, reason: 'storage' };
      }
      if (!writeItems(storage, accountKey, built.items)) {
        clearIntent(storage);
        return { ok: false, reason: 'storage' };
      }
      markConsumed(storage, expected.email);
      var cleared = clearGuestState(storage);
      clearIntent(storage);
      return { ok: true, recovered: false, items: built.items, cleared: cleared };
    }

    // fail-closed: لا دمج بلا قفل حقيقي (Web Locks). مسار الـlease غير
    // مقبول للدمج لأنه لا يضمن الحصرية بين تبويبين.
    if (typeof lock !== 'function') return Promise.resolve({ ok: false, reason: 'no_lock' });
    return Promise.resolve().then(function () {
      return lock(run);
    });
  }

  // سلة مقسّمة داخل مودال النقل: صفحات صغيرة بدل رسم كل الصفوف (DOM ثابت).
  function paginate(items, page, pageSize) {
    var list = items || [];
    var size = Math.max(1, Number(pageSize) || MERGE_PREVIEW_LIMIT);
    var pageCount = Math.max(1, Math.ceil(list.length / size));
    var current = Math.min(Math.max(1, Number(page) || 1), pageCount);
    var start = (current - 1) * size;
    return {
      page: current,
      pageCount: pageCount,
      size: size,
      total: list.length,
      items: list.slice(start, start + size)
    };
  }

  function toQty(value) {    var qty = Number(value);
    if (!isFinite(qty) || qty <= 0) return 0;
    return Math.round(qty * 1000) / 1000;
  }

  function sameUom(a, b) {
    return String(a || '').trim().toLowerCase() === String(b || '').trim().toLowerCase();
  }

  // تسعير/تحقق سيرفر-سايد فقط: أي سطر بلا سعر أو بكمية غير صالحة لا
  // يُنقل، ويُعرض صراحة للمستخدم (لا إسقاط صامت).
  function priceLines(lines, prices) {
    var indexed = prices || {};
    var out = { lines: [], unavailable: [], invalid: [] };
    (lines || []).forEach(function (raw) {
      var code = raw && raw.item_code ? String(raw.item_code) : '';
      if (!code) return;
      var qty = toQty(raw.quantity);
      if (!qty) {
        out.invalid.push({
          item_code: code,
          item_name: raw.item_name || ''
        });
        return;
      }
      var price = indexed[code];
      if (!price || price.price === null || price.price === undefined) {
        out.unavailable.push({
          item_code: code,
          item_name: raw.item_name || ''
        });
        return;
      }
      out.lines.push({
        item_code: code,
        item_name: raw.item_name || '',
        item_group: raw.item_group || '',
        quantity: qty,
        uom: price.display_uom || raw.uom || '',
        price: Number(price.price),
        // سعر المتصفح القديم للعرض فقط: نُظهر الفرق، ولا نعتمده أبدًا.
        wasPrice: Number(raw.price || 0),
        image: price.thumbnail || raw.image || ''
      });
    });
    return out;
  }

  // خطة عرض فقط — لا تكتب شيئًا. كل قرار يتخذه المستخدم صراحةً.
  function buildPlan(input) {
    var options = input || {};
    var guestLines = options.guestLines || [];
    var accountItems = options.accountItems || [];
    var accountUnavailable = options.accountUnavailable || [];
    var unavailable = options.unavailable || [];
    var invalid = options.invalid || [];
    var maxLines = Number(options.maxLines) > 0 ? Number(options.maxLines) : MAX_LINES;

    var rows = [];
    var additions = 0;
    var matches = 0;
    var conflicts = 0;

    guestLines.forEach(function (line) {
      var existing = null;
      for (var i = 0; i < accountItems.length; i += 1) {
        if (accountItems[i] && accountItems[i].item_code === line.item_code) {
          existing = accountItems[i];
          break;
        }
      }
      if (!existing) {
        additions += 1;
        rows.push({
          kind: 'add',
          action: 'add',
          item_code: line.item_code,
          item_name: line.item_name,
          quantity: line.quantity,
          uom: line.uom,
          price: line.price,
          wasPrice: Number(line.wasPrice || 0),
          image: line.image,
          item_group: line.item_group
        });
        return;
      }
      if (sameUom(existing.uom, line.uom)) {
        matches += 1;
        rows.push({
          kind: 'qty',
          action: 'sum',
          item_code: line.item_code,
          item_name: line.item_name || existing.item_name,
          uom: line.uom,
          accountQty: toQty(existing.quantity),
          guestQty: line.quantity,
          price: line.price,
          wasPrice: Number(line.wasPrice || 0),
          accountPrice: Number(existing.price || 0),
          image: line.image,
          item_group: line.item_group
        });
        return;
      }
      conflicts += 1;
      rows.push({
        kind: 'uom',
        action: '',
        item_code: line.item_code,
        item_name: line.item_name || existing.item_name,
        accountUom: existing.uom || '',
        guestUom: line.uom || '',
        accountQty: toQty(existing.quantity),
        guestQty: line.quantity,
        price: line.price,
        wasPrice: Number(line.wasPrice || 0),
        accountPrice: Number(existing.price || 0),
        image: line.image,
        item_group: line.item_group
      });
    });

    var projected = accountItems.length + additions;
    var block = null;
    if (projected > maxLines) {
      block = {
        type: 'line_cap',
        projected: projected,
        max: maxLines,
        overflow: projected - maxLines
      };
    }

    return {
      rows: rows,
      unavailable: unavailable,
      invalid: invalid,
      accountUnavailable: accountUnavailable,
      stats: {
        guestCount: guestLines.length + unavailable.length + invalid.length,
        accountCount: accountItems.length,
        additions: additions,
        matches: matches,
        conflicts: conflicts,
        unavailableCount: unavailable.length + invalid.length,
        projected: projected,
        maxLines: maxLines
      },
      block: block
    };
  }

  function copyLine(line) {
    return {
      item_code: line.item_code,
      item_name: line.item_name || '',
      item_group: line.item_group || '',
      price: Number(line.price || 0),
      image: line.image || '',
      quantity: toQty(line.quantity),
      uom: line.uom || ''
    };
  }

  // تطبيق الخطة على سلة الحساب (في الذاكرة فقط — الكتابة مسؤولية المستدعي).
  // يفشل بلا تغيير إذا: تجاوز السقف، أو قرار وحدة بيع غير محسوم.
  function applyPlan(plan, decisions, accountItems) {
    if (!plan) return { ok: false, error: 'no_plan' };
    if (plan.block) return { ok: false, error: 'line_cap', block: plan.block };
    var chosen = decisions || {};
    var base = (accountItems || []).map(copyLine);
    var unresolved = [];

    (plan.rows || []).forEach(function (row) {
      if (row.kind === 'add') {
        base.push(copyLine(row));
        return;
      }
      var action = chosen[row.item_code] || row.action;
      var index = -1;
      for (var i = 0; i < base.length; i += 1) {
        if (base[i].item_code === row.item_code) {
          index = i;
          break;
        }
      }
      if (index === -1) return;

      // السعر/الصورة من الخادم دائمًا لأي سطر نُقل — حتى لو كانت
      // القيمة المخزنة في المتصفح قديمة أو معدّلة يدويًا.
      base[index].price = Number(row.price || 0);
      if (row.image) base[index].image = row.image;

      if (row.kind === 'qty') {
        if (action === 'keep_account') return;
        if (action === 'use_guest') {
          base[index].quantity = row.guestQty;
          return;
        }
        base[index].quantity = Math.round((base[index].quantity + row.guestQty) * 1000) / 1000;
        return;
      }

      // تعارض وحدة بيع: لا قرار افتراضي — المستخدم يختار.
      if (action !== 'keep_account' && action !== 'use_guest') {
        unresolved.push({
          item_code: row.item_code,
          item_name: row.item_name
        });
        return;
      }
      if (action === 'use_guest') {
        base[index] = copyLine(row);
        base[index].uom = row.guestUom;
        base[index].quantity = row.guestQty;
      }
    });

    if (unresolved.length) {
      return { ok: false, error: 'unresolved', unresolved: unresolved };
    }
    if (base.length > (plan.stats && plan.stats.maxLines ? plan.stats.maxLines : MAX_LINES)) {
      return {
        ok: false,
        error: 'line_cap',
        block: {
          type: 'line_cap',
          projected: base.length,
          max: plan.stats.maxLines || MAX_LINES,
          overflow: base.length - (plan.stats.maxLines || MAX_LINES)
        }
      };
    }
    return { ok: true, items: base };
  }

  function totalOf(items) {
    return (items || []).reduce(function (sum, it) {
      return sum + Number(it.price || 0) * Number(it.quantity || 0);
    }, 0);
  }

  function summarize(plan) {
    if (!plan || !plan.stats) {
      return { added: 0, merged: 0, conflicts: 0, excluded: 0, projected: 0 };
    }
    return {
      added: plan.stats.additions,
      merged: plan.stats.matches,
      conflicts: plan.stats.conflicts,
      excluded: plan.stats.unavailableCount,
      projected: plan.stats.projected
    };
  }

  return {
    GUEST_KEY: GUEST_KEY,
    OWNER_KEY: OWNER_KEY,
    INTENT_KEY: INTENT_KEY,
    MAX_LINES: MAX_LINES,
    MERGE_PREVIEW_LIMIT: MERGE_PREVIEW_LIMIT,
    accountKey: accountKey,
    hasWebLocks: hasWebLocks,
    mergeUiState: mergeUiState,
    readItems: readItems,
    readOwner: readOwner,
    bindOwner: bindOwner,
    ownerAllows: ownerAllows,
    isConsumed: isConsumed,
    markConsumed: markConsumed,
    hasPendingGuestCart: hasPendingGuestCart,
    clearGuestState: clearGuestState,
    signature: signature,
    commitMerge: commitMerge,
    priceLines: priceLines,
    buildPlan: buildPlan,
    applyPlan: applyPlan,
    paginate: paginate,
    totalOf: totalOf,
    summarize: summarize
  };
});
