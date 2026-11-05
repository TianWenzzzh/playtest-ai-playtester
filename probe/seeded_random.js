/*
 * GSPP SeededRandom —— 把 Math.random 换成可播种的确定性伪随机源
 *
 * 与 virtual_clock.js 一起在页脚本之前注入。
 * 没有它，"随机种子"这个字段就没有意义：游戏里的 Math.random() 吃的是系统熵，
 * 同一个 seed 重跑会得到完全不同的关卡，复现包立刻作废。
 *
 * 算法：mulberry32。选它是因为 32 位整数运算、跨引擎逐位一致
 * （JS 的 double 乘法和 Math.imul 在 V8/SpiderMonkey/JSC 上都是精确的），
 * 且在 Spark(arm64) 与创空间(x86) 上结果相同 —— 这是跨机复现的前提。
 */
(function () {
  "use strict";
  if (window.__GSPP_RANDOM__) return;

  var seed = 0;
  var calls = 0;
  var nativeRandom = Math.random;

  function mulberry32(a) {
    return function () {
      a |= 0;
      a = (a + 0x6d2b79f5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  var gen = mulberry32(0);

  Math.random = function () {
    calls += 1;
    return gen();
  };

  window.__GSPP_RANDOM__ = {
    version: "0.1",
    algorithm: "mulberry32",
    seed: function (s) {
      seed = (s | 0) ^ 0x9e3779b9;
      gen = mulberry32(seed);
      calls = 0;
      return seed;
    },
    currentSeed: function () { return seed; },
    state: function () { return { seed: seed, calls: calls }; },
    native: nativeRandom
  };

  // 有些游戏用 crypto.getRandomValues 做随机（不可控），标记出来而不是静默失败
  if (window.crypto && window.crypto.getRandomValues) {
    var nativeCgrv = window.crypto.getRandomValues.bind(window.crypto);
    window.crypto.getRandomValues = function (buf) {
      (window.__GSPP_WARN__ = window.__GSPP_WARN__ || []).push("crypto.getRandomValues 被调用，该处随机不受种子控制");
      return nativeCgrv(buf);
    };
  }
})();
