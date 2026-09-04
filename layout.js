/* ---------- 布局状态 ----------
   布局是用户状态，不再是代码常量：localStorage[LAYOUT_KEY] 存 [[{k, w}]...]，
   w 是行内宽度权重（占比）。候补池不存储——派生 = 有数据的来源 − 已放置的 key。
   PROFILES 里新增的来源不在布局中时，自动作为新行追加到末尾（保证一眼看到），
   不满意再进编辑态调整。 */
const LAYOUT_KEY = 'tdb-layout-v1';
const DEFAULT_LAYOUT = [
  [{ k: 'cco', w: 1 }],
  [{ k: 'codex', w: 1 }, { k: 'grok', w: 1 }],
  [{ k: 'ccs', w: 1 }, { k: 'kimi', w: 1 }],
];
const defaultLayout = () => DEFAULT_LAYOUT.map(row => row.map(s => ({ ...s })));

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
  let rows = null;
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) rows = parsed;              // 裸数组也按行布局读
      else if (parsed && Array.isArray(parsed.rows)) rows = parsed.rows;
    }
  } catch (err) { /* 存储损坏等同无存储，回落默认布局 */ }
  return healLayout(rows || defaultLayout());
}

function saveLayout(rows) {
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(rows)); } catch (err) { /* 隐私模式等写不进去就算了 */ }
}

/* 入口：加载 + 自愈 + 新来源自动落位；有追加才写回，避免无意义的存储写入 */
function getLayout() {
  const rows = loadLayout();
  const placed = new Set(rows.flat().map(s => s.k));
  let added = false;
  for (const p of PROFILES) {
    if (!placed.has(p.key)) { rows.push([{ k: p.key, w: 1 }]); added = true; }
  }
  if (added) saveLayout(rows);
  return rows;
}
