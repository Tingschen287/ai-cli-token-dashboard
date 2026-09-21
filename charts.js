/* ---------- 右：排行 ----------
   时间窗口跟随左侧视图：每日 = 最后一天，每周 = 最后一周，累计 = 整个范围。
   条形只显示前 7 名 + 一条 Other 汇总（共 8 条，长尾不再滚动）；下方剩余
   空间放占比空心环（donut），扇区标 logo 不标名——≥5% 的扇区才放得下，
   更小的和 Other 靠 hover 看明细。 */

// 排行板顶层 tab（Model | Project 两榜同构合并渲染，独享侧栏 2/3 高度）
let rankTab = 'models';

function groupRank(id, rows, nameKey) {
  const win = activeWindow();
  // 按名字分组，组内再按段拆开——同一个项目常常横跨多个渠道/模型，
  // 分开列成多行就看不出这个项目总共烧了多少。
  // 段的粒度两个榜不同：按模型的段 = 来源（取主来源的品牌色和 logo），
  // 按项目的段 = 模型（段色与按模型排行同源）；profiles 记录每个段来自哪个来源
  const groups = new Map();
  for (const r of rows) {
    if (r.incr <= 0 || !win.test(r.date)) continue;
    let g = groups.get(r[nameKey]);
    if (!g) groups.set(r[nameKey], g = { name: r[nameKey], total: 0, parts: {}, profiles: {} });
    g.total += r.incr;
    const pk = id === 'projects' ? r.model : r.profile;
    g.parts[pk] = (g.parts[pk] || 0) + r.incr;
    g.profiles[pk] = r.profile;
  }
  return [...groups.values()].sort((a, b) => b.total - a.total);
}

/* 组的主段：models 榜按来源（用量最大的 profile），projects 榜按模型 */
function domPart(id, g) {
  if (id === 'models') {
    const p = PROFILES.reduce((best, p) =>
      (g.parts[p.key] || 0) > (g.parts[best.key] || 0) ? p : best, PROFILES[0]);
    return { key: p.key, model: g.name, profile: p.key };
  }
  const m = Object.keys(g.parts).reduce((a, b) => g.parts[a] >= g.parts[b] ? a : b);
  return { key: m, model: m, profile: g.profiles[m] };
}

function renderRank(id, rows, nameKey) {
  const box = document.getElementById('rank');
  const list = groupRank(id, rows, nameKey);
  if (!list.length) {
    box.innerHTML = `<div class="empty">No activity in this window</div>`;
    return;
  }

  const total = list.reduce((s, g) => s + g.total, 0);
  // 前 7 名 + Other 汇总：剩余条目的段合并进 Other，保持堆叠构成可 hover
  const top = list.slice(0, 7);
  const rest = list.slice(7);
  const rows8 = [...top];
  if (rest.length) {
    const parts = {}, profiles = {};
    let t = 0;
    for (const g of rest) {
      t += g.total;
      for (const [k, v] of Object.entries(g.parts)) {
        parts[k] = (parts[k] || 0) + v;
        profiles[k] = g.profiles[k];
      }
    }
    rows8.push({ name: `Other (${rest.length})`, total: t, parts, profiles, isOther: true });
  }

  const max = rows8[0].total;
  const trackPx = 420; // 轨道近似宽度，用于估算名字放不放得下；差一点无碍，只是内/外之别
  const barHtml = rows8.map(g => {
    const w = g.total / max * 100;
    const needPx = g.name.length * 6.6 + 18;
    const inside = (w / 100 * trackPx) >= needPx;
    // 段拆分：Other 也按段堆叠（models 榜 = 来源、projects 榜 = 模型），
    // hover 任意段显示构成；普通 models 行是单色条 + 条前 logo
    const segs = Object.entries(g.parts)
      .map(([m, v]) => ({ m, v, c: v / g.total * 100,
                          color: modelColor(id === 'projects' ? g.profiles[m] : m,
                                            id === 'projects' ? m : g.name) }))
      .filter(p => p.v > 0).sort((a, b) => b.v - a.v)
      .map(p => `<span style="width:${p.c.toFixed(2)}%;background:${soft(p.color)}"`
        + ` data-sn="${g.name}" data-seg="${p.m}" data-sv="${p.v}"`
        + ` data-sp="${(p.v / total * 100).toFixed(0)}"></span>`).join('');
    const val = `<div class="rank-val">${human(g.total)}</div>`;
    if (id === 'models' && !g.isOther) {
      const bg = modelColor(domPart(id, g).profile, g.name);
      const logo = modelLogo(domPart(id, g).profile, g.name);
      return `<div class="mrow">${logo}
        <div class="mtrack"><div class="mbar" style="width:${w.toFixed(1)}%;background:${soft(bg)}"`
          + ` data-sn="${g.name}" data-sv="${g.total}" data-sp="${(g.total / total * 100).toFixed(0)}">`
          + `${inside ? `<span>${g.name}</span>` : ''}</div>`
          + `${inside ? '' : `<span class="mname-out">${g.name}</span>`}</div>${val}</div>`;
    }
    return `<div class="mrow no-logo"><div class="mtrack">`
      + `<div class="stack" style="width:${w.toFixed(1)}%">${segs}</div>`
      + `${inside ? `<span class="mname-in">${g.name}</span>` : `<span class="mname-out">${g.name}</span>`}`
      + `</div>${val}</div>`;
  }).join('');

  box.innerHTML = `<div class="rank-bars">${barHtml}</div>` + donutHtml(id, rows8, total);
}

