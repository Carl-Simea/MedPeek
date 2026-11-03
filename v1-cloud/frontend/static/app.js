// Shared across pages: theme, "Report a problem" dialog, CSRF helper.
window.MP = window.MP || {};
MP.csrf = document.querySelector('meta[name=csrf-token]')?.content || '';
MP.api = async function (path, options = {}) {
  const headers = Object.assign({ 'X-CSRF-Token': MP.csrf }, options.headers || {});
  const r = await fetch(path, Object.assign({}, options, { headers, signal: options.signal || AbortSignal.timeout(options.timeout || 95000) }));
  let data = {};
  try { data = await r.json(); } catch (e) { throw Error('The request could not be completed.'); }
  if (!r.ok) throw Error(data.error || 'Something went wrong.');
  return data;
};

(function theme() {
  const root = document.documentElement;
  const btn = document.querySelector('#theme-toggle');
  if (!btn) return;
  btn.addEventListener('click', () => {
    root.classList.toggle('dark');
    try { localStorage.setItem('medpeek-theme', root.classList.contains('dark') ? 'dark' : 'light'); } catch (e) {}
  });
})();

(function reportProblem() {
  const modal = document.querySelector('#report-modal'), open = document.querySelector('#report-open');
  if (!modal || !open) return;
  const status = document.querySelector('#report-status'), send = document.querySelector('#report-send');
  const close = () => { modal.hidden = true; };
  open.onclick = () => { status.textContent = ''; modal.hidden = false; document.querySelector('#report-message').focus(); };
  document.querySelector('#report-cancel').onclick = close;
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !modal.hidden) close(); });
  document.querySelector('#report-form').onsubmit = async (e) => {
    e.preventDefault();
    send.disabled = true; status.textContent = 'Sending…';
    const reportId = new URL(location.href).searchParams.get('chat') || '';
    try {
      const data = await MP.api('/api/report-issue', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind: document.querySelector('#report-kind').value, message: document.querySelector('#report-message').value,
          contact: document.querySelector('#report-contact').value, report_id: reportId }),
      });
      status.textContent = 'Thanks! Your ticket is ' + data.ticket + '.';
      document.querySelector('#report-message').value = '';
      setTimeout(close, 1800);
    } catch (err) {
      status.textContent = err.message === 'Please sign in to continue.' ? 'Please sign in first, then report the problem.' : err.message;
    } finally { send.disabled = false; }
  };
})();
