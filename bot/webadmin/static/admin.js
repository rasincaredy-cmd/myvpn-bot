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
        <div style="display: flex; flex-wrap: wrap; gap: 4px;">
          <button class="btn btn-secondary btn-sm" onclick="openServerModal(${JSON.stringify(s).replace(/"/g, '&quot;')})">⚙️ Настройки</button>
          <button class="btn btn-secondary btn-sm" onclick="checkServerHarden(${s.id}, '${s.name}')">🛡 Защита</button>
          <button class="btn btn-secondary btn-sm" onclick="syncServerPeers(${s.id}, '${s.name}')">🧹 Пиры</button>
          ${s.wdtt_enabled ? `<button class="btn btn-secondary btn-sm" onclick="updateServerWdtt(${s.id}, '${s.name}')">⚡ qWDTT</button>` : ''}
        </div>
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
  if (!data.devices || data.devices.length === 0) {
    devList.innerHTML = '<div class="text-muted" style="font-size: 12px;">Нет добавленных устройств.</div>';
  } else {
    data.devices.forEach(d => {
      const card = document.createElement('div');
      card.className = 'item-card';
      const isActive = d.status === 'ACTIVE' || d.status === 'active';
      const statusBadge = isActive
        ? '<span class="badge badge-success">Активен</span>'
        : `<span class="badge badge-subtle">${d.status}</span>`;

      card.innerHTML = `
        <div class="item-info">
          <div>📱 <b>${d.label}</b> ${statusBadge}</div>
          <small class="text-muted">Создан: ${d.created_at}</small>
        </div>
        <div class="item-actions">
          <button class="btn btn-secondary btn-sm" onclick="sendDeviceConfig(${data.id}, ${d.id}, '${d.label}')" title="Отправить .conf файл пользователю в Telegram">📤 В TG</button>
          <button class="btn btn-secondary btn-sm" onclick="downloadDeviceConfig(${data.id}, ${d.id}, '${d.label}')" title="Скачать файл конфигурации">📥 .conf</button>
          <button class="btn btn-danger btn-sm" onclick="revokeDevice(${data.id}, ${d.id}, '${d.label}')" title="Удалить устройство">🗑</button>
        </div>
      `;
      devList.appendChild(card);
    });
  }

  const bypassList = document.getElementById('user-bypass-list');
  bypassList.innerHTML = '';
  if (!data.bypass || data.bypass.length === 0) {
    bypassList.innerHTML = '<div class="text-muted" style="font-size: 12px;">Нет резервных подключений.</div>';
  } else {
    data.bypass.forEach(b => {
      const card = document.createElement('div');
      card.className = 'item-card';
      const isActive = b.status === 'ACTIVE' || b.status === 'active';
      const statusBadge = isActive
        ? '<span class="badge badge-success">Активен</span>'
        : `<span class="badge badge-subtle">${b.status}</span>`;

      card.innerHTML = `
        <div class="item-info">
          <div>🛡 <b>${b.label}</b> (${b.platform || 'all'}) ${statusBadge}</div>
          <small class="text-muted">Трафик: ${b.traffic_used_mb} МБ</small>
        </div>
        <div class="item-actions">
          <button class="btn btn-danger btn-sm" onclick="revokeBypass(${data.id}, ${b.id}, '${b.label}')" title="Удалить подключение обхода">🗑 Удалить</button>
        </div>
      `;
      bypassList.appendChild(card);
    });
  }

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

  const res = await api(`/users/${userId}/subscription`, {
    method: 'POST',
    body: JSON.stringify({
      add_days: days,
      reset_traffic: true,
      mark_paid: true
    })
  });
  if (res && res.ok) {
    let msg = `Подписка успешно продлена на ${days} дней!`;
    if (res.revived && (res.revived.devices || res.revived.bypass)) {
      msg += ` Восстановлено: устройств ${res.revived.devices}, обходов ${res.revived.bypass}.`;
    }
    alert(msg);
    openUserModal(userId);
    loadUsers();
  }
}

async function quickSubAction(action) {
  const userId = document.getElementById('user-card-id').value;
  const labels = {
    gift_month: 'подарить 30 дней подписки',
    give_trial: 'выдать триал на 7 дней',
    set_perpetual: 'сделать подписку бессрочной',
    turn_off: 'отключить подписку и отозвать доступы',
    reset_traffic: 'сбросить счётчик трафика'
  };

  if (!confirm(`Подтвердите действие: ${labels[action] || action}?`)) return;

  const res = await api(`/users/${userId}/subscription`, {
    method: 'POST',
    body: JSON.stringify({ action })
  });

  if (res && res.ok) {
    let msg = 'Подписка успешно обновлена!';
    if (res.revived && (res.revived.devices || res.revived.bypass)) {
      msg += ` Восстановлено: устройств ${res.revived.devices}, обходов ${res.revived.bypass}.`;
    }
    alert(msg);
    openUserModal(userId);
    loadUsers();
  } else {
    alert(res && res.error ? res.error : 'Произошла ошибка при изменении подписки');
  }
}

