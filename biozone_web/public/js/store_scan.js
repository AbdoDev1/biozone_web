// Biozone store barcode/QR scan — local only, no build needed.
// Fills catalog search input[name=q] then submits GET /catalog preserving filters.
(function () {
  "use strict";
  var MAX_LEN = 128;
  var FRAPPE_QR = "/assets/frappe/node_modules/html5-qrcode/html5-qrcode.min.js";

  function normalizeScanText(raw) {
    var t = String(raw == null ? "" : raw).trim();
    if (!t) return "";
    // QR may hold a full URL like /item?code=BZ-11567 — extract code param.
    try {
      if (/^https?:\/\//i.test(t)) {
        var u = new URL(t);
        var c = u.searchParams.get("code") || u.searchParams.get("item_code") || u.searchParams.get("q");
        if (c && c.trim()) t = c.trim();
        else {
          var m = u.pathname.match(/([A-Za-z0-9][A-Za-z0-9\-_]{1,64})/);
          if (m) t = m[1];
        }
      }
    } catch (e) {}
    if (t.length > MAX_LEN) t = t.slice(0, MAX_LEN);
    return t;
  }

  function loadScriptOnce(src) {
    return new Promise(function (resolve, reject) {
      if (document.querySelector('script[data-bz-src="' + src + '"]')) return resolve(true);
      var s = document.createElement("script");
      s.src = src;
      s.async = true;
      s.setAttribute("data-bz-src", src);
      s.onload = function () { resolve(true); };
      s.onerror = function () { reject(new Error("load-failed")); };
      document.head.appendChild(s);
    });
  }

  function ensureOverlay(form, input) {
    var ov = document.getElementById("bz-scan-overlay");
    if (ov) return ov;
    ov = document.createElement("div");
    ov.id = "bz-scan-overlay";
    ov.setAttribute("dir", "rtl");
    ov.style.cssText = "position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.75);display:none;align-items:center;justify-content:center;padding:16px";
    ov.innerHTML =
      '<div style="background:#fff;border-radius:16px;max-width:420px;width:100%;padding:16px">' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
      '<strong>مسح باركود / QR</strong>' +
      '<button type="button" data-bz-close style="border:0;background:#f1f5f9;border-radius:9999px;width:32px;height:32px">✕</button>' +
      "</div>" +
      '<video data-bz-video playsinline muted style="width:100%;border-radius:12px;background:#000;display:none"></video>' +
      '<p data-bz-msg style="font-size:13px;color:#475569;margin:8px 0">وجّه الكاميرا للكود، أو اكتب الكود يدويًا في البحث واضغط Enter.</p>' +
      '<div style="display:flex;gap:8px">' +
      '<button type="button" data-bz-manual style="flex:1;background:#f1f5f9;border:0;border-radius:10px;padding:10px">كتابة يدوية</button>' +
      '<button type="button" data-bz-stop style="flex:1;background:#0ea5e9;color:#fff;border:0;border-radius:10px;padding:10px">إغلاق</button>' +
      "</div></div>";
    document.body.appendChild(ov);
    ov.querySelector("[data-bz-close]").addEventListener("click", function () { stopAll(ov); });
    ov.querySelector("[data-bz-stop]").addEventListener("click", function () { stopAll(ov); });
    ov.querySelector("[data-bz-manual]").addEventListener("click", function () {
      stopAll(ov);
      try { input.focus(); } catch (e) {}
    });
    ov.addEventListener("click", function (e) { if (e.target === ov) stopAll(ov); });
    return ov;
  }

  var activeStream = null;
  var activeDetectorTimer = null;
  var activeHtml5 = null;

  function stopAll(ov) {
    try {
      if (activeDetectorTimer) clearInterval(activeDetectorTimer);
      activeDetectorTimer = null;
    } catch (e) {}
    try {
      if (activeHtml5 && activeHtml5.stop) activeHtml5.stop();
    } catch (e) {}
    activeHtml5 = null;
    try {
      if (activeStream) activeStream.getTracks().forEach(function (tr) { try { tr.stop(); } catch (e2) {} });
    } catch (e) {}
    activeStream = null;
    try {
      var v = ov && ov.querySelector("[data-bz-video]");
      if (v) { v.pause(); v.srcObject = null; v.style.display = "none"; }
      if (ov) ov.style.display = "none";
    } catch (e) {}
  }

  function submitScan(form, input, raw) {
    var t = normalizeScanText(raw);
    if (!t) return;
    input.value = t;
    try {
      if (form.requestSubmit) form.requestSubmit();
      else form.submit();
    } catch (e) {
      form.submit();
    }
  }

  async function startFor(form, input) {
    var ov = ensureOverlay(form, input);
    var msg = ov.querySelector("[data-bz-msg]");
    var video = ov.querySelector("[data-bz-video]");
    ov.style.display = "flex";
    if (msg) msg.textContent = "جارٍ فتح الكاميرا…";
    stopAll(ov);
    ov.style.display = "flex";

    // Path 1: native BarcodeDetector (Chrome/Android).
    try {
      if (window.BarcodeDetector) {
        var stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
        activeStream = stream;
        video.srcObject = stream;
        video.style.display = "block";
        await video.play();
        var det = new window.BarcodeDetector({ formats: ["qr_code", "code_128", "code_39", "ean_13", "ean_8", "upc_a", "upc_e"] });
        if (msg) msg.textContent = "وجّه الكاميرا للكود…";
        activeDetectorTimer = setInterval(async function () {
          try {
            var codes = await det.detect(video);
            if (codes && codes.length) {
              var raw = codes[0].rawValue || "";
              clearInterval(activeDetectorTimer);
              activeDetectorTimer = null;
              stopAll(ov);
              submitScan(form, input, raw);
            }
          } catch (e) {}
        }, 400);
        return;
      }
    } catch (e) {}

    // Path 2: frappe-bundled html5-qrcode (no new vendor file).
    try {
      await loadScriptOnce(FRAPPE_QR);
      if (window.Html5Qrcode) {
        var box = document.createElement("div");
        box.id = "bz-html5-box";
        box.style.cssText = "width:100%";
        video.parentNode.insertBefore(box, video.nextSibling);
        var h = new window.Html5Qrcode("bz-html5-box");
        activeHtml5 = h;
        if (msg) msg.textContent = "وجّه الكاميرا للكود…";
        await h.start({ facingMode: "environment" }, { fps: 10, qrbox: 250 }, function (decoded) {
          try { h.stop(); } catch (e2) {}
          try { box.remove(); } catch (e3) {}
          stopAll(ov);
          submitScan(form, input, decoded);
        }, function () {});
        return;
      }
    } catch (e) {}

    if (msg) msg.textContent = "لا كاميرا متاحة هنا — اكتب الكود يدويًا في البحث ثم Enter.";
  }

  function bind() {
    document.querySelectorAll('form[action="/catalog"] button[data-bz-scan]').forEach(function (btn) {
      if (btn.getAttribute("data-bz-bound") === "1") return;
      btn.setAttribute("data-bz-bound", "1");
      btn.addEventListener("click", function () {
        var form = btn.closest("form");
        if (!form) return;
        var input = form.querySelector('input[name="q"]');
        if (!input) return;
        // Keyboard-wedge scanners just type + Enter; camera path below.
        startFor(form, input);
      });
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();
