/* Мини-приложение Moschata VPN.
 *
 * Без внешних библиотек: страница открывается на телефоне в дороге, и каждый
 * лишний килобайт — это лишняя секунда белого экрана. Всё состояние живёт в
 * одном объекте, экраны собираются заново — данных здесь на пару десятков
 * строк, и «умная» перерисовка обошлась бы дороже полной.
 */
'use strict';

const tg = window.Telegram && window.Telegram.WebApp;
const S = { data: null, tab: 'home', stack: [], busy: false, last: null };

/* ── Мелкие помощники ──────────────────────────────────────────────────── */

function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'html') node.innerHTML = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

function haptic(kind) {
  try { tg.HapticFeedback.impactOccurred(kind || 'light'); } catch (e) { /* старый клиент */ }
}

function toast(text) {
  const box = document.getElementById('toast');
  box.textContent = text;
  box.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { box.hidden = true; }, 2400);
}

function alertBox(text) {
  if (tg && tg.showAlert) { try { return tg.showAlert(text); } catch (e) { /* fallthrough */ } }
  toast(text);
}

function confirmBox(text) {
  return new Promise((resolve) => {
    if (tg && tg.showConfirm) {
      try { tg.showConfirm(text, (ok) => resolve(!!ok)); return; } catch (e) { /* fallthrough */ }
    }
    resolve(window.confirm(text));
  });
}

function copy(text, said) {
  const done = () => { haptic('light'); toast(said || 'Скопировано'); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallback());
  } else fallback();
  function fallback() {
    const ta = h('textarea', { style: 'position:fixed;opacity:0' });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (e) { alertBox('Скопируй вручную: ' + text); }
    ta.remove();
  }
}

/* ── Разговор с ботом ──────────────────────────────────────────────────── */

async function api(path, options) {
  const opts = options || {};
  const init = {
    method: opts.method || 'GET',
    headers: { 'Authorization': 'tma ' + ((tg && tg.initData) || '') },
  };
  if (opts.body) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch('/api' + path, init);
  if (opts.raw) {
    if (!res.ok) throw new Error('Не получилось загрузить.');
    return res.blob();
  }
  let data = {};
  try { data = await res.json(); } catch (e) { /* пустой ответ */ }
  if (!res.ok || !data.ok) {
    const err = new Error(data.message || 'Что-то пошло не так.');
    err.code = data.error;
    throw err;
  }
  return data;
}

/** Запускает действие, блокируя кнопку и показывая ошибку человеческим текстом. */
async function act(btn, fn) {
  if (S.busy) return;
  S.busy = true;
  const label = btn && btn.innerHTML;
  if (btn) { btn.disabled = true; btn.innerHTML = ''; btn.append(h('div', { class: 'spinner' })); }
  try {
    await fn();
  } catch (e) {
    haptic('heavy');
    alertBox(e.message);
  } finally {
    S.busy = false;
    if (btn && btn.isConnected) { btn.disabled = false; btn.innerHTML = label; }
  }
}

/* ── Навигация ─────────────────────────────────────────────────────────── */

function show(node) {
  const screen = document.getElementById('screen');
  screen.innerHTML = '';
  screen.append(node);
  screen.hidden = false;
  window.scrollTo(0, 0);
  document.getElementById('tabs').hidden = false;
  document.getElementById('boot').hidden = true;
  syncBack();
}

/** Рисует экран, ловя сбой сети: иначе на месте экрана вечно крутится спиннер. */
function run(render) {
  S.last = render;
  try {
    const maybe = render();
    if (maybe && maybe.catch) maybe.catch(showError);
  } catch (e) {
    showError(e);
  }
}

function showError(e) {
  const retry = h('button', { class: 'btn', onclick: () => run(S.last) }, '↻ Попробовать ещё раз');
  show(h('div', {},
    h('div', { class: 'card empty' },
      h('span', { class: 'em' }, '😔'),
      (e && e.message) || 'Не получилось загрузить. Проверь интернет.'),
    retry));
}

/** Открывает вложенный экран: он умеет закрываться системной стрелкой «назад». */
function push(render) {
  S.stack.push(render);
  run(render);
  syncBack();
}

