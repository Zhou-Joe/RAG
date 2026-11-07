// Downtime dashboard charts. Loaded on demand; ECharts is served from local static (no CDN).
const echarts = await import('./vendor/echarts-5.6.0/echarts.esm.min.mjs');

const SVG = 'http://www.w3.org/2000/svg';
const TOP = 10;
const t = text => globalThis.FOSI18n?.t(text) || text;
const html = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const eventSummary = (count, minutes) => html(t('事件')) + ' ' + number(count) + ' · ' + html(t('停机')) + ' ' + number(minutes) + ' ' + html(t('分钟'));

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function icon(name) {
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('class', 'ops-icon');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(SVG, 'use');
  use.setAttribute('href', '#ops-i-' + name);
  svg.append(use);
  return svg;
}

function tokens() {
  const style = getComputedStyle(document.documentElement);
  const read = (name, fallback) => style.getPropertyValue(name).trim() || fallback;
  return {
    text: read('--text', '#1f2533'), dim: read('--dim', '#5c6472'), mute: read('--mute', '#8a919e'),
    rule: read('--rule', '#e3e6eb'), surface: read('--surface', '#ffffff'), brand: read('--brand', '#3b7bf0'),
    ok: read('--ok', '#1f9d6b'), warn: read('--warn', '#d97706'), danger: read('--danger', '#d54941'),
    violet: read('--violet', '#7c4dbe'), font: read('--fb', 'system-ui, sans-serif'),
  };
}

function number(value, digits = 0) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString(window.FOSI18n?.locale || 'zh-CN', {maximumFractionDigits: digits});
}

const METRICS = [
  ['event_count', '停机事件', 'alert', 0],
  ['minutes', '停机分钟', 'clock', 0],
  ['average_minutes', '平均每次（分钟）', 'gauge', 1],
  ['ongoing_count', '尚未恢复', 'pulse', 0],
];

function change(current, previous) {
  if (previous == null || current == null) return null;
  const now = Number(current);
  const before = Number(previous);
  if (!Number.isFinite(now) || !Number.isFinite(before)) return null;
  if (before === 0) return now === 0 ? {direction: 'flat', text: '与上期持平'} : {direction: 'up', text: '上期为 0'};
  const ratio = (now - before) / before;
  if (Math.abs(ratio) < 0.005) return {direction: 'flat', text: '与上期持平'};
  return {direction: ratio > 0 ? 'up' : 'down', text: (ratio > 0 ? '+' : '') + number(ratio * 100, 1) + '%'};
}

export function statCards(totals, comparison) {
  const row = el('div', null, 'ops-dt-cards');
  const before = comparison?.totals;
  for (const [key, label, glyph, digits] of METRICS) {
    if (totals?.[key] === undefined) continue;
    const card = el('div', null, 'ops-dt-card');
    const head = el('div', null, 'ops-dt-card-head');
    head.append(icon(glyph), el('span', label));
    card.append(head, el('b', totals[key] == null ? '—' : number(totals[key], digits)));
    const delta = before ? change(totals[key], before[key]) : null;
    if (delta) {
      // More downtime is worse, so an increase is shown as a warning.
      const note = el('small', null, 'ops-dt-delta is-' + delta.direction);
      note.append(el('span', delta.direction === 'up' ? '▲' : delta.direction === 'down' ? '▼' : '■', 'ops-dt-arrow'),
        document.createTextNode(' ' + delta.text + ' · 上期 ' + (before[key] == null ? '—' : number(before[key], digits))));
      note.title = '上期 ' + comparison.start_date + ' 至 ' + comparison.end_date;
      card.append(note);
    }
    row.append(card);
  }
  return row;
}

function baseOption(theme) {
  return {
    animationDuration: 300,
    textStyle: {fontFamily: theme.font, color: theme.dim},
    tooltip: {
      confine: true, backgroundColor: theme.surface, borderColor: theme.rule, textStyle: {color: theme.text, fontSize: 12},
    },
    grid: {left: 8, right: 12, top: 34, bottom: 8, containLabel: true},
  };
}

