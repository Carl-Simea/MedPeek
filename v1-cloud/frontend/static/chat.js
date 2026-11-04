// MedPeek conversation workspace: chat, image analysis, care finder with map.
(() => {
  const $ = (s) => document.querySelector(s);
  const form = $('#chat-form'), files = $('#xray_upload'), input = $('#user-message'), messages = $('#messages');
  const windowEl = $('#chat-window'), error = $('#form-error'), historyBox = $('#history-list');
  const controls = [$('#send'), $('#attach'), $('#new-chat'), $('#zip-save')];
  const WORDS = ['Thinking', 'Pondering', 'Honking', 'Processing', 'Compiling'];
  const HEART = '<svg class="heart" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21.3 10.6 20C5.4 15.4 2 12.3 2 8.5 2 5.4 4.4 3 7.5 3c1.7 0 3.4.8 4.5 2.1C13.1 3.8 14.8 3 16.5 3 19.6 3 22 5.4 22 8.5c0 3.8-3.4 6.9-8.6 11.5L12 21.3z"/></svg>';
  let chatId = new URL(location.href).searchParams.get('chat');
  let busy = false, loading = false, signature = '', runStart = null, zipCode = '';
  let pendingBefore = new Set(), finalizing = new Map();
  const maps = new Map();

  // ---------- helpers
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  function showError(e) { error.textContent = e && e.message ? e.message : String(e || ''); }
  function word() { if (!runStart) runStart = Date.now(); const i = Math.floor((Date.now() - runStart) / 6600); return i < WORDS.length ? WORDS[i] : 'Finalizing'; }
  function thinkingNode(note, fixed) {
    const w = el('div', 'thinking');
    w.innerHTML = HEART + '<div><p class="thinking-word"></p><p class="note"></p></div>';
    const label = w.querySelector('.thinking-word');
    label.textContent = (fixed || word()) + '…';
    if (fixed) label.dataset.fixed = '1';
    w.querySelector('.note').textContent = note || '';
    return w;
  }
  setInterval(() => document.querySelectorAll('.thinking-word:not([data-fixed])').forEach((n) => { n.textContent = word() + '…'; }), 300);
  function bubble(role, text) {
    const wrap = el('article', 'message ' + role);
    wrap.append(el('div', 'label', role === 'user' ? 'YOU' : 'MEDPEEK'));
    const content = el('div', 'bubble', text || '');
    wrap.append(content);
    messages.append(wrap);
    return { el: wrap, content };
  }
  function scrollDown() { windowEl.scrollTop = windowEl.scrollHeight; }
  function setZipUI(z) {
    zipCode = z || '';
    if ($('#zip-input') && z) $('#zip-input').value = z;
    $('#zip-status').textContent = z ? 'Care results use ZIP ' + z + '.' : 'Used to find the closest ER, urgent care and specialists.';
  }

  // ---------- conversations
  async function list() {
    const data = await MP.api('/api/chats');
    historyBox.replaceChildren();
    if (!data.chats.length) { historyBox.append(el('p', 'note', 'Your conversations will appear here.')); return; }
    for (const c of data.chats) {
      const link = el('a', null, c.title);
      link.href = '/chat?chat=' + encodeURIComponent(c.id);
      const t = el('time', null, new Date(c.updated * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }));
      link.append(t);
      const del = el('button', 'del', '✕'); del.type = 'button'; del.title = 'Delete conversation';
      del.onclick = async (e) => { e.preventDefault(); e.stopPropagation(); if (!confirm('Delete this conversation?')) return; await MP.api('/api/chats/' + encodeURIComponent(c.id), { method: 'DELETE' }); if (c.id === chatId) { chatId = null; history.replaceState(null, '', '/chat'); messages.replaceChildren(); signature = ''; } await list(); };
      link.append(del);
      if (c.id === chatId) link.setAttribute('aria-current', 'page');
      link.onclick = (e) => { e.preventDefault(); if (busy) return; pendingBefore = new Set(); chatId = c.id; history.replaceState(null, '', '/chat?chat=' + chatId); signature = ''; closeSidebar(); load().catch(showError); };
      historyBox.append(link);
    }
  }
  async function ensureChat() {
    if (!chatId) { const c = await MP.api('/api/chats', { method: 'POST' }); chatId = c.id; history.replaceState(null, '', '/chat?chat=' + chatId); }
  }
  async function load() {
    if (!chatId || loading) return;
    loading = true;
    try {
      const c = await MP.api('/api/chats/' + encodeURIComponent(chatId));
      $('#image-count').textContent = c.image_count ? c.image_count + '/20 images' : '';
      $('#chat-title').textContent = c.title || 'New conversation';
      if (c.zip && c.zip !== zipCode) setZipUI(c.zip);
      const sig = JSON.stringify(c.messages);
      if (sig !== signature) {
        const nearBottom = windowEl.scrollHeight - windowEl.scrollTop - windowEl.clientHeight < 120;
        signature = sig;
        maps.clear();
        messages.replaceChildren();
        if (c.messages.length) $('#welcome')?.remove();
        const pendingNow = new Set();
        c.messages.forEach((m, i) => {
          const b = bubble(m.role === 'user' ? 'user' : 'bot', m.text);
          const key = chatId + ':' + i;
          const r = m.result;
          if (r && pendingBefore.has(key) && !finalizing.has(key)) { finalizing.set(key, Date.now() + 1100); setTimeout(() => { signature = ''; load().catch(showError); }, 1200); }
          if (r && finalizing.has(key) && Date.now() < finalizing.get(key)) {
            b.el.classList.add('pending'); b.content.replaceChildren(thinkingNode('Putting your answer together.', 'Finalizing'));
          } else if (r && r.type === 'chat') {
            renderChat(b.content, r);
          } else if (r && r.type === 'care') {
            renderCare(b.content, r, key);
          } else if (r && r.summary) {
            renderAnalysis(b.content, r, m.text, c.zip);
          }
          if (['sending', 'queued', 'running'].includes(m.status)) {
            pendingNow.add(key); b.el.classList.add('pending');
            b.content.replaceChildren(thinkingNode(m.status === 'sending' ? 'Sending…' : 'Gemma 4 is reading your image and comparing it with verified cases.'));
          }
          if (m.status === 'error') b.el.classList.add('error');
        });
        pendingBefore = pendingNow;
        if (!pendingNow.size && !busy) runStart = null;
        if (nearBottom) scrollDown();
      }
      await list();
    } finally { loading = false; }
  }

  // ---------- rendering
  function renderChat(container, r) {
    container.textContent = r.reply || '';
    if (r.urgency_notice) { const a = el('div', 'analysis-alert', r.urgency_notice); container.replaceChildren(a, el('div', null, r.reply || '')); }
    const chips = el('div', 'chips');
    if (r.needs_image) { const b = el('button', null, '📎 Attach the scan'); b.type = 'button'; b.onclick = () => files.click(); chips.append(b); }
    if (r.care && r.care.groups) { container.classList.add('rich'); container.append(el('div', null, '')); const box = el('div', 'care-card'); renderCare(box, r.care, 'chat-' + Math.random()); container.append(box); }
    else if (r.specialist && r.specialist !== 'Primary care doctor' && !r.care) {
      const b = el('button', null, '📍 Find a ' + r.specialist.split(' (')[0].toLowerCase() + ' near me'); b.type = 'button';
      b.onclick = () => { if (zipCode) saveZip(zipCode); else { $('#zip-input').focus(); openSidebar(); $('#zip-status').textContent = 'Enter your ZIP code to find care near you.'; } };
      chips.append(b);
    }
    if (chips.children.length) container.append(chips);
  }

  function renderAnalysis(container, result, fallback, chatZip) {
    if (!result || typeof result !== 'object') { container.textContent = fallback || 'No result returned.'; return; }
    container.replaceChildren(); container.classList.add('analysis-card', 'rich');
    const add = (parent, tag, text, cls) => { const n = el(tag, cls, text); parent.append(n); return n; };
    const section = (title) => { const s = el('section', 'analysis-section'); add(s, 'h3', title); container.append(s); return s; };
    const list = (parent, items) => { if (!Array.isArray(items) || !items.length) return; const ul = el('ul'); items.forEach((t) => add(ul, 'li', String(t))); parent.append(ul); };
    if (result.mock) add(container, 'div', 'Demo result · simulated answer', 'analysis-alert');
    if (result.urgency_notice) add(container, 'div', result.urgency_notice, 'analysis-alert');
    const meta = [result.body_part_label, result.modality_label].filter(Boolean).join(' · ') + (result.body_part_detected_by && result.body_part_detected_by !== 'user' ? ' · detected from your ' + result.body_part_detected_by : '');
    add(container, 'div', meta, 'analysis-meta');
    add(container, 'h2', 'Your image, explained');
    add(container, 'p', result.summary || 'No summary provided.', 'analysis-summary');
    if (result.coverage_note) add(container, 'p', result.coverage_note, 'note');
    if (result.is_medical_image === false || result.image_quality === 'unreadable') add(container, 'p', 'This image could not be read as a medical scan.', 'analysis-limit');
    if (Array.isArray(result.findings) && result.findings.length) {
      const block = section('What MedPeek noticed');
      result.findings.forEach((f) => { const item = el('div', 'finding'); add(item, 'h4', f.finding || 'Finding'); if (f.location) add(item, 'small', f.location); if (f.explanation) add(item, 'p', f.explanation); if (f.supported_by_reference_data === false) add(item, 'small', 'Not supported by the available reference data.'); block.append(item); });
    }
    const ref = result.referral;
    if (ref && ref.specialist) { const block = section('Who to see'); const box = el('div', 'referral'); add(box, 'strong', ref.specialist); if (ref.timeframe) add(box, 'div', ref.timeframe, 'note'); if (ref.reason) add(box, 'p', ref.reason); block.append(box); }
    if (result.next_steps?.length) list(section('What to do next'), result.next_steps);
    if (result.safety_notes?.length) list(section('Limitations to keep in mind'), result.safety_notes);
    const details = el('details', 'analysis-details'); add(details, 'summary', 'References & questions for your doctor');
    if (result.reference_comparison) add(details, 'p', result.reference_comparison);
    if (result.references_used?.length) list(details, result.references_used.map((c) => (c.label === 'normal' ? 'Normal case' : 'Abnormal case') + (c.diagnosis ? ': ' + c.diagnosis : '') + ' (' + c.source + ')'));
    if (result.questions_for_doctor?.length) { add(details, 'h4', 'Questions to bring to your appointment'); list(details, result.questions_for_doctor); }
    container.append(details);

    // Care near you
    const care = section('Care near you');
    if (chatZip) { add(care, 'p', 'Nearby places for ZIP ' + chatZip + ' are listed below in this conversation.', 'note'); const b = add(care, 'button', '📍 Find care near ' + chatZip + ' again', 'btn small'); b.type = 'button'; b.onclick = () => saveZip(chatZip); }
    else {
      add(care, 'p', 'Enter your ZIP code and MedPeek will list the 3 closest places that can help' + (ref && ref.specialist ? ' (' + ref.specialist.split(' (')[0].toLowerCase() + ', urgent care and emergency rooms).' : '.'), 'note');
      const row = el('div', 'row'); const zin = el('input'); zin.placeholder = 'ZIP code'; zin.maxLength = 5; zin.inputMode = 'numeric'; zin.style.maxWidth = '160px';
      const go = add(row, 'button', 'Find care', 'btn primary small'); go.type = 'button'; row.prepend(zin); care.append(row);
      go.onclick = () => saveZip(zin.value); zin.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); saveZip(zin.value); } };
    }

    add(container, 'div', result.ai_notice || 'MedPeek is AI and can make mistakes.', 'ai-notice');
    add(container, 'p', result.disclaimer || 'Educational information, not a diagnosis.', 'analysis-disclaimer');
    const ask = el('div', 'pdf-ask'); add(ask, 'p', 'Would you like a PDF copy of this MedPeek report, with the date and your scan details, to bring to your doctor?');
    const row = el('div', 'row'); row.style.justifyContent = 'center';
    const yes = add(row, 'button', 'Yes, download the PDF', 'btn primary small'); yes.type = 'button';
    const no = add(row, 'button', 'No, thanks', 'btn small'); no.type = 'button'; ask.append(row); container.append(ask);
    no.onclick = () => ask.replaceChildren(el('p', 'note', 'No problem. You can always report a mistake with "Report a problem" at the top.'));
    yes.onclick = async () => {
      yes.disabled = true; yes.textContent = 'Preparing PDF…';
      const status = ask.querySelector('.note') || add(ask, 'p', '', 'note');
      try {
        const r = await fetch('/api/report-pdf', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': MP.csrf }, body: JSON.stringify(result), signal: AbortSignal.timeout(75000) });
        if (!r.ok) { let msg = 'The PDF could not be made.'; try { msg = (await r.json()).error || msg; } catch (e) {} throw Error(msg); }
        const blob = await r.blob(); const url = URL.createObjectURL(blob); const a = el('a'); a.href = url; a.download = 'MedPeek-report-' + String(result.id || 'report').replace(/[^A-Za-z0-9_-]/g, '').slice(0, 40) + '.pdf'; document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 60000);
        status.textContent = 'Download started. Check your browser downloads.';
      } catch (e) { status.textContent = e.name === 'TimeoutError' ? 'The download timed out. Please try again.' : e.message; }
      finally { yes.disabled = false; yes.textContent = 'Download PDF'; }
    };
  }

  function renderCare(container, care, key) {
    container.replaceChildren(); container.classList.add('care-card', 'rich');
    const add = (parent, tag, text, cls) => { const n = el(tag, cls, text); parent.append(n); return n; };
    add(container, 'h3', 'Care near ' + (care.city ? care.city + ', ' + care.state + ' ' : '') + care.zip);
    const mapEl = el('div', 'map'); mapEl.id = 'map-' + Math.random().toString(36).slice(2); container.append(mapEl);
    const pins = [];
    const pinClass = { emergency: 'er', urgent_care: 'uc' };
    (care.groups || []).forEach((g) => {
      const box = el('div', 'care-group'); add(box, 'h4', g.title); if (g.why) add(box, 'p', g.why, 'why');
      if (!g.places || !g.places.length) add(box, 'p', 'Nothing found within range. Try a nearby ZIP code or call 211 for local help.', 'note');
      (g.places || []).forEach((p, i) => {
        const row = el('div', 'place'); const n = pins.length + 1; pins.push({ p, n, kind: g.kind });
        add(row, 'div', String(n), 'pin ' + (pinClass[g.kind] || ''));
        const body = el('div'); const name = add(body, 'strong', p.name);
        if (p.open_24_7) name.append(Object.assign(el('span', 'badge open', 'open 24/7')));
        add(body, 'div', [p.distance_miles != null ? p.distance_miles + ' mi' : '', p.address + ', ' + p.city + ' ' + p.zip].filter(Boolean).join(' · '), 'sub');
        if (p.details) add(body, 'div', p.details, 'sub');
        row.append(body);
        const actions = el('div', 'actions');
        if (p.phone) { const a = add(actions, 'a', '📞 ' + p.phone); a.href = 'tel:' + p.phone.replace(/[^\d+]/g, ''); }
        const d = add(actions, 'a', 'Directions', 'dir'); d.href = p.directions_url; d.target = '_blank'; d.rel = 'noopener';
        row.append(actions); box.append(row);
      });
      container.append(box);
    });
    if (care.note) add(container, 'p', care.note, 'note');
    requestAnimationFrame(() => drawMap(mapEl, care, pins));
  }

  function drawMap(mapEl, care, pins) {
    if (!window.L || !mapEl.isConnected) return;
    const map = L.map(mapEl, { scrollWheelZoom: false }).setView([care.lat, care.lon], 12);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 18, referrerPolicy: 'strict-origin-when-cross-origin', attribution: '© OpenStreetMap contributors' }).addTo(map);
    const you = L.circleMarker([care.lat, care.lon], { radius: 8, color: '#137c70', fillColor: '#137c70', fillOpacity: .9 }).addTo(map).bindPopup('Your ZIP code ' + care.zip);
    const bounds = [[care.lat, care.lon]];
    const colors = { emergency: '#e5484d', urgent_care: '#d98c1f', specialist: '#137c70' };
    pins.forEach(({ p, n, kind }) => {
      const icon = L.divIcon({ className: '', html: '<div style="width:26px;height:26px;border-radius:50%;background:' + (colors[kind] || '#137c70') + ';color:#fff;font:800 12px Manrope,sans-serif;display:flex;align-items:center;justify-content:center;border:2px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,.35)">' + n + '</div>', iconSize: [26, 26], iconAnchor: [13, 13] });
      L.marker([p.lat, p.lon], { icon }).addTo(map).bindPopup('<strong>' + p.name + '</strong><br>' + p.address + '<br>' + (p.phone || '') + '<br><a href="' + p.directions_url + '" target="_blank" rel="noopener">Directions</a>');
      bounds.push([p.lat, p.lon]);
    });
    if (bounds.length > 1) map.fitBounds(bounds, { padding: [28, 28], maxZoom: 14 });
    setTimeout(() => map.invalidateSize(), 300);
  }

  // ---------- ZIP / care finder
  async function saveZip(value) {
    const z = String(value || '').replace(/\D/g, '').slice(0, 5);
    if (z.length !== 5) { $('#zip-status').textContent = 'Please enter a 5-digit US ZIP code.'; openSidebar(); $('#zip-input').focus(); return; }
    if (busy) return;
    busy = true; controls.forEach((c) => c && (c.disabled = true));
    $('#zip-status').textContent = 'Finding the closest places…';
    try {
      await ensureChat(); $('#welcome')?.remove();
      const data = await MP.api('/api/chats/' + encodeURIComponent(chatId) + '/zip', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ zip: z }) });
      setZipUI(data.zip); $('#zip-status').textContent = 'Showing care near ' + data.city + ', ' + data.state + ' ' + data.zip + '.';
      signature = ''; await load(); scrollDown(); closeSidebar();
    } catch (e) { $('#zip-status').textContent = e.message; }
    finally { busy = false; controls.forEach((c) => c && (c.disabled = false)); }
  }
  $('#zip-save').onclick = () => saveZip($('#zip-input').value);
  $('#zip-input').onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); saveZip($('#zip-input').value); } };

  // ---------- sidebar (mobile)
  function openSidebar() { $('#sidebar').classList.add('open'); }
  function closeSidebar() { $('#sidebar').classList.remove('open'); }
  $('#sidebar-toggle').onclick = () => $('#sidebar').classList.toggle('open');

  // ---------- composer
  document.querySelectorAll('[data-prompt]').forEach((b) => b.onclick = () => { input.value = b.dataset.prompt; input.focus(); input.dispatchEvent(new Event('input')); });
  $('#attach').onclick = () => files.click();
  files.onchange = () => {
    const f = files.files[0];
    if (!f) { $('#attach-preview').hidden = true; return; }
    $('#attach-name').textContent = f.name + ' · ' + (f.size / 1e6).toFixed(1) + ' MB';
    const thumb = $('#attach-thumb');
    if (f.type.startsWith('image/')) { thumb.src = URL.createObjectURL(f); thumb.style.visibility = 'visible'; } else { thumb.removeAttribute('src'); thumb.style.visibility = 'hidden'; }
    $('#attach-preview').hidden = false; input.focus();
  };
  $('#attach-remove').onclick = () => { files.value = ''; $('#attach-preview').hidden = true; };
  input.oninput = () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 160) + 'px'; };
  input.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); } };
  $('#new-chat').onclick = () => { if (!busy) { chatId = null; history.replaceState(null, '', '/chat?new=1'); messages.replaceChildren(); signature = ''; $('#chat-title').textContent = 'New conversation'; closeSidebar(); location.reload(); } };

  form.onsubmit = async (e) => {
    e.preventDefault();
    if (busy) return;
    error.textContent = '';
    const file = files.files[0], text = input.value.trim();
    if (!file && !text) return;
    if (file && file.size > 25 * 1024 * 1024) { error.textContent = 'The image must be smaller than 25 MB.'; return; }
    busy = true; controls.forEach((c) => c && (c.disabled = true));
    let ok = false;
    try {
      await ensureChat(); $('#welcome')?.remove();
      bubble('user', [text, file ? 'Attached: ' + file.name : ''].filter(Boolean).join('\n'));
      runStart = Date.now();
      const pending = bubble('bot', ''); pending.el.classList.add('pending');
      pending.content.replaceChildren(thinkingNode(file ? 'Sending your image…' : 'MedPeek is thinking about your message.'));
      scrollDown();
      const data = new FormData();
      data.append('chat_id', chatId); data.append('user_message', text);
      data.append('body_part', $('#body-part').value); data.append('modality', $('#modality').value);
      if (zipCode) data.append('zip', zipCode);
      if (file) data.append('xray_image', file);
      await MP.api('/api/submit', { method: 'POST', body: data, timeout: 120000 });
      ok = true;
      input.value = ''; input.style.height = 'auto'; files.value = ''; $('#attach-preview').hidden = true;
    } catch (err) { showError(err); }
    finally {
      busy = false; controls.forEach((c) => c && (c.disabled = false));
      signature = ''; await load().catch(showError);
      if (ok) { input.focus(); scrollDown(); }
    }
  };

  setInterval(() => { if (chatId && !busy && !document.hidden) load().catch(showError); }, 4000);
  (async () => {
    if (!chatId && !new URL(location.href).searchParams.has('new')) {
      const data = await MP.api('/api/chats');
      if (data.chats.length) { chatId = data.chats[0].id; history.replaceState(null, '', '/chat?chat=' + chatId); }
    }
    await list(); await load();
  })().catch(showError);
  window.addEventListener('beforeunload', (e) => { if (busy) { e.preventDefault(); e.returnValue = ''; } });
})();