function back() {
  S.stack.pop();
  const prev = S.stack[S.stack.length - 1];
  if (prev) run(prev); else openTab(S.tab);
  syncBack();
}

function syncBack() {
  if (!tg || !tg.BackButton) return;
  try {
    if (S.stack.length) tg.BackButton.show(); else tg.BackButton.hide();
  } catch (e) { /* старый клиент */ }
}

function openTab(name) {
  S.tab = name;
  S.stack = [];
  for (const b of document.querySelectorAll('.tab')) b.classList.toggle('on', b.dataset.tab === name);
  run({ home: renderHome, devices: renderDevices, bypass: renderBypass, money: renderMoney }[name]);
}

async function reload() {
  S.data = await api('/state');
}

function loading() {
  show(h('div', { class: 'boot' }, h('div', { class: 'spinner' })));
}

/* ── Главная ───────────────────────────────────────────────────────────── */

function daysWord(n) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return 'дней';
  if (b > 1 && b < 5) return 'дня';
  if (b === 1) return 'день';
  return 'дней';
}

function row(name, value, onclick) {
  return h('div', { class: 'row' + (onclick ? ' tap' : ''), onclick: onclick || null },
    h('div', { class: 'name' }, name),
    h('div', { class: 'value' }, value),
    onclick ? h('div', { class: 'chev' }, '›') : null,
  );
}

function heroCard(sub) {
  if (sub.perpetual) {
    return h('div', { class: 'card hero' },
      h('div', { class: 'big' }, '∞'),
      h('div', { class: 'sub' }, 'Подписка бессрочная'));
  }
  if (!sub.active) {
    return h('div', { class: 'card hero warn' },
      h('div', { class: 'big' }, 'Пауза'),
      h('div', { class: 'sub' }, 'Подписка закончилась. Всё сохранено — продли, и VPN включится сам.'));
  }
  return h('div', { class: 'card hero' },
    h('div', { class: 'big' }, String(sub.days_left), h('small', {}, daysWord(sub.days_left))),
    h('div', { class: 'sub' }, sub.trial ? 'Идёт бесплатный пробный период' : 'до ' + sub.expires_at + ' (МСК)'));
}

function renderHome() {
  const d = S.data, sub = d.sub;
  const wrap = h('div', { class: 'list-enter' },
    heroCard(sub),
    h('div', { class: 'card' },
      row('📱 Устройства', sub.devices_used + ' из ' + sub.devices_max, () => openTab('devices')),
      row('⚡ Резервные подключения', sub.bypass_used + ' из ' + sub.bypass_max, () => openTab('bypass')),
      row('📊 Трафик', sub.traffic),
      row('💰 Баланс', d.balance.text, () => openTab('money')),
    ),
    h('button', {
      class: 'btn', onclick: () => { haptic(); openTab('money'); },
    }, sub.active ? '🔁 Продлить подписку' : '🔁 Включить снова'),
    h('div', { class: 'section-title' }, 'Ещё'),
    h('div', { class: 'card' },
      row('👥 Друзья', d.flags.referral_percent + '% с пополнений', () => push(renderReferral)),
      row('🧾 История операций', '', () => push(renderHistory)),
      row('🆘 Поддержка', '', () => {
        try { tg.close(); } catch (e) { /* открыто в браузере */ }
      }),
    ),
    h('div', { class: 'note' }, 'Поддержка живёт в чате с ботом: закрой приложение и нажми «🆘 Поддержка».'),
  );
  show(wrap);
}

/* ── Устройства ────────────────────────────────────────────────────────── */

