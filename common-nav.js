/* =====================================================================
 * common-nav.js —— 全站顶栏注入 + 心情换肤 + 聚光灯/波纹特效 + 无刷新视图切换
 * ===================================================================== */
(function () {
  'use strict';

  /* ---- 皮肤注册表 v8 ------------------------------------------------
     12 款皮肤分两类：
     A. 独立物理材质引擎（8 款）：隐藏 .orb，由 .hw-material-canvas 接管，
        每款一套独立的几何生成与光学着色算法，互不共用。
     B. 原版保留（4 款）：100% 保留原有 CSS .orb 氛围，只在顶层叠加
        专属鼠标跟随笔触与专属点击特效。
     旧存档里的 hypercar / gold 会被映射到替代皮肤，不丢设置。 */
  var LEGACY_THEME_MAP = { 'hypercar': 'sunrise-gold', 'gold': 'ferrofluid-chrome' };
  var THEMES = [
    'blue-porcelain', 'sunrise-gold', 'ink-wash', 'ferrofluid-chrome',
    'black-opal', 'gemini-nebula', 'gemini-porcelain', 'gemini-halftone',
    'prism', 'rainbow', 'liquid-glass', 'forest-breath'
  ];
  var CANVAS_SKINS = {
    'blue-porcelain': true, 'sunrise-gold': true, 'ink-wash': true,
    'ferrofluid-chrome': true, 'black-opal': true, 'gemini-nebula': true,
    'gemini-porcelain': true, 'gemini-halftone': true
  };
  var LIGHT_SKINS = { 'blue-porcelain': true, 'ink-wash': true, 'gemini-porcelain': true };
  var THEME_KEY = 'hwmon_theme';
  var DEFAULT_THEME = 'sunrise-gold';
  var THEME_LABEL = {
    'blue-porcelain': '天青·元青花釉',
    'sunrise-gold': '日照·梅里金山',
    'ink-wash': '烟雨·徽墨宣纸',
    'ferrofluid-chrome': '磁流·液态水银',
    'black-opal': '欧泊·黑曜变彩',
    'gemini-nebula': '星云·双子流体',
    'gemini-porcelain': '晨曦·白瓷彩雾',
    'gemini-halftone': '极光·点阵波束',
    prism: '幻彩·液态棱镜',
    rainbow: '极昼·全息极光',
    'liquid-glass': '澄光·液态玻璃',
    'forest-breath': '森息·林间呼吸'
  };
  function resolveTheme(t) {
    if (!t) { return DEFAULT_THEME; }
    var mapped = LEGACY_THEME_MAP[t] || t;
    return THEMES.indexOf(mapped) >= 0 ? mapped : DEFAULT_THEME;
  }
  /* Canvas 图表取色板：与皮肤材质同源，换肤后曲线/环图跟着换色。
     亮色材质皮肤专用深色轴线与网格，避免浅底上出现显影不足的浅灰线。 */
  var PALETTES = {
    'sunrise-gold': {
      stops: ['#3b5998', '#f59e0b', '#ffd166', '#ff5e2a', '#ffe49e'],
      warn: '#fbbf24', bad: '#ef4444', ok: '#34d399', cpu: '#f59e0b', gpu: '#ffd166',
      track: 'rgba(255,215,110,.14)', lineCap: 'round',
      axis: '#b4c0d9', muted: '#7684a3', grid: 'rgba(255,215,110,.09)',
      series: ['#f59e0b', '#ffd166', '#60a5fa', '#ff5e2a']
    },
    'ferrofluid-chrome': {
      stops: ['#64748b', '#cbd5e1', '#f8fafc', '#93c5fd'],
      warn: '#f59e0b', bad: '#f87171', ok: '#34d399', cpu: '#cbd5e1', gpu: '#93c5fd',
      track: 'rgba(203,213,225,.14)', lineCap: 'round',
      axis: '#94a3b8', muted: '#64748b', grid: 'rgba(203,213,225,.08)',
      series: ['#cbd5e1', '#93c5fd', '#67e8f9', '#f8fafc']
    },
    'black-opal': {
      stops: ['#00e676', '#00b0ff', '#9b51e0', '#ff5252'],
      warn: '#ffab40', bad: '#ff5252', ok: '#00e676', cpu: '#00b0ff', gpu: '#00e676',
      track: 'rgba(0,230,118,.12)', lineCap: 'round',
      axis: '#9ab0c9', muted: '#5d708a', grid: 'rgba(0,230,118,.08)',
      series: ['#00e676', '#00b0ff', '#9b51e0', '#ffab40']
    },
    'gemini-nebula': {
      stops: ['#4285f4', '#9b72cb', '#d96570', '#f49c46'],
      warn: '#fbbc04', bad: '#ea4335', ok: '#34a853', cpu: '#4285f4', gpu: '#d96570',
      track: 'rgba(168,199,250,.12)', lineCap: 'round',
      axis: '#9aa8c7', muted: '#687594', grid: 'rgba(168,199,250,.08)',
      series: ['#4285f4', '#9b72cb', '#d96570', '#f49c46']
    },
    'gemini-halftone': {
      stops: ['#78d9ec', '#4285f4', '#9b72cb', '#f49c46'],
      warn: '#fbbc04', bad: '#ea4335', ok: '#34a853', cpu: '#78d9ec', gpu: '#4285f4',
      track: 'rgba(120,217,236,.12)', lineCap: 'round',
      axis: '#95abc4', muted: '#5d7491', grid: 'rgba(120,217,236,.08)',
      series: ['#78d9ec', '#4285f4', '#9b72cb', '#f49c46']
    },
    'blue-porcelain': {
      stops: ['#1b3b73', '#2e5fa1', '#6b99c3', '#a62c2b'],
      warn: '#b45309', bad: '#a62c2b', ok: '#1f7a5c', cpu: '#2e5fa1', gpu: '#a62c2b',
      track: 'rgba(27,59,115,.12)', lineCap: 'round',
      axis: '#3b536e', muted: '#627b96', grid: 'rgba(27,59,115,.10)',
      series: ['#2e5fa1', '#a62c2b', '#387c9e', '#6b99c3']
    },
    'ink-wash': {
      stops: ['#1c1e22', '#5c626b', '#9c988e', '#c83c23'],
      warn: '#b45309', bad: '#c83c23', ok: '#2d6a4f', cpu: '#1c1e22', gpu: '#c83c23',
      track: 'rgba(28,30,34,.10)', lineCap: 'round',
      axis: '#4a4d52', muted: '#76787d', grid: 'rgba(28,30,34,.09)',
      series: ['#1c1e22', '#5c626b', '#c83c23', '#3b556e']
    },
    'gemini-porcelain': {
      stops: ['#4285f4', '#9b72cb', '#d96570', '#f49c46'],
      warn: '#e37400', bad: '#d93025', ok: '#1e8e3e', cpu: '#4285f4', gpu: '#d96570',
      track: 'rgba(66,133,244,.12)', lineCap: 'round',
      axis: '#4a5568', muted: '#718096', grid: 'rgba(66,133,244,.10)',
      series: ['#4285f4', '#9b72cb', '#d96570', '#f49c46']
    },
    prism: {
      stops: ['#7c3aed', '#a855f7', '#d946ef', '#ec4899', '#f472b6', '#67e8f9', '#818cf8'],
      warn: '#fbbf24', bad: '#fb7185', cpu: '#22d3ee', gpu: '#ec4899',
      track: 'rgba(255,255,255,.10)', lineCap: 'round'
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

  var currentTheme = DEFAULT_THEME;
  var motionRequested = 'auto', visualReady = false, spotFrame = null;
  var motionReduce, motionNarrow, motionCoarse, motionHover;
  var THEME_DESC = {
    'blue-porcelain': '汝窑天青 · 素笔缠枝',
    'sunrise-gold': '丁达尔光 · 阴阳雪峰',
    'ink-wash': '生宣宿墨 · 气韵洇纸',
    'ferrofluid-chrome': '液态水银 · 强磁玫瑰刺',
    'black-opal': '多面晶格 · 入射游彩',
    'gemini-nebula': '旋涡星云 · 真丝星芒',
    'gemini-porcelain': '羊脂白瓷 · 贝塞尔纱带',
    'gemini-halftone': '动态点阵 · 光学透镜',
    prism: '液态棱镜 · 流体透色',
    rainbow: '全息极光 · 光幕舒展',
    'liquid-glass': '澄光玻璃 · 冷银折射',
    'forest-breath': '森息林间 · 呼吸薄雾'
  };
  /* 所有注册皮肤都有氛围层，故 12 款统一走严格档位判定：
     仅 full 动效 + 悬停设备才启用鼠标跟随/点击特效。 */
  function isMaterialTheme() { return THEMES.indexOf(resolveTheme(currentTheme)) >= 0; }
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
      if (t) { return resolveTheme(t); }
    } catch (e) {}
    return DEFAULT_THEME;
  }

  function themeLabel(t) { return THEME_LABEL[resolveTheme(t)] || THEME_LABEL[DEFAULT_THEME]; }

  window.getHwmonThemePalette = function () {
    var key = resolveTheme(currentTheme);
    var p = PALETTES[key] || PALETTES[DEFAULT_THEME];
    return {
      theme: key,
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
    var key = resolveTheme(t);
    currentTheme = key;
    if (document.body) {
      document.body.setAttribute('data-theme', key);
      document.body.setAttribute('data-theme-tone', LIGHT_SKINS[key] ? 'light' : 'dark');
    }
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
    html += '<div id="hwThemeGallery" class="hw-theme-gallery" hidden><div class="hw-gallery-heading"><strong>选择你的氛围</strong><span>十二种材质 · 同一份秩序</span></div>' +
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
    html += '<label class="hw-motion-control" title="十二套主题环境动效">动效<select id="hwMotionSelect" aria-label="十二套主题环境动效档位">' + opt +
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
    /* 旧存档迁移：localStorage 里可能还是 hypercar / gold，
       映射成功后回写一次，避免每次加载都走映射分支。 */
    try {
      var stored = window.localStorage ? window.localStorage.getItem(THEME_KEY) : null;
      if (stored && stored !== currentTheme) { window.localStorage.setItem(THEME_KEY, currentTheme); }
    } catch (e) {}
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

/* =====================================================================
 * HwmonMaterialEngine v1 —— 8 套独立物理材质 Canvas 渲染管线
 *  + 12 款皮肤专属鼠标跟随 / 点击特效
 *  + 硬件温度实时联动（旁路 /api/snapshot）
 *
 * 设计约束：
 *  1. 每款皮肤的几何生成与着色算法完全独立，不存在「同一条正弦波换色」
 *     的共用逻辑；8 个 render* 函数各自维护自己的空间结构与光学模型。
 *  2. 背景画布只画材质，鼠标特效单独走顶层 .hw-fx-canvas，互不干扰。
 *  3. 全部受 data-motion-effective (full|lite|off) 与 data-fx-paused 管控：
 *     full 才跑动画，lite/off 只画一帧静态材质，不空转绘制。
 * ===================================================================== */
(function initHwmonMaterialEngine() {
  'use strict';

  var CANVAS_SKINS = {
    'blue-porcelain': true, 'sunrise-gold': true, 'ink-wash': true,
    'ferrofluid-chrome': true, 'black-opal': true, 'gemini-nebula': true,
    'gemini-porcelain': true, 'gemini-halftone': true
  };
  var LEGACY_MAP = { 'hypercar': 'sunrise-gold', 'gold': 'ferrofluid-chrome' };

  /* 背景降采样倍率：细节型（点阵/欧泊）取高清，烟雨型（云/雾/墨）取柔化。
     点阵是 880 个圆，倍率直接决定主线程占用，0.72 是清晰度与帧率的平衡点。 */
  var BG_SCALE = {
    'blue-porcelain': 0.62, 'sunrise-gold': 0.54, 'ink-wash': 0.62,
    'ferrofluid-chrome': 0.70, 'black-opal': 0.74, 'gemini-nebula': 0.50,
    'gemini-porcelain': 0.52, 'gemini-halftone': 0.72
  };

  var tauTarget = 0.25, tau = 0.25, t = 0;
  var pointer = { x: 0.5, y: 0.4, active: false, vx: 0, vy: 0 };
  var trail = [], bursts = [];

  function clamp01(v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }
  function lerp(a, b, k) { return a + (b - a) * k; }

  /* ------------------------------------------------------------------
   * 1. 硬件温度无感旁路采集
   * agent.py 的真实字段是 cpu.temp / gpu.core / gpu.hot。
   * 同时兼容 gpu.temp_c / gpu.hotspot_c，便于复用到别的采集端。
   * ------------------------------------------------------------------ */
  window.setHwmonThermalState = function (tempC) {
    if (typeof tempC === 'number' && isFinite(tempC) && tempC > 0) {
      tauTarget = clamp01((tempC - 38) / 52);
      return true;
    }
    return false;
  };
  window.getHwmonThermalState = function () { return { tau: tau, target: tauTarget }; };

  if (window.fetch && !window.__hwThermalHooked) {
    window.__hwThermalHooked = true;
    var origFetch = window.fetch;
    window.fetch = function () {
      var args = arguments, self = this;
      return origFetch.apply(self, args).then(function (res) {
        try {
          var req = args[0];
          var url = (typeof req === 'string') ? req : ((req && req.url) || '');
          if (url.indexOf('snapshot') !== -1 && res && res.clone) {
            res.clone().json().then(function (d) {
              if (!d) { return; }
              var g = d.gpu || {}, c = d.cpu || {};
              var gv = g.hot != null ? g.hot : (g.core != null ? g.core
                : (g.hotspot_c != null ? g.hotspot_c : g.temp_c));
              var cv = c.temp != null ? c.temp : c.temp_c;
              var pick = (typeof gv === 'number' && gv > 0) ? gv
                : ((typeof cv === 'number' && cv > 0) ? cv : null);
              if (pick != null) { window.setHwmonThermalState(Number(pick)); }
            }).catch(function () {});
          }
        } catch (e) {}
        return res;
      });
    };
  }

  /* ------------------------------------------------------------------
   * 2. 各材质专属几何结构（一次生成，逐帧复用）
   * ------------------------------------------------------------------ */

  /* 青花瓷：蝉翼冰裂 —— 28 条随机折身裂纹 */
  var CRACKLES = [];
  (function () {
    for (var i = 0; i < 28; i++) {
      var pts = [], cx = ((i * 37 + 13) % 100) / 100, cy = ((i * 53 + 7) % 100) / 100;
      pts.push([cx, cy]);
      var ang = (i * 1.7) % (Math.PI * 2);
      for (var s = 0; s < 5; s++) {
        ang += (s % 2 === 0 ? 1 : -1) * (0.35 + (i % 3) * 0.18);
        cx += Math.cos(ang) * 0.085;
        cy += Math.sin(ang) * 0.095;
        pts.push([cx, cy]);
      }
      CRACKLES.push(pts);
    }
  })();

  /* 青花瓷：烟雨细雨线与涟漪 */
  var RAIN_DROPS = [];
  (function () {
    for (var i = 0; i < 32; i++) {
      RAIN_DROPS.push({
        u: ((i * 61) % 100) / 100, v: ((i * 29) % 100) / 100,
        speed: 0.008 + (i % 4) * 0.003, len: 14 + (i % 3) * 8
      });
    }
  })();

  /* 日照金山：三列山脊 + 峰高 */
  var MOUNTAIN_RANGES = [
    { yBase: 0.54, peaks: [0.0, 0.14, 0.28, 0.44, 0.62, 0.78, 0.92, 1.0], heights: [0.12, 0.28, 0.16, 0.36, 0.18, 0.31, 0.15, 0.22] },
    { yBase: 0.68, peaks: [0.0, 0.18, 0.35, 0.52, 0.71, 0.86, 1.0], heights: [0.16, 0.34, 0.14, 0.29, 0.35, 0.16, 0.20] },
    { yBase: 0.84, peaks: [0.0, 0.12, 0.31, 0.48, 0.66, 0.83, 1.0], heights: [0.10, 0.22, 0.28, 0.12, 0.25, 0.18, 0.10] }
  ];

  /* 宣纸：草木纤维 */
  var PAPER_FIBERS = [];
  (function () {
    for (var i = 0; i < 90; i++) {
      PAPER_FIBERS.push({
        u: ((i * 43 + 11) % 100) / 100, v: ((i * 71 + 19) % 100) / 100,
        ang: (i * 2.1) % Math.PI, len: 8 + (i % 5) * 5
      });
    }
  })();

  /* 水墨：五团宿墨（位置/半径/墨阶/随机种子） */
  var INK_CLOUDS = [
    { u: 0.22, v: 0.42, rx: 0.18, ry: 0.14, shade: 28, seed: 1.1 },
    { u: 0.28, v: 0.46, rx: 0.24, ry: 0.17, shade: 75, seed: 2.4 },
    { u: 0.74, v: 0.36, rx: 0.16, ry: 0.13, shade: 36, seed: 3.7 },
    { u: 0.68, v: 0.42, rx: 0.25, ry: 0.18, shade: 95, seed: 4.9 },
    { u: 0.48, v: 0.68, rx: 0.28, ry: 0.15, shade: 118, seed: 6.2 }
  ];

  /* 黑欧泊：58 块多面体晶格切面 */
  var OPAL_FACETS = [];
  (function () {
    for (var i = 0; i < 58; i++) {
      var r = Math.floor(i / 10), c = i % 10;
      OPAL_FACETS.push({
        u: (c + 0.3 + ((i * 37) % 10) * 0.05) / 10,
        v: (r + 0.3 + ((i * 53) % 10) * 0.05) / 6,
        rx: 22 + (i % 4) * 9, ry: 14 + (i % 3) * 7,
        rot: (i * 0.85) % Math.PI, sides: 5 + (i % 3),
        phase: (i * 1.37) % (Math.PI * 2)
      });
    }
  })();

  /* 金尘 / 星尘 */
  var MOTES = [];
  (function () {
    for (var i = 0; i < 95; i++) {
      MOTES.push({
        u: ((i * 73) % 100) / 100, v: ((i * 41 + 17) % 100) / 100,
        r: 0.9 + (i % 4) * 0.6, phase: (i * 1.13) % (Math.PI * 2)
      });
    }
  })();

  /* 双子流体：四核公转参数 + 四角星芒落点 */
  var GEMINI_CORES = [
    { u: 0.28, v: 0.38, r: 0.36, col: [66, 133, 244] },
    { u: 0.52, v: 0.48, r: 0.38, col: [155, 114, 203] },
    { u: 0.72, v: 0.42, r: 0.34, col: [217, 101, 112] },
    { u: 0.45, v: 0.62, r: 0.32, col: [244, 156, 70] }
  ];
  var GEMINI_STARS = [
    { u: 0.11, v: 0.14, s: 1.0 }, { u: 0.89, v: 0.17, s: 0.82 },
    { u: 0.15, v: 0.86, s: 0.74 }, { u: 0.85, v: 0.83, s: 0.92 }
  ];

  /* ------------------------------------------------------------------
   * 3. 八套独立材质渲染管线
   * ------------------------------------------------------------------ */

  /* (1) 天青·元青花釉
     汝窑天青球面釉光 + 拉坯旋纹 + 蝉翼冰裂 + 素笔贝塞尔缠枝青花 + 烟雨涟漪 */
  function renderBluePorcelain(ctx, w, h, tauVal, mx, my) {
    var sheenX = pointer.active ? mx : w * (0.46 + 0.08 * Math.cos(t * 0.4));
    var sheenY = pointer.active ? my : h * (0.34 + 0.07 * Math.sin(t * 0.3));

    /* 胎体：球面釉光，越靠边缘越沉，形成碗形立体感 */
    var base = ctx.createRadialGradient(sheenX, sheenY, w * 0.02, w * 0.5, h * 0.52, w * 0.92);
    base.addColorStop(0, '#fbfdff');
    base.addColorStop(0.28, '#e9f2f7');
    base.addColorStop(0.62, '#d3e3ec');
    base.addColorStop(0.86, '#bcd2dd');
    base.addColorStop(1, '#a3bccb');
    ctx.fillStyle = base;
    ctx.fillRect(0, 0, w, h);

    /* 拉坯旋纹：偏心同心圆，中心随呼吸微动 */
    var spinCx = w * 0.5 + Math.sin(t * 0.22) * w * 0.022;
    var spinCy = h * 0.5 + Math.cos(t * 0.18) * h * 0.026;
    ctx.strokeStyle = 'rgba(255,255,255,.30)';
    ctx.lineWidth = 1.1;
    for (var rr = 0.05 * w; rr < w * 0.78; rr += w * 0.052) {
      ctx.beginPath();
      ctx.ellipse(spinCx, spinCy, rr, rr * 0.84, 0, 0, Math.PI * 2);
      ctx.stroke();
    }

    /* 蝉翼冰裂 */
    ctx.strokeStyle = 'rgba(42,68,96,.15)';
    ctx.lineWidth = 0.85;
    for (var ci = 0; ci < CRACKLES.length; ci++) {
      var pts = CRACKLES[ci];
      ctx.beginPath();
      for (var pi = 0; pi < pts.length; pi++) {
        var px = pts[pi][0] * w, py = pts[pi][1] * h;
        if (pi === 0) { ctx.moveTo(px, py); } else { ctx.lineTo(px, py); }
      }
      ctx.stroke();
    }

    /* 素笔缠枝：五组卷草，青花为骨，窑变后局部转朱砂 */
    var scrolls = [
      { cx: 0.16, cy: 0.28, sc: 0.13, rot: 0.2 },
      { cx: 0.84, cy: 0.24, sc: 0.14, rot: 2.1 },
      { cx: 0.50, cy: 0.74, sc: 0.16, rot: -1.1 },
      { cx: 0.22, cy: 0.80, sc: 0.11, rot: 1.4 },
      { cx: 0.80, cy: 0.78, sc: 0.12, rot: -2.4 }
    ];
    for (var si = 0; si < scrolls.length; si++) {
      var sc = scrolls[si];
      var unit = sc.sc * w;
      ctx.save();
      ctx.translate(sc.cx * w, sc.cy * h);
      ctx.rotate(sc.rot + Math.sin(t * 0.35 + si) * 0.10);
      var kiln = tauVal > 0.45 && (si % 2 === 1);
      ctx.fillStyle = kiln
        ? 'rgba(186,62,58,' + (0.10 + tauVal * 0.16) + ')'
        : 'rgba(46,95,161,.17)';
      for (var pf = 0; pf < 4; pf++) {
        ctx.rotate(Math.PI / 2);
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.bezierCurveTo(unit * 0.4, -unit * 0.35, unit * 0.85, -unit * 0.15, unit * 0.7, unit * 0.25);
        ctx.bezierCurveTo(unit * 0.4, unit * 0.45, unit * 0.15, unit * 0.2, 0, 0);
        ctx.fill();
      }
      ctx.strokeStyle = kiln
        ? 'rgba(166,44,43,' + (0.26 + tauVal * 0.34) + ')'
        : 'rgba(22,56,112,.36)';
      ctx.lineWidth = 1.5;
      for (var arm = 0; arm < 3; arm++) {
        ctx.rotate((Math.PI * 2) / 3);
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.bezierCurveTo(unit * 0.35, -unit * 0.5, unit * 0.9, -unit * 0.2, unit * 0.75, unit * 0.3);
        ctx.bezierCurveTo(unit * 0.6, unit * 0.65, unit * 0.25, unit * 0.5, unit * 0.35, unit * 0.22);
        ctx.stroke();
      }
      ctx.restore();
    }

    /* 烟雨：细雨线 + 落点涟漪 */
    ctx.lineWidth = 1;
    for (var di = 0; di < RAIN_DROPS.length; di++) {
      var rd = RAIN_DROPS[di];
      var rx = ((rd.u - t * 0.04 + 10) % 1) * w;
      var ry = ((rd.v + t * rd.speed * 18) % 1) * h;
      ctx.strokeStyle = 'rgba(35,78,128,.18)';
      ctx.beginPath();
      ctx.moveTo(rx, ry);
      ctx.lineTo(rx - 4, ry + rd.len);
      ctx.stroke();
      if (di % 4 === 0) {
        var rp = (t * 0.9 + di * 0.7) % 1;
        ctx.strokeStyle = 'rgba(27,59,115,' + ((1 - rp) * 0.22) + ')';
        ctx.beginPath();
        ctx.ellipse(rd.u * w, rd.v * h, rp * 30, rp * 12, 0, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    /* 釉面反光：右上角一道斜向高光，模拟窑内釉流 */
    var gl = ctx.createLinearGradient(w * 0.55, 0, w * 0.9, h * 0.6);
    gl.addColorStop(0, 'rgba(255,255,255,.30)');
    gl.addColorStop(0.5, 'rgba(255,255,255,.05)');
    gl.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = gl;
    ctx.fillRect(0, 0, w, h);
  }

  /* (2) 日照·梅里金山
     破晓大气散射 + 三层 3D 阴阳面雪峰金脊 + 峡谷晨雾 + 16 道丁达尔体积光柱与金尘 */
  function renderSunriseGold(ctx, w, h, tauVal) {
    var sunX = pointer.active ? w * (0.10 + pointer.x * 0.42) : w * (0.20 + 0.05 * Math.sin(t * 0.3));
    var sunY = pointer.active ? h * (0.06 + pointer.y * 0.30) : h * (0.17 - tauVal * 0.05);

    /* 大气层：由深空蓝向日出橙过渡，温度越高地平线越红 */
    var sky = ctx.createLinearGradient(0, 0, 0, h);
    sky.addColorStop(0, '#05070f');
    sky.addColorStop(0.30, '#101733');
    sky.addColorStop(0.56, '#2b2340');
    sky.addColorStop(0.76, tauVal > 0.5 ? '#7a2c22' : '#4a2b34');
    sky.addColorStop(0.92, tauVal > 0.5 ? '#c25a1e' : '#8a4420');
    sky.addColorStop(1, '#e08a2a');
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);

    /* 太阳与光晕 */
    var sunGlow = ctx.createRadialGradient(sunX, sunY, 2, sunX, sunY, w * 0.62);
    sunGlow.addColorStop(0, 'rgba(255,250,225,.95)');
    sunGlow.addColorStop(0.12, 'rgba(255,215,120,' + (0.55 + tauVal * 0.25) + ')');
    sunGlow.addColorStop(0.42, 'rgba(255,120,50,' + (0.20 + tauVal * 0.20) + ')');
    sunGlow.addColorStop(1, 'rgba(255,90,30,0)');
    ctx.fillStyle = sunGlow;
    ctx.fillRect(0, 0, w, h);

    /* 三层山脊：每段三角拆成受光面与背光面，峰脊描金 */
    for (var ri = 0; ri < MOUNTAIN_RANGES.length; ri++) {
      var rng = MOUNTAIN_RANGES[ri];
      var baseH = h * rng.yBase;
      var litTop = ri === 0 ? (tauVal > 0.45 ? '#fff0b8' : '#ffe08a')
        : (ri === 1 ? '#ffd166' : '#e39a4e');
      for (var i = 0; i < rng.peaks.length - 1; i++) {
        var x1 = rng.peaks[i] * w, y1 = baseH - rng.heights[i] * h;
        var x2 = rng.peaks[i + 1] * w, y2 = baseH - rng.heights[i + 1] * h;
        var midX = (x1 + x2) * 0.5;
        var peakY = Math.min(y1, y2) - h * 0.055;

        var lit = ctx.createLinearGradient(x1, peakY, midX, h);
        lit.addColorStop(0, litTop);
        lit.addColorStop(0.34, ri === 0 ? '#ff9c33' : '#b9682c');
        lit.addColorStop(1, '#141a2c');
        ctx.fillStyle = lit;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(midX, peakY);
        ctx.lineTo(midX + w * 0.035, h);
        ctx.lineTo(x1, h);
        ctx.closePath();
        ctx.fill();

        var shad = ctx.createLinearGradient(midX, peakY, x2, h);
        shad.addColorStop(0, ri === 2 ? '#1a2338' : '#232e4a');
        shad.addColorStop(1, '#06080f');
        ctx.fillStyle = shad;
        ctx.beginPath();
        ctx.moveTo(midX, peakY);
        ctx.lineTo(x2, y2);
        ctx.lineTo(x2, h);
        ctx.lineTo(midX + w * 0.035, h);
        ctx.closePath();
        ctx.fill();

        ctx.strokeStyle = 'rgba(255,236,150,' + (0.5 + tauVal * 0.45) + ')';
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(midX, peakY);
        ctx.stroke();
      }
      /* 峡谷晨雾：贴着山脚的一层横向雾带 */
      var mist = ctx.createLinearGradient(0, baseH - h * 0.04, 0, baseH + h * 0.07);
      mist.addColorStop(0, 'rgba(255,215,165,0)');
      mist.addColorStop(0.5, 'rgba(255,205,150,' + (0.10 + tauVal * 0.05) + ')');
      mist.addColorStop(1, 'rgba(15,20,38,0)');
      ctx.fillStyle = mist;
      ctx.fillRect(0, baseH - h * 0.04, w, h * 0.11);
    }

    /* 流云：贴在山腰上方缓慢横移，给静态山体补上肉眼可辨的运动 */
    for (var cl = 0; cl < 4; cl++) {
      var cy0 = h * (0.26 + cl * 0.10);
      var drift = ((t * (0.012 + cl * 0.005) + cl * 0.31) % 1.5) - 0.25;
      var cxx = drift * w;
      var bandW = w * 0.55;
      var cloudGrad = ctx.createLinearGradient(cxx - bandW, 0, cxx + bandW, 0);
      cloudGrad.addColorStop(0, 'rgba(255,196,152,0)');
      cloudGrad.addColorStop(0.5, 'rgba(255,196,152,' + (0.075 + cl * 0.018) + ')');
      cloudGrad.addColorStop(1, 'rgba(255,196,152,0)');
      ctx.fillStyle = cloudGrad;
      ctx.fillRect(cxx - bandW, cy0 - h * 0.018, bandW * 2, h * 0.036);
    }

    /* 丁达尔体积光柱：screen 混合，光心锁在太阳位置 */
    ctx.save();
    ctx.globalCompositeOperation = 'screen';
    var rayLen = Math.hypot(w, h) * 1.25;
    for (var r = 0; r < 16; r++) {
      var baseAng = 0.10 + (r / 16) * 1.30 + Math.sin(t * 0.4 + r * 1.7) * 0.055;
      var spread = 0.014 + (r % 3) * 0.013;
      var intensity = (0.07 + 0.11 * Math.sin(t * 0.7 + r * 2.1)) * (0.75 + tauVal * 0.6);
      if (intensity <= 0) { intensity = 0.02; }
      var rayGrad = ctx.createLinearGradient(sunX, sunY,
        sunX + Math.cos(baseAng) * rayLen, sunY + Math.sin(baseAng) * rayLen);
      rayGrad.addColorStop(0, 'rgba(255,238,180,' + (intensity * 1.5) + ')');
      rayGrad.addColorStop(0.42, 'rgba(255,178,80,' + intensity + ')');
      rayGrad.addColorStop(1, 'rgba(255,120,40,0)');
      ctx.fillStyle = rayGrad;
      ctx.beginPath();
      ctx.moveTo(sunX, sunY);
      ctx.lineTo(sunX + Math.cos(baseAng - spread) * rayLen, sunY + Math.sin(baseAng - spread) * rayLen);
      ctx.lineTo(sunX + Math.cos(baseAng + spread) * rayLen, sunY + Math.sin(baseAng + spread) * rayLen);
      ctx.closePath();
      ctx.fill();
    }
    /* 金尘：缓慢上浮 + 闪烁 */
    for (var mi = 0; mi < MOTES.length; mi++) {
      var m = MOTES[mi];
      var gx = ((m.u + t * 0.012 + 1) % 1) * w;
      var gy = ((m.v - t * 0.008 + 1) % 1) * h;
      var tw = 0.3 + 0.7 * (0.5 + 0.5 * Math.sin(t * 2.5 + m.phase));
      ctx.fillStyle = '#ffe49e';
      ctx.globalAlpha = tw * 0.55;
      ctx.beginPath();
      ctx.arc(gx, gy, m.r * 1.15, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
    ctx.restore();
  }

  /* (3) 烟雨·徽墨宣纸
     生宣竹帘横纹与草木纤维 + 极坐标分形水墨团毛细扩散 + 宿墨水线 + 高温朱砂点染 */
  function renderInkWash(ctx, w, h, tauVal) {
    ctx.fillStyle = '#f3efe4';
    ctx.fillRect(0, 0, w, h);

    /* 竹帘横纹：抄纸留下的帘痕，等距但浓淡有微差 */
    for (var y = 5; y < h; y += 8) {
      ctx.strokeStyle = 'rgba(160,148,125,' + (0.055 + 0.02 * Math.sin(y * 0.7)) + ')';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }
    /* 草木纤维 */
    ctx.strokeStyle = 'rgba(135,122,102,.15)';
    ctx.lineWidth = 0.8;
    for (var fi = 0; fi < PAPER_FIBERS.length; fi++) {
      var fb = PAPER_FIBERS[fi];
      var fx = fb.u * w, fy = fb.v * h;
      ctx.beginPath();
      ctx.moveTo(fx, fy);
      ctx.lineTo(fx + Math.cos(fb.ang) * fb.len, fy + Math.sin(fb.ang) * fb.len);
      ctx.stroke();
    }

    /* 宿墨：极坐标分形边界 + 三层毛细扩散，multiply 叠加出纸背洇透感 */
    ctx.save();
    ctx.globalCompositeOperation = 'multiply';
    for (var ci = 0; ci < INK_CLOUDS.length; ci++) {
      var ic = INK_CLOUDS[ci];
      var cxp = (ic.u + 0.032 * Math.sin(t * 0.25 + ic.seed)) * w;
      var cyp = (ic.v + 0.026 * Math.cos(t * 0.2 + ic.seed)) * h;
      var maxRx = ic.rx * w, maxRy = ic.ry * h;
      var isCinnabar = tauVal > 0.45 && ci === 2;
      var rCol = isCinnabar ? 195 : ic.shade;
      var gCol = isCinnabar ? 52 : ic.shade + 3;
      var bCol = isCinnabar ? 32 : ic.shade + 8;
      for (var layer = 2; layer >= 0; layer--) {
        var scale = 0.52 + layer * 0.27 + 0.045 * Math.sin(t * 0.4 + layer);
        ctx.beginPath();
        for (var s = 0; s <= 40; s++) {
          var a = (s / 40) * Math.PI * 2;
          var noise = 1
            + 0.16 * Math.sin(a * 5 + ic.seed + t * 0.2)
            + 0.11 * Math.cos(a * 9 - ic.seed * 2)
            + 0.07 * Math.sin(a * 15 + t * 0.3);
          var qx = cxp + Math.cos(a) * maxRx * scale * noise;
          var qy = cyp + Math.sin(a) * maxRy * scale * noise;
          if (s === 0) { ctx.moveTo(qx, qy); } else { ctx.lineTo(qx, qy); }
        }
        ctx.closePath();
        var inkGrad = ctx.createRadialGradient(cxp, cyp, 3, cxp, cyp, Math.max(maxRx, maxRy) * scale);
        inkGrad.addColorStop(0, 'rgba(' + rCol + ',' + gCol + ',' + bCol + ',' + (0.26 - layer * 0.065) + ')');
        inkGrad.addColorStop(0.72, 'rgba(' + rCol + ',' + gCol + ',' + bCol + ',' + (0.15 - layer * 0.035) + ')');
        inkGrad.addColorStop(1, 'rgba(' + rCol + ',' + gCol + ',' + bCol + ',0.02)');
        ctx.fillStyle = inkGrad;
        ctx.fill();
        if (layer === 2) {
          ctx.strokeStyle = 'rgba(' + rCol + ',' + gCol + ',' + bCol + ',0.20)';
          ctx.lineWidth = 1.1;
          ctx.stroke();
        }
      }
    }
    ctx.restore();

    /* 宿墨水线：干笔飞白，一段实一段虚，模拟提按 */
    ctx.save();
    ctx.lineCap = 'round';
    for (var li = 0; li < 7; li++) {
      var by = h * (0.16 + li * 0.115);
      var ph = t * 0.28 + li * 0.9;
      var amp = h * (0.018 + (li % 3) * 0.008);
      ctx.strokeStyle = 'rgba(38,38,42,' + (0.10 + (li % 3) * 0.035) + ')';
      ctx.lineWidth = 1.4 + (li % 2) * 1.6;
      var segs = 7;
      for (var q = 0; q < segs; q++) {
        if ((li + q) % 3 === 0) { continue; }
        var xA = (q / segs) * w, xB = ((q + 0.72) / segs) * w;
        ctx.beginPath();
        ctx.moveTo(xA, by + Math.sin(ph + q * 0.8) * amp);
        ctx.quadraticCurveTo((xA + xB) * 0.5, by + Math.sin(ph + q * 0.8 + 1.4) * amp * 1.6,
          xB, by + Math.sin(ph + q * 0.8 + 2.6) * amp);
        ctx.stroke();
      }
    }
    ctx.restore();

    /* 朱砂点染：高温时零星落朱，像画师最后点的印色 */
    if (tauVal > 0.4) {
      var cinn = clamp01((tauVal - 0.4) / 0.6);
      for (var di = 0; di < 9; di++) {
        var du = ((di * 47 + 23) % 100) / 100;
        var dv = ((di * 71 + 11) % 100) / 100;
        var dr = (2.4 + (di % 3) * 2.2) * (0.7 + 0.3 * Math.sin(t * 0.6 + di));
        ctx.fillStyle = 'rgba(200,60,35,' + (0.10 + cinn * 0.22) + ')';
        ctx.beginPath();
        ctx.arc(du * w, dv * h, dr, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  /* (4) 磁流·液态水银
     悬浮液态水银母滴池 + 28 簇放射状磁流体玫瑰尖刺 + 铬金属球面高光 */
  function renderFerrofluid(ctx, w, h, tauVal, mx, my) {
    var base = ctx.createRadialGradient(w * 0.5, h * 0.42, 10, w * 0.5, h * 0.5, w * 0.9);
    base.addColorStop(0, '#12161e');
    base.addColorStop(0.55, '#0a0d14');
    base.addColorStop(1, '#05070b');
    ctx.fillStyle = base;
    ctx.fillRect(0, 0, w, h);

    var pools = [
      { u: 0.24, v: 0.46, r: w * 0.115, ph: 0 },
      { u: 0.74, v: 0.42, r: w * 0.125, ph: 2 },
      { u: 0.50, v: 0.66, r: w * 0.098, ph: 4 }
    ];
    var spikes = 28;

    for (var idx = 0; idx < pools.length; idx++) {
      var pl = pools[idx];
      var cxp = pl.u * w + Math.cos(t * 0.5 + pl.ph) * w * 0.022;
      var cyp = pl.v * h + Math.sin(t * 0.4 + pl.ph) * h * 0.024;
      /* 磁吸：水滴整体被指针牵引，越近牵引越强 */
      if (pointer.active) {
        cxp += (mx - cxp) * 0.16;
        cyp += (my - cyp) * 0.16;
      }
      var toPointer = pointer.active ? Math.atan2(my - cyp, mx - cxp) : 0;

      ctx.beginPath();
      var steps = spikes * 5;
      for (var s = 0; s <= steps; s++) {
        var a = (s / steps) * Math.PI * 2;
        /* 尖刺包络：cos^k 形成尖锐的玫瑰刺，随温度增高 */
        var env = Math.pow(0.5 + 0.5 * Math.cos(a * spikes + t * 2.2 + pl.ph), 2.4);
        var align = pointer.active ? Math.max(0, Math.cos(a - toPointer)) : 0;
        var pull = align * align * 0.5 + tauVal * 0.28;
        var r = pl.r * (0.66 + env * (0.34 + pull));
        var sx = cxp + Math.cos(a) * r;
        var sy = cyp + Math.sin(a) * r * 0.94;
        if (s === 0) { ctx.moveTo(sx, sy); } else { ctx.lineTo(sx, sy); }
      }
      ctx.closePath();
      /* 铬金属：左上高光 → 灰阶过渡 → 右下深影 */
      var cg = ctx.createRadialGradient(cxp - pl.r * 0.32, cyp - pl.r * 0.34, 2, cxp, cyp, pl.r * 1.4);
      cg.addColorStop(0, '#ffffff');
      cg.addColorStop(0.16, '#e2e8f0');
      cg.addColorStop(0.40, '#94a3b8');
      cg.addColorStop(0.66, '#334155');
      cg.addColorStop(0.86, tauVal > 0.5 ? '#3b5c8a' : '#1e293b');
      cg.addColorStop(1, '#080c14');
      ctx.fillStyle = cg;
      ctx.fill();
      ctx.strokeStyle = 'rgba(241,245,249,' + (0.55 + tauVal * 0.3) + ')';
      ctx.lineWidth = 1.3;
      ctx.stroke();

      /* 镜面高光点 */
      ctx.fillStyle = 'rgba(255,255,255,.85)';
      ctx.beginPath();
      ctx.ellipse(cxp - pl.r * 0.3, cyp - pl.r * 0.36, pl.r * 0.16, pl.r * 0.10, -0.6, 0, Math.PI * 2);
      ctx.fill();
    }

    /* 磁场线：从每滴向外辐射的细弧，交代「强磁」的物理语境 */
    ctx.strokeStyle = 'rgba(148,163,184,' + (0.10 + tauVal * 0.08) + ')';
    ctx.lineWidth = 0.7;
    for (var fi = 0; fi < 3; fi++) {
      var pp = pools[fi];
      var fx0 = pp.u * w, fy0 = pp.v * h;
      for (var k = 0; k < 10; k++) {
        var ka = (k / 10) * Math.PI * 2 + t * 0.12;
        ctx.beginPath();
        ctx.moveTo(fx0 + Math.cos(ka) * pp.r * 1.1, fy0 + Math.sin(ka) * pp.r * 1.1);
        ctx.quadraticCurveTo(
          fx0 + Math.cos(ka + 0.3) * pp.r * 2.1, fy0 + Math.sin(ka + 0.3) * pp.r * 2.1,
          fx0 + Math.cos(ka + 0.15) * pp.r * 3.0, fy0 + Math.sin(ka + 0.15) * pp.r * 3.0
        );
        ctx.stroke();
      }
    }
  }

  /* (5) 欧泊·黑曜变彩
     58 块多面体宝石晶格切面 + 入射角布拉格衍射全息变彩 */
  function renderBlackOpal(ctx, w, h, tauVal, mx, my) {
    var bgGrad = ctx.createLinearGradient(0, 0, w, h);
    bgGrad.addColorStop(0, '#04050a');
    bgGrad.addColorStop(0.5, '#070a12');
    bgGrad.addColorStop(1, '#04050a');
    ctx.fillStyle = bgGrad;
    ctx.fillRect(0, 0, w, h);

    /* 游彩色板：低温冷色（蓝绿紫），高温暖色（红橙金）—— 这是欧泊的变彩本质 */
    var cold = [[0, 230, 140], [0, 176, 255], [145, 85, 255], [30, 225, 195]];
    var warm = [[255, 65, 85], [255, 165, 40], [0, 230, 150], [180, 90, 255]];
    var pal = tauVal > 0.5 ? warm : cold;
    var lensR = w * 0.30;

    for (var i = 0; i < OPAL_FACETS.length; i++) {
      var fc = OPAL_FACETS[i];
      var cxp = fc.u * w, cyp = fc.v * h;
      var ddx = pointer.active ? (cxp - mx) : 0;
      var ddy = pointer.active ? (cyp - my) : 0;
      var dist = pointer.active ? Math.hypot(ddx, ddy) : w * 0.4;
      /* 入射角：指针存在时按视线角度计算，否则按时间自转 */
      var inc = pointer.active ? Math.atan2(ddy, ddx) : (t * 0.6 + fc.phase);
      /* 布拉格条件近似：入射角余弦决定干涉强度 */
      var bragg = 0.5 + 0.5 * Math.cos(inc * 2.4 + t * 1.5 + fc.phase);
      var near = pointer.active && dist < lensR ? (1 - dist / lensR) : 0;
      var alpha = bragg * 0.42 + near * 0.5;
      if (alpha < 0.05) { continue; }

      var cIdx = Math.abs(Math.floor((inc / Math.PI) * 2 + i * 0.7)) % 4;
      var rgb = pal[cIdx];
      var grow = 1 + near * 0.35;

      ctx.save();
      ctx.translate(cxp, cyp);
      ctx.rotate(fc.rot + (pointer.active ? inc * 0.12 : 0));
      ctx.beginPath();
      for (var s = 0; s < fc.sides; s++) {
        var a = (s / fc.sides) * Math.PI * 2;
        var rx = fc.rx * (w / 1200) * grow, ry = fc.ry * (w / 1200) * grow;
        var vx = Math.cos(a) * rx * 1.35, vy = Math.sin(a) * ry * 1.35;
        if (s === 0) { ctx.moveTo(vx, vy); } else { ctx.lineTo(vx, vy); }
      }
      ctx.closePath();
      ctx.fillStyle = 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ',' + Math.min(0.9, alpha) + ')';
      ctx.fill();
      /* 棱线：切面边缘的一线白光，交代「晶格」 */
      ctx.strokeStyle = 'rgba(255,255,255,' + Math.min(0.5, alpha * 0.55) + ')';
      ctx.lineWidth = 0.9;
      ctx.stroke();
      ctx.restore();
    }

    /* 宝石体感：中心一团极弱的高光，避免整屏碎片显得平 */
    var sheen = ctx.createRadialGradient(w * 0.5, h * 0.42, 0, w * 0.5, h * 0.5, w * 0.7);
    sheen.addColorStop(0, 'rgba(255,255,255,.055)');
    sheen.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = sheen;
    ctx.fillRect(0, 0, w, h);
  }

  /* (6) 双子流体（星云 / 白瓷共用几何，光学混合完全不同）
     四核旋涡极光场 + 9 根居中真丝贝塞尔流光弦线 + 四角双子星芒 */
  function renderGeminiFluid(ctx, w, h, tauVal, isPorcelain) {
    var bg = ctx.createLinearGradient(0, 0, w * 0.6, h);
    if (isPorcelain) {
      bg.addColorStop(0, '#ffffff');
      bg.addColorStop(0.55, '#f6f8fc');
      bg.addColorStop(1, '#eef2fa');
    } else {
      bg.addColorStop(0, '#06070f');
      bg.addColorStop(0.55, '#080a12');
      bg.addColorStop(1, '#0b0d18');
    }
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, w, h);

    ctx.save();
    /* 星云靠加光叠加，白瓷靠乘色叠加 —— 两种完全不同的光学模型 */
    ctx.globalCompositeOperation = isPorcelain ? 'multiply' : 'screen';

    /* 四核：绕画布中心公转，核间距离随时间呼吸 */
    for (var i = 0; i < GEMINI_CORES.length; i++) {
      var b = GEMINI_CORES[i];
      var orbAng = t * 0.22 * (i % 2 === 0 ? 1 : -1) + i * 1.57;
      var orbitR = 0.10 + 0.02 * Math.sin(t * 0.4 + i);
      var bx = (b.u + Math.cos(orbAng) * orbitR) * w;
      var by = (b.v + Math.sin(orbAng) * orbitR * 0.8) * h;
      var br = b.r * w * (1 + tauVal * 0.12);
      var a0 = isPorcelain ? 0.20 + tauVal * 0.06 : 0.42 + tauVal * 0.12;
      var g = ctx.createRadialGradient(bx, by, 2, bx, by, br);
      g.addColorStop(0, 'rgba(' + b.col[0] + ',' + b.col[1] + ',' + b.col[2] + ',' + a0 + ')');
      g.addColorStop(0.42, 'rgba(' + b.col[0] + ',' + b.col[1] + ',' + b.col[2] + ',' + (a0 * 0.5) + ')');
      g.addColorStop(1, 'rgba(' + b.col[0] + ',' + b.col[1] + ',' + b.col[2] + ',0)');
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(bx, by, br, 0, Math.PI * 2);
      ctx.fill();
    }

    /* 真丝弦线：9 根横贯全屏的贝塞尔，相位错开形成丝绸褶皱 */
    for (var line = 0; line < 9; line++) {
      var offset = (line - 4) * (h * 0.028);
      var fade = 1 - Math.abs(line - 4) * 0.16;
      ctx.strokeStyle = isPorcelain
        ? 'rgba(110,96,196,' + (0.13 * fade + tauVal * 0.03) + ')'
        : 'rgba(198,216,255,' + (0.20 * fade + tauVal * 0.05) + ')';
      ctx.lineWidth = 0.8 + fade * 0.9;
      ctx.beginPath();
      ctx.moveTo(-w * 0.05, h * 0.56 + offset);
      ctx.bezierCurveTo(
        w * 0.30, h * (0.20 + 0.16 * Math.sin(t * 0.55 + line * 0.20)) + offset,
        w * 0.70, h * (0.80 + 0.16 * Math.cos(t * 0.48 + line * 0.20)) + offset,
        w * 1.05, h * 0.44 + offset
      );
      ctx.stroke();
    }
    ctx.restore();

    /* 四角双子星芒：每个角一枚四向星，长度随时间呼吸 */
    for (var si = 0; si < GEMINI_STARS.length; si++) {
      var st = GEMINI_STARS[si];
      var sxp = st.u * w, syp = st.v * h;
      var pulse = 0.55 + 0.45 * Math.sin(t * 1.4 + si * 1.7);
      var len = w * 0.045 * st.s * (0.6 + pulse * 0.7);
      ctx.strokeStyle = isPorcelain
        ? 'rgba(90,80,170,' + (0.16 + pulse * 0.12) + ')'
        : 'rgba(255,255,255,' + (0.20 + pulse * 0.22) + ')';
      ctx.lineWidth = 1.1;
      ctx.beginPath();
      ctx.moveTo(sxp - len, syp); ctx.lineTo(sxp + len, syp);
      ctx.moveTo(sxp, syp - len); ctx.lineTo(sxp, syp + len);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(sxp, syp, 1.6 + pulse, 0, Math.PI * 2);
      ctx.fillStyle = isPorcelain ? 'rgba(90,80,170,.5)' : 'rgba(255,255,255,.75)';
      ctx.fill();
    }

    /* 星尘 / 磁粉：星云是亮点，白瓷是淡彩尘 */
    var count = isPorcelain ? 34 : 46;
    for (var mi = 0; mi < count; mi++) {
      var m = MOTES[mi];
      var px = ((m.u + t * 0.010 + 1) % 1) * w;
      var py = ((m.v + Math.sin(t + mi) * 0.02 + 1) % 1) * h;
      var sz = (2.2 + (mi % 3) * 1.8) * (0.55 + 0.45 * Math.sin(t * 2.2 + m.phase));
      if (sz <= 0.2) { continue; }
      ctx.fillStyle = isPorcelain ? '#8b93a8' : '#ffffff';
      ctx.globalAlpha = isPorcelain ? 0.30 : 0.70;
      /* 十字星形点，不是圆点 —— 真丝星芒的细节 */
      ctx.beginPath();
      ctx.moveTo(px, py - sz);
      ctx.quadraticCurveTo(px, py, px + sz, py);
      ctx.quadraticCurveTo(px, py, px, py + sz);
      ctx.quadraticCurveTo(px, py, px - sz, py);
      ctx.quadraticCurveTo(px, py, px, py - sz);
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  /* (7) 极光·点阵波束
     44×20 动态矩阵点阵 + 光学磁吸放大透镜 + 径向量子冲击波 */
  function renderHalftone(ctx, w, h, tauVal, mx, my) {
    ctx.fillStyle = '#070910';
    ctx.fillRect(0, 0, w, h);

    var cols = 44, rows = 20;
    var cellW = w / cols, cellH = h / rows;
    var maxR = Math.min(cellW, cellH) * 0.50;
    var pal = [[120, 217, 236], [66, 133, 244], [155, 114, 203], [244, 156, 70]];
    var lensR = Math.min(w, h) * 0.22;
    var heat = tauVal * 0.45;

    for (var r = 0; r < rows; r++) {
      for (var c = 0; c < cols; c++) {
        var nx = (c + 0.5) / cols, ny = (r + 0.5) / rows;
        var px = nx * w, py = ny * h;

        /* 光学透镜：距指针越近，点越大越亮（真实的凸透镜放大） */
        var lens = 0;
        if (pointer.active) {
          var dm = Math.hypot(px - mx, py - my);
          if (dm < lensR) {
            var k = 1 - dm / lensR;
            lens = k * k * 0.95;
          }
        }

        /* 量子冲击波：点击后从落点扩散的环 */
        var wave = 0;
        for (var bi = 0; bi < bursts.length; bi++) {
          var b = bursts[bi];
          var d = Math.hypot(nx - b.u, ny - b.v);
          var ringR = (b.age / b.maxAge) * 0.62;
          var dw = Math.abs(d - ringR);
          if (dw < 0.055) { wave += (1 - b.age / b.maxAge) * (1 - dw / 0.055) * 1.0; }
        }

        /* 两组不同频率的波场叠加，形成极光的流动感（不是单一正弦） */
        var w1 = Math.sin(nx * 6.2 + t * 1.25 + Math.cos(ny * 4.6 - t) * 1.35);
        var w2 = Math.cos(ny * 5.1 - t * 0.95 + Math.sin(nx * 5.2 + t * 0.8) * 1.3);
        var w3 = Math.sin((nx + ny) * 3.4 + t * 0.7);
        var energy = clamp01((w1 * 0.42 + w2 * 0.36 + w3 * 0.22 + 1) * 0.5 + lens + wave);

        if (energy > 0.16) {
          var rad = Math.max(0.7, maxR * (0.18 + energy * 0.92));
          var ci = (c + r * 3) % 4;
          var col = pal[ci];
          /* 高温时整体向暖色偏，冷色衰减 */
          var rC = Math.round(lerp(col[0], 255, heat * 0.55));
          var gC = Math.round(lerp(col[1], 120, heat * 0.30));
          var bC = Math.round(lerp(col[2], 60, heat * 0.25));
          ctx.fillStyle = 'rgba(' + rC + ',' + gC + ',' + bC + ',' + (0.16 + energy * 0.66) + ')';
          ctx.beginPath();
          ctx.arc(px, py, rad, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    }
  }

  /* ------------------------------------------------------------------
   * 4. 十二款皮肤专属鼠标跟随 / 点击特效
   * ------------------------------------------------------------------ */
  function trailColorStub() { return '#93c5fd'; }

  function renderTrailHead(fxCtx, theme, tx, ty, life, pt, prev) {
    switch (theme) {
      case 'blue-porcelain': {
        if (!prev) { return; }
        fxCtx.strokeStyle = tau > 0.5
          ? 'rgba(166,44,43,' + (life * 0.55) + ')'
          : 'rgba(22,56,112,' + (life * 0.55) + ')';
        fxCtx.lineWidth = 2.4 * life;
        fxCtx.lineCap = 'round';
        fxCtx.beginPath();
        fxCtx.moveTo(prev.u * fxCanvas.width, prev.v * fxCanvas.height);
        fxCtx.lineTo(tx, ty);
        fxCtx.stroke();
        return;
      }
      case 'sunrise-gold': {
        fxCtx.fillStyle = '#ffe082';
        fxCtx.globalAlpha = life * 0.62;
        fxCtx.beginPath();
        fxCtx.arc(tx, ty, 3.0 * life, 0, Math.PI * 2);
        fxCtx.fill();
        fxCtx.globalAlpha = 1;
        return;
      }
      case 'ink-wash': {
        var ir = (4 + (1 - pt.speed) * 9 + pt.age * 0.4) * (0.4 + life * 0.6);
        fxCtx.fillStyle = tau > 0.5
          ? 'rgba(195,52,32,' + (life * 0.26) + ')'
          : 'rgba(28,30,34,' + (life * 0.26) + ')';
        fxCtx.beginPath();
        fxCtx.arc(tx, ty, ir, 0, Math.PI * 2);
        fxCtx.fill();
        return;
      }
      case 'ferrofluid-chrome': {
        var fr = 2.0 + (1 - life) * 4.2;
        var fg = fxCtx.createRadialGradient(tx - fr * 0.35, ty - fr * 0.35, 0, tx, ty, fr);
        fg.addColorStop(0, 'rgba(255,255,255,' + (life * 0.9) + ')');
        fg.addColorStop(0.55, 'rgba(148,163,184,' + (life * 0.55) + ')');
        fg.addColorStop(1, 'rgba(30,41,59,0)');
        fxCtx.fillStyle = fg;
        fxCtx.beginPath();
        fxCtx.arc(tx, ty, fr, 0, Math.PI * 2);
        fxCtx.fill();
        return;
      }
      case 'black-opal': {
        var hues = tau > 0.5 ? ['255,65,85', '255,165,40', '0,230,150'] : ['0,230,140', '0,176,255', '145,85,255'];
        fxCtx.fillStyle = 'rgba(' + hues[pt.age % 3] + ',' + (life * 0.62) + ')';
        fxCtx.save();
        fxCtx.translate(tx, ty);
        fxCtx.rotate(pt.age * 0.4);
        fxCtx.beginPath();
        fxCtx.moveTo(0, -3.2 * life); fxCtx.lineTo(3.2 * life, 0);
        fxCtx.lineTo(0, 3.2 * life); fxCtx.lineTo(-3.2 * life, 0);
        fxCtx.closePath();
        fxCtx.fill();
        fxCtx.restore();
        return;
      }
      case 'gemini-nebula': {
        fxCtx.fillStyle = 'rgba(168,199,250,' + (life * 0.6) + ')';
        fxCtx.beginPath();
        fxCtx.arc(tx, ty, 2.4 * life, 0, Math.PI * 2);
        fxCtx.fill();
        return;
      }
      case 'gemini-porcelain': {
        fxCtx.fillStyle = tau > 0.5 ? 'rgba(217,101,112,' + (life * 0.4) + ')' : 'rgba(66,133,244,' + (life * 0.38) + ')';
        fxCtx.beginPath();
        fxCtx.arc(tx, ty, 5.2 * life, 0, Math.PI * 2);
        fxCtx.fill();
        return;
      }
      case 'gemini-halftone': {
        var hx = Math.round(tx / 9) * 9, hy = Math.round(ty / 9) * 9;
        fxCtx.fillStyle = 'rgba(120,217,236,' + (life * 0.72) + ')';
        fxCtx.fillRect(hx - 1.6, hy - 1.6, 3.2 * life + 1, 3.2 * life + 1);
        return;
      }
      case 'prism': {
        var pc = ['244,114,182', '167,139,250', '34,211,238'];
        fxCtx.fillStyle = 'rgba(' + pc[pt.age % 3] + ',' + (life * 0.6) + ')';
        fxCtx.beginPath();
        fxCtx.moveTo(tx, ty - 3.4 * life);
        fxCtx.lineTo(tx + 3.0 * life, ty + 2.0 * life);
        fxCtx.lineTo(tx - 3.0 * life, ty + 2.0 * life);
        fxCtx.closePath();
        fxCtx.fill();
        return;
      }
      case 'rainbow': {
        if (!prev) { return; }
        var hue = (pt.age * 24) % 360;
        fxCtx.strokeStyle = 'hsla(' + hue + ',92%,70%,' + (life * 0.62) + ')';
        fxCtx.lineWidth = 2.6 * life;
        fxCtx.beginPath();
        fxCtx.moveTo(prev.u * fxCanvas.width, prev.v * fxCanvas.height);
        fxCtx.lineTo(tx, ty);
        fxCtx.stroke();
        return;
      }
      case 'liquid-glass': {
        fxCtx.strokeStyle = 'rgba(214,243,255,' + (life * 0.55) + ')';
        fxCtx.lineWidth = 1.6;
        fxCtx.beginPath();
        fxCtx.arc(tx, ty, 3.4 + (1 - life) * 5, 0, Math.PI * 2);
        fxCtx.stroke();
        return;
      }
      case 'forest-breath': {
        fxCtx.fillStyle = 'rgba(167,243,208,' + (life * 0.52) + ')';
        fxCtx.beginPath();
        fxCtx.ellipse(tx, ty, 3.4 * life, 1.8 * life, pt.age * 0.12, 0, Math.PI * 2);
        fxCtx.fill();
        return;
      }
      default: {
        fxCtx.fillStyle = trailColorStub();
        fxCtx.globalAlpha = life * 0.55;
        fxCtx.beginPath();
        fxCtx.arc(tx, ty, 2.8 * life, 0, Math.PI * 2);
        fxCtx.fill();
        fxCtx.globalAlpha = 1;
      }
    }
  }

  function renderBurst(fxCtx, theme, b) {
    var p = b.age / b.maxAge;
    var blife = 1 - p;
    var bx = b.u * fxCanvas.width;
    var by = b.v * fxCanvas.height;
    var maxR = Math.min(fxCanvas.width, fxCanvas.height) * 0.22;

    switch (theme) {
      case 'blue-porcelain': {
        /* 冰裂放射：一笔落下的裂纹 + 一圈釉面涟漪 */
        fxCtx.strokeStyle = tau > 0.5
          ? 'rgba(166,44,43,' + (blife * 0.78) + ')'
          : 'rgba(22,56,112,' + (blife * 0.74) + ')';
        fxCtx.lineWidth = 1.4 * blife;
        for (var i = 0; i < b.angles.length; i++) {
          var ang = b.angles[i];
          fxCtx.beginPath();
          fxCtx.moveTo(bx, by);
          fxCtx.lineTo(bx + Math.cos(ang + 0.20) * p * maxR * 0.42, by + Math.sin(ang + 0.20) * p * maxR * 0.42);
          fxCtx.lineTo(bx + Math.cos(ang - 0.14) * p * maxR, by + Math.sin(ang - 0.14) * p * maxR);
          fxCtx.stroke();
        }
        fxCtx.beginPath();
        fxCtx.arc(bx, by, p * maxR * 0.78, 0, Math.PI * 2);
        fxCtx.stroke();
        return;
      }
      case 'sunrise-gold': {
        /* 金光放射：16 道细长光针 + 中心暖核 */
        fxCtx.strokeStyle = 'rgba(255,224,130,' + (blife * 0.88) + ')';
        fxCtx.lineWidth = 2.2 * blife;
        for (var j = 0; j < b.angles.length; j++) {
          var a2 = b.angles[j];
          fxCtx.beginPath();
          fxCtx.moveTo(bx, by);
          fxCtx.lineTo(bx + Math.cos(a2) * p * maxR * 1.2, by + Math.sin(a2) * p * maxR * 1.2);
          fxCtx.stroke();
        }
        var core = fxCtx.createRadialGradient(bx, by, 0, bx, by, p * maxR * 0.4);
        core.addColorStop(0, 'rgba(255,246,214,' + (blife * 0.55) + ')');
        core.addColorStop(1, 'rgba(255,180,80,0)');
        fxCtx.fillStyle = core;
        fxCtx.beginPath();
        fxCtx.arc(bx, by, p * maxR * 0.4, 0, Math.PI * 2);
        fxCtx.fill();
        return;
      }
      case 'ink-wash': {
        /* 泼墨：不规则毛边的墨团向外洇开 */
        var c = tau > 0.5 ? [195, 52, 32] : [28, 30, 34];
        fxCtx.fillStyle = 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + (blife * 0.34) + ')';
        fxCtx.strokeStyle = 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + (blife * 0.6) + ')';
        fxCtx.lineWidth = 1.2;
        fxCtx.beginPath();
        for (var s2 = 0; s2 <= 34; s2++) {
          var aa = (s2 / 34) * Math.PI * 2;
          var wob = 1 + 0.24 * Math.sin(aa * 7 + b.u * 12) + 0.12 * Math.cos(aa * 13);
          var rad2 = Math.pow(p, 0.6) * maxR * 0.6 * wob;
          var qx2 = bx + Math.cos(aa) * rad2, qy2 = by + Math.sin(aa) * rad2;
          if (s2 === 0) { fxCtx.moveTo(qx2, qy2); } else { fxCtx.lineTo(qx2, qy2); }
        }
        fxCtx.closePath();
        fxCtx.fill();
        fxCtx.stroke();
        return;
      }
      case 'ferrofluid-chrome': {
        /* 磁爆：尖刺环 + 金属描边 */
        var spikes = 12;
        fxCtx.beginPath();
        for (var q2 = 0; q2 <= spikes * 2; q2++) {
          var aq = (q2 / (spikes * 2)) * Math.PI * 2;
          var rr2 = p * maxR * (q2 % 2 === 0 ? 1.0 : 0.55);
          var xq = bx + Math.cos(aq) * rr2, yq = by + Math.sin(aq) * rr2;
          if (q2 === 0) { fxCtx.moveTo(xq, yq); } else { fxCtx.lineTo(xq, yq); }
        }
        fxCtx.closePath();
        fxCtx.strokeStyle = 'rgba(226,232,240,' + (blife * 0.8) + ')';
        fxCtx.lineWidth = 1.8 * blife;
        fxCtx.stroke();
        return;
      }
      case 'black-opal': {
        /* 晶格炸裂：三圈错位棱形 */
        for (var k2 = 0; k2 < 3; k2++) {
          var pr = p * maxR * (0.55 + k2 * 0.28);
          var cols = tau > 0.5 ? ['255,65,85', '255,165,40', '0,230,150'] : ['0,230,140', '0,176,255', '145,85,255'];
          fxCtx.strokeStyle = 'rgba(' + cols[k2] + ',' + (blife * 0.72) + ')';
          fxCtx.lineWidth = 1.5 * blife;
          fxCtx.save();
          fxCtx.translate(bx, by);
          fxCtx.rotate(k2 * 0.5 + p * 1.2);
          fxCtx.beginPath();
          for (var v = 0; v < 6; v++) {
            var av = (v / 6) * Math.PI * 2;
            var vx2 = Math.cos(av) * pr, vy2 = Math.sin(av) * pr * 0.82;
            if (v === 0) { fxCtx.moveTo(vx2, vy2); } else { fxCtx.lineTo(vx2, vy2); }
          }
          fxCtx.closePath();
          fxCtx.stroke();
          fxCtx.restore();
        }
        return;
      }
      case 'gemini-nebula':
      case 'gemini-porcelain': {
        /* 双子星芒：四向十字星 + 柔光同心 */
        var isPorc = theme === 'gemini-porcelain';
        var col2 = isPorc ? '110,96,196' : '255,255,255';
        var len2 = p * maxR;
        fxCtx.strokeStyle = 'rgba(' + col2 + ',' + (blife * (isPorc ? 0.5 : 0.8)) + ')';
        fxCtx.lineWidth = 1.6 * blife;
        fxCtx.beginPath();
        fxCtx.moveTo(bx - len2, by); fxCtx.lineTo(bx + len2, by);
        fxCtx.moveTo(bx, by - len2); fxCtx.lineTo(bx, by + len2);
        fxCtx.stroke();
        fxCtx.beginPath();
        fxCtx.arc(bx, by, len2 * 0.72, 0, Math.PI * 2);
        fxCtx.stroke();
        return;
      }
      case 'gemini-halftone': {
        /* 点阵冲击波：由背景点阵自身承载，这里只补一圈虚线 */
        fxCtx.strokeStyle = 'rgba(120,217,236,' + (blife * 0.4) + ')';
        fxCtx.lineWidth = 1;
        fxCtx.setLineDash([4, 6]);
        fxCtx.beginPath();
        fxCtx.arc(bx, by, p * maxR * 0.9, 0, Math.PI * 2);
        fxCtx.stroke();
        fxCtx.setLineDash([]);
        return;
      }
      case 'prism': {
        /* 棱镜折射：三片扇形色散 */
        for (var m2 = 0; m2 < 3; m2++) {
          var cols2 = ['244,114,182', '167,139,250', '34,211,238'];
          fxCtx.strokeStyle = 'rgba(' + cols2[m2] + ',' + (blife * 0.66) + ')';
          fxCtx.lineWidth = 2.0 * blife;
          fxCtx.beginPath();
          fxCtx.arc(bx, by, p * maxR * (0.5 + m2 * 0.2), (m2 * 2 * Math.PI) / 3, (m2 * 2 * Math.PI) / 3 + 1.7);
          fxCtx.stroke();
        }
        return;
      }
      case 'rainbow': {
        /* 极光波纹：六道错频同心环 */
        for (var n2 = 0; n2 < 6; n2++) {
          var hue2 = (n2 * 60 + p * 180) % 360;
          fxCtx.strokeStyle = 'hsla(' + hue2 + ',90%,68%,' + (blife * 0.6) + ')';
          fxCtx.lineWidth = 2.2 * blife;
          fxCtx.beginPath();
          fxCtx.arc(bx, by, p * maxR * (0.35 + n2 * 0.13), 0, Math.PI * 2);
          fxCtx.stroke();
        }
        return;
      }
      case 'liquid-glass': {
        /* 玻璃涟漪：双环 + 折射高光 */
        fxCtx.strokeStyle = 'rgba(224,244,255,' + (blife * 0.7) + ')';
        fxCtx.lineWidth = 1.8 * blife;
        fxCtx.beginPath();
        fxCtx.arc(bx, by, p * maxR * 0.85, 0, Math.PI * 2);
        fxCtx.stroke();
        fxCtx.strokeStyle = 'rgba(197,192,255,' + (blife * 0.45) + ')';
        fxCtx.lineWidth = 1.2;
        fxCtx.beginPath();
        fxCtx.arc(bx, by, p * maxR * 0.52, 0, Math.PI * 2);
        fxCtx.stroke();
        return;
      }
      case 'forest-breath': {
        /* 叶脉扩散：叶片形椭圆 + 主脉 */
        for (var l2 = 0; l2 < 4; l2++) {
          var ang2 = (l2 / 4) * Math.PI * 2 + 0.4;
          fxCtx.save();
          fxCtx.translate(bx, by);
          fxCtx.rotate(ang2);
          fxCtx.strokeStyle = 'rgba(167,243,208,' + (blife * 0.6) + ')';
          fxCtx.lineWidth = 1.3 * blife;
          fxCtx.beginPath();
          fxCtx.ellipse(p * maxR * 0.45, 0, p * maxR * 0.45, p * maxR * 0.2, 0, 0, Math.PI * 2);
          fxCtx.stroke();
          fxCtx.beginPath();
          fxCtx.moveTo(0, 0);
          fxCtx.lineTo(p * maxR * 0.9, 0);
          fxCtx.stroke();
          fxCtx.restore();
        }
        return;
      }
      default: {
        fxCtx.strokeStyle = 'rgba(147,197,253,' + (blife * 0.72) + ')';
        fxCtx.lineWidth = 1.8 * blife;
        fxCtx.beginPath();
        fxCtx.arc(bx, by, p * maxR * 0.85, 0, Math.PI * 2);
        fxCtx.stroke();
      }
    }
  }

  function renderInteractiveFx(fxCtx, w, h, theme) {
    fxCtx.clearRect(0, 0, w, h);
    var i;

    for (i = trail.length - 1; i >= 0; i--) {
      var pt = trail[i];
      pt.age += 1;
      if (pt.age > 24) { trail.splice(i, 1); continue; }
      var life = 1 - pt.age / 24;
      renderTrailHead(fxCtx, theme, pt.u * w, pt.v * h, life, pt, trail[i - 1]);
    }

    for (i = bursts.length - 1; i >= 0; i--) {
      var b = bursts[i];
      b.age += 1;
      if (b.age >= b.maxAge) { bursts.splice(i, 1); continue; }
      renderBurst(fxCtx, theme, b);
    }
  }

  /* ------------------------------------------------------------------
   * 5. 画布挂载 / 受控循环 / 事件绑定
   * ------------------------------------------------------------------ */
  var bgCanvas = null, bgCtx = null, fxCanvas = null, fxCtx = null;
  var rafId = 0, lastPaint = 0, lastSig = '', dirty = true;

  function resolveTheme(raw) {
    if (!raw) { return 'sunrise-gold'; }
    return LEGACY_MAP[raw] || raw;
  }

  function ensureCanvases() {
    var amb = document.querySelector('.ambient-bg');
    if (!amb) { return false; }
    if (!bgCanvas || !bgCanvas.parentNode) {
      bgCanvas = amb.querySelector('.hw-material-canvas');
      if (!bgCanvas) {
        bgCanvas = document.createElement('canvas');
        bgCanvas.className = 'hw-material-canvas';
        bgCanvas.setAttribute('aria-hidden', 'true');
        amb.insertBefore(bgCanvas, amb.firstChild);
      }
      bgCtx = bgCanvas.getContext('2d');
    }
    if (!fxCanvas || !fxCanvas.parentNode) {
      fxCanvas = document.querySelector('.hw-fx-canvas');
      if (!fxCanvas) {
        fxCanvas = document.createElement('canvas');
        fxCanvas.className = 'hw-fx-canvas';
        fxCanvas.setAttribute('aria-hidden', 'true');
        document.body.appendChild(fxCanvas);
      }
      fxCtx = fxCanvas.getContext('2d');
    }
    return !!(bgCtx && fxCtx);
  }

  function resizeCanvases() {
    /* 无刷新切页会重建 body 内容，画布若被摘掉就重新挂一次 */
    if (!bgCanvas || !bgCanvas.parentNode || !fxCanvas || !fxCanvas.parentNode) {
      if (!ensureCanvases()) { return; }
    }
    if (!bgCanvas || !fxCanvas) { return; }
    var vw = window.innerWidth || 1280;
    var vh = window.innerHeight || 720;
    var theme = resolveTheme(document.body && document.body.getAttribute('data-theme'));
    var scale = BG_SCALE[theme] || 0.6;
    var bw = Math.max(360, Math.round(vw * scale));
    var bh = Math.max(240, Math.round(vh * scale));
    if (bgCanvas.width !== bw || bgCanvas.height !== bh) {
      bgCanvas.width = bw;
      bgCanvas.height = bh;
    }
    var dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    var fw = Math.round(vw * dpr), fh = Math.round(vh * dpr);
    if (fxCanvas.width !== fw || fxCanvas.height !== fh) {
      fxCanvas.width = fw;
      fxCanvas.height = fh;
    }
    dirty = true;
  }

  function drawBackground(theme) {
    if (!bgCtx) { return; }
    var w = bgCanvas.width, h = bgCanvas.height;
    var mx = pointer.x * w, my = pointer.y * h;
    bgCtx.setTransform(1, 0, 0, 1, 0, 0);
    bgCtx.globalAlpha = 1;
    bgCtx.globalCompositeOperation = 'source-over';

    switch (theme) {
      case 'blue-porcelain': renderBluePorcelain(bgCtx, w, h, tau, mx, my); break;
      case 'sunrise-gold': renderSunriseGold(bgCtx, w, h, tau); break;
      case 'ink-wash': renderInkWash(bgCtx, w, h, tau); break;
      case 'ferrofluid-chrome': renderFerrofluid(bgCtx, w, h, tau, mx, my); break;
      case 'black-opal': renderBlackOpal(bgCtx, w, h, tau, mx, my); break;
      case 'gemini-nebula': renderGeminiFluid(bgCtx, w, h, tau, false); break;
      case 'gemini-porcelain': renderGeminiFluid(bgCtx, w, h, tau, true); break;
      case 'gemini-halftone': renderHalftone(bgCtx, w, h, tau, mx, my); break;
      default: break;
    }
  }

  function tick(now) {
    if (!window.requestAnimationFrame) { return; }
    rafId = window.requestAnimationFrame(tick);
    var body = document.body;
    if (!body) { return; }
    var motion = body.getAttribute('data-motion-effective') || 'full';
    var paused = body.getAttribute('data-fx-paused') === 'true';
    var theme = resolveTheme(body.getAttribute('data-theme'));
    var isCanvas = !!CANVAS_SKINS[theme];
    var sig = theme + '|' + motion + '|' + (paused ? '1' : '0');

    if (sig !== lastSig) { lastSig = sig; dirty = true; }

    if (paused) { return; }

    /* off：只保证一帧正确的静态材质，不产生持续绘制。
       lite：照常流动，降频到 ~12 FPS（手机/省流场景），不跑鼠标特效。
       full：24 FPS + 鼠标特效。
       时间推进按真实 dt 计算，换档不会让材质「跳一下」或变速。 */
    if (motion === 'off') {
      if (!dirty) { return; }
      if (now - lastPaint < 90) { return; }
      lastPaint = now;
      dirty = false;
      tau = tauTarget;
      if (isCanvas) { drawBackground(theme); }
      if (fxCtx) { fxCtx.clearRect(0, 0, fxCanvas.width, fxCanvas.height); fxCtx.__dirtyFx = false; }
      return;
    }

    var isLite = (motion === 'lite');
    var frameGap = isLite ? 83 : 41;
    if (now - lastPaint < frameGap) { return; }
    var dt = lastPaint ? Math.min(now - lastPaint, 200) : frameGap;
    lastPaint = now;

    tau += (tauTarget - tau) * (isLite ? 0.12 : 0.075);
    if (isCanvas) { drawBackground(theme); }

    var hasFx = !isLite && (trail.length > 0 || bursts.length > 0);
    if (fxCtx) {
      if (hasFx) {
        renderInteractiveFx(fxCtx, fxCanvas.width, fxCanvas.height, theme);
        fxCtx.__dirtyFx = true;
      } else if (fxCtx.__dirtyFx !== false) {
        fxCtx.clearRect(0, 0, fxCanvas.width, fxCanvas.height);
        fxCtx.__dirtyFx = false;
      }
    }
    dirty = false;

    /* 温度越高，材质内部流速越快。
       按真实帧间隔推进，24 FPS（41ms）与 lite 12 FPS（83ms）的流速一致。 */
    t += dt * 0.00044 * (0.45 + tau * 1.85);
  }

  var startTries = 0;
  function start() {
    if (!document.body) { return; }
    if (!ensureCanvases()) {
      /* .ambient-bg 由顶栏脚本注入，最多等 60 帧 */
      if (startTries++ < 60) { window.requestAnimationFrame(start); }
      return;
    }
    resizeCanvases();
    bindEvents();
    if (window.requestAnimationFrame && !rafId) { rafId = window.requestAnimationFrame(tick); }
  }

  var bound = false;
  /* 顶栏脚本为了「无刷新切页」会包一层 window/document.addEventListener，
     并在 teardownView() 时把登记过的监听器全部摘掉。引擎的监听器必须
     活过切页，所以这里直接走 EventTarget 原型，绕开那层登记。 */
  var rawAdd = (window.EventTarget && window.EventTarget.prototype && window.EventTarget.prototype.addEventListener)
    ? window.EventTarget.prototype.addEventListener : null;
  function onWin(type, fn, opts) {
    if (rawAdd) { rawAdd.call(window, type, fn, opts); }
    else { window.addEventListener(type, fn, opts); }
  }
  function onDoc(type, fn, opts) {
    if (rawAdd) { rawAdd.call(document, type, fn, opts); }
    else { document.addEventListener(type, fn, opts); }
  }

  function bindEvents() {
    if (bound) { return; }
    bound = true;

    onWin('resize', function () { resizeCanvases(); }, { passive: true });
    onWin('orientationchange', function () { resizeCanvases(); }, { passive: true });

    onWin('pointermove', function (e) {
      var nx = e.clientX / (window.innerWidth || 1);
      var ny = e.clientY / (window.innerHeight || 1);
      pointer.vx = nx - pointer.x;
      pointer.vy = ny - pointer.y;
      pointer.x = nx;
      pointer.y = ny;
      pointer.active = true;
      var body = document.body;
      if (!body) { return; }
      if (body.getAttribute('data-motion-effective') !== 'full') { return; }
      var spd = Math.min(1, Math.hypot(pointer.vx, pointer.vy) * 18);
      trail.push({ u: nx, v: ny, age: 0, speed: spd });
      if (trail.length > 30) { trail.shift(); }
    }, { passive: true });

    onWin('pointerdown', function (e) {
      var body = document.body;
      if (!body) { return; }
      if (body.getAttribute('data-motion-effective') === 'off') { return; }
      var nx = e.clientX / (window.innerWidth || 1);
      var ny = e.clientY / (window.innerHeight || 1);
      var angles = [];
      for (var k = 0; k < 12; k++) { angles.push((k / 12) * Math.PI * 2 + Math.random() * 0.2); }
      bursts.push({ u: nx, v: ny, age: 0, maxAge: 54, angles: angles });
      if (bursts.length > 6) { bursts.shift(); }
    }, { passive: true });

    /* 切到后台立即停笔，回前台继续 */
    onDoc('visibilitychange', function () { dirty = true; });

    /* 换肤事件：换主题后立刻按新倍率重建画布 */
    onWin('hwmon:theme', function () {
      resizeCanvases();
      dirty = true;
      if (fxCtx) { fxCtx.__dirtyFx = true; }
      var m = document.body && document.body.getAttribute('data-motion-effective');
      if (m && m !== 'full') { drawBackground(resolveTheme(document.body.getAttribute('data-theme'))); }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
