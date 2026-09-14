/* ---------- VPS 带宽 ----------
   顶栏一枚胶囊显示本计费周期用掉多少，点开是每日分账号的堆叠柱状图。

   两个口径故意不混，页面上也分开标：
     billing  网卡进出字节，和服务商账单同口径，决定会不会超额 —— 胶囊和大数字用它。
     user     S-UI 记的每个账号实际用量，能分人，但天生约为账单的一半
              （一个字节进来再出去，网卡上算两遍）—— 柱状图用它。
   把两者相加或互相比较都是错的，所以脚注里写死了这句话。 */

/* 账号配色：与 BRAND 表无关，这是人不是模型厂。
   全部落在暖色系里，跟着整套暖米白 + 品牌橙的调子走——冷蓝冷绿摆进来会很跳。
   按用量从大到小取色，主力使用者拿品牌橙，一眼看得出谁是大头。
   实际渲染时还会过一道 soft() 混面板底，和右侧排行条同样的硬度。 */
const VPS_PALETTE = ['#d97757', '#8a9a7b', '#c9a227', '#a1808f', '#6f9f95', '#b08968'];

function vpsColors(totals) {
  const map = {};
  Object.keys(totals)
    .sort((a, b) => (totals[b] - totals[a]) || a.localeCompare(b))
    .forEach((n, i) => { map[n] = VPS_PALETTE[i % VPS_PALETTE.length]; });
  return map;
}

function gb(bytes) {
  const v = bytes / 1073741824;
  if (v >= 100) return v.toFixed(1);
  if (v >= 10) return v.toFixed(2);
  return v.toFixed(2);
}

function vpsPct(v) {
  const p = v.billing && v.billing.quota_bytes
    ? v.billing.bytes / v.billing.quota_bytes * 100 : 0;
  return Math.max(0, Math.min(100, p));
}

/* 电量式四档：用得少是绿，越用越往红走。色值都往暖里调过，
   纯正的红绿摆在暖米白底上会像贴上去的。
   档位按「还剩多少」的心理预期切：一半以内安心，七成五开始要看一眼，
   九成就是真该管了。 */
const VPS_TONES = [
  { at: 90, color: '#b5443a' },   // 危险：暖红
  { at: 75, color: '#d97757' },   // 警告：品牌橙
  { at: 50, color: '#c9a227' },   // 注意：暖金
  { at: 0, color: '#6e8b5e' },   // 安全：暖橄榄绿
];

function vpsTone(pct) {
  return (VPS_TONES.find(t => pct >= t.at) || VPS_TONES[VPS_TONES.length - 1]).color;
}

function renderVpsPill(v) {
  const btn = document.getElementById('vps-btn');
  if (!v || v.error && !v.billing) { btn.hidden = true; return; }
  btn.hidden = false;
  const pct = vpsPct(v);
  const arc = document.getElementById('vps-ring-arc');
  const circ = 2 * Math.PI * 8;
  arc.setAttribute('stroke-dasharray', `${(pct / 100 * circ).toFixed(2)} 999`);
  btn.style.color = vpsTone(pct);
  const quota = Math.round(v.billing.quota_bytes / 1073741824);
  document.getElementById('vps-pill-text').innerHTML =
    `<b>${gb(v.billing.bytes)}</b> GB <span class="vps-den">/ ${quota}</span>`
    + (v.stale ? ' <span class="vps-den">·</span>' : '');
  btn.dataset.tip = v.stale
    ? `流量数据没取到，显示的是上一次的结果：${v.error || '未知原因'}`
    : `本周期已用 ${pct.toFixed(1)}%，点击看每日明细`;
}

/* 堆叠柱状图：一天一根，段 = 账号。
   高度按当天合计占窗口最大值的比例，最小可见高度 2px——用得少的那天
   也要看得出有没有用，而不是干脆消失。 */
function vpsBars(v) {
  const dates = Object.keys(v.daily || {}).sort();
  if (!dates.length) return '<div class="empty">这个周期还没有数据</div>';

  const names = [...new Set(dates.flatMap(d => Object.keys(v.daily[d])))];
  const totals = {};
  for (const n of names) {
    totals[n] = dates.reduce((s, d) => s + (v.daily[d][n]
      ? v.daily[d][n].up + v.daily[d][n].down : 0), 0);
  }
  const colors = vpsColors(totals);
  // 堆叠顺序固定用这一份排序，每根柱子的段顺序才一致
  const order = Object.keys(totals).sort((a, b) => (totals[b] - totals[a]) || a.localeCompare(b));

  const totalOf = d => Object.values(v.daily[d] || {})
    .reduce((s, x) => s + x.up + x.down, 0);
  const max = Math.max(...dates.map(totalOf), 1);

  const bars = dates.map(d => {
    const per = v.daily[d] || {};
    const tot = totalOf(d);
    const h = tot > 0 ? Math.max(2, tot / max * 100) : 0;
    const segs = order.filter(n => per[n] && (per[n].up + per[n].down) > 0)
      .map(n => {
        const val = per[n].up + per[n].down;
        return `<span style="height:${(val / tot * 100).toFixed(2)}%;background:${soft(colors[n])}"
                      data-vn="${n}" data-vd="${d}" data-vv="${val}"
                      data-vu="${per[n].up}" data-vw="${per[n].down}"></span>`;
      }).join('');
    // 计费周期会跨月，只标日号会看不出 31 之后的 01 是哪个月，
    // 所以每月 1 号那根标成 9/1
    const day = d.slice(8).replace(/^0/, '');
    const label = day === '1' ? `${+d.slice(5, 7)}/1` : d.slice(8);
    return `<div class="vps-col" data-vd="${d}" data-vt="${tot}">
              <div class="vps-bar" style="height:${h.toFixed(2)}%">${segs}</div>
              <span class="vps-day${day === '1' ? ' vps-month' : ''}">${label}</span>
            </div>`;
  }).join('');

  const legend = order.map(n =>
    `<span class="vps-key"><i style="background:${soft(colors[n])}"></i>${n}
     <b>${gb(totals[n])} GB</b></span>`).join('');

  return `<div class="vps-chart">${bars}</div>
          <div class="vps-legend">${legend}</div>`;
}