async function renderDevices() {
  loading();
  const d = await api('/devices');
  const cards = d.items.map((item) => h('div', {
    class: 'card tap', onclick: () => { haptic(); push(() => renderDevice(item.id)); },
  },
    h('div', { class: 'row first' },
      h('div', { class: 'name', style: 'color:var(--text);font-weight:600' }, item.label),
      h('div', { class: 'value' }, item.active ? h('span', { class: 'pill' }, 'работает')
                                               : h('span', { class: 'pill gray' }, 'на паузе')),
      h('div', { class: 'chev' }, '›')),
    h('div', { class: 'chips' },
      item.locations.map((loc) => h('span', { class: 'chip' }, loc)),
      h('span', { class: 'chip' }, '📊 ' + item.traffic),
      item.bypass ? h('span', { class: 'chip' }, '⚡ ' + item.bypass) : null),
  ));

  show(h('div', { class: 'list-enter' },
    h('div', { class: 'section-title' }, 'Устройства — ' + d.used + ' из ' + d.max),
    cards.length ? cards : h('div', { class: 'card empty' },
      h('span', { class: 'em' }, '📱'),
      'Пока пусто. Устройство — это телефон, планшет или компьютер, на котором будет работать VPN.'),
    d.can_add
      ? h('button', { class: 'btn', onclick: () => { haptic(); push(renderAddDevice); } }, '➕ Добавить устройство')
      : h('div', { class: 'note' }, d.sub_active
          ? 'Все устройства тарифа заняты. Добавь их в тариф на вкладке «Оплата» — неиспользованные дни не сгорят, а пересчитаются.'
          : 'Подписка на паузе — продли её на вкладке «Оплата», и устройства оживут сами.'),
  ));
}

function renderAddDevice() {
  const input = h('input', { type: 'text', maxlength: '32', placeholder: 'Например: Телефон' });
  const go = h('button', { class: 'btn' }, '🚀 Создать и получить конфиг');
  go.addEventListener('click', () => act(go, async () => {
    const res = await api('/devices', { method: 'POST', body: { label: input.value } });
    haptic('medium');
    toast('Готово: конфигов — ' + res.configs);
    S.stack.pop();
    push(() => renderDevice(res.id));
  }));
  show(h('div', { class: 'list-enter' },
    h('div', { class: 'section-title' }, 'Новое устройство'),
    h('div', { class: 'card' },
      h('div', { class: 'note', style: 'margin:0 0 4px' },
        'Название нужно только тебе — чтобы отличать устройства в списке.'),
      input, go),
    h('div', { class: 'note' }, 'Создание занимает несколько секунд: бот заводит конфиг в каждой доступной стране.'),
  ));
  setTimeout(() => input.focus(), 120);
}

async function renderDevice(id) {
  loading();
  const d = await api('/devices/' + id);
  const configs = d.configs.map((c) => h('div', { class: 'card tap', onclick: () => { haptic(); push(() => renderConfig(c.peer_id, c.location)); } },
    h('div', { class: 'row first' },
      h('div', { class: 'name', style: 'color:var(--text);font-weight:600' }, c.location),
      h('div', { class: 'value', style: 'font-weight:400;color:var(--hint)' }, c.traffic),
      h('div', { class: 'chev' }, '›'))));

  const rename = h('button', { class: 'btn ghost small' }, '✏️ Переименовать');
  rename.addEventListener('click', () => push(() => renderRename(d)));
  const del = h('button', { class: 'btn danger small' }, '🗑 Удалить устройство');
  del.addEventListener('click', () => act(del, async () => {
    const ok = await confirmBox('Удалить «' + d.label + '»? Конфиги перестанут работать, это необратимо.');
    if (!ok) return;
    const res = await api('/devices/' + id + '/delete', { method: 'POST' });
    haptic('medium');
    toast(res.message);
    back();
  }));

  show(h('div', { class: 'list-enter' },
    h('div', { class: 'section-title' }, d.label),
    d.active ? null : h('div', { class: 'card' },
      h('div', { class: 'note', style: 'margin:0' },
        '⏸ Устройство на паузе до продления подписки. Конфиги хранятся 30 дней и оживут сами.')),
    h('div', { class: 'section-title' }, 'Конфиги по странам'),
    configs.length ? configs : h('div', { class: 'card empty' }, 'Конфигов пока нет.'),
    d.bypass.length ? h('div', { class: 'section-title' }, 'Резервные подключения') : null,
    d.bypass.map((b) => h('div', { class: 'card' },
      h('div', { class: 'row first' },
        h('div', { class: 'name', style: 'color:var(--text)' }, b.label),
        h('div', { class: 'value' }, b.platform)))),
    h('div', { class: 'card' }, row('📊 Всего трафика', d.traffic)),
    rename, del,
  ));
}