function axisStyle(theme) {
  return {
    axisLine: {lineStyle: {color: theme.rule}}, axisTick: {show: false},
    axisLabel: {color: theme.dim, fontSize: 11}, splitLine: {lineStyle: {color: theme.rule, type: 'dashed'}},
  };
}

function dailyOption(summary, theme) {
  const days = summary.daily || [];
  const axis = axisStyle(theme);
  return {
    ...baseOption(theme),
    legend: {top: 0, left: 'center', formatter: name => t(name), textStyle: {color: theme.dim, fontSize: 11}, itemWidth: 12, itemHeight: 8},
    tooltip: {...baseOption(theme).tooltip, trigger: 'axis', axisPointer: {type: 'shadow'},
      formatter: items => html(items[0]?.axisValue ?? '') + items.map(item => '<br>' + html(t(item.seriesName)) + ': ' + number(item.value)).join('')},
    grid: {left: 8, right: 8, top: 34, bottom: days.length > 31 ? 40 : 8, containLabel: true},
    xAxis: {type: 'category', data: days.map(day => day.key), ...axis, splitLine: {show: false},
      axisLabel: {...axis.axisLabel, formatter: value => value.slice(5)}},
    yAxis: [
      {type: 'value', name: t('事件'), minInterval: 1, nameTextStyle: {color: theme.mute}, ...axis},
      {type: 'value', name: t('分钟'), nameTextStyle: {color: theme.mute}, ...axis, splitLine: {show: false}},
    ],
    dataZoom: days.length > 31 ? [{type: 'inside'}, {type: 'slider', height: 18, bottom: 6, borderColor: theme.rule}] : [],
    series: [
      {name: '事件数', type: 'bar', data: days.map(day => day.event_count), itemStyle: {color: theme.brand, borderRadius: [3, 3, 0, 0]},
        barMaxWidth: 22},
      {name: '停机分钟', type: 'line', yAxisIndex: 1, data: days.map(day => day.minutes), smooth: true, symbolSize: 5,
        itemStyle: {color: theme.warn}, lineStyle: {width: 2}},
    ],
  };
}

function rankOption(rows, theme, color, valueKey = 'event_count', valueName = '事件数') {
  const top = rows.slice(0, TOP).reverse();
  const axis = axisStyle(theme);
  return {
    ...baseOption(theme),
    tooltip: {...baseOption(theme).tooltip, trigger: 'axis', axisPointer: {type: 'shadow'},
      formatter: items => {
        const row = top[items[0].dataIndex];
        return html(t(row.label)) + '<br>' + eventSummary(row.event_count, row.minutes);
      }},
    grid: {left: 8, right: 36, top: 8, bottom: 8, containLabel: true},
    xAxis: {type: 'value', minInterval: valueKey === 'event_count' ? 1 : 0, ...axis},
    yAxis: {type: 'category', data: top.map(row => row.label), ...axis, splitLine: {show: false},
      axisLabel: {...axis.axisLabel, width: 120, overflow: 'truncate', formatter: value => t(value)}},
    series: [{name: valueName, type: 'bar', data: top.map(row => row[valueKey]), itemStyle: {color, borderRadius: [0, 3, 3, 0]},
      barMaxWidth: 16, label: {show: true, position: 'right', color: theme.dim, fontSize: 11,
        formatter: item => number(item.value)}}],
  };
}

