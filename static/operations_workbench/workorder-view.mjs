const {RAW_TIME_PAIRS, fieldLabel, formatDateTime, formatField, formatHours, statusInfo} =
  await import('./presentation.mjs' + new URL(import.meta.url).search);
const t = text => globalThis.FOSI18n?.t(text) || text;

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function recordText(tag, text, className) {
  const node = el(tag, text, className);
  node.setAttribute('translate', 'no');
  return node;
}

function present(value) {
  return value != null && value !== '';
}

export function statusBadge(code) {
  const info = statusInfo(code);
  const badge = el('span', info.label, 'ops-status-badge is-' + info.tone);
  if (info.label !== info.code) badge.title = info.code;
  badge.dataset.status = info.code;
  return badge;
}

// Site-time value with the UTC original (when Maximo returns both) as a tooltip.
function timeText(record, key) {
  const raw = Object.keys(RAW_TIME_PAIRS).find(name => RAW_TIME_PAIRS[name] === key);
  const node = el('span', formatField(key, record[key]));
  if (raw && present(record[raw])) node.title = 'Maximo 原值（UTC）：' + formatField(raw, record[raw]);
  return node;
}

function group(title, open, ...content) {
  const section = el('details', null, 'ops-wo-group');
  section.open = open;
  section.append(el('summary', title), ...content);
  return section;
}

function fact(label, value, extra) {
  const item = el('div', null, 'ops-wo-fact');
  const shown = el('dd');
  shown.append(value instanceof Node ? value : document.createTextNode(value));
  if (extra) shown.append(el('small', extra));
  item.append(el('dt', label), shown);
  return item;
}

function link(text, onClick) {
  if (!present(text) || text === '—' || !onClick) return text;
  const anchor = el('button', text, 'ops-wo-link');
  anchor.type = 'button';
  anchor.addEventListener('click', onClick);
  return anchor;
}

const DESCRIPTION_KEYS = ['description_en', 'description_zh', 'long_description_en', 'long_description_zh',
  'jp_description_en', 'jp_description_zh'];
const TIMELINE = [
  ['report_local', '报告'], ['targstart_local', '目标开始'], ['schedstart_local', '计划开始'],
  ['actstart_local', '实际开始'], ['pmdue_local', 'PM 到期'], ['schedfinish_local', '计划完成'],
  ['targcomp_local', '目标完成'], ['actfinish_local', '实际完成'],
];
const HOURS = [['estdur', '预计工期'], ['estlabhrs', '预计人工工时'], ['cust_wrenchtime', '扳手工时']];
const SHOWN = new Set(['wonum', 'workorderid', 'status', 'worktype', 'persongroup', 'jpnum', 'assetnum',
  'asset_description', 'wopriority', 'parent', 'reportedby', 'failurecode', 'attraction_code', 'attraction_name_zh',
  'attraction_name_en', ...DESCRIPTION_KEYS, ...TIMELINE.map(([key]) => key), ...HOURS.map(([key]) => key),
  ...Object.keys(RAW_TIME_PAIRS)]);

function header(record, options) {
  const head = el('header', null, 'ops-wo-head');
  const title = el('div', null, 'ops-wo-title');
  title.append(el('h3', record.wonum || '—'), statusBadge(record.status));
  const place = [record.attraction_name_zh, record.attraction_name_en].filter(present).join(' ')
    || record.location_description || record.location;
  const subtitle = [place, options.worktypeLabel?.(record.worktype) || record.worktype, record.persongroup]
    .filter(present).join(' · ');
  head.append(title, el('p', subtitle || '—', 'ops-wo-subtitle'));
  if (options.queriedAt) head.append(el('small', '查询时间 ' + formatField('queried_at', options.queriedAt), 'ops-wo-queried'));
  if (options.actions?.length) {
    const actions = el('div', null, 'ops-wo-actions');
    for (const {label, onClick} of options.actions) {
      const action = el('button', label, 'ops-row-open');
      action.type = 'button';
      action.addEventListener('click', onClick);
      actions.append(action);
    }
    head.append(actions);
  }
  return head;
}

