/* 部门套餐额度独立拉取，不依赖主屏会话扫描，也不把 Key 带入前端。 */
const CP_PROVIDERS = {
  minimax: { name: 'MiniMax', brand: 'minimax', plan: 'Token Plan', url: 'https://platform.minimaxi.com/' },
  kimi: { name: 'Kimi', brand: 'kimi', plan: 'Kimi Code', url: 'https://www.kimi.com/code/console' },
  glm: { name: 'GLM', brand: 'zai', plan: 'Coding Plan', url: 'https://bigmodel.cn/' },
  deepseek: { name: 'DeepSeek', brand: 'deepseek', plan: 'API 余额', url: 'https://platform.deepseek.com/' },
};
const cpDialog = document.getElementById('coding-plan-dialog');
const cpButton = document.getElementById('coding-plan-btn');
const cpGrid = document.getElementById('cp-grid');
const cpRefresh = document.getElementById('cp-refresh');
const cpEscape = value => String(value ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));
let cpData = null, cpFetching = false, cpError = '', cpWaitAfter = null, cpWaitTimer = null;
let cpThresholds = {};
try { cpThresholds = JSON.parse(localStorage.getItem('tdb-cp-thresholds-v1') || '{}') || {}; }
catch (err) { /* 本地偏好损坏时使用默认提醒线。 */ }
if (typeof cpThresholds !== 'object' || Array.isArray(cpThresholds)) cpThresholds = {};

