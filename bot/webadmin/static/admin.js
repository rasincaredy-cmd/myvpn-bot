// Moschata VPN Admin SPA Logic
let currentTab = 'overview';
let usersOffset = 0;
let userSearchTimeout = null;

document.addEventListener('DOMContentLoaded', () => {
  initNav();
  loadOverview();
});

function initNav() {
  document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', () => {
      const target = item.dataset.tab;
      switchTab(target);
    });
  });
}

function switchTab(tabId) {
  currentTab = tabId;
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === tabId);
  });
  document.querySelectorAll('.tab-pane').forEach(el => {
    el.classList.toggle('active', el.id === `tab-${tabId}`);
  });

  const titles = {
    overview: 'Сводка сервиса',
    servers: 'Серверы и ноды',
    users: 'Пользователи',
    tariffs: 'Тарифы и настройки',
    broadcast: 'Рассылка сообщений',
    audit: 'Журнал аудита'
  };
  document.getElementById('current-page-title').innerText = titles[tabId] || tabId;

  if (tabId === 'overview') loadOverview();
  else if (tabId === 'servers') loadServers();
  else if (tabId === 'users') loadUsers();
  else if (tabId === 'tariffs') loadTariffs();
  else if (tabId === 'audit') loadAudit();
}

async function api(path, options = {}) {
  try {
    const res = await fetch(`/admin/api${path}`, {
      headers: {
        'Content-Type': 'application/json',
        ...options.headers
      },
      ...options
    });
    if (res.status === 401) {
      alert('Сессия истекла или требуется повторный вход через команду /admin_web в Telegram!');
      window.location.reload();
      return null;
    }
    return await res.json();
  } catch (e) {
    console.error('API Error:', e);
    return null;
  }
}

// ── Overview ──────────────────────────────────────────────
async function loadOverview() {
  const data = await api('/overview');
  if (!data) return;

  document.getElementById('stat-total-users').innerText = data.users.total;
  document.getElementById('stat-paid-users').innerText = data.users.active_paid;
  document.getElementById('stat-trial-users').innerText = data.users.active_trial;
  document.getElementById('stat-expired-users').innerText = data.users.expired;
  document.getElementById('stat-revenue').innerText = data.finance.formatted_revenue;
  document.getElementById('stat-traffic').innerText = `${data.infrastructure.total_traffic_gb} ГБ`;
  document.getElementById('stat-peers').innerText = `${data.infrastructure.active_peers} пиров`;
  document.getElementById('stat-bypass').innerText = `${data.infrastructure.active_bypass} обходов`;
}