function keyFacts(record, options) {
  const grid = el('dl', null, 'ops-wo-facts');
  grid.append(
    fact('工单号', record.wonum || '—'),
    fact('工单 ID', formatField('workorderid', record.workorderid)),
    fact('作业计划', link(formatField('jpnum', record.jpnum), options.openJobplan), record.jp_description_zh || record.jp_description_en),
    fact('资产', formatField('assetnum', record.assetnum), record.asset_description),
    fact('优先级', present(record.wopriority) ? 'P' + record.wopriority : '—'),
    fact('父工单', link(formatField('parent', record.parent), options.openWorkorder && (() => options.openWorkorder(record.parent)))),
    fact('报告人', formatField('reportedby', record.reportedby)),
    fact('故障代码', formatField('failurecode', record.failurecode)),
  );
  return grid;
}

function descriptions(record) {
  const columns = el('div', null, 'ops-wo-columns');
  for (const [lang, name] of [['zh', '中文'], ['en', 'English']]) {
    const column = el('section', null, 'ops-wo-text');
    column.lang = lang === 'zh' ? 'zh-CN' : 'en';
    column.append(el('h4', name));
    for (const [key, label] of [['description_', '说明'], ['long_description_', '详细说明'], ['jp_description_', '作业计划']]) {
      const value = record[key + lang];
      if (!present(value)) continue;
      column.append(el('h5', label), recordText('p', String(value)));
    }
    if (column.children.length === 1) column.append(el('p', '—', 'ops-wo-empty'));
    columns.append(column);
  }
  return columns;
}

function timeline(record) {
  const points = TIMELINE.filter(([key]) => present(record[key]))
    .map(([key, label]) => ({key, label, sort: formatDateTime(record[key]) || String(record[key])}))
    .sort((a, b) => a.sort.localeCompare(b.sort));
  const wrap = el('div');
  if (points.length) {
    const list = el('ol', null, 'ops-wo-timeline');
    for (const point of points) {
      const item = el('li', null, point.key.startsWith('act') ? 'is-actual' : '');
      item.append(el('span', point.label, 'ops-wo-when'), timeText(record, point.key));
      list.append(item);
    }
    wrap.append(list);
  }
  const missing = TIMELINE.filter(([key]) => !present(record[key])).map(([, label]) => label);
  if (missing.length) {
    const note = el('p', '未设置：', 'ops-wo-empty');
    missing.forEach((label, index) => {
      if (index) note.append(document.createTextNode(' · '));
      note.append(el('span', label));
    });
    wrap.append(note);
  }
  return wrap;
}

function hours(record) {
  const row = el('div', null, 'ops-stats ops-wo-hours');
  for (const [key, label] of HOURS) {
    const metric = el('span', null, 'ops-stat');
    metric.append(el('b', present(record[key]) ? formatHours(record[key]) : '—'), document.createTextNode(label));
    row.append(metric);
  }
  return row;
}

function otherFields(record) {
  const facts = el('dl', null, 'ops-facts');
  for (const [key, value] of Object.entries(record)) {
    if (SHOWN.has(key) || value == null || value === '' || typeof value === 'object') continue;
    facts.append(el('dt', fieldLabel(key)), el('dd', formatField(key, value)));
  }
  return facts.children.length ? facts : el('p', '没有其他字段。', 'ops-wo-empty');
}

// Work-order tasks (wotask) and job-plan steps (jobtask) in one shape.
function normalizeTask(row) {
  const number = row.taskid ?? row.jptask;
  return {
    row,
    number: present(number) ? String(number) : '',
    order: Number.isFinite(Number(number)) ? Number(number) : Infinity,
    titleZh: row.description_zh || '',
    titleEn: row.description_en || row.description || '',
    longZh: row.long_description_zh || '',
    longEn: row.long_description_en || '',
    duration: row.estdur ?? row.taskduration,
    child: row.wonum || '',
    taskId: row.jobtaskid ?? '',
    status: row.status || '',
    finished: row.actfinish_local || '',
  };
}

