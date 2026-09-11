/* framevalet — vanilla JS, no deps. Every block guards its elements. */
(() => {
  'use strict';
  const $ = (sel) => document.querySelector(sel);
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

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

  async function post(url, body) {
    const res = await fetch(url, { method: 'POST', body });
    let data = {};
    try { data = await res.json(); } catch (_) { /* non-JSON error body */ }
    if (!res.ok) throw new Error(data.detail || data.error || `Request failed (${res.status})`);
    return data;
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
        if (r.ok) anyOk = true;
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

  /* ------------------------------------------------------------- filters */
  const chips = $('#filterChips');
  if (chips && grid) {
    chips.addEventListener('click', (e) => {
      const chip = e.target.closest('.chip');
      if (!chip) return;
      chips.querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', c === chip));
      const f = chip.dataset.filter;
      grid.querySelectorAll('.card').forEach((card) => {
        const { status, source } = card.dataset;
        let show = true;
        if (f === 'external') show = source === 'external';
        else if (f === 'queued') show = status === 'queued' || status === 'processing';
        else if (f !== 'all') show = status === f;
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
      });
    });
  }

  /* ------------------------------------------------------------- overlay */
  const overlay = $('#overlay');
  if (overlay && grid) {
    const img = $('#overlayImg');
    const name = $('#ovName');
    const taken = $('#ovTaken');
    const uploader = $('#ovUploader');
    const btnDisplay = $('#ovDisplay');
    const btnDelete = $('#ovDelete');
    let current = null;

    function openCard(card) {
      current = card;
      const d = card.dataset;
      img.src = d.source !== 'import' ? `/photo/${d.id}/full` : `/thumbs/${d.id}.jpg`;
      img.alt = d.filename;
      name.textContent = d.filename;
      taken.textContent = d.taken ? `Taken ${d.taken}` : '';
      uploader.textContent = d.uploader ? `Uploaded by ${d.uploader}` : '';
      btnDisplay.hidden = d.status !== 'on_tv';
      btnDelete.hidden = d.candelete !== '1';
      overlay.hidden = false;
      document.body.style.overflow = 'hidden';
    }
    let closing = false;
    function close() {
      if (overlay.hidden || closing) return;
      const finish = () => {
        overlay.classList.remove('closing');
        overlay.hidden = true;
        img.src = '';
        current = null;
        closing = false;
        document.body.style.overflow = '';
      };
      if (reduceMotion) { finish(); return; }
      closing = true;
      overlay.classList.add('closing');
      setTimeout(finish, 160);
    }

    grid.addEventListener('click', (e) => {
      const card = e.target.closest('.card');
      if (card) openCard(card);
    });
    grid.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.closest('.card')) openCard(e.target.closest('.card'));
    });
    $('#overlayClose').addEventListener('click', close);
    $('#overlayBackdrop').addEventListener('click', close);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !overlay.hidden) close(); });

    btnDisplay.addEventListener('click', async () => {
      if (!current) return;
      btnDisplay.disabled = true;
      try {
        await post(`/photo/${current.dataset.id}/display`);
        btnDisplay.textContent = 'Now showing ✓';
        setTimeout(() => { btnDisplay.textContent = 'Show on TV now'; }, 2000);
      } catch (e) {
        toast(e.message);
      } finally { btnDisplay.disabled = false; }
    });

    btnDelete.addEventListener('click', async () => {
      if (!current) return;
      if (!confirm(`Delete "${current.dataset.filename}"? It will also be removed from the TV.`)) return;
      btnDelete.disabled = true;
      try {
        await post(`/photo/${current.dataset.id}/delete`);
        const card = current;
        if (reduceMotion) {
          card.remove();
        } else {
          card.classList.add('removing');
          setTimeout(() => card.remove(), 200);
        }
        close();
      } catch (e) {
        toast(e.message);
      } finally { btnDelete.disabled = false; }
    });
  }

  /* ------------------------------------------------------------- polling */
  const countsLine = $('#countsLine');
  if (grid && countsLine) {
    const pill = $('#tvPill');
    const pillText = $('#tvPillText');
    const importLine = $('#importLine');
    const importProgress = $('#importProgress');
    async function refresh() {
      try {
        const res = await fetch('/api/status');
        if (!res.ok) return;
        const s = await res.json();
        if (pill && pillText && s.tv) {
          const wasOk = pill.classList.contains('pill-ok');
          pill.classList.toggle('pill-ok', !!s.tv.tv_ok);
          pill.classList.toggle('pill-warn', !s.tv.tv_ok);
          pillText.textContent = s.tv.tv_ok ? 'TV connected' : (s.tv.tv_error || 'TV not connected');
          if (!wasOk && s.tv.tv_ok && !reduceMotion) {
            pill.classList.add('pill-glow');
            pill.addEventListener('animationend', () => pill.classList.remove('pill-glow'), { once: true });
          }
        }
        if (s.counts) {
          const c = s.counts;
          let line = `${c.on_tv} on TV · ${c.queued} queued`;
          if (c.processing) line += ` · ${c.processing} processing`;
          if (c.failed) line += ` · ${c.failed} failed`;
          countsLine.textContent = line;
        }
        if (importLine && s.import) {
          importLine.hidden = !s.import.running;
          if (importProgress) importProgress.textContent = `${s.import.done}/${s.import.total}`;
        }
      } catch (_) { /* offline; try again next tick */ }
    }
    setInterval(refresh, 15000);
  }

  /* ------------------------------------------------------------- doctor */
  const pairBtn = $('#pairBtn');
  if (pairBtn) {
    const panel = $('#pairPanel');
    const count = $('#pairCount');
    const errBox = $('#pairError');
    pairBtn.addEventListener('click', async () => {
      pairBtn.disabled = true;
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
        await post('/doctor/pair');
        location.reload();
      } catch (e) {
        clearInterval(timer);
        if (panel) panel.hidden = true;
        if (errBox) { errBox.textContent = `Pairing failed: ${e.message}`; errBox.hidden = false; }
        pairBtn.disabled = false;
      }
    });
  }
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
})();