async function quickSetLimits() {
  const userId = document.getElementById('user-card-id').value;
  const devStr = prompt('Максимум устройств (AmneziaWG, 1-10):', '2');
  if (devStr === null) return;
  const bypStr = prompt('Максимум обходов БС (qWDTT, 0-10):', '1');
  if (bypStr === null) return;

  const maxDev = parseInt(devStr, 10);
  const maxByp = parseInt(bypStr, 10);

  const res = await api(`/users/${userId}/subscription`, {
    method: 'POST',
    body: JSON.stringify({
      max_devices: isNaN(maxDev) ? null : maxDev,
      max_bypass: isNaN(maxByp) ? null : maxByp
    })
  });

  if (res && res.ok) {
    alert('Лимиты успешно обновлены!');
    openUserModal(userId);
    loadUsers();
  }
}

async function sendDeviceConfig(userId, deviceId, label) {
  if (!confirm(`Отправить .conf файл для «${label}» пользователю в Telegram?`)) return;
  const res = await api(`/users/${userId}/devices/${deviceId}/send_config`, {
    method: 'POST',
    body: JSON.stringify({ send_to_user: true })
  });
  if (res && res.ok) {
    if (res.sent_to_user) {
      alert(`✅ Конфиг «${res.filename}» успешно отправлен в Telegram юзеру!`);
    } else {
      alert(`⚠️ Не удалось отправить сообщение в Telegram. Конфиг сформирован: ${res.filename}`);
    }
  } else {
    alert(res && res.error ? res.error : 'Ошибка отправки конфига');
  }
}

