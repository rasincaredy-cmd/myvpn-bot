/* Прогон страницы мини-приложения без браузера.
 *
 * Зачем: страница живёт внутри Telegram, и единственный способ проверить её
 * иначе — открыть телефон. Одна опечатка в javascript оставляет человека перед
 * вечным спиннером, и по логам бота этого не видно вовсе: запрос просто не
 * приходит.
 *
 * Здесь страница поднимается в jsdom, ответы API — заглушки нужной формы,
 * дальше harness кликает по всем экранам и проверяет, что каждый нарисовался и
 * что нажатия дошли до API. Ошибки javascript копятся в `errors`.
 *
 * НЕ входит в pytest-набор: node и jsdom проекту больше нигде не нужны, а
 * тащить их в зависимости ради одного файла дороже, чем запустить руками.
 *
 *     npm i jsdom && node scripts/miniapp_ui_check.js
 *
 * Правишь app.js — прогони и обнови ожидания здесь.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const DIR = '/root/myvpn-bot/bot/miniapp/static';
const errors = [];

const API = {
  '/api/state': {
    ok: true,
    user: { name: 'Влад', is_admin: true },
    sub: { active: true, perpetual: false, trial: false, expires_at: '30.09.2026 12:00',
           days_left: 25, devices_used: 1, devices_max: 3, bypass_used: 2, bypass_max: 2,
           traffic: 'безлимит', autopay: true, can_switch: true },
    balance: { kopeks: 45000, text: '450 ₽' },
    pay: { cryptobot: true, card: true, stars: true,
           amounts: [{ rub: 120, label: '120 ₽', hint: 'месяц', stars: 150 },
                     { rub: 320, label: '320 ₽', hint: '3 мес', stars: 400 },
                     { rub: 610, label: '610 ₽', hint: 'полгода', stars: 763 },
                     { rub: 1080, label: '1080 ₽', hint: 'год', stars: 1350 }],
           bonus: { cryptobot: 4, platega: 0, stars: 0 }, min_rub: 10, max_rub: 100000 },
    presets: [
      { key: 'solo', emoji: '📱', name: 'Один', hint: 'телефон или компьютер',
        devices: 1, bypass: 1, monthly: '120 ₽', per_device: '', best: false },
      { key: 'duo', emoji: '💻', name: 'Два устройства', hint: 'телефон и компьютер',
        devices: 2, bypass: 1, monthly: '160 ₽', per_device: '80 ₽', best: false },
      { key: 'family', emoji: '👪', name: 'Семейный', hint: 'себе и близким',
        devices: 4, bypass: 2, monthly: '270 ₽', per_device: '68 ₽', best: true },
    ],
    prices: { first: 90, extra_device: 40, extra_bypass: 30,
              terms: [{ months: 1, label: 'месяц', discount: 0 },
                      { months: 3, label: '3 мес', discount: 10 },
                      { months: 6, label: 'полгода', discount: 15 },
                      { months: 12, label: 'год', discount: 25 }] },
    flags: { bypass_enabled: true, trial_days: 7, referral_percent: 15,
             privacy_url: 'https://telegra.ph/p', terms_url: 'https://telegra.ph/t' },
  },
  '/api/devices': {
    ok: true, used: 1, max: 3, can_add: true, sub_active: true,
    items: [{ id: 5, label: 'Телефон', active: true,
              locations: ['🇳🇱 Нидерланды', '🇩🇪 Германия'], bypass: 1, traffic: '3.4 ГБ' }],
  },
  '/api/devices/5': {
    ok: true, id: 5, label: 'Телефон', active: true, traffic: '3.4 ГБ',
    configs: [{ peer_id: 12, location: '🇳🇱 Нидерланды', traffic: '2.1 ГБ' },
              { peer_id: 13, location: '🇩🇪 Германия', traffic: '1.3 ГБ' }],
    bypass: [{ id: 3, label: 'Телефон', platform: 'android' }],
  },
  '/api/peers/12': { ok: true, location: '🇳🇱 Нидерланды', label: 'Телефон',
                     conf: '[Interface]\nPrivateKey = x\n', link: 'vpn://AAAF' + 'x'.repeat(400) },
  '/api/bypass': {
    ok: true, used: 1, max: 2, can_add: true, enabled: true,
    items: [{ id: 3, label: 'Телефон', active: true, platform: 'android',
              platform_name: 'Android', app: 'qWDTT', location: '🇳🇱 Нидерланды',
              traffic: '120 МБ', link: 'wdtt://1.1.1.1:56000:1:2:PASS:hx' }],
    locations: [{ key: '🇳🇱 Нидерланды', name: '🇳🇱 Нидерланды' },
                { key: '🇩🇪 Германия', name: '🇩🇪 Германия' }],
    devices: [{ id: 5, label: 'Телефон' }],
    platforms: [{ key: 'android', name: 'Android', app: 'qWDTT' },
                { key: 'ios', name: 'iOS', app: 'VK Turn Proxy (iOS)' },
                { key: 'pc', name: 'ПК', app: 'PWDTT (Windows/Linux/macOS)' }],
  },
  '/api/deposit': { ok: true, kind: 'web', url: 'https://pay.example/invoice/1', id: 7, ttl: 30 },
  '/api/history': { ok: true, rows: [{ amount: '450 ₽', positive: true, note: 'Пополнение картой', at: '20.08 14:02' },
                                     { amount: '120 ₽', positive: false, note: 'Подписка 1 мес', at: '20.08 14:03' }] },
  '/api/referral': { ok: true, link: 'https://t.me/MoschataVPN_bot?start=ref_gelosum',
                     code: 'gelosum', count: 3, earned: '96 ₽', percent: 15 },
};

function tariffAnswer(qs) {
  const devices = Number(new URLSearchParams(qs).get('devices'));
  const bypass = Number(new URLSearchParams(qs).get('bypass'));
  return { ok: true, devices, bypass, ceiling: { devices: 10, bypass: 10 },
           monthly: (90 + (devices - 1) * 40 + bypass * 30) + ' ₽',
           terms: [1, 3, 6, 12].map((m) => ({ months: m, label: String(m) + ' мес', discount: 0,
                                              kopeks: m * 12000, price: m * 120 + ' ₽',
                                              per_month: '120 ₽', affordable: m < 3 })),
           switch: { ok: true, reason: '', old_days: 25, new_days: 19, used_devices: 1, used_bypass: 2 } };
}

const dom = new JSDOM(fs.readFileSync(path.join(DIR, 'index.html'), 'utf8'), {
  runScripts: 'outside-only',
  url: 'https://example.test/app/',
  virtualConsole: new VirtualConsole().on('jsdomError', (e) => errors.push('jsdom: ' + e.message)),
});
const { window } = dom;

const calls = [];
window.fetch = async (url, init) => {
  calls.push((init && init.method) || 'GET', url);
  const [pathname, qs] = url.split('?');
  let body = API[pathname];
  if (pathname === '/api/tariff') body = tariffAnswer(qs || '');
  if (pathname.endsWith('/qr')) {
    return { ok: true, status: 200, blob: async () => ({ size: 10 }) };
  }
  if (!body) body = { ok: true, message: 'Готово', kind: 'chat', id: 9, label: 'x',
                      link: 'wdtt://1.1.1.1:1:2:3:P:h', location: 'NL', app: 'qWDTT',
                      install: 'https://example.test/app-release', configs: 2 };
  return { ok: true, status: 200, json: async () => body };
};
window.URL.createObjectURL = () => 'blob:fake';
const shown = [];
window.Telegram = { WebApp: {
  initData: 'auth_date=1&user=%7B%22id%22%3A1%7D&hash=x',
  ready() {}, expand() {}, close() { shown.push('close'); },
  setHeaderColor() {}, openLink(u) { shown.push('openLink ' + u); },
  openTelegramLink(u) { shown.push('openTelegramLink ' + u); },
  showAlert(t) { shown.push('alert: ' + t); },
  showConfirm(t, cb) { shown.push('confirm: ' + t); cb(true); },
  BackButton: { onClick() {}, show() {}, hide() {} },
  HapticFeedback: { impactOccurred() {} },
} };
window.navigator.clipboard = { writeText: async (t) => shown.push('copy: ' + t.slice(0, 24)) };
window.addEventListener('error', (e) => errors.push('window.onerror: ' + e.message));
window.onunhandledrejection = (e) => errors.push('rejection: ' + (e.reason && e.reason.message));

window.eval(fs.readFileSync(path.join(DIR, 'app.js'), 'utf8'));

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const text = () => window.document.getElementById('screen').textContent.replace(/\s+/g, ' ');
// Берём САМЫЙ ГЛУБОКИЙ подходящий узел: обработчик висит на кнопке или строке,
// а не на карточке, которая их содержит.
const byText = (needle) => {
  const all = [...window.document.querySelectorAll('button, .row, .card, .opt')]
    .filter((n) => n.textContent.includes(needle));
  return all.filter((n) => !all.some((other) => other !== n && n.contains(other)));
};
const click = async (needle, note) => {
  const target = byText(needle)[0];
  if (!target) { errors.push('НЕ НАЙДЕНО: ' + needle + (note ? ' (' + note + ')' : '')); return false; }
  target.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await wait(60);
  return true;
};

(async () => {
  await wait(120);
  const steps = [];
  steps.push(['главная', text().includes('25') && text().includes('Устройства')]);

  await click('📱Устройства');            // нижняя вкладка
  steps.push(['вкладка «Устройства»', text().includes('Телефон')]);
  await click('Телефон');
  steps.push(['карточка устройства', text().includes('Конфиги по странам')]);
  await click('🇳🇱 Нидерланды');
  steps.push(['экран конфига', text().includes('Скопировать ссылку')]);
  await click('🔗 Скопировать ссылку');
  steps.push(['копирование ссылки', shown.some((s) => s.startsWith('copy: vpn://'))]);
  await click('📱 Показать QR-код');
  steps.push(['QR показан', !!window.document.querySelector('.qr:not(.hidden)')]);
  await click('📄 Файлом');
  steps.push(['отправка в чат', calls.includes('/api/peers/12/send')]);

  await click('⚡Резерв');
  steps.push(['вкладка «Резерв»', text().includes('Резервное подключение')]);
  await click('➕ Добавить подключение');
  steps.push(['форма подключения', text().includes('Страна') && text().includes('Android')]);
  await click('⚡ Создать подключение');
  steps.push(['подключение создано', text().includes('Готово')]);

  await click('💳Оплата');
  steps.push(['вкладка «Оплата»', text().includes('450 ₽')]);
  await click('➕ Пополнить');
  steps.push(['экран пополнения', text().includes('Чем платить')]);
  await click('💳 Картой или через СБП');
  steps.push(['счёт открыт', shown.some((s) => s.startsWith('openLink'))]);

  await click('💳Оплата');
  await click('🎫 Продлить или сменить тариф');
  steps.push(['витрина тарифов', text().includes('Семейный') && text().includes('выгоднее')]);
  await click('Два устройства');
  await wait(150);
  steps.push(['срок после пресета', text().includes('На какой срок')]);
  steps.push(['цена за месяц на кнопке срока', text().includes('/мес')]);
  steps.push(['состав пришёл из витрины', calls.includes('/api/tariff?devices=2&bypass=1')]);

  // Денег не хватает — страница обязана увести на пополнение, а не отказать.
  API['/api/tariff/buy'] = null;
  window.fetch = ((orig) => async (url, init) => {
    if (url === '/api/tariff/buy') {
      calls.push('POST', url);
      return { ok: false, status: 400, json: async () => ({
        ok: false, error: 'no_money', message: 'На балансе не хватает 45 ₽.',
        missing_rub: 50, missing: '45 ₽', price: '430 ₽' }) };
    }
    return orig(url, init);
  })(window.fetch);
  await click('💳 Оплатить');
  await wait(120);
  steps.push(['без денег ведёт на пополнение', text().includes('не хватило')]);
  steps.push(['сумма подставлена', text().includes('50 ₽')]);

  await click('💳Оплата');
  await click('🎫 Продлить или сменить тариф');
  await click('🧮 Собрать свой тариф');
  await wait(150);
  steps.push(['конструктор за кнопкой', text().includes('Свой тариф')]);
  const plus = window.document.querySelectorAll('.stepper')[0].querySelectorAll('button')[1];
  plus.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await wait(150);
  steps.push(['плюс устройства работает', calls.includes('/api/tariff?devices=4&bypass=2')]);
  await click('⚙️ Сменить');
  steps.push(['смена тарифа отправлена', calls.includes('/api/tariff/change')]);

  await click('🏠Главная');
  await click('👥 Друзья');
  steps.push(['друзья', text().includes('gelosum') || text().includes('96 ₽')]);
  await click('🏠Главная');
  await click('🧾 История операций');
  steps.push(['история', text().includes('Пополнение картой')]);

  for (const [name, ok] of steps) console.log((ok ? 'ok   ' : 'ПЛОХО') + '  ' + name);
  if (errors.length) { console.log('\nОШИБКИ:'); errors.forEach((e) => console.log('  ' + e)); }
  else console.log('\nисключений нет');
})();