function taskCard(task) {
  const card = el('details', null, 'ops-task');
  card.dataset.task = task.number;
  const summary = el('summary', null, 'ops-task-head');
  const main = el('div', null, 'ops-task-main');
  main.append(el('span', t('Task ' + (task.number || '—')), 'ops-task-no'),
    (task.titleZh || task.titleEn ? recordText : el)('strong', task.titleZh || task.titleEn || '（无标题）', 'ops-task-title'));
  const side = el('div', null, 'ops-task-side');
  if (task.status) side.append(statusBadge(task.status));
  if (present(task.duration) && Number(task.duration) > 0) side.append(el('span', formatHours(task.duration), 'ops-task-duration'));
  const ids = [task.child && '子工单 ' + task.child, present(task.taskId) && '任务 ID ' + task.taskId].filter(Boolean).join(' · ');
  if (ids) side.append(el('small', ids, 'ops-task-ids'));
  summary.append(main, side);
  card.append(summary);
  card.addEventListener('toggle', () => {
    if (!card.open || card.children.length > 1) return;
    const body = el('div', null, 'ops-task-body');
    const zh = el('div', null, 'ops-task-text');
    zh.lang = 'zh-CN';
    zh.append((task.longZh || task.titleZh ? recordText : el)('p', task.longZh || task.titleZh || '没有中文说明。'));
    const en = el('div', null, 'ops-task-text is-en');
    en.lang = 'en';
    en.hidden = true;
    if (task.titleEn) en.append(recordText('h5', task.titleEn));
    if (task.longEn) en.append(recordText('p', task.longEn));
    body.append(zh);
    if (task.titleEn || task.longEn) {
      const toggle = el('button', '显示英文', 'ops-row-open');
      toggle.type = 'button';
      toggle.setAttribute('aria-expanded', 'false');
      toggle.addEventListener('click', () => {
        en.hidden = !en.hidden;
        toggle.textContent = en.hidden ? '显示英文' : '隐藏英文';
        toggle.setAttribute('aria-expanded', String(!en.hidden));
      });
      body.append(toggle, en);
    }
    if (task.finished) body.append(el('p', '实际完成 ' + formatField('actfinish_local', task.finished), 'ops-task-finished'));
    card.append(body);
  });
  return card;
}

function taskTable(tasks) {
  const wrap = el('div', null, 'ops-table-wrap');
  const table = el('table', null, 'ops-table ops-task-table');
  const head = table.createTHead().insertRow();
  for (const label of ['任务号', '中文标题', '状态', '工期', '子工单', '实际完成']) head.append(el('th', label));
  const body = table.createTBody();
  for (const task of tasks) {
    const line = body.insertRow();
    line.dataset.task = task.number;
    line.insertCell().textContent = task.number || '—';
    const title = line.insertCell();
    title.textContent = task.titleZh || task.titleEn || '—';
    title.setAttribute('translate', 'no');
    line.insertCell().append(task.status ? statusBadge(task.status) : document.createTextNode('—'));
    line.insertCell().textContent = present(task.duration) ? formatHours(task.duration) : '—';
    line.insertCell().textContent = task.child || '—';
    line.insertCell().textContent = formatField('actfinish_local', task.finished);
  }
  wrap.append(table);
  return wrap;
}

function matches(task, word) {
  return [task.number, task.titleZh, task.titleEn, task.longZh, task.longEn, task.child, task.taskId, task.status]
    .some(value => String(value ?? '').toLowerCase().includes(word));
}

