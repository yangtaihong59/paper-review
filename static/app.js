// 论文审阅工具前端（无构建步骤；KaTeX 为本地 vendor）。
'use strict';
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const LANG_NAME = { zh: '中文', en: '英文', both: '中英' };
  const STATUS_NAME = { open: '待处理', addressed: '已处理', declined: '不采纳' };
  const SYNC_BADGE = {
    aligned: ['已同步', 'ok synced', '中英指纹一致（zh_en_sync check）'],
    'en-pending': ['英文待同步', 'warn', '中文已改，英文编号行的 zh= 指纹未更新'],
    'zh-only': ['仅中文', 'bad', '英文稿没有这个编号'],
    'en-only': ['仅英文', 'bad', '中文稿没有这个编号'],
    'en-drift': ['英文单边改动', 'warn', '中文未变而英文变了（需回写中文或登记仅英文润色）'],
    unmarked: ['未编号', 'plain', '没有 %%¶ 编号行的文字'],
    monolingual: ['单语', 'plain', '补充材料只有一种语言'],
  };
  const LEVEL = { part: 0, section: 1, subsection: 2, subsubsection: 3 };

  const S = {
    papers: [], pid: null, doc: null, base: null, supp: false,
    notes: [], notesBy: new Map(), rounds: [], reads: {}, notesStamp: '',
    view: 'dual', filters: { changed: false, notes: false, unread: false, status: '', diff: false },
    rowEls: [], scopes: [], selection: null, observer: null, refOptions: [], loading: false,
  };

  // 界面状态（每个浏览器各自记住，见 prefs）。默认只露出一行极简顶栏，其余都在“设置”里。
  const UI = {
    autohide: true, barHidden: false, toc: false, notes: false, legend: true, width: 'm',
    sideSingle: true, sideDual: false, away: false, settings: false,
  };
  const isMobile = () => window.matchMedia('(max-width: 900px)').matches;

  const prefs = {
    get(key, fallback) {
      try { const v = localStorage.getItem('paper-review:' + key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
    },
    set(key, value) { try { localStorage.setItem('paper-review:' + key, JSON.stringify(value)); } catch { /* 浏览器禁用存储时忽略 */ } },
  };

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pidOf = (row) => row.id || row.key;
  const rowDomId = (row, i) => (row.id ? 'p-' + row.id : 'r-' + i);
  const anchorId = (lang, key) => `L-${lang}-${key}`;

  async function api(path, { method = 'GET', body } = {}) {
    const opts = { method, headers: {} };
    if (method !== 'GET') {
      opts.headers['X-Paper-Review'] = '1';
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body || {});
    }
    const res = await fetch('api/' + path, opts);
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (!res.ok) throw new Error((data && data.detail) || `${res.status} ${res.statusText}`);
    return data;
  }

  function fmtTime(value) {
    if (value === null || value === undefined || value === '') return '–';
    const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  // 当天的时间只显示时分。
  function fmtShort(value) {
    if (value === null || value === undefined || value === '') return '–';
    const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    const now = new Date();
    if (d.toDateString() !== now.toDateString()) return fmtTime(value);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  }

  let toastTimer = null;
  function toast(message, error = false) {
    const t = $('#toast');
    t.textContent = message;
    t.className = error ? 'error' : '';
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, error ? 6000 : 2600);
  }

  function showBanner(text, action, handler) {
    const b = $('#banner');
    b.innerHTML = `<span>${esc(text)}</span>` + (action ? `<button class="btn small" type="button">${esc(action)}</button>` : '');
    b.hidden = false;
    if (action) b.querySelector('button').addEventListener('click', handler, { once: true });
    syncHeaderHeight();
  }
  function hideBanner() { $('#banner').hidden = true; syncHeaderHeight(); }

  // ---------------------------------------------------------------- 偏好
  function applyPrefs() {
    S.view = prefs.get('view', 'dual');
    setView(S.view, false);
    setTheme(prefs.get('theme', 'auto'), false);
    setFont(prefs.get('font', 'serif'), false);
    setSize(prefs.get('size', 'm'), false);
    setWidth(prefs.get('width', 'm'), false);
    $('#s-ids').checked = prefs.get('ids', true);
    $('#s-synced').checked = prefs.get('synced', false);
    document.body.classList.toggle('hide-ids', !$('#s-ids').checked);
    document.body.classList.toggle('show-synced', $('#s-synced').checked);
    UI.autohide = prefs.get('autohide', true) !== false;
    UI.barHidden = prefs.get('barHidden', false) === true;
    UI.toc = prefs.get('tocCollapsed', false) === true;
    UI.notes = prefs.get('notesCollapsed', false) === true;
    UI.legend = prefs.get('legendShown', true) !== false;
    UI.sideSingle = prefs.get('notesSideSingle', true) !== false;
    UI.sideDual = prefs.get('notesSideDual', false) === true;
    calibrateMeasure();
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { calibrateMeasure(); applyLayout(); });
    applyUi();
  }
  // 同一设置可能有两组按钮（顶栏与设置面板），按 data-属性统一标出当前值。
  function pressed(attr, value) {
    $$(`button[data-${attr}]`).forEach((b) => b.setAttribute('aria-pressed', String(b.dataset[attr] === value)));
  }
  function setView(view, save = true) {
    S.view = ['dual', 'zh', 'en'].includes(view) ? view : 'dual';
    document.body.classList.remove('view-dual', 'view-zh', 'view-en');
    document.body.classList.add('view-' + S.view);
    pressed('view', S.view);
    if (save) { prefs.set('view', S.view); if (S.doc) renderToc(); }
    if (document.getElementById('notes-layout')) applyLayout();
  }
  function setTheme(theme, save = true) {
    if (theme === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', theme);
    pressed('theme', theme);
    if (save) prefs.set('theme', theme);
  }
  function setFont(font, save = true) {
    document.body.classList.toggle('font-sans', font === 'sans');
    pressed('font', font);
    if (save) { prefs.set('font', font); calibrateMeasure(); applyLayout(); }
  }
  function setSize(size, save = true) {
    document.body.classList.remove('size-s', 'size-l');
    if (size !== 'm') document.body.classList.add('size-' + size);
    pressed('size', size);
    if (save) { prefs.set('size', size); calibrateMeasure(); applyLayout(); }
  }
  function setWidth(width, save = true) {
    UI.width = ['s', 'm', 'l'].includes(width) ? width : 'm';
    document.body.classList.remove('width-s', 'width-l');
    if (UI.width !== 'm') document.body.classList.add('width-' + UI.width);
    pressed('width', UI.width);
    if (save) { prefs.set('width', UI.width); calibrateMeasure(); applyLayout(); }
  }

  // 顶栏当前遮住的高度（自动隐藏或手动隐藏、滑出视野时为 0）。
  const barGone = () => UI.away || (UI.barHidden && !document.body.classList.contains('bar-peek'));
  const headerOffset = () => (barGone() ? 0 : $('#top').getBoundingClientRect().height);

  function syncHeaderHeight() {
    document.documentElement.style.setProperty('--header-h', Math.round(headerOffset()) + 'px');
  }

  // ---------------------------------------------------------------- 界面开关与快捷键
  function applyUi() {
    const body = document.body;
    body.classList.toggle('autohide', UI.autohide);
    body.classList.toggle('bar-hidden', UI.barHidden);
    body.classList.toggle('toc-collapsed', UI.toc);
    body.classList.toggle('notes-collapsed', UI.notes);
    body.classList.toggle('legend-hidden', !UI.legend);
    pressed('toc', UI.toc ? 'hide' : 'show');
    pressed('fold', UI.notes ? 'fold' : 'open');
    $$('.opt-autohide').forEach((box) => { box.checked = UI.autohide; });
    $('#s-legend').checked = UI.legend;
    if (!UI.autohide) setAway(false);
    syncHeaderHeight();
    applyLayout();
  }

  // 英文版心按实际字体校准：每行约 95 个字符（中文为每行 44 个汉字，按 em 计即可）；“版心”设置按比例缩放。
  const MEASURE_SAMPLE = 'Ground truth in simulation plays the role of privileged information for the policy. ';
  const WIDTH_SCALE = { s: 0.86, m: 1, l: 1.18 };
  function calibrateMeasure() {
    const probe = document.createElement('span');
    probe.className = 'cell en';
    probe.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap;left:-99999px;top:0;';
    probe.textContent = MEASURE_SAMPLE.repeat(4);
    document.body.appendChild(probe);
    const avg = probe.getBoundingClientRect().width / probe.textContent.length;
    probe.remove();
    if (avg > 2) document.body.style.setProperty('--measure-en', `${Math.round(avg * 95 * WIDTH_SCALE[UI.width || 'm'])}px`);
  }

  // 批注位置：单语视图默认放在右侧栏，双栏视图可选；窗口不够三栏舒适宽度时退回段下，手机上总在段下。
  const sidePref = () => (S.view === 'dual' ? UI.sideDual : UI.sideSingle);
  function sideFits() {
    if (isMobile()) return false;
    const doc = $('#doc');
    const style = getComputedStyle(doc);
    const width = doc.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const root = getComputedStyle(document.body);
    const fs = parseFloat(root.getPropertyValue('--fs')) || 16.5;
    const rail = parseFloat(root.getPropertyValue('--rail-w')) || 300;
    const gap = parseFloat(root.getPropertyValue('--rail-gap')) || 36;
    const text = S.view === 'dual' ? 2 * 28 * fs + 40 : 30 * fs;
    return width >= text + rail + gap;
  }
  function applyLayout() {
    const want = sidePref();
    const side = want && sideFits();
    const body = document.body;
    const changed = body.classList.contains('notes-side') !== side;
    body.classList.toggle('notes-side', side);
    pressed('layout', want ? 'side' : 'below');
    const seg = $('#notes-layout');
    if (seg) seg.classList.toggle('fallback', want && !side);
    const rail = $('#rail-toggle');
    if (rail) {
      rail.setAttribute('aria-pressed', String(want));
      rail.classList.toggle('fallback', want && !side);
      rail.title = want && !side ? '批注侧栏（s）：窗口太窄，暂时显示在段下' : want ? '批注在右侧栏（s）' : '批注在段落下方（s）';
      rail.setAttribute('aria-label', rail.title);
    }
    if (changed) setActive(null, { force: true });
    scheduleRail();
  }
  function toggleNotesLayout() {
    if (S.view === 'dual') setUi('sideDual', !UI.sideDual, 'notesSideDual');
    else setUi('sideSingle', !UI.sideSingle, 'notesSideSingle');
    if (sidePref() && !document.body.classList.contains('notes-side')) toast('窗口太窄，批注暂时显示在段下；加宽窗口或收起目录（t）后显示在侧栏');
  }

  function setUi(key, value, pref) {
    UI[key] = value;
    prefs.set(pref, value);
    applyUi();
  }

  function toggleBar() {
    setUi('barHidden', !UI.barHidden, 'barHidden');
    document.body.classList.remove('bar-peek');
    syncHeaderHeight();
    if (UI.barHidden) toast('顶栏已隐藏；按 h 或把鼠标移到页面顶端时显示');
  }

  function toggleToc() {
    if (isMobile()) {
      document.body.classList.toggle('toc-open');
      closeSettings();
      return;
    }
    setUi('toc', !UI.toc, 'tocCollapsed');
  }

  function toggleNotes() {
    $$('.row.notes-open').forEach((el) => el.classList.remove('notes-open'));
    setUi('notes', !UI.notes, 'notesCollapsed');
    toast(UI.notes ? '批注已全部收起；点段落旁的“批注 n”可单独展开' : '批注已全部展开');
  }

  function openSettings(section) {
    const panel = $('#settings-panel');
    panel.hidden = false;
    UI.settings = true;
    $('#settings-btn').setAttribute('aria-expanded', 'true');
    $('#sheet-scrim').hidden = !isMobile();
    setAway(false);
    if (section) {
      const el = panel.querySelector(`[data-section="${section}"]`);
      if (el) panel.scrollTop = Math.max(0, el.offsetTop - 44);
    }
  }
  function closeSettings() {
    if (!UI.settings) return;
    $('#settings-panel').hidden = true;
    UI.settings = false;
    $('#settings-btn').setAttribute('aria-expanded', 'false');
    $('#sheet-scrim').hidden = true;
  }
  function toggleSettings(section) { if (UI.settings) closeSettings(); else openSettings(section); }

  // 自动隐藏（默认开）：向下滚动时顶栏滑出，向上滚动或鼠标碰到页面顶端时回来。
  let lastScrollY = 0;
  function setAway(value) {
    if (UI.away === value) return;
    UI.away = value;
    document.body.classList.toggle('header-away', value);
    syncHeaderHeight();
  }
  function setPeek(value) {
    if (document.body.classList.contains('bar-peek') === value) return;
    document.body.classList.toggle('bar-peek', value);
    syncHeaderHeight();
  }

  function autohideOnScroll() {
    const y = window.scrollY;
    const dy = y - lastScrollY;
    lastScrollY = y;
    if (!UI.autohide) return;
    if (y < 60) { setAway(false); return; }
    const top = $('#top');
    const active = document.activeElement;
    const busy = UI.settings || top.matches(':hover')
      || (active && top.contains(active) && /^(SELECT|INPUT|TEXTAREA)$/.test(active.tagName));
    if (dy > 4 && !busy) setAway(true);
    else if (dy < -4) setAway(false);
  }

  function onShortcut(ev) {
    if (ev.defaultPrevented || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const t = ev.target;
    if (t instanceof Element && (t.isContentEditable || t.closest('input, textarea, select'))) return;
    if (document.querySelector('dialog[open], .zoom')) return;
    if (ev.key === 'Escape') { closeSettings(); return; }
    const actions = {
      h: toggleBar, b: () => toggleSettings(), ',': () => toggleSettings(), t: toggleToc, n: toggleNotes,
      s: toggleNotesLayout, '?': () => openSettings('keys'),
    };
    const action = actions[ev.key];
    if (!action) return;
    ev.preventDefault();
    action();
  }

  // ---------------------------------------------------------------- 载入
  async function init() {
    applyPrefs();
    bindChrome();
    new ResizeObserver(syncHeaderHeight).observe($('#top'));
    try {
      const info = await api('papers');
      S.papers = info.papers;
      const params = new URLSearchParams(location.search);
      const wanted = params.get('paper');
      S.pid = wanted && S.papers.some((p) => p.id === wanted) ? wanted : info.default;
      S.base = params.get('base') || null;
      S.supp = params.get('supp') === '1';
      const sel = $('#paper-select');
      sel.innerHTML = S.papers.map((p) => `<option value="${esc(p.id)}">${esc(p.dir)}</option>`).join('');
      sel.value = S.pid;
      sel.hidden = S.papers.length < 2;
      await loadDoc();
    } catch (err) {
      showDocError(err);
    }
    setInterval(poll, 20000);
  }

  function query(extra = {}) {
    const q = new URLSearchParams();
    if (S.base) q.set('base', S.base);
    if (S.supp) q.set('supp', '1');
    for (const [k, v] of Object.entries(extra)) q.set(k, v);
    return q.toString();
  }

  function updateUrl() {
    const q = new URLSearchParams();
    q.set('paper', S.pid);
    if (S.base) q.set('base', S.base);
    if (S.supp) q.set('supp', '1');
    history.replaceState(null, '', '?' + q.toString() + location.hash);
  }

  function showDocError(err) {
    $('#doc').innerHTML = `<div class="doc-error">载入失败：${esc(err && err.message ? err.message : err)}</div>`;
  }

  async function loadDoc({ keep = false, refresh = false } = {}) {
    if (S.loading) return;
    S.loading = true;
    const anchor = keep ? topVisibleRow() : null;
    if (!keep) $('#doc').innerHTML = '<div class="loading">正在解析稿件…</div>';
    try {
      const doc = await api(`papers/${S.pid}/document?${query(refresh ? { refresh: '1' } : {})}`);
      S.doc = doc;
      renderHeader();
      loadRefs();
      await Promise.all([loadNotes(), loadReads()]);
      renderDoc();
      renderToc();
      applyFilters();
      updateProgress();
      updateSubmit();
      hideBanner();
      if (doc.header.base.note) showBanner(doc.header.base.note);
      if (anchor) scrollToRow(anchor.id, anchor.offset);
      else if (location.hash) scrollToHash(location.hash);
    } catch (err) {
      showDocError(err);
    } finally {
      S.loading = false;
    }
  }

  function renderHeader() {
    const h = S.doc.header;
    document.title = `${h.title.zh || S.doc.paper.dir} · 审阅`;
    const name = $('#short-name');
    name.textContent = shortName(S.doc);
    name.title = [h.title.zh, h.title.en].filter(Boolean).join('\n') + '\n（点击回到开头）';
    const dirtyFiles = [...h.dirty.zh, ...h.dirty.en];
    const s = h.sync || {};
    let sync = s.tag ? `同步点 <code title="${esc(s.utc || '')}">${esc(s.tag)}</code>` : '尚未同步';
    if (s.tag && s.exists === false) sync += ' <span class="warn">本地无此 tag</span>';
    if (s.check_ok === false) sync += ` <span class="warn" title="${esc(s.check_error || '')}">同步检查不可用</span>`;
    else if (s.warnings) sync += ` <span class="warn" title="zh_en_sync check --json 的告警数">告警 ${s.warnings}</span>`;
    const lines = [
      `提交 <code title="${esc(h.commit.subject)}">${esc(h.commit.short)}</code>` +
        (dirtyFiles.length ? ` <span class="warn" title="未提交的改动：\n${esc(dirtyFiles.join('\n'))}">+${dirtyFiles.length} 未提交</span>` : ''),
      sync,
      `对比基准 <code>${esc(h.base.ref)}</code>${h.base.short ? `（${esc(h.base.short)}）` : ''}`,
      `渲染 ${fmtTime(h.build.rendered_at)}，耗时 ${h.build.seconds}s`,
      `PDF 中 ${fmtTime(h.build.pdf.zh)} · 英 ${fmtTime(h.build.pdf.en)}（编号取自 .aux）`,
    ];
    $('#meta-info').innerHTML = lines.map((line) => `<div>${line}</div>`).join('');
  }

  function shortName(doc) {
    const title = doc.header.title || {};
    const en = (title.en || '').split(':')[0].trim();
    if (en && en.length <= 20) return en;
    const zh = (title.zh || '').split('：')[0].trim();
    if (zh && zh.length <= 14) return zh;
    return doc.paper.id.toUpperCase();
  }

  async function loadRefs() {
    try { S.refOptions = (await api(`papers/${S.pid}/refs`)).options; } catch { S.refOptions = []; }
    fillBaseSelect();
  }

  function fillBaseSelect() {
    const sel = $('#base-select');
    const base = S.doc.header.base;
    const groups = new Map();
    let found = false;
    for (const o of S.refOptions) {
      if (!groups.has(o.group)) groups.set(o.group, []);
      groups.get(o.group).push(o);
      if (o.ref === base.ref) found = true;
    }
    let html = '';
    if (!found) html += `<option value="${esc(base.ref)}">${esc(base.ref)}</option>`;
    for (const [group, options] of groups) {
      html += `<optgroup label="${esc(group)}">` + options.map((o) =>
        `<option value="${esc(o.ref)}"${o.available ? '' : ' disabled'}>${esc(o.label)}</option>`).join('') + '</optgroup>';
    }
    html += '<option value="__custom">其他 ref…</option>';
    sel.innerHTML = html;
    sel.value = base.ref;
    sel.title = `“本轮修改”相对 ${base.ref}（${base.short}）` + (base.note ? '；' + base.note : '');
  }

  async function loadNotes() {
    const data = await api(`papers/${S.pid}/annotations`);
    S.notes = data.annotations;
    S.rounds = data.rounds;
    S.notesStamp = notesStamp(S.notes, S.rounds);
    S.notesBy = new Map();
    for (const n of S.notes) {
      if (!S.notesBy.has(n.para_id)) S.notesBy.set(n.para_id, []);
      S.notesBy.get(n.para_id).push(n);
    }
  }
  const notesStamp = (notes, rounds) => notes.length + '|' + notes.map((n) => n.updated_at).sort().pop() + '|' +
    rounds.map((r) => r.round + ':' + (r.submitted_at || '')).join(',');

  async function loadReads() {
    S.reads = (await api(`papers/${S.pid}/reads`)).reads;
  }

  // ---------------------------------------------------------------- 渲染
  function isChanged(row) { return !!row.deleted || ['zh', 'en'].some((l) => row.changed && row.changed[l]); }

  function badgeHtml(row) {
    const out = [];
    if (row.deleted) {
      out.push('<span class="badge del" title="对比基准中有、当前稿件已删除">本轮删除</span>');
    } else {
      const ch = row.changed || {};
      const langs = ['zh', 'en'].filter((l) => ch[l]);
      if (langs.length) {
        const added = langs.every((l) => ch[l] === 'added');
        const which = langs.length === 2 ? '中英' : (langs[0] === 'zh' ? '中' : '英');
        out.push(`<span class="badge chg" title="相对对比基准 ${esc(S.doc.header.base.ref)}">${added ? '本轮新增' : '本轮修改'} · ${which}</span>`);
      }
    }
    if (row.sync && row.sync.state !== 'deleted') {
      const [label, cls, tip] = SYNC_BADGE[row.sync.state] || [row.sync.state, 'plain', ''];
      out.push(`<span class="badge ${cls}" title="${esc(tip)}">${label}</span>`);
      const warnings = row.sync.warnings || [];
      if (warnings.length) {
        const text = warnings.map((w) => JSON.stringify(w)).join('\n');
        out.push(`<span class="badge warn" title="${esc(text)}">检查 ${warnings.length}</span>`);
      }
    }
    out.push('<span class="badge read read-mark" hidden>已读</span>');
    return out.join('');
  }

  function actionsHtml(row) {
    const diff = isChanged(row) && !row.deleted ? '<button class="btn ghost act-diff" type="button" aria-pressed="false" title="显示相对对比基准的改动">对比</button>' : '';
    return `<span class="row-actions">${diff}<button class="btn ghost act-read" type="button">标为已读</button>` +
      '<button class="btn ghost act-note" type="button">+ 批注</button></span>';
  }

  const noteCountHtml = () => '<button class="badge note-count" type="button" hidden></button>';

  function headingHtml(row, i) {
    const tag = { part: 'h2', section: 'h2', subsection: 'h3', subsubsection: 'h4' }[row.level] || 'h4';
    const cell = (lang) => {
      const c = row[lang];
      const L = lang === 'zh' ? 'zh-CN' : 'en';
      if (!c) return `<${tag} class="${lang} missing" data-lang="${lang}">（${lang === 'zh' ? '中文' : '英文'}稿无此标题）</${tag}>`;
      const id = c.anchor ? ` id="${esc(anchorId(lang, c.anchor))}"` : '';
      const num = c.number ? `<span class="hnum">${esc(c.number)}</span>` : '';
      return `<${tag} class="${lang}" lang="${L}" data-lang="${lang}"${id}>${num}${c.html}</${tag}>`;
    };
    const pid = pidOf(row);
    const pidLink = row.id ? `<a class="pid" href="#${esc(rowDomId(row, i))}">${esc(row.id)}</a>` : '';
    const missing = !row.zh || !row.en ? ' missing-side' : '';
    return `<section class="row heading lvl-${row.level}${missing}${isChanged(row) ? ' is-changed' : ''}" data-index="${i}" data-pid="${esc(pid)}" id="${esc(rowDomId(row, i))}">` +
      `<div class="row-head">${pidLink}<span class="badges">${badgeHtml(row)}${noteCountHtml()}</span>` +
      '<span class="row-actions"><button class="btn ghost act-note" type="button">+ 批注</button></span></div>' +
      `<div class="heading-cells">${cell('zh')}${cell('en')}</div><div class="notes"></div></section>`;
  }

  function cellHtml(row, lang) {
    const c = row[lang];
    const L = lang === 'zh' ? 'zh-CN' : 'en';
    if (row.deleted) {
      const d = row.diff && row.diff[lang];
      return `<div class="cell ${lang}" data-lang="${lang}" lang="${L}">` +
        (d ? `<div class="diff"><div class="diff-legend">已删除（基准 ${esc(S.doc.header.base.ref)} 中的原文）</div>${d}</div>` : `<div class="cell missing">（${LANG_NAME[lang]}基准中也没有）</div>`) + '</div>';
    }
    if (!c) return `<div class="cell ${lang} missing" data-lang="${lang}">${lang === 'zh' ? '中文稿中没有这个编号' : '英文稿中没有这个编号'}</div>`;
    return `<div class="cell ${lang}" data-lang="${lang}" lang="${L}"><div class="content">${c.html}</div></div>`;
  }

  function blockHtml(row, i) {
    const cls = ['row', 'block', 'kind-' + row.kind];
    if (row.wide) cls.push('wide');
    if (row.deleted) cls.push('deleted');
    if (row.media_shared && row.media && row.media.length) cls.push('media-shared');
    if (isChanged(row)) cls.push('is-changed');
    const where = ['zh', 'en'].filter((l) => row[l]).map((l) => `${LANG_NAME[l]} ${row[l].file}:${row[l].line}`).join('\n');
    const label = row.unmarked ? '未编号' : pidOf(row);
    const head = `<div class="row-head"><a class="pid" href="#${esc(rowDomId(row, i))}" title="${esc(where)}">${esc(label)}</a>` +
      `<span class="badges">${badgeHtml(row)}${noteCountHtml()}</span>${row.deleted ? '' : actionsHtml(row)}</div>`;
    const media = row.media_shared && row.media && row.media.length
      ? `<div class="media">${row.media.map((m) => `<img src="${esc(m.url)}" alt="${esc(m.name)}" loading="lazy" data-zoom="1">`).join('')}</div>` : '';
    return `<section class="${cls.join(' ')}" data-index="${i}" data-pid="${esc(pidOf(row))}" id="${esc(rowDomId(row, i))}">` +
      `${head}${media}<div class="cells">${cellHtml(row, 'zh')}${cellHtml(row, 'en')}</div><div class="notes"></div></section>`;
  }

  function renderDoc() {
    const rows = S.doc.rows;
    $('#doc').innerHTML = rows.map((row, i) => (row.type === 'heading' ? headingHtml(row, i) : blockHtml(row, i))).join('');
    S.rowEls = $$('#doc > .row');
    $('#doc').insertAdjacentHTML('beforeend',
      '<svg id="connector" aria-hidden="true" hidden><path d=""></path><circle r="3" cx="0" cy="0"></circle></svg>');
    S.stickyNote = null;
    S.hoverNote = null;
    RAIL.sticky = null;
    RAIL.hover = null;
    RAIL.scroll = null;
    RAIL.cards = [];
    computeScopes();
    for (const el of S.rowEls) { renderNotes(el); updateReadState(el); }
    setupObserver();
    if (S.railObserver) S.railObserver.disconnect();
    S.railObserver = new ResizeObserver(scheduleRail);
    for (const el of S.rowEls) {
      S.railObserver.observe(el);
      el.querySelectorAll(':scope > .notes > *').forEach((card) => S.railObserver.observe(card));
    }
    scheduleRail();
  }

  function computeScopes() {
    const rows = S.doc.rows;
    S.scopes = rows.map((row, i) => {
      if (row.type !== 'heading') return null;
      const level = LEVEL[row.level] ?? 9;
      let end = rows.length;
      for (let j = i + 1; j < rows.length; j += 1) {
        if (rows[j].type === 'heading' && (LEVEL[rows[j].level] ?? 9) <= level) { end = j; break; }
      }
      return [i + 1, end];
    });
  }

  // KaTeX：行进入视口附近时才渲染，长稿件也能快速打开。
  function setupObserver() {
    if (S.observer) S.observer.disconnect();
    S.observer = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) { renderMath(e.target); S.observer.unobserve(e.target); }
      }
    }, { rootMargin: '900px 0px' });
    S.rowEls.forEach((el) => S.observer.observe(el));
  }

  function renderMath(root) {
    if (!window.katex) { setTimeout(() => renderMath(root), 150); return; }
    const all = S.doc.katex_macros || {};
    root.querySelectorAll('.math:not([data-done])').forEach((m) => {
      const holder = m.closest('[data-lang]');
      const lang = holder ? holder.dataset.lang : 'en';
      try {
        window.katex.render(m.dataset.tex, m, {
          displayMode: m.classList.contains('display'),
          throwOnError: false,
          strict: 'ignore',
          trust: (ctx) => ctx.command === '\\htmlClass',
          macros: Object.assign({}, all[lang] || {}),
        });
      } catch (err) {
        m.classList.add('math-error');
        m.title = String(err && err.message ? err.message : err);
      }
      m.dataset.done = '1';
    });
  }

  // ---------------------------------------------------------------- 引文高亮、批注与段落的关联
  // 把段落内容展平成文字（数学式整体记为 $tex$，与选区引文的写法一致），记下每段文字对应的 DOM 节点。
  function flatten(root) {
    const parts = [];
    let text = '';
    const walk = (node) => {
      for (const child of Array.from(node.childNodes)) {
        if (child.nodeType === Node.TEXT_NODE) {
          parts.push({ node: child, start: text.length, end: text.length + child.data.length });
          text += child.data;
        } else if (child.nodeType === Node.ELEMENT_NODE) {
          if (child.classList.contains('math')) {
            const tex = `$${child.dataset.tex || ''}$`;
            parts.push({ math: child, start: text.length, end: text.length + tex.length });
            text += tex;
          } else if (!child.classList.contains('anchor') && !child.classList.contains('katex-mathml')) {
            walk(child);
          }
        }
      }
    };
    walk(root);
    return { text, parts };
  }

  function normalizeMap(text, dropSpaces) {
    let out = '';
    const map = [];
    let space = true;
    for (let i = 0; i < text.length; i += 1) {
      const ch = text[i];
      if (/\s/.test(ch)) {
        if (dropSpaces || space) continue;
        out += ' ';
        map.push(i);
        space = true;
      } else {
        out += ch;
        map.push(i);
        space = false;
      }
    }
    if (out.endsWith(' ')) { out = out.slice(0, -1); map.pop(); }
    return { out, map };
  }

  function findQuote(root, quote) {
    if (!root || !quote || !quote.trim()) return null;
    const flat = flatten(root);
    for (const drop of [false, true]) {
      const hay = normalizeMap(flat.text, drop);
      const needle = normalizeMap(quote, drop).out;
      if (!needle) return null;
      const at = hay.out.indexOf(needle);
      if (at >= 0) return { flat, start: hay.map[at], end: hay.map[at + needle.length - 1] + 1 };
    }
    return null;
  }

  function wrapFound(found, cls, noteId) {
    const marks = [];
    for (const part of found.flat.parts) {
      const s0 = Math.max(found.start, part.start);
      const e0 = Math.min(found.end, part.end);
      if (s0 >= e0) continue;
      if (part.math) {
        part.math.classList.add('qmark-math');
        if (cls.includes('live')) part.math.classList.add('live');
        part.math.dataset.notes = `${part.math.dataset.notes || ''} ${noteId}`.trim();
        marks.push(part.math);
        continue;
      }
      let node = part.node;
      const a = s0 - part.start;
      const b = e0 - part.start;
      if (b < node.data.length) node.splitText(b);
      if (a > 0) node = node.splitText(a);
      const mark = document.createElement('mark');
      mark.className = cls;
      mark.dataset.note = noteId;
      // 嵌套层数（重叠引文的内层另画一条下划线以便区分）。
      let depth = 1;
      for (let p = node.parentElement; p; p = p.parentElement) if (p.matches && p.matches('mark.qmark')) depth += 1;
      mark.dataset.depth = String(depth);
      node.parentNode.insertBefore(mark, node);
      mark.appendChild(node);
      marks.push(mark);
    }
    return marks;
  }

  function clearMarks(root, liveOnly = false) {
    root.querySelectorAll(liveOnly ? 'mark.qmark.live' : 'mark.qmark').forEach((m) => {
      const parent = m.parentNode;
      while (m.firstChild) parent.insertBefore(m.firstChild, m);
      parent.removeChild(m);
      parent.normalize();
    });
    root.querySelectorAll('.math.qmark-math').forEach((m) => {
      const ids = (m.dataset.notes || '').split(' ').filter((x) => x && (liveOnly ? x !== 'live' : false));
      if (ids.length) { m.dataset.notes = ids.join(' '); m.classList.remove('live'); return; }
      m.classList.remove('qmark-math', 'live', 'active');
      delete m.dataset.notes;
    });
  }

  function quoteRoots(rowEl, lang) {
    const langs = lang === 'zh' || lang === 'en' ? [lang] : ['zh', 'en'];
    return langs.map((l) => rowEl.querySelector(`.cells > .cell[data-lang="${l}"] > .content, .heading-cells > [data-lang="${l}"]`))
      .filter(Boolean);
  }

  // 已保存批注的引文：每次重绘批注时先清掉再按当前段落重新标出；找不到时批注上显示提示。
  function applyMarks(rowEl, notes) {
    rowEl.querySelectorAll('.cells > .cell > .content, .heading-cells > [data-lang]').forEach((root) => clearMarks(root));
    // 先标长引文、后标短引文：重叠时短的在内层，悬停时取最内层（最短）的那条。
    const ordered = notes.filter((n) => n.quote).sort((a, b) => b.quote.length - a.quote.length || a.id - b.id);
    for (const note of ordered) {
      let found = false;
      for (const root of quoteRoots(rowEl, note.lang)) {
        const hit = findQuote(root, note.quote);
        if (hit) { wrapFound(hit, 'qmark', String(note.id)); found = true; break; }
      }
      const art = rowEl.querySelector(`.note[data-id="${note.id}"]`);
      if (art) art.classList.toggle('no-quote-match', !found);
    }
  }

  function markLive(rowEl, lang, quote) {
    rowEl.querySelectorAll('.cells > .cell > .content, .heading-cells > [data-lang]').forEach((root) => clearMarks(root, true));
    if (!quote) return;
    for (const root of quoteRoots(rowEl, lang)) {
      const hit = findQuote(root, quote);
      if (hit) { wrapFound(hit, 'qmark live', 'live'); return; }
    }
  }

  const marksFor = (id) => $$(`mark.qmark[data-note="${id}"]`).concat(
    $$('.math.qmark-math').filter((m) => (m.dataset.notes || '').split(' ').includes(String(id))));

  // 悬停或点选一条批注时，标出它的段落与引文（连线由侧栏布局统一绘制）。
  function setActive(id, { sticky = false, force = false } = {}) {
    if (!force && !sticky && S.stickyNote != null) return;
    if (sticky || force) S.stickyNote = sticky ? id : null;
    S.hoverNote = id;
    $$('.note.active').forEach((n) => n.classList.remove('active'));
    $$('mark.qmark.active, .math.qmark-math.active').forEach((m) => m.classList.remove('active'));
    $$('.row.linked').forEach((r) => r.classList.remove('linked'));
    if (id != null) {
      const art = $(`.note[data-id="${id}"]`);
      if (art) {
        art.classList.add('active');
        marksFor(id).forEach((m) => m.classList.add('active'));
        const row = art.closest('.row');
        if (row) row.classList.add('linked');
      }
    }
    drawConnectors();
  }

  function pulse(el, cls) {
    el.classList.remove(cls);
    void el.offsetWidth;
    el.classList.add(cls);
  }

  // ---------------------------------------------------------------- 批注侧栏：每张卡片贴着自己的引文（Google Docs 式）
  // 锚点：批注引文高亮第一行的顶端；没有引文的批注锚在段落顶端；编辑框锚在实时引文（或所编辑批注的引文）处。
  // 排布：按锚点顺序，在互不重叠的前提下使各卡片离锚点的平方偏移之和最小（带权保序回归，PAV 算法）。
  // 当前批注（点选粘住 > 悬停引文 80 ms > 都没有时取最靠近视口 35% 阅读线的锚点）权重极大，精确对齐，其余卡片在上下让位。
  const RAIL = { sticky: null, hover: null, scroll: null, timer: null, cards: [], activeId: null };
  const RAIL_GAP = 10;
  const docTop = () => $('#doc').getBoundingClientRect().top;
  const quoteLen = (id) => ((S.notes.find((n) => String(n.id) === String(id)) || {}).quote || '').length;

  function rowHasCards(row) {
    return !!(row && row.querySelector(':scope > .notes > .note:not([hidden]), :scope > .notes > .note-editor'));
  }
  // 指针下的全部引文批注，从最内层（最短）到最外层。
  function notesAt(target) {
    const ids = [];
    for (let m = target.closest('mark.qmark:not(.live)'); m; m = m.parentElement && m.parentElement.closest('mark.qmark:not(.live)')) {
      ids.push(m.dataset.note);
    }
    const math = target.closest('.math.qmark-math');
    if (math) {
      const extra = (math.dataset.notes || '').split(' ').filter((x) => x && x !== 'live');
      extra.sort((a, b) => quoteLen(a) - quoteLen(b));
      ids.push(...extra);
    }
    return [...new Set(ids)];
  }
  function setRailHover(id) {
    clearTimeout(RAIL.timer);
    RAIL.timer = setTimeout(() => {
      const next = id == null ? null : String(id);
      if (RAIL.hover === next) return;
      RAIL.hover = next;
      scheduleRail();
    }, 80);
  }
  function setRailSticky(id) {
    clearTimeout(RAIL.timer);
    RAIL.sticky = id == null ? null : String(id);
    scheduleRail();
  }

  function railCards() {
    const base = docTop();
    const cards = [];
    for (const el of S.rowEls) {
      if (el.hidden) continue;
      const box = el.querySelector(':scope > .notes');
      if (!box || !box.firstElementChild) continue;
      const cells = el.querySelector(':scope > .cells, :scope > .heading-cells');
      const paraTop = el.offsetTop + (cells ? cells.offsetTop : 0);
      for (const card of box.children) {
        if (!card.matches('.note, .note-editor') || card.hidden) continue;
        const height = card.offsetHeight;
        if (!height) continue;
        const editor = card.classList.contains('note-editor');
        const id = editor ? 'editor' : card.dataset.id;
        let marks;
        if (!editor) marks = marksFor(id);
        else if (card.dataset.noteId) marks = marksFor(card.dataset.noteId);
        else marks = $$('mark.qmark.live, .math.qmark-math.live', el);
        let top = Infinity;
        let lineHeight = 28;
        for (const m of marks) {
          const r = m.getClientRects()[0];
          if (r && r.top < top) { top = r.top; lineHeight = r.height; }
        }
        const quoted = top < Infinity;
        cards.push({ el, box, card, id, anchor: quoted ? top - base : paraTop, lineHeight, height, order: cards.length, quoted });
      }
    }
    return cards;
  }

  // 带权保序回归：z_i = y_i − c_i（c_i 为前面卡片高度与间距之和），在 z 非降、z ≥ 0 下拟合 b_i = 锚点 − c_i。
  function packRail(cards, active) {
    let acc = 0;
    const c = cards.map((card) => { const v = acc; acc += card.height + RAIL_GAP; return v; });
    const blocks = [];
    cards.forEach((card, i) => {
      const w = i === active ? 1e6 : 1;
      blocks.push({ sw: w * (card.anchor - c[i]), w, n: 1 });
      while (blocks.length > 1) {
        const b = blocks[blocks.length - 1];
        const a = blocks[blocks.length - 2];
        if (a.sw / a.w <= b.sw / b.w) break;
        a.sw += b.sw;
        a.w += b.w;
        a.n += b.n;
        blocks.pop();
      }
    });
    let i = 0;
    for (const blk of blocks) {
      const z = Math.max(blk.sw / blk.w, 0);
      for (let k = 0; k < blk.n; k += 1, i += 1) cards[i].top = z + c[i];
    }
  }

  let railQueued = false;
  function scheduleRail() {
    if (railQueued) return;
    railQueued = true;
    requestAnimationFrame(() => { railQueued = false; layoutRail(); });
  }
  function layoutRail() {
    if (!S.rowEls.length) return;
    const doc = $('#doc');
    const side = document.body.classList.contains('notes-side');
    if (!side) {
      $$('#doc > .row > .notes > .note, #doc > .row > .notes > .note-editor').forEach((card) => {
        if (card.style.transform) card.style.transform = '';
        card.classList.remove('rail-active');
      });
      doc.style.minHeight = '';
      RAIL.cards = [];
      RAIL.activeId = null;
      drawConnectors();
      return;
    }
    const cards = railCards();
    cards.sort((a, b) => a.anchor - b.anchor || a.order - b.order);
    if (RAIL.sticky && !cards.some((c) => c.id === RAIL.sticky)) RAIL.sticky = null;
    if (RAIL.hover && !cards.some((c) => c.id === RAIL.hover)) RAIL.hover = null;
    let activeId = RAIL.sticky ?? RAIL.hover;
    if (activeId == null) {
      // 跟随滚动：最靠近阅读线（视口 35% 处）的锚点。
      const line = window.innerHeight * 0.35 - docTop();
      let best = null;
      let distance = Infinity;
      for (const c of cards) {
        const d = Math.abs(c.anchor - line);
        if (d < distance) { distance = d; best = c.id; }
      }
      RAIL.scroll = best;
      activeId = best;
    }
    const k = cards.findIndex((c) => c.id === activeId);
    packRail(cards, k);
    cards.forEach((c, i) => {
      const value = `translateY(${Math.round(c.top - c.el.offsetTop)}px)`;
      if (c.card.style.transform !== value) c.card.style.transform = value;
      c.card.classList.toggle('rail-active', i === k);
      c.card.dataset.anchor = String(Math.round(c.anchor));
    });
    RAIL.cards = cards;
    RAIL.activeId = k >= 0 ? activeId : null;
    const last = cards.reduce((m, c) => Math.max(m, c.top + c.height), 0);
    doc.style.minHeight = cards.length ? `${Math.ceil(last + 120)}px` : '';
    drawConnectors();
  }

  // 连线：当前（点选、悬停或跟随滚动的）批注画实线；其余卡片只有离锚点较远时才画淡线，避免杂乱。
  function drawConnectors() {
    const svg = $('#connector');
    if (!svg) return;
    const side = document.body.classList.contains('notes-side');
    if (!side || !RAIL.cards.length) { svg.innerHTML = ''; svg.setAttribute('hidden', ''); return; }
    const focus = S.stickyNote ?? S.hoverNote ?? RAIL.activeId;
    const base = $('#doc').getBoundingClientRect();
    let html = '';
    for (const c of RAIL.cards) {
      const strong = focus != null && c.id === String(focus);
      if (!strong && Math.abs(c.top - c.anchor) <= 6) continue;
      const cells = c.el.querySelector(':scope > .cells, :scope > .heading-cells');
      const edge = (cells || c.el).getBoundingClientRect();
      const x1 = edge.right + 6 - base.left;
      const y1 = c.anchor + (c.quoted ? c.lineHeight / 2 : 14);
      const x2 = c.box.getBoundingClientRect().left - base.left - 8;
      const y2 = c.top + 19;
      const mid = Math.max(x1 + 12, (x1 + x2) / 2);
      const d = `M${x1.toFixed(1)},${y1.toFixed(1)} C${mid.toFixed(1)},${y1.toFixed(1)} ${mid.toFixed(1)},${y2.toFixed(1)} ${x2.toFixed(1)},${y2.toFixed(1)}`;
      html += `<path class="${strong ? 'strong' : 'faint'}" d="${d}"></path>`;
      if (strong) html += `<circle r="3" cx="${x1.toFixed(1)}" cy="${y1.toFixed(1)}"></circle>`;
    }
    svg.innerHTML = html;
    if (html) svg.removeAttribute('hidden');
    else svg.setAttribute('hidden', '');
  }

  // 点一条批注：它粘住并对齐到引文，滚到段落（不在视野内时）并闪一下段落与引文。
  function focusNote(id) {
    const art = $(`.note[data-id="${id}"]`);
    const row = art ? art.closest('#doc > .row') : null;
    if (!row) return;
    setRailSticky(id);
    setActive(id, { sticky: true });
    const r = row.getBoundingClientRect();
    const top = headerOffset();
    if (r.top < top || r.top > window.innerHeight - 80) {
      window.scrollTo({ top: window.scrollY + r.top - top - 12, behavior: 'smooth' });
    }
    pulse(row, 'flash');
    marksFor(id).forEach((m) => pulse(m, 'pulse'));
  }

  // ---------------------------------------------------------------- 已读
  function readState(row) {
    const mark = S.reads[pidOf(row)];
    if (!mark) return 'unread';
    const okZh = !row.zh || mark.zh_sha === row.zh.sha || !row.zh.sha;
    const okEn = !row.en || mark.en_sha === row.en.sha || !row.en.sha;
    return okZh && okEn ? 'read' : 'stale';
  }
  const isRead = (row) => readState(row) === 'read';
  const countable = (row) => row.type === 'block' && !row.deleted;

  function updateReadState(el) {
    const row = S.doc.rows[+el.dataset.index];
    if (row.type !== 'block' || row.deleted) return;
    const state = readState(row);
    el.classList.toggle('is-read', state === 'read');
    const badge = el.querySelector('.read-mark');
    if (badge) {
      badge.hidden = state === 'unread';
      badge.textContent = state === 'stale' ? '读后有改动' : '已读';
      badge.title = state === 'stale' ? '标记已读之后此段已修改，按未读计' : '';
    }
    const btn = el.querySelector('.act-read');
    if (btn) btn.textContent = state === 'read' ? '取消已读' : '标为已读';
  }

  function updateProgress() {
    const rows = S.doc.rows.filter(countable);
    const read = rows.filter(isRead).length;
    $('#progress-text').textContent = `${read}/${rows.length}`;
    $('#progress').title = `已读 ${read} 段，共 ${rows.length} 段`;
    $('#progress-fill').style.width = rows.length ? `${(100 * read / rows.length).toFixed(1)}%` : '0';
  }

  async function toggleRead(el) {
    const row = S.doc.rows[+el.dataset.index];
    const read = !isRead(row);
    try {
      const data = await api(`papers/${S.pid}/reads`, {
        method: 'PUT',
        body: { para_id: pidOf(row), read, zh_sha: row.zh && row.zh.sha, en_sha: row.en && row.en.sha },
      });
      S.reads = data.reads;
      updateReadState(el);
      updateProgress();
      renderTocCounts();
      if (S.filters.unread) applyFilters();
    } catch (err) { toast(err.message, true); }
  }

  // ---------------------------------------------------------------- 批注
  function roundOf(number) { return S.rounds.find((r) => r.round === number); }
  function currentRound() { return S.rounds.find((r) => !r.submitted_at) || S.doc.round; }
  function editable(note) { const r = roundOf(note.round); return !r || !r.submitted_at; }
  function noteStale(note, row) {
    return ['zh', 'en'].some((l) => note[l + '_sha'] && row[l] && row[l].sha && note[l + '_sha'] !== row[l].sha);
  }

  function noteHtml(note, row) {
    const actions = editable(note)
      ? '<button class="link act-edit" type="button">编辑</button><button class="link danger act-delete" type="button">删除</button>'
      : '<span class="chip locked" title="本轮已提交，批注只读；可另加新批注">已提交</span>';
    const stale = (noteStale(note, row) ? '<span class="chip stale" title="批注之后这一段已经修改">段落已改</span>' : '') +
      (note.quote ? '<span class="chip stale quote-missing" title="引文在当前段落中找不到（段落可能已改）">引文未在段中</span>' : '');
    let reply = '';
    if (note.reply) reply = `<div class="note-reply"><span class="who">统筹回复</span>${esc(note.reply)}</div>`;
    else if (note.status !== 'open') reply = `<div class="note-reply"><span class="who">统筹</span>标记为${STATUS_NAME[note.status]}</div>`;
    return `<article class="note st-${note.status}" data-id="${note.id}">` +
      `<div class="note-meta"><span class="chip st-${note.status}">${STATUS_NAME[note.status] || note.status}</span>` +
      `<span>#${note.id} · 第 ${note.round} 轮 · ${fmtTime(note.created_at)}</span>${stale}<span class="grow"></span>${actions}</div>` +
      (note.quote ? `<blockquote>${esc(note.quote)}</blockquote>` : '') +
      `<div class="note-text">${esc(note.text)}</div>${reply}</article>`;
  }

  function renderNotes(el) {
    const row = S.doc.rows[+el.dataset.index];
    const list = S.notesBy.get(pidOf(row)) || [];
    const box = el.querySelector('.notes');
    const editor = box.querySelector('.note-editor');
    const editingId = editor ? editor.dataset.noteId : null;
    box.innerHTML = list.map((n) => noteHtml(n, row)).join('');
    if (editor) {
      box.appendChild(editor);
      if (editingId) { const art = box.querySelector(`.note[data-id="${editingId}"]`); if (art) art.hidden = true; }
    }
    el.classList.toggle('has-notes', list.length > 0);
    if (S.railObserver) box.querySelectorAll(':scope > *').forEach((card) => S.railObserver.observe(card));
    const badge = el.querySelector('.note-count');
    if (badge) {
      const open = list.filter((n) => n.status === 'open').length;
      badge.hidden = list.length === 0;
      badge.textContent = `批注 ${list.length}`;
      badge.title = `${list.length} 条批注（待处理 ${open}）；点击展开或收起本段批注`;
    }
    applyMarks(el, list);
  }

  function rerenderAllNotes() {
    for (const el of S.rowEls) renderNotes(el);
    renderTocCounts();
    applyFilters();
    updateSubmit();
    if (S.stickyNote != null && !$(`.note[data-id="${S.stickyNote}"]`)) setActive(null, { force: true });
    scheduleRail();
  }

  function closeEditor(form) {
    const box = form.parentElement;
    const id = form.dataset.noteId;
    form.remove();
    if (id && box) { const art = box.querySelector(`.note[data-id="${id}"]`); if (art) art.hidden = false; }
    const rowEl = box && box.closest('.row');
    if (rowEl) {
      rowEl.classList.remove('has-editor');
      markLive(rowEl, null, '');
    }
    scheduleRail();
  }

  function openEditor(el, { lang = null, quote = '', note = null } = {}) {
    $$('.note-editor').forEach((f) => { if (!f.querySelector('textarea[name="text"]').value.trim()) closeEditor(f); });
    const box = el.querySelector('.notes');
    const existing = box.querySelector('.note-editor');
    if (existing) closeEditor(existing);
    // 只有一个文本框；引文取自选中的文字（编辑时沿用原引文），不再选语言和类别。
    const captured = note ? note.quote || '' : quote || '';
    const form = document.createElement('form');
    form.className = 'note-editor';
    if (note) form.dataset.noteId = note.id;
    form.dataset.quote = captured;
    form.innerHTML =
      (captured ? `<blockquote class="ed-quote" title="引文（选中的文字）">${esc(captured)}</blockquote>` : '') +
      `<textarea name="text" rows="3" placeholder="写批注…" aria-label="批注内容" required>${esc(note ? note.text : '')}</textarea>` +
      '<div class="ed-actions"><span class="hint">Enter 保存 · Shift+Enter 换行 · Esc 取消</span>' +
      '<button type="button" class="btn ghost cancel">取消</button><button type="submit" class="btn primary">保存</button></div>';
    if (note) { const art = box.querySelector(`.note[data-id="${note.id}"]`); if (art) art.hidden = true; }
    box.appendChild(form);
    if (S.railObserver) S.railObserver.observe(form);
    el.classList.add('has-editor');
    form.querySelector('.cancel').addEventListener('click', () => closeEditor(form));
    form.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') { ev.preventDefault(); closeEditor(form); return; }
      // Enter 保存（输入法确认候选词时不算），Shift+Enter 换行；Ctrl/Cmd+Enter 也保存。
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.altKey && !ev.isComposing && ev.keyCode !== 229) {
        ev.preventDefault();
        form.requestSubmit();
      }
    });
    form.addEventListener('submit', (ev) => { ev.preventDefault(); saveNote(el, form, note); });
    // 新批注：在段落中标出选中的引文。
    if (!note && captured) markLive(el, lang || 'auto', captured);
    scheduleRail();
    const text = form.querySelector('textarea[name="text"]');
    text.focus();
    text.setSelectionRange(text.value.length, text.value.length);
    form.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  async function saveNote(el, form, note) {
    const row = S.doc.rows[+el.dataset.index];
    const text = form.querySelector('textarea[name="text"]').value;
    if (!text.trim()) { toast('批注内容不能为空', true); return; }
    try {
      if (note) {
        await api(`papers/${S.pid}/annotations/${note.id}`, { method: 'PATCH', body: { text } });
      } else {
        await api(`papers/${S.pid}/annotations`, {
          method: 'POST',
          body: { text, quote: form.dataset.quote || null, para_id: pidOf(row), zh_sha: row.zh && row.zh.sha,
                  en_sha: row.en && row.en.sha },
        });
      }
      await loadNotes();
      closeEditor(form);
      rerenderAllNotes();
      if (UI.notes) el.classList.add('notes-open');
      toast(note ? '批注已更新' : '批注已保存');
    } catch (err) { toast(err.message, true); }
  }

  async function deleteNote(el, id) {
    if (!window.confirm(`删除批注 #${id}？`)) return;
    try {
      await api(`papers/${S.pid}/annotations/${id}`, { method: 'DELETE' });
      await loadNotes();
      rerenderAllNotes();
      toast('批注已删除');
    } catch (err) { toast(err.message, true); }
  }

  function updateSubmit() {
    const btn = $('#submit-round');
    const cur = currentRound();
    if (!cur) { btn.disabled = true; return; }
    const count = S.notes.filter((n) => n.round === cur.round).length;
    btn.textContent = `提交批注 (${count})`;
    btn.disabled = count === 0;
    const last = [...S.rounds].reverse().find((r) => r.submitted_at);
    btn.title = `提交第 ${cur.round} 轮批注（${count} 条）` + (last ? `；第 ${last.round} 轮已于 ${fmtTime(last.submitted_at)} 提交` : '；提交后本轮锁定，交给统筹批量处理');
  }

  async function submitRound() {
    const cur = currentRound();
    const count = S.notes.filter((n) => n.round === cur.round).length;
    if (!count) return;
    const dialog = $('#submit-dialog');
    $('#submit-title').textContent = `提交第 ${cur.round} 轮批注（${count} 条）`;
    const open = S.notes.filter((n) => n.round === cur.round && n.status === 'open').length;
    $('#submit-desc').textContent = `提交后本轮批注锁定（不再能编辑或删除），统筹据此批量修改；其中待处理 ${open} 条。之后新加的批注进入第 ${cur.round + 1} 轮。`;
    $('#submit-note').value = '';
    dialog.returnValue = '';
    dialog.showModal();
    dialog.addEventListener('close', async () => {
      if (dialog.returnValue !== 'ok') return;
      try {
        const res = await api(`papers/${S.pid}/rounds/submit`, { method: 'POST', body: { note: $('#submit-note').value } });
        await loadNotes();
        rerenderAllNotes();
        toast(`第 ${res.submitted.round} 轮已提交（${res.submitted.count} 条）；新批注进入第 ${res.current.round} 轮`);
      } catch (err) { toast(err.message, true); }
    }, { once: true });
  }

  // ---------------------------------------------------------------- 对比
  function ensureDiff(el) {
    if (el.querySelector('.cell .diff')) return;
    const row = S.doc.rows[+el.dataset.index];
    for (const cell of el.querySelectorAll('.cells > .cell')) {
      const lang = cell.dataset.lang;
      if (!row[lang]) continue;
      const d = row.diff && row.diff[lang];
      const div = document.createElement('div');
      div.className = 'diff';
      div.innerHTML = d
        ? `<div class="diff-legend">相对 ${esc(S.doc.header.base.ref)}：<del class="d">删去</del> <ins class="d">新增</ins></div>${d}`
        : `<div class="diff-legend">相对 ${esc(S.doc.header.base.ref)} 无改动</div>${row[lang].html}`;
      cell.appendChild(div);
    }
    renderMath(el);
  }

  function setDiff(el, on) {
    if (on) ensureDiff(el);
    el.classList.toggle('view-diff', on);
    const btn = el.querySelector('.act-diff');
    if (btn) btn.setAttribute('aria-pressed', String(on));
    scheduleRail();
  }

  // ---------------------------------------------------------------- 目录与筛选
  function tocTitle(entry) {
    if (S.view === 'en') return entry.en || entry.zh;
    return entry.zh || entry.en;
  }

  function renderToc() {
    const rows = S.doc.rows;
    const entries = [];
    const firstHeading = rows.findIndex((r) => r.type === 'heading');
    if (firstHeading !== 0 && rows.length) entries.push({ index: -1, level: 'section', number: '', zh: '标题与摘要', en: 'Front matter' });
    entries.push(...S.doc.toc);
    S.tocEntries = entries;
    const items = entries.map((e) => {
      const target = e.index < 0 ? rowDomId(rows[0], 0) : rowDomId(rows[e.index], e.index);
      return `<a href="#${esc(target)}" class="lvl-${e.level}" data-index="${e.index}"><span class="tnum">${esc(e.number || '')}</span>` +
        `<span class="ttl">${esc(tocTitle(e))}</span><span class="cnts"></span><span class="tread"><span></span></span></a>`;
    }).join('');
    $('#toc').innerHTML = `<div class="toc-head">目录</div><div class="toc">${items}</div>` +
      '<div class="toc-legend"><span class="cnt cnt-chg">改</span> 本轮修改段数　<span class="cnt cnt-note">注</span> 批注数<br>下方细线：本节已读进度' +
      '<button class="link legend-toggle" type="button" title="收起这段说明">收起</button></div>' +
      '<button class="link legend-show" type="button">显示说明</button>';
    renderTocCounts();
    spy();
  }

  function tocScope(entry) {
    if (entry.index < 0) {
      const end = S.doc.rows.findIndex((r) => r.type === 'heading');
      return [0, end < 0 ? S.doc.rows.length : end];
    }
    return S.scopes[entry.index] || [entry.index + 1, entry.index + 1];
  }

  function renderTocCounts() {
    if (!S.tocEntries) return;
    const links = $$('#toc a[data-index]');
    links.forEach((a, k) => {
      const entry = S.tocEntries[k];
      const [start, end] = tocScope(entry);
      let changed = 0; let notes = 0; let read = 0; let total = 0;
      const own = entry.index >= 0 ? [entry.index] : [];
      for (const i of [...own, ...range(start, end)]) {
        const row = S.doc.rows[i];
        if (!row) continue;
        if (isChanged(row) && row.type === 'block') changed += 1;
        notes += (S.notesBy.get(pidOf(row)) || []).length;
        if (countable(row)) { total += 1; if (isRead(row)) read += 1; }
      }
      a.querySelector('.cnts').innerHTML = (changed ? `<span class="cnt cnt-chg" title="本轮修改 ${changed} 段">改 ${changed}</span>` : '') +
        (notes ? `<span class="cnt cnt-note" title="批注 ${notes} 条">注 ${notes}</span>` : '');
      a.querySelector('.tread span').style.width = total ? `${(100 * read / total).toFixed(0)}%` : '0';
      a.title = `${tocTitle(entry)}：${total} 段，已读 ${read}`;
    });
  }
  const range = (a, b) => Array.from({ length: Math.max(0, b - a) }, (_, k) => a + k);

  function rowMatches(row) {
    const f = S.filters;
    let notes = S.notesBy.get(pidOf(row)) || [];
    if (f.status) notes = notes.filter((n) => n.status === f.status);
    if (f.changed && !isChanged(row)) return false;
    if ((f.notes || f.status) && notes.length === 0) return false;
    if (f.unread && (row.type !== 'block' || row.deleted || isRead(row))) return false;
    return true;
  }

  function applyFilters() {
    const f = S.filters;
    const active = f.changed || f.notes || f.unread || !!f.status;
    const rows = S.doc.rows;
    const visible = rows.map((row) => row.type === 'block' && (!active || rowMatches(row)));
    rows.forEach((row, i) => {
      if (row.type !== 'heading') return;
      if (!active) { visible[i] = true; return; }
      const [start, end] = S.scopes[i];
      let any = rowMatches(row) && (row.id || (S.notesBy.get(pidOf(row)) || []).length || isChanged(row));
      for (let j = start; j < end && !any; j += 1) if (rows[j].type === 'block' && visible[j]) any = true;
      visible[i] = !!any;
    });
    S.rowEls.forEach((el, i) => { el.hidden = !visible[i]; });
    const blocks = rows.filter((r) => r.type === 'block');
    const shown = rows.filter((r, i) => r.type === 'block' && visible[i]).length;
    $('#filter-count').textContent = active ? `显示 ${shown}/${blocks.length} 段` : '';
    renderFilterChips(active ? `显示 ${shown}/${blocks.length} 段` : '');
    scheduleRail();
    $$('#toc a[data-index]').forEach((a, k) => {
      const entry = S.tocEntries[k];
      const [start, end] = tocScope(entry);
      let any = false;
      for (let j = start; j < end && !any; j += 1) if (visible[j]) any = true;
      a.classList.toggle('filtered-out', active && !any && !(entry.index >= 0 && visible[entry.index]));
    });
    S.rowEls.forEach((el) => {
      const row = S.doc.rows[+el.dataset.index];
      if (row.type === 'block' && !row.deleted && isChanged(row)) {
        if (f.diff) setDiff(el, true);
        else if (el.dataset.manualDiff !== '1') setDiff(el, false);
      }
    });
  }

  // 顶栏的筛选小标签：有筛选时才出现，点 × 去掉对应条件。
  const FILTER_LABEL = { changed: '本轮修改', notes: '有批注', unread: '未读' };
  function renderFilterChips(summary) {
    const f = S.filters;
    const chips = [];
    for (const key of ['changed', 'notes', 'unread']) if (f[key]) chips.push([key, FILTER_LABEL[key]]);
    if (f.status) chips.push(['status', `状态 ${STATUS_NAME[f.status] || f.status}`]);
    const box = $('#filter-chips');
    box.innerHTML = chips.map(([key, label]) => `<span class="chip-f" data-filter="${key}" title="${esc(summary)}">${esc(label)}` +
      `<button type="button" aria-label="取消筛选：${esc(label)}">×</button></span>`).join('');
  }
  function clearFilter(key) {
    if (key === 'status') {
      S.filters.status = '';
      $('#f-status').value = '';
    } else {
      S.filters[key] = false;
      $('#f-' + key).checked = false;
    }
    applyFilters();
  }

  // 目录高亮当前节。
  let spyQueued = false;
  function spy() {
    if (spyQueued) return;
    spyQueued = true;
    requestAnimationFrame(() => {
      spyQueued = false;
      if (!S.tocEntries) return;
      const limit = headerOffset() + 40;
      let current = 0;
      S.tocEntries.forEach((e, k) => {
        const el = e.index < 0 ? S.rowEls[0] : S.rowEls[e.index];
        if (el && !el.hidden && el.getBoundingClientRect().top <= limit) current = k;
      });
      $$('#toc a[data-index]').forEach((a, k) => a.classList.toggle('active', k === current));
    });
  }

  function topVisibleRow() {
    const limit = headerOffset();
    for (const el of S.rowEls) {
      const r = el.getBoundingClientRect();
      if (r.bottom > limit + 4) return { id: el.id, offset: r.top - limit };
    }
    return null;
  }
  function scrollToRow(id, offset = 0) {
    const el = document.getElementById(id);
    if (!el) return;
    const limit = headerOffset();
    window.scrollTo({ top: window.scrollY + el.getBoundingClientRect().top - limit - offset });
  }
  function scrollToHash(hash) {
    const id = decodeURIComponent(hash.slice(1));
    const el = document.getElementById(id);
    if (!el) return;
    el.scrollIntoView({ block: 'start' });
    const row = el.closest('.row');
    if (row) { row.classList.remove('flash'); void row.offsetWidth; row.classList.add('flash'); }
  }

  // ---------------------------------------------------------------- 选区 → 引文
  function cellOf(node) {
    const el = node && (node.nodeType === 1 ? node : node.parentElement);
    return el ? el.closest('.cells > .cell[data-lang], .heading-cells > [data-lang]') : null;
  }
  function quoteOf(range) {
    const r = range.cloneRange();
    const startEl = r.startContainer.nodeType === 1 ? r.startContainer : r.startContainer.parentElement;
    const endEl = r.endContainer.nodeType === 1 ? r.endContainer : r.endContainer.parentElement;
    const sm = startEl && startEl.closest('.math');
    const em = endEl && endEl.closest('.math');
    if (sm) r.setStartBefore(sm);
    if (em) r.setEndAfter(em);
    const frag = r.cloneContents();
    frag.querySelectorAll('.math').forEach((m) => m.replaceWith(document.createTextNode(`$${m.dataset.tex || ''}$`)));
    frag.querySelectorAll('.katex-mathml, .anchor, .diff-legend').forEach((m) => m.remove());
    return frag.textContent.replace(/\s+/g, ' ').trim().slice(0, 2000);
  }
  function checkSelection() {
    const btn = $('#sel-note');
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) { btn.hidden = true; S.selection = null; return; }
    const range = sel.getRangeAt(0);
    const a = cellOf(range.startContainer);
    const b = cellOf(range.endContainer);
    if (!a || a !== b) { btn.hidden = true; S.selection = null; return; }
    const quote = quoteOf(range);
    if (!quote) { btn.hidden = true; S.selection = null; return; }
    const rowEl = a.closest('.row');
    S.selection = { index: +rowEl.dataset.index, lang: a.dataset.lang, quote };
    const rect = range.getBoundingClientRect();
    btn.hidden = false;
    const w = btn.offsetWidth || 70;
    btn.style.left = `${Math.min(Math.max(8, rect.right - w), window.innerWidth - w - 8)}px`;
    btn.style.top = `${Math.min(rect.bottom + 8, window.innerHeight - 40)}px`;
  }

  // ---------------------------------------------------------------- 提示框
  const tip = () => $('#tooltip');
  function showCite(el) {
    const holder = el.closest('[data-lang]');
    const lang = holder ? holder.dataset.lang : 'en';
    const rowEl = el.closest('.row');
    const row = rowEl ? S.doc.rows[+rowEl.dataset.index] : null;
    const key = el.dataset.cite;
    const table = (row && row.part === 'supp' && S.doc.bib[lang + '-supp']) || S.doc.bib[lang] || {};
    const info = table[key];
    const t = tip();
    t.innerHTML = `<div class="tt-key">[${esc(info && info.n ? info.n : '?')}] ${esc(key)}</div>` +
      (info && info.title ? `<div class="tt-title">${esc(info.title)}</div>` : '') +
      `<div class="tt-text">${esc(info && info.text ? info.text : '（.bbl 中没有这条文献）')}</div>`;
    t.hidden = false;
    const r = el.getBoundingClientRect();
    const w = t.offsetWidth;
    t.style.left = `${Math.min(Math.max(8, r.left), window.innerWidth - w - 8)}px`;
    const below = r.bottom + 8;
    t.style.top = (below + t.offsetHeight > window.innerHeight ? Math.max(8, r.top - t.offsetHeight - 8) : below) + 'px';
  }
  function hideTip() { tip().hidden = true; }

  function zoom(src) {
    const overlay = document.createElement('div');
    overlay.className = 'zoom';
    overlay.innerHTML = `<img src="${esc(src)}" alt="">`;
    overlay.addEventListener('click', () => overlay.remove());
    document.addEventListener('keydown', function esc2(ev) { if (ev.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', esc2); } });
    document.body.appendChild(overlay);
  }

  // ---------------------------------------------------------------- 轮询
  async function poll() {
    if (!S.doc || document.hidden || S.loading) return;
    try {
      const v = await api(`papers/${S.pid}/version?${query()}`);
      if (v.signature !== S.doc.signature) showBanner('稿件、构建或 Git 状态已更新。', '重新载入', () => loadDoc({ keep: true }));
    } catch { /* 服务暂不可用时下次再试 */ }
    try {
      const data = await api(`papers/${S.pid}/annotations`);
      if (notesStamp(data.annotations, data.rounds) !== S.notesStamp && !$('.note-editor')) {
        await loadNotes();
        rerenderAllNotes();
      }
    } catch { /* 同上 */ }
  }

  // ---------------------------------------------------------------- 事件
  function bindChrome() {
    // 同一设置的按钮组可能在顶栏和设置面板各有一组：按 data-属性统一分派。
    document.addEventListener('click', (ev) => {
      const b = ev.target instanceof Element ? ev.target.closest('button') : null;
      if (!b) return;
      const d = b.dataset;
      if (d.view) setView(d.view);
      else if (d.theme) setTheme(d.theme);
      else if (d.font) setFont(d.font);
      else if (d.size) setSize(d.size);
      else if (d.width) setWidth(d.width);
      else if (d.layout) { if ((d.layout === 'side') !== sidePref()) toggleNotesLayout(); }
      else if (d.fold) { if ((d.fold === 'fold') !== UI.notes) toggleNotes(); }
      else if (d.toc) {
        const hide = d.toc === 'hide';
        if (isMobile()) {
          document.body.classList.toggle('toc-open', !hide);
          if (!hide) closeSettings();
        } else if (hide !== UI.toc) {
          toggleToc();
        }
      }
    });
    $('#s-ids').addEventListener('change', (ev) => { document.body.classList.toggle('hide-ids', !ev.target.checked); prefs.set('ids', ev.target.checked); });
    $('#s-synced').addEventListener('change', (ev) => { document.body.classList.toggle('show-synced', ev.target.checked); prefs.set('synced', ev.target.checked); });
    $('#s-legend').addEventListener('change', (ev) => setUi('legend', ev.target.checked, 'legendShown'));
    $$('.opt-autohide').forEach((box) => box.addEventListener('change', () => setUi('autohide', box.checked, 'autohide')));
    const filterInputs = { 'f-changed': 'changed', 'f-notes': 'notes', 'f-unread': 'unread', 'f-diff': 'diff' };
    for (const [id, key] of Object.entries(filterInputs)) {
      $('#' + id).addEventListener('change', (ev) => { S.filters[key] = ev.target.checked; if (S.doc) applyFilters(); });
    }
    $('#f-status').addEventListener('change', (ev) => { S.filters.status = ev.target.value; if (S.doc) applyFilters(); });
    $('#f-clear').addEventListener('click', () => {
      S.filters = { changed: false, notes: false, unread: false, status: '', diff: S.filters.diff };
      ['f-changed', 'f-notes', 'f-unread'].forEach((id) => { $('#' + id).checked = false; });
      $('#f-status').value = '';
      if (S.doc) applyFilters();
    });
    $('#filter-chips').addEventListener('click', (ev) => {
      const chip = ev.target instanceof Element ? ev.target.closest('.chip-f') : null;
      if (chip && ev.target.closest('button')) clearFilter(chip.dataset.filter);
    });
    $('#paper-select').addEventListener('change', (ev) => {
      const q = new URLSearchParams();
      q.set('paper', ev.target.value);
      location.search = q.toString();
    });
    $('#base-select').addEventListener('change', (ev) => {
      let value = ev.target.value;
      if (value === '__custom') {
        value = (window.prompt('对比基准：输入 Git tag、分支或提交号', S.doc.header.base.ref) || '').trim();
        if (!value) { fillBaseSelect(); return; }
      }
      S.base = value;
      updateUrl();
      loadDoc({ keep: true });
    });
    $('#rebuild').addEventListener('click', async () => {
      try {
        $('#rebuild').disabled = true;
        await api(`papers/${S.pid}/rebuild?${query()}`, { method: 'POST' });
        await loadDoc({ keep: true });
        toast('已重新解析稿件');
      } catch (err) { toast(err.message, true); } finally { $('#rebuild').disabled = false; }
    });
    $('#submit-round').addEventListener('click', submitRound);
    $('#settings-btn').addEventListener('click', (ev) => { ev.stopPropagation(); toggleSettings(); });
    $('#settings-close').addEventListener('click', closeSettings);
    $('#sheet-scrim').addEventListener('click', closeSettings);
    document.addEventListener('click', (ev) => {
      if (UI.settings && ev.target instanceof Element && !ev.target.closest('#settings-panel, #settings-btn')) closeSettings();
    });
    $('#rail-toggle').addEventListener('click', toggleNotesLayout);
    $('#short-name').addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
    $('#toc-handle').addEventListener('click', toggleToc);
    $('#toc-scrim').addEventListener('click', () => document.body.classList.remove('toc-open'));
    let resizeTimer = null;
    window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(applyLayout, 120); });
    document.addEventListener('keydown', onShortcut);
    document.addEventListener('mousemove', (ev) => {
      if (ev.clientY > 6) return;
      if (UI.autohide && UI.away) setAway(false);
      if (UI.barHidden) setPeek(true);
    }, { passive: true });
    $('#top').addEventListener('mouseleave', () => { if (UI.barHidden && !UI.settings) setPeek(false); });
    $('#toc').addEventListener('click', (ev) => {
      if (!(ev.target instanceof Element)) return;
      if (ev.target.closest('.legend-toggle, .legend-show')) { setUi('legend', !UI.legend, 'legendShown'); return; }
      const a = ev.target.closest('a[href^="#"]');
      if (!a) return;
      ev.preventDefault();
      document.body.classList.remove('toc-open');
      history.replaceState(null, '', a.getAttribute('href'));
      scrollToHash(a.getAttribute('href'));
    });

    const doc = $('#doc');
    const elOf = (t) => (t instanceof Element ? t : (t && t.parentElement) || null);
    doc.addEventListener('click', (ev) => {
      const target = elOf(ev.target);
      if (!target) return;
      const rowEl = target.closest('#doc > .row');
      if (target.closest('.act-note') && rowEl) {
        const sel = S.selection && S.selection.index === +rowEl.dataset.index ? S.selection : null;
        openEditor(rowEl, sel ? { lang: sel.lang, quote: sel.quote } : {});
        return;
      }
      if (target.closest('.act-read') && rowEl) { toggleRead(rowEl); return; }
      if (target.closest('.note-count') && rowEl) { rowEl.classList.toggle('notes-open'); scheduleRail(); return; }
      if (target.closest('.act-diff') && rowEl) {
        const on = !rowEl.classList.contains('view-diff');
        rowEl.dataset.manualDiff = on ? '1' : '';
        setDiff(rowEl, on);
        return;
      }
      const noteEl = target.closest('.note');
      if (noteEl && rowEl) {
        const note = S.notes.find((n) => n.id === +noteEl.dataset.id);
        if (target.closest('.act-edit') && note) { openEditor(rowEl, { note }); return; }
        if (target.closest('.act-delete') && note) { deleteNote(rowEl, note.id); return; }
        const sel = window.getSelection();
        if (note && !target.closest('button, a') && (!sel || sel.isCollapsed)) { focusNote(note.id); return; }
      }
      // 点引文：这条批注粘住；同一处有多条重叠引文时，再点依次切换到外层。
      const ids = rowEl ? notesAt(target) : [];
      if (ids.length) {
        const at = ids.indexOf(RAIL.sticky);
        const id = at >= 0 ? ids[(at + 1) % ids.length] : ids[0];
        const art = $(`.note[data-id="${id}"]`);
        if (art) {
          if (UI.notes && !rowEl.classList.contains('notes-open')) rowEl.classList.add('notes-open');
          setRailSticky(id);
          setActive(+id, { sticky: true });
          pulse(art, 'flash');
          return;
        }
      }
      if (!target.closest('.notes')) setRailSticky(null);
      if (S.stickyNote != null) setActive(null, { force: true });
      const img = target.closest('img[data-zoom], img.fig-img');
      if (img) { zoom(img.getAttribute('src')); return; }
      const xref = target.closest('a.xref[href^="#"], a.pid[href^="#"]');
      if (xref) {
        ev.preventDefault();
        const href = xref.getAttribute('href');
        history.replaceState(null, '', href);
        scrollToHash(href);
        return;
      }
      const cite = target.closest('.cite[data-cite]');
      if (cite && window.matchMedia('(hover: none)').matches) { showCite(cite); return; }
      hideTip();
    });
    doc.addEventListener('mouseover', (ev) => {
      const t = elOf(ev.target);
      if (!t) return;
      const c = t.closest('.cite[data-cite]');
      if (c) showCite(c);
      const art = t.closest('.note[data-id]');
      const quoteIds = art ? [] : notesAt(t);
      const id = art ? +art.dataset.id : quoteIds.length ? +quoteIds[0] : null;
      if (id && id !== S.hoverNote && S.stickyNote == null) setActive(id);
      else if (!id && S.hoverNote != null && S.stickyNote == null) setActive(null);
      // 只有悬停引文才移动卡片（取最内层、最短的那条）；悬停段落其余部分或卡片本身不动。
      if (quoteIds.length) setRailHover(quoteIds[0]);
    });
    doc.addEventListener('mouseleave', () => setRailHover(null));
    doc.addEventListener('focusin', (ev) => {
      const t = elOf(ev.target);
      if (t && t.closest('#doc > .row > .notes > .note-editor')) setRailSticky('editor');
    });
    doc.addEventListener('mouseout', (ev) => { const t = elOf(ev.target); if (t && t.closest('.cite[data-cite]')) hideTip(); });
    document.addEventListener('mouseup', (ev) => {
      const t = elOf(ev.target);
      if (!t || !t.closest('#sel-note, .note-editor')) setTimeout(checkSelection, 0);
    });
    document.addEventListener('keyup', (ev) => { if (ev.shiftKey) setTimeout(checkSelection, 0); });
    let selTimer = null;
    document.addEventListener('selectionchange', () => { clearTimeout(selTimer); selTimer = setTimeout(checkSelection, 300); });
    const selBtn = $('#sel-note');
    selBtn.addEventListener('mousedown', (ev) => ev.preventDefault());
    selBtn.addEventListener('click', () => {
      if (!S.selection) return;
      const el = S.rowEls[S.selection.index];
      const sel = S.selection;
      openEditor(el, { lang: sel.lang, quote: sel.quote });
      window.getSelection().removeAllRanges();
      selBtn.hidden = true;
      S.selection = null;
    });
    window.addEventListener('scroll', () => { spy(); hideTip(); autohideOnScroll(); scheduleRail(); }, { passive: true });
    window.matchMedia('(max-width: 900px)').addEventListener('change', () => { closeSettings(); applyUi(); });
    window.addEventListener('hashchange', () => scrollToHash(location.hash));
  }

  document.addEventListener('DOMContentLoaded', init);
})();
