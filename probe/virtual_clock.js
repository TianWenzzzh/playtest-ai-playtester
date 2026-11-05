/*
 * GSPP VirtualClock —— 浏览器游戏的确定性时间源
 *
 * 必须在游戏自己的脚本之前执行（Playwright addInitScript）。
 * 接管 rAF / setTimeout / setInterval / performance.now / Date.now，
 * 把墙钟驱动的游戏循环改成由宿主按固定帧栅格手动推进。
 *
 * 为什么这样做：赛题 C 要求输出「初始状态 + 随机种子 + 完整动作序列」并可复现。
 * 只要游戏循环还吃真实墙钟，同一串动作重跑就会得到不同结果，"复现"这个词就不成立。
 * 而要求作者把游戏重构成纯函数状态机是不现实的 —— 所以把不确定性从下面接管掉。
 *
 * 代价（诚实记录）：时间被量化到帧栅格，非整帧的 setTimeout 会被推迟到下一个帧边界触发。
 * 这个量化正是确定性的来源，不是缺陷。
 */
(function () {
  "use strict";
  if (window.__GSPP_CLOCK__) return;

  var FRAME_MS = 16;
  var EPOCH = 1700000000000;

  var vt = 0;
  var frames = 0;
  var seq = 0;
  var timers = [];
  var rafQueue = [];
  var nextId = 1;

  var nativeRaf = window.requestAnimationFrame
    ? window.requestAnimationFrame.bind(window)
    : null;

  function byDueThenSeq(a, b) {
    if (a.due !== b.due) return a.due - b.due;
    return a.seq - b.seq;
  }

  function insertTimer(t) {
    var i = timers.length;
    while (i > 0 && byDueThenSeq(timers[i - 1], t) > 0) i--;
    timers.splice(i, 0, t);
  }

  function runDueTimers() {
    var guard = 0;
    while (timers.length && timers[0].due <= vt && guard++ < 100000) {
      var t = timers.shift();
      if (t.cancelled) continue;
      if (t.interval) {
        t.due = vt + Math.max(t.delay, FRAME_MS);
        t.seq = ++seq;
        insertTimer(t);
      }
      try {
        t.fn.apply(null, t.args);
      } catch (e) {
        reportError("timer", e);
      }
    }
  }

  function reportError(kind, e) {
    try {
      (window.__GSPP_ERRORS__ = window.__GSPP_ERRORS__ || []).push({
        kind: kind,
        message: String((e && e.message) || e),
        stack: String((e && e.stack) || ""),
        atFrame: frames
      });
    } catch (_) {}
  }

  function stepFrame() {
    frames += 1;
    vt = frames * FRAME_MS;

    runDueTimers();

    // 快照本帧回调：帧内新注册的 rAF 排到下一帧，避免无限自增在同一帧里跑光
    var batch = rafQueue;
    rafQueue = [];
    batch.sort(byDueThenSeq);
    for (var i = 0; i < batch.length; i++) {
      if (batch[i].cancelled) continue;
      try {
        batch[i].fn(vt);
      } catch (e) {
        reportError("raf", e);
      }
    }
  }

  window.requestAnimationFrame = function (cb) {
    var item = { due: vt + FRAME_MS, seq: ++seq, fn: cb, id: nextId++, cancelled: false };
    rafQueue.push(item);
    return item.id;
  };

  window.cancelAnimationFrame = function (id) {
    for (var i = 0; i < rafQueue.length; i++) if (rafQueue[i].id === id) rafQueue[i].cancelled = true;
  };

  window.webkitRequestAnimationFrame = window.requestAnimationFrame;

  function findTimer(id) {
    for (var i = 0; i < timers.length; i++) if (timers[i].id === id) return timers[i];
    return null;
  }

  window.setTimeout = function (fn, delay) {
    var args = Array.prototype.slice.call(arguments, 2);
    var d = Math.max(0, Number(delay) || 0);
    var t = {
      id: nextId++, due: vt + d, seq: ++seq, fn: typeof fn === "function" ? fn : function () { (0, eval)(fn); },
      args: args, delay: d, interval: false, cancelled: false
    };
    insertTimer(t);
    return t.id;
  };

  window.setInterval = function (fn, delay) {
    var args = Array.prototype.slice.call(arguments, 2);
    var d = Math.max(FRAME_MS, Number(delay) || FRAME_MS);
    var t = {
      id: nextId++, due: vt + d, seq: ++seq, fn: typeof fn === "function" ? fn : function () { (0, eval)(fn); },
      args: args, delay: d, interval: true, cancelled: false
    };
    insertTimer(t);
    return t.id;
  };

  window.clearTimeout = function (id) {
    var t = findTimer(id);
    if (t) t.cancelled = true;
  };
  window.clearInterval = window.clearTimeout;

  var nativePerfNow = window.performance && window.performance.now
    ? window.performance.now.bind(window.performance)
    : function () { return Date.now() - EPOCH; };

  window.performance.now = function () { return vt; };

  var NativeDate = Date;
  function FakeDate(y, m, d, h, mi, s, ms) {
    if (arguments.length === 0) return new NativeDate(EPOCH + vt);
    return arguments.length === 1 ? new NativeDate(y) : new NativeDate(y, m, d, h, mi, s, ms);
  }
  FakeDate.prototype = NativeDate.prototype;
  FakeDate.now = function () { return EPOCH + vt; };
  FakeDate.parse = NativeDate.parse;
  FakeDate.UTC = NativeDate.UTC;
  window.Date = FakeDate;

  // 有些游戏在 document.hidden 时暂停主循环；确定性环境下强制视为可见
  try {
    Object.defineProperty(document, "visibilityState", { get: function () { return "visible"; }, configurable: true });
    Object.defineProperty(document, "hidden", { get: function () { return false; }, configurable: true });
  } catch (_) {}

  window.__GSPP_CLOCK__ = {
    version: "0.1",
    frameMs: function () { return FRAME_MS; },
    setFrameMs: function (ms) { FRAME_MS = Math.max(1, Math.round(ms)); },
    // 推进 n 帧；返回推进后的虚拟毫秒
    advance: function (n) {
      var k = Math.max(0, Math.round(n || 0));
      for (var i = 0; i < k; i++) stepFrame();
      return vt;
    },
    advanceMs: function (ms) { return window.__GSPP_CLOCK__.advance(Math.ceil(Math.max(0, ms) / FRAME_MS)); },
    // 把当前所有到期任务清空（用于"启动期"：加载后先跑掉初始化回调）
    // 精确推进 n 帧，不做"队列空了就提前停"的优化：
    // 提前 break 会让不同游戏的起始帧数不一致，等于把不确定性放回起点。
    drain: function (n) {
      var limit = Math.max(0, Math.round(n || 0));
      for (var i = 0; i < limit; i++) stepFrame();
      return frames;
    },
    // 回卷时间但保留已排队的 rAF 主循环。
    // 为什么不能连 rafQueue 一起清：游戏的 requestAnimationFrame(loop) 链一旦断掉，
    // 主循环就永久停了，而我们不能要求每个作者都为测试工具重写循环。
    // 定时器则必须清 —— 上一局排下的 setTimeout 不该漏进下一局。
    rewind: function () {
      vt = 0; frames = 0; seq = 0; timers = [];
      window.__GSPP_ERRORS__ = [];
    },
    state: function () {
      return {
        vt: vt,
        frames: frames,
        frameMs: FRAME_MS,
        timers: timers.length,
        rafPending: rafQueue.length,
        seq: seq
      };
    },
    reset: function () {
      vt = 0; frames = 0; seq = 0; timers = []; rafQueue = [];
      window.__GSPP_ERRORS__ = [];
    },
    nativeRaf: nativeRaf
  };

  window.__GSPP_ERRORS__ = [];
})();