async function downloadDeviceConfig(userId, deviceId, label) {
  const res = await api(`/users/${userId}/devices/${deviceId}/send_config`, {
    method: 'POST',
    body: JSON.stringify({ send_to_user: false })
  });
  if (res && res.ok && res.conf) {
    const blob = new Blob([res.conf], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = res.filename || `${label}.conf`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } else {
    alert(res && res.error ? res.error : 'Не удалось получить текст конфигурации');
  }
}

async function revokeDevice(userId, deviceId, label) {
  if (!confirm(`Вы действительно хотите удалить устройство «${label}»? Пиры на серверах будут сняты.`)) return;
  const res = await api(`/users/${userId}/devices/${deviceId}/revoke`, { method: 'POST' });
  if (res && res.ok) {
    alert(`Устройство «${label}» удалено!`);
    openUserModal(userId);
    loadUsers();
  } else {
    alert(res && res.error ? res.error : 'Ошибка удаления устройства');
  }
}

async function revokeBypass(userId, accessId, label) {
  if (!confirm(`Вы действительно хотите отозвать резервное подключение «${label}»?`)) return;
  const res = await api(`/users/${userId}/bypass/${accessId}/revoke`, { method: 'POST' });
  if (res && res.ok) {
    alert(`Подключение «${label}» отозвано!`);
    openUserModal(userId);
    loadUsers();
  } else {
    alert(res && res.error ? res.error : 'Ошибка отзыва подключения');
  }
}

// ── Server Maintenance ─────────────────────────────────────
let currentServerIdForSync = null;

async function checkServerHarden(serverId, serverName) {
  document.getElementById('harden-modal-title').innerText = `Защита: ${serverName} (#${serverId})`;
  document.getElementById('harden-modal-content').innerHTML = '<div style="text-align: center; padding: 20px;">⏳ Выполняю аудит безопасности через SSH...</div>';
  openModal('modal-server-harden');

  const res = await api(`/servers/${serverId}/harden_check`);
  if (!res) {
    document.getElementById('harden-modal-content').innerHTML = '<div style="color: var(--danger); padding: 12px;">Ошибка подключения к серверу</div>';
    return;
  }

  const statusColor = res.compliant ? 'var(--success)' : 'var(--warning)';
  const statusText = res.compliant ? '✅ Соответствует эталону' : '⚠️ Обнаружены несоответствия';
  const watchdogBadge = res.watchdog_active
    ? '<span class="badge badge-success">● Активен</span>'
    : '<span class="badge badge-danger">● Не запущен</span>';

  let checksHtml = '';
  if (res.passed && res.passed.length > 0) {
    checksHtml += '<h4 style="font-size: 12px; color: var(--success); margin: 12px 0 6px;">Пройденные проверки:</h4><ul style="padding-left: 18px; font-size: 12px;">';
    res.passed.forEach(p => { checksHtml += `<li>${p}</li>`; });
    checksHtml += '</ul>';
  }
  if (res.failed && res.failed.length > 0) {
    checksHtml += '<h4 style="font-size: 12px; color: var(--danger); margin: 12px 0 6px;">Замечания / несоответствия:</h4><ul style="padding-left: 18px; font-size: 12px;">';
    res.failed.forEach(f => { checksHtml += `<li><b>${f}</b></li>`; });
    checksHtml += '</ul>';
  }

  document.getElementById('harden-modal-content').innerHTML = `
    <div style="font-size: 14px; margin-bottom: 8px;">Статус: <b style="color: ${statusColor};">${statusText}</b></div>
    <div style="font-size: 13px; margin-bottom: 12px;">Сторож за машиной бота (Watchdog Timer): ${watchdogBadge}</div>
    ${checksHtml}
  `;
}

async function syncServerPeers(serverId, serverName) {
  currentServerIdForSync = serverId;
  document.getElementById('sync-peers-modal-title').innerText = `Сверка пиров: ${serverName} (#${serverId})`;
  document.getElementById('sync-peers-modal-content').innerHTML = '<div style="text-align: center; padding: 20px;">⏳ Опрашиваю интерфейс awg0 через SSH...</div>';
  document.getElementById('btn-clean-orphans').style.display = 'none';
  openModal('modal-server-peers');

  const res = await api(`/servers/${serverId}/sync_peers`, { method: 'POST' });
  if (!res) {
    document.getElementById('sync-peers-modal-content').innerHTML = '<div style="color: var(--danger); padding: 12px;">Ошибка подключения к серверу</div>';
    return;
  }

  const orphansCount = res.orphans ? res.orphans.length : 0;
  const missingCount = res.missing ? res.missing.length : 0;

  let detailsHtml = `
    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 16px; font-size: 13px;">
      <div>Пиров в awg0 на сервере: <b class="mono">${res.server_peers_count}</b></div>
      <div>Пиров в базе данных бота: <b class="mono">${res.db_peers_count}</b></div>
      <div>Осиротевших (нет в БД): <b class="mono" style="color: ${orphansCount ? 'var(--danger)' : 'var(--success)'};">${orphansCount}</b></div>
      <div>Отсутствующих на сервере: <b class="mono">${missingCount}</b></div>
    </div>
  `;

  if (orphansCount > 0) {
    detailsHtml += `<div style="margin-bottom: 12px; font-size: 12px; color: var(--text-secondary);">
      Найдены пиры на интерфейсе сервера, которых нет среди активных в базе данных. Вы можете удалить их в 1 клик.
    </div>`;
    document.getElementById('btn-clean-orphans').style.display = 'inline-block';
  } else {
    detailsHtml += `<div style="color: var(--success); font-size: 13px;">✅ Интерфейс чист: посторонних пиров не обнаружено.</div>`;
  }

  document.getElementById('sync-peers-modal-content').innerHTML = detailsHtml;
}

async function runCleanOrphans() {
  if (!currentServerIdForSync) return;
  if (!confirm('Удалить все осиротевшие пиры с интерфейса сервера?')) return;

  const res = await api(`/servers/${currentServerIdForSync}/sync_peers`, {
    method: 'POST',
    body: JSON.stringify({ clean: true })
  });

  if (res && res.ok) {
    alert(`Очистка завершена! Удалено осиротевших пиров: ${res.cleaned_count}`);
    closeModal('modal-server-peers');
    loadServers();
  } else {
    alert(res && res.error ? res.error : 'Ошибка при очистке пиров');
  }
}

async function updateServerWdtt(serverId, serverName) {
  if (!confirm(`Обновить бинарник qWDTT на ноде «${serverName}» до эталона? Служба обхода будет перезапущена.`)) return;
  const res = await api(`/servers/${serverId}/wdtt_update`, { method: 'POST' });
  if (res && res.ok) {
    alert(`✅ ${serverName}: ${res.detail}`);
    loadServers();
  } else {
    alert(res && res.error ? res.error : `Ошибка обновления: ${res ? res.detail : 'нет ответа'}`);
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
