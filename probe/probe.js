/*
 * GSPP Probe —— 游戏状态与动作的读取/下发层
 *
 * 两种模式：
 *   registered  作者注册了 adapter，拿到游戏内部真状态（高保真，约 20 行接入）
 *   dom-fallback 作者什么都没写，从 DOM + canvas 像素推断（零接入，保真度低但通用）
 *
 * 有 fallback 才谈得上"帮其他创作者"：本批赛题会产出几百个没人打算改造的 HTML5 游戏，
 * 要求它们先接协议等于这个工具对它们不存在。fallback 是实用价值的载体。
 *
 * 依赖：virtual_clock.js、seeded_random.js 先执行（时间量化与哈希只在这里读结果）。
 */
(function () {
  "use strict";
  if (window.__GSPP__) return;

  var adapter = null;
  var KEY_ACTIONS = {
    ArrowLeft: "ArrowLeft", ArrowRight: "ArrowRight", ArrowUp: "ArrowUp", ArrowDown: "ArrowDown",
    Space: " ", Enter: "Enter", Escape: "Escape",
    KeyW: "w", KeyA: "a", KeyS: "s", KeyD: "d"
  };

  function canonical(v, depth) {
    depth = depth || 0;
    if (depth > 6) return "<deep>";
    if (v === null || v === undefined) return null;
    var t = typeof v;
    if (t === "number") return Number.isFinite(v) ? Math.round(v * 1e6) / 1e6 : String(v);
    if (t === "boolean" || t === "string") return t === "string" && v.length > 120 ? v.slice(0, 120) : v;
    if (t === "function") return "<fn>";
    if (Array.isArray(v)) {
      var a = [];
      for (var i = 0; i < Math.min(v.length, 64); i++) a.push(canonical(v[i], depth + 1));
      return a;
    }
    if (t === "object") {
      var out = {};
      var keys = Object.keys(v).sort().slice(0, 64);
      for (var k = 0; k < keys.length; k++) {
        try { out[keys[k]] = canonical(v[keys[k]], depth + 1); } catch (_) { out[keys[k]] = "<err>"; }
      }
      return out;
    }
    return String(v);
  }

  function canvasSignature() {
    var out = [];
    var cs = document.querySelectorAll("canvas");
    for (var i = 0; i < Math.min(cs.length, 4); i++) {
      var c = cs[i];
      try {
        var s = 8, tmp = document.createElement("canvas");
        tmp.width = s; tmp.height = s;
        var ctx = tmp.getContext("2d");
        ctx.drawImage(c, 0, 0, s, s);
        var d = ctx.getImageData(0, 0, s, s).data;
        var g = "";
        for (var p = 0; p < d.length; p += 4) {
          var lum = (d[p] * 299 + d[p + 1] * 587 + d[p + 2] * 114) / 1000;
          g += Math.round(lum / 25.5).toString(36);
        }
        out.push(c.width + "x" + c.height + ":" + g);
      } catch (e) {
        out.push("tainted:" + c.width + "x" + c.height);
      }
    }
    return out;
  }

  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    var r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    var st = window.getComputedStyle ? window.getComputedStyle(el) : null;
    if (st && (st.display === "none" || st.visibility === "hidden" || st.opacity === "0")) return false;
    return true;
  }

  function labelOf(el) {
    var s = (el.getAttribute("aria-label") || el.title || el.innerText || el.value || el.textContent || "").trim();
    s = s.replace(/\s+/g, " ");
    if (!s && el.tagName === "CANVAS") s = "canvas";
    return s.slice(0, 40);
  }

  function interactive() {
    var sel = "button,[role=button],a[href],input:not([type=hidden]),select,canvas,[onclick],[tabindex]";
    var els = [];
    try { els = Array.prototype.slice.call(document.querySelectorAll(sel)); } catch (_) {}
    var out = [];
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!visible(el)) continue;
      if (el.disabled) continue;
      var lb = labelOf(el);
      if (!lb) continue;
      out.push({ el: el, label: lb, tag: el.tagName.toLowerCase(), id: el.id || "" });
    }
    // 同名标签去重，保留第一个；否则一个"开始"按钮渲染两遍会给出两个动作
    var seen = {}, uniq = [];
    for (var j = 0; j < out.length; j++) {
      var key = out[j].tag + "|" + out[j].label;
      if (seen[key]) continue;
      seen[key] = 1;
      uniq.push(out[j]);
    }
    return uniq.slice(0, 40);
  }

  function domRead() {
    var texts = [];
    try {
      var nodes = document.querySelectorAll("body, body *");
      for (var i = 0; i < nodes.length && texts.length < 40; i++) {
        var n = nodes[i];
        if (n.children.length > 0) continue;
        var tx = (n.innerText || n.textContent || "").trim().replace(/\s+/g, " ");
        if (tx && tx.length <= 80 && visible(n)) texts.push(n.tagName.toLowerCase() + ":" + tx);
      }
    } catch (_) {}

    var btns = [];
    var its = interactive();
    for (var b = 0; b < its.length; b++) btns.push(its[b].tag + ":" + its[b].label);

    var inputs = [];
    try {
      var ins = document.querySelectorAll("input:not([type=hidden]),select,textarea");
      for (var q = 0; q < ins.length && inputs.length < 20; q++) {
        inputs.push(((ins[q].id || ins[q].name || labelOf(ins[q])) + "=" + String(ins[q].value || "")).slice(0, 60));
      }
    } catch (_) {}

    return {
      mode: "dom-fallback",
      title: document.title || "",
      url_hash: String(location.hash || ""),
      texts: texts.sort(),
      buttons: btns.sort(),
      inputs: inputs.sort(),
      canvas: canvasSignature()
    };
  }

  function domActions() {
    var out = [];
    var its = interactive();
    for (var i = 0; i < its.length; i++) {
      out.push({
        name: "click:" + its[i].tag + ":" + its[i].label,
        label: "点击 " + its[i].label,
        kind: "click",
        tag: its[i].tag,
        label_key: its[i].label,
        index: i
      });
    }
    var keys = Object.keys(KEY_ACTIONS);
    for (var k = 0; k < keys.length; k++) {
      out.push({ name: "key:" + keys[k], label: "按键 " + keys[k], kind: "key", code: keys[k], key: KEY_ACTIONS[keys[k]] });
    }
    return out;
  }

  function domApply(action) {
    // DOM 模式的动作对象是扁平的（kind/tag/label_key 直接在顶层），
    // 注册模式才有 spec。之前一律按 .spec 读，等于这条路径从没被走过。
    var a = action.spec || action;
    if (a.kind === "click") {
      // 按名字重新定位，不用录制时的 index：
      // DOM 兜底的元素列表会随界面切换而变（商店打开后"挖矿"就不在场了），
      // 拿旧 index 去点，点的会是另一个按钮 —— 轨迹当场失真。
      var its = interactive();
      var el = null;
      for (var q = 0; q < its.length; q++) {
        if (its[q].tag === a.tag && its[q].label === a.label_key) { el = its[q].el; break; }
      }
      if (!el) return false;
      var r = el.getBoundingClientRect();
      var x = r.left + r.width / 2, y = r.top + r.height / 2;
      var opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, view: window };
      try {
        el.focus && el.focus();
        if (window.PointerEvent) {
          el.dispatchEvent(new PointerEvent("pointerdown", Object.assign({ pointerId: 1, isPrimary: true }, opts)));
        } else {
          el.dispatchEvent(new MouseEvent("mousedown", opts));
        }
        el.dispatchEvent(new MouseEvent("mouseup", opts));
        el.dispatchEvent(new MouseEvent("click", opts));
        if (el.tagName === "A" || el.tagName === "BUTTON") el.click && el.click();
      } catch (e) {
        (window.__GSPP_ERRORS__ = window.__GSPP_ERRORS__ || []).push({ kind: "apply", message: String(e), atFrame: -1 });
        return false;
      }
      return true;
    }
    if (a.kind === "key") {
      var targets = [document.body, document.querySelector("canvas")].filter(Boolean);
      var base = { bubbles: true, cancelable: true, key: a.key, code: a.code, view: window };
      try {
        for (var i = 0; i < targets.length; i++) {
          targets[i].dispatchEvent(new KeyboardEvent("keydown", base));
        }
        document.dispatchEvent(new KeyboardEvent("keydown", base));
        window.dispatchEvent(new KeyboardEvent("keydown", base));
        for (var f = 0; f < targets.length; f++) {
          targets[f].dispatchEvent(new KeyboardEvent("keyup", base));
        }
      } catch (e) {
        (window.__GSPP_ERRORS__ = window.__GSPP_ERRORS__ || []).push({ kind: "apply", message: String(e), atFrame: -1 });
        return false;
      }
      return true;
    }
    return false;
  }

  function registeredRead() {
    try {
      var s = adapter.read ? adapter.read() : {};
      return { mode: "registered", data: canonical(s), over: adapter.isOver ? !!adapter.isOver(s) : false, win: adapter.isWin ? !!adapter.isWin(s) : false };
    } catch (e) {
      (window.__GSPP_ERRORS__ = window.__GSPP_ERRORS__ || []).push({ kind: "read", message: String(e && e.message || e), atFrame: -1 });
      return { mode: "registered", data: null, read_error: String(e && e.message || e), over: false, win: false };
    }
  }

  var api = {
    version: "0.1",

    register: function (a) {
      adapter = a;
      if (a && typeof a.seedFrom === "function" && window.__GSPP_RANDOM__) {
        // 允许 adapter 在 reset 时重建自己的随机源
        a.seedFrom(window.__GSPP_RANDOM__.currentSeed());
      }
      return api;
    },

    mode: function () { return adapter ? "registered" : "dom-fallback"; },

    meta: function () {
      if (adapter && adapter.meta) return adapter.meta;
      return { id: (document.title || "untitled").replace(/\s+/g, "-").toLowerCase(), title: document.title, ticksPerAction: 4 };
    },

    begin: function (seed) {
      if (window.__GSPP_RANDOM__) window.__GSPP_RANDOM__.seed(seed);
      if (window.__GSPP_CLOCK__ && window.__GSPP_CLOCK__.rewind) window.__GSPP_CLOCK__.rewind();
      window.__GSPP_ERRORS__ = [];
      window.__GSPP_WARN__ = [];
      if (adapter && adapter.reset) {
        try { adapter.reset(seed); } catch (e) { (window.__GSPP_ERRORS__ = window.__GSPP_ERRORS__ || []).push({ kind: "reset", message: String(e), atFrame: -1 }); }
      }
      return api.read();
    },

    read: function () {
      var s = adapter ? registeredRead() : domRead();
      if (adapter) s.canonical = canonical(s.data);
      else s.canonical = canonical(s);
      if (adapter && adapter.key) {
        try { s.pos = canonical(adapter.key(adapter.read())); } catch (e) { s.pos = null; }
      }
      return s;
    },

    // 状态等价投影。为什么需要它：
    // 真实游戏几乎都带回合数/计时器/分数这类单调字段，它们一涨，
    // 完整状态哈希就永不重复 ⇒ 状态空间看起来是无限的，
    // 可达性探测必然截断，softlock 与 unwinnable 一个都判不出来。
    // 作者声明"哪些字段决定局面本身"，探测才能在真有限的状态空间上收敛。
    pos: function () {
      var s = api.read();
      return s.pos || null;
    },

    actions: function () {
      if (adapter && adapter.actions) {
        try {
          var list = adapter.actions() || [];
          var out = [];
          for (var i = 0; i < list.length; i++) {
            var it = typeof list[i] === "string" ? { name: list[i], label: list[i] } : list[i];
            out.push({ name: String(it.name), label: String(it.label || it.name), kind: "registered", spec: it });
          }
          return out;
        } catch (e) {
          (window.__GSPP_ERRORS__ = window.__GSPP_ERRORS__ || []).push({ kind: "actions", message: String(e), atFrame: -1 });
          return [];
        }
      }
      return domActions();
    },

    apply: function (name) {
      var found = null, list = api.actions();
      for (var i = 0; i < list.length; i++) if (list[i].name === name) { found = list[i]; break; }
      if (!found) return { ok: false, reason: "unknown_action:" + name };
      if (adapter) {
        try { adapter.apply(found.spec); return { ok: true }; }
        catch (e) {
          (window.__GSPP_ERRORS__ = window.__GSPP_ERRORS__ || []).push({ kind: "apply", message: String(e && e.message || e), atFrame: -1 });
          return { ok: false, reason: "apply_threw:" + String(e && e.message || e) };
        }
      }
      return { ok: domApply(found) };
    },

    invariants: function () {
      if (!adapter || !adapter.invariants) return [];
      try {
        var s = adapter.read ? adapter.read() : {};
        var r = adapter.invariants(s) || [];
        var out = [];
        for (var i = 0; i < r.length; i++) out.push(String(r[i]));
        return out;
      } catch (e) {
        return ["invariant_check_threw:" + String(e && e.message || e)];
      }
    },

    snapshot: function () {
      if (adapter && adapter.snapshot) { try { return canonical(adapter.snapshot()); } catch (_) { return null; } }
      return null;
    },

    restore: function (snap) {
      if (adapter && adapter.restore) { try { adapter.restore(snap); return true; } catch (e) { return false; } }
      return false;
    },

    clock: function () { return window.__GSPP_CLOCK__ ? window.__GSPP_CLOCK__.state() : null; },
    rng: function () { return window.__GSPP_RANDOM__ ? window.__GSPP_RANDOM__.state() : null; },

    // 🔴 持久状态。种子的前提是"状态只由 (seed, 动作) 决定"，
    // 但真实应用普遍把进度写进 localStorage / sessionStorage / cookie：
    // 第一遍试玩点了"开始新游戏"就落了档，重放时界面已经不一样了，
    // 于是复现包在真实作品上必然回放不过 —— 实测在牛马地下城上就是这么分歧的。
    // 所以复现包必须自带这份持久状态，回放前先装回去。
    storage: function () {
      var out = { localStorage: {}, sessionStorage: {}, cookie_present: false, oversized: [] };
      function dump(store, bucket) {
        try {
          for (var i = 0; i < store.length; i++) {
            var k = store.key(i);
            if (k === null) continue;
            var v = store.getItem(k);
            if (v && v.length > 4096) {
              out.oversized.push(k + ":" + v.length);
              bucket[k] = "<too_large:" + v.length + ">";
            } else {
              bucket[k] = v;
            }
          }
        } catch (e) {
          out.error = String(e && e.message || e);
        }
      }
      dump(window.localStorage, out.localStorage);
      dump(window.sessionStorage, out.sessionStorage);
      try { out.cookie_present = !!document.cookie; } catch (_) {}
      return out;
    },

    wipeStorage: function () {
      try { window.localStorage.clear(); } catch (_) {}
      try { window.sessionStorage.clear(); } catch (_) {}
    },

    loadStorage: function (st) {
      if (!st) return false;
      try {
        if (st.localStorage) {
          window.localStorage.clear();
          for (var k in st.localStorage) {
            var v = st.localStorage[k];
            if (typeof v === "string" && v.indexOf("<too_large:") !== 0) window.localStorage.setItem(k, v);
          }
        }
        if (st.sessionStorage) {
          for (var k2 in st.sessionStorage) window.sessionStorage.setItem(k2, st.sessionStorage[k2]);
        }
        return true;
      } catch (e) { return false; }
    },
    errors: function () {
      var e = window.__GSPP_ERRORS__ || [];
      var out = [];
      for (var i = 0; i < Math.min(e.length, 20); i++) out.push(canonical(e[i]));
      return { count: e.length, items: out };
    },
    warn: function () { return (window.__GSPP_WARN__ || []).slice(0, 10); }
  };

  window.__GSPP__ = api;

  // 让游戏作者可以用更顺手的写法
  window.GSPP = api;
})();
