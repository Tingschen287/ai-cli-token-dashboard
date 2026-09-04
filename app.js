/* ---------- tooltip ---------- */
const tip = document.getElementById('tip');
function clearColumn() {
  document.querySelectorAll('.cal i.col-hot').forEach(x => x.classList.remove('col-hot'));
}
function placeTip(anchor) {
  const r = anchor.getBoundingClientRect();
  tip.style.opacity = 1;
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  tip.style.left = Math.max(8, Math.min(window.innerWidth - tw - 8, r.left + r.width / 2 - tw / 2)) + 'px';
  // 贴近顶部时翻到下方，免得被裁掉
  tip.style.top = (r.top - th - 8 < 4 ? r.bottom + 8 : r.top - th - 8) + 'px';
}

document.addEventListener('mouseover', e => {
  // 顶部合计卡：显示统计窗口、缓存读、命中率（总 token 和次数已在卡片显示，不重复）
  const mc = e.target.closest('#metrics');
  if (mc) {
    tip.innerHTML = `<b>${mc.dataset.win}</b>`
      + `<br>cache read <b>${human(+mc.dataset.read)}</b>`
      + `<br>hit rate <b>${mc.dataset.hit}</b>`;
    placeTip(mc);
    return;
  }
  // 占比饼图的扇区：显示这个名字的总量和占比
  const arc = e.target.closest('.pie-wrap path, .pie-wrap circle');
  if (arc) {
    tip.innerHTML = `<b>${arc.dataset.sn}</b><br><b>${human(+arc.dataset.sv)}</b>`
      + `<span class="sm"> · ${arc.dataset.sp}%</span>`;
    placeTip(arc);
    return;
  }
  // 按模型品牌条：显示该模型总量和占窗口比例
  const mbar = e.target.closest('.mbar');
  if (mbar) {
    tip.innerHTML = `<b>${mbar.dataset.sn}</b><br><b>${human(+mbar.dataset.sv)}</b>`
      + `<span class="sm"> · ${mbar.dataset.sp}%</span>`;
    placeTip(mbar);
    return;
  }
  tip.classList.remove('plain');
  // 其余带 data-tip 的元素（按钮、额度组、标题数字等）：统一走自定义气泡。
  // 原生 title 的气泡是系统样式、和整体设计不搭，已全部换轨到这里
  const tipped = e.target.closest('[data-tip]');
  if (tipped && tipped.dataset.tip) {
    tip.textContent = tipped.dataset.tip;
    tip.classList.add('plain');
    placeTip(tipped);
    return;
  }
  // 堆叠条的某一段：显示这个项目里该模型的具体用量
  const seg = e.target.closest('.stack span');
  if (seg) {
    tip.innerHTML = `<b>${seg.dataset.sn}</b><br>${seg.dataset.seg} `
      + `<b>${human(+seg.dataset.sv)}</b><span class="sm"> · ${seg.dataset.sp}%</span>`;
    placeTip(seg);
    return;
  }
  const cell = e.target.closest('.cal i');
  if (!cell) return;
  // 格子可能来自主屏或长区间遮罩，各自跟随自己的 view 状态
  const cview = cell.closest('#longview') ? lvState.view : state.view;
  const unit = cview === 'weekly' ? 'calls (whole week)' : 'calls';
  // 累计视图 hover 显示截至当日的累计值（格子着色仍是当日增量）
  const cum = cview === 'cumulative' && cell.dataset.cum
    ? ` · cumulative <b>${human(+cell.dataset.cum)}</b>` : '';
  // token 用 K/M/B，调用次数保持整数——次数本来就是个位数量级的计数
  // dataset.w 是当日/当周增量最高的主力模型（格子颜色即它的品牌色）
  const dom = cell.dataset.w ? `<br><span class="sm">mostly ${cell.dataset.w}</span>` : '';
  tip.innerHTML = cell.dataset.v
    ? `<b>${cell.dataset.d}</b> · ${cell.dataset.p}<br>incr <b>${human(+cell.dataset.i)}</b>${cum}`
      + `<br><span class="sm">cache read ${human(+cell.dataset.c)} · ${fmt(+cell.dataset.m)} ${unit}</span>${dom}`
    : `<b>${cell.dataset.d}</b> · ${cell.dataset.p}<br><span class="sm">no activity${cum}</span>`;

  // 每周模式下整列同属一周，一起高亮才看得出 hover 的是哪一周
  clearColumn();
  if (cview === 'weekly') {
    cell.closest('.cal').querySelectorAll(`i[data-col="${cell.dataset.col}"]`)
        .forEach(x => x.classList.add('col-hot'));
  }

  placeTip(cell);
});
document.addEventListener('mouseout', e => {
  if (!e.target.closest('#metrics') && !e.target.closest('.cal i') && !e.target.closest('.stack span')
      && !e.target.closest('.mbar') && !e.target.closest('.pie-wrap') && !e.target.closest('[data-tip]')) return;
  tip.style.opacity = 0;
  clearColumn();
});

