/* ---------- 性能卡：CPU / 内存 / GPU / 磁盘四环 ----------
   侧栏上块（Performance，1/3 高；下块是 Model | Project 排行板）。和流量一样
   是本地快照，但节奏快两个
   量级：后端 SysPoller 线程每 2 秒采一次 /proc 与 nvidia-smi 存快照，这里每 2 秒
   拉一次 /api/sys 只重画这一块，不碰主屏的 60 秒重绘逻辑。
   静态打开（无服务）时 fetch 静默失败，面板停留在骨架。 */

const SYS_POLL_MS = 2000;
// 大环 r=25，周长 = 2πr，stroke-dashoffset 按占比留空
const SYS_C = 2 * Math.PI * 25;

function sysRing() {
  const size = 56, r = 25;
  return `<span class="sys-ring"><svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}">
    <circle class="sys-bg" cx="${size / 2}" cy="${size / 2}" r="${r}"/>
    <circle class="sys-fg" cx="${size / 2}" cy="${size / 2}" r="${r}"
      stroke-dasharray="${SYS_C.toFixed(1)}" stroke-dashoffset="${SYS_C.toFixed(1)}"
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
    <div class="sys-cell" id="sys-mem">${sysRing()}<span class="sys-lab"><b>Memory</b><span class="sys-sub" id="sys-mem-sub"></span></span></div>
    <div class="sys-cell" id="sys-gpu">${sysRing()}<span class="sys-lab"><b>GPU</b><span class="sys-sub" id="sys-gpu-sub"></span><span class="sys-sub" id="sys-gpu-temp"></span></span></div>
    <div class="sys-cell" id="sys-disk">${sysRing()}<span class="sys-lab"><b>Disk</b></span></div>`;
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
    if (sub) sub.textContent = s.cpu.cores ? s.cpu.cores + ' cores' : '';
    document.getElementById('sys-cpu').dataset.tip =
      `CPU ${pct == null ? '--' : pct + '%'} · ${s.cpu.cores || '?'} logical cores (Windows host; no freq/sensor from WSL)`;
  }
  if (s.mem) {
    const pct = s.mem.pct;
    sysSet('sys-mem', pct, pctColor(pct), pct == null ? '--' : Math.round(pct) + '<i>%</i>');
    const sub = document.getElementById('sys-mem-sub');
    if (sub) sub.textContent = `${fmtGB(s.mem.used)}/${fmtGB(s.mem.total)} GB`;
    document.getElementById('sys-mem').dataset.tip =
      `Memory ${pct == null ? '--' : pct + '%'} · ${fmtGB(s.mem.used)} / ${fmtGB(s.mem.total)} GB`;
  }
  if (s.disk) {
    const pct = s.disk.pct;
    sysSet('sys-disk', pct, pctColor(pct), pct == null ? '--' : Math.round(pct) + '<i>%</i>');
    document.getElementById('sys-disk').dataset.tip =
      `Disk activity ${pct == null ? '--' : pct + '%'} (C: + D: active time, Windows host)`;
  }
  if (s.gpu) {
    const pct = s.gpu.pct;
    sysSet('sys-gpu', pct, pctColor(pct), pct == null ? '--' : Math.round(pct) + '<i>%</i>');
    const sub = document.getElementById('sys-gpu-sub');
    const temp = document.getElementById('sys-gpu-temp');
    const hot = s.gpu.temp >= 80 ? ' style="color:' + pctColor(90) + '"' : '';
    if (sub) sub.textContent = `${fmtGB(s.gpu.vused)}/${fmtGB(s.gpu.vtotal)} GB`;
    if (temp) temp.innerHTML = `<span${hot}>${s.gpu.temp}°C</span>`;
    document.getElementById('sys-gpu').dataset.tip =
      `${s.gpu.name} · util ${pct == null ? '--' : pct + '%'} · VRAM ${s.gpu.vused}/${s.gpu.vtotal} GB (${s.gpu.vpct}%) · ${s.gpu.temp}°C`;
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
