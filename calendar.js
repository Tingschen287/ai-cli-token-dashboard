/* ---------- 行尾额度区 ----------
   额度是账号实时状态，不随 activeWindow() 变——这是它有别于行标题大数字的根本点。
   数据来自后端 QuotaPoller（DATA.quota），静态生成的 HTML 没有这个键，直接不画。 */
function pctColor(pct) {
  return pct >= 90 ? '#e5484d' : pct >= 70 ? '#d4a000' : pct >= 50 ? '#e8930c' : '#2c9e4f';
}

function fmtReset(iso, style) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  let h = d.getHours();
  const m = String(d.getMinutes()).padStart(2, '0'), ap = h >= 12 ? 'pm' : 'am';
  const hm = `${h % 12 || 12}:${m}${ap}`;
  if (style === 'time') return hm;
  // 周重置：日期+时刻。接口返回的是完整时间戳，只显示日期会丢掉「几点重置」
  if (style === 'datetime') return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function quotaGroup(key, w, showReset, stale, color, provName) {
  const pct = Math.max(0, Math.min(100, w.pct || 0));
  const weekly = w.key !== '5h';
  const resetIso = w.reset || (w.reset_ms ? new Date(w.reset_ms).toISOString() : '');
  const reset = fmtReset(resetIso, weekly ? 'datetime' : 'time');
  const tip = (provName ? provName + ' · ' : '')
    + (reset ? `↻ resets ${reset}` : 'no reset time') + (stale ? ' · query failed, showing last data' : '');
  return `<span class="qgrp${weekly ? ' qgrp-w' : ''}" data-tip="${tip}">
    <span class="qw">${weekly ? 'wk' : '5h'}</span>
    <span class="qbar"><i style="width:${pct}%;background:${color || COLORS[key]}"></i></span>
    <span class="qn" style="color:${pctColor(pct)}">${pct}%</span>
    ${showReset && reset ? `<span class="qr">↻${reset}</span>` : ''}
  </span>`;
}

/* 行尾额度一行排开：cco/grok 窗口少，重置时间内联；ccs 的供应商名收进气泡 */
function quotaInline(key) {
  // codex 的额度来自会话文件里的 rate_limits（collect 时随 meta 下发），
  // 不联网、静态模式也有；其余来源走后端 QuotaPoller（DATA.quota）
  if (key === 'codex') {
    const p = (DATA.profiles || []).find(x => x.key === 'codex');
    const q = p && p.quota;
    if (!q) return '';
    const out = q.windows.map(w => quotaGroup(key, w, true, false)).join('');
    return out ? `<span class="qinline"${q.plan ? ` data-tip="plan: ${q.plan}"` : ''}>${out}</span>` : '';
  }
  const Q = DATA.quota;
  if (!Q) return '';
  let out = '';
  if (key === 'cco' && Q.cco) {
    out = Q.cco.windows.map(w => quotaGroup(key, w, true, false)).join('');
  } else if (key === 'kimi' && Q.kimi) {
    // kimi code 官方账号额度（CLI 的 OAuth 凭证轮询 /v1/usages），同 cco 处理
    out = Q.kimi.windows.map(w => quotaGroup(key, w, true, false)).join('');
  } else if (key === 'grok' && Q.grok) {
    // grok 只有周额度一个窗口（CLI 内部 billing 接口），同 cco 处理
    out = Q.grok.windows.map(w => quotaGroup(key, w, true, false)).join('');
  } else if (key === 'ccs' && Q.ccs) {
    // Kimi 的额度条已挪到 kimi code 行（同一账号），ccs 这里只剩 MiniMax 一家
    // 有额度，不带供应商标签也不会歧义（用户明确不要）；供应商名由 quotaGroup
    // 的悬停气泡携带。一行排开不换行（用户明确要求）。
    const cells = [];
    Q.ccs.providers.forEach(prov => {
      const isMmx = /^minimax$/i.test(prov.name);
      const short = isMmx ? 'Minimax' : prov.name.replace(/\s*For Coding\s*/i, '');
      const color = isMmx ? BRAND.minimax.color : null;
      if (prov.error && !prov.windows.length) {
        cells.push(`<span class="qerr" data-tip="${short}: ${prov.error}">${short}: ${prov.error}</span>`);
      } else {
        prov.windows.forEach(w => cells.push(quotaGroup(key, w, true, prov.stale, color, short)));
      }
    });
    return `<span class="qinline qinline-ccs">${cells.join('')}</span>`;
  }
  return out ? `<span class="qinline">${out}</span>` : '';
}