/* ---------- 渲染 ---------- */
function render() {
  // 三个视图共用同一套格子行：每行一个平台，标题带窗口数字，图例/额度并进标题右端。
  // 累计视图的区别只在 tooltip（hover 显示截至当日的累计值），格子着色不变。
  renderCalendar('view', state.view, state.weeks);
}

function renderAll() {
  renderMeta();
  render();
  renderRanks();
  // 长区间遮罩开着的话跟着刷新
  if (!document.getElementById('longview').hidden) renderLongview();
}

function bindSeg(id, key, cast) {
  document.getElementById(id).addEventListener('click', e => {
    const btn = e.target.closest('button');
    if (!btn) return;
    state[key] = cast(btn.dataset[key === 'weeks' ? 'weeks' : 'view']);
    [...btn.parentNode.children].forEach(b => b.setAttribute('aria-pressed', b === btn));
    render();
    // 时间窗口变了，右侧卡片和排行都要跟着重算
    renderMeta();
    renderRanks();
  });
}

bindSeg('view-seg', 'view', String);

/* ---------- 长区间遮罩 ----------
   主屏只放 3 个月；半年/一年在这里用整个屏幕渲染，Esc 或 ✕ 关闭。 */
const lv = document.getElementById('longview');
function renderLongview() {
  renderCalendar('lv-view', lvState.view, lvState.weeks);
}
document.getElementById('longview-btn').addEventListener('click', () => {
  lv.hidden = false;
  renderLongview();
});
document.getElementById('lv-close').addEventListener('click', () => { lv.hidden = true; });
for (const [id, key, cast] of [['lv-view-seg', 'view', String], ['lv-range-seg', 'weeks', Number]]) {
  document.getElementById(id).addEventListener('click', e => {
    const btn = e.target.closest('button');
    if (!btn) return;
    lvState[key] = cast(btn.dataset[key]);
    [...btn.parentNode.children].forEach(b => b.setAttribute('aria-pressed', b === btn));
    renderLongview();
  });
}

// 排行面板的 列表/占比 切换：只影响本面板，不动时间窗口
for (const id of ['models', 'projects']) {
  document.getElementById(id + '-toggle').addEventListener('click', e => {
    const btn = e.target.closest('button');
    if (!btn) return;
    rankMode[id] = btn.dataset.mode;
    [...btn.parentNode.children].forEach(b => b.setAttribute('aria-pressed', b === btn));
    renderRanks();
  });
}

applyData(DATA);
renderAll();

