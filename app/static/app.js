/* framevalet — vanilla JS, no deps. Every block guards its elements. */
(() => {
  'use strict';
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => [...document.querySelectorAll(sel)];
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const TVS = window.FV_TVS || [];
  const authed = !!document.querySelector('.logout-form');

  /* --------------------------------------------------------------- toasts */
  const toastBox = $('#toasts');
  const srLive = $('#srLive');
  function toast(msg, kind = 'error') {
    if (srLive) srLive.textContent = msg;
    if (!toastBox) return;
    const t = document.createElement('div');
    t.className = `toast toast-${kind}`;
    t.textContent = msg;
    toastBox.appendChild(t);
    setTimeout(() => {
      t.classList.add('out');
      setTimeout(() => t.remove(), 250);
    }, 4000);
  }

  async function jsonFetch(url, opts) {
    const res = await fetch(url, opts);
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON body */ }
    return { ok: res.ok, status: res.status, data };
  }
  async function post(url, body) {
    const r = await jsonFetch(url, { method: 'POST', body });
    if (!r.ok) throw new Error(r.data.detail || r.data.error || `Request failed (${r.status})`);
    return r.data;
  }
  async function postJSON(url, obj) {
    const r = await jsonFetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(obj),
    });
    if (!r.ok) throw new Error(r.data.detail || r.data.error || `Request failed (${r.status})`);
    return r.data;
  }
  async function getJSON(url) {
    const r = await jsonFetch(url);
    if (!r.ok) throw new Error(r.data.detail || r.data.error || `Request failed (${r.status})`);
    return r.data;
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function agoText(epoch) {
    if (!epoch) return 'never';
    const secs = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
    if (secs < 60) return 'just now';
    if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
    if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
    return `${Math.floor(secs / 86400)}d ago`;
  }

  /* ---------------------------------------------------------------- theme */
  const themeToggle = $('#themeToggle');
  if (themeToggle) {
    const root = document.documentElement;
    const label = () => themeToggle.setAttribute('aria-label',
      root.dataset.theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
    label();
    themeToggle.addEventListener('click', () => {
      const next = root.dataset.theme === 'dark' ? 'light' : 'dark';
      root.dataset.theme = next;
      try { localStorage.setItem('fv-theme', next); } catch (_) { /* private mode */ }
      label();
    });
    // While no explicit choice is stored, follow live OS theme changes.
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
      let stored = null;
      try { stored = localStorage.getItem('fv-theme'); } catch (_) { /* ignore */ }
      if (stored !== 'dark' && stored !== 'light') {
        root.dataset.theme = e.matches ? 'dark' : 'light';
        label();
      }
    });
  }

  /* ---------------------------------------------------------- header pill */
  const pill = $('#tvPill');
  const pillText = $('#tvPillText');
  function tvName(id) {
    const t = TVS.find((x) => x.id === +id);
    return t ? t.name : `TV ${id}`;
  }
  function updatePillFromTVS() {
    if (!pill || !pillText) return;
    const ids = (pill.dataset.enabled || '').split(',').filter(Boolean);
    if (!ids.length) return; // neutral "No TV yet" pill
    let okCount = 0;
    ids.forEach((id) => {
      const t = TVS.find((x) => x.id === +id);
      if (t && t.ok) okCount += 1;
    });
    const allOk = okCount === ids.length;
    const wasOk = pill.classList.contains('pill-ok');
    pill.classList.toggle('pill-ok', allOk);
    pill.classList.toggle('pill-warn', !allOk);
    if (allOk) {
      pillText.textContent = ids.length === 1 && pill.dataset.single
        ? pill.dataset.single : 'TVs connected';
    } else {
      pillText.textContent = `${okCount} of ${ids.length} TVs reachable`;
    }
    if (!wasOk && allOk && !reduceMotion) {
      pill.classList.add('pill-glow');
      pill.addEventListener('animationend', () => pill.classList.remove('pill-glow'), { once: true });
    }
  }
  function absorbTvMap(tvMap) {
    if (!tvMap) return;
    TVS.forEach((t) => {
      const s = tvMap[String(t.id)];
      if (s) t.ok = s.ok ? 1 : 0;
    });
    updatePillFromTVS();
  }

  /* ------------------------------------------------------------- uploads */
  const dropZone = $('#dropZone');
  const fileInput = $('#fileInput');
  const uploadBtn = $('#uploadBtn');
  const uploadResults = $('#uploadResults');
  const uploadProgress = $('#uploadProgress');

  async function sendFiles(files) {
    if (!files || !files.length) return;
    const fd = new FormData();
    [...files].forEach((f) => fd.append('files', f));
    uploadBtn.disabled = true;
    uploadBtn.textContent = `Uploading ${files.length} photo${files.length > 1 ? 's' : ''}…`;
    if (uploadProgress) uploadProgress.hidden = false;
    try {
      const data = await post('/upload', fd);
      uploadResults.hidden = false;
      uploadResults.innerHTML = '';
      let anyOk = false;
      (data.results || []).forEach((r) => {
        const li = document.createElement('li');
        li.className = r.ok ? 'ok' : 'err';
        li.textContent = r.ok ? `✓ ${r.file}` : `✕ ${r.file}: ${r.error}`;
        uploadResults.appendChild(li);
        if (r.ok) anyOk = true; else toast(`${r.file}: ${r.error}`);
      });
      if (anyOk) setTimeout(() => location.reload(), 1500);
    } catch (e) {
      toast(`Upload failed: ${e.message}`);
    } finally {
      if (uploadProgress) uploadProgress.hidden = true;
      uploadBtn.disabled = false;
      uploadBtn.textContent = 'Add photos';
    }
  }

  if (dropZone && fileInput && uploadBtn) {
    uploadBtn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => { sendFiles(fileInput.files); fileInput.value = ''; });
    ['dragover', 'dragenter'].forEach((ev) =>
      dropZone.addEventListener(ev, (e) => { e.preventDefault(); dropZone.classList.add('dragover'); }));
    ['dragleave', 'drop'].forEach((ev) =>
      dropZone.addEventListener(ev, (e) => { e.preventDefault(); dropZone.classList.remove('dragover'); }));
    dropZone.addEventListener('drop', (e) => sendFiles(e.dataTransfer.files));
  }

  /* --------------------------------------------------- entrance stagger */
  const grid = $('#photoGrid');
  if (grid && !reduceMotion) {
    [...grid.querySelectorAll('.card')].slice(0, 24)
      .forEach((card, i) => card.style.setProperty('--d', `${i * 25}ms`));
    grid.classList.add('enter');
    setTimeout(() => grid.classList.remove('enter'), 1100);
  }

  const allCards = () => (grid ? [...grid.querySelectorAll('.card')] : []);
  function cardTags(card) {
    if (!card._tags) {
      try { card._tags = JSON.parse(card.dataset.tags || '[]'); } catch (_) { card._tags = []; }
    }
    return card._tags;
  }
  function cardStates(card) {
    try { return JSON.parse(card.dataset.states || '{}'); } catch (_) { return {}; }
  }

  /* ------------------------------------------------------------- counts */
  const countsLine = $('#countsLine');
  function renderCounts(c) {
    if (!countsLine) return;
    let line = `${c.on_tv} on TV · ${c.queued} queued`;
    if (c.failed) line += ` · ${c.failed} failed`;
    countsLine.textContent = line;
  }
  function refreshCountsFromCards() {
    if (!grid || !countsLine) return;
    const c = { on_tv: 0, queued: 0, failed: 0 };
    allCards().forEach((card) => {
      if (+card.dataset.ontv > 0) c.on_tv += 1;
      if (+card.dataset.queued > 0) c.queued += 1;
      if (+card.dataset.failed > 0) c.failed += 1;
    });
    renderCounts(c);
  }
  refreshCountsFromCards();

  /* -------------------------------------------------- card badge updates */
  function applyState(card, tvId, status, error) {
    const states = cardStates(card);
    states[String(tvId)] = { status, error: error || null };
    card.dataset.states = JSON.stringify(states);
    const vals = Object.values(states).filter((s) => s.status !== 'removed');
    const nOn = vals.filter((s) => s.status === 'on_tv').length;
    const nQ = vals.filter((s) => s.status === 'queued').length;
    const nF = vals.filter((s) => s.status === 'failed').length;
    card.dataset.ontv = nOn;
    card.dataset.queued = nQ;
    card.dataset.failed = nF;
    card.dataset.assigned = vals.length;
    let badge = card.querySelector('.badge');
    if (!vals.length) { if (badge) badge.remove(); return null; }
    if (!badge) { badge = el('span'); card.appendChild(badge); }
    let cls; let text;
    if (vals.length === 1) {
      const st = vals[0].status;
      cls = st === 'on_tv' ? 'ontv' : (st === 'failed' ? 'failed' : 'queued');
      text = st === 'on_tv' ? 'on TV' : st;
    } else {
      cls = nF ? 'failed' : (nQ ? 'queued' : 'ontv');
      text = `${nOn}/${vals.length}`;
    }
    badge.className = `badge badge-${cls}`;
    badge.textContent = '';
    badge.append(el('span', 'dot'), document.createTextNode(text));
    const errs = vals.filter((s) => s.status === 'failed' && s.error).map((s) => s.error);
    if (errs.length) badge.title = errs.join(' · '); else badge.removeAttribute('title');
    return badge;
  }

  /* ------------------------------------------------------------- filters */
  const chips = $('#filterChips');
  const tagFilter = $('#tagFilter');
  const filterState = { chip: 'all', tag: '' };

  function setCardVisible(card, show) {
    if (show) {
      card.classList.remove('f-out');
      if (card.hidden) {
        card.hidden = false;
        if (!reduceMotion) {
          card.classList.add('f-in');
          card.addEventListener('animationend', () => card.classList.remove('f-in'), { once: true });
        }
      }
    } else if (!card.hidden && !card.classList.contains('f-out')) {
      if (reduceMotion) { card.hidden = true; return; }
      card.classList.add('f-out');
      setTimeout(() => {
        if (card.classList.contains('f-out')) {
          card.hidden = true;
          card.classList.remove('f-out');
        }
      }, 150);
    }
  }

  function cardMatches(card) {
    const d = card.dataset;
    const f = filterState.chip;
    let show = true;
    if (f === 'on_tv') show = +d.ontv > 0;
    else if (f === 'queued') show = +d.queued > 0;
    else if (f === 'failed') show = +d.failed > 0;
    else if (f === 'favorites') show = d.fav === '1';
    else if (f === 'external') show = d.source === 'external';
    if (show && filterState.tag) show = cardTags(card).includes(filterState.tag);
    return show;
  }
  function applyFilters() {
    allCards().forEach((card) => setCardVisible(card, cardMatches(card)));
  }

  if (chips && grid) {
    chips.addEventListener('click', (e) => {
      const chip = e.target.closest('.chip');
      if (!chip) return;
      chips.querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', c === chip));
      filterState.chip = chip.dataset.filter;
      applyFilters();
    });
  }
  if (tagFilter && grid) {
    tagFilter.addEventListener('change', () => {
      filterState.tag = tagFilter.value;
      applyFilters();
    });
  }

  /* --------------------------------------------------- selection + bulk */
  const selectToggle = $('#selectToggle');
  const bulkBar = $('#bulkBar');
  const bulkCount = $('#bulkCount');
  let selectMode = false;

  function selectedCards() { return allCards().filter((c) => c.classList.contains('selected')); }

  function closeBulkMenus() {
    $$('.bulk-menu').forEach((m) => { m.hidden = true; });
  }
  function updateBulkBar() {
    if (!bulkBar) return;
    const n = selectedCards().length;
    if (bulkCount) bulkCount.textContent = `${n} selected`;
    const shouldShow = selectMode && n > 0;
    if (shouldShow && bulkBar.hidden) {
      bulkBar.classList.remove('out');
      bulkBar.hidden = false;
      document.body.classList.add('has-bulkbar');
    } else if (!shouldShow && !bulkBar.hidden) {
      closeBulkMenus();
      document.body.classList.remove('has-bulkbar');
      if (reduceMotion) { bulkBar.hidden = true; return; }
      bulkBar.classList.add('out');
      setTimeout(() => {
        if (bulkBar.classList.contains('out')) {
          bulkBar.hidden = true;
          bulkBar.classList.remove('out');
        }
      }, 200);
    }
  }
  function setSelectMode(on) {
    selectMode = on;
    if (grid) grid.classList.toggle('selecting', on);
    if (selectToggle) {
      selectToggle.setAttribute('aria-pressed', on ? 'true' : 'false');
      selectToggle.textContent = on ? 'Done' : 'Select';
    }
    if (selectAllBtn) selectAllBtn.hidden = !on;
    if (!on) allCards().forEach((c) => c.classList.remove('selected'));
    updateBulkBar();
  }
  const selectAllBtn = $('#selectAll');
  if (selectToggle && grid) {
    selectToggle.addEventListener('click', () => setSelectMode(!selectMode));
  }
  if (selectAllBtn && grid) {
    selectAllBtn.addEventListener('click', () => {
      const vis = allCards().filter((c) => c.style.display !== 'none' && !c.hidden);
      const allSelected = vis.length && vis.every((c) => c.classList.contains('selected'));
      vis.forEach((c) => c.classList.toggle('selected', !allSelected));
      updateBulkBar();
    });
  }

  function buildTvMenus() {
    $$('.bulk-menu:not(.bulk-matte-menu)').forEach((menu) => {
      menu.innerHTML = '';
      TVS.forEach((tv) => {
        const b = el('button', '', tv.name + (tv.enabled ? '' : ' (disabled)'));
        b.type = 'button';
        b.addEventListener('click', () => {
          menu.hidden = true;
          runBulk(menu.dataset.menuFor, String(tv.id));
        });
        menu.appendChild(b);
      });
      if (!TVS.length) {
        const b = el('button', '', 'No TVs configured');
        b.type = 'button';
        b.disabled = true;
        menu.appendChild(b);
      }
    });
  }

  function buildMatteMenu() {
    const menu = $('.bulk-matte-menu');
    if (!menu) return;
    const types = [...document.querySelectorAll('#matteTypes [data-mtype]')]
      .map((b) => ({ v: b.dataset.mtype, label: b.textContent.trim() }));
    const colors = [...document.querySelectorAll('#matteColors .matte-swatch')]
      .map((s) => ({ name: s.dataset.mcolor, hex: s.dataset.hex }));
    let selType = 'flexible';
    let selColor = colors[0] ? colors[0].name : 'antique';
    menu.innerHTML = '';
    const typeRow = el('div', 'bm-types');
    types.forEach((ty) => {
      const b = el('button', 'chip' + (ty.v === selType ? ' active' : ''), ty.label || 'TV default');
      b.type = 'button';
      b.addEventListener('click', () => {
        selType = ty.v;
        typeRow.querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', c === b));
        colorRow.hidden = (selType === '' || selType === 'none');
      });
      typeRow.appendChild(b);
    });
    const colorRow = el('div', 'bm-colors');
    colors.forEach((c) => {
      const s = el('button', 'matte-swatch' + (c.name === selColor ? ' active' : ''));
      s.type = 'button'; s.style.background = '#' + c.hex; s.title = c.name;
      s.addEventListener('click', () => {
        selColor = c.name;
        colorRow.querySelectorAll('.matte-swatch').forEach((x) => x.classList.toggle('active', x === s));
      });
      colorRow.appendChild(s);
    });
    colorRow.hidden = (selType === '' || selType === 'none');
    const apply = el('button', 'btn btn-sm btn-primary bm-apply', 'Apply matte');
    apply.type = 'button';
    apply.addEventListener('click', () => {
      menu.hidden = true;
      let matte = null;
      if (selType === 'none') matte = 'none';
      else if (selType) matte = `${selType}_${selColor}`;
      runBulk('matte', matte === null ? '' : matte);
    });
    menu.append(typeRow, colorRow, apply);
  }

  async function runBulk(action, param) {
    let cards = selectedCards();
    if (!cards.length) return;
    if (action === 'delete') {
      cards = cards.filter((c) => c.dataset.candelete === '1');
      if (!cards.length) { toast('You cannot delete any of the selected photos'); return; }
      if (!confirm(`Delete ${cards.length} photo${cards.length > 1 ? 's' : ''}? They will also be removed from the TVs.`)) return;
    }
    if ((action === 'tag' || action === 'untag') && !param) {
      param = prompt(action === 'tag' ? 'Tag name to add:' : 'Tag name to remove:') || '';
      param = param.trim();
      if (!param) return;
    }
    const ids = cards.map((c) => +c.dataset.id);
    const sendParam = (action === 'matte') ? (param || null) : (param || null);
    try {
      const data = await postJSON('/photos/bulk', { ids, action, param: sendParam });
      toast(`${data.done} photo${data.done === 1 ? '' : 's'} updated`, 'success');
      setTimeout(() => location.reload(), 900);
    } catch (e) {
      toast(e.message);
    }
  }

  if (bulkBar) {
    buildTvMenus();
    buildMatteMenu();
    bulkBar.addEventListener('click', (e) => {
      const menuBtn = e.target.closest('[data-menu]');
      if (menuBtn) {
        const menu = bulkBar.querySelector(`[data-menu-for="${menuBtn.dataset.menu}"]`);
        const wasOpen = menu && !menu.hidden;
        closeBulkMenus();
        if (menu && !wasOpen) menu.hidden = false;
        return;
      }
      const actBtn = e.target.closest('[data-bulk]');
      if (actBtn) { closeBulkMenus(); runBulk(actBtn.dataset.bulk, null); }
    });
    document.addEventListener('click', (e) => {
      if (!e.target.closest('.bulk-menu-wrap')) closeBulkMenus();
    });
  }

  /* ------------------------------------------------------------ favorite */
  async function toggleFavorite(card, btn) {
    btn.disabled = true;
    try {
      const data = await post(`/photo/${card.dataset.id}/favorite`);
      const fav = !!data.favorite;
      card.dataset.fav = fav ? '1' : '0';
      btn.classList.toggle('is-fav', fav);
      btn.setAttribute('aria-pressed', fav ? 'true' : 'false');
      if (!reduceMotion) {
        btn.classList.add('pop');
        btn.addEventListener('animationend', () => btn.classList.remove('pop'), { once: true });
      }
      if (filterState.chip === 'favorites') applyFilters();
    } catch (e) {
      toast(e.message);
    } finally { btn.disabled = false; }
  }

  /* ------------------------------------------------- date helpers (TV fmt) */
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function humanDate(tvdate) {
    // "YYYY:MM:DD HH:MM:SS" -> "Jan 2, 2024 10:30"
    if (!tvdate) return '';
    const [d, t] = tvdate.split(' ');
    const parts = (d || '').split(':');
    if (parts.length !== 3) return tvdate;
    const label = `${MONTHS[+parts[1] - 1] || parts[1]} ${+parts[2]}, ${parts[0]}`;
    return t ? `${label} ${t.slice(0, 5)}` : label;
  }
  function tvDateToInput(tvdate) {
    // "YYYY:MM:DD HH:MM:SS" -> "YYYY-MM-DDTHH:MM"
    if (!tvdate) return '';
    const [d, t] = tvdate.split(' ');
    const parts = (d || '').split(':');
    if (parts.length !== 3) return '';
    return `${parts[0]}-${parts[1]}-${parts[2]}T${(t || '12:00').slice(0, 5)}`;
  }
  function inputToTvDate(v) {
    // "YYYY-MM-DDTHH:MM" or "YYYY-MM-DD" -> "YYYY:MM:DD HH:MM:SS"
    if (!v) return '';
    const [d, t] = v.split('T');
    return `${d.replaceAll('-', ':')} ${(t || '12:00')}:00`;
  }

  /* ------------------------------------------------------------- overlay */
  const overlay = $('#overlay');
  let currentCard = null;
  let rebuildOverlayRows = () => {};
  if (overlay && grid) {
    const img = $('#overlayImg');
    const nameInput = $('#ovName');
    const taken = $('#ovTaken');
    const dateEdit = $('#ovDateEdit');
    const dateForm = $('#ovDateForm');
    const dateInput = $('#ovDateInput');
    const dateSave = $('#ovDateSave');
    const dateClear = $('#ovDateClear');
    const dateHint = $('#ovDateHint');
    const uploader = $('#ovUploader');
    const tagsInput = $('#ovTags');
    const tagsSave = $('#ovTagsSave');
    const tvRows = $('#ovTvRows');
    const styleMatte = $('#ovStyleMatte');
    const styleRow = $('#ovStyleRow');
    const styleSel = $('#ovStyle');
    const matteWrap = $('#ovMatteWrap');
    const matteTypes = $('#matteTypes');
    const matteColors = $('#matteColors');
    const matteApply = $('#matteApply');
    const btnCrop = $('#ovCrop');
    const btnDelete = $('#ovDelete');

    function buildTvRows(card) {
      tvRows.innerHTML = '';
      const states = cardStates(card);
      TVS.forEach((tv) => {
        const st = states[String(tv.id)];
        const row = el('div', 'ov-tvrow');
        const gone = !st || st.status === 'removed';
        row.appendChild(el('span', 'tvname', tv.name));
        row.appendChild(el('span',
          `tvstate ${gone ? 'st-none' : `st-${st.status}`}`,
          gone ? 'not on this TV' : (st.status === 'on_tv' ? 'on TV' : st.status)));
        const btn = el('button', 'btn btn-sm', 'Show on TV');
        btn.type = 'button';
        btn.disabled = gone || st.status !== 'on_tv';
        btn.addEventListener('click', async () => {
          btn.disabled = true;
          try {
            await post(`/photo/${card.dataset.id}/display/${tv.id}`);
            btn.textContent = 'Showing ✓';
            setTimeout(() => { btn.textContent = 'Show on TV'; btn.disabled = false; }, 2000);
          } catch (e) {
            toast(e.message);
            btn.disabled = false;
          }
        });
        row.appendChild(btn);
        if (!gone && st.status === 'failed' && st.error) row.appendChild(el('p', 'tverr', st.error));
        tvRows.appendChild(row);
      });
    }
    rebuildOverlayRows = (card) => { if (currentCard === card) buildTvRows(card); };

    // ---- matte picker state
    const imgwrap = overlay.querySelector('.overlay-imgwrap');
    const matteNote = $('#ovMatteNote');
    const MAT_PADS = { modernthin: '3%', modern: '6%', modernwide: '10%',
                       flexible: '5%', shadowbox: '7%', panoramic: '6%',
                       triptych: '6%', mix: '6%', squares: '6%' };
    function updateMattePreview() {
      const { mtype, mcolor } = matteSelection();
      const active = matteColors.querySelector('.matte-swatch.active');
      const hex = active ? active.dataset.hex : 'e0dbd2';
      imgwrap.classList.remove(...[...imgwrap.classList].filter((c) => c.startsWith('mat-')));
      if (!mtype || mtype === 'none') {   // TV default or no mat: plain preview
        imgwrap.classList.remove('matted');
        if (matteNote) matteNote.hidden = true;
        return;
      }
      imgwrap.classList.add('matted', `mat-${mtype}`);
      imgwrap.style.setProperty('--mat-color', `#${hex}`);
      imgwrap.style.setProperty('--mat-pad', MAT_PADS[mtype] || '6%');
      if (matteNote) matteNote.hidden = false;
    }
    function matteSelection() {
      const t = matteTypes.querySelector('.chip.active');
      const c = matteColors.querySelector('.matte-swatch.active');
      return { mtype: t ? t.dataset.mtype : '', mcolor: c ? c.dataset.mcolor : '' };
    }
    function setMatteUI(matte) {
      let mtype = ''; let mcolor = '';
      if (matte === 'none') mtype = 'none';
      else if (matte) {
        const i = matte.indexOf('_');
        if (i > 0) { mtype = matte.slice(0, i); mcolor = matte.slice(i + 1); } else mtype = matte;
      }
      matteTypes.querySelectorAll('.chip').forEach((b) =>
        b.classList.toggle('active', b.dataset.mtype === mtype));
      matteColors.querySelectorAll('.matte-swatch').forEach((b) =>
        b.classList.toggle('active', b.dataset.mcolor === mcolor));
      matteColors.hidden = (mtype === '' || mtype === 'none');
      updateMattePreview();
    }
    matteTypes.addEventListener('click', (e) => {
      const b = e.target.closest('[data-mtype]');
      if (!b) return;
      matteTypes.querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', c === b));
      const needsColor = b.dataset.mtype !== '' && b.dataset.mtype !== 'none';
      matteColors.hidden = !needsColor;
      if (needsColor && !matteColors.querySelector('.matte-swatch.active')) {
        const first = matteColors.querySelector('.matte-swatch');
        if (first) first.classList.add('active');
      }
      updateMattePreview();
    });
    matteColors.addEventListener('click', (e) => {
      const b = e.target.closest('.matte-swatch');
      if (!b) return;
      matteColors.querySelectorAll('.matte-swatch').forEach((c) => c.classList.toggle('active', c === b));
      updateMattePreview();
    });
    matteApply.addEventListener('click', async () => {
      if (!currentCard) return;
      const { mtype, mcolor } = matteSelection();
      let matte = null;
      if (mtype === 'none') matte = 'none';
      else if (mtype) matte = `${mtype}_${mcolor || 'neutral'}`;
      matteApply.disabled = true;
      try {
        const r = await postJSON(`/photo/${currentCard.dataset.id}/matte`, { matte });
        currentCard.dataset.matte = matte || '';
        let msg = `Matte applied on ${r.live} TV${r.live === 1 ? '' : 's'}`;
        if (r.deferred > 0) msg += ` (${r.deferred} will update when the TV is back)`;
        toast(msg, 'success');
      } catch (e) {
        toast(e.message);
      } finally { matteApply.disabled = false; }
    });

    // ---- open/close
    function openCard(card) {
      currentCard = card;
      const d = card.dataset;
      const croppable = d.croppable === '1';
      const editsTag = encodeURIComponent(d.edits || '');
      img.src = croppable ? `/photo/${d.id}/preview?e=${editsTag}`
        : (d.thumb === '1' ? `/thumbs/${d.id}.jpg` : '');
      img.alt = d.filename;
      nameInput.value = d.filename;
      taken.textContent = d.taken ? `Taken ${humanDate(d.taken)}` : 'No date';
      dateForm.hidden = true;
      dateInput.value = tvDateToInput(d.taken);
      dateHint.hidden = true;
      uploader.textContent = d.uploader ? `Uploaded by ${d.uploader}` : '';
      tagsInput.value = cardTags(card).join(', ');
      buildTvRows(card);
      const hasMatte = croppable || d.source === 'import';
      styleMatte.hidden = !croppable && !hasMatte;
      styleRow.hidden = !croppable;
      styleSel.value = d.style || 'fit';
      matteWrap.hidden = !hasMatte;
      if (hasMatte) setMatteUI(d.matte || '');
      else { imgwrap.classList.remove('matted'); if (matteNote) matteNote.hidden = true; }
      btnCrop.hidden = !croppable;
      btnDelete.hidden = d.candelete !== '1';
      overlay.hidden = false;
      document.body.style.overflow = 'hidden';
    }
    let closing = false;
    function closeOverlay() {
      if (overlay.hidden || closing) return;
      const finish = () => {
        overlay.classList.remove('closing');
        overlay.hidden = true;
        img.src = '';
        currentCard = null;
        closing = false;
        document.body.style.overflow = '';
      };
      if (reduceMotion) { finish(); return; }
      closing = true;
      overlay.classList.add('closing');
      setTimeout(finish, 160);
    }
    overlay._close = closeOverlay;
    window.__fvRefreshPreview = (card) => {
      if (currentCard && card === currentCard) {
        const editsTag = encodeURIComponent(card.dataset.edits || '');
        img.src = `/photo/${card.dataset.id}/preview?e=${editsTag}`;
      }
    };

    grid.addEventListener('click', (e) => {
      const card = e.target.closest('.card');
      if (!card) return;
      if (e.target.closest('.badge-failed')) { openCard(card); return; }
      const favBtn = e.target.closest('button.fav');
      if (selectMode) {
        card.classList.toggle('selected');
        updateBulkBar();
        return;
      }
      if (favBtn) { toggleFavorite(card, favBtn); return; }
      openCard(card);
    });
    grid.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      const card = e.target.closest('.card');
      if (!card) return;
      if (selectMode) { card.classList.toggle('selected'); updateBulkBar(); } else openCard(card);
    });
    $('#overlayClose').addEventListener('click', closeOverlay);
    $('#overlayBackdrop').addEventListener('click', closeOverlay);

    // ---- editable title
    async function saveTitle() {
      if (!currentCard) return;
      const name = nameInput.value.trim();
      if (!name || name === currentCard.dataset.filename) {
        nameInput.value = currentCard.dataset.filename;
        return;
      }
      try {
        await postJSON(`/photo/${currentCard.dataset.id}/meta`, { filename: name });
        currentCard.dataset.filename = name;
        const ci = currentCard.querySelector('img');
        if (ci) ci.alt = name;
        toast('Title saved', 'success');
      } catch (e) {
        toast(e.message);
        nameInput.value = currentCard.dataset.filename;
      }
    }
    nameInput.addEventListener('blur', saveTitle);
    nameInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); nameInput.blur(); }
      if (e.key === 'Escape') {
        e.stopPropagation();
        nameInput.value = currentCard ? currentCard.dataset.filename : '';
        nameInput.blur();
      }
    });

    // ---- editable date
    dateEdit.addEventListener('click', () => {
      dateForm.hidden = !dateForm.hidden;
      if (!dateForm.hidden && currentCard) {
        dateHint.hidden = !(+currentCard.dataset.assigned > 0);
        dateInput.focus();
      } else dateHint.hidden = true;
    });
    async function saveDate(raw) {
      if (!currentCard) return;
      try {
        await postJSON(`/photo/${currentCard.dataset.id}/meta`, { taken_date: raw });
        currentCard.dataset.taken = raw ? inputToTvDate(raw) : '';
        taken.textContent = currentCard.dataset.taken
          ? `Taken ${humanDate(currentCard.dataset.taken)}` : 'No date';
        dateForm.hidden = true;
        dateHint.hidden = true;
        toast(raw ? 'Date saved' : 'Date cleared', 'success');
      } catch (e) {
        toast(e.message);
      }
    }
    dateSave.addEventListener('click', () => saveDate(dateInput.value));
    dateClear.addEventListener('click', () => { dateInput.value = ''; saveDate(''); });

    // ---- tags
    tagsSave.addEventListener('click', async () => {
      if (!currentCard) return;
      const tags = tagsInput.value.split(',').map((t) => t.trim()).filter(Boolean);
      tagsSave.disabled = true;
      try {
        const data = await postJSON(`/photo/${currentCard.dataset.id}/tags`, { tags });
        currentCard.dataset.tags = JSON.stringify(data.tags || tags);
        currentCard._tags = null;
        toast('Tags saved', 'success');
      } catch (e) {
        toast(e.message);
      } finally { tagsSave.disabled = false; }
    });
    tagsInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); tagsSave.click(); }
    });

    // ---- style
    styleSel.addEventListener('change', async () => {
      if (!currentCard) return;
      try {
        await post(`/photo/${currentCard.dataset.id}/style`,
          new URLSearchParams({ style: styleSel.value }));
        currentCard.dataset.style = styleSel.value;
        toast('Style saved — re-rendering for TV', 'success');
      } catch (e) {
        toast(e.message);
        styleSel.value = currentCard.dataset.style || 'fit';
      }
    });

    btnCrop.addEventListener('click', () => {
      if (!currentCard) return;
      let crop = null;
      try {
        const raw = JSON.parse(currentCard.dataset.edits || '""');
        if (raw) crop = (JSON.parse(raw).crop) || null;
      } catch (_) { /* no valid edits */ }
      openCropEditor(currentCard, crop);
    });

    btnDelete.addEventListener('click', async () => {
      if (!currentCard) return;
      if (!confirm(`Delete "${currentCard.dataset.filename}"? It will also be removed from the TVs.`)) return;
      btnDelete.disabled = true;
      try {
        await post(`/photo/${currentCard.dataset.id}/delete`);
        const card = currentCard;
        if (reduceMotion) {
          card.remove();
        } else {
          card.classList.add('removing');
          setTimeout(() => card.remove(), 200);
        }
        closeOverlay();
        refreshCountsFromCards();
      } catch (e) {
        toast(e.message);
      } finally { btnDelete.disabled = false; }
    });
  }

  /* --------------------------------------------------------- crop editor */
  const cropEd = $('#cropEditor');
  let openCropEditor = () => {};
  if (cropEd) {
    const stage = $('#cropStage');
    const cimg = $('#cropImg');
    const box = $('#cropBox');
    const aspects = $('#cropAspects');
    const MIN = 32;
    let cropCard = null;
    let aspect = null;            // width / height, or null for free
    let norm = [0, 0, 1, 1];      // normalized [x,y,w,h] source of truth
    let ib = { x: 0, y: 0, w: 1, h: 1 }; // displayed image box in stage px
    let rect = { x: 0, y: 0, w: 1, h: 1 }; // crop box in px, relative to image
    let drag = null;

    const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

    function layout() {
      if (!cimg.naturalWidth) return;
      const sw = stage.clientWidth - 28;
      const sh = stage.clientHeight - 28;
      const scale = Math.min(sw / cimg.naturalWidth, sh / cimg.naturalHeight);
      ib.w = cimg.naturalWidth * scale;
      ib.h = cimg.naturalHeight * scale;
      ib.x = (stage.clientWidth - ib.w) / 2;
      ib.y = (stage.clientHeight - ib.h) / 2;
      cimg.style.left = `${ib.x}px`;
      cimg.style.top = `${ib.y}px`;
      cimg.style.width = `${ib.w}px`;
      cimg.style.height = `${ib.h}px`;
      rect = { x: norm[0] * ib.w, y: norm[1] * ib.h, w: norm[2] * ib.w, h: norm[3] * ib.h };
      paint();
    }
    function paint() {
      box.style.left = `${ib.x + rect.x}px`;
      box.style.top = `${ib.y + rect.y}px`;
      box.style.width = `${rect.w}px`;
      box.style.height = `${rect.h}px`;
    }
    function syncNorm() {
      norm = [
        clamp(rect.x / ib.w, 0, 1), clamp(rect.y / ib.h, 0, 1),
        clamp(rect.w / ib.w, 0.001, 1), clamp(rect.h / ib.h, 0.001, 1),
      ];
    }

    function reshapeToAspect() {
      if (!aspect) return;
      const cx = rect.x + rect.w / 2;
      const cy = rect.y + rect.h / 2;
      let w = rect.w;
      let h = w / aspect;
      if (h > rect.h) { h = rect.h; w = h * aspect; }
      if (w > ib.w) { w = ib.w; h = w / aspect; }
      if (h > ib.h) { h = ib.h; w = h * aspect; }
      rect = {
        x: clamp(cx - w / 2, 0, ib.w - w),
        y: clamp(cy - h / 2, 0, ib.h - h),
        w, h,
      };
      syncNorm();
      paint();
    }

    function applyDrag(mode, dx, dy) {
      const s = drag.start;
      let { x, y, w, h } = s;
      if (mode === 'move') {
        x = clamp(s.x + dx, 0, ib.w - w);
        y = clamp(s.y + dy, 0, ib.h - h);
        rect = { x, y, w, h };
        return;
      }
      if (mode.includes('e')) w = s.w + dx;
      if (mode.includes('s')) h = s.h + dy;
      if (mode.includes('w')) { x = s.x + dx; w = s.w - dx; }
      if (mode.includes('n')) { y = s.y + dy; h = s.h - dy; }
      if (w < MIN) { if (mode.includes('w')) x -= MIN - w; w = MIN; }
      if (h < MIN) { if (mode.includes('n')) y -= MIN - h; h = MIN; }
      if (aspect) {
        if (mode === 'n' || mode === 's') {
          const nw = h * aspect; x += (w - nw) / 2; w = nw;
        } else if (mode === 'e' || mode === 'w') {
          const nh = w / aspect; y += (h - nh) / 2; h = nh;
        } else {
          const nh = w / aspect;
          if (mode.includes('n')) y += h - nh;
          h = nh;
        }
      }
      if (x < 0) { w += x; x = 0; }
      if (y < 0) { h += y; y = 0; }
      if (x + w > ib.w) w = ib.w - x;
      if (y + h > ib.h) h = ib.h - y;
      if (aspect) {
        if (w / h > aspect) {
          const nw = h * aspect;
          if (mode.includes('w')) x += w - nw;
          w = nw;
        } else if (w / h < aspect) {
          const nh = w / aspect;
          if (mode.includes('n')) y += h - nh;
          h = nh;
        }
      }
      rect = { x, y, w, h };
    }

    stage.addEventListener('pointerdown', (e) => {
      const handle = e.target.closest('.ch');
      if (!handle && !e.target.closest('.crop-box')) return;
      e.preventDefault();
      stage.setPointerCapture(e.pointerId);
      drag = {
        mode: handle ? handle.dataset.h : 'move',
        sx: e.clientX, sy: e.clientY,
        start: { ...rect },
      };
      box.classList.add('dragging');
    });
    stage.addEventListener('pointermove', (e) => {
      if (!drag) return;
      e.preventDefault();
      applyDrag(drag.mode, e.clientX - drag.sx, e.clientY - drag.sy);
      paint();
    });
    ['pointerup', 'pointercancel'].forEach((ev) => stage.addEventListener(ev, () => {
      if (!drag) return;
      drag = null;
      box.classList.remove('dragging');
      syncNorm();
    }));

    aspects.addEventListener('click', (e) => {
      const chip = e.target.closest('[data-aspect]');
      if (!chip) return;
      aspects.querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', c === chip));
      aspect = chip.dataset.aspect ? parseFloat(chip.dataset.aspect) : null;
      reshapeToAspect();
    });

    function closeCrop() {
      cropEd.hidden = true;
      cimg.src = '';
      cropCard = null;
      document.body.style.overflow = overlay && !overlay.hidden ? 'hidden' : '';
    }
    $('#cropCancel').addEventListener('click', closeCrop);

    $('#cropSave').addEventListener('click', async () => {
      if (!cropCard) return;
      syncNorm();
      const crop = norm.map((v) => Math.round(clamp(v, 0, 1) * 100000) / 100000);
      crop[2] = Math.min(crop[2], 1 - crop[0]) || 0.001;
      crop[3] = Math.min(crop[3], 1 - crop[1]) || 0.001;
      try {
        await postJSON(`/photo/${cropCard.dataset.id}/crop`, { crop });
        cropCard.dataset.edits = JSON.stringify(JSON.stringify({ crop }));
        if (window.__fvRefreshPreview) window.__fvRefreshPreview(cropCard);
        toast('Crop saved', 'success');
        closeCrop();
      } catch (e) {
        toast(e.message);
      }
    });
    $('#cropClear').addEventListener('click', async () => {
      if (!cropCard) return;
      try {
        await postJSON(`/photo/${cropCard.dataset.id}/crop`, { crop: null });
        cropCard.dataset.edits = JSON.stringify('');
        if (window.__fvRefreshPreview) window.__fvRefreshPreview(cropCard);
        toast('Crop cleared', 'success');
        closeCrop();
      } catch (e) {
        toast(e.message);
      }
    });

    window.addEventListener('resize', () => { if (!cropEd.hidden) layout(); });

    openCropEditor = (card, existingCrop) => {
      cropCard = card;
      norm = (existingCrop && existingCrop.length === 4) ? [...existingCrop] : [0, 0, 1, 1];
      aspect = null;
      aspects.querySelectorAll('.chip').forEach((c) =>
        c.classList.toggle('active', c.dataset.aspect === ''));
      cropEd.hidden = false;
      document.body.style.overflow = 'hidden';
      const src = `/photo/${card.dataset.id}/original`;
      if (cimg.src.endsWith(src) && cimg.naturalWidth) { layout(); return; }
      cimg.onload = layout;
      cimg.src = src;
    };
    cropEd._close = closeCrop;
  }

  /* -------------------------------------------------------- discover art */
  const artPanel = $('#artPanel');
  if (artPanel) {
    const srcSel = $('#artSource');
    const qIn = $('#artQuery');
    const goBtn = $('#artSearch');
    const agrid = $('#artGrid');
    const more = $('#artMore');
    const note = $('#artNote');
    const emptyEl = $('#artEmpty');
    const openBtn = $('#discoverBtn');
    const PLACEHOLDERS = {
      reddit: 'Subreddit, e.g. EarthPorn',
      nasa: 'Random APOD picks, search not used',
    };
    let page = 1;

    function setPlaceholder() {
      qIn.placeholder = PLACEHOLDERS[srcSel.value] || 'Search for art, e.g. coastline';
      qIn.disabled = srcSel.value === 'nasa';
    }
    srcSel.addEventListener('change', setPlaceholder);
    setPlaceholder();

    function addResult(x) {
      const fig = el('figure', 'art-card');
      const im = el('img');
      im.loading = 'lazy';
      im.src = x.thumb;
      im.alt = x.title || '';
      fig.appendChild(im);
      const cap = el('figcaption');
      cap.append(el('span', 'art-t', x.title || 'Untitled'), el('span', 'art-a muted', x.author || ''));
      const row = el('div', 'art-actions');
      const add = el('button', 'btn btn-sm btn-primary', 'Add to library');
      add.type = 'button';
      add.addEventListener('click', async () => {
        add.disabled = true;
        add.textContent = 'Adding…';
        const r = await jsonFetch('/sources/import', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source: x.source, url: x.full, title: x.title }),
        });
        if (r.ok || r.status === 409) {
          add.textContent = r.status === 409 ? 'Already in library' : 'Added ✓';
          fig.classList.add('art-added');
          toast(r.status === 409 ? 'Already in the library'
            : 'Added to library — rendering for TV', 'success');
        } else {
          add.disabled = false;
          add.textContent = 'Add to library';
          toast(r.data.detail || 'Import failed');
        }
      });
      row.appendChild(add);
      if (x.link) {
        const a = el('a', 'art-link', '↗');
        a.href = x.link;
        a.target = '_blank';
        a.rel = 'noopener';
        a.title = 'Open original page';
        a.setAttribute('aria-label', 'Open original page');
        row.appendChild(a);
      }
      cap.appendChild(row);
      fig.appendChild(cap);
      agrid.appendChild(fig);
    }

    async function search(reset) {
      if (reset) { page = 1; agrid.innerHTML = ''; more.hidden = true; }
      note.hidden = true;
      emptyEl.hidden = true;
      goBtn.disabled = true;
      more.disabled = true;
      more.textContent = 'Loading…';
      try {
        const r = await jsonFetch(`/sources/search?source=${encodeURIComponent(srcSel.value)}`
          + `&q=${encodeURIComponent(qIn.value)}&page=${page}`);
        if (!r.ok) {
          note.textContent = r.data.detail || `Search failed (${r.status})`;
          note.hidden = false;
          return;
        }
        const results = r.data.results || [];
        results.forEach(addResult);
        more.hidden = !results.length;
        emptyEl.hidden = !(page === 1 && !results.length);
      } catch (e) {
        note.textContent = e.message;
        note.hidden = false;
      } finally {
        goBtn.disabled = false;
        more.disabled = false;
        more.textContent = 'More';
      }
    }
    goBtn.addEventListener('click', () => search(true));
    qIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') search(true); });
    more.addEventListener('click', () => { page += 1; search(false); });

    function closeArt() {
      artPanel.hidden = true;
      document.body.style.overflow = (overlay && !overlay.hidden) ? 'hidden' : '';
    }
    artPanel._close = closeArt;
    if (openBtn) {
      openBtn.addEventListener('click', () => {
        artPanel.hidden = false;
        document.body.style.overflow = 'hidden';
        if (!qIn.disabled) qIn.focus();
      });
    }
    $('#artClose').addEventListener('click', closeArt);
  }

  /* --------------------------------------------------- websocket + poll */
  const importLine = $('#importLine');
  const importProgress = $('#importProgress');
  let wsOpen = false;

  async function refreshStatus() {
    try {
      const res = await fetch('/api/status');
      if (!res.ok) return;
      const s = await res.json();
      absorbTvMap(s.tvs);
      if (grid && s.counts) renderCounts(s.counts);
      if (importLine && s.import) {
        importLine.hidden = !s.import.running;
        if (importProgress) importProgress.textContent = `${s.import.done}/${s.import.total}`;
      }
    } catch (_) { /* offline; try again next tick */ }
  }
  if (grid && countsLine) {
    (function pollLoop() {
      setTimeout(async () => {
        await refreshStatus();
        pollLoop();
      }, wsOpen ? 60000 : 15000);
    })();
  }

  function handleWsEvent(msg) {
    switch (msg.type) {
      case 'tv_status': {
        const t = TVS.find((x) => x.id === +msg.tv_id);
        if (t) t.ok = msg.ok ? 1 : 0;
        updatePillFromTVS();
        const tvCard = document.querySelector(`.tv-card[data-tv="${msg.tv_id}"]`);
        if (tvCard) {
          const p = tvCard.querySelector('.js-tvpill');
          if (p) {
            p.classList.toggle('pill-ok', !!msg.ok);
            p.classList.toggle('pill-warn', !msg.ok);
            const txt = p.querySelector('.js-tvpill-text');
            if (txt) txt.textContent = msg.ok ? 'connected' : 'unreachable';
          }
          const err = tvCard.querySelector('.tv-err');
          if (err) {
            err.textContent = msg.error || '';
            err.hidden = !!msg.ok || !msg.error;
          }
        }
        break;
      }
      case 'pushed': {
        const card = grid && grid.querySelector(`.card[data-id="${msg.photo_id}"]`);
        if (card) {
          const b = applyState(card, msg.tv_id, 'on_tv');
          if (b && !reduceMotion) {
            b.classList.add('pulse');
            b.addEventListener('animationend', () => b.classList.remove('pulse'), { once: true });
          }
          refreshCountsFromCards();
          rebuildOverlayRows(card);
        }
        break;
      }
      case 'push_failed': {
        const card = grid && grid.querySelector(`.card[data-id="${msg.photo_id}"]`);
        if (card) {
          applyState(card, msg.tv_id, 'failed', msg.error);
          refreshCountsFromCards();
          rebuildOverlayRows(card);
        }
        toast(`${msg.filename || 'Photo'}: ${msg.error || 'push failed'}`);
        break;
      }
      case 'import':
        if (importLine) {
          importLine.hidden = !msg.running;
          if (importProgress) importProgress.textContent = `${msg.done}/${msg.total}`;
        }
        if (msg.error) toast(`Import: ${msg.error}`);
        break;
      case 'schedule_fired':
        toast(`Schedule changed the art on ${tvName(msg.tv_id)}`, 'success');
        break;
      case 'ingested':
        toast(`${msg.count} new photo${msg.count === 1 ? '' : 's'} from watched folder`, 'success');
        if (grid) setTimeout(() => location.reload(), 1500);
        break;
      case 'counts_dirty':
        refreshStatus();
        break;
      default:
        break;
    }
  }

  if (authed && 'WebSocket' in window) {
    let backoff = 1000;
    let pingTimer = null;
    let dead = false;
    const connect = () => {
      if (dead) return;
      let sock;
      try {
        sock = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
      } catch (_) { retry(); return; }
      sock.onopen = () => {
        wsOpen = true;
        backoff = 1000;
        pingTimer = setInterval(() => {
          try { sock.send('ping'); } catch (_) { /* closing */ }
        }, 25000);
      };
      sock.onmessage = (e) => {
        try { handleWsEvent(JSON.parse(e.data)); } catch (_) { /* not JSON */ }
      };
      sock.onclose = (e) => {
        wsOpen = false;
        clearInterval(pingTimer);
        if (e.code === 4401) { dead = true; return; } // not authenticated
        retry();
      };
      sock.onerror = () => { try { sock.close(); } catch (_) { /* already closed */ } };
    };
    const retry = () => {
      setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 30000);
    };
    connect();
  }

  /* -------------------------------------------------------- pairing (TV) */
  $$('.js-pair').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const wrap = btn.closest('[data-pairwrap]') || document;
      const panel = wrap.querySelector('.pair-panel');
      const count = wrap.querySelector('.pair-count');
      const errBox = wrap.querySelector('.pair-error');
      btn.disabled = true;
      if (errBox) errBox.hidden = true;
      if (panel) panel.hidden = false;
      let remaining = 60;
      if (count) count.textContent = remaining;
      const timer = setInterval(() => {
        remaining -= 1;
        if (count) count.textContent = Math.max(remaining, 0);
        if (remaining <= 0) clearInterval(timer);
      }, 1000);
      try {
        await post(btn.dataset.url);
        location.reload();
      } catch (e) {
        clearInterval(timer);
        if (panel) panel.hidden = true;
        if (errBox) { errBox.textContent = `Pairing failed: ${e.message}`; errBox.hidden = false; }
        btn.disabled = false;
      }
    });
  });

  /* ---------------------------------------------------------------- wake */
  $$('.js-wake').forEach((btn) => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await post(btn.dataset.url);
        toast('Wake packet sent to the TV', 'success');
      } catch (e) {
        toast(e.message);
      } finally { btn.disabled = false; }
    });
  });

  /* -------------------------------------------------------------- export */
  const exportModal = $('#exportModal');
  if (exportModal) {
    const envBox = $('#exportEnv');
    const title = $('#exportTitle');
    $$('.js-export').forEach((btn) => {
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        try {
          const data = await getJSON(btn.dataset.url);
          envBox.value = data.env || '';
          title.textContent = `Export connection: ${btn.dataset.name}`;
          exportModal.hidden = false;
        } catch (e) {
          toast(e.message);
        } finally { btn.disabled = false; }
      });
    });
    exportModal.querySelectorAll('[data-export-close]').forEach((el) =>
      el.addEventListener('click', () => { exportModal.hidden = true; }));
  }

  /* --------------------------------------------------------- TV discovery */
  const scanBtn = $('#scanBtn');
  if (scanBtn) {
    const box = $('#scanResults');
    const empty = $('#scanEmpty');
    const addForm = $('#addTvForm');
    scanBtn.addEventListener('click', async () => {
      scanBtn.disabled = true;
      scanBtn.classList.add('btn-scanning');
      const old = scanBtn.textContent;
      scanBtn.textContent = 'Scanning…';
      empty.hidden = true;
      try {
        const data = await getJSON('/tvs/discover');
        const rows = data.results || [];
        box.innerHTML = '';
        box.hidden = !rows.length;
        empty.hidden = !!rows.length;
        rows.forEach((r) => {
          const row = el('div', 'scan-row' + (r.already_added ? ' scan-known' : ''));
          const info = el('div', 'scan-info');
          const top = el('strong', '', r.name || r.model || r.host);
          if (r.frame) top.append(' ', el('span', 'tag tag-frame', 'FRAME'));
          if (r.already_added) top.append(' ', el('span', 'tag', 'already added'));
          const meta = el('span', 'scan-meta',
            [r.model, r.host, r.power ? `power: ${r.power}` : '', r.mac].filter(Boolean).join(' · '));
          info.append(top, meta);
          row.appendChild(info);
          if (!r.already_added && addForm) {
            const add = el('button', 'btn btn-sm btn-primary', 'Add');
            add.type = 'button';
            add.addEventListener('click', () => {
              const set = (n, v) => {
                const el = addForm.querySelector(`[name="${n}"]`);
                if (el) el.value = v || '';
              };
              set('name', r.name || r.model);
              set('host', r.host);
              set('mac', r.mac);
              addForm.requestSubmit ? addForm.requestSubmit() : addForm.submit();
            });
            row.appendChild(add);
          }
          box.appendChild(row);
        });
      } catch (e) {
        toast(`Scan failed: ${e.message}`);
      } finally {
        scanBtn.disabled = false;
        scanBtn.classList.remove('btn-scanning');
        scanBtn.textContent = old;
      }
    });
  }

  /* ------------------------------------------------------------ art mode */
  const asVal = (v) => ((v && typeof v === 'object' && 'value' in v) ? v.value : v);
  const asBool = (v) => {
    v = asVal(v);
    return v === true || v === 1 || v === 'on' || v === 'true' || v === '1';
  };
  function settingsMap(s) {
    const m = {};
    if (Array.isArray(s)) {
      s.forEach((it) => { if (it && it.item !== undefined) m[it.item] = it.value; });
    } else if (s && typeof s === 'object') {
      Object.assign(m, s);
    }
    return m;
  }

  $$('[data-artmode]').forEach((panel) => {
    const spin = panel.querySelector('[data-am-spin]');
    const form = panel.querySelector('[data-am-form]');
    const errBox = panel.querySelector('[data-am-error]');
    const note = panel.querySelector('[data-am-note]');
    const applyBtn = panel.querySelector('[data-am-apply]');
    const ctrl = (key) => panel.querySelector(`[data-key="${key}"]`);

    function markUnsupported(key) {
      const c = ctrl(key);
      if (!c) return;
      c.disabled = true;
      delete c.dataset.dirty;
      const lab = c.closest('label');
      if (lab) { lab.setAttribute('data-unsupported', ''); lab.title = 'Not supported by this TV'; }
    }
    function setCtrl(key, value, isBool) {
      const c = ctrl(key);
      if (!c) return;
      if (value === null || value === undefined) { markUnsupported(key); return; }
      if (isBool) c.checked = asBool(value);
      else c.value = String(asVal(value));
      const out = panel.querySelector(`[data-out="${key}"]`);
      if (out) out.value = c.value;
    }

    function populate(s) {
      setCtrl('artmode', s.artmode, true);
      setCtrl('brightness', asVal(s.brightness));
      setCtrl('color_temperature', asVal(s.color_temperature));
      const m = settingsMap(s.settings);
      setCtrl('motion_timer', m.motion_timer !== undefined ? m.motion_timer : null);
      setCtrl('motion_sensitivity', m.motion_sensitivity !== undefined ? m.motion_sensitivity : null);
      const bs = m.brightness_sensor !== undefined ? m.brightness_sensor
        : (m.brightness_sensor_setting !== undefined ? m.brightness_sensor_setting : null);
      setCtrl('brightness_sensor', bs, true);
      const ss = s.slideshow;
      if (ss === null || ss === undefined) {
        markUnsupported('slideshow_minutes');
        markUnsupported('slideshow_shuffle');
      } else if (typeof ss === 'object') {
        let dur = ss.duration !== undefined ? ss.duration : ss.value;
        if (dur === 'off' || dur === undefined || dur === null) dur = 0;
        const sel = ctrl('slideshow_minutes');
        if (sel) sel.value = String(parseInt(dur, 10) || 0);
        const sh = ctrl('slideshow_shuffle');
        if (sh) sh.checked = ss.type === true || /shuffle/i.test(String(ss.type || ''));
      }
    }

    async function load() {
      if (spin) spin.hidden = false;
      if (errBox) errBox.hidden = true;
      try {
        const s = await getJSON(`/tvs/${panel.dataset.tv}/artmode`);
        populate(s);
        panel.dataset.loaded = '1';
        if (form) form.hidden = false;
      } catch (e) {
        if (errBox) {
          errBox.textContent = `Could not read Art Mode settings: ${e.message}`;
          errBox.hidden = false;
        }
      } finally {
        if (spin) spin.hidden = true;
      }
    }
    panel.addEventListener('toggle', () => {
      if (panel.open && !panel.dataset.loaded) load();
    });

    panel.addEventListener('input', (e) => {
      const c = e.target.closest('[data-key]');
      if (!c) return;
      c.dataset.dirty = '1';
      const out = panel.querySelector(`[data-out="${c.dataset.key}"]`);
      if (out) out.value = c.value;
      if (note) note.hidden = true;
    });

    if (applyBtn) {
      applyBtn.addEventListener('click', async () => {
        const body = {};
        panel.querySelectorAll('[data-key]').forEach((c) => {
          if (c.dataset.dirty !== '1' || c.disabled) return;
          const key = c.dataset.key;
          if (key === 'artmode' || key === 'brightness_sensor') body[key] = c.checked;
          else if (key === 'brightness' || key === 'color_temperature') body[key] = parseInt(c.value, 10);
          else if (key === 'slideshow_minutes' || key === 'slideshow_shuffle') body._slideshow = true;
          else body[key] = c.value;
        });
        if (body._slideshow) {
          delete body._slideshow;
          const sel = ctrl('slideshow_minutes');
          const sh = ctrl('slideshow_shuffle');
          body.slideshow_minutes = parseInt(sel ? sel.value : '0', 10) || 0;
          body.slideshow_shuffle = sh ? sh.checked : true;
        }
        if (!Object.keys(body).length) {
          if (note) { note.textContent = 'Nothing changed.'; note.hidden = false; }
          return;
        }
        applyBtn.disabled = true;
        try {
          const r = await jsonFetch(`/tvs/${panel.dataset.tv}/artmode`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          const applied = r.data.applied || [];
          const errors = r.data.errors || {};
          if (applied.length) {
            toast(`Applied: ${applied.join(', ')}`, 'success');
            panel.querySelectorAll('[data-key]').forEach((c) => { delete c.dataset.dirty; });
          }
          Object.entries(errors).forEach(([k, m2]) => toast(`${k}: ${m2}`));
          if (!applied.length && !Object.keys(errors).length) {
            toast(r.data.detail || 'The TV did not accept the changes');
          }
        } catch (e) {
          toast(e.message);
        } finally { applyBtn.disabled = false; }
      });
    }
  });

  /* ---------------------------------------------------------- admin logs */
  const logBox = $('#logBox');
  if (logBox) {
    const refreshBtn = $('#logsRefresh');
    const autoCb = $('#logsAuto');
    let autoTimer = null;
    const LVL = { WARNING: 'log-warn', ERROR: 'log-err', CRITICAL: 'log-err' };
    async function loadLogs() {
      try {
        const data = await getJSON('/admin/logs');
        logBox.innerHTML = '';
        const logs = data.logs || [];
        logs.forEach((l) => {
          const row = el('div', `log-line ${LVL[l.level] || ''}`.trim());
          const ts = el('span', 'log-ts', agoText(l.ts));
          ts.title = new Date(l.ts * 1000).toLocaleString();
          row.append(ts, el('span', 'log-logger', l.logger || ''), el('span', 'log-msg', l.message || ''));
          logBox.appendChild(row);
        });
        if (!logs.length) logBox.innerHTML = '<p class="muted">No recent events.</p>';
        logBox.scrollTop = logBox.scrollHeight;
      } catch (_) {
        logBox.innerHTML = '<p class="muted">Could not load logs.</p>';
      }
    }
    loadLogs();
    if (refreshBtn) refreshBtn.addEventListener('click', loadLogs);
    if (autoCb) {
      autoCb.addEventListener('change', () => {
        if (autoCb.checked) autoTimer = setInterval(loadLogs, 5000);
        else clearInterval(autoTimer);
      });
    }
  }

  /* ----------------------------------------------------- escape handling */
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (cropEd && !cropEd.hidden) { cropEd._close(); return; }
    if (artPanel && !artPanel.hidden) { artPanel._close(); return; }
    if (exportModal && !exportModal.hidden) { exportModal.hidden = true; return; }
    if (overlay && !overlay.hidden) { overlay._close(); return; }
    if (bulkBar && !bulkBar.hidden) closeBulkMenus();
  });

  /* ------------------------------------------------------------- doctor */
  const rerunBtn = $('#rerunBtn');
  if (rerunBtn) rerunBtn.addEventListener('click', () => location.reload());

  /* ---------------------------------------------------- confirm + copy */
  document.querySelectorAll('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (e) => {
      if (!confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  document.querySelectorAll('[data-copy]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const src = document.querySelector(btn.dataset.copy);
      if (!src) return;
      const text = src.value !== undefined ? src.value : src.textContent;
      try {
        await navigator.clipboard.writeText(text);
      } catch (_) {
        src.select && src.select();
        document.execCommand('copy');
      }
      const old = btn.textContent;
      btn.textContent = 'Copied ✓';
      setTimeout(() => { btn.textContent = old; }, 1500);
    });
  });

  /* ------------------------------------------------------ relative time */
  function renderAgo() {
    $$('[data-ago]').forEach((el) => {
      el.textContent = agoText(parseFloat(el.dataset.ago) || 0);
    });
  }
  if (document.querySelector('[data-ago]')) {
    renderAgo();
    setInterval(renderAgo, 30000);
  }
})();
