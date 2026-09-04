/* ---------- 右：排行 ----------
   时间窗口跟随左侧视图：每日 = 最后一天，每周 = 最后一周，累计 = 整个范围。 */

// 每个排行面板各自的展示模式：list = 横向堆叠条，pie = 占比环形图
let rankMode = { models: 'list', projects: 'list' };

function renderRank(id, rows, nameKey) {
  const box = document.getElementById(id);
  const win = activeWindow();
  document.getElementById(id + '-scope').textContent = win.label;

  // 按名字分组，组内再按段拆开——同一个项目常常横跨多个渠道/模型，
  // 分开列成多行就看不出这个项目总共烧了多少。
  // 段的粒度两个面板不同：按模型的段 = 来源（取主来源的品牌色和 logo），
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
  const list = [...groups.values()].sort((a, b) => b.total - a.total);

  if (!list.length) {
    box.innerHTML = `<div class="empty">No activity in this window</div>`;
    return;
  }
  if (rankMode[id] === 'pie') return renderPie(box, id, list);
  const max = list[0].total;

  // 排行宽带样式：条内放得下名字就白字放条内（居左），条太短时放条外。
  // 按模型：单色条（取该模型品牌色）+ 条前 logo；按项目：分段堆叠条，恢复各模型分布，
  // 段长 = 该模型在此项里的占比、段色与按模型排行同源。两种面板条高一致、名字规则一致。
  {
    const trackPx = 420; // 轨道近似宽度，用于估算名字放不放得下；差一点无碍，只是内/外之别
    const totalAll = list.reduce((s, x) => s + x.total, 0);
    box.innerHTML = list.map(g => {
      const dom = PROFILES.reduce((best, p) =>
        (g.parts[p.key] || 0) > (g.parts[best.key] || 0) ? p : best, PROFILES[0]).key;
      const w = g.total / max * 100;
      const needPx = g.name.length * 6.6 + 18; // 名字宽度 + 内边距
      const inside = (w / 100 * trackPx) >= needPx;
      if (id === 'models') {
        const bg = modelColor(dom, g.name);
        const logo = modelLogo(dom, g.name);
        return `
        <div class="mrow">
          ${logo}
          <div class="mtrack">
            <div class="mbar" style="width:${w.toFixed(1)}%;background:${soft(bg)}"
                 data-sn="${g.name}" data-sv="${g.total}"
                 data-sp="${(g.total / totalAll * 100).toFixed(0)}">
              ${inside ? `<span>${g.name}</span>` : ''}
            </div>
            ${inside ? '' : `<span class="mname-out">${g.name}</span>`}
          </div>
          <div class="rank-val">${human(g.total)}</div>
        </div>`;
      }
      // 按项目：分段堆叠，段 = 模型，段长按该模型在此项目中的占比，
      // 段色与按模型排行同源（modelColor）
      const parts = Object.entries(g.parts)
        .map(([m, v]) => ({ model: m, v, c: v / g.total * 100,
                            color: modelColor(g.profiles[m], m) }))
        .filter(p => p.v > 0)
        .sort((a, b) => b.v - a.v);
      const segs = parts.map(p => `
        <span style="width:${p.c.toFixed(2)}%;background:${soft(p.color)}"
              data-sn="${g.name}" data-seg="${p.model}"
              data-sv="${p.v}" data-sp="${(p.v / totalAll * 100).toFixed(0)}"></span>`).join('');
      return `
      <div class="mrow no-logo">
        <div class="mtrack">
          <div class="stack" style="width:${w.toFixed(1)}%">${segs}</div>
          ${inside ? `<span class="mname-in">${g.name}</span>` : `<span class="mname-out">${g.name}</span>`}
        </div>
        <div class="rank-val">${human(g.total)}</div>
      </div>`;
    }).join('');
    return;
  }
}

/* 占比饼图：实心扇区，每块直接标百分比；图例给名字和用量。
   颜色沿用堆叠条的规则——按模型取主来源下该模型的品牌色，按项目取用量最大
   模型段的品牌色，保证列表/占比两种看法同色系。 */
function renderPie(box, id, list) {
  const total = list.reduce((s, g) => s + g.total, 0);
  // 扇区颜色 = 这个名字里最大的一段的颜色
  const colorOf = g => {
    if (id === 'models') {
      const p = PROFILES.reduce((best, p) =>
        (g.parts[p.key] || 0) > (g.parts[best.key] || 0) ? p : best, PROFILES[0]);
      return modelColor(p.key, g.name);
    }
    // 按项目：段 = 模型，取用量最大的模型段的品牌色
    const m = Object.keys(g.parts).reduce((a, b) => g.parts[a] >= g.parts[b] ? a : b);
    return modelColor(g.profiles[m], m);
  };

  const CX = 110, CY = 110, R = 104;
  const pt = (ang, r) => [CX + r * Math.cos(ang), CY + r * Math.sin(ang)];
  let acc = -Math.PI / 2; // 从正上方开始，顺时针
  let slices = '', labels = '';
  for (const g of list) {
    const frac = g.total / total;
    const a0 = acc, a1 = acc + frac * 2 * Math.PI;
    acc = a1;
    const col = soft(colorOf(g));
    if (frac >= 0.9999) {
      // 单一项目占满整圆时弧线首尾重合画不出来，直接画个整圆
      slices += `<circle cx="${CX}" cy="${CY}" r="${R}" fill="${col}"
        data-sn="${g.name}" data-sv="${g.total}" data-sp="100.0"/>`;
    } else {
      const [x0, y0] = pt(a0, R), [x1, y1] = pt(a1, R);
      const large = frac > 0.5 ? 1 : 0;
      slices += `<path d="M${CX},${CY} L${x0.toFixed(2)},${y0.toFixed(2)}`
        + ` A${R},${R} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)} Z" fill="${col}"
        stroke="var(--panel)" stroke-width="1"
        data-sn="${g.name}" data-sv="${g.total}" data-sp="${(frac * 100).toFixed(1)}"/>`;
    }
    // 名字直接标在扇区里（占比看 hover）。扇区放不下全名时按可用宽度截断，
    // 太小的扇区不标——可用宽度沿弧向和径向取加权和，朝侧向的窄扇区也能放短名。
    const mid = (a0 + a1) / 2;
    const arcW = frac * 2 * Math.PI * (R * 0.62);
    const avail = arcW * Math.abs(Math.sin(mid)) + R * 0.55 * Math.abs(Math.cos(mid));
    const maxChars = Math.floor(avail / 6.6);
    if (maxChars >= 4) {
      // 项目名区分度在结尾（infra-…-wiki-mcp），保留结尾；模型名保留开头
      const keepTail = id !== 'models';
      const name = g.name.length > maxChars
        ? (keepTail ? '…' + g.name.slice(-(maxChars - 1)) : g.name.slice(0, maxChars - 1) + '…')
        : g.name;
      const [lx, ly] = pt(mid, R * 0.62);
      labels += `<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" class="pie-label">${name}</text>`;
    }
  }

  box.innerHTML = `<div class="pie-wrap">
    <svg width="220" height="220" viewBox="0 0 220 220">${slices}${labels}</svg>
  </div>`;
}

function renderRanks() {
  renderRank('models', DATA.models, 'model');
  renderRank('projects', DATA.projects, 'project');
}
