(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.BzCartCheckout = api;
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  var LOCK_PREFIX = 'biozone_checkout_lock:';
  var CART_INSTANCE_PREFIX = 'biozone_cart_instance:';
  var CART_INSTANCE_RE = /^[A-Za-z0-9_-]{16,64}$/;

  // هوية دورة السلة: عشوائية آمنة، مشتركة بين تبويبات المتصفح، وتُدوَّر
  // بعد نجاح الطلب أو إفراغ السلة. ليست بديلًا عن checkout_token.
  function createCartInstance() {
    if (typeof window !== 'undefined' && window.crypto && window.crypto.getRandomValues) {
      var bytes = new Uint8Array(16);
      window.crypto.getRandomValues(bytes);
      return Array.prototype.map.call(bytes, function (b) {
        return ('0' + b.toString(16)).slice(-2);
      }).join('');
    }
    return 'ci' + randomId() + randomId();
  }

  function readCartInstance(storage, cartKey) {
    try {
      var value = storage && storage.getItem(CART_INSTANCE_PREFIX + cartKey);
      return value && CART_INSTANCE_RE.test(value) ? value : '';
    } catch (e) {
      return '';
    }
  }

  function ensureCartInstance(storage, cartKey, idFactory) {
    var existing = readCartInstance(storage, cartKey);
    if (existing) return existing;
    var created = (idFactory || createCartInstance)();
    try {
      storage.setItem(CART_INSTANCE_PREFIX + cartKey, created);
      return readCartInstance(storage, cartKey) === created ? created : '';
    } catch (e) {
      return '';
    }
  }

  // compare-and-rotate: إن دوّره تبويب آخر بالفعل (current != expected)
  // لا ندوّره مرة ثانية، فلا نُفقد ارتباط الطلب بدورته.
  function rotateCartInstance(storage, cartKey, expected, idFactory) {
    var current = readCartInstance(storage, cartKey);
    if (expected && current && current !== expected) return current;
    var created = (idFactory || createCartInstance)();
    try {
      storage.setItem(CART_INSTANCE_PREFIX + cartKey, created);
      return created;
    } catch (e) {
      return current;
    }
  }

  function signature(items) {
    return JSON.stringify((items || []).map(function (it) {
      return [it.item_code || '', Number(it.quantity || 0), it.uom || ''];
    }));
  }

  function randomId() {
    if (typeof window !== 'undefined' && window.crypto) {
      if (window.crypto.randomUUID) return window.crypto.randomUUID().replace(/-/g, '');
      if (window.crypto.getRandomValues) {
        var bytes = new Uint8Array(16);
        window.crypto.getRandomValues(bytes);
        return Array.prototype.map.call(bytes, function (b) {
          return ('0' + b.toString(16)).slice(-2);
        }).join('');
      }
    }
    return String(Date.now()) + Math.random().toString(16).slice(2, 18);
  }

  function createAttemptStore(storage, cartKey, idFactory) {
    var key = 'biozone_checkout_pending:' + cartKey;
    var volatileAttempt = null;
    var makeId = idFactory || randomId;
    var maxAttempts = 10;

    // CR-02: كل قراءة تعرف مصدرها. المحاولة المقروءة من التخزين ثابتة
    // (تنجو من reload)؛ المحاولة الموجودة في الذاكرة وحدها ليست ضمانًا.
    function readState() {
      var payload = null;
      var fromStorage = false;
      try {
        var raw = storage && storage.getItem(key);
        if (raw) {
          payload = JSON.parse(raw);
          fromStorage = true;
        }
      } catch (e) {}
      if (!payload && volatileAttempt) payload = volatileAttempt;
      var attempts = [];
      if (payload && Array.isArray(payload.attempts)) attempts = payload.attempts;
      // توافق مع الصيغة القديمة (محاولة واحدة) قبل أي نشر.
      else if (payload && payload.signature && payload.token) attempts = [payload];
      return { attempts: attempts, fromStorage: fromStorage };
    }

    // CR-02: ترجع true فقط إذا نُفِّذت الكتابة وتأكدت قراءتها. فشل
    // setItem (مساحة/سياسة متصفح) لم يعد يُبتلع بصمت.
    function writeAll(attempts) {
      var payload = { attempts: attempts.slice(0, maxAttempts) };
      volatileAttempt = payload;
      try {
        if (!storage) return false;
        var serialized = JSON.stringify(payload);
        storage.setItem(key, serialized);
        return storage.getItem(key) === serialized;
      } catch (e) {
        return false;
      }
    }

    function split(attempts, expected) {
      var found = null;
      var rest = [];
      attempts.forEach(function (attempt) {
        if (!found && attempt && attempt.signature === expected && attempt.token) {
          found = attempt;
        } else {
          rest.push(attempt);
        }
      });
      return { found: found, rest: rest };
    }

    function getAttempt(items, fingerprint) {
      var expected = signature(items);
      var state = readState();
      var parts = split(state.attempts, expected);
      if (parts.found) {
        // نفس لقطة السلة تحتفظ بنفس هوية التأكيد حتى لو تغيّر السعر —
        // الخادم يقرر: replay لطلب ناجح، أو إعادة استخدام سجل فاشل.
        // الاحتفاظ بعدة لقطات يمنع أيضًا طلبًا ثانيًا إذا عدّل العميل
        // السلة ثم أعادها لحالتها الأولى بعد ضياع الرد.
        parts.found.fingerprint = fingerprint;
        var refreshed = writeAll([parts.found].concat(parts.rest));
        return {
          token: parts.found.token,
          // الهوية المقروءة من التخزين ثابتة أصلًا؛ فشل تحديث البصمة
          // التجميلي لا يُفقد ضمان الـreplay.
          persisted: state.fromStorage || refreshed,
          reused: true,
          signature: expected
        };
      }
      var attempt = { signature: expected, fingerprint: fingerprint, token: makeId() };
      return {
        token: attempt.token,
        persisted: writeAll([attempt].concat(state.attempts)),
        reused: false,
        signature: expected
      };
    }

    function get(items, fingerprint) {
      return getAttempt(items, fingerprint).token;
    }

    // CR-03: حذف المحاولة المنتهية وحدها — لا clear() شامل يمحو
    // محاولات معلقة لتكوينات سلة أخرى من تبويب آخر.
    function clearAttempt(token) {
      var state = readState();
      var rest = state.attempts.filter(function (attempt) {
        return !attempt || attempt.token !== token;
      });
      if (rest.length === state.attempts.length) return true;
      if (rest.length) return writeAll(rest);
      volatileAttempt = null;
      try {
        if (!storage) return false;
        storage.removeItem(key);
        return storage.getItem(key) === null;
      } catch (e) {
        return false;
      }
    }

    function clear() {
      volatileAttempt = null;
      try {
        if (!storage) return false;
        storage.removeItem(key);
        return storage.getItem(key) === null;
      } catch (e) {
        return false;
      }
    }

    return { get: get, getAttempt: getAttempt, clear: clear, clearAttempt: clearAttempt };
  }

  // حالة زر التأكيد: نص/أيقونة لكل مرحلة مع إبقاء الزر معطّلًا أثناء العمل.
  var CONFIRM_PHASES = {
    idle: { text: 'تأكيد الطلب', icon: 'check_circle', busy: false },
    verifying: { text: 'جارٍ التحقق من الأسعار...', icon: 'progress_activity', busy: true },
    submitting: { text: 'جارٍ تأكيد الطلب...', icon: 'progress_activity', busy: true }
  };

  function confirmButtonContent(phase) {
    return CONFIRM_PHASES[phase] || CONFIRM_PHASES.idle;
  }

  // تعارض دورة السلة (409): قرار عرض فقط — نعم للتدوير والرابط،
  // لا لمسح سلة أحدث، ولا إعادة إرسال تلقائي.
  function planInstanceConflict(message) {
    var payload = message || {};
    return {
      rotate: true,
      keepCart: true,
      resend: false,
      redirect: typeof payload.redirect === 'string' ? payload.redirect : '',
      message:
        'تم إنشاء طلب من هذه السلة بالفعل، ولم يُنشأ طلب ثانٍ. ' +
        'راجع السلة، وإن أردت طلبًا جديدًا فاضغط «تأكيد الطلب» مجددًا.'
    };
  }

  // CR-01/4: طلب HTTP بمهلة صريحة — لا انتظار بلا نهاية إن علِق الخادم.
  // ملاحظة: عند الإلغاء لا نعرف هل وصل الطلب؛ الـtoken المحفوظ يجعل
  // إعادة المحاولة replay لا طلبًا ثانيًا.
  function fetchWithTimeout(fetchImpl, url, options, timeoutMs) {
    var opts = options || {};
    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = null;
    if (controller) {
      opts = Object.assign({}, opts, { signal: controller.signal });
    }
    var request = Promise.resolve().then(function () { return fetchImpl(url, opts); });
    var timeout = new Promise(function (_resolve, reject) {
      timer = setTimeout(function () {
        if (controller) {
          try { controller.abort(); } catch (e) {}
        }
        var err = new Error('request timeout');
        err.code = 'REQUEST_TIMEOUT';
        reject(err);
      }, timeoutMs);
    });
    return Promise.race([request, timeout]).finally(function () {
      if (timer) clearTimeout(timer);
    });
  }

  function createRevision() {
    var current = 0;
    return {
      read: function () { return current; },
      bump: function () { current += 1; return current; },
      isCurrent: function (snapshot) { return current === snapshot; }
    };
  }

  // سلة مقسّمة (20/صفحة) + بحث محلي — دوال نقية قابلة للاختبار.
  function filterCartItems(items, query) {
    var needle = String(query || '').trim().toLowerCase();
    if (!needle) return (items || []).slice();
    return (items || []).filter(function (it) {
      var name = String((it && it.item_name) || '').toLowerCase();
      var code = String((it && it.item_code) || '').toLowerCase();
      return name.indexOf(needle) !== -1 || code.indexOf(needle) !== -1;
    });
  }

  function paginate(items, page, pageSize) {
    var list = items || [];
    var size = Math.max(1, Number(pageSize) || 20);
    var pageCount = Math.max(1, Math.ceil(list.length / size));
    var current = Math.min(Math.max(1, Number(page) || 1), pageCount);
    var start = (current - 1) * size;
    return {
      page: current,
      pageCount: pageCount,
      items: list.slice(start, start + size)
    };
  }

  function lockTimeoutError() {
    var err = new Error('checkout lock timeout');
    err.code = 'CHECKOUT_LOCK_TIMEOUT';
    return err;
  }

  function readLease(storage, key) {
    try {
      var raw = storage && storage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function acquireLease(storage, key, owner, ttl) {
    var current = readLease(storage, key);
    var stamp = Date.now();
    if (current && current.owner && current.owner !== owner && Number(current.expires) > stamp) {
      return false;
    }
    var lease = { owner: owner, expires: stamp + ttl };
    try {
      storage.setItem(key, JSON.stringify(lease));
    } catch (e) {
      return false;
    }
    var check = readLease(storage, key);
    return !!(check && check.owner === owner);
  }

  function releaseLease(storage, key, owner) {
    var current = readLease(storage, key);
    if (current && current.owner === owner) {
      try { storage.removeItem(key); } catch (e) {}
    }
  }

  // CR-01: قفل واحد لكل سلة/حساب. Web Locks هو المسار المفضّل (ذرّي عبر
  // التبويبات)؛ عند غيابه يُستخدم lease مخزّن له owner وانتهاء وتجديد.
  // الـlease ليس ذرّيًا بنفس قوة Web Locks — لذلك يُتحقق من الملكية بعد
  // الكتابة، ويُنتظر حتى مهلة محددة ثم يُرفض التأكيد (لا POST بلا قفل).
  function withCheckoutLock(cartKey, callback, options) {
    var opts = options || {};
    var storage = opts.storage || null;
    var timeoutMs = opts.timeoutMs === undefined ? 15000 : opts.timeoutMs;
    var leaseMs = opts.leaseMs === undefined ? 120000 : opts.leaseMs;
    var lockName = LOCK_PREFIX + cartKey;
    var nav = typeof navigator !== 'undefined' ? navigator : null;

    if (nav && nav.locks && typeof nav.locks.request === 'function') {
      var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
      var timer = null;
      var lockOptions = { mode: 'exclusive' };
      if (controller) {
        lockOptions.signal = controller.signal;
        timer = setTimeout(function () {
          try { controller.abort(); } catch (e) {}
        }, timeoutMs);
      }
      return Promise.resolve(
        nav.locks.request(lockName, lockOptions, function () {
          return callback({ mode: 'web-locks', locked: true });
        })
      )
        .catch(function (err) {
          if (err && (err.name === 'AbortError' || err.code === 20)) {
            throw lockTimeoutError();
          }
          throw err;
        })
        .finally(function () {
          if (timer) clearTimeout(timer);
        });
    }
    if (!storage) {
      return Promise.resolve(callback({ mode: 'none', locked: false }));
    }

    var owner = randomId();
    var deadline = Date.now() + timeoutMs;
    var renew = null;

    return new Promise(function (resolve, reject) {
      function stopRenewing() {
        if (renew) {
          clearInterval(renew);
          renew = null;
        }
      }
      function run() {
        if (!acquireLease(storage, lockName, owner, leaseMs)) {
          if (Date.now() >= deadline) {
            reject(lockTimeoutError());
            return;
          }
          setTimeout(run, 40 + Math.floor(Math.random() * 40));
          return;
        }
        if (typeof setInterval === 'function') {
          renew = setInterval(function () {
            acquireLease(storage, lockName, owner, leaseMs);
          }, Math.max(1000, Math.floor(leaseMs / 3)));
        }
        Promise.resolve()
          .then(function () { return callback({ mode: 'lease', locked: true }); })
          .then(
            function (value) {
              stopRenewing();
              releaseLease(storage, lockName, owner);
              resolve(value);
            },
            function (err) {
              stopRenewing();
              releaseLease(storage, lockName, owner);
              reject(err);
            }
          );
      }
      run();
    });
  }

  return {
    signature: signature,
    createAttemptStore: createAttemptStore,
    createRevision: createRevision,
    withCheckoutLock: withCheckoutLock,
    filterCartItems: filterCartItems,
    paginate: paginate,
    confirmButtonContent: confirmButtonContent,
    planInstanceConflict: planInstanceConflict,
    fetchWithTimeout: fetchWithTimeout,
    createCartInstance: createCartInstance,
    readCartInstance: readCartInstance,
    ensureCartInstance: ensureCartInstance,
    rotateCartInstance: rotateCartInstance,
    randomId: randomId
  };
});