// ── Servers ───────────────────────────────────────────────
async function loadServers() {
  const data = await api('/servers');
  if (!data) return;

  const tbody = document.getElementById('servers-table-body');
  tbody.innerHTML = '';

  data.servers.forEach(s => {
    const tr = document.createElement('tr');
    const isReady = s.status === 'ready';
    const statusBadge = isReady
      ? `<span class="badge badge-success">● READY</span>`
      : `<span class="badge badge-danger">● ${s.status}</span>`;

    const privBadge = s.is_private
      ? `<span class="badge badge-warning">🔒 Приватный</span>`
      : `<span class="badge badge-subtle">Публичный</span>`;

    const wdttBadge = s.wdtt_enabled
      ? `<span class="badge badge-success">qWDTT (${s.active_bypass}/${s.wdtt_max_accesses || '∞'})</span>`
      : `<span class="badge badge-subtle">Выключен</span>`;

    tr.innerHTML = `
      <td class="mono"><b>#${s.id}</b></td>
      <td><b>${s.location || s.name}</b><br><small class="mono text-muted">${s.host}:${s.ssh_port}</small></td>
      <td>${statusBadge}</td>
      <td class="mono">AWG :${s.wg_port}<br><small>${s.active_peers} / ${s.max_peers || '∞'}</small></td>
      <td>${wdttBadge}</td>
      <td>${privBadge}</td>
      <td>
        <button class="btn btn-secondary btn-sm" onclick="openServerModal(${JSON.stringify(s).replace(/"/g, '&quot;')})">⚙️ Настроить</button>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

function openServerModal(server) {
  document.getElementById('edit-server-id').value = server.id;
  document.getElementById('edit-server-name').value = server.name;
  document.getElementById('edit-server-location').value = server.location;
  document.getElementById('edit-server-dns').value = server.dns;
  document.getElementById('edit-server-max-peers').value = server.max_peers || '';
  document.getElementById('edit-server-max-bypass').value = server.wdtt_max_accesses || '';
  document.getElementById('edit-server-wdtt').checked = server.wdtt_enabled;
  document.getElementById('edit-server-private').checked = server.is_private;

  openModal('modal-server');
}

async function saveServerSettings() {
  const id = document.getElementById('edit-server-id').value;
  const body = {
    name: document.getElementById('edit-server-name').value,
    location: document.getElementById('edit-server-location').value,
    dns: document.getElementById('edit-server-dns').value,
    max_peers: document.getElementById('edit-server-max-peers').value,
    wdtt_max_accesses: document.getElementById('edit-server-max-bypass').value,
    wdtt_enabled: document.getElementById('edit-server-wdtt').checked,
    is_private: document.getElementById('edit-server-private').checked,
  };
  const res = await api(`/servers/${id}`, { method: 'POST', body: JSON.stringify(body) });
  if (res && res.ok) {
    closeModal('modal-server');
    loadServers();
  }
}

// ── Users ─────────────────────────────────────────────────
async function loadUsers(resetOffset = false) {
  if (resetOffset) usersOffset = 0;
  const q = document.getElementById('user-search-input').value.trim();
  const filter = document.getElementById('user-filter-select').value;

  const data = await api(`/users?q=${encodeURIComponent(q)}&filter=${filter}&offset=${usersOffset}&limit=25`);
  if (!data) return;

  const tbody = document.getElementById('users-table-body');
  tbody.innerHTML = '';

  data.items.forEach(u => {
    const tr = document.createElement('tr');

    let statusHtml = '';
    if (u.is_blocked) statusHtml = '<span class="badge badge-danger">Блок</span>';
    else if (u.is_vip) statusHtml = '<span class="badge badge-warning">⭐ VIP</span>';
    else if (u.is_trial) statusHtml = '<span class="badge badge-subtle">Триал</span>';
    else if (u.is_expired) statusHtml = '<span class="badge badge-danger">Истёк</span>';
    else statusHtml = '<span class="badge badge-success">Активен</span>';

    tr.innerHTML = `
      <td class="mono">#${u.id}</td>
      <td>
        <b>${u.full_name || 'Без имени'}</b>
        ${u.username ? `<br><small class="text-muted">@${u.username}</small>` : ''}
        <br><small class="mono text-muted">TG: ${u.tg_id}</small>
      </td>
      <td>${statusHtml}</td>
      <td class="mono">${u.sub_expires_msk}</td>
      <td class="mono"><b>${u.balance_rub} ₽</b></td>
      <td class="mono">${u.traffic_used_mb} МБ</td>
      <td>
        <button class="btn btn-secondary btn-sm" onclick="openUserModal(${u.id})">Карточка</button>
      </td>
    `;
    tbody.appendChild(tr);
  });

  document.getElementById('users-pagination-info').innerText =
    `Показано ${data.items.length} из ${data.total} юзеров`;
}

function onUserSearchInput() {
  clearTimeout(userSearchTimeout);
  userSearchTimeout = setTimeout(() => {
    loadUsers(true);
  }, 300);
}

async function openUserModal(userId) {
  const data = await api(`/users/${userId}`);
  if (!data) return;

  document.getElementById('user-card-id').value = data.id;
  document.getElementById('user-card-title').innerText = `Пользователь #${data.id} — ${data.full_name} (@${data.username || 'нет'})`;
  document.getElementById('user-card-tg-id').innerText = data.tg_id;
  document.getElementById('user-card-balance').innerText = `${data.balance_rub} ₽`;
  document.getElementById('user-card-expires').innerText = data.sub_expires_msk;
  document.getElementById('user-card-traffic').innerText = `${data.traffic_used_mb} МБ / ${data.traffic_limit_mb ? data.traffic_limit_mb + ' МБ' : 'Безлимит'}`;
  document.getElementById('user-card-limits').innerText = `📱 ${data.sub_max_devices} устр. / 🛡 ${data.sub_max_bypass} обход.`;

  document.getElementById('user-card-sub-url').value = `${window.location.origin}${data.sub_url}`;

  // Рендер пиров и устройств
  const devList = document.getElementById('user-devices-list');
  devList.innerHTML = '';
  data.devices.forEach(d => {
    const li = document.createElement('li');
    li.innerHTML = `📱 <b>${d.label}</b> (${d.status}) — создан ${d.created_at}`;
    devList.appendChild(li);
  });

  const peerList = document.getElementById('user-peers-list');
  peerList.innerHTML = '';
  data.peers.forEach(p => {
    const li = document.createElement('li');
    li.innerHTML = `🔑 <b>${p.label}</b> | IP: ${p.ip} | Трафик: ${p.traffic_used_mb} МБ (${p.status})`;
    peerList.appendChild(li);
  });

  const bypassList = document.getElementById('user-bypass-list');
  bypassList.innerHTML = '';
  data.bypass.forEach(b => {
    const li = document.createElement('li');
    li.innerHTML = `🛡 <b>${b.label}</b> (${b.platform || 'all'}) | Трафик: ${b.traffic_used_mb} МБ (${b.status})`;
    bypassList.appendChild(li);
  });

  openModal('modal-user');
}

async function quickUserAction(action) {
  const userId = document.getElementById('user-card-id').value;
  let body = { action };

  if (action === 'adjust_balance') {
    const amount = prompt('Введите сумму корректировки баланса в ₽ (например, 100 или -50):');
    if (!amount) return;
    body.amount_rub = parseFloat(amount);
  }

  const res = await api(`/users/${userId}/actions`, { method: 'POST', body: JSON.stringify(body) });
  if (res && res.ok) {
    alert('Действие успешно выполнено!');
    openUserModal(userId);
    loadUsers();
  }
}

async function quickExtendSub(days) {
  const userId = document.getElementById('user-card-id').value;
  const now = new Date();
  const future = new Date(now.getTime() + days * 24 * 60 * 60 * 1000);

  const res = await api(`/users/${userId}/subscription`, {
    method: 'POST',
    body: JSON.stringify({
      expires_at: future.toISOString(),
      reset_traffic: true,
      mark_paid: true
    })
  });
  if (res && res.ok) {
    alert(`Подписка успешно продлена на ${days} дней!`);
    openUserModal(userId);
    loadUsers();
  }
}

async function wipeUserConfirm() {
  const userId = document.getElementById('user-card-id').value;
  if (!confirm(`ВНИМАНИЕ: Вы действительно хотите ПОЛНОСТЬЮ стереть пользователя #${userId}? Все его конфиги будут отозваны на нодах, а аккаунт удалён.`)) {
    return;
  }
  const res = await api(`/users/${userId}/wipe`, { method: 'POST' });
  if (res && res.ok) {
    alert('Пользователь успешно стерт из базы данных.');
    closeModal('modal-user');
    loadUsers();
  }
}

// ── Tariffs ───────────────────────────────────────────────
async function loadTariffs() {
  const data = await api('/tariffs');
  if (!data) return;

  document.getElementById('tariff-price-first').value = data.price_first_rub;
  document.getElementById('tariff-price-device').value = data.price_extra_device_rub;
  document.getElementById('tariff-price-bypass').value = data.price_extra_bypass_rub;
  document.getElementById('tariff-trial-days').value = data.trial_days;
  document.getElementById('tariff-trial-traffic').value = data.trial_traffic_gb;
  document.getElementById('tariff-referral').value = data.referral_percent;
}

// ── Broadcast ─────────────────────────────────────────────
async function sendBroadcast() {
  const text = document.getElementById('broadcast-text').value.trim();
  const target = document.getElementById('broadcast-target').value;
  if (!text) {
    alert('Введите текст сообщения');
    return;
  }
  if (!confirm(`Запустить рассылку в Telegram для аудитории "${target}"?`)) return;

  const res = await api('/broadcast', {
    method: 'POST',
    body: JSON.stringify({ text, target })
  });
  if (res && res.ok) {
    alert(`Рассылка запущена для ${res.target_count} пользователей!`);
    document.getElementById('broadcast-text').value = '';
  }
}

// ── Audit ─────────────────────────────────────────────────
async function loadAudit() {
  const data = await api('/audit?limit=50');
  if (!data) return;

  const tbody = document.getElementById('audit-table-body');
  tbody.innerHTML = '';
  data.items.forEach(log => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="mono">#${log.id}</td>
      <td class="mono">${log.created_at_msk}</td>
      <td><span class="badge badge-subtle mono">${log.action}</span></td>
      <td class="mono">${log.actor_tg_id || 'system'}</td>
      <td class="mono">${log.target_type || ''} #${log.target_id || ''}</td>
      <td><small>${log.details || ''}</small></td>
    `;
    tbody.appendChild(tr);
  });
}

// ── Modals & Helpers ──────────────────────────────────────
function openModal(id) {
  document.getElementById(id).classList.add('active');
}
function closeModal(id) {
  document.getElementById(id).classList.remove('active');
}
function copySubUrl() {
  const input = document.getElementById('user-card-sub-url');
  navigator.clipboard.writeText(input.value);
  alert('Ссылка на подписку скопирована!');
}