function renderVpsPop(v) {
  const box = document.getElementById('vps-pop');
  if (!v || !v.billing) {
    box.innerHTML = `<div class="vps-err">取不到 VPS 流量：${v && v.error || '未配置'}</div>`;
    return;
  }
  const pct = vpsPct(v);
  const b = v.billing, f = v.forecast || {}, c = v.cycle || {};
  const quota = Math.round(b.quota_bytes / 1073741824);
  const R = 26, circ = 2 * Math.PI * R;

  // 还剩几天到重置日，和「按现在的速度还能撑几天」放一起才有意义
  const daysToReset = c.reset
    ? Math.max(0, Math.ceil((new Date(c.reset + 'T00:00:00') - new Date()) / 86400000))
    : null;
  const safe = f.days_left != null && daysToReset != null && f.days_left >= daysToReset;

  // 估算说明：vnstat 攒满整天后这段会自己消失
  const note = b.estimated_days > 0
    ? `${b.estimated_days} 天按 ${b.ratio}× 估算`
      + (b.ratio_measured ? '（倍数已实测）' : '（倍数用理论值 2×，vnstat 攒够一整天后自动校准）')
    : '全部为 vnstat 实测';

  box.innerHTML = `
    <div class="vps-head">
      <div>
        <div class="vps-title">${v.label || 'Bandwidth Usage'}</div>
        <div class="vps-sub">${c.start || ''} → ${c.reset || ''}
          · 第 ${c.days_in || 0} 天${daysToReset != null ? ` · ${daysToReset} 天后重置` : ''}</div>
      </div>
      <svg class="vps-big-ring" viewBox="0 0 64 64" width="64" height="64" aria-hidden="true">
        <circle cx="32" cy="32" r="${R}" fill="none" stroke="var(--empty)" stroke-width="7"/>
        <circle cx="32" cy="32" r="${R}" fill="none" stroke="${vpsTone(pct)}" stroke-width="7"
                stroke-linecap="round" transform="rotate(-90 32 32)"
                stroke-dasharray="${(pct / 100 * circ).toFixed(2)} 999"/>
        <text x="32" y="36" class="vps-ring-num" fill="${vpsTone(pct)}">${pct.toFixed(0)}%</text>
      </svg>
    </div>

    <div class="vps-big" style="color:${vpsTone(pct)}">
      ${gb(b.bytes)} <span class="vps-unit">GB</span>
      <span class="vps-den">/ ${quota}GB</span>
    </div>

    ${vpsBars(v)}

    <div class="vps-foot">
      <div><b>日均</b> ${gb(f.per_day_bytes || 0)} GB
        <span class="sm">（近 ${f.sample_days || 0} 个完整天）</span></div>
      <div><b>按这个速度</b>
        ${f.days_left == null ? '—'
          : `还能撑 <span style="color:${safe ? 'inherit' : VPS_TONES[0].color}">${f.days_left} 天</span>`}
        ${daysToReset != null && f.days_left != null
          ? (safe ? '<span class="sm">，够到重置</span>'
                  : '<span class="sm">，撑不到重置日</span>') : ''}</div>
      <div class="sm vps-cal">
        顶部数字是<b>网卡进出</b>口径，和服务商账单一致；${note}。<br>
        下面柱状图是 <b>S-UI 按账号</b>口径，约为账单的一半（一个字节进来再出去，
        网卡上算两遍），两者不能相加或直接比较。
        ${v.stale ? '<br><b>这是上一次取到的数据</b>：' + (v.error || '') : ''}
      </div>
    </div>`;
}

function renderVps() {
  const v = (typeof DATA !== 'undefined' && DATA) ? DATA.vps : null;
  renderVpsPill(v);
  if (!document.getElementById('vps-pop').hidden) renderVpsPop(v);
}

/* ---------- 交互 ---------- */
{
  const pop = document.getElementById('vps-pop');
  const btn = document.getElementById('vps-btn');

  btn.addEventListener('click', e => {
    e.stopPropagation();
    const show = pop.hidden;
    if (show) renderVpsPop(DATA.vps);
    pop.hidden = !show;
    btn.setAttribute('aria-expanded', String(show));
  });

  // 点空白关掉，和 #info 一个行为
  document.addEventListener('click', e => {
    if (!pop.hidden && !pop.contains(e.target) && !btn.contains(e.target)) {
      pop.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
    }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { pop.hidden = true; btn.setAttribute('aria-expanded', 'false'); }
  });

  // 柱子段的气泡：走和其它图表同一套 #tip
  document.addEventListener('mouseover', e => {
    const seg = e.target.closest('.vps-bar span');
    if (!seg) return;
    const tipEl = document.getElementById('tip');
    tipEl.classList.remove('plain');
    tipEl.innerHTML = `<b>${seg.dataset.vd}</b> · ${seg.dataset.vn}`
      + `<br><b>${gb(+seg.dataset.vv)} GB</b>`
      + `<br><span class="sm">↑ ${gb(+seg.dataset.vu)} · ↓ ${gb(+seg.dataset.vw)} GB</span>`;
    placeTip(seg);
  });
  document.addEventListener('mouseout', e => {
    if (e.target.closest('.vps-bar span')) document.getElementById('tip').style.opacity = 0;
  });
}
