// MedPeek front end: theme, login tabs, chat history, uploads, live replies, Gemma status.
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const csrf = () => $('meta[name="csrf-token"]').content;

function toast(text, ok = false) {
    const area = $('#toast-area');
    if (!area) return;
    const box = document.createElement('div');
    box.className = 'toast-msg' + (ok ? ' ok' : '');
    box.textContent = text;
    area.appendChild(box);
    setTimeout(() => box.remove(), 5000);
}

document.addEventListener('DOMContentLoaded', () => {
    setUpTheme();
    setUpConfirms();
    setUpLogin();
    setUpSidebar();
    setUpChat();
    setUpStatus();
});

// ---------- light / dark ----------
function setUpTheme() {
    const button = $('#theme-toggle');
    if (!button) return;
    button.addEventListener('click', () => {
        const next = document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-bs-theme', next);
        localStorage.setItem('theme', next);
    });
}

// ---------- "are you sure?" on delete ----------
function setUpConfirms() {
    $$('form[data-confirm]').forEach(form => {
        form.addEventListener('submit', event => {
            if (!confirm(form.dataset.confirm)) event.preventDefault();
        });
    });
}

// ---------- login / register page ----------
function setUpLogin() {
    const tabs = $('.tabs');
    if (!tabs) return;

    const show = name => {
        $$('.tab', tabs).forEach(t => t.classList.toggle('active', t.dataset.tab === name));
        $$('.auth-form').forEach(f => f.classList.toggle('active', f.id === `form-${name}`));
        tabs.classList.toggle('register', name === 'register');
        const first = $(`#form-${name} input:not([type=hidden])`);
        if (first) first.focus();
    };
    $$('.tab', tabs).forEach(tab => tab.addEventListener('click', () => show(tab.dataset.tab)));
    show($('.tab.active', tabs).dataset.tab);

    $$('.show-password').forEach(button => {
        button.addEventListener('click', () => {
            const input = button.previousElementSibling;
            const hidden = input.type === 'password';
            input.type = hidden ? 'text' : 'password';
            button.textContent = hidden ? 'Hide' : 'Show';
        });
    });

    const password = $('#form-register input[name=password]');
    const bar = $('.strength-bar');
    if (password && bar) {
        password.addEventListener('input', () => {
            const p = password.value;
            const score = [p.length >= 8, p.length >= 12, /[A-Z]/.test(p) && /[a-z]/.test(p), /\d/.test(p), /[^A-Za-z0-9]/.test(p)]
                .filter(Boolean).length;
            bar.style.width = `${score * 20}%`;
            bar.style.background = score <= 2 ? 'var(--danger)' : score === 3 ? 'var(--warn)' : 'var(--ok)';
        });
    }

    const register = $('#form-register');
    register.addEventListener('submit', event => {
        if (register.password.value !== register.confirm.value) {
            event.preventDefault();
            toast("The two passwords don't match.");
        }
    });
}

// ---------- chat history sidebar ----------
function setUpSidebar() {
    const sidebar = $('#sidebar');
    if (!sidebar) return;
    $$('[data-sidebar-toggle]').forEach(el => el.addEventListener('click', () => sidebar.classList.toggle('open')));

    // "2 X-rays · 5 min ago"
    $$('.chat-item-meta[data-utc]').forEach(el => {
        const when = new Date(el.dataset.utc.replace(' ', 'T') + 'Z');
        el.textContent += ` · ${timeAgo(when)}`;
    });

    const search = $('#chat-search');
    const items = $$('.chat-item');
    const empty = $('.no-results');
    search.addEventListener('input', () => {
        const q = search.value.trim().toLowerCase();
        let shown = 0;
        items.forEach(item => {
            const match = item.dataset.title.includes(q);
            item.hidden = !match;
            shown += match;
        });
        empty.hidden = shown > 0;
    });

    const current = $('#current-chat-item');
    if (current) current.scrollIntoView({ block: 'nearest' });
}

function timeAgo(date) {
    const seconds = Math.max(0, (Date.now() - date) / 1000);
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
    if (seconds < 7 * 86400) return `${Math.floor(seconds / 86400)} d ago`;
    return date.toLocaleDateString();
}