function renderRename(device) {
  const input = h('input', { type: 'text', maxlength: '32', value: device.label });
  const go = h('button', { class: 'btn' }, 'Сохранить');
  go.addEventListener('click', () => act(go, async () => {
    await api('/devices/' + device.id + '/rename', { method: 'POST', body: { label: input.value } });
    haptic('medium');
    toast('Название изменено');
    back();
  }));
  show(h('div', { class: 'list-enter' },
    h('div', { class: 'section-title' }, 'Название устройства'),
    h('div', { class: 'card' }, input, go),
    h('div', { class: 'note' }, 'Конфиги на устройствах перенастраивать не нужно — название меняется только здесь.'),
  ));
}

async function renderConfig(peerId, location) {
  loading();
  const cfg = await api('/peers/' + peerId);
  const img = h('img', { class: 'qr hidden', alt: 'QR-код конфига' });

  const qrBtn = h('button', { class: 'btn ghost' }, '📱 Показать QR-код');
  qrBtn.addEventListener('click', () => act(qrBtn, async () => {
    if (!img.src) {
      const blob = await api('/peers/' + peerId + '/qr', { raw: true });
      img.src = URL.createObjectURL(blob);
    }
    img.classList.toggle('hidden');
    qrBtn.textContent = img.classList.contains('hidden') ? '📱 Показать QR-код' : '🙈 Скрыть QR-код';
  }));

  const linkBtn = h('button', { class: 'btn' }, '🔗 Скопировать ссылку');
  linkBtn.addEventListener('click', () => copy(cfg.link, 'Ссылка скопирована — вставь её в AmneziaVPN'));

  const send = (kind, label) => {
    const b = h('button', { class: 'btn ghost small' }, label);
    b.addEventListener('click', () => act(b, async () => {
      const res = await api('/peers/' + peerId + '/send', { method: 'POST', body: { kind } });
      haptic('medium');
      toast(res.message);
    }));
    return b;
  };

  show(h('div', { class: 'list-enter' },
    h('div', { class: 'section-title' }, location),
    h('div', { class: 'card' },
      h('div', { class: 'note', style: 'margin:0' },
        'Настраиваешь этот же телефон — жми «Скопировать ссылку» и вставь её в AmneziaVPN. ' +
        'Настраиваешь другое устройство — покажи ему QR-код.'),
      linkBtn, qrBtn, img),
    h('div', { class: 'section-title' }, 'Прислать в чат'),
    h('div', { class: 'card' },
      h('div', { class: 'btn-row' }, send('file', '📄 Файлом'), send('qr', '📱 Картинкой')),
      send('link', '🔗 Ссылкой')),
    h('div', { class: 'note' }, 'Файл открывается в AmneziaVPN: «＋» → выбрать файл.'),
  ));
}

/* ── Резервное подключение ─────────────────────────────────────────────── */