/* 占比空心环：扇区 = 环带，颜色 = 该名字最大段的品牌色（Other 用 muted 灰），
   中角放 logo（≥5% 的扇区才有位置），环心是窗口总量。 */
function donutHtml(id, rows8, total) {
  const CX = 110, CY = 110, R = 104, r = 66;
  const pt = (ang, rad) => [CX + rad * Math.cos(ang), CY + rad * Math.sin(ang)];
  const colorOf = g => g.isOther ? 'color-mix(in srgb, var(--muted) 40%, var(--panel))'
    : soft(modelColor(domPart(id, g).profile, domPart(id, g).model));
  const logoUri = g => {
    if (g.isOther) return '';
    const d = domPart(id, g);
    const b = brandOf(d.profile, d.model);
    return b ? BRAND[b].img : (LOGOS[d.profile] || LOGOS.ccs);
  };
  let acc = -Math.PI / 2, slices = '', marks = '';
  for (const g of rows8) {
    const frac = g.total / total;
    // 满圆扇区的弧起终点重合画不出来，留 0.36° 的缝（stroke 的 panel 底色也会盖住它）
    const a0 = acc, a1 = acc + Math.min(frac, 0.9999) * 2 * Math.PI;
    acc = a1;
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const [x0, y0] = pt(a0, R), [x1, y1] = pt(a1, R);
    const [x2, y2] = pt(a1, r), [x3, y3] = pt(a0, r);
    slices += `<path d="M${x0.toFixed(2)},${y0.toFixed(2)} A${R},${R} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`
      + ` L${x2.toFixed(2)},${y2.toFixed(2)} A${r},${r} 0 ${large} 0 ${x3.toFixed(2)},${y3.toFixed(2)} Z"`
      + ` fill="${colorOf(g)}" stroke="var(--panel)" stroke-width="1"`
      + ` data-sn="${g.name}" data-sv="${g.total}" data-sp="${(frac * 100).toFixed(1)}"/>`;
    if (!g.isOther && frac >= 0.05) {
      const uri = logoUri(g);
      if (uri) {
        const [lx, ly] = pt((a0 + a1) / 2, (R + r) / 2);
        marks += `<image href="${uri}" x="${(lx - 11).toFixed(1)}" y="${(ly - 11).toFixed(1)}" width="22" height="22"/>`;
      }
    }
  }
  return `<div class="pie-wrap"><svg width="220" height="220" viewBox="0 0 220 220">${slices}${marks}</svg>
    <div class="donut-center"><b>${human(total)}</b><span>total</span></div></div>`;
}

function renderRanks() {
  if (rankTab === 'models') renderRank('models', DATA.models, 'model');
  else renderRank('projects', DATA.projects, 'project');
}