function cpThreshold(account, currency) {
  const value = cpThresholds[account.id + ':' + currency];
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : currency === 'CNY' ? 50 : 10;
}
function cpTone(percent) {
  return percent === null ? 'unknown' : percent < 10 ? 'danger' : percent < 20 ? 'warning' : 'normal';
}
function cpAccountTone(account) {
  if (account.status !== 'ok') return 'unknown';
  if (account.provider === 'deepseek') {
    if (account.available === false || account.balances.some(b => b.total <= 0)) return 'danger';
    return account.balances.some(b => b.total < cpThreshold(account, b.currency)) ? 'warning' : 'normal';
  }
  const values = account.windows.map(w => w.remaining_pct).filter(v => v !== null);
  return cpTone(values.length ? Math.min(...values) : null);
}
function cpDate(seconds) {
  if (!seconds) return '时间未提供';
  const d = new Date(seconds * 1000);
  const today = d.toDateString() === new Date().toDateString();
  return (today ? '今天 ' : `${d.getMonth() + 1} 月 ${d.getDate()} 日 `)
    + d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
}
function cpCountdown(seconds) {
  const mins = Math.ceil((seconds * 1000 - Date.now()) / 60000);
  if (mins <= 0) return '已到重置时间，等待更新';
  if (mins < 60) return `${mins} 分钟后恢复`;
  if (mins < 1440) return `${Math.floor(mins / 60)} 小时 ${mins % 60} 分钟后恢复`;
  return `${Math.floor(mins / 1440)} 天 ${Math.floor(mins % 1440 / 60)} 小时后恢复`;
}
function cpPercent(value) {
  return value === null ? '—' : Number(value.toFixed(1)).toLocaleString('zh-CN');
}
function cpMoney(value, currency) {
  return value === null ? '—' : new Intl.NumberFormat('zh-CN', { style: 'currency', currency }).format(value);
}
function cpWindowHtml(w, secondary) {
  const pct = w.remaining_pct, tone = cpTone(pct);
  const reset = w.reset_at;
  return `<section class="cp-window ${secondary ? 'secondary' : ''}" data-tone="${tone}">
    <div class="cp-window-head"><span>${cpEscape(w.label)}剩余</span>
      <strong>${cpPercent(pct)}${pct === null ? '' : '<small>%</small>'}</strong></div>
    ${pct === null ? '<p class="cp-missing">平台未提供此窗口额度</p>' :
      `<div class="cp-track" role="meter" aria-label="${cpEscape(w.label)}剩余百分比" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><span style="width:${pct}%"></span></div>`}
    <div class="cp-reset"><span${reset ? ` data-cp-reset="${reset}"` : ''}>${reset ? cpCountdown(reset) : '重置时间未提供'}</span>
      ${reset ? `<time title="${cpEscape(new Date(reset * 1000).toLocaleString('zh-CN'))}">${cpDate(reset)}</time>` : ''}</div>
  </section>`;
}
function cpBalanceHtml(a) {
  return a.balances.map(b => `<section class="cp-balance">
    <span class="cp-balance-label">可用余额 · ${b.currency}</span>
    <strong>${cpMoney(b.total, b.currency)}</strong>
    <div class="cp-wallet-parts"><span>充值余额 <b>${cpMoney(b.topped_up, b.currency)}</b></span>
      <span>赠金 <b>${cpMoney(b.granted, b.currency)}</b></span></div>
    <label class="cp-threshold">低余额提醒线
      <span>${b.currency === 'CNY' ? '¥' : '$'} <input type="number" min="0" max="1000000000" step="0.01"
        data-account="${a.id}" data-currency="${b.currency}" value="${cpThreshold(a, b.currency)}"
        aria-label="${cpEscape(a.label)} ${b.currency} 低余额提醒线"></span>
    </label>
  </section>`).join('');
}
function cpCardHtml(a) {
  const p = CP_PROVIDERS[a.provider], tone = cpAccountTone(a);
  const state = a.status === 'stale' ? '数据已过期' : a.status === 'error' ? '查询失败' :
    a.status === 'unconfigured' ? '未配置 Key' : tone === 'danger' ? '余额耗尽 / 额度告急' :
    tone === 'warning' ? '留意余量' : tone === 'unknown' ? '额度未知' : '余量充足';
  let body;
  if (a.updated_at) {
    body = a.provider === 'deepseek' ? cpBalanceHtml(a) : a.windows.map((w, i) => cpWindowHtml(w, i > 0)).join('');
  } else {
    body = `<div class="cp-card-empty"><strong>—</strong><span>${a.status === 'unconfigured' ? '填入此账号的 Key 后自动显示' : '暂时无法读取余额或额度'}</span></div>`;
  }
  return `<article class="cp-card" data-tone="${tone}" data-status="${a.status}">
    <header class="cp-card-head"><div class="cp-identity"><img src="${BRAND[p.brand].img}" alt="">
      <div><h3>${p.name}</h3><p>${cpEscape(a.label)} · ${p.plan}</p></div></div>
      <span class="cp-badge">${state}</span></header>
    ${a.error ? `<p class="cp-card-error">${cpEscape(a.error)}${a.status === 'stale' ? '，以下为上次成功结果。' : ''}</p>` : ''}
    <div class="cp-card-body">${body}</div>
    <footer><span>${a.updated_at ? `${cpDate(a.updated_at)} 更新` : '等待有效数据'}</span>
      <a href="${p.url}" target="_blank" rel="noopener noreferrer">打开控制台 ↗</a></footer>
  </article>`;
}
function renderCodingPlans() {
  const accounts = cpData?.accounts || [];
  const counts = { normal: 0, warning: 0, danger: 0, unknown: 0 };
  accounts.forEach(a => counts[cpAccountTone(a)]++);
  const alertCount = counts.warning + counts.danger;
  const unknown = counts.unknown || Boolean(cpError || cpData?.error);
  const dot = cpButton.querySelector('.cp-alert-dot');
  dot.hidden = !alertCount && !unknown;
  dot.dataset.tone = counts.danger ? 'danger' : alertCount ? 'warning' : 'unknown';
  cpButton.setAttribute('aria-label', `Coding Plan，${alertCount ? `${alertCount} 个账号余量偏低` : unknown ? '部分额度待确认' : '查看部门额度'}`);
  const waiting = cpFetching || cpData?.refreshing || cpWaitAfter !== null;
  cpRefresh.disabled = Boolean(waiting);
  cpRefresh.textContent = waiting ? '正在更新…' : '↻ 刷新额度';
  document.getElementById('cp-summary').textContent = accounts.length
    ? `${accounts.length} 个账号 · ${counts.normal} 个余量充足${alertCount ? ` · ${alertCount} 个需要关注` : ''}${counts.unknown ? ` · ${counts.unknown} 个待确认` : ''}`
    : cpData ? '暂无已配置账号' : '正在读取额度…';
  document.getElementById('cp-updated').textContent = cpData?.checked_at ? `${cpDate(cpData.checked_at)} 检查` : '';
  const error = document.getElementById('cp-error');
  error.textContent = cpError || cpData?.error || '';
  error.hidden = !error.textContent;
  // 定时查询时不打断正在编辑提醒线的输入框。
  if (!(cpGrid.contains(document.activeElement) && document.activeElement.tagName === 'INPUT')) {
    cpGrid.innerHTML = accounts.length ? accounts.map(cpCardHtml).join('') :
      `<div class="cp-empty">${cpData ? '在 coding-plans.local.json 中添加账号，点击刷新即可。' : cpError ? '启动本地看板服务后即可查看实时额度。' : '正在连接本地额度服务…'}</div>`;
  }
}
async function loadCodingPlans(refresh = false) {
  if (cpFetching) return;
  cpFetching = true;
  if (refresh) cpWaitAfter = cpData?.checked_at || 0;
  renderCodingPlans();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const res = await fetch('/api/coding-plans' + (refresh ? '?refresh=1' : ''), { cache: 'no-store', signal: controller.signal });
    if (!res.ok) throw new Error();
    const payload = await res.json();
    if (!Array.isArray(payload.accounts)) throw new Error();
    cpData = payload;
    cpError = '';
    if (cpWaitAfter !== null && payload.checked_at > cpWaitAfter && !payload.refreshing) cpWaitAfter = null;
  } catch (err) {
    cpError = '额度服务暂时不可用，请确认本地服务已启动。';
    cpWaitAfter = null;
    if (cpData) cpData = { ...cpData, refreshing: false, accounts: cpData.accounts.map(a => a.updated_at
      ? { ...a, status: 'stale', error: '无法连接额度服务' } : a) };
  } finally {
    clearTimeout(timeout);
    cpFetching = false;
    renderCodingPlans();
    clearTimeout(cpWaitTimer);
    if (cpWaitAfter !== null || cpData?.refreshing) cpWaitTimer = setTimeout(() => loadCodingPlans(), 1500);
  }
}
cpButton.addEventListener('click', () => {
  document.getElementById('tip').style.opacity = 0;
  cpDialog.showModal();
  renderCodingPlans();
  loadCodingPlans();
});
function closeCodingPlans() {
  cpDialog.close();
  cpButton.focus();
}
document.getElementById('cp-close').addEventListener('click', closeCodingPlans);
cpDialog.addEventListener('click', e => {
  if (e.target !== cpDialog) return;
  const r = cpDialog.getBoundingClientRect();
  if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) closeCodingPlans();
});
cpDialog.addEventListener('keydown', e => {
  // 模态窗口中的快捷键不触发主页面刷新、退出布局等操作。
  e.stopPropagation();
  if (e.key === 'Escape') { e.preventDefault(); closeCodingPlans(); return; }
  if (e.key === 'Tab') {
    const stops = [...cpDialog.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled)')];
    const first = stops[0], last = stops[stops.length - 1];
    if (e.shiftKey && e.target === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && e.target === last) { e.preventDefault(); first.focus(); }
  }
  if (e.key === 'r' && !e.ctrlKey && !e.metaKey && !e.altKey && e.target.tagName !== 'INPUT') loadCodingPlans(true);
});
cpDialog.addEventListener('close', () => cpButton.focus());
cpRefresh.addEventListener('click', () => loadCodingPlans(true));
cpGrid.addEventListener('change', e => {
  const input = e.target.closest('input[data-account]');
  if (!input) return;
  const value = input.valueAsNumber;
  if (!Number.isFinite(value) || value < 0 || value > 1e9) { input.reportValidity(); return; }
  cpThresholds[input.dataset.account + ':' + input.dataset.currency] = value;
  try { localStorage.setItem('tdb-cp-thresholds-v1', JSON.stringify(cpThresholds)); }
  catch (err) { /* 当前页面仍使用新提醒线。 */ }
  renderCodingPlans();
});
cpGrid.addEventListener('focusout', () => setTimeout(renderCodingPlans, 0));
setInterval(() => {
  if (!document.hidden) loadCodingPlans();
}, 30000);
setInterval(() => {
  if (cpDialog.open) cpGrid.querySelectorAll('[data-cp-reset]').forEach(el => {
    el.textContent = cpCountdown(Number(el.dataset.cpReset));
  });
}, 1000);
loadCodingPlans();