function donutOption(rows, theme) {
  const palette = [theme.brand, theme.warn, theme.ok, theme.violet, theme.danger, theme.mute];
  const option = {
    ...baseOption(theme),
    color: palette,
    tooltip: {...baseOption(theme).tooltip, trigger: 'item',
      formatter: item => html(t(item.name)) + '<br>' + html(t('事件')) + ' ' + number(item.value) + ' (' + number(item.percent, 1) + '%)'},
    legend: {type: 'scroll', bottom: 0, formatter: name => t(name), textStyle: {color: theme.dim, fontSize: 11}, itemWidth: 10, itemHeight: 10},
    series: [{type: 'pie', radius: ['45%', '70%'], center: ['50%', '45%'], avoidLabelOverlap: true,
      itemStyle: {borderColor: theme.surface, borderWidth: 2},
      label: {color: theme.dim, fontSize: 11, formatter: item => t(item.name) + '\n' + number(item.percent, 1) + '%'},
      data: rows.map(row => ({name: row.label, value: row.event_count, key: row.key}))}],
  };
  // Narrow cards: slice labels would be clipped, the legend and tooltip carry the values.
  return {baseOption: option, media: [
    {option: {series: [{label: {show: true}, radius: ['45%', '70%']}]}},
    {query: {maxWidth: 420}, option: {series: [{label: {show: false}, radius: ['42%', '68%']}]}},
  ]};
}

function heatmapOption(summary, theme) {
  const cells = summary.heatmap || [];
  const days = (summary.daily || []).map(day => day.key);
  const totals = new Map();
  for (const cell of cells) totals.set(cell.key, (totals.get(cell.key) || 0) + cell.event_count);
  const keys = [...totals.keys()].sort((a, b) => totals.get(b) - totals.get(a)).slice(0, 15);
  const names = new Map(cells.map(cell => [cell.key, cell.label]));
  const data = cells.filter(cell => keys.includes(cell.key))
    .map(cell => [days.indexOf(cell.day), keys.indexOf(cell.key), cell.event_count, cell.minutes]);
  const axis = axisStyle(theme);
  return {
    ...baseOption(theme),
    tooltip: {...baseOption(theme).tooltip, formatter: item => html(t(names.get(keys[item.value[1]]))) + ' · ' + html(days[item.value[0]])
      + '<br>' + eventSummary(item.value[2], item.value[3])},
    grid: {left: 8, right: 8, top: 8, bottom: 44, containLabel: true},
    xAxis: {type: 'category', data: days, ...axis, splitArea: {show: false},
      axisLabel: {...axis.axisLabel, formatter: value => value.slice(5)}},
    yAxis: {type: 'category', data: keys.map(key => names.get(key)), ...axis,
      axisLabel: {...axis.axisLabel, width: 110, overflow: 'truncate', formatter: value => t(value)}},
    visualMap: {dimension: 2, min: 0, max: Math.max(1, ...data.map(item => item[2])), calculable: true, orient: 'horizontal',
      left: 'center', bottom: 0, itemHeight: 120, itemWidth: 10, textStyle: {color: theme.dim, fontSize: 11},
      // Theme surfaces can be translucent, so scale the brand colour's opacity rather than interpolating colours.
      inRange: {color: [theme.brand], opacity: [0.18, 1]}, controller: {inRange: {color: [theme.brand]}}},
    series: [{type: 'heatmap', data, itemStyle: {color: theme.brand, borderColor: theme.surface, borderWidth: 1},
      emphasis: {itemStyle: {borderColor: theme.text}}}],
  };
}

function describe(rows, unit = '次') {
  return rows.slice(0, 3).map(row => t(row.label) + ' ' + number(row.event_count) + ' ' + t(unit)).join(' · ');
}

/**
 * Draws the dashboard into `target`. `onDrill(kind, key, label)` filters the event table; charts keep the full scope.
 * Returns {dispose()}.
 */