// ---------- the chat ----------
function setUpChat() {
    const form = $('#composer');
    if (!form) return;

    const chatWindow = $('#chat-window');
    const messages = $('#messages');
    const input = $('#message-input');
    const fileInput = $('#xray-input');
    const previews = $('#previews');
    const sendBtn = $('#send-btn');
    const overlay = $('#drop-overlay');
    const maxImages = Number(form.dataset.maxImages);
    const maxPerSend = Number(form.dataset.maxPerSend);
    let imageCount = Number(form.dataset.imageCount);
    let pending = [];
    let busy = false;

    const scrollDown = () => { chatWindow.scrollTop = chatWindow.scrollHeight; };
    scrollDown();
    $('#counter-fill').style.width = `${$('#counter-fill').dataset.percent}%`;

    const autosize = () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 180) + 'px';
    };
    input.addEventListener('input', autosize);
    input.addEventListener('keydown', event => {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            form.requestSubmit();
        }
    });
    input.focus();

    $$('.suggestion').forEach(button => button.addEventListener('click', () => {
        input.value = button.dataset.text;
        autosize();
        input.focus();
    }));

    // attaching files
    $('#attach-btn').addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
        addFiles(fileInput.files);
        fileInput.value = '';
    });

    function addFiles(files) {
        for (const file of files) {
            if (!['image/png', 'image/jpeg'].includes(file.type)) {
                toast(`${file.name} isn't a PNG or JPEG.`);
                continue;
            }
            if (pending.length >= maxPerSend) {
                toast(`You can send up to ${maxPerSend} X-rays at a time.`);
                break;
            }
            if (imageCount + pending.length >= maxImages) {
                toast(`This chat has room for ${maxImages - imageCount} more X-ray(s). Start a new chat for more.`);
                break;
            }
            pending.push({ file, url: URL.createObjectURL(file) });
        }
        renderPreviews();
    }

    function renderPreviews() {
        previews.innerHTML = '';
        pending.forEach((item, index) => {
            const box = document.createElement('div');
            box.className = 'preview';
            box.title = item.file.name;
            box.innerHTML = '<img alt=""><button type="button" aria-label="Remove">✕</button>';
            $('img', box).src = item.url;
            $('button', box).addEventListener('click', () => {
                URL.revokeObjectURL(item.url);
                pending.splice(index, 1);
                renderPreviews();
            });
            previews.appendChild(box);
        });
    }

    // drag and drop anywhere on the page
    let dragDepth = 0;
    const hasFiles = event => event.dataTransfer && Array.from(event.dataTransfer.types).includes('Files');
    window.addEventListener('dragenter', event => {
        if (!hasFiles(event)) return;
        dragDepth++;
        overlay.classList.add('show');
    });
    window.addEventListener('dragleave', () => {
        dragDepth = Math.max(0, dragDepth - 1);
        if (!dragDepth) overlay.classList.remove('show');
    });
    window.addEventListener('dragover', event => { if (hasFiles(event)) event.preventDefault(); });
    window.addEventListener('drop', event => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        dragDepth = 0;
        overlay.classList.remove('show');
        addFiles(event.dataTransfer.files);
    });

    // paste an image straight from the clipboard
    input.addEventListener('paste', event => {
        const files = Array.from(event.clipboardData.files || []);
        if (files.length) addFiles(files);
    });

    // sending
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (busy) return;
        const text = input.value.trim();
        const items = pending;
        if (!text && !items.length) return;

        busy = true;
        sendBtn.disabled = true;
        input.value = '';
        autosize();
        pending = [];
        renderPreviews();

        // One request per X-ray so each answer appears as soon as it's ready.
        // The typed note goes with the first X-ray.
        const jobs = items.length
            ? items.map((item, i) => ({ file: item.file, url: item.url, text: i === 0 ? text : '' }))
            : [{ file: null, url: null, text }];
        for (const [index, job] of jobs.entries()) {
            const label = job.file
                ? `Gemma is reading ${jobs.length > 1 ? `X-ray ${index + 1} of ${jobs.length}` : 'your X-ray'}`
                : 'Gemma is thinking';
            const ok = await send(job, label);
            if (job.url) URL.revokeObjectURL(job.url);
            if (!ok) {
                jobs.slice(index + 1).forEach(rest => rest.url && URL.revokeObjectURL(rest.url));
                break;
            }
        }

        busy = false;
        sendBtn.disabled = false;
        input.focus();
    });

    async function send(job, label) {
        const after = lastMessageId();
        const echo = showOwnMessage(job);  // show what was sent right away
        const typing = showTyping(label);
        const data = new FormData();
        data.append('chat_id', form.dataset.chatId);
        data.append('text', job.text);
        data.append('after', after);
        if (job.file) data.append('xray', job.file);

        try {
            const response = await fetch('/api/message', {
                method: 'POST',
                body: data,
                headers: { 'X-CSRF-Token': csrf() },
            });
            const reply = await response.json().catch(() => ({ error: `Server error (${response.status}).` }));
            typing.remove();
            echo.remove();
            if (!response.ok || reply.error) {
                toast(reply.error || 'Something went wrong.');
                if (response.status === 401) setTimeout(() => location.assign('/'), 1500);
                return false;
            }
            messages.insertAdjacentHTML('beforeend', reply.html);
            updateCounter(reply.image_count);
            if (reply.title) updateTitle(reply.title);
            scrollDown();
            return true;
        } catch (error) {
            typing.remove();
            echo.remove();
            toast("Couldn't reach MedPeek. Is the Flask server still running?");
            return false;
        }
    }

    function showOwnMessage(job) {
        const row = document.createElement('div');
        row.className = 'msg msg-user';
        const body = document.createElement('div');
        body.className = 'msg-body';
        if (job.file) {
            const thumb = document.createElement('div');
            thumb.className = 'xray-thumb';
            thumb.innerHTML = '<img alt=""><span class="thumb-name"></span>';
            $('img', thumb).src = job.url;
            $('.thumb-name', thumb).textContent = job.file.name;
            body.appendChild(thumb);
        }
        if (job.text) {
            const bubble = document.createElement('div');
            bubble.className = 'bubble bubble-user';
            bubble.textContent = job.text;
            body.appendChild(bubble);
        }
        row.appendChild(body);
        messages.appendChild(row);
        return row;
    }

    function showTyping(label) {
        const row = document.createElement('div');
        row.className = 'msg msg-bot';
        row.innerHTML = `
            <div class="avatar" aria-hidden="true">${$('.avatar') ? $('.avatar').innerHTML : ''}</div>
            <div class="typing glass"><span class="typing-dots"><i></i><i></i><i></i></span>
            <span class="typing-label"></span></div>`;
        const labelEl = $('.typing-label', row);
        const started = Date.now();
        const tick = () => {
            const seconds = Math.floor((Date.now() - started) / 1000);
            labelEl.textContent = seconds >= 3 ? `${label}... ${seconds}s` : `${label}...`;
        };
        tick();
        const timer = setInterval(tick, 1000);
        row.remove = (original => () => { clearInterval(timer); original.call(row); })(row.remove);
        messages.appendChild(row);
        scrollDown();
        return row;
    }

    function lastMessageId() {
        const all = $$('.msg[data-id]', messages);
        return all.length ? all[all.length - 1].dataset.id : 0;
    }

    function updateCounter(count) {
        imageCount = count;
        form.dataset.imageCount = count;
        $('#counter-text').textContent = `${count}/${maxImages}`;
        $('#counter-fill').style.width = `${Math.round(count / maxImages * 100)}%`;
        const meta = $('#current-chat-item .chat-item-meta');
        if (meta) meta.textContent = `${count} X-ray${count === 1 ? '' : 's'} · just now`;
    }

    function updateTitle(title) {
        $('#chat-title').textContent = title;
        document.title = `${title} · MedPeek`;
        const item = $('#current-chat-item');
        if (item) {
            $('.chat-item-title', item).textContent = title;
            item.dataset.title = title.toLowerCase();
        }
    }
}

// ---------- Gemma status light ----------
function setUpStatus() {
    const pill = $('#model-status');
    if (!pill) return;
    const text = $('.status-text', pill);
    let timer = null;

    async function check() {
        try {
            const status = await (await fetch('/api/status')).json();
            pill.className = `status-pill status-${status.state}`;
            text.textContent = status.state === 'error' ? `Gemma offline: ${status.message} (click to retry)` : status.message;
            pill.title = status.message;
            clearTimeout(timer);
            timer = setTimeout(check, status.state === 'ready' ? 30000 : 2000);
        } catch {
            pill.className = 'status-pill status-error';
            text.textContent = "Can't reach MedPeek";
            timer = setTimeout(check, 5000);
        }
    }

    pill.addEventListener('click', async () => {
        if (!pill.classList.contains('status-error')) return;
        await fetch('/api/status/retry', { method: 'POST', headers: { 'X-CSRF-Token': csrf() } });
        toast('Trying to start Gemma again...', true);
        check();
    });
    check();
}