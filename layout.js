/* 布局是用户状态，不再是代码常量：localStorage[LAYOUT_KEY] 存
   { rows: [[{k, w}]...], known: [key...] }，w 是行内宽度权重（占比）。
   known 记录所有「见过」的来源：用户移除进候补池的 key 仍在 known 里，
   不会被 autoSync → getLayout 当成新来源复活；只有真正新出现的来源
   （PROFILES 新增）才自动作为新行追加。候补池不存储——派生 = 有数据的来源 − 已放置。 */
const LAYOUT_KEY = 'tdb-layout-v1';
const DEFAULT_LAYOUT = [
  [{ k: 'cco', w: 1 }],
  [{ k: 'codex', w: 1 }, { k: 'grok', w: 1 }],
  [{ k: 'ccs', w: 1 }, { k: 'kimi', w: 1 }],
];
const defaultLayout = () => DEFAULT_LAYOUT.map(row => row.map(s => ({ ...s })));
let KNOWN = new Set();   // 见过的来源集合，随 getLayout 重算、随 saveLayout 持久化

/* 自愈：剔除未知 key、跨行去重、回收空行、非法权重回落 1。
   暂无数据的已知来源允许留在布局里（渲染时按 byProfile 过滤），数据回来自动恢复 */
function healLayout(rows) {
  const known = new Set((DATA.profiles || []).map(p => p.key));
  const seen = new Set();
  return rows
    .map(row => (Array.isArray(row) ? row : [])
      .filter(s => s && known.has(s.k) && !seen.has(s.k) && seen.add(s.k))
      .map(s => ({ k: s.k, w: +s.w > 0 ? +s.w : 1 })))
    .filter(row => row.length);
}

function loadLayout() {
  let rows = null, known = [];
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        rows = parsed;                                  // 旧版裸数组：known 用已放置 key 顶
        known = parsed.flat().filter(s => s && s.k).map(s => s.k);
      } else if (parsed && Array.isArray(parsed.rows)) {
        rows = parsed.rows;
        known = Array.isArray(parsed.known) ? parsed.known : [];
      }
    }
  } catch (err) { /* 存储损坏等同无存储，回落默认布局 */ }
  return { rows: healLayout(rows || defaultLayout()), known };
}

/* known 取累计见过的 ∪ 当前已放置：移除进候补池不影响 known，防止被当新来源复活 */
function saveLayout(rows) {
  const known = new Set([...KNOWN, ...rows.flat().map(s => s.k)]);
  KNOWN = known;
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify({ rows, known: [...known] }));
  } catch (err) { /* 隐私模式等写不进去就算了 */ }
}

/* 入口：加载 + 自愈 + 新来源自动落位（真正没见过的才追加）；有追加才写回 */
function getLayout() {
  const { rows, known } = loadLayout();
  KNOWN = new Set([...known, ...rows.flat().map(s => s.k)]);
  const placed = new Set(rows.flat().map(s => s.k));
  let added = false;
  for (const p of PROFILES) {
    if (!placed.has(p.key) && !KNOWN.has(p.key)) {
      rows.push([{ k: p.key, w: 1 }]);
      KNOWN.add(p.key);
      added = true;
    }
  }
  if (added) saveLayout(rows);
  return rows;
}

/* ---------- 编辑态 ----------
   iOS 组件式：顶栏 Layout 按钮进入编辑态——槽位点 − 回候补池、拖动换位/开新行、
   拖相邻槽位间的分隔条调占比；候补池点击或拖入上屏；Reset 恢复默认。
   所有变更立即写 localStorage 并重渲日历（render() 末尾会调 applyEditChrome
   重新挂编辑态覆盖层，所以编辑中 autoSync 重渲也不会丢把手）。 */
let editing = false;
let dragKey = null;   // 正在拖拽的来源 key（可能来自槽位，也可能来自候补池）

/* 候补池是派生的：有数据的来源 − 已放置的 key，不单独存储 */
function poolProfiles() {
  const placed = new Set(LAYOUT.flat().map(s => s.k));
  return PROFILES.filter(p => !placed.has(p.key));
}

/* 在 LAYOUT 中定位 key，返回 [行索引, 槽索引]；找不到返回 null */
function findSlot(key) {
  for (let ri = 0; ri < LAYOUT.length; ri++) {
    const si = LAYOUT[ri].findIndex(s => s.k === key);
    if (si >= 0) return [ri, si];
  }
  return null;
}

/* 防御性清全：正常情况一个 key 只出现一次，但历史 bug 可能留下重复，一并清掉 */
function removeSlot(key) {
  for (let ri = LAYOUT.length - 1; ri >= 0; ri--) {
    const row = LAYOUT[ri].filter(s => s.k !== key);
    if (row.length) LAYOUT[ri] = row;
    else LAYOUT.splice(ri, 1);                          // 行空了就回收
  }
}

/* 移动/插入槽位。target 四选一：{ beforeKey } / { afterKey } /
   { newRowBeforeKey } / { newRowEnd: true }。key 可能来自候补池（尚未放置）。
   先摘除再按键名找目标，对「暂无数据的来源占了位置」的行索引漂移免疫 */
