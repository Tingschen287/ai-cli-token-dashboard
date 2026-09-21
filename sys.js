/* ---------- 性能卡：CPU / 内存 / GPU / 磁盘四环 ----------
   右侧第三块（By Model 与 By Project 之间）。和流量一样是本地快照，但节奏快两个
   量级：后端 SysPoller 线程每 2 秒采一次 /proc 与 nvidia-smi 存快照，这里每 2 秒
   拉一次 /api/sys 只重画这一块，不碰主屏的 60 秒重绘逻辑。
   静态打开（无服务）时 fetch 静默失败，面板停留在骨架。 */

const SYS_POLL_MS = 2000;
// 大环 r=25 / 小环 r=13.5，周长 = 2πr，stroke-dashoffset 按占比留空
const SYS_C = 2 * Math.PI * 25;
const SYS_C_SM = 2 * Math.PI * 13.5;

function sysRing(pct, cls) {
  const size = cls ? 34 : 56, r = cls ? 13.5 : 25;
  return `<span class="sys-ring${cls ? ' ' + cls : ''}"><svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}">
    <circle class="sys-bg" cx="${size / 2}" cy="${size / 2}" r="${r}"/>
    <circle class="sys-fg" cx="${size / 2}" cy="${size / 2}" r="${r}"
      stroke-dasharray="${(cls ? SYS_C_SM : SYS_C).toFixed(1)}" stroke-dashoffset="${(cls ? SYS_C_SM : SYS_C).toFixed(1)}"
      transform="rotate(-90 ${size / 2} ${size / 2})"/></svg>
    <span class="sys-num">--</span></span>`;
}

/* 首帧骨架：灰环 0%，拉到数据后只更新 dashoffset / 颜色 / 文本——
   不整块 innerHTML 重画，环的 0.6s 过渡才有起点 */
function buildSys() {
  const grid = document.getElementById('sys-grid');
  if (!grid) return;
  grid.innerHTML = `
    <div class="sys-cell" id="sys-cpu">${sysRing()}<span class="sys-lab"><b>CPU</b><span class="sys-sub" id="sys-cpu-sub"></span></span></div>
    <div class="sys-cell" id="sys-mem">${sysRing()}<span class="sys-lab"><b>内存</b><span class="sys-sub" id="sys-mem-sub"></span></span></div>
    <div class="sys-cell" id="sys-gpu">${sysRing()}<span class="sys-lab"><b>GPU</b><span class="sys-sub" id="sys-gpu-sub"></span></span></div>
    <div class="sys-cell sys-disks" id="sys-disk">
      <span class="sys-disk-row" id="sys-disk-rings">${sysRing(null, 'sm')}${sysRing(null, 'sm')}</span>
      <span class="sys-lab"><b>磁盘</b><span class="sys-sub" id="sys-disk-sub"></span></span>
    </div>`;
}

function sysSet(id, pct, color, num) {
  const cell = document.getElementById(id);
  if (!cell) return;
  const fg = cell.querySelector('.sys-fg'), numEl = cell.querySelector('.sys-num');
  const p = Math.max(0, Math.min(100, pct || 0));
  fg.style.stroke = color;
  fg.style.strokeDashoffset = (SYS_C * (1 - p / 100)).toFixed(1);
  if (num !== undefined) numEl.innerHTML = num;
}

function fmtGB(v) { return v >= 10 ? Math.round(v) : v.toFixed(1); }

function updateSys(s) {
  const note = document.getElementById('sys-note');
  if (s.updated && note) note.textContent = '↻ ' + s.updated.slice(11, 19);
  if (s.cpu) {
    const pct = s.cpu.pct;
    sysSet('sys-cpu', pct, pctColor(pct), pct == null ? '--' : Math.round(pct) + '<i>%</i>');
    const sub = document.getElementById('sys-cpu-sub');
    if (sub) sub.textContent = s.cpu.cores ? s.cpu.cores + ' 逻辑核' : '';
    document.getElementById('sys-cpu').dataset.tip =
      `CPU ${pct == null ? '--' : pct + '%'}（Windows 宿主机视角；WSL 里拿不到频率与温度）`;
  }
  if (s.mem) {
    const pct = s.mem.pct;
    sysSet('sys-mem', pct, pctColor(pct), pct == null ? '--' : Math.round(pct) + '<i>%</i>');
    const sub = document.getElementById('sys-mem-sub');
    if (sub) sub.textContent = `${fmtGB(s.mem.used)}/${fmtGB(s.mem.total)} GB`;
    document.getElementById('sys-mem').dataset.tip =
      `内存 ${pct == null ? '--' : pct + '%'} · ${fmtGB(s.mem.used)} / ${fmtGB(s.mem.total)} GB`;
  }
  if (s.gpu) {
    const pct = s.gpu.pct;
    sysSet('sys-gpu', pct, pctColor(pct), pct == null ? '--' : Math.round(pct) + '<i>%</i>');
    const sub = document.getElementById('sys-gpu-sub');
    const hot = s.gpu.temp >= 80 ? ' style="color:' + pctColor(90) + '"' : '';
    if (sub) sub.innerHTML = `${fmtGB(s.gpu.vused)}/${fmtGB(s.gpu.vtotal)} GB · <span${hot}>${s.gpu.temp}°C</span>`;
    document.getElementById('sys-gpu').dataset.tip =
      `${s.gpu.name} · 利用率 ${pct == null ? '--' : pct + '%'} · 显存 ${s.gpu.vused}/${s.gpu.vtotal} GB（${s.gpu.vpct}%） · ${s.gpu.temp}°C`;
  }
  if (s.disks && s.disks.length) {
    const row = document.getElementById('sys-disk-rings');
    // 小环数量跟盘数走（盘变了重建，多了/少了才动）
    if (row && row.children.length !== s.disks.length)
      row.innerHTML = s.disks.map(() => sysRing(null, 'sm')).join('');
    const cells = document.querySelectorAll('#sys-disk .sys-ring');
    const subParts = [];
    s.disks.forEach((d, i) => {
      const el = cells[i];
      if (!el) return;
      const fg = el.querySelector('.sys-fg');
      fg.style.stroke = pctColor(d.pct);
      fg.style.strokeDashoffset = (SYS_C_SM * (1 - d.pct / 100)).toFixed(1);
      el.dataset.tip = `${d.name} ${d.pct}% · ${fmtGB(d.used)} / ${fmtGB(d.total)} GB`;
      subParts.push(`${d.name} ${Math.round(d.pct)}%`);
    });
    const sub = document.getElementById('sys-disk-sub');
    if (sub) sub.textContent = subParts.join(' · ');
  }
}

async function sysTick() {
  try {
    const res = await fetch('/api/sys', { cache: 'no-store' });
    if (!res.ok) return;
    updateSys(await res.json());
  } catch (err) { /* 静态打开无服务，静默 */ }
}

buildSys();
setInterval(sysTick, SYS_POLL_MS);
sysTick();