async function renderBypass() {
  loading();
  const d = await api('/bypass');
  const cards = d.items.map((item) => {
    const copyBtn = h('button', { class: 'btn ghost small' }, '🔗 Скопировать ссылку');
    copyBtn.addEventListener('click', () => copy(item.link, 'Ссылка скопирована — вставь её в приложение'));
    const del = h('button', { class: 'btn danger small' }, '🗑 Удалить');
    del.addEventListener('click', () => act(del, async () => {
      const ok = await confirmBox('Удалить «' + item.label + '»? Ссылка перестанет работать.');
      if (!ok) return;
      const res = await api('/bypass/' + item.id + '/delete', { method: 'POST' });
      haptic('medium');
      toast(res.message);
      run(renderBypass);
    }));
    return h('div', { class: 'card' },
      h('div', { class: 'row first' },
        h('div', { class: 'name', style: 'color:var(--text);font-weight:600' }, item.label),
        h('div', { class: 'value' }, item.active ? h('span', { class: 'pill' }, 'работает')
                                                 : h('span', { class: 'pill gray' }, 'на паузе'))),
      h('div', { class: 'chips' },
        h('span', { class: 'chip' }, item.location),
        item.platform_name ? h('span', { class: 'chip' }, item.platform_name) : null,
        h('span', { class: 'chip' }, '📊 ' + item.traffic)),
      item.active ? copyBtn : null,
      item.active && item.app ? h('div', { class: 'note' }, 'Приложение: ' + item.app) : null,
      del);
  });

  show(h('div', { class: 'list-enter' },
    h('div', { class: 'section-title' }, 'Резервное подключение — ' + d.used + ' из ' + d.max),
    h('div', { class: 'card' }, h('div', { class: 'note', style: 'margin:0' },
      'Запасной путь в интернет на случай, когда обычный VPN не проходит. ' +
      'Работает через отдельное приложение по ссылке.')),
    cards,
    d.can_add
      ? h('button', { class: 'btn', onclick: () => { haptic(); push(() => renderAddBypass(d)); } }, '➕ Добавить подключение')
      : h('div', { class: 'note' }, d.enabled
          ? 'Свободных мест в тарифе нет — добавь позицию на вкладке «Оплата».'
          : 'Сейчас недоступно. Загляни позже.'),
  ));
}

function renderAddBypass(d) {
  const chosen = { location: d.locations.length ? d.locations[0].key : '', platform: 'android', device: '' };

  const picker = (items, key, getK, getV) => {
    const box = h('div', { class: 'opts' });
    items.forEach((it) => {
      const b = h('button', { class: 'opt' + (chosen[key] === getK(it) ? ' on' : '') },
        h('span', { class: 'k' }, getV(it)));
      b.addEventListener('click', () => {
        chosen[key] = getK(it);
        for (const other of box.children) other.classList.remove('on');
        b.classList.add('on');
        haptic();
      });
      box.append(b);
    });
    return box;
  };

  const vk = h('input', { type: 'text', placeholder: 'vk.com/call/… — можно оставить пустым' });
  const go = h('button', { class: 'btn' }, '⚡ Создать подключение');
  go.addEventListener('click', () => act(go, async () => {
    const res = await api('/bypass', {
      method: 'POST',
      body: {
        location: chosen.location,
        platform: chosen.platform,
        device_id: chosen.device || null,
        vk: vk.value,
      },
    });
    haptic('medium');
    S.stack.pop();
    push(() => renderBypassDone(res));
  }));

  show(h('div', { class: 'list-enter' },
    h('div', { class: 'section-title' }, 'Новое резервное подключение'),
    h('div', { class: 'card' },
      h('div', { class: 'note', style: 'margin:0' }, 'Страна'),
      picker(d.locations, 'location', (x) => x.key, (x) => x.name),
      h('div', { class: 'note' }, 'Устройство'),
      picker([{ id: '', label: 'Отдельно' }].concat(d.devices), 'device', (x) => String(x.id), (x) => x.label),
      h('div', { class: 'note' }, 'Где будешь пользоваться'),
      picker(d.platforms, 'platform', (x) => x.key, (x) => x.name)),
    h('div', { class: 'card' },
      h('div', { class: 'note', style: 'margin:0' },
        'Своя ссылка на звонок VK — если хочешь пользоваться отдельной. Не знаешь, о чём речь, — оставь пустым, подставим нашу.'),
      vk),
    go,
  ));
}

function renderBypassDone(res) {
  const copyBtn = h('button', { class: 'btn' }, '🔗 Скопировать ссылку');
  copyBtn.addEventListener('click', () => copy(res.link, 'Ссылка скопирована'));
  const install = h('button', { class: 'btn ghost' }, '⬇️ Где взять приложение');
  install.addEventListener('click', () => {
    if (/^https?:/.test(res.install)) { try { tg.openLink(res.install); return; } catch (e) { /* браузер */ } }
    alertBox(res.install.replace(/<[^>]+>/g, ''));
  });
  show(h('div', { class: 'list-enter' },
    h('div', { class: 'section-title' }, 'Готово'),
    h('div', { class: 'card' },
      h('div', { class: 'row first' }, h('div', { class: 'name' }, 'Название'), h('div', { class: 'value' }, res.label)),
      row('Страна', res.location),
      row('Приложение', res.app),
      h('div', { class: 'mono' }, res.link),
      copyBtn, install),
    h('div', { class: 'note' }, 'Порядок такой: поставить приложение → вставить в него эту ссылку → включить.'),
  ));
}