function moveSlot(key, target) {
  const at = findSlot(key);
  const slot = at ? LAYOUT[at[0]][at[1]] : { k: key, w: 1 };
  if (at) removeSlot(key);
  if (target.newRowEnd) { LAYOUT.push([slot]); return; }
  if (target.newRowBeforeKey !== undefined) {
    const to = findSlot(target.newRowBeforeKey);
    LAYOUT.splice(to ? to[0] : LAYOUT.length, 0, [slot]);
    return;
  }
  const to = findSlot(target.beforeKey !== undefined ? target.beforeKey : target.afterKey);
  if (!to) { LAYOUT.push([slot]); return; }             // 目标已被自己挪走等边界：放末尾新行
  LAYOUT[to[0]].splice(to[1] + (target.afterKey !== undefined ? 1 : 0), 0, slot);
}

/* 每次编辑动作的收尾：存 + 重渲日历 + 重画候补池 */
function persist() {
  saveLayout(LAYOUT);
  render();
  renderPool();
}

function enterEdit() {
  editing = true;
  document.body.classList.add('editing');
  document.getElementById('layout-btn').textContent = 'Done';
  renderPool();
  render();
}

function exitEdit() {
  if (!editing) return;
  editing = false;
  dragKey = null;
  document.body.classList.remove('editing', 'dragging');
  document.getElementById('layout-btn').textContent = 'Layout';
  document.getElementById('pool').hidden = true;
  render();
}

function renderPool() {
  const pool = document.getElementById('pool');
  if (!editing) { pool.hidden = true; return; }
  const avail = poolProfiles();
  pool.innerHTML =
    `<span class="pool-label">Available — 点击追加为新行，拖到某个槽位上并入该行</span>`
    + (avail.length
      ? avail.map(p => `<span class="chip" draggable="true" data-k="${p.key}">
          <img src="${LOGOS[p.key] || ''}" alt="">${p.label}</span>`).join('')
      : `<span class="pool-empty">全部已上屏 — 点槽位右上角的 − 移除</span>`)
    + `<span class="spacer"></span>`
    + `<button class="btn" id="pool-reset" data-tip="恢复默认布局">Reset</button>`;
  pool.hidden = false;
  for (const chip of pool.querySelectorAll('.chip')) {
    const key = chip.dataset.k;
    chip.addEventListener('click', () => {
      if (findSlot(key)) return;                        // 防御：池里出现已放置的 key 是过期状态
      LAYOUT.push([{ k: key, w: 1 }]);                  // 点击 = 追加为新行
      persist();
    });
    chip.addEventListener('dragstart', e => {
      dragKey = key;
      document.body.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', key);
    });
    chip.addEventListener('dragend', () => {
      dragKey = null;
      document.body.classList.remove('dragging');
    });
  }
  pool.querySelector('#pool-reset').addEventListener('click', () => {
    try { localStorage.removeItem(LAYOUT_KEY); } catch (err) {}
    LAYOUT = getLayout();                               // 无存档 → 默认布局 + 新来源落位
    render();
    renderPool();
  });
}

function clearDropHints() {
  document.querySelectorAll('.cal-slot.drop-before, .cal-slot.drop-after')
    .forEach(x => x.classList.remove('drop-before', 'drop-after'));
  document.querySelectorAll('.row-drop.dragover, #pool.dragover')
    .forEach(x => x.classList.remove('dragover'));
}

/* 行间/末尾的「新行」投放条。targetKey 是该位置下一行的首个槽位 key，null = 末尾 */
function bindRowDrop(strip, targetKey) {
  strip.addEventListener('dragover', e => {
    if (dragKey === null) return;
    e.preventDefault();
    clearDropHints();
    strip.classList.add('dragover');
  });
  strip.addEventListener('dragleave', () => strip.classList.remove('dragover'));
  strip.addEventListener('drop', e => {
    e.preventDefault();
    if (dragKey === null) return;
    const key = dragKey;
    dragKey = null;
    document.body.classList.remove('dragging');
    moveSlot(key, targetKey ? { newRowBeforeKey: targetKey } : { newRowEnd: true });
    persist();
  });
}

/* 相邻槽位间的占比分隔条：拖动实时改两个槽位的内联宽度，松手按像素比换算回权重 */
function bindDivider(div, leftEl, rightEl) {
  div.addEventListener('pointerdown', e => {
    e.preventDefault();
    const startX = e.clientX;
    const lw0 = leftEl.offsetWidth, rw0 = rightEl.offsetWidth;
    const sum = lw0 + rw0;
    const minW = 4 * (CELL + GAP) + 60;   // 至少 4 列格子 + 标题余量，与 colsFor 的 max(4,…) 对齐
    const move = ev => {
      const lw = Math.max(minW, Math.min(sum - minW, lw0 + ev.clientX - startX));
      leftEl.style.width = lw + 'px';
      rightEl.style.width = (sum - lw) + 'px';
      div.style.left = (leftEl.offsetLeft + lw + (SLOT_GAP - 9) / 2) + 'px';
    };
    const up = () => {
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', up);
      const la = findSlot(leftEl.dataset.k), ra = findSlot(rightEl.dataset.k);
      if (!la || la[0] !== ra[0]) { render(); return; }
      const wSum = LAYOUT[la[0]][la[1]].w + LAYOUT[ra[0]][ra[1]].w;
      const lw = Math.round(wSum * leftEl.offsetWidth / sum * 100) / 100;
      LAYOUT[la[0]][la[1]].w = lw;
      LAYOUT[ra[0]][ra[1]].w = Math.round((wSum - lw) * 100) / 100;
      persist();
    };
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up);
  });
}