export function renderCharts(target, summary, comparison, {onDrill, fileName = '停机统计'} = {}) {
  const charts = [];
  const cards = statCards(summary.totals, comparison);
  const grid = el('div', null, 'ops-dt-grid');
  target.append(cards, grid);

  function card(title, hint, build, {wide = false, height = 260, label = ''} = {}) {
    const box = el('section', null, 'ops-dt-chart' + (wide ? ' is-wide' : ''));
    const head = el('header');
    const titles = el('div');
    titles.append(el('h3', title));
    if (hint) titles.append(el('small', hint));
    const save = el('button', '下载 PNG', 'ops-row-open');
    save.type = 'button';
    save.title = '下载 ' + title + ' 图片';
    head.append(titles, save);
    const canvas = el('div', null, 'ops-dt-canvas');
    canvas.style.height = height + 'px';
    canvas.setAttribute('role', 'img');
    box.append(head, canvas);
    grid.append(box);
    const entry = {canvas, build, chart: null, title, label};
    save.addEventListener('click', () => {
      if (!entry.chart) return;
      const link = el('a');
      link.href = entry.chart.getDataURL({type: 'png', pixelRatio: 2, backgroundColor: tokens().surface});
      link.download = (fileName === '停机统计' ? t(fileName) : fileName) + '-' + t(title) + '.png';
      document.body.append(link);
      link.click();
      link.remove();
    });
    charts.push(entry);
    return entry;
  }

  const drill = (kind, rows) => item => {
    const row = rows[item.dataIndex];
    if (row && onDrill) onDrill(kind, row.key, row.label);
  };
  const attractions = summary.attractions || [];
  const codes = summary.codes || [];
  const reasons = summary.reasons || [];
  const assets = summary.assets || [];
  card('每日趋势', '柱：事件数 · 线：停机分钟；点击柱筛选当天事件', theme => dailyOption(summary, theme),
    {wide: true, height: 280, label: () => t('共 ' + (summary.daily || []).length + ' 天')}).onClick = drill('day', summary.daily || []);
  const topAttractions = attractions.slice(0, TOP).reverse();
  card('景点 Top 10', '按事件数；点击筛选', theme => rankOption(attractions, theme, tokens().brand),
    {label: () => describe(attractions)}).onClick = drill('attraction', topAttractions);
  card('停机代码分布', '点击扇区筛选', theme => donutOption(codes, theme), {label: () => describe(codes)}).onClick = drill('code', codes);
  const topReasons = reasons.slice(0, TOP).reverse();
  card('分类 Top 10', '按事件数；点击筛选', theme => rankOption(reasons, theme, tokens().violet),
    {label: () => describe(reasons)}).onClick = drill('reason', topReasons);
  const topAssets = assets.slice(0, TOP).reverse();
  card('设备（资产）Top 10', '按停机分钟' + (summary.asset_count ? '，共 ' + summary.asset_count + ' 台' : '') + '；点击筛选',
    theme => rankOption(assets, theme, tokens().warn, 'minutes', '停机分钟'),
    {label: () => assets.slice(0, 3).map(row => row.label + ' ' + number(row.minutes) + ' ' + t('分钟')).join(' · ')}).onClick = drill('asset', topAssets);
  if ((summary.heatmap || []).length) {
    card('日期 × 景点热力图', '事件数，前 15 个景点', theme => heatmapOption(summary, theme), {wide: true, height: 380});
  }

  function draw() {
    const theme = tokens();
    for (const entry of charts) {
      const label = typeof entry.label === 'function' ? entry.label() : entry.label;
      entry.canvas.setAttribute('aria-label', t(entry.title) + (label ? ': ' + label : ''));
      entry.chart?.dispose();
      entry.chart = echarts.init(entry.canvas, null, {renderer: 'canvas'});
      entry.chart.setOption(entry.build(theme));
      if (entry.onClick) {
        entry.chart.on('click', entry.onClick);
        entry.canvas.classList.add('is-clickable');
      }
    }
  }
  draw();
  const resize = new ResizeObserver(() => charts.forEach(entry => entry.chart?.resize()));
  charts.forEach(entry => resize.observe(entry.canvas));
  const theme = new MutationObserver(draw);
  theme.observe(document.documentElement, {attributes: true, attributeFilter: ['data-theme', 'class']});
  document.addEventListener('fos:languagechange', draw);
  return {
    dispose() {
      resize.disconnect();
      theme.disconnect();
      document.removeEventListener('fos:languagechange', draw);
      charts.forEach(entry => entry.chart?.dispose());
    },
  };
}
