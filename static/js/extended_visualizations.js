/* Bounded, inert-data business visuals shared by chat and offline reports. */
(function(root) {
  'use strict';
  const TYPES = new Set(['progress', 'sparkline', 'heatmap', 'pareto', 'timeline']);
  const NS = 'http://www.w3.org/2000/svg';
  const COLOR = 'var(--erc-series-1,#3574d5)', SECOND = 'var(--erc-series-2,#18a48e)';
  const DIM = 'var(--dim,#63758a)';
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  function label(value, limit = 1200) {
    if (typeof value !== 'string' || value.length > limit) throw new TypeError('Invalid visualization label');
    return value;
  }
  function numeric(value, nullable = false) {
    if (nullable && value === null) return null;
    if (typeof value !== 'number' || !Number.isFinite(value)) throw new TypeError('Invalid visualization number');
    return value;
  }
  function labels(value, limit) {
    if (!Array.isArray(value) || !value.length || value.length > limit) throw new TypeError('Invalid visualization labels');
    return value.map(item => label(item, 4000));
  }
  function normalize(data, language) {
    if (!object(data) || !TYPES.has(data.type)) throw new TypeError('Invalid extended visualization type');
    const result = {type: data.type, title: label(data.title || ''), language: (language || data.language) === 'zh-hans' ? 'zh-hans' : 'en'};
    if (data.unit !== undefined) result.unit = label(data.unit, 80);
    if (data.meta !== undefined) {
      if (!object(data.meta)) throw new TypeError('Invalid visualization metadata');
      result.meta = {};
      for (const key of ['row_count', 'group_count', 'displayed_groups', 'omitted_groups', 'omitted_row_count',
        'point_count', 'displayed_points', 'omitted_points']) if (data.meta[key] !== undefined) {
        if (!Number.isSafeInteger(data.meta[key]) || data.meta[key] < 0) throw new TypeError('Invalid visualization count');
        result.meta[key] = data.meta[key];
      }
      if (data.meta.other_included !== undefined) {
        if (typeof data.meta.other_included !== 'boolean') throw new TypeError('Invalid visualization metadata');
        result.meta.other_included = data.meta.other_included;
      }
    }
    if (data.type === 'progress') {
      result.value = numeric(data.value, true); result.target = numeric(data.target);
      if (result.target <= 0 || result.value !== null && result.value < 0) throw new TypeError('A progress target must be positive and its value nonnegative');
      if (result.value !== null && !Number.isFinite(result.value / result.target * 100)) throw new TypeError('The progress percentage exceeds the numeric range');
      return result;
    }
    if (data.type === 'sparkline' || data.type === 'pareto') {
      result.labels = labels(data.labels, data.type === 'sparkline' ? 500 : 50);
      if (!Array.isArray(data.series) || data.series.length !== 1 || !object(data.series[0])
          || !Array.isArray(data.series[0].values) || data.series[0].values.length !== result.labels.length)
        throw new TypeError('The visualization requires one aligned series');
      const series = data.series[0];
      result.series = [{label: label(series.label || '', 4000), values: series.values.map(value => numeric(value, data.type === 'sparkline'))}];
      if (data.type === 'pareto') {
        if (result.series[0].values.some(value => value < 0)) throw new TypeError('Pareto values must be nonnegative');
        if (!Number.isFinite(result.series[0].values.reduce((sum, value) => sum + value, 0))) throw new TypeError('Pareto total exceeds the supported numeric range');
        const order = result.labels.map((name, index) => ({name, value: result.series[0].values[index], index}))
          .sort((a, b) => b.value - a.value || a.index - b.index);
        result.labels = order.map(item => item.name); result.series[0].values = order.map(item => item.value);
      } else if (data.x_values !== undefined) {
        if (!Array.isArray(data.x_values) || data.x_values.length !== result.labels.length) throw new TypeError('Invalid sparkline horizontal values');
        result.x_values = data.x_values.map(value => numeric(value));
        if (result.x_values.some((value, index) => index && value <= result.x_values[index - 1]))
          throw new TypeError('Sparkline horizontal values must increase');
      }
      return result;
    }
    if (data.type === 'heatmap') {
      result.x_labels = labels(data.x_labels, 32); result.y_labels = labels(data.y_labels, 32);
      if (new Set(result.x_labels).size !== result.x_labels.length || new Set(result.y_labels).size !== result.y_labels.length)
        throw new TypeError('Heatmap axis labels must be unique');
      if (!Array.isArray(data.cells) || data.cells.length > 1024) throw new TypeError('Invalid heatmap cells');
      const seen = new Set();
      result.cells = data.cells.map(cell => {
        if (!object(cell) || !Number.isSafeInteger(cell.x) || !Number.isSafeInteger(cell.y)
            || cell.x < 0 || cell.x >= result.x_labels.length || cell.y < 0 || cell.y >= result.y_labels.length)
          throw new TypeError('Invalid heatmap coordinate');
        const key = cell.x + ':' + cell.y;
        if (seen.has(key)) throw new TypeError('Duplicate heatmap coordinate');
        seen.add(key); return {x: cell.x, y: cell.y, value: numeric(cell.value, true)};
      });
      return result;
    }
    if (!Array.isArray(data.items) || data.items.length > 100) throw new TypeError('Invalid timeline items');
    result.items = data.items.map(item => {
      if (!object(item)) throw new TypeError('Invalid timeline item');
      const start = numeric(item.start), end = numeric(item.end);
      if (Math.abs(start) > 8640000000000000 || Math.abs(end) > 8640000000000000 || end < start)
        throw new TypeError('Timeline end must not precede start');
      const clean = {label: label(item.label, 4000), start, end};
      for (const key of ['group', 'start_label', 'end_label']) if (item[key] !== undefined) clean[key] = label(item[key], 4000);
      return clean;
    });
    return result;
  }
  function element(document, tag, className, text) {
    const item = document.createElement(tag); item.className = className;
    if (text !== undefined) item.textContent = String(text);
    return item;
  }
  function svgNode(document, tag, attrs, text) {
    const item = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs || {})) item.setAttribute(key, String(value));
    if (text !== undefined) item.textContent = String(text);
    return item;
  }
  const shorten = (value, count = 18) => value.length > count ? value.slice(0, count - 1) + '…' : value;
  function destroy(host) {
    host._ercCleanup?.(); delete host._ercCleanup;
    host._ercTooltip?.destroy(); delete host._ercTooltip;
  }
  function render(host, raw) {
    const data = normalize(raw), document = host.ownerDocument || root.document;
    const runtime = root.EnGenieReportCharts;
    if (!runtime?.createTooltip || !runtime?.format) throw new Error('The shared chart runtime is unavailable');
    destroy(host); host.replaceChildren();
    const zh = data.language === 'zh-hans', format = value => runtime.format(value, data.language);
    const withUnit = value => format(value) + (data.unit ? (data.unit === '%' ? '' : ' ') + data.unit : '');
    const tips = runtime.createTooltip(host, document); host._ercTooltip = tips;
    const empty = text => host.appendChild(element(document, 'p', 'erc-empty', text));
    function canvas(width, height, scroll = false) {
      const svg = svgNode(document, 'svg', {viewBox: `0 0 ${width} ${height}`, role: 'group', 'aria-label': data.title || data.type});
      if (scroll) { const viewport = element(document, 'div', 'erc-viz-scroll'); viewport.appendChild(svg); host.appendChild(viewport); }
      else host.appendChild(svg);
      return {svg, add(tag, attrs, text) { const node = svgNode(document, tag, attrs, text); svg.appendChild(node); return node; }};
    }
    if (data.type === 'progress') {
      const rootNode = element(document, 'div', 'erc-progress');
      const values = element(document, 'div', 'erc-progress-values');
      values.appendChild(element(document, 'span', 'erc-progress-value', withUnit(data.value)));
      values.appendChild(element(document, 'span', 'erc-progress-target', '/ ' + withUnit(data.target)));
      const track = element(document, 'div', 'erc-progress-track'), fill = element(document, 'div', 'erc-progress-fill');
      const percentage = data.value === null ? null : data.value / data.target * 100;
      fill.style.width = percentage === null ? '0%' : Math.max(0, Math.min(100, percentage)) + '%';
      fill.style.background = COLOR; track.appendChild(fill);
      const summary = (zh ? '实际' : 'Actual') + ': ' + withUnit(data.value) + '\n' + (zh ? '目标' : 'Target') + ': ' + withUnit(data.target)
        + (percentage === null ? '' : '\n' + format(percentage) + '%');
      tips.bind(track, summary);
      track.setAttribute('role', percentage === null ? 'img' : 'progressbar');
      track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', String(data.target));
      track.setAttribute('aria-valuetext', summary);
      if (data.value !== null) track.setAttribute('aria-valuenow', String(Math.min(data.value, data.target)));
      const note = element(document, 'div', 'erc-progress-percent', percentage === null ? (zh ? '暂无实际值' : 'Actual value unavailable') : format(percentage) + '%');
      rootNode.appendChild(values); rootNode.appendChild(track); rootNode.appendChild(note); host.appendChild(rootNode); return;
    }
    if (data.type === 'sparkline') {
      const values = data.series[0].values, valid = values.filter(value => value !== null);
      if (!valid.length) { empty(zh ? '没有可绘制的数值。' : 'No numeric values to plot.'); return; }
      const {add} = canvas(560, 165), left = 12, right = 548, top = 15, bottom = 126;
      const rawX = data.x_values || data.labels.map((_, index) => index);
      const xFactor = Math.max(1, ...rawX.map(Math.abs)), yFactor = Math.max(1, ...valid.map(Math.abs));
      const first = rawX[0] / xFactor, span = rawX.at(-1) / xFactor - first;
      const {low, high} = runtime.bounds(valid.map(value => value / yFactor), false);
      const x = index => span ? left + (rawX[index] / xFactor - first) / span * (right - left) : (left + right) / 2;
      const y = value => bottom - (value / yFactor - low) / (high - low) * (bottom - top);
      let points = [];
      const flush = () => { if (points.length > 1) add('path', {d: runtime.linePath(points, false), fill: 'none', stroke: COLOR, 'stroke-width': 2.2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round'}); points = []; };
      values.forEach((value, index) => {
        if (value === null) { flush(); return; }
        points.push([x(index), y(value)]);
      }); flush();
      values.forEach((value, index) => {
        if (value === null) return;
        add('circle', {cx: x(index), cy: y(value), r: 2.2, fill: COLOR, 'pointer-events': 'none'});
        const hit = add('circle', {cx: x(index), cy: y(value), r: 8, fill: 'transparent'});
        tips.bind(hit, data.labels[index] + '\n' + data.series[0].label + ': ' + withUnit(value));
      });
      add('text', {x: left, y: 151, class: 'erc-axis'}, shorten(data.labels[0], 26));
      if (data.labels.length > 1) add('text', {x: right, y: 151, class: 'erc-axis', 'text-anchor': 'end'}, shorten(data.labels.at(-1), 26));
      return;
    }
    if (data.type === 'heatmap') {
      const width = 560, left = 116, top = 72, cellWidth = (width - left - 14) / data.x_labels.length;
      const cellHeight = 24, height = top + data.y_labels.length * cellHeight + 12;
      const {add} = canvas(width, height, data.y_labels.length > 10);
      const values = data.cells.filter(cell => cell.value !== null).map(cell => cell.value), scale = Math.max(0, ...values.map(Math.abs));
      const cells = new Map(data.cells.map(cell => [cell.x + ':' + cell.y, cell.value]));
      data.x_labels.forEach((name, x) => add('text', {x: left + (x + .5) * cellWidth, y: top - 8,
        transform: `rotate(-35 ${left + (x + .5) * cellWidth} ${top - 8})`, 'text-anchor': 'start', class: 'erc-axis'}, shorten(name, 14)));
      data.y_labels.forEach((name, y) => {
        add('text', {x: left - 7, y: top + (y + .65) * cellHeight, 'text-anchor': 'end', class: 'erc-axis'}, shorten(name, 18));
        data.x_labels.forEach((xName, x) => {
          const value = cells.get(x + ':' + y) ?? null;
          const rect = add('rect', {x: left + x * cellWidth + 1, y: top + y * cellHeight + 1,
            width: Math.max(1, cellWidth - 2), height: cellHeight - 2, rx: 2,
            fill: value === null ? 'var(--surface-2,#edf2f8)' : value < 0 ? 'var(--erc-series-5,#de6583)' : COLOR,
            'fill-opacity': value === null ? 1 : .16 + .84 * (scale ? Math.abs(value) / scale : 0)});
          tips.bind(rect, xName + '\n' + name + ': ' + withUnit(value));
        });
      });
      const scaleNote = element(document, 'div', 'erc-viz-scale');
      scaleNote.textContent = values.length ? (zh ? '范围' : 'Range') + ': ' + withUnit(Math.min(...values)) + ' – ' + withUnit(Math.max(...values))
        : (zh ? '暂无数值' : 'No numeric values');
      host.appendChild(scaleNote); return;
    }
    if (data.type === 'pareto') {
      const values = data.series[0].values, total = values.reduce((sum, value) => sum + value, 0);
      if (!Number.isFinite(total)) throw new TypeError('Pareto total exceeds the supported numeric range');
      if (!total) { empty(zh ? '所有数值均为0，无法计算累计占比。' : 'All values are zero; cumulative percentages are undefined.'); return; }
      const {add} = canvas(560, 310), left = 48, right = 512, top = 20, bottom = 244;
      const maximum = Math.max(...values), slot = (right - left) / values.length, percentages = [];
      let cumulative = 0;
      values.forEach(value => { cumulative += value; percentages.push(cumulative / total * 100); });
      percentages[percentages.length - 1] = 100;
      for (let index = 0; index <= 4; index++) {
        const portion = index / 4, y = bottom - portion * (bottom - top);
        add('line', {x1: left, x2: right, y1: y, y2: y, class: 'erc-grid'});
        add('text', {x: left - 6, y: y + 3, 'text-anchor': 'end', class: 'erc-axis'}, format(maximum * portion));
        add('text', {x: right + 6, y: y + 3, class: 'erc-axis'}, format(portion * 100) + '%');
      }
      const referenceY = bottom - .8 * (bottom - top);
      add('line', {x1: left, x2: right, y1: referenceY, y2: referenceY, stroke: DIM, 'stroke-width': 1, 'stroke-dasharray': '4 4', opacity: .6});
      const points = [];
      values.forEach((value, index) => {
        const x = left + (index + .5) * slot, y = bottom - value / maximum * (bottom - top), cumulativeY = bottom - percentages[index] / 100 * (bottom - top);
        const bar = add('rect', {x: left + index * slot + slot * .14, y, width: Math.max(.5, slot * .72), height: Math.max(.5, bottom - y), rx: 2, fill: COLOR});
        const summary = data.labels[index] + '\n' + data.series[0].label + ': ' + withUnit(value) + '\n' + (zh ? '累计占比' : 'Cumulative') + ': ' + format(percentages[index]) + '%';
        tips.bind(bar, summary); points.push([x, cumulativeY]);
        if (index % Math.max(1, Math.ceil(values.length / 10)) === 0)
          add('text', {x, y: bottom + 17, 'text-anchor': 'end', transform: `rotate(-30 ${x} ${bottom + 17})`, class: 'erc-axis'}, shorten(data.labels[index], 13));
      });
      if (points.length > 1) add('path', {d: runtime.linePath(points, false), fill: 'none', stroke: SECOND, 'stroke-width': 2.2, 'pointer-events': 'none'});
      points.forEach((point, index) => {
        add('circle', {cx: point[0], cy: point[1], r: 2.2, fill: SECOND, 'pointer-events': 'none'});
        const hit = add('circle', {cx: point[0], cy: point[1], r: 8, fill: 'transparent'});
        tips.bind(hit, data.labels[index] + '\n' + (zh ? '累计占比' : 'Cumulative') + ': ' + format(percentages[index]) + '%');
      });
      const note = element(document, 'div', 'erc-viz-scale', (zh ? '柱：数值 · 线：累计占比 · 虚线：80%' : 'Bars: values · Line: cumulative share · Guide: 80%'));
      host.appendChild(note); return;
    }
    if (!data.items.length) { empty(zh ? '没有可显示的时间项。' : 'No timeline items to display.'); return; }
    const rawStart = Math.min(...data.items.map(item => item.start)), rawEnd = Math.max(...data.items.map(item => item.end));
    const padding = rawEnd === rawStart ? 1800000 : (rawEnd - rawStart) * .03;
    const low = Math.max(-8640000000000000, rawStart - padding), high = Math.min(8640000000000000, rawEnd + padding);
    const left = 155, right = 546, top = 36, rowHeight = 27, height = top + data.items.length * rowHeight + 25;
    const {add} = canvas(560, height, true), x = value => left + (value - low) / (high - low) * (right - left);
    const dateLabel = value => {
      const date = new Date(value);
      return high - low < 86400000 ? new Intl.DateTimeFormat(zh ? 'zh-CN' : 'en-US', {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC'}).format(date)
        : date.toISOString().slice(0, 10);
    };
    for (let index = 0; index <= 3; index++) {
      const stamp = low + index / 3 * (high - low), coordinate = x(stamp);
      add('line', {x1: coordinate, x2: coordinate, y1: top - 7, y2: height - 18, class: 'erc-grid'});
      add('text', {x: coordinate, y: 18, 'text-anchor': index === 0 ? 'start' : index === 3 ? 'end' : 'middle', class: 'erc-axis'}, dateLabel(stamp));
    }
    const groups = [...new Set(data.items.map(item => item.group || ''))];
    data.items.forEach((item, index) => {
      const y = top + index * rowHeight, color = `var(--erc-series-${groups.indexOf(item.group || '') % 8 + 1},#3574d5)`;
      add('text', {x: left - 10, y: y + 15, 'text-anchor': 'end', class: 'erc-axis'}, shorten(item.label, 24));
      let mark;
      if (item.end === item.start) {
        add('circle', {cx: x(item.start), cy: y + 10, r: 3, fill: color, 'pointer-events': 'none'});
        mark = add('circle', {cx: x(item.start), cy: y + 10, r: 8, fill: 'transparent'});
      } else mark = add('rect', {x: x(item.start), y: y + 2, width: Math.max(.5, x(item.end) - x(item.start)), height: 16, rx: 3, fill: color});
      tips.bind(mark, [item.label, item.group, (zh ? '开始' : 'Start') + ': ' + (item.start_label || new Date(item.start).toISOString()),
        (zh ? '结束' : 'End') + ': ' + (item.end_label || new Date(item.end).toISOString())].filter(Boolean).join('\n'));
    });
    add('text', {x: right, y: height - 2, 'text-anchor': 'end', class: 'erc-axis'}, 'UTC');
  }
  const api = {normalize, render, destroy, types: [...TYPES]};
  root.EnGenieExtendedVisualizations = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