/* 编辑态覆盖层：每次 renderCalendar('view', …) 之后由 render() 调用重挂。
   槽位加拖动手柄与 − 徽标，行间加新行投放条，相邻槽位间加占比分隔条 */
function applyEditChrome() {
  if (!editing) return;
  const box = document.getElementById('view');
  const stack = box.querySelector('.cal-stack');
  if (!stack) return;                                 // 空态提示下没有可挂的
  const narrow = matchMedia('(max-width: 940px)').matches;

  stack.style.position = 'relative';                  // 投放条绝对定位的锚
  const rowEls = [...stack.querySelectorAll('.cal-row')];
  rowEls.forEach(rowEl => {
    rowEl.style.position = 'relative';                // 分隔条绝对定位的锚
    // 投放条悬浮在本行上边缘（上下行各盖一半），拖拽中才显示，不占纵向空间
    const strip = document.createElement('div');
    strip.className = 'row-drop';
    strip.innerHTML = '<span>＋ 新行</span>';
    strip.style.top = (rowEl.offsetTop - 9) + 'px';
    stack.appendChild(strip);
    bindRowDrop(strip, rowEl.querySelector('.cal-slot')?.dataset.k || null);
  });
  if (rowEls.length) {
    const last = rowEls[rowEls.length - 1];
    const endStrip = document.createElement('div');
    endStrip.className = 'row-drop';
    endStrip.innerHTML = '<span>＋ 新行</span>';
    endStrip.style.top = (last.offsetTop + last.offsetHeight - 9) + 'px';
    stack.appendChild(endStrip);
    bindRowDrop(endStrip, null);
  }

  for (const slotEl of stack.querySelectorAll('.cal-slot')) {
    const key = slotEl.dataset.k;
    slotEl.draggable = true;
    slotEl.addEventListener('dragstart', e => {
      dragKey = key;
      document.body.classList.add('dragging');        // 拖拽中才浮现新行投放条
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', key);
    });
    slotEl.addEventListener('dragend', () => {
      dragKey = null;
      document.body.classList.remove('dragging');
      clearDropHints();
    });
    slotEl.addEventListener('dragover', e => {
      if (dragKey === null || dragKey === key) return;
      e.preventDefault();
      clearDropHints();
      const r = slotEl.getBoundingClientRect();
      slotEl.classList.add(e.clientX < r.left + r.width / 2 ? 'drop-before' : 'drop-after');
    });
    slotEl.addEventListener('drop', e => {
      e.preventDefault();
      if (dragKey === null || dragKey === key) return;
      const r = slotEl.getBoundingClientRect();
      const before = e.clientX < r.left + r.width / 2;
      const drag = dragKey;
      dragKey = null;
      document.body.classList.remove('dragging');
      moveSlot(drag, before ? { beforeKey: key } : { afterKey: key });
      persist();
    });

    const x = document.createElement('button');
    x.className = 'slot-x';
    x.textContent = '−';
    x.dataset.tip = '移除到候补池';
    x.addEventListener('click', () => { removeSlot(key); persist(); });
    slotEl.appendChild(x);
  }

  if (narrow) return;                                 // 窄屏槽位整宽堆叠，占比无意义
  for (const rowEl of stack.querySelectorAll('.cal-row')) {
    const slots = [...rowEl.querySelectorAll('.cal-slot')];
    for (let i = 0; i < slots.length - 1; i++) {
      const div = document.createElement('div');
      div.className = 'slot-divider';
      div.dataset.tip = '拖动调占比';
      div.style.left = (slots[i].offsetLeft + slots[i].offsetWidth + (SLOT_GAP - 9) / 2) + 'px';
      rowEl.appendChild(div);
      bindDivider(div, slots[i], slots[i + 1]);
    }
  }
}

/* 一次性绑定：Layout 按钮 + 候补池的拖放（= 拖回池即移除）。
   #pool 元素常驻，监听器只挂这一次；内部 chip 每次 renderPool 重建重绑 */
document.getElementById('layout-btn').addEventListener('click', () => editing ? exitEdit() : enterEdit());
const poolEl = document.getElementById('pool');
poolEl.addEventListener('dragover', e => {
  if (dragKey === null || !findSlot(dragKey)) return; // 池内 chip 拖回池无意义
  e.preventDefault();
  clearDropHints();
  poolEl.classList.add('dragover');
});
poolEl.addEventListener('drop', e => {
  e.preventDefault();
  if (dragKey === null || !findSlot(dragKey)) return;
  const key = dragKey;
  dragKey = null;
  document.body.classList.remove('dragging');
  removeSlot(key);
  persist();
});