/* ---------- 左：日历 ----------
   布局分行分组（见上面「布局状态」），每个来源占一个槽位，一行 1~N 个槽位
   按权重分宽并排。格子是固定大小的正方形、绝不拉伸（用户明确要求），
   列数随槽位宽度能放几列放几列（一列 = 一周）——宽的槽位天然多放几列、
   多显示几周历史。月份轴在每个槽位内部，按自己的列几何对齐。
   主屏与长区间遮罩共用，box 各自传入；weeks 是列数上限（遮罩的 6mo/1yr）。 */
const SLOT_GAP = 24;
const CELL = 17;   // 格子边长固定（分支开始时 1920×1080 下的大小）

function renderCalendar(boxId, view, weeks) {
  const box = document.getElementById(boxId);
  const weekly = view === 'weekly';
  const cumulative = view === 'cumulative';
  const { acc } = windowTotals(view, weeks);
  const end = parse(lastDate);

  // 暂无数据的来源（目录缺失/无记录）从渲染剔除；布局记录保留，数据回来自动恢复；
  // 整行为空则整行去掉
  const layout = LAYOUT.map(row => row.filter(s => byProfile[s.k])).filter(r => r.length);
  if (!layout.length) {
    // 布局被清空（全部移除到候补池）时的空态提示
    box.innerHTML = '<div class="cal-empty">Nothing on board — click Layout to add sources</div>';
    return;
  }
  // 槽宽按行内权重分配；窄屏单栏模式下槽位纵向堆叠（见 940px 媒体查询），一律整宽
  const narrow = matchMedia('(max-width: 940px)').matches;
  const slotWidths = row => {
    if (narrow || row.length === 1) return row.map(() => box.clientWidth);
    const total = row.reduce((s, x) => s + x.w, 0);
    const avail = box.clientWidth - SLOT_GAP * (row.length - 1);
    return row.map(x => Math.round(avail * x.w / total));
  };
  // 槽位放得下的列数（一列 = 一周）。主屏不设上限：能放几列放几列，多放就是
  // 多显示几周历史（用户明确要求）；遮罩里受 weeks 约束（6mo/1yr 是显式选择）
  const colCap = boxId === 'view' ? 999 : weeks;
  const colsFor = w => Math.max(4, Math.min(colCap, Math.floor((w + GAP) / (CELL + GAP))));

  const P = Object.fromEntries(PROFILES.map(p => [p.key, p]));
  const MN = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  // 单个槽位（一个来源）：标题行 + 月份轴 + 格子 + 额度行（格子下方一行排开、不换行）
  const slotHtml = (key, w) => {
    const p = P[key];
    const map = byProfile[key];
    const cols = colsFor(w);
    // 起点 = end 所在周往前 cols-1 周，再对齐到周日 → 恰好 cols 列
    const start = new Date(end.getTime());
    start.setDate(start.getDate() - (cols - 1) * 7);
    start.setDate(start.getDate() - start.getDay());

    // 每周模式：把日聚合成周，整列共用同一个值
    let bucket = map, keyOf = k => k;
    if (weekly) {
      bucket = {};
      for (const [date, row] of Object.entries(map)) {
        const wk = weekKey(date);
        const b = bucket[wk] || (bucket[wk] = { incr: 0, cache_read: 0, msgs: 0 });
        b.incr += row.incr;
        b.cache_read += row.cache_read;
        b.msgs += row.msgs;
      }
      keyOf = weekKey;
    }
    // 每个桶（日/周）的主力模型与品牌色：日视图直查 domModel；周视图把周内
    // 各模型 incr 汇总取最大。没数据的桶回落行色
    const domColor = {}, domName = {};
    if (weekly) {
      const wk = {};
      for (const r of DATA.models) {
        if (r.profile !== key) continue;
        const mm = wk[weekKey(r.date)] || (wk[weekKey(r.date)] = {});
        mm[r.model] = (mm[r.model] || 0) + r.incr;
      }
      for (const [k, mm] of Object.entries(wk)) {
        const top = Object.entries(mm).sort((a, b) => b[1] - a[1])[0];
        if (top) { domName[k] = top[0]; domColor[k] = modelColor(key, top[0]); }
      }
    } else {
      for (const [dt, dm] of Object.entries(domModel[key] || {})) {
        domName[dt] = dm.model;
        domColor[dt] = modelColor(key, dm.model);
      }
    }
    // 累计视图格子仍按当日增量着色：累计值单调递增，按它着色是一条
    // 越来越深的渐变带，没有信息量；累计值挪进 hover tooltip
    const th = thresholds(Object.values(bucket).map(r => r.incr));

    const cells = [];
    let colIndex = -1, run = 0;
    for (let t = start.getTime(); t <= end.getTime(); t += DAY) {
      const d = new Date(t), dk = iso(d);   // dk：日期键，别盖住槽位的 profile key
      if (d.getDay() === 0) colIndex++;
      const row = bucket[keyOf(dk)];
      if (cumulative) run += row ? row.incr : 0;
      const lv = row ? level(row.incr, th) : 0;
      const bg = shade(domColor[keyOf(dk)] || COLORS[key], lv);
      const label = weekly ? `week of ${keyOf(dk)}` : dk;
      const cum = cumulative ? ` data-cum="${run}"` : '';
      const who = domName[keyOf(dk)] ? ` data-w="${domName[keyOf(dk)]}"` : '';
      const attrs = row
        ? ` data-v="1" data-col="${colIndex}" data-d="${label}" data-i="${row.incr}"`
          + ` data-c="${row.cache_read}" data-m="${row.msgs}" data-p="${p.label}"${who}${cum}`
        : ` data-col="${colIndex}" data-d="${label}" data-p="${p.label}"${cum}`;
      cells.push(`<i style="${bg ? 'background:' + bg : ''}"${attrs}></i>`);
    }

    // 月份轴按本槽位的列几何：每列一个标签位，列宽与格子完全一致（固定 17px）。
    // 标签打在「包含每月 1 号」的那一列：用这一周最后一天（周六）的月份判定，
    // 否则用周日的月份会晚一格（比如 9/1 是周四时，标签会落到 9/4 那列）
    const monthCols = [];
    let lastMonth = -1;
    for (let t = start.getTime(); t <= end.getTime(); t += DAY) {
      const d = new Date(t);
      if (d.getDay() !== 0) continue;
      const m = new Date(Math.min(t + 6 * DAY, end.getTime())).getMonth();
      monthCols.push(m !== lastMonth ? (lastMonth = m, MN[m]) : '');
    }
    const monthsHtml = `<div class="months"><div class="maxis" style="grid-template-columns:repeat(${monthCols.length},${CELL}px)">`
      + monthCols.map(m => `<span>${m}</span>`).join('') + `</div></div>`;

    const unit = weekly ? Object.keys(bucket).length + ' wk'
                        : Object.values(map).filter(r => r.incr > 0).length + ' d';
    // 槽位标题：左端 logo + 名称 + 窗口大数字/副信息（口径与排行、合计一致）；
    // 右端 ccs 可用模型 logo + 范围总量小字。额度在格子下方的 cal-foot。
    const d = acc[key];
    const sub = winSub(d);
    // ccs 可用模型是用户状态（CCS_MODELS，localStorage 持久化），标题右侧按它渲染；
    // 末尾 + 按钮常显（不需进 Layout 编辑态），点开选择器切换
    const names = CCS_MODELS.map(brandDisplayName);
    const models = key === 'ccs'
      ? `<span class="models" data-tip="${names.length ? 'Available models: ' + names.join(' · ') : '点击 + 选择可用模型'}">`
        + CCS_MODELS.map(k => `<img class="mlogo" src="${BRAND[k].img}" alt="${brandDisplayName(k)}">`).join('')
        + `<button class="models-add" data-ccs-add="1" data-tip="选择可用模型">+</button></span>`
      : '';
    const quota = quotaInline(key);
    return `
      <div class="cal-slot" data-k="${key}" style="width:${w}px">
        <div class="cal-title">
          <img class="picon" src="${LOGOS[key]}" alt="">
          <span class="pname">${p.label}</span>
          <span class="win" data-tip="${fmt(d.incr)} tokens · ${sub}">${human(d.incr)}</span>
          <span class="winsub" data-tip="${sub}">${sub}</span>
          <span class="tr">${models}<span class="range">${unit} active · ${human(p.incr)}</span></span>
        </div>
        ${monthsHtml}
        <div class="cal${weekly ? ' by-week' : ''}">${cells.join('')}</div>
        ${quota ? `<div class="cal-foot">${quota}</div>` : ''}
      </div>`;
  };

  const rows = layout.map(row => {
    const widths = slotWidths(row);
    return `<div class="cal-row">${row.map((s, i) => slotHtml(s.k, widths[i])).join('')}</div>`;
  }).join('');

  box.innerHTML = `
    <div class="cal-stack" style="--cell:${CELL}px;--gap:${GAP}px">${rows}</div>`;

  // ccs 槽位标题栏 + 按钮：innerHTML 整体替换后监听器不保留，每次重渲后重绑
  box.querySelectorAll('[data-ccs-add]').forEach(btn => btn.addEventListener('click', e => {
    e.stopPropagation();   // 否则 document 的「点外关闭」当场关掉刚打开的浮层
    openCcsPicker(btn);
  }));

  if (boxId === 'view') document.getElementById('scale-note').textContent = '';
}

