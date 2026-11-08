(function () {
  'use strict';

  var GUEST_KEY = 'biozone_cart_items';
  var ACCOUNT_PREFIX = 'biozone_cart_items:';
  var SOURCE_UID_PREFIX = 'biozone_cart_source_uid:';
  var CHECKOUT_KEY = 'biozone_checkout_pending';
  var MERGE_DISMISS_PREFIX = 'biozone_merge_dismissed:';

  function readJson(key, fallback) {
    try {
      var raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) {
      return fallback;
    }
  }

  function writeJson(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (e) {}
  }

  function randomId() {
    try {
      if (window.crypto && window.crypto.randomUUID) {
        return window.crypto.randomUUID().replace(/-/g, '');
      }
      if (window.crypto && window.crypto.getRandomValues) {
        var bytes = new Uint8Array(16);
        window.crypto.getRandomValues(bytes);
        return Array.prototype.map.call(bytes, function (b) {
          return ('0' + b.toString(16)).slice(-2);
        }).join('');
      }
    } catch (e) {}
    return String(Date.now()) + Math.random().toString(16).slice(2, 18);
  }

  function accountKey(user) {
    return ACCOUNT_PREFIX + String(user || '').trim().toLowerCase();
  }

  function undoToken() {
    var token = '';
    while (token.length < 32) {
      token += randomId().replace(/[^0-9a-f]/gi, '').toLowerCase();
    }
    return token.slice(0, 64);
  }

  function roundQty(value) {
    return Math.round((Number(value) || 0) * 1000) / 1000;
  }

  function totalUnits(items) {
    return roundQty((items || []).reduce(function (sum, it) {
      return sum + (Number(it.quantity) || 0);
    }, 0));
  }

  function normalizeItem(it) {
    it = it || {};
    return {
      item_code: String(it.item_code || '').trim(),
      item_name: it.item_name || it.item_code || '',
      item_group: it.item_group || '',
      price: it.price === null || it.price === undefined ? null : Number(it.price),
      quantity: roundQty(it.quantity !== undefined ? it.quantity : it.qty),
      uom: it.uom || it.display_uom || '',
      image: it.image || it.thumbnail || '',
      line_total: null,
      available: it.available !== false,
      issues: it.issues || []
    };
  }

  function CartApiError(payload, status) {
    payload = payload || {};
    var message = payload.error || payload.message;
    if (!message || typeof message !== 'string') {
      message = status === 401
        ? 'سجّل الدخول أولًا لإتمام طلبك'
        : 'تعذر تنفيذ الطلب، يرجى المحاولة مرة أخرى';
    }
    Error.call(this, message);
    this.name = 'CartApiError';
    this.message = message;
    this.status = status || 0;
    this.code = payload.error_code || payload.code || (status === 401 ? 'UNAUTHORIZED' : 'REQUEST_FAILED');
    this.requestId = payload.request_id || '';
    this.payload = payload;
  }
  CartApiError.prototype = Object.create(Error.prototype);
  CartApiError.prototype.constructor = CartApiError;

  async function callApi(name, payload, method) {
    var httpMethod = method || 'POST';
    var opts = {
      method: httpMethod,
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    };
    if (httpMethod !== 'GET') {
      opts.headers['Content-Type'] = 'application/json';
      var csrf = window.BZ_CSRF_TOKEN
        || (window.frappe && window.frappe.csrf_token)
        || '';
      if (csrf) opts.headers['X-Frappe-CSRF-Token'] = csrf;
      opts.body = JSON.stringify(payload || {});
    }
    var res;
    try {
      res = await fetch('/api/method/biozone_web.api.' + name, opts);
    } catch (e) {
      throw new CartApiError({ error: 'تعذر الاتصال بالخادم، تحقق من الشبكة', error_code: 'NETWORK_ERROR' }, 0);
    }
    var data = {};
    try {
      data = await res.json();
    } catch (e) {}
    var out = data && data.message !== undefined ? data.message : data;
    if (!res.ok || !out || out.ok === false) {
      throw new CartApiError(out || {}, res.status);
    }
    return out;
  }

  function CartStore() {
    this.user = 'Guest';
    this.isGuest = true;
    this.key = GUEST_KEY;
    this.accountKey = accountKey('');
    this._localMode = true;
    this._bound = false;
    this._initPromise = null;
    this.preview = null;
    this.notices = {};
    this.state = this._emptyState();
    this.configure({});
  }

  CartStore.prototype._emptyState = function () {
    return {
      mode: 'local',
      items: [],
      cart: { version: 0, item_count: 0, total_units: 0 },
      quote: { valid: true, net_total: 0 },
      quote_fingerprint: null,
      version: 0,
      net_total: 0,
      total_units: 0,
      item_count: 0,
      loading: false,
      error: null,
      droppedCount: 0,
      undo: null,
      skipped: []
    };
  };

  CartStore.prototype.configure = function (opts) {
    var previousUser = this.user;
    opts = opts || {};
    this.user = opts.user || window.BZ_STORE_USER || 'Guest';
    this.isGuest = !this.user || this.user === 'Guest';
    this.key = this.isGuest ? GUEST_KEY : accountKey(this.user);
    this.accountKey = accountKey(this.user);
    if (this.isGuest) {
      this._localMode = true;
    } else if (previousUser !== this.user) {
      this._localMode = false;
    }
  };

  CartStore.prototype.init = function (opts) {
    opts = opts || {};
    if (opts.user) this.configure(opts);
    if (this._initPromise) return this._initPromise;
    this._bind();
    this._initPromise = (async function (self) {
      try {
        await self.refresh({ silent: true, includeUndo: !!opts.includeUndo });
      } catch (e) {}
      if (!self.isGuest && opts.autoMergeLocalAccount !== false) {
        await self._autoMergeLocalAccount();
      }
      return self.state;
    })(this);
    return this._initPromise;
  };

  CartStore.prototype._bind = function () {
    if (this._bound) return;
    this._bound = true;
    var self = this;
    window.addEventListener('storage', function (e) {
      if (!e || !e.key) return;
      if (self._localMode || self.isGuest) {
        if (e.key === self.key || e.key === GUEST_KEY) self._reloadLocal();
      } else if (e.key === GUEST_KEY || e.key === self.accountKey) {
        self.updateBadge();
        self._emit();
      }
    });
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden && !self.isGuest && !self._localMode) {
        self.refresh({ silent: true });
      }
    });
  };

  CartStore.prototype._emit = function () {
    this.updateBadge();
    try {
      window.dispatchEvent(new CustomEvent('biozone:cart-changed', {
        detail: { state: this.state, preview: this.preview, notices: this.notices }
      }));
    } catch (e) {}
  };

  CartStore.prototype.updateBadge = function () {
    var badge = document.getElementById('cart-count-badge');
    if (badge) badge.textContent = String(this.getBadgeCount());
  };

  CartStore.prototype._localItems = function (key) {
    var items = readJson(key, []);
    if (!Array.isArray(items)) return [];
    return items.map(normalizeItem).filter(function (it) { return !!it.item_code; });
  };

  CartStore.prototype._repriceLocal = async function (items) {
    var codes = [];
    items.forEach(function (it) {
      if (it.item_code && codes.indexOf(it.item_code) === -1) codes.push(it.item_code);
    });
    if (!codes.length) return { items: items, dropped: 0, failed: false };
    try {
      var data = await callApi('biozone_get_cart_prices', { item_codes: codes });
      var prices = data.prices || {};
      var kept = [];
      var dropped = 0;
      items.forEach(function (it) {
        var price = prices[it.item_code];
        if (!price || price.price === null || price.price === undefined) {
          dropped += 1;
          return;
        }
        it.price = Number(price.price);
        it.line_total = null;
        if (!it.uom && price.display_uom) it.uom = price.display_uom;
        if (price.thumbnail) it.image = price.thumbnail;
        if (price.item_name) it.item_name = price.item_name;
        if (price.item_group) it.item_group = price.item_group;
        kept.push(it);
      });
      return { items: kept, dropped: dropped, failed: false };
    } catch (e) {
      return { items: items, dropped: 0, failed: true };
    }
  };

  CartStore.prototype._setLocalState = async function (items, opts) {
    opts = opts || {};
    this._localMode = true;
    items = (items || []).map(normalizeItem);
    var outcome = { items: items, dropped: 0, failed: false };
    if (opts.reprice !== false) {
      outcome = await this._repriceLocal(items);
      items = outcome.items;
      if (outcome.dropped > 0) writeJson(this.key, items);
    }
    var net = items.reduce(function (sum, it) {
      return sum + (Number(it.price) || 0) * (Number(it.quantity) || 0);
    }, 0);
    var units = totalUnits(items);
    this.state = {
      mode: 'local',
      items: items,
      cart: { version: 0, item_count: items.length, total_units: units },
      quote: { valid: true, net_total: Math.round(net * 100) / 100 },
      quote_fingerprint: null,
      version: 0,
      net_total: Math.round(net * 100) / 100,
      total_units: units,
      item_count: items.length,
      loading: false,
      error: null,
      droppedCount: outcome.dropped
    };
    this._emit();
    return this.state;
  };

  CartStore.prototype._reloadLocal = async function () {
    return this._setLocalState(this._localItems(this.key));
  };

  CartStore.prototype._setServerState = function (data) {
    var cart = data.cart || {};
    var quote = data.quote || {};
    var items = (quote.items || []).map(function (it) {
      return {
        item_code: it.item_code,
        item_name: it.item_name || it.item_code,
        item_group: it.item_group || '',
        price: it.price === null || it.price === undefined ? null : Number(it.price),
        quantity: roundQty(it.qty),
        uom: it.uom || '',
        image: it.thumbnail || '',
        line_total: it.line_total === undefined ? null : it.line_total,
        available: it.available !== false,
        issues: it.issues || []
      };
    });
    this._localMode = false;
    this.state = {
      mode: 'server',
      items: items,
      cart: cart,
      quote: quote,
      quote_fingerprint: data.quote_fingerprint || quote.fingerprint || null,
      version: Number(cart.version || 0),
      net_total: Number(quote.net_total || 0),
      total_units: Number(quote.total_units || 0),
      item_count: Number(quote.item_count || items.length),
      loading: false,
      error: null,
      droppedCount: 0,
      undo: data.undo || null,
      skipped: data.skipped || []
    };
    this._emit();
    return this.state;
  };

  CartStore.prototype.refresh = async function (opts) {
    opts = opts || {};
    if (this.isGuest || this._localMode) return this._reloadLocal();
    this.state = Object.assign({}, this.state, { loading: true, error: null });
    this._emit();
    try {
      var path = opts.includeUndo
        ? 'biozone_cart_get?include_undo=1'
        : 'biozone_cart_get';
      var data = await callApi(path, null, 'GET');
      return this._setServerState(data);
    } catch (err) {
      if (err.code === 'CUSTOMER_MISSING' || err.code === 'CUSTOMER_GROUP_INELIGIBLE') {
        this._localMode = true;
        await this._reloadLocal();
        this.state = Object.assign({}, this.state, { error: err.message });
        this._emit();
        if (!opts.silent) throw err;
        return this.state;
      }
      this.state = Object.assign({}, this.state, { loading: false, error: err.message });
      this._emit();
      if (!opts.silent) throw err;
      return this.state;
    }
  };

  CartStore.prototype._sourceUid = function (kind, key) {
    var storageKey = SOURCE_UID_PREFIX + kind + ':' + key;
    var uid = '';
    try { uid = localStorage.getItem(storageKey) || ''; } catch (e) {}
    if (!uid || uid.length < 8) {
      uid = randomId();
      try { localStorage.setItem(storageKey, uid); } catch (e) {}
    }
    return uid;
  };

  CartStore.prototype._buildSource = function (kind) {
    var key = kind === 'Guest' ? GUEST_KEY : this.accountKey;
    var items = this._localItems(key);
    if (!items.length) return null;
    var uidSeed = kind === 'Guest' ? 'guest:' + this.accountKey : this.accountKey;
    return {
      source_uid: this._sourceUid(kind, uidSeed),
      source_kind: kind,
      source_key: kind === 'Guest' ? key : this.user,
      items: items.map(function (it) {
        return { item_code: it.item_code, qty: it.quantity, uom: it.uom };
      }),
      target_version: Number(this.state.version || 0),
      local_key: key
    };
  };

  CartStore.prototype.transferPreview = async function (source) {
    if (!source) throw new CartApiError({ error: 'سلة المصدر فارغة', error_code: 'CART_EMPTY' }, 422);
    var payload = {
      source_uid: source.source_uid,
      source_kind: source.source_kind,
      source_key: source.source_key,
      items: source.items,
      target_version: source.target_version
    };
    var res = await callApi('biozone_cart_transfer_preview', { source: payload });
    if (res.already_applied) {
      writeJson(source.local_key, []);
      this.preview = null;
      await this.refresh({ silent: true });
      return res;
    }
    this.preview = Object.assign({}, res, { source: source });
    this._emit();
    return this.preview;
  };

  CartStore.prototype.transferApply = async function (preview) {
    var p = preview || this.preview;
    if (!p || !p.merge_token) {
      throw new CartApiError({ error: 'جلسة الدمج غير موجودة — أعد المحاولة', error_code: 'MERGE_NOT_FOUND' }, 409);
    }
    var res = await callApi('biozone_cart_transfer_apply', {
      merge_token: p.merge_token,
      source_fingerprint: p.source_fingerprint,
      cart_version: p.target_version
    });
    if (p.source && p.source.local_key) writeJson(p.source.local_key, []);
    this.preview = null;
    this.notices.mergeApplied = true;
    await this.refresh({ silent: true });
    this._emit();
    return res;
  };

  CartStore.prototype.dismissPreview = function () {
    if (this.preview && this.preview.source) {
      try {
        sessionStorage.setItem(MERGE_DISMISS_PREFIX + this.preview.source.source_uid, '1');
      } catch (e) {}
    }
    this.preview = null;
    this._emit();
  };

  CartStore.prototype._autoMergeLocalAccount = async function () {
    if (this.isGuest || this._localMode) return;
    if (this.state.items && this.state.items.length) return;
    if (!this._localItems(this.accountKey).length) return;
    var source = this._buildSource('Local Account');
    if (!source) return;
    try {
      var preview = await this.transferPreview(source);
      if (preview && preview.auto_apply && preview.can_apply) {
        await this.transferApply(preview);
        this.notices.autoMerged = true;
        this._emit();
      }
    } catch (err) {
      this.notices.mergeError = err.message;
      this._emit();
    }
  };

  CartStore.prototype.maybePreviewTransfer = async function () {
    if (this.isGuest || this._localMode) return null;
    var kind = this._localItems(GUEST_KEY).length
      ? 'Guest'
      : (this._localItems(this.accountKey).length ? 'Local Account' : null);
    if (!kind) return null;
    var source = this._buildSource(kind);
    if (!source) return null;
    var dismissed = '';
    try { dismissed = sessionStorage.getItem(MERGE_DISMISS_PREFIX + source.source_uid) || ''; } catch (e) {}
    if (dismissed === '1') return null;
    try {
      var preview = await this.transferPreview(source);
      if (kind === 'Local Account' && preview && preview.auto_apply && preview.can_apply) {
        if (!this.state.items || !this.state.items.length) {
          await this.transferApply(preview);
          return null;
        }
      }
      return preview;
    } catch (err) {
      this.notices.mergeError = err.message;
      this._emit();
      return null;
    }
  };

  CartStore.prototype._localAdd = function (itemCode, qty, uom, meta) {
    meta = meta || {};
    var items = this._localItems(this.key);
    var existing = null;
    for (var i = 0; i < items.length; i += 1) {
      if (items[i].item_code === itemCode) { existing = items[i]; break; }
    }
    if (existing) {
      if (existing.uom && uom && existing.uom !== uom) {
        throw new CartApiError({ error: 'الصنف موجود في السلة بوحدة مختلفة — أزله ثم أضفه بالوحدة الجديدة', error_code: 'CART_UOM_CONFLICT' }, 409);
      }
      var nextQty = roundQty(existing.quantity + qty);
      if (nextQty > 100000) {
        throw new CartApiError({ error: 'الكمية أكبر من الحد المسموح', error_code: 'QTY_LIMIT' }, 422);
      }
      existing.quantity = nextQty;
      if (uom) existing.uom = uom;
      if (meta.item_name) existing.item_name = meta.item_name;
      if (meta.item_group) existing.item_group = meta.item_group;
      if (meta.price !== undefined && meta.price !== null) existing.price = Number(meta.price);
    } else {
      items.push(normalizeItem({
        item_code: itemCode,
        item_name: meta.item_name || itemCode,
        item_group: meta.item_group || '',
        price: meta.price,
        quantity: qty,
        uom: uom || '',
        image: meta.image || ''
      }));
    }
    writeJson(this.key, items);
    return this._setLocalState(items, { reprice: false });
  };

  CartStore.prototype._localSetQty = function (itemCode, qty) {
    if (qty > 100000) {
      throw new CartApiError({ error: 'الكمية أكبر من الحد المسموح', error_code: 'QTY_LIMIT' }, 422);
    }
    var items = this._localItems(this.key);
    var found = false;
    items.forEach(function (it) {
      if (it.item_code === itemCode) { it.quantity = roundQty(qty); found = true; }
    });
    if (!found) throw new CartApiError({ error: 'الصنف غير موجود في السلة', error_code: 'ITEM_NOT_IN_CART' }, 422);
    writeJson(this.key, items);
    return this._setLocalState(items, { reprice: false });
  };

  CartStore.prototype._localRemove = function (itemCode) {
    var items = this._localItems(this.key).filter(function (it) {
      return it.item_code !== itemCode;
    });
    writeJson(this.key, items);
    return this._setLocalState(items, { reprice: false });
  };

  CartStore.prototype.addItem = async function (itemCode, qty, uom, meta) {
    itemCode = String(itemCode || '').trim();
    if (!itemCode) {
      throw new CartApiError({ error: 'بيانات صنف غير صحيحة في السلة', error_code: 'ITEM_DATA_INVALID' }, 422);
    }
    var amount = Number(qty === undefined || qty === null || qty === '' ? 1 : qty);
    if (!isFinite(amount) || amount <= 0 || amount > 100000) {
      throw new CartApiError({ error: 'بيانات صنف غير صحيحة في السلة', error_code: 'ITEM_DATA_INVALID' }, 422);
    }
    if (this.isGuest || this._localMode) {
      return this._localAdd(itemCode, amount, uom, meta);
    }
    try {
      var data = await callApi('biozone_cart_add', {
        item_code: itemCode,
        qty: amount,
        uom: uom || null,
        cart_version: Number(this.state.version || 0)
      });
      return this._setServerState(data);
    } catch (err) {
      if (err.status === 409) await this.refresh({ silent: true });
      throw err;
    }
  };

  CartStore.prototype.setQty = async function (itemCode, qty) {
    var amount = roundQty(qty);
    if (amount <= 0) return this.removeItem(itemCode);
    if (amount > 100000) {
      throw new CartApiError({ error: 'الكمية أكبر من الحد المسموح', error_code: 'QTY_LIMIT' }, 422);
    }
    if (this.isGuest || this._localMode) return this._localSetQty(itemCode, amount);
    try {
      var data = await callApi('biozone_cart_set_qty', {
        item_code: itemCode,
        qty: amount,
        cart_version: Number(this.state.version || 0)
      });
      return this._setServerState(data);
    } catch (err) {
      if (err.status === 409) await this.refresh({ silent: true });
      throw err;
    }
  };

  CartStore.prototype.removeItem = async function (itemCode) {
    if (this.isGuest || this._localMode) return this._localRemove(itemCode);
    try {
      var data = await callApi('biozone_cart_remove', {
        item_code: itemCode,
        cart_version: Number(this.state.version || 0)
      });
      return this._setServerState(data);
    } catch (err) {
      if (err.status === 409) await this.refresh({ silent: true });
      throw err;
    }
  };

  CartStore.prototype.clear = async function () {
    if (this.isGuest || this._localMode) {
      writeJson(this.key, []);
      return this._setLocalState([]);
    }
    try {
      var data = await callApi('biozone_cart_clear', {
        cart_version: Number(this.state.version || 0)
      });
      return this._setServerState(data);
    } catch (err) {
      if (err.status === 409) await this.refresh({ silent: true });
      throw err;
    }
  };

  CartStore.prototype.undo = async function (undoInfo) {
    if (!undoInfo || !undoInfo.snapshot) {
      throw new CartApiError(
        { error: 'لا توجد نسخة للاستعادة', error_code: 'SNAPSHOT_NOT_AVAILABLE' },
        404
      );
    }
    if (this.isGuest || this._localMode) {
      throw new CartApiError(
        { error: 'التراجع متاح بعد تسجيل الدخول فقط', error_code: 'FEATURE_DISABLED' },
        404
      );
    }
    try {
      var data = await callApi('biozone_cart_undo', {
        snapshot: undoInfo.snapshot,
        restore_token: undoToken(),
        cart_version: Number(this.state.version || 0)
      });
      var state = this._setServerState(data);
      // _setServerState rebuilds the public state and drops the undo
      // response counters; the toast reads them from the result.
      state.restored_count = Number(data.restored_count || 0);
      state.skipped_count = Number(data.skipped_count || 0);
      state.already_restored = !!data.already_restored;
      state.applied_mode = data.applied_mode || null;
      return state;
    } catch (err) {
      if (err.status === 409) {
        await this.refresh({ silent: true, includeUndo: true });
      }
      throw err;
    }
  };

  CartStore.prototype.getQty = function (itemCode) {
    var items = this.state.items || [];
    for (var i = 0; i < items.length; i += 1) {
      if (items[i].item_code === itemCode) return Number(items[i].quantity || 0);
    }
    return 0;
  };

  CartStore.prototype.getBadgeCount = function () {
    if (this.isGuest || this._localMode) return totalUnits(this.state.items);
    var count = Number(this.state.total_units || 0);
    count += totalUnits(this._localItems(GUEST_KEY));
    count += totalUnits(this._localItems(this.accountKey));
    return roundQty(count);
  };

  CartStore.prototype._checkoutToken = function (version, fingerprint) {
    var saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(CHECKOUT_KEY) || 'null'); } catch (e) {}
    if (saved && saved.version === version && saved.fingerprint === fingerprint && saved.token) {
      return saved.token;
    }
    var token = randomId();
    try {
      sessionStorage.setItem(CHECKOUT_KEY, JSON.stringify({
        version: version,
        fingerprint: fingerprint,
        token: token
      }));
    } catch (e) {}
    return token;
  };

  CartStore.prototype._clearCheckoutToken = function () {
    try { sessionStorage.removeItem(CHECKOUT_KEY); } catch (e) {}
  };

  CartStore.prototype._clearLocalCarts = function () {
    try {
      localStorage.removeItem(GUEST_KEY);
      localStorage.removeItem(this.accountKey);
    } catch (e) {}
  };

  CartStore.prototype.checkoutStatus = async function (token) {
    var res = await callApi('biozone_checkout_status', { checkout_token: token });
    if (res && res.status === 'Success') this._clearCheckoutToken();
    return res;
  };

  CartStore.prototype.confirmOrder = async function () {
    if (this.isGuest) {
      throw new CartApiError({ error: 'سجّل الدخول أولًا لإتمام طلبك', error_code: 'LOGIN_REQUIRED' }, 401);
    }
    if (this._localMode || this.state.mode !== 'server') {
      var localError = this.state && this.state.error
        ? this.state.error
        : 'تعذر الاتصال بسلة الحساب، حدّث الصفحة وحاول مجددًا';
      throw new CartApiError({ error: localError, error_code: 'CART_UNAVAILABLE' }, 422);
    }
    var version = Number(this.state.version || 0);
    var fingerprint = this.state.quote_fingerprint;
    if (!fingerprint) {
      throw new CartApiError({ error: 'حدّث السلة ثم أعد المحاولة', error_code: 'CART_QUOTE_INVALID' }, 422);
    }
    var token = this._checkoutToken(version, fingerprint);
    try {
      var res = await callApi('biozone_confirm_order', {
        checkout_token: token,
        cart_version: version,
        quote_fingerprint: fingerprint
      });
      this._clearCheckoutToken();
      await this.refresh({ silent: true });
      return res;
    } catch (err) {
      if (err.code === 'CHECKOUT_IN_PROGRESS') {
        try {
          var status = await this.checkoutStatus(token);
          if (status && status.status === 'Success') {
            return {
              ok: true,
              sales_order: status.sales_order,
              redirect: status.redirect
            };
          }
        } catch (e) {}
      } else if (err.code === 'PRICE_CHANGED' || err.code === 'CART_VERSION_CONFLICT') {
        this._clearCheckoutToken();
        await this.refresh({ silent: true });
      } else if (err.status === 422 || err.status === 409) {
        this._clearCheckoutToken();
        await this.refresh({ silent: true });
      }
      throw err;
    }
  };

  var store = new CartStore();
  window.BiozoneCart = store;
  window.BZCart = store;
  window.CartApiError = CartApiError;
})();