/* ── Оплата ────────────────────────────────────────────────────────────── */

async function renderMoney() {
  loading();
  await reload();
  const d = S.data, sub = d.sub;

  const autopayBtn = h('button', { class: 'btn ghost small' },
    sub.autopay ? '♻️ Автопродление: включено' : '♻️ Автопродление: выключено');
  autopayBtn.addEventListener('click', () => act(autopayBtn, async () => {
    const res = await api('/autopay', { method: 'POST', body: { on: !sub.autopay } });
    sub.autopay = res.autopay;
    haptic('medium');
    toast(res.autopay ? 'Продлю сам, когда срок закончится' : 'Продлевать сам не буду');
    autopayBtn.innerHTML = res.autopay ? '♻️ Автопродление: включено' : '♻️ Автопродление: выключено';
  }));

  show(h('div', { class: 'list-enter' },
    h('div', { class: 'card hero' },
      h('div', { class: 'big' }, d.balance.text),
      h('div', { class: 'sub' }, 'На балансе')),
    h('button', { class: 'btn', onclick: () => { haptic(); push(renderTopUp); } }, '➕ Пополнить'),
    h('div', { class: 'section-title' }, 'Подписка'),
    h('div', { class: 'card' },
      row('📅 Срок', sub.perpetual ? 'бессрочно' : (sub.active ? 'до ' + sub.expires_at : 'закончился')),
      row('🎫 Тариф', sub.devices_max + ' устр. + ' + sub.bypass_max + ' подкл.'),
      autopayBtn),
    h('button', {
      class: 'btn', onclick: () => { haptic(); push(renderTariff); },
    }, sub.active && !sub.trial ? '🎫 Продлить или сменить тариф' : '🎫 Выбрать тариф'),
    h('div', { class: 'section-title' }, 'Ещё'),
    h('div', { class: 'card' },
      row('🧾 История операций', '', () => push(renderHistory)),
      row('👥 Друзья', d.flags.referral_percent + '%', () => push(renderReferral))),
  ));
}

function renderTopUp() {
  const d = S.data;
  const chosen = { rub: d.pay.amounts.length ? d.pay.amounts[1].rub : 100 };
  const custom = h('input', { type: 'number', min: String(d.pay.min_rub), max: String(d.pay.max_rub), placeholder: 'Своя сумма, ₽' });

  const box = h('div', { class: 'opts' });
  d.pay.amounts.forEach((a) => {
    const b = h('button', { class: 'opt' + (a.rub === chosen.rub ? ' on' : '') },
      h('span', { class: 'k' }, a.label), h('span', { class: 's' }, a.hint));
    b.addEventListener('click', () => {
      chosen.rub = a.rub;
      custom.value = '';
      for (const other of box.children) other.classList.remove('on');
      b.classList.add('on');
      haptic();
    });
    box.append(b);
  });
  custom.addEventListener('input', () => {
    for (const other of box.children) other.classList.remove('on');
    chosen.rub = parseInt(custom.value, 10) || 0;
  });

  const pay = (method, label, hint) => {
    const b = h('button', { class: 'btn' + (method === 'card' ? '' : ' ghost') }, label);
    b.addEventListener('click', () => act(b, async () => {
      const res = await api('/deposit', { method: 'POST', body: { method, rub: chosen.rub } });
      haptic('medium');
      if (res.kind === 'web') {
        toast('Счёт создан. Оплати — баланс пополнится сам.');
        try { tg.openLink(res.url); } catch (e) { window.open(res.url, '_blank'); }
      } else if (res.kind === 'telegram') {
        try { tg.openTelegramLink(res.url); } catch (e) { window.open(res.url, '_blank'); }
      } else {
        alertBox(res.message);
        try { tg.close(); } catch (e) { /* браузер */ }
      }
    }));
    return h('div', {}, b, hint ? h('div', { class: 'note' }, hint) : null);
  };

  show(h('div', { class: 'list-enter' },
    h('div', { class: 'section-title' }, 'Сколько пополнить'),
    h('div', { class: 'card' }, box, custom),
    h('div', { class: 'section-title' }, 'Чем платить'),
    h('div', { class: 'card' },
      d.pay.card ? pay('card', '💳 Картой или через СБП') : null,
      d.pay.cryptobot ? pay('cryptobot', '🪙 Через CryptoBot',
        d.pay.bonus.cryptobot ? 'Плюс ' + d.pay.bonus.cryptobot + '% к сумме пополнения.' : null) : null,
      pay('stars', '⭐ Звёздами Telegram', 'Счёт придёт в чат с ботом.')),
  ));
}