/* ---------- ccs 可用模型选择器 ----------
   body 级单例浮层：点标题栏 + 按钮打开，按按钮位置 fixed 定位（clamp 进视口）。
   浮层独立于日历 DOM——切换选中只重渲日历和浮层内容，浮层本身保持打开、不闪跳。
   点浮层外 / Esc / 窗口 resize 关闭。 */
let ccsPickerEl = null;
function closeCcsPicker() {
  if (!ccsPickerEl) return;
  ccsPickerEl.remove();
  ccsPickerEl = null;
}
function renderCcsPicker() {
  if (!ccsPickerEl) return;
  const sel = new Set(CCS_MODELS);
  ccsPickerEl.innerHTML =
    `<div class="ccs-picker-title">可用模型</div>`
    + `<div class="ccs-picker-grid">` + Object.keys(BRAND).map(k =>
      `<button class="ccs-pick${sel.has(k) ? ' on' : ''}" data-k="${k}" aria-pressed="${sel.has(k)}">`
      + `<img src="${BRAND[k].img}" alt=""><span>${brandDisplayName(k)}</span></button>`).join('')
    + `</div>`
    + `<button class="btn ccs-picker-reset" data-tip="恢复默认三家">Reset</button>`;
  for (const b of ccsPickerEl.querySelectorAll('.ccs-pick')) {
    b.addEventListener('click', e => {
      e.stopPropagation();
      const set = new Set(CCS_MODELS);
      if (set.has(b.dataset.k)) set.delete(b.dataset.k);
      else set.add(b.dataset.k);
      // 展示顺序恒为 BRAND 表序，不随点选顺序乱跳
      saveCcsModels(Object.keys(BRAND).filter(k => set.has(k)));
      render();
      renderCcsPicker();
    });
  }
  ccsPickerEl.querySelector('.ccs-picker-reset').addEventListener('click', e => {
    e.stopPropagation();
    saveCcsModels(DEFAULT_CCS_MODELS);
    render();
    renderCcsPicker();
  });
}
function openCcsPicker(anchor) {
  if (ccsPickerEl) { closeCcsPicker(); return; }   // 再点 + 切关闭
  ccsPickerEl = document.createElement('div');
  ccsPickerEl.id = 'ccs-picker';
  document.body.appendChild(ccsPickerEl);
  renderCcsPicker();
  // 右对齐到按钮右缘、下方展开；clamp 进视口
  const r = anchor.getBoundingClientRect();
  const w = ccsPickerEl.offsetWidth, h = ccsPickerEl.offsetHeight;
  ccsPickerEl.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + 'px';
  ccsPickerEl.style.top = Math.max(8, Math.min(window.innerHeight - h - 8, r.bottom + 6)) + 'px';
}
// 一次性绑定：点浮层外关闭、Esc 关闭、resize 关闭（fixed 定位窗口变了会飘）
if (!window.__ccsPickerBound) {
  window.__ccsPickerBound = true;
  document.addEventListener('click', e => {
    if (!ccsPickerEl) return;
    if (ccsPickerEl.contains(e.target) || (e.target.closest && e.target.closest('[data-ccs-add]'))) return;
    closeCcsPicker();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeCcsPicker(); });
  window.addEventListener('resize', closeCcsPicker);
}
