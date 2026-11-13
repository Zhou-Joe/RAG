/* Safe inline EnGenie charts, using the offline HTML report renderer. */
(function(root) {
  'use strict';
  const EXTENDED_TYPES = new Set(['progress', 'sparkline', 'heatmap', 'pareto', 'timeline']);
  const TYPES = new Set(['line', 'bar', 'pie', 'combo', 'area', 'scatter', 'doughnut', 'metric', ...EXTENDED_TYPES, 'table']);
  const MAX_CHARTS = 8, MAX_SERIES = 8, MAX_POINTS = 500, mounted = new Set(), sizes = new WeakMap(), tablePreferences = new WeakMap();
  const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  function text(value, max = 4000) {
    if (typeof value !== 'string' || value.length > max) throw new TypeError('Invalid chart label');
    return value;
  }
  function number(value, nullable = true) {
    if (nullable && value === null) return null;
    if ((typeof value !== 'number' && typeof value !== 'string') || value === '' ||
        typeof value === 'string' && !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value) ||
        !Number.isFinite(Number(value))) throw new TypeError('Invalid chart number');
    return Number(value);
  }
  function normalize(value, language) {
    if (!plain(value) || !TYPES.has(value.type)) throw new TypeError('Invalid chart type');
    if (EXTENDED_TYPES.has(value.type)) {
      if (!root.EnGenieExtendedVisualizations?.normalize) throw new TypeError('Extended visualization renderer unavailable');
      return root.EnGenieExtendedVisualizations.normalize(value, language);
    }
    if (value.type === 'table') {
      if (!root.EnGenieInteractiveTable?.normalize) throw new TypeError('Interactive table renderer unavailable');
      return root.EnGenieInteractiveTable.normalize(value, language);
    }
    if (value.type === 'metric') {
      const metric = {type: 'metric', title: text(value.title || '', 1200), value: number(value.value),
        language: (language || value.language) === 'zh-hans' ? 'zh-hans' : 'en'};
      for (const [key, limit] of [['unit', 80], ['subtitle', 500]])
        if (value[key] !== undefined) metric[key] = text(value[key], limit);
      if (value.quality !== undefined) {
        const quality = value.quality;
        if (!plain(quality) || !Number.isSafeInteger(quality.valid) || !Number.isSafeInteger(quality.records) ||
            quality.valid < 0 || quality.records < quality.valid) throw new TypeError('Invalid metric quality');
        metric.quality = {valid: quality.valid, records: quality.records};
        if (quality.missing !== undefined) {
          if (!Number.isSafeInteger(quality.missing) || quality.missing < 0 || quality.missing > quality.records)
            throw new TypeError('Invalid metric quality');
          metric.quality.missing = quality.missing;
        }
      }
      return metric;
    }
    if (!Array.isArray(value.labels) || !value.labels.length || value.labels.length > MAX_POINTS)
      throw new TypeError('Invalid chart points');
    const labels = value.labels.map(label => text(label));
    if (!Array.isArray(value.series) || !value.series.length || value.series.length > MAX_SERIES)
      throw new TypeError('Invalid chart series');
    const series = value.series.map((item, index) => {
      if (!plain(item) || !Array.isArray(item.values) || item.values.length !== labels.length)
        throw new TypeError('Invalid chart series values');
      const normalized = {label: text(item.label === undefined ? 'Series ' + (index + 1) : item.label),
        values: item.values.map(value => number(value))};
      if (value.type === 'combo') {
        if (!['line', 'bar'].includes(item.type) || !['left', 'right'].includes(item.axis || 'left'))
          throw new TypeError('Invalid combo series');
        normalized.type = item.type; normalized.axis = item.axis || 'left';
      }
      if (item.quality !== undefined) {
        if (!Array.isArray(item.quality) || item.quality.length !== labels.length)
          throw new TypeError('Invalid chart quality');
        normalized.quality = item.quality.map(quality => {
          if (quality === null || plain(quality) && quality.valid === null) return null;
          if (!plain(quality) || !Number.isInteger(quality.valid) || !Number.isInteger(quality.records) ||
              quality.valid < 0 || quality.records < quality.valid) throw new TypeError('Invalid chart quality');
          return {valid: quality.valid, records: quality.records};
        });
      }
      return normalized;
    });
    if (value.type === 'pie' || value.type === 'doughnut') {
      if (series.length !== 1 || series[0].values.some(value => value !== null && value < 0))
        throw new TypeError('Invalid chart distribution');
    }
    if (value.type === 'combo' && (!series.some(item => item.type === 'bar') || !series.some(item => item.type === 'line')))
      throw new TypeError('Combo charts require lines and bars');
    const options = {legend: true};
    if (value.options !== undefined && !plain(value.options)) throw new TypeError('Invalid chart options');
    for (const [key, option] of Object.entries(value.options || {})) {
      if (['legend', 'stacked', 'horizontal', 'smooth'].includes(key)) {
        if (typeof option !== 'boolean') throw new TypeError('Invalid chart option');
        options[key] = option;
      } else if (['y_label', 'right_y_label'].includes(key)) options[key] = text(option, 500);
    }
    if (options.horizontal && value.type !== 'bar' || options.stacked && !['bar', 'combo'].includes(value.type))
      throw new TypeError('Invalid chart layout');
    const result = {type: value.type, title: text(value.title || '', 1200), labels, series, options,
      language: (language || value.language) === 'zh-hans' ? 'zh-hans' : 'en'};
    if (value.x_values !== undefined) {
      if (!['scatter', 'line', 'area'].includes(value.type) || !Array.isArray(value.x_values) || value.x_values.length !== labels.length)
        throw new TypeError('Invalid chart horizontal values');
      result.x_values = value.x_values.map(value => number(value, false));
    }
    if (plain(value.meta)) {
      result.meta = {};
      for (const key of ['row_count', 'group_count', 'displayed_groups', 'omitted_groups', 'omitted_row_count',
        'point_count', 'displayed_points', 'omitted_points']) {
        if (value.meta[key] !== undefined) {
          if (!Number.isSafeInteger(value.meta[key]) || value.meta[key] < 0) throw new TypeError('Invalid chart count');
          result.meta[key] = value.meta[key];
        }
      }
      if (value.meta.other_included !== undefined) result.meta.other_included = value.meta.other_included === true;
      if (['value', 'change', 'change_pct'].includes(value.meta.system_metric)) {
        result.meta.system_metric = value.meta.system_metric;
        const names = result.language === 'zh-hans'
          ? {value: '数值', change: '变化量', change_pct: '变化率 (%)'}
          : {value: 'Value', change: 'Change', change_pct: 'Change (%)'};
        result.options.y_label = names[value.meta.system_metric];
        if (value.meta.system_summary === true)
          result.series.forEach(item => { item.label = names[value.meta.system_metric]; });
      }
    }
    return result;
  }
  function discard(state) {
    const preferences = tablePreferences.get(state.container) || new Map();
    for (const canvas of state.canvases) {
      if (canvas._eitState && canvas._engenieTableKey) preferences.set(canvas._engenieTableKey, {...canvas._eitState});
      canvas._ercTooltip?.destroy(); canvas._ercCleanup?.();
    }
    tablePreferences.set(state.container, preferences);
    state.region.remove(); mounted.delete(state);
  }
  function destroy(container) {
    for (const state of [...mounted])
      if (state.container === container || container?.contains?.(state.region)) discard(state);
  }
  function rowFor(bubble) {
    if (!bubble?.ownerDocument) return null;
    let row = Array.from(bubble.children).find(node => node.className?.split(/\s+/).includes('chat-visual-row'));
    if (!row) {
      row = bubble.ownerDocument.createElement('div'); row.className = 'chat-visual-row';
    }
    const answer = Array.from(bubble.children).find(node => node.className?.split(/\s+/).includes('answer'));
    if (answer) {
      if (row.nextSibling !== answer) bubble.insertBefore(row, answer);
    } else if (!row.parentNode) bubble.appendChild(row);
    return row;
  }
  function mount(container, charts, options = {}) {
    const items = Array.isArray(charts) ? charts : charts ? [charts] : [];
    destroy(container);
    for (const state of [...mounted]) if (state.region.isConnected === false) discard(state);
    if (!items.length || !root.EnGenieReportCharts?.render || items.length > MAX_CHARTS) return 0;
    const document = container.ownerDocument || root.document;
    const region = document.createElement('section');
    region.className = 'chat-chart-grid'; region.setAttribute('aria-label', options.language === 'zh-hans' ? '交互图表' : 'Interactive Charts');
    const expanded = sizes.get(container) || new Set(); sizes.set(container, expanded);
    const state = {container, region, canvases: [], charts: items, options: {...options}};
    let count = 0;
    for (const [index, item] of items.entries()) {
      let data;
      try { data = normalize(item, options.language); } catch (_error) { continue; }
      const card = document.createElement('figure');
      card.className = 'chat-chart-card notranslate chat-chart-' + data.type;
      card.setAttribute('translate', 'no');
      const header = document.createElement('figcaption'); header.className = 'chat-chart-header';
      const title = document.createElement('span'); title.className = 'chat-chart-title';
      title.textContent = data.title; header.appendChild(title);
      const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'chat-chart-size';
      const key = index + ':' + data.type + ':' + data.title;
      function setExpanded(value) {
        if (value) { expanded.add(key); card.classList.add('is-expanded'); }
        else { expanded.delete(key); card.classList.remove('is-expanded'); }
        const label = data.language === 'zh-hans' ? (value ? '收起' : '展开') : (value ? 'Collapse' : 'Expand');
        toggle.textContent = label; toggle.setAttribute('aria-label', label + (data.title ? ': ' + data.title : ''));
        toggle.setAttribute('aria-expanded', String(value));
        canvas._ercTooltip?.hide?.();
      }
      toggle.addEventListener('click', event => { event.stopPropagation?.(); setExpanded(!expanded.has(key)); });
      header.addEventListener('click', event => { if (event.target !== toggle) setExpanded(!expanded.has(key)); });
      header.appendChild(toggle); card.appendChild(header);
      const canvas = document.createElement('div'); canvas.className = 'chat-chart-canvas';
      if (data.type === 'table') {
        canvas._engenieTableKey = JSON.stringify([index, data.title, data.columns.map(column => column.key)]);
        const previous = tablePreferences.get(container)?.get(canvas._engenieTableKey);
        if (previous) canvas._eitState = {...previous};
      }
      card.appendChild(canvas); region.appendChild(card); state.canvases.push(canvas);
      setExpanded(expanded.has(key));
      try { root.EnGenieReportCharts.render(canvas, data); } catch (_error) {
        canvas._ercTooltip?.destroy(); canvas._ercCleanup?.(); card.remove(); state.canvases.pop(); continue;
      }
      const omitted = data.meta?.omitted_groups || data.meta?.omitted_points || 0;
      if (omitted) {
        const note = document.createElement('p'); note.className = 'chat-chart-note';
        note.textContent = data.language === 'zh-hans'
          ? (data.meta?.other_included ? '另有 ' + omitted + ' 组合并为“其他”。' : '另有 ' + omitted + ' 个分组或数据点未绘制。')
          : (data.meta?.other_included ? omitted + ' additional groups combined into Other.' : omitted + ' additional groups or points are not plotted.');
        card.appendChild(note);
      }
      count++;
    }
    if (count) { container.appendChild(region); mounted.add(state); } else region.remove();
    return count;
  }
  root.document?.addEventListener('fos:languagechange', event => {
    const language = event.detail?.language;
    if (!['en', 'zh-hans'].includes(language)) return;
    for (const state of [...mounted])
      mount(state.container, state.charts, {...state.options, language});
  });

  // Runs only inside the existing local Python sandbox. Capture actual artist data;
  // unsupported/oversized figures retain the existing PNG fallback.
  const pythonCaptureSetup = String.raw`
import json as _eg_json, math as _eg_math
from numbers import Real as _eg_Real
import matplotlib.pyplot as _eg_plt
from matplotlib.figure import Figure as _eg_Figure
from matplotlib.axes import Axes as _eg_Axes
from matplotlib.container import BarContainer as _eg_BarContainer
from matplotlib.collections import PathCollection as _eg_PathCollection
from matplotlib.ticker import FixedLocator as _eg_FixedLocator
if not hasattr(_eg_plt, '_engenie_originals'):
    _eg_plt._engenie_originals = (_eg_Figure.savefig, _eg_plt.show, _eg_plt.close, _eg_Axes.pie)
_eg_save, _eg_show, _eg_close, _eg_pie = _eg_plt._engenie_originals
_eg_close('all')
_eg_captured = {}
_eg_metrics = []

def engenie_metric(title, value, unit='', subtitle=''):
    """Show a single actual scalar as an interactive EnGenie value card."""
    if not isinstance(title, str) or not title.strip() or len(title) > 1200:
        raise ValueError('A metric title must be nonempty text of up to 1200 characters')
    if not isinstance(unit, str) or len(unit) > 80 or not isinstance(subtitle, str) or len(subtitle) > 500:
        raise ValueError('Invalid metric unit or subtitle')
    if value is not None:
        if isinstance(value, bool):
            raise ValueError('A metric value must be a finite number or None')
        try:
            value = float(value)
        except (ValueError, TypeError, OverflowError):
            raise ValueError('A metric value must be a finite number or None') from None
        if not _eg_math.isfinite(value):
            raise ValueError('A metric value must be a finite number or None')
    if len(_eg_metrics) >= 8:
        raise ValueError('At most eight metric cards are supported')
    _eg_metrics.append({'type': 'metric', 'title': title, 'value': value, 'unit': unit, 'subtitle': subtitle})
def _eg_number(value):
    try:
        result = float(value)
        return result if _eg_math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None
def _eg_text(value):
    if hasattr(value, 'isoformat'):
        value = value.isoformat()
    result = str(value)
    if len(result) > 4000:
        raise ValueError('Chart label is too long')
    return result
def _eg_uniform(values):
    deltas = [b-a for a, b in zip(values, values[1:])]
    return not deltas or all(_eg_math.isclose(delta, deltas[0], rel_tol=1e-8, abs_tol=1e-8) for delta in deltas)
def _eg_label(axis, value, bar=False):
    if isinstance(value, str):
        return value
    try:
        converted = float(axis.convert_xunits(value))
        if not _eg_math.isfinite(converted):
            raise RuntimeError('Unsupported missing horizontal coordinate')
        converter = axis.xaxis.get_converter() if hasattr(axis.xaxis, 'get_converter') else axis.xaxis.converter
        if converter is not None and 'date' in type(converter).__name__.lower():
            import matplotlib.dates as _eg_dates
            return _eg_dates.num2date(converted).isoformat()
        ticks, texts = axis.get_xticks(), axis.get_xticklabels()
        category_axis = converter is not None and 'category' in type(converter).__name__.lower()
        fixed_labels = isinstance(axis.xaxis.get_major_locator(), _eg_FixedLocator)
        if len(ticks) and len(ticks) == len(texts) and (category_axis or fixed_labels):
            nearest = min(range(len(ticks)), key=lambda index: abs(float(ticks[index]) - converted))
            step = min((abs(float(ticks[i+1]) - float(ticks[i])) for i in range(len(ticks)-1)), default=1)
            label = texts[nearest].get_text()
            tolerance = max(1e-8, step * .48) if bar else 1e-8
            if label and abs(float(ticks[nearest]) - converted) < tolerance:
                return _eg_text(label)
        if converted.is_integer():
            return str(int(converted))
    except (ValueError, TypeError, OverflowError):
        pass
    return _eg_text(value)
def _eg_figure_charts(figure):
    figure.canvas.draw()
    groups = {}
    for axis in figure.axes:
        if axis.get_visible():
            if axis.name != 'rectilinear' or axis.get_xscale() != 'linear' or axis.get_yscale() != 'linear' or axis.images:
                raise ValueError('Unsupported chart axes')
            if any(not isinstance(item, _eg_PathCollection) for item in axis.collections):
                raise ValueError('Unsupported chart artists')
            groups.setdefault(tuple(round(value, 5) for value in axis.get_position().bounds), []).append(axis)
    charts = []
    for axes in groups.values():
        primary = axes[0]
        title = _eg_text(primary.get_title() or (figure._suptitle.get_text() if figure._suptitle else ''))
        pies = getattr(primary, '_engenie_pies', [])
        if pies:
            pie = pies[-1]
            if len(axes) > 1 or primary.lines or primary.containers or primary.collections:
                raise ValueError('Unsupported mixed pie chart')
            if len(primary.patches) != len(pie['values']):
                raise ValueError('Unsupported pie overlays')
            if len(pie['values']) > 500:
                continue
            charts.append({'type': pie['type'], 'title': title, 'labels': pie['labels'],
                           'series': [{'label': primary.get_ylabel() or title or 'Value', 'values': pie['values']}],
                           'options': {'legend': True}})
            continue
        items, categories, horizontal = [], [], False
        for axis_index, axis in enumerate(axes):
            allowed_patches = {id(patch) for bars in axis.containers if isinstance(bars, _eg_BarContainer) for patch in bars.patches}
            if any(id(patch) not in allowed_patches for patch in axis.patches):
                raise ValueError('Unsupported chart patches')
            side = 'right' if axis_index else 'left'
            for line in axis.lines:
                if line.get_visible() and line.get_linestyle() not in ('None', 'none', '', ' '):
                    if line.get_transform() != axis.transData or line.get_drawstyle() != 'default':
                        raise ValueError('Unsupported line geometry')
                    xs, ys = list(line.get_xdata()), list(line.get_ydata())
                    if len(xs) != len(ys) or len(xs) > 500:
                        raise ValueError('Unsupported chart point count')
                    label = line.get_label()
                    part = {'label': _eg_text(label if label and not label.startswith('_') else axis.get_ylabel() or 'Series ' + str(len(items)+1)),
                            'points': [(_eg_label(axis, x), _eg_number(y)) for x, y in zip(xs, ys)],
                            'type': 'line', 'axis': side}
                    if all(isinstance(x, _eg_Real) for x in xs):
                        numeric = [float(x) for x in xs]
                        if not (all(b >= a for a, b in zip(numeric, numeric[1:])) or all(b <= a for a, b in zip(numeric, numeric[1:]))):
                            raise ValueError('Unsupported nonmonotonic line coordinates')
                        part['numeric_x'] = dict(zip((point[0] for point in part['points']), numeric))
                    elif xs and not isinstance(xs[0], str):
                        # Date coordinates need date-axis formatting not offered by this runtime.
                        if not _eg_uniform([float(axis.convert_xunits(x)) for x in xs]):
                            raise ValueError('Unsupported irregular date coordinates')
                    items.append(part)
            for bars in axis.containers:
                if not isinstance(bars, _eg_BarContainer):
                    continue
                is_horizontal = bars.orientation == 'horizontal'
                if is_horizontal and (items or len(axes) > 1):
                    raise ValueError('Unsupported mixed horizontal chart')
                horizontal, points, coordinates = is_horizontal, [], []
                for patch in bars.patches:
                    if abs(patch.get_x() if is_horizontal else patch.get_y()) > 1e-12:
                        raise ValueError('Unsupported stacked or floating bars')
                    if is_horizontal:
                        center, value = patch.get_y() + patch.get_height()/2, patch.get_width()
                        ticks, texts = axis.get_yticks(), axis.get_yticklabels()
                        nearest = min(range(len(ticks)), key=lambda index: abs(float(ticks[index])-center)) if len(ticks) else -1
                        category = texts[nearest].get_text() if nearest >= 0 and abs(float(ticks[nearest])-center) < .49 and texts[nearest].get_text() else _eg_text(center)
                    else:
                        center = patch.get_x() + patch.get_width()/2
                        category, value = _eg_label(axis, center, bar=True), patch.get_height()
                        coordinates.append(center)
                    points.append((_eg_text(category), _eg_number(value)))
                if len(points) > 500:
                    raise ValueError('Unsupported chart point count')
                label = bars.get_label()
                converter = axis.xaxis.get_converter() if hasattr(axis.xaxis, 'get_converter') else axis.xaxis.converter
                if not is_horizontal and converter is None and not isinstance(axis.xaxis.get_major_locator(), _eg_FixedLocator) and not _eg_uniform(coordinates):
                    raise ValueError('Unsupported irregular bar spacing')
                items.append({'label': _eg_text(label if label and not label.startswith('_') else axis.get_ylabel() or 'Series ' + str(len(items)+1)),
                              'points': points, 'type': 'bar', 'axis': side})
            for collection in axis.collections:
                if not isinstance(collection, _eg_PathCollection) or not collection.get_visible():
                    continue
                offsets = collection.get_offsets()
                if len(offsets) > 500:
                    raise ValueError('Unsupported chart point count')
                label = collection.get_label()
                items.append({'label': _eg_text(label if label and not label.startswith('_') else axis.get_ylabel() or 'Series ' + str(len(items)+1)),
                              'points': [(_eg_text(float(x)), _eg_number(y)) for x, y in offsets], 'type': 'scatter', 'axis': side})
        if not items:
            continue
        if len(items) > 8 or horizontal and any(item['type'] != 'bar' for item in items):
            raise ValueError('Unsupported chart series')
        kinds = {item['type'] for item in items}
        if 'scatter' in kinds and len(kinds) > 1:
            raise ValueError('Unsupported mixed scatter chart')
        kind = 'combo' if kinds == {'line', 'bar'} else next(iter(kinds))
        if kind == 'combo' and any('numeric_x' in item and not _eg_uniform(list(item['numeric_x'].values())) for item in items):
            raise ValueError('Unsupported irregular combo spacing')
        for item in items:
            seen = set()
            for category, _value in item['points']:
                if category in seen:
                    raise ValueError('Duplicate chart categories')
                seen.add(category)
                if category not in categories:
                    categories.append(category)
        if len(categories) > 500:
            raise ValueError('Unsupported chart point count')
        numeric_x = None
        if kind == 'line' and all('numeric_x' in item for item in items):
            mapping = {key: value for item in items for key, value in item['numeric_x'].items()}
            categories.sort(key=mapping.get)
            numeric_x = [mapping[category] for category in categories]
        if kind == 'scatter':
            categories.sort(key=float)
        series = []
        for item in items:
            mapping = dict(item['points'])
            series.append({'label': item['label'], 'values': [mapping.get(category) for category in categories],
                           'type': item['type'], 'axis': item['axis']})
        options = {'legend': True, 'y_label': _eg_text(primary.get_ylabel()), 'horizontal': horizontal}
        if len(axes) > 1:
            options['right_y_label'] = _eg_text(axes[1].get_ylabel())
        if len(axes) > 1 and kind != 'combo':
            raise ValueError('Unsupported dual-axis chart')
        chart = {'type': kind, 'title': title, 'labels': categories, 'series': series, 'options': options}
        if kind == 'scatter':
            chart['x_values'] = [float(category) for category in categories]
        elif numeric_x is not None:
            chart['x_values'] = numeric_x
        charts.append(chart)
    return charts
def _eg_capture(figure):
    try:
        charts = _eg_figure_charts(figure)
        if charts:
            _eg_captured[id(figure)] = charts
        else:
            _eg_captured.pop(id(figure), None)
    except Exception:
        _eg_captured.pop(id(figure), None)
def _eg_savefig(figure, *args, **kwargs):
    _eg_capture(figure)
    return _eg_save(figure, *args, **kwargs)
def _eg_showfig(*args, **kwargs):
    for index in _eg_plt.get_fignums():
        _eg_capture(_eg_plt.figure(index))
    return _eg_show(*args, **kwargs)
def _eg_closefig(figure=None):
    try:
        figures = list(_eg_plt.get_fignums()) if figure == 'all' else [figure if isinstance(figure, _eg_Figure) else _eg_plt.gcf() if figure is None else _eg_plt.figure(figure)]
        for item in figures:
            _eg_capture(_eg_plt.figure(item) if isinstance(item, int) else item)
    except Exception:
        pass
    return _eg_close(figure)
def _eg_piechart(axis, values, *args, **kwargs):
    result = _eg_pie(axis, values, *args, **kwargs)
    labels = kwargs.get('labels')
    if labels is None and len(args) > 1:
        labels = args[1]
    clean = [_eg_number(value) for value in values]
    if all(value is not None and value >= 0 for value in clean):
        axis._engenie_pies = [{'type': 'doughnut' if kwargs.get('wedgeprops', {}).get('width') else 'pie',
            'values': clean, 'labels': [_eg_text(label) for label in labels] if labels is not None else [str(index + 1) for index in range(len(clean))]}]
    return result
_eg_Figure.savefig = _eg_savefig
_eg_plt.show = _eg_showfig
_eg_plt.close = _eg_closefig
_eg_Axes.pie = _eg_piechart
`;
  const pythonCaptureRead = String.raw`
for _eg_index in _eg_plt.get_fignums():
    _eg_capture(_eg_plt.figure(_eg_index))
_eg_result = _eg_metrics + [chart for charts in _eg_captured.values() for chart in charts]
_eg_json.dumps(_eg_result if len(_eg_result) <= 8 else [], ensure_ascii=True, allow_nan=False)
`;
  const api = {normalize, mount, destroy, rowFor, pythonCaptureSetup, pythonCaptureRead};
  root.EnGenieChatVisualizations = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