async function renderTariff() {
  loading();
  const sub = S.data.sub;
  const pick = { devices: sub.devices_max, bypass: sub.bypass_max, months: 1 };
  if (pick.devices + pick.bypass < 1) pick.devices = 1;
  let info = null;

  const draw = () => {
    const stepper = (key, name, emoji) => h('div', { class: 'row' },
      h('div', { class: 'name' }, emoji + ' ' + name),
      h('div', { class: 'stepper' },
        h('button', {
          disabled: pick[key] <= 0, onclick: () => { pick[key]--; haptic(); refresh(); },
        }, '−'),
        h('div', { class: 'n' }, String(pick[key])),
        h('button', {
          disabled: info && pick[key] >= info.ceiling[key], onclick: () => { pick[key]++; haptic(); refresh(); },
        }, '+')));

    const terms = (info ? info.terms : []).map((t) => {
      const b = h('button', { class: 'opt' + (pick.months === t.months ? ' on' : '') },
        h('span', { class: 'k' }, t.price),
        h('span', { class: 's' }, t.label + (t.discount ? ' · −' + t.discount + '%' : '')));
      b.addEventListener('click', () => { pick.months = t.months; haptic(); draw(); });
      return b;
    });

    const term = (info ? info.terms : []).find((t) => t.months === pick.months);
    const buy = h('button', { class: 'btn' }, term ? '💳 Оплатить ' + term.price : 'Считаю…');
    buy.addEventListener('click', () => act(buy, async () => {
      const res = await api('/tariff/buy', {
        method: 'POST',
        body: { devices: pick.devices, bypass: pick.bypass, months: pick.months },
      });
      haptic('medium');
      await reload();
      alertBox(res.message);
      S.stack = [];
      openTab('home');
    }));

    const switchable = info && info.switch.ok;
    const change = h('button', { class: 'btn ghost' }, switchable
      ? '⚙️ Сменить тариф без оплаты: ' + info.switch.old_days + ' → ' + info.switch.new_days + ' дн.'
      : '⚙️ Сменить тариф без оплаты');
    change.addEventListener('click', () => act(change, async () => {
      const ok = await confirmBox('Сменить тариф? Неиспользованные дни пересчитаются: '
        + info.switch.old_days + ' → ' + info.switch.new_days + ' дн.');
      if (!ok) return;
      const res = await api('/tariff/change', {
        method: 'POST',
        body: { devices: pick.devices, bypass: pick.bypass },
      });
      haptic('medium');
      await reload();
      alertBox(res.message);
      S.stack = [];
      openTab('home');
    }));

    show(h('div', { class: 'list-enter' },
      h('div', { class: 'section-title' }, 'Из чего собрать тариф'),
      h('div', { class: 'card' },
        stepper('devices', 'Устройства', '📱'),
        stepper('bypass', 'Резервные подключения', '⚡'),
        h('div', { class: 'row' },
          h('div', { class: 'name' }, 'Цена'),
          h('div', { class: 'value' }, info ? info.monthly + '/мес' : '…'))),
      h('div', { class: 'section-title' }, 'На какой срок'),
      h('div', { class: 'card' }, h('div', { class: 'opts' }, terms), buy),
      sub.active && !sub.trial && !sub.perpetual
        ? h('div', {}, change, switchable ? null : h('div', { class: 'note' },
            'Сменить тариф без оплаты сейчас нельзя: ' + ((info && info.switch.reason === 'same')
              ? 'это твой текущий тариф.' : 'выбери другой набор.')))
        : null,
      h('div', { class: 'note' },
        'Первая позиция — ' + S.data.prices.first + ' ₽/мес, каждое следующее устройство +'
        + S.data.prices.extra_device + ' ₽, каждое следующее подключение +'
        + S.data.prices.extra_bypass + ' ₽. Что-то не нужно — ставь 0.'),
    ));
  };

  const refresh = async () => {
    if (pick.devices + pick.bypass < 1) { pick.devices = 1; }
    draw();
    try {
      info = await api('/tariff?devices=' + pick.devices + '&bypass=' + pick.bypass);
      draw();
    } catch (e) { toast(e.message); }
  };
  await refresh();
}

