window.AT = (() => {
  const escape = (value) => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const fetchJSON = async (url, options={}) => {
    const response = await fetch(url, {credentials:'same-origin', ...options, headers:{'Accept':'application/json', ...(options.headers || {})}});
    let body = null;
    try { body = await response.json(); } catch (_) { body = {}; }
    if (!response.ok) {
      const error = new Error(body.message || `Request failed (${response.status})`);
      error.status = response.status; error.body = body; throw error;
    }
    return body;
  };
  const age = (timestamp) => {
    if (!timestamp) return 'never heard';
    const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    if (seconds < 2) return 'just now';
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds/60)}m ago`;
    return `${Math.floor(seconds/3600)}h ago`;
  };
  const eta = (seconds) => {
    if (seconds === null || seconds === undefined) return '—';
    if (seconds < 45) return 'Due';
    return `${Math.max(1, Math.ceil(seconds/60))} min`;
  };
  const mode = value => value === 'rs485' ? 'RS-485' : value === 'uart_direct' ? 'UART direct' : value === 'espnow_direct' ? 'ESP-NOW direct' : 'No heartbeat';
  const toast = (message, kind='success') => {
    const node = document.createElement('div'); node.className = `toast ${kind}`;
    node.innerHTML = `<span></span><p>${escape(message)}</p>`;
    document.querySelector('#toast-region')?.append(node);
    setTimeout(() => node.remove(), 5000);
  };

  async function updateStatus() {
    const pill = document.querySelector('#connection-pill');
    if (!pill) return;
    try {
      const status = await fetchJSON('/api/status');
      pill.className = `connection-pill ${status.online ? 'online' : 'offline'}`;
      pill.querySelector('b').textContent = status.online ? `Live · ${age(status.lastMessageAtMs)}` : (status.lastMessageAtMs ? `Data stale · ${age(status.lastMessageAtMs)}` : 'Awaiting data');
    } catch (_) {
      pill.className = 'connection-pill offline'; pill.querySelector('b').textContent = 'Backend unreachable';
    }
  }
  document.addEventListener('DOMContentLoaded', () => {
    const sidebar = document.querySelector('#sidebar'), scrim = document.querySelector('#sidebar-scrim');
    const close = () => {sidebar?.classList.remove('open'); scrim?.classList.remove('show');};
    document.querySelector('#menu-button')?.addEventListener('click', () => {sidebar?.classList.add('open'); scrim?.classList.add('show');});
    scrim?.addEventListener('click', close);
    updateStatus(); setInterval(updateStatus, 5000);
  });
  return {escape, fetchJSON, age, eta, mode, toast};
})();
