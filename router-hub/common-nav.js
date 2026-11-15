/* =====================================================================
 * common-nav.js —— 全站顶栏注入 + 心情换肤 + 聚光灯/波纹特效 + 无刷新视图切换
 * ===================================================================== */
(function () {
  'use strict';

  var THEMES = ['hypercar', 'prism', 'gold', 'rainbow', 'liquid-glass', 'forest-breath'];
  var THEME_KEY = 'hwmon_theme';
  var THEME_LABEL = {
    hypercar: '曜夜·超跑熔岩',
    prism: '幻彩·液态棱镜',
    gold: '香槟·黑金奢华',
    rainbow: '极昼·全息极光',
    'liquid-glass': '澄光·液态玻璃',
    'forest-breath': '森息·林间呼吸'
  };
  var PALETTES = {
    hypercar: {
      stops: ['#a82a00', '#e04a00', '#ff5e1a', '#ffb347', '#ff8a4c'],
      warn: '#ffb347', bad: '#ff3b5c', cpu: '#ffb347', gpu: '#ff5e1a',
      track: 'rgba(255,255,255,.10)', lineCap: 'round'
    },
    prism: {
      stops: ['#7c3aed', '#a855f7', '#d946ef', '#ec4899', '#f472b6', '#67e8f9', '#818cf8'],
      warn: '#fbbf24', bad: '#fb7185', cpu: '#22d3ee', gpu: '#ec4899',
      track: 'rgba(255,255,255,.10)', lineCap: 'round'
    },
    gold: {
      stops: ['#8a6420', '#b8893a', '#e5c17b', '#f6e2b3', '#c99742'],
      warn: '#fbbf24', bad: '#fb7185', cpu: '#2dd4bf', gpu: '#e5c17b',
      track: 'rgba(229,193,123,.18)', lineCap: 'round'
    },
    rainbow: {
      stops: ['#7dd3fc', '#93c5fd', '#a5b4fc', '#c4b5fd', '#d8b4fe', '#f0abfc', '#f9a8d4', '#fda4af', '#fecdd3'],
      warn: '#fbbf24', bad: '#fb7185', cpu: '#34d399', gpu: '#ff5e7e',
      track: 'rgba(255,255,255,.12)', lineCap: 'round'
    }
  };

  PALETTES['liquid-glass'] = {
    stops: ['#8CB9D8', '#9ADFFF', '#C9F0FF', '#C5C0FF'],
    warn: '#FFD080', bad: '#FF8E9E', ok: '#75D8AB', cpu: '#9ADFFF', gpu: '#C5C0FF',
    track: 'rgba(203,226,250,.16)', lineCap: 'round',
    axis: '#B3C5D8', muted: '#8CA3BB', grid: 'rgba(195,220,245,.12)',
    series: ['#9ADFFF', '#C5C0FF', '#F6CA95', '#A9EEE6']
  };
  PALETTES['forest-breath'] = {
    stops: ['#72A888', '#A5D6A7', '#D6DEA5', '#81C9B5'],
    warn: '#FFD080', bad: '#FF8E9E', ok: '#75D8AB', cpu: '#A5D6A7', gpu: '#D8C3EF',
    track: 'rgba(188,220,191,.16)', lineCap: 'round',
    axis: '#B4C8B8', muted: '#91AD9C', grid: 'rgba(190,219,189,.11)',
    series: ['#A5D6A7', '#D8C3EF', '#E6CA8B', '#8BD2D5']
  };

  var currentTheme = 'hypercar';
  var motionRequested = 'auto', visualReady = false, spotFrame = null;
  var motionReduce, motionNarrow, motionCoarse, motionHover;
  var THEME_DESC = {
    hypercar: '曜黑熔岩 · 热流推进', prism: '液态棱镜 · 流体透色', gold: '黑金奢华 · 浮金游丝',
    rainbow: '全息极光 · 光幕舒展', 'liquid-glass': '澄光玻璃 · 冷银折射', 'forest-breath': '森息林间 · 呼吸薄雾'
  };
  function isMaterialTheme() { return THEMES.indexOf(currentTheme) >= 0; }
  function readMotion() {
    try {
      var m = window.localStorage.getItem('hwmon_motion');
      if (['auto', 'full', 'lite', 'off'].indexOf(m) >= 0) { return m; }
    } catch (e) {}
    return 'full';
  }
  function syncVisualState() {
    if (!visualReady || !document.body) { return; }
    var connection = navigator.connection;
    var effective = motionRequested;
    if (motionReduce.matches) { effective = 'off'; }
    else if (effective === 'auto') {
      effective = (motionNarrow.matches || motionCoarse.matches || (connection && connection.saveData)) ? 'lite' : 'full';
    }
    document.body.setAttribute('data-motion', motionRequested);
    document.body.setAttribute('data-motion-effective', effective);
    document.body.setAttribute('data-fx-paused', String(document.hidden));
    var control = document.getElementById('hwMotionSelect');
    var reason = motionReduce.matches ? '系统减少动态已开启' : (document.hidden ? '后台暂停' : (effective === 'lite' ? '精简氛围' : effective === 'off' ? '动效已关闭' : '背景缓缓流动'));
    if (control) { control.value = motionRequested; control.title = '实际动效：' + effective + ' · ' + reason; }
    var readout = document.getElementById('hwMotionState');
    if (readout) { readout.textContent = reason; readout.title = '系统减少动态设置优先；当前实际档位：' + effective; }
    if (!allowPointerEffects()) {
      if (spotFrame !== null) {
        if (window.requestAnimationFrame && window.cancelAnimationFrame) { window.cancelAnimationFrame(spotFrame); }
        else if (nativeClearTimeout) { nativeClearTimeout(spotFrame); }
      }
      spotFrame = null; spotQueued = false; spotTarget = null;
      var ripples = document.querySelectorAll('.hw-ripple');
      for (var i = 0; i < ripples.length; i++) { if (ripples[i].parentNode) { ripples[i].parentNode.removeChild(ripples[i]); } }
    }
  }
  function allowPointerEffects() {
    if (!isMaterialTheme()) { return !document.hidden; }
    return visualReady && !document.hidden && !motionCoarse.matches && motionHover.matches && document.body.getAttribute('data-motion-effective') === 'full';
  }
  function initializeVisualState() {
    if (visualReady) { return; }
    motionRequested = readMotion();
    motionReduce = window.matchMedia('(prefers-reduced-motion: reduce)');
    motionNarrow = window.matchMedia('(max-width: 768px)');
    motionCoarse = window.matchMedia('(pointer: coarse)');
    motionHover = window.matchMedia('(hover: hover)');
    [motionReduce, motionNarrow, motionCoarse, motionHover].forEach(function (mq) {
      if (mq.addEventListener) { mq.addEventListener('change', syncVisualState); }
      else if (mq.addListener) { mq.addListener(syncVisualState); }
    });
    if (navigator.connection && navigator.connection.addEventListener) { navigator.connection.addEventListener('change', syncVisualState); }
    document.addEventListener('visibilitychange', syncVisualState);
    window.addEventListener('resize', function () { placeThemeGallery(); });
    window.addEventListener('scroll', function () {
      var g = document.getElementById('hwThemeGallery');
      if (g && !g.hidden) { closeThemeGallery(false); }
    }, { passive: true });
    document.addEventListener('change', function (e) {
      if (!e.target || e.target.id !== 'hwMotionSelect') { return; }
      var value = e.target.value;
      if (['auto', 'full', 'lite', 'off'].indexOf(value) < 0) { return; }
      motionRequested = value;
      try { window.localStorage.setItem('hwmon_motion', value); } catch (err) {}
      syncVisualState();
    });
    visualReady = true;
    syncVisualState();
  }

  function readTheme() {
    try {
      var t = window.localStorage ? window.localStorage.getItem(THEME_KEY) : null;
      if (t && THEMES.indexOf(t) >= 0) { return t; }
    } catch (e) {}
    return 'hypercar';
  }

  function themeLabel(t) { return THEME_LABEL[t] || THEME_LABEL.hypercar; }

  window.getHwmonThemePalette = function () {
    var p = PALETTES[currentTheme] || PALETTES.hypercar;
    return {
      theme: currentTheme,
      stops: p.stops.slice(),
      warn: p.warn,
      bad: p.bad,
      cpu: p.cpu,
      gpu: p.gpu,
      track: p.track,
      lineCap: p.lineCap,
      ok: p.ok || '#34d399', axis: p.axis || '#94a0b2', muted: p.muted || '#667286',
      grid: p.grid || 'rgba(255,255,255,.045)',
      series: p.series ? p.series.slice() : [p.cpu, p.gpu, p.warn, '#60a5fa']
    };
  };
  window.getHwmonTheme = function () { return currentTheme; };

  function applyTheme(t) {
    if (THEMES.indexOf(t) < 0) { t = 'hypercar'; }
    currentTheme = t;
    if (document.body) { document.body.setAttribute('data-theme', t); }
    updateMoodBtn();
    syncVisualState();
  }

  function setTheme(t, silent) {
    applyTheme(t);
    try { if (window.localStorage) { window.localStorage.setItem(THEME_KEY, currentTheme); } } catch (e) {}
    if (!silent) {
      try {
        window.dispatchEvent(new CustomEvent('hwmon:theme', { detail: { theme: currentTheme } }));
      } catch (e) {}
    }
  }
  window.setHwmonTheme = function (t) { setTheme(t); };

  function updateMoodBtn() {
    var btn = document.getElementById('moodBtn');
    if (!btn) { return; }
    var name = btn.querySelector('.hw-theme-current');
    if (name) { name.textContent = themeLabel(currentTheme); }
    var preview = btn.querySelector('.hw-theme-thumb');
    if (preview) { preview.setAttribute('data-preview', currentTheme); }
    var choices = document.querySelectorAll('#hwThemeMenu [data-theme-choice]');
    for (var i = 0; i < choices.length; i++) {
      var selected = choices[i].getAttribute('data-theme-choice') === currentTheme;
      choices[i].setAttribute('aria-selected', String(selected));
      choices[i].tabIndex = selected ? 0 : -1;
    }
    btn.setAttribute('title', '当前主题：' + themeLabel(currentTheme) + ' · 打开主题画廊');
  }

  function closeThemeGallery(restoreFocus) {
    var gallery = document.getElementById('hwThemeGallery'), trigger = document.getElementById('moodBtn');
    if (!gallery || !trigger) { return; }
    gallery.hidden = true; trigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus) { trigger.focus(); }
  }
  function placeThemeGallery() {
    var gallery = document.getElementById('hwThemeGallery'), trigger = document.getElementById('moodBtn');
    if (!gallery || !trigger || gallery.hidden) { return; }
    var r = trigger.getBoundingClientRect();
    var vw = window.innerWidth || document.documentElement.clientWidth;
    var vh = window.innerHeight || document.documentElement.clientHeight;
    var w = Math.min(352, vw - 24);
    var left = Math.min(Math.max(12, r.right - w), vw - 12 - w);
    var top = r.bottom + 8;
    var maxH = Math.max(220, vh - top - 16);
    gallery.style.left = Math.round(left) + 'px';
    gallery.style.right = 'auto';
    gallery.style.top = Math.round(top) + 'px';
    gallery.style.width = Math.round(w) + 'px';
    gallery.style.maxHeight = Math.round(maxH) + 'px';
  }
  function toggleThemeGallery() {
    var gallery = document.getElementById('hwThemeGallery'), trigger = document.getElementById('moodBtn');
    if (!gallery || !trigger) { return; }
    if (!gallery.hidden) { closeThemeGallery(false); return; }
    if (gallery.parentNode !== document.body) { document.body.appendChild(gallery); }
    gallery.hidden = false; trigger.setAttribute('aria-expanded', 'true');
    placeThemeGallery();
    var selected = gallery.querySelector('[aria-selected="true"]');
    if (selected) { selected.focus(); }
  }
  function onGalleryClick(e) {
    var choice = e.target.closest && e.target.closest('[data-theme-choice]');
    if (choice && choice.closest('#hwThemeMenu')) {
      setTheme(choice.getAttribute('data-theme-choice')); closeThemeGallery(true); return;
    }
    if (e.target.closest && e.target.closest('#hwThemeGallery')) { return; }
    if (!(e.target.closest && e.target.closest('.hw-theme-picker'))) { closeThemeGallery(false); }
  }
  function onGalleryKey(e) {
    var trigger = document.getElementById('moodBtn'), gallery = document.getElementById('hwThemeGallery');
    if (!trigger || !gallery) { return; }
    if (e.target === trigger && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault(); if (gallery.hidden) { toggleThemeGallery(); } return;
    }
    if (gallery.hidden) { return; }
    if (e.key === 'Escape') { e.preventDefault(); closeThemeGallery(true); return; }
    if (e.key === 'Tab') { closeThemeGallery(true); return; }
    if (!gallery.contains(e.target)) { return; }
    var items = gallery.querySelectorAll('[data-theme-choice]');
    var index = Array.prototype.indexOf.call(items, document.activeElement), next = index;
    if (e.key === 'ArrowDown') { next = (index + 1) % items.length; }
    else if (e.key === 'ArrowUp') { next = (index - 1 + items.length) % items.length; }
    else if (e.key === 'Home') { next = 0; }
    else if (e.key === 'End') { next = items.length - 1; }
    else { return; }
    e.preventDefault(); items[next].focus();
  }

  var EMBLEM = '<div class="emblem-box" aria-hidden="true">' +
    '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">' +
    '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg></div>';

  function brandHtml(title, subtitle) {
    return EMBLEM + '<div class="brand-text">' + escapeHtml(title || '') +
      (subtitle ? '<em>' + escapeHtml(subtitle) + '</em>' : '') + '</div>';
  }

  function ensureAmbient() {
    if (!document.body || document.querySelector('.ambient-bg')) { return; }
    var box = document.createElement('div');
    box.className = 'ambient-bg';
    box.setAttribute('aria-hidden', 'true');
    box.innerHTML = '<div class="orb orb-1"></div><div class="orb orb-2"></div><div class="orb orb-3"></div>';
    document.body.insertBefore(box, document.body.firstChild);
  }

  /* 鼠标聚光灯追踪 */
  var spotTarget = null, spotX = 0, spotY = 0, spotQueued = false;
  var SPOT_SELECTOR = '.card,.panel,.live-card,.master-card,.gauge-card,.peer,.link,.app,.stat';

  function flushSpotlight() {
    spotQueued = false;
    spotFrame = null;
    if (!allowPointerEffects()) { return; }
    var el = spotTarget;
    if (!el || !el.getBoundingClientRect) { return; }
    var rect = el.getBoundingClientRect();
    el.style.setProperty('--mx', (spotX - rect.left).toFixed(1) + 'px');
    el.style.setProperty('--my', (spotY - rect.top).toFixed(1) + 'px');
  }

  function onSpotlightMove(e) {
    if (!allowPointerEffects()) { return; }
    var el = e.target && e.target.closest ? e.target.closest(SPOT_SELECTOR) : null;
    if (!el) { return; }
    spotTarget = el;
    spotX = e.clientX;
    spotY = e.clientY;
    if (spotQueued) { return; }
    spotQueued = true;
    if (window.requestAnimationFrame) { spotFrame = window.requestAnimationFrame(flushSpotlight); }
    else { spotFrame = nativeSetTimeout(flushSpotlight, 16); }
  }

  /* 点击涟漪特效 */
  var RIPPLE_SELECTOR = '.btn,.tool-link,#moodBtn,.dev,.range-btn,.seg button,.gauge-card,.master-card,.link,.app';
  function onRippleDown(e) {
    if (!allowPointerEffects()) { return; }
    if (e.button !== 0 && e.pointerType === 'mouse') { return; }
    var el = e.target && e.target.closest ? e.target.closest(RIPPLE_SELECTOR) : null;
    if (!el || el.disabled || el.classList.contains('off') || el.id === 'moodBtn') { return; }
    var rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) { return; }
    var size = Math.max(rect.width, rect.height) * 1.1;
    var previous = el.querySelector('.hw-ripple');
    if (previous && previous.parentNode) { previous.parentNode.removeChild(previous); }
    var rip = document.createElement('span');
    rip.className = 'hw-ripple';
    rip.style.width = size + 'px';
    rip.style.height = size + 'px';
    rip.style.left = (e.clientX - rect.left - size / 2) + 'px';
    rip.style.top = (e.clientY - rect.top - size / 2) + 'px';
    el.appendChild(rip);
    nativeSetTimeout(function () {
      if (rip.parentNode) { rip.parentNode.removeChild(rip); }
    }, 580);
  }

  applyTheme(readTheme());

  /* ---- 流动相位补偿 ---------------------------------------------------
     详情面板每 2 秒重建一次行，CSS 动画会跟着从 0 重播，看起来就是
     「走到一半突然跳回起点」。这里在节点插入时补一个负 animation-delay，
     让颜色从当前应有的相位接着走，才真正循环起来。 */
  var flowDuration = 10;
  function readFlowDuration() {
    var n = parseFloat(getComputedStyle(document.body).getPropertyValue('--flow-duration'));
    flowDuration = (isFinite(n) && n > 0) ? n : 10;
  }
  function applyFlowPhase(el, nowSec) {
    if (!el || el.tagName !== 'I' || !el.style || !el.style.width) { return; }
    el.style.animationDelay = (-(nowSec % flowDuration)).toFixed(2) + 's';
  }
  function syncFlowPhase() {
    var t = performance.now() / 1000;
    var list = document.querySelectorAll('i[style*="width"]');
    for (var i = 0; i < list.length; i++) { applyFlowPhase(list[i], t); }
  }
  function startFlowSync() {
    readFlowDuration();
    syncFlowPhase();
    try {
      var mo = new MutationObserver(function (muts) {
        var t = performance.now() / 1000;
        for (var i = 0; i < muts.length; i++) {
          var added = muts[i].addedNodes;
          for (var j = 0; j < added.length; j++) {
            var n = added[j];
            if (n.nodeType !== 1) { continue; }
            if (n.tagName === 'I') { applyFlowPhase(n, t); }
            else if (n.querySelectorAll) {
              var inner = n.querySelectorAll('i[style*="width"]');
              for (var k = 0; k < inner.length; k++) { applyFlowPhase(inner[k], t); }
            }
          }
        }
      });
      mo.observe(document.body, { childList: true, subtree: true });
    } catch (e) {}
    window.addEventListener('hwmon:theme', function () { readFlowDuration(); syncFlowPhase(); });
  }

  var NAV_ITEMS = [
    { label: '硬件监控', href: '/' },
    { label: '模型监控', href: '/llm' },
    { label: '应用监控', href: '/app' },
    { label: '工具箱', href: '/tools' }
  ];

  var VIEW_ROUTES = [
    { href: '/', paths: ['/', '/index.html', '/monitor', '/monitor.html', '/ui', '/ui.html'] },
    { href: '/llm', paths: ['/llm', '/llm.html'] },
    { href: '/app', paths: ['/app', '/app.html'] },
    { href: '/tools', paths: ['/tools', '/tools.html'] }
  ];

  function normalizePath(p) {
    if (!p) { return '/'; }
    if (p.charAt(0) !== '/') { p = '/' + p; }
    if (p.length > 1 && p.charAt(p.length - 1) === '/') { p = p.slice(0, -1); }
    return p || '/';
  }
  function currentPath() { return normalizePath(window.location.pathname); }
  function routeOf(path) {
    var p = normalizePath(path);
    for (var i = 0; i < VIEW_ROUTES.length; i++) {
      if (VIEW_ROUTES[i].paths.indexOf(p) >= 0) { return VIEW_ROUTES[i]; }
    }
    return null;
  }
  function activeHref() { var r = routeOf(currentPath()); return r ? r.href : null; }
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  var nativeSetInterval = window.setInterval ? window.setInterval.bind(window) : null;
  var nativeClearInterval = window.clearInterval ? window.clearInterval.bind(window) : null;
  var nativeSetTimeout = window.setTimeout ? window.setTimeout.bind(window) : null;
  var nativeClearTimeout = window.clearTimeout ? window.clearTimeout.bind(window) : null;
  var nativeFetch = window.fetch ? window.fetch.bind(window) : null;

  var viewEpoch = 1, intervals = [], timeouts = [], winListeners = [], docListeners = [], cleanups = [];

  if (nativeFetch) {
    window.fetch = function (input, init) {
      var gen = viewEpoch;
      return nativeFetch(input, init).then(function (resp) {
        if (gen !== viewEpoch) { return new Promise(function () {}); }
        return resp;
      });
    };
  }

  function removeAt(arr, id) { var i = arr.indexOf(id); if (i >= 0) { arr.splice(i, 1); } }
  function trackInterval(fn, ms) {
    var args = Array.prototype.slice.call(arguments, 2);
    var id = nativeSetInterval.apply(window, [fn, ms].concat(args));
    intervals.push(id);
    return id;
  }
  function trackTimeout(fn, ms) {
    var args = Array.prototype.slice.call(arguments, 2);
    var id = nativeSetTimeout(function () {
      removeAt(timeouts, id);
      if (typeof fn === 'function') { fn.apply(null, args); }
    }, ms);
    timeouts.push(id);
    return id;
  }
  if (nativeSetInterval) { window.setInterval = function () { return trackInterval.apply(null, arguments); }; }
  if (nativeSetTimeout) { window.setTimeout = function () { return trackTimeout.apply(null, arguments); }; }

  window.registerViewTimer = function (id) { if (id != null && intervals.indexOf(id) < 0) { intervals.push(id); } return id; };
  window.registerViewTimeout = function (id) { if (id != null && timeouts.indexOf(id) < 0) { timeouts.push(id); } return id; };
  window.onViewCleanup = function (fn) { if (typeof fn === 'function') { cleanups.push(fn); } };

  var internalAdd = false;
  (function () {
    var origWinAdd = window.addEventListener ? window.addEventListener.bind(window) : null;
    if (origWinAdd) {
      window.addEventListener = function (type, fn, opts) {
        if (!internalAdd && typeof fn === 'function') { winListeners.push({ type: type, fn: fn, opts: opts }); }
        return origWinAdd(type, fn, opts);
      };
    }
    var origDocAdd = document.addEventListener ? document.addEventListener.bind(document) : null;
    if (origDocAdd) {
      document.addEventListener = function (type, fn, opts) {
        if (!internalAdd && typeof fn === 'function') { docListeners.push({ type: type, fn: fn, opts: opts }); }
        return origDocAdd(type, fn, opts);
      };
    }
  })();

  function teardownView() {
    var i;
    for (i = 0; i < intervals.length; i++) { try { nativeClearInterval(intervals[i]); } catch (e) {} }
    intervals = [];
    for (i = 0; i < timeouts.length; i++) { try { nativeClearTimeout(timeouts[i]); } catch (e) {} }
    timeouts = [];
    if (window.removeEventListener) {
      for (i = 0; i < winListeners.length; i++) { try { window.removeEventListener(winListeners[i].type, winListeners[i].fn, winListeners[i].opts); } catch (e) {} }
    }
    winListeners = [];
    if (document.removeEventListener) {
      for (i = 0; i < docListeners.length; i++) { try { document.removeEventListener(docListeners[i].type, docListeners[i].fn, docListeners[i].opts); } catch (e) {} }
    }
    docListeners = [];
    for (i = 0; i < cleanups.length; i++) { try { cleanups[i](); } catch (e) {} }
    cleanups = [];
    viewEpoch++;
  }

  function build() {
    var header = document.querySelector('header.topbar');
    if (!header || header.getAttribute('data-nav-ready') === '1') { return; }

    var title = header.getAttribute('data-title') || '硬件监控';
    var subtitle = header.getAttribute('data-subtitle') || '';
    var active = activeHref();

    var html = '<div class="brand">' + brandHtml(title, subtitle) + '</div>';
    html += '<nav class="nav-links" role="navigation" aria-label="主导航">';
    for (var i = 0; i < NAV_ITEMS.length; i++) {
      var it = NAV_ITEMS[i];
      var cls = (active && it.href === active) ? 'tool-link active' : 'tool-link';
      var cur = (active && it.href === active) ? ' aria-current="page"' : '';
      html += '<a class="' + cls + '"' + cur + ' href="' + escapeHtml(it.href) + '">' + escapeHtml(it.label) + '</a>';
    }
    html += '</nav>';
    html += '<div class="hw-theme-picker"><button id="moodBtn" class="hw-gallery-trigger" type="button" aria-haspopup="listbox" aria-expanded="false" aria-controls="hwThemeMenu">' +
      '<span class="hw-theme-thumb" data-preview="' + currentTheme + '" aria-hidden="true"><i></i><b></b></span>' +
      '<span class="hw-trigger-copy"><small>外观画廊</small><span class="hw-theme-current">' + escapeHtml(themeLabel(currentTheme)) + '</span></span><span class="hw-gallery-chevron" aria-hidden="true">⌄</span></button>';
    html += '<div id="hwThemeGallery" class="hw-theme-gallery" hidden><div class="hw-gallery-heading"><strong>选择你的氛围</strong><span>六种材质 · 同一份秩序</span></div>' +
      '<div id="hwThemeMenu" role="listbox" aria-label="视觉主题">';
    for (var ti = 0; ti < THEMES.length; ti++) {
      var themeId = THEMES[ti];
      html += '<button type="button" role="option" class="hw-theme-option" data-theme-choice="' + themeId + '" aria-selected="false" tabindex="-1">' +
        '<span class="hw-theme-thumb" data-preview="' + themeId + '" aria-hidden="true"><i></i><b></b></span>' +
        '<span class="hw-option-copy"><strong>' + escapeHtml(themeLabel(themeId)) + '</strong><small>' + THEME_DESC[themeId] + '</small></span><span class="hw-theme-check" aria-hidden="true">✓</span></button>';
    }
    html += '</div><div class="hw-gallery-footer">方向键浏览 · Enter 选择 · Esc 收起</div></div></div>';
    var mv = readMotion(), opt = '';
    [['auto', '自动'], ['full', '完整'], ['lite', '精简'], ['off', '关闭']].forEach(function (o) {
      opt += '<option value="' + o[0] + '"' + (mv === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
    });
    html += '<label class="hw-motion-control" title="六套主题环境动效">动效<select id="hwMotionSelect" aria-label="六套主题环境动效档位">' + opt +
      '</select><span id="hwMotionState" role="status" aria-live="polite"></span></label>';
    html += '<div class="live" id="live"><i></i><span id="liveTxt">正在连接</span></div>';

    var oldGallery = document.getElementById('hwThemeGallery');
    if (oldGallery && oldGallery.parentNode) { oldGallery.parentNode.removeChild(oldGallery); }
    header.innerHTML = html;
    header.setAttribute('data-nav-ready', '1');

    var mood = document.getElementById('moodBtn');
    if (mood) { mood.addEventListener('click', function () { toggleThemeGallery(); }); }
    updateMoodBtn();
  }

  function updateTopbarChrome(title, subtitle, href) {
    var header = document.querySelector('header.topbar');
    if (!header) { return; }
    if (title) { header.setAttribute('data-title', title); }
    if (subtitle != null) { header.setAttribute('data-subtitle', subtitle); }
    var brand = header.querySelector('.brand');
    if (brand) {
      var html = brandHtml(title || header.getAttribute('data-title') || '', subtitle != null ? subtitle : header.getAttribute('data-subtitle') || '');
      if (brand.innerHTML !== html) { brand.innerHTML = html; }
    }
    var links = header.querySelectorAll('.tool-link');
    for (var i = 0; i < links.length; i++) {
      var a = links[i], on = !!href && a.getAttribute('href') === href;
      if (on) { a.classList.add('active'); a.setAttribute('aria-current', 'page'); }
      else { a.classList.remove('active'); a.removeAttribute('aria-current'); }
    }
  }

  function setTopbarLive(state, text) {
    var live = document.getElementById('live'), txt = document.getElementById('liveTxt');
    if (!live || !txt) { return; }
    var s = (state === 'ok' || state === 'warn' || state === 'bad') ? state : 'warn';
    var cls = 'live ' + s;
    if (live.className !== cls) { live.className = cls; }
    var t = (text == null) ? '' : String(text);
    if (txt.textContent !== t) { txt.textContent = t; }
  }
  window.setTopbarLive = setTopbarLive;

  var loading = false, pendingUrl = null, fallbackTried = {};
  function hardNavigate(url) { try { window.location.href = url; } catch (e) { window.location.assign(url); } }
  function safeHardNavigate(url) { if (fallbackTried[url]) { return; } fallbackTried[url] = true; hardNavigate(url); }
  function pickViewStyle(doc) { return doc.querySelector('style[data-view-style]'); }

  function collectViewNodes(doc) {
    var out = [], kids = doc.body ? doc.body.children : [];
    for (var i = 0; i < kids.length; i++) {
      var el = kids[i], tag = el.tagName;
      if (tag === 'HEADER' || tag === 'SCRIPT' || tag === 'LINK') { continue; }
      out.push(el);
    }
    return out;
  }

  function collectViewScripts(doc) {
    var out = [], all = doc.querySelectorAll('script');
    for (var i = 0; i < all.length; i++) {
      var s = all[i];
      if (s.getAttribute('src')) { continue; }
      var code = s.textContent || '';
      if (code.trim()) { out.push(code); }
    }
    return out;
  }

  function runViewScripts(scripts) {
    for (var i = 0; i < scripts.length; i++) {
      try { (new Function(scripts[i])).call(window); }
      catch (err) { if (window.console && console.error) { console.error('[common-nav] 视图脚本执行出错：' + (err && err.message)); } }
    }
  }

  function clearBodyExceptChrome() {
    var keep = [], kids = document.body.children, i;
    for (i = 0; i < kids.length; i++) {
      var el = kids[i];
      if (el.id === 'hwThemeGallery' ||
          (el.tagName === 'HEADER' && el.classList && el.classList.contains('topbar')) ||
          (el.classList && el.classList.contains('liveline')) ||
          (el.classList && el.classList.contains('ambient-bg')) ||
          (el.tagName === 'SCRIPT' && el.getAttribute('src'))) {
        keep.push(el);
      }
    }
    var snapshot = [];
    for (i = 0; i < kids.length; i++) { snapshot.push(kids[i]); }
    for (i = 0; i < snapshot.length; i++) {
      if (keep.indexOf(snapshot[i]) < 0 && snapshot[i].parentNode) {
        snapshot[i].parentNode.removeChild(snapshot[i]);
      }
    }
  }

  function applyView(doc, url) {
    var newTitleEl = doc.querySelector('title');
    if (newTitleEl && newTitleEl.textContent) { document.title = newTitleEl.textContent; }

    var newStyleEl = pickViewStyle(doc), curStyle = document.querySelector('style[data-view-style]');
    if (newStyleEl) {
      var css = newStyleEl.textContent || '';
      if (!curStyle) { curStyle = document.createElement('style'); curStyle.setAttribute('data-view-style', '1'); document.head.appendChild(curStyle); }
      if (curStyle.textContent !== css) { curStyle.textContent = css; }
    } else if (curStyle && curStyle.textContent) {
      curStyle.textContent = '';
    }

    teardownView();
    clearBodyExceptChrome();

    var nodes = collectViewNodes(doc);
    for (var i = 0; i < nodes.length; i++) {
      document.body.appendChild(document.importNode(nodes[i], true));
    }

    var newHeader = doc.querySelector('header.topbar');
    var title = newHeader ? (newHeader.getAttribute('data-title') || '') : '';
    var subtitle = newHeader ? (newHeader.getAttribute('data-subtitle') || '') : '';
    var route = routeOf(url);
    updateTopbarChrome(title, subtitle, route ? route.href : null);

    runViewScripts(collectViewScripts(doc));
    try { window.scrollTo(0, 0); } catch (e) {}
  }

  function loadView(url, pushState, force) {
    var target = normalizePath(url);
    if (loading) { pendingUrl = target; return; }
    var current = normalizePath(window.location.pathname);
    if (!force && target === current && !pendingUrl) {
      var same = routeOf(target);
      if (same) { updateTopbarChrome(null, null, same.href); }
      return;
    }
    loading = true;
    fetch(target, { cache: 'no-store', credentials: 'same-origin' })
      .then(function (resp) { if (!resp.ok) { throw new Error('HTTP ' + resp.status); } return resp.text(); })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        if (!doc.querySelector('main.wrap') && !doc.body) { throw new Error('目标页结构异常'); }
        applyView(doc, target);
        if (pushState !== false) { try { window.history.pushState({ nav: target }, '', target); } catch (e) {} }
        loading = false;
        if (pendingUrl) { var next = pendingUrl; pendingUrl = null; loadView(next, true); }
      })
      .catch(function () {
        loading = false; pendingUrl = null; safeHardNavigate(target);
      });
  }

  function onNavClick(e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) { return; }
    var a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
    if (!a || (a.target && a.target !== '' && a.target !== '_self') || a.hasAttribute('download')) { return; }
    var raw = a.getAttribute('href');
    if (!raw || raw.charAt(0) === '#' || /^(mailto|tel|javascript):/i.test(raw)) { return; }
    var url;
    try { url = new URL(raw, window.location.href); } catch (err) { return; }
    if (url.origin !== window.location.origin || !routeOf(url.pathname)) { return; }
    e.preventDefault();
    loadView(url.pathname + url.search, true);
  }

  function onPopState() {
    var path = window.location.pathname + window.location.search;
    if (!routeOf(path)) { return; }
    loadView(path, false, true);
  }

  function boot() {
    applyTheme(currentTheme);
    ensureAmbient();
    build();
    internalAdd = true;
    initializeVisualState();
    document.addEventListener('click', onGalleryClick);
    document.addEventListener('keydown', onGalleryKey);
    document.addEventListener('click', onNavClick);
    window.addEventListener('popstate', onPopState);
    document.addEventListener('pointermove', onSpotlightMove, { passive: true });
    document.addEventListener('pointerdown', onRippleDown, { passive: true });
    startFlowSync();
    internalAdd = false;
  }

  window.buildTopbar = build;
  window.navigateView = function (url) { loadView(url, true); };

  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', boot); }
  else { boot(); }
})();