export function taskSection(rows) {
  const tasks = (Array.isArray(rows) ? rows : []).map(normalizeTask).sort((a, b) => a.order - b.order);
  const wrap = el('div', null, 'ops-tasks');
  if (!tasks.length) {
    wrap.append(el('p', '没有任务步骤。', 'ops-wo-empty'));
    return wrap;
  }
  const bar = el('div', null, 'ops-task-bar');
  const search = el('input');
  search.type = 'search';
  search.placeholder = '搜索任务号、标题或说明…';
  search.setAttribute('aria-label', '搜索任务');
  const count = el('span', '', 'ops-task-count');
  count.setAttribute('aria-live', 'polite');
  const expand = el('button', '全部展开', 'ops-row-open');
  const collapse = el('button', '全部收起', 'ops-row-open');
  const compact = el('button', '紧凑表格', 'ops-row-open');
  for (const control of [expand, collapse, compact]) control.type = 'button';
  compact.setAttribute('aria-pressed', 'false');
  bar.append(search, count, expand, collapse, compact);
  const list = el('div', null, 'ops-task-list');
  const cards = tasks.map(task => ({task, card: taskCard(task)}));
  let tableMode = false;
  function paint() {
    const word = search.value.trim().toLowerCase();
    const shown = cards.filter(({task}) => !word || matches(task, word));
    count.textContent = word ? '显示 ' + shown.length + ' / ' + tasks.length + ' 项' : '共 ' + tasks.length + ' 项';
    expand.disabled = collapse.disabled = tableMode;
    if (!shown.length) list.replaceChildren(el('p', '没有符合搜索条件的任务。', 'ops-wo-empty'));
    else list.replaceChildren(...(tableMode ? [taskTable(shown.map(({task}) => task))] : shown.map(({card}) => card)));
  }
  search.addEventListener('input', paint);
  expand.addEventListener('click', () => list.querySelectorAll('.ops-task').forEach(card => { card.open = true; }));
  collapse.addEventListener('click', () => list.querySelectorAll('.ops-task').forEach(card => { card.open = false; }));
  compact.addEventListener('click', () => {
    tableMode = !tableMode;
    compact.setAttribute('aria-pressed', String(tableMode));
    compact.textContent = tableMode ? '卡片视图' : '紧凑表格';
    paint();
  });
  wrap.append(bar, list);
  paint();
  return wrap;
}

function span(from, to) {
  const start = Date.parse(String(from).replace(' ', 'T'));
  const end = Date.parse(String(to).replace(' ', 'T'));
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return '';
  const minutes = Math.round((end - start) / 60000);
  const days = Math.floor(minutes / 1440);
  const hoursLeft = Math.floor((minutes % 1440) / 60);
  if (days) return days + ' 天' + (hoursLeft ? ' ' + hoursLeft + ' 小时' : '');
  if (hoursLeft) return hoursLeft + ' 小时' + (minutes % 60 ? ' ' + (minutes % 60) + ' 分' : '');
  return (minutes % 60) + ' 分钟';
}

export function statusTimeline(rows) {
  const history = (Array.isArray(rows) ? rows : []).filter(row => row && typeof row === 'object');
  if (!history.length) return el('p', '没有状态变更记录。', 'ops-wo-empty');
  const ordered = history.map((row, index) => ({row, index, when: row.changed_local || row.changedate || ''}))
    .sort((a, b) => String(b.when).localeCompare(String(a.when)) || b.index - a.index);
  const list = el('ol', null, 'ops-history');
  ordered.forEach(({row, when}, position) => {
    const item = el('li', null, position === 0 ? 'is-current' : '');
    const head = el('div', null, 'ops-history-head');
    head.append(statusBadge(row.status), el('time', formatField('changed_local', when)));
    const newer = ordered[position - 1];
    const held = newer ? span(when, newer.when) : '';
    if (held) head.append(el('small', '持续 ' + held, 'ops-history-held'));
    item.append(head);
    const meta = [row.changeby && '变更人 ' + row.changeby].filter(Boolean).join(' · ');
    if (meta) item.append(el('p', meta, 'ops-history-meta'));
    if (present(row.memo)) item.append(recordText('p', String(row.memo), 'ops-history-memo'));
    list.append(item);
  });
  return list;
}

/**
 * options: actions [{label, onClick}], openJobplan(), openWorkorder(wonum), worktypeLabel(code), queriedAt,
 * tasks, statusHistory
 */
export function workorderView(record, options = {}) {
  const view = el('article', null, 'ops-wo');
  const tasks = Array.isArray(options.tasks) ? options.tasks : [];
  const history = Array.isArray(options.statusHistory) ? options.statusHistory : [];
  view.append(
    header(record, options),
    keyFacts(record, options),
    group('描述', true, descriptions(record)),
    group('计划时间', true, timeline(record)),
    group('工时', true, hours(record)),
    group('任务步骤（' + tasks.length + '）', true, taskSection(tasks)),
    group('状态履历（' + history.length + '，最新在前）', true, statusTimeline(history)),
    group('其他字段', false, otherFields(record)),
  );
  return view;
}