/* ---------- 口径说明 ---------- */
const info = document.getElementById('info');
info.innerHTML = `
  <div><b>Caliber</b>: incremental tokens = non-cached input + output + cache write. cache_read is listed separately and not used for coloring — it's over 90% of the total and would make the chart only reflect session length.</div>
  <div><b>Cross-tool alignment</b>: Grok's <code>inputTokens</code> includes cache reads, Claude's <code>input_tokens</code> doesn't; this is subtracted at collection time.</div>
  <div><b>Dedup</b>: Claude by <code>message.id</code> (streaming writes repeat), Grok by <code>session+prompt_id+model</code> (naturally unique).</div>
  <div><b>Timezone</b>: Claude's UTC strings and Grok's unix seconds are both converted to local time.</div>
  <div><b>Cost</b>: only Grok records <code>costUsdTicks</code>, estimated at 1e-9 USD and nominal under a subscription — not actual billing.</div>
  <div><b>Quota area</b>: below each grid, single row. The plan's 5-hour and weekly window usage, independent of the time window. cco / ccs / grok / kimi are polled every 3 min (serve mode only; on failure the last successful data is shown). Codex's <code>rate_limits</code> rides along in its session files — read locally, no network.</div>
  <div><b>Cumulative view</b>: cells are still colored by that day's increment (cumulative is monotonic, coloring carries no information); hover a cell for the cumulative value up to that day.</div>
  <div><b>Refresh</b>: auto-syncs every 60s, reads only local session logs, consumes no tokens.</div>`;
const infoBtn = document.getElementById('info-btn');
infoBtn.addEventListener('click', () => { info.hidden = !info.hidden; });
document.addEventListener('click', e => {
  if (!info.hidden && !info.contains(e.target) && e.target !== infoBtn) info.hidden = true;
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { info.hidden = true; lv.hidden = true; }
});

/* ---------- 每分钟自动同步 ----------
   服务端后台线程按自己的节奏重扫，这里只是把结果取回来。数据没变化时
   不重绘，免得整点把正在 hover 的格子刷掉。 */
let lastSync = Date.now();
const signature = p => JSON.stringify([
  p.profiles.map(x => [x.key, x.incr, x.msgs]), p.daily.length
]);
let lastSig = signature(DATA);

async function autoSync() {
  try {
    const res = await fetch('/api/data', { cache: 'no-store' });
    if (!res.ok) return;                 // 静态模式下静默跳过，不打扰
    const payload = await res.json();
    lastSync = Date.now();
    const sig = signature(payload);
    if (sig === lastSig) return;         // 无变化就不动 DOM
    lastSig = sig;
    applyData(payload);
    renderAll();
  } catch (err) { /* 离线打开时必然失败，忽略 */ }
}

function tickSync() {
  const sec = Math.round((Date.now() - lastSync) / 1000);
  document.getElementById('sync').innerHTML =
    sec < 90 ? `updated <b>${sec}</b>s ago` : `updated <b>${Math.round(sec / 60)}</b>m ago`;
}
setInterval(autoSync, 60000);
setInterval(tickSync, 1000);
tickSync();

/* ---------- 刷新 ----------
   有本地服务时 /api/data 会重新扫描目录并返回新聚合结果；
   直接双击 file:// 打开时 fetch 必然失败，降级成给出命令。 */
const REFRESH_CMD = 'python3 collect.py --serve';
const btn = document.getElementById('refresh');
const btnText = document.getElementById('refresh-text');
let busy = false, resetTimer = null;

function flash(cls, text, ms) {
  btn.classList.add(cls);
  btnText.textContent = text;
  clearTimeout(resetTimer);
  resetTimer = setTimeout(() => {
    btn.classList.remove('ok', 'warn');
    btnText.textContent = 'Refresh';
  }, ms);
}

async function doRefresh() {
  if (busy) return;
  busy = true;
  btn.disabled = true;
  btn.classList.add('spin');
  btn.classList.remove('ok', 'warn');
  btnText.textContent = 'Scanning';
  try {
    // force=1 让服务端立刻重扫，不等下一个定时周期
    const res = await fetch('/api/data?force=1', { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    applyData(await res.json());
    renderAll();
    lastSync = Date.now();
    flash('ok', 'Updated', 2000);
  } catch (err) {
    info.hidden = false;
    flash('warn', 'Need server', 3600);
  } finally {
    busy = false;
    btn.disabled = false;
    btn.classList.remove('spin');
  }
}

btn.addEventListener('click', doRefresh);
// r 键刷新，但别抢输入框的按键
document.addEventListener('keydown', e => {
  if (e.key === 'r' && !e.metaKey && !e.ctrlKey && !e.altKey
      && !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) doRefresh();
});

// 窗口尺寸变了要重算格子与行数，否则不再刚好一屏
let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(renderAll, 150);
});
