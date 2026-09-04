const DAY = 86400000;
const GAP = 3;

/* ---------- 工具 ---------- */
const fmt = n => n.toLocaleString('en-US');
/* token 一律用 K/M/B：模型计价本来就是按 per-million 报的，M 和成本直觉同刻度，
   比数逗号快得多。全站共用一套单位，避免同页出现两种写法。 */
function human(n) {
  for (const [div, suffix] of [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']]) {
    if (n >= div) {
      const v = n / div;
      return (v >= 10 ? v.toFixed(1) : v.toFixed(2)) + suffix;
    }
  }
  return String(Math.round(n));
}
// 紧凑数字（副指标用）：无小数，只够一眼看量级。比 human 短 1-2 字符，挤宽度用。
const hc = n => {
  for (const [div, suffix] of [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']]) {
    if (n >= div) return Math.round(n / div) + suffix;
  }
  return String(Math.round(n));
};
const iso = d => {
  const t = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
  return t.toISOString().slice(0, 10);
};
const parse = s => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
function weekKey(dateStr) {
  const d = parse(dateStr);
  d.setDate(d.getDate() - d.getDay());
  return iso(d);
}

/* 分位数分档：token 分布跨几个数量级，线性着色会让绝大多数格子挤在最浅一档 */
function thresholds(values) {
  const v = values.filter(x => x > 0).sort((a, b) => a - b);
  if (!v.length) return [0, 0, 0, 0];
  const q = p => v[Math.min(v.length - 1, Math.floor(v.length * p))];
  return [q(0.25), q(0.5), q(0.75), q(0.92)];
}
function level(value, th) {
  if (value <= 0) return 0;
  let lv = 1;
  for (const t of th) if (value > t) lv++;
  return Math.min(lv, 5);
}
/* 单色顺序色阶：同一色相下按明度递进，色盲安全，明暗主题都可读 */
function shade(hex, lv) {
  if (lv === 0) return null;
  const alpha = [0, 0.18, 0.36, 0.58, 0.8, 1][lv];
  const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

/* ---------- 数据整形 ---------- */
// 刷新会整体换掉 DATA，所以派生结构必须能重算，不能是顶层 const
let PROFILES = [], byProfile = {}, domModel = {}, firstDate = null, lastDate = null, LAYOUT = [];

function applyData(payload) {
  DATA = payload;
  // 目录不存在或没有任何记录的来源直接不画，避免出现一整行空白日历
  PROFILES = DATA.profiles.filter(p => p.present && p.msgs > 0);
  byProfile = {};
  for (const p of PROFILES) byProfile[p.key] = {};
  for (const row of DATA.daily) {
    if (byProfile[row.profile]) byProfile[row.profile][row.date] = row;
  }
  // 每天用量最大（incr 最高）的模型：格子按当日主力模型的品牌色着色——
  // ccs 行一眼看出哪天在跑哪家（用户明确要求）；单品牌行命中同一品牌，视觉不变
  domModel = {};
  for (const r of DATA.models) {
    const dm = domModel[r.profile] || (domModel[r.profile] = {});
    const cur = dm[r.date];
    if (!cur || r.incr > cur.incr) dm[r.date] = { model: r.model, incr: r.incr };
  }
  const allDates = DATA.daily.map(r => r.date).sort();
  firstDate = allDates[0];
  lastDate = allDates[allDates.length - 1];
  LAYOUT = getLayout();
  CCS_MODELS = loadCcsModels();   // ccs 可用模型是用户状态，随刷新重算自愈
}

// 主屏固定 3 个月；半年/一年在长区间遮罩里看（lvState）
let state = { view: 'daily', weeks: 13 };
let lvState = { view: 'daily', weeks: 26 };

/* ---------- 顶栏与窗口总量 ----------
   行标题大数字和右侧排行共用 activeWindow()：切到「每日」时排行只剩今天，
   行标题若还停在全期总量，同一屏上两个数字就对不上。
   view/weeks 可显式传入（长区间遮罩用自己的 lvState 渲染，不动主屏 state）。 */
function windowTotals(view = state.view, weeks = state.weeks) {
  const win = activeWindow(view, weeks);
  const acc = {};
  for (const p of PROFILES) {
    acc[p.key] = { incr: 0, cache_read: 0, input: 0, msgs: 0, reasoning: 0, cost_ticks: 0 };
  }
  for (const r of DATA.daily) {
    const a = acc[r.profile];
    if (!a || !win.test(r.date)) continue;
    a.incr += r.incr;
    a.cache_read += r.cache_read;
    a.input += r.input || 0;
    a.msgs += r.msgs;
    a.reasoning += r.reasoning || 0;
    a.cost_ticks += r.cost_ticks || 0;
  }
  return { win, acc };
}

/* 窗口副信息：cache 读量 · 调用次数×，再挂推理/名义金额（grok）。
   行标题大数字旁与 tooltip 共用这一套，保证同一窗口下各处数字一致 */
function winSub(d) {
  const extra = [];
  if (d.reasoning) extra.push(`reason ${human(d.reasoning)}`);
  // ticks 单位按 1e-9 USD 推定；grok 是订阅登录，这只是名义价值
  if (d.cost_ticks) {
    const usd = d.cost_ticks / 1e9;
    // 单日窗口下金额常常不足 $1，取整会全变成 $0
    extra.push(`≈$${usd.toFixed(usd >= 10 ? 0 : 2)}`);
  }
  // 缓存命中率 = cache_read / (cache_read + 非缓存输入)，即输入侧 token 有多大比例直接命中缓存。
  // 输入侧完全没有流量（cache_read 和 input 都为 0）时分母为 0，不显示，避免误导成 0%。
  const read = d.cache_read || 0, fresh = d.input || 0;
  if (read + fresh > 0) extra.unshift(`hit ${(read / (read + fresh) * 100).toFixed(0)}%`);
  return `cache ${human(d.cache_read)} · ${fmt(d.msgs)}×${extra.length ? ' · ' + extra.join(' · ') : ''}`;
}

function renderMeta() {
  const { win, acc } = windowTotals();

  // 顶栏固定给全期总账，作为不随 tab 变动的锚点
  const lifeIncr = PROFILES.reduce((s, p) => s + p.incr, 0);
  const lifeRead = PROFILES.reduce((s, p) => s + p.cache_read, 0);
  document.getElementById('total').innerHTML =
    `lifetime <b>${human(lifeIncr)}</b> · cache read ${human(lifeRead)} · ${firstDate} → ${lastDate}`;

  const totalIncr = PROFILES.reduce((s, p) => s + acc[p.key].incr, 0);
  const totalDup = PROFILES.reduce((s, p) => s + p.deduped, 0);
  const totalRead = PROFILES.reduce((s, p) => s + acc[p.key].cache_read, 0);
  const totalInput = PROFILES.reduce((s, p) => s + acc[p.key].input, 0);
  const totalMsgs = PROFILES.reduce((s, p) => s + acc[p.key].msgs, 0);
  // 缓存命中率 = cache_read / (cache_read + 非缓存输入)；无输入流量时留空
  const denom = totalRead + totalInput;
  const hit = denom > 0 ? (totalRead / denom * 100).toFixed(0) + '%' : '—';
  // 次数单位：calls；数字大了用 K
  const calls = totalMsgs >= 1000 ? (totalMsgs / 1000).toFixed(1) + 'K' : fmt(totalMsgs);
  // 合计卡：只留总 token + 调用次数两项，缓存量和命中率塞进 hover（自定义 tooltip）
  const metrics = document.getElementById('metrics');
  metrics.innerHTML =
    `<span class="big">${human(totalIncr)}<span class="lab">total</span></span>`
    + `<span class="sub"><b>${calls}</b> calls</span>`;
  // hover 数据：缓存读、命中率、统计窗口、精确值——供 mouseover 的 .metrics 分支用
  metrics.dataset.read = totalRead;
  metrics.dataset.hit = hit;
  metrics.dataset.win = win.label;
  metrics.dataset.ti = fmt(totalIncr);
  metrics.dataset.tr = fmt(totalRead);
  metrics.dataset.tm = fmt(totalMsgs);
}

function activeWindow(view = state.view, weeks = state.weeks) {
  if (view === 'daily') {
    return { test: d => d === lastDate, label: lastDate };
  }
  if (view === 'weekly') {
    const wk = weekKey(lastDate);
    return { test: d => weekKey(d) === wk, label: `week of ${wk}` };
  }
  const start = new Date(parse(lastDate).getTime() - (weeks * 7 - 1) * DAY);
  start.setDate(start.getDate() - start.getDay());
  const from = iso(start);
  const label = weeks >= 53 ? 'past 1 yr' : `past ${Math.round(weeks / 4.345)} mo`;
  return { test: d => d >= from && d <= lastDate, label };
}