async function renderHistory() {
  loading();
  const d = await api('/history');
  show(h('div', { class: 'list-enter' },
    h('div', { class: 'section-title' }, 'История операций'),
    d.rows.length ? h('div', { class: 'card' }, d.rows.map((r) => h('div', { class: 'row' },
      h('div', { class: 'name' }, h('div', { style: 'color:var(--text)' }, r.note), r.at),
      h('div', { class: 'value', style: r.positive ? 'color:var(--accent)' : '' },
        (r.positive ? '+' : '') + r.amount))))
      : h('div', { class: 'card empty' }, 'Пока пусто.'),
  ));
}

async function renderReferral() {
  loading();
  const d = await api('/referral');
  const copyBtn = h('button', { class: 'btn' }, '🔗 Скопировать ссылку');
  copyBtn.addEventListener('click', () => copy(d.link, 'Ссылка скопирована'));
  const share = h('button', { class: 'btn ghost' }, '📨 Отправить другу');
  share.addEventListener('click', () => {
    const url = 'https://t.me/share/url?url=' + encodeURIComponent(d.link)
      + '&text=' + encodeURIComponent('Пользуюсь этим VPN — работает и стоит недорого.');
    try { tg.openTelegramLink(url); } catch (e) { window.open(url, '_blank'); }
  });
  show(h('div', { class: 'list-enter' },
    h('div', { class: 'section-title' }, 'Друзья'),
    h('div', { class: 'card' },
      h('div', { class: 'note', style: 'margin:0' },
        d.percent + '% с каждого пополнения приглашённых падает тебе на баланс. Навсегда, не только с первого.'),
      h('div', { class: 'mono' }, d.link),
      copyBtn, share),
    h('div', { class: 'grid2' },
      h('div', { class: 'card tile' }, h('div', { class: 'k' }, 'Пришли по ссылке'), h('div', { class: 'v' }, String(d.count))),
      h('div', { class: 'card tile' }, h('div', { class: 'k' }, 'Заработано'), h('div', { class: 'v' }, d.earned))),
  ));
}

/* ── Запуск ────────────────────────────────────────────────────────────── */

function fatal(text) {
  document.getElementById('boot').hidden = true;
  const screen = document.getElementById('screen');
  screen.hidden = false;
  screen.innerHTML = '';
  screen.append(h('div', { class: 'card empty' }, h('span', { class: 'em' }, '😔'), text));
}

async function main() {
  if (tg) {
    try {
      tg.ready();
      tg.expand();
      tg.BackButton.onClick(back);
      // Шапка Telegram должна совпадать с фоном страницы, иначе сверху висит
      // полоса чужого цвета.
      if (tg.setHeaderColor) tg.setHeaderColor('secondary_bg_color');
    } catch (e) { /* старый клиент — обойдёмся без украшений */ }
  }
  for (const b of document.querySelectorAll('.tab')) {
    b.addEventListener('click', () => { haptic(); openTab(b.dataset.tab); });
  }
  try {
    await reload();
  } catch (e) {
    fatal(e.code === 'consent'
      ? 'Сначала прими условия — они на первом экране бота.'
      : e.message);
    return;
  }
  openTab('home');
}

main();
