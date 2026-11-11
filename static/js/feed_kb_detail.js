(() => {
  const panel = document.getElementById('feedKbData');
  if (!panel) return;
  const $ = id => document.getElementById(id);
  const endpoint = panel.dataset.url;
  const trackerMode = panel.dataset.mode === 'tracker';
  const state = {rowOffset: 0, summaryOffset: 0, rowToken: 0, summaryToken: 0, fields: [], visibleRows: [], selectedRows: new Set()};
  const editable = Boolean($('feedKbRecordActions') && window.trackerRecordEditor);
  const uiText = source => window.FOSI18n?.t(source) || source;
  const periodMode = () => $('feedKbGroup').value.startsWith('$period:');
  const periodScopeNote = '周期统计使用上方日期范围和已保存历史，对象值精确匹配；搜索、字段筛选及最新/历史切换不影响结果。空周期不补零；覆盖更新仅保留当前快照。';
  const formatTime = value => value ? new Date(value).toLocaleString(document.documentElement.lang || 'zh-CN') : '—';
  const valueText = value => {
    if (value === null || value === undefined || value === '') return '—';
    if (typeof value === 'object' && value.truncated) return `${value.text}…（原文 ${value.original_chars} 字）`;
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
  };
  const element = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  };
  const clear = node => node.replaceChildren();
  const optionSync = (select, fields, blankLabel) => {
    const selected = select.value;
    clear(select);
    if (blankLabel !== null) select.append(new Option(blankLabel, ''));
    fields.forEach(field => { const option = new Option(field, field); option.setAttribute('translate', 'no'); select.append(option); });
    if (fields.includes(selected)) select.value = selected;
  };

  function syncFields(fields) {
    if (JSON.stringify(state.fields) === JSON.stringify(fields)) return false;
    state.fields = fields;
    optionSync($('feedKbFilterField'), fields, '不限字段');
    // 「时间周期」下拉是模板里的固定五项（每 10 分钟 … 每月），这里不再重建，
    // 免得字段一变化就把周期选项挤下去。
    const selectedMeasure = $('feedKbMeasure').value;
    optionSync($('feedKbMeasure'), fields, null);
    const trendField = $('feedKbTrendField');
    const selectedTrendField = trendField.value;
    clear(trendField);
    trendField.append(new Option(trackerMode ? '文档名' : '来源 ID', trackerMode ? '$document_name' : '$source_id'));
    fields.forEach(field => { const option = new Option(field, field); option.setAttribute('translate', 'no'); trendField.append(option); });
    trendField.value = [trackerMode ? '$document_name' : '$source_id', ...fields].includes(selectedTrendField)
      ? selectedTrendField : (['景点', '设备', '标题'].find(field => fields.includes(field)) || trendField.options[0].value);
    if (trendField.value !== selectedTrendField) {
      $('feedKbTrendValue').value = '';
    }
    syncPeriodControls();
    const columnSelect = $('feedKbColumns');
    const selected = Number(columnSelect.value) || 0;
    clear(columnSelect);
    for (let start = 0; start < fields.length; start += 12) {
      columnSelect.append(new Option(`字段 ${start + 1}–${Math.min(start + 12, fields.length)} / ${fields.length}`, String(start)));
    }
    columnSelect.value = String(selected < fields.length ? selected : 0);
    $('feedKbColumnWrap').hidden = fields.length <= 12;
    return (periodMode() && trendField.value !== selectedTrendField)
      || ($('feedKbOperation').value !== 'count' && $('feedKbMeasure').value !== selectedMeasure);
  }

  function params(kind, offset) {
    const q = new URLSearchParams({
      kind, offset: String(offset), scope: $('feedKbScope').value,
      column_offset: $('feedKbColumns').value || '0',
      feed: $('feedKbFeed').value, q: $('feedKbSearch').value.trim(),
      field: $('feedKbFilterField').value, value: $('feedKbFilterValue').value.trim(),
      from: $('feedKbFrom').value, to: $('feedKbTo').value,
    });
    if (kind === 'summary') {
      q.set('group', $('feedKbGroup').value);
      q.set('op', $('feedKbOperation').value);
      q.set('measure', $('feedKbMeasure').value);
    } else if (kind === 'trend') {
      q.set('object_field', $('feedKbTrendField').value);
      q.set('object_value', $('feedKbTrendValue').value.trim());
      q.set('period', $('feedKbGroup').value.slice('$period:'.length));
      q.set('op', $('feedKbOperation').value);
      q.set('measure', $('feedKbMeasure').value);
    }
    return q;
  }

  async function request(kind, offset) {
    const response = await fetch(`${endpoint}?${params(kind, offset)}`, {headers: {'Accept': 'application/json'}});
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.message || `HTTP ${response.status}`);
    return data;
  }

  function showRun(data) {
    if (trackerMode) return;
    const run = data.latest_run;
    const success = data.latest_success;
    const labels = {completed: '采集成功', failed: '采集失败', running: '正在采集'};
    $('feedKbStatusHeading').textContent = run ? (labels[run.status] || run.status) : '尚未运行采集任务';
    $('feedKbRunMeta').textContent = run ? `${run.feed_name} · ${formatTime(run.finished_at || run.started_at)}${run.error ? ` · ${run.error}` : ''}` : '任务运行后，这里会显示最近一次状态。';
    const counts = run?.counts || {};
    const amount = Number(counts.created || 0) + Number(counts.updated || 0);
    $('feedKbRunCount').textContent = run?.status === 'completed' ? `本次写入 ${amount} 条` : '';
    $('feedKbLastSuccess').hidden = !success || run?.id === success.id;
    if (success && run?.id !== success.id) $('feedKbLastSuccess').textContent = `下方展示最近一次成功采集：${formatTime(success.finished_at)}。`;
    $('feedKbScopeNote').textContent = data.scope === 'latest'
      ? (success ? (data.latest_is_current ? '显示该数据源的当前记录（覆盖更新模式）。' : '显示最近一次成功采集的批次记录。') : '尚无成功采集记录。')
      : '显示已保存的全部历史记录；日期按记录更新时间筛选。';
  }

  function updateSelection() {
    if (!editable) return;
    const checks = [...document.querySelectorAll('.fkd-row-select')];
    const count = checks.filter(check => check.checked).length;
    $('feedKbSelectedCount').textContent = uiText(`已选 ${count} 条`);
    $('feedKbDeleteSelected').disabled = count === 0;
    $('feedKbSelectLoaded').checked = checks.length > 0 && count === checks.length;
    $('feedKbSelectLoaded').indeterminate = count > 0 && count < checks.length;
  }

  function recordName(record) {
    return record.document_name || record.values?.标题 || record.source_id || '数据记录';
  }

  function detailFields(record, values, list, fullRecord = false) {
    clear(list);
    Object.entries(values || {}).forEach(([label, value]) => {
      const pair = element('div');
      const head = element('div', 'fkd-detail-field-head');
      head.append(element('dt', 'notranslate', label));
      if (editable && fullRecord && state.fields.includes(label)) {
        const edit = element('button', 'fkd-field-edit', '编辑');
        edit.type = 'button'; edit.setAttribute('aria-label', uiText(`编辑字段：${label}`));
        edit.addEventListener('click', async () => {
          const changed = await window.trackerRecordEditor.open({
            rowId: record.row_id, recordName: recordName(record),
            fields: [{name: label, value}], selectedField: label,
            expectedUpdatedAt: record.updated_at,
          });
          if (changed) reload();
        });
        head.append(edit);
      }
      pair.append(head, element('dd', 'notranslate', valueText(value)));
      list.append(pair);
    });
  }

  function rowDetail(record, columnCount) {
    const detail = element('tr', 'fkd-detail');
    detail.hidden = true;
    const cell = element('td'); cell.colSpan = columnCount;
    const list = element('dl');
    detailFields(record, record.values, list);
    cell.append(list);
    if (editable) {
      const actions = element('div', 'fkd-detail-actions');
      const remove = element('button', 'btn sm danger', '删除记录');
      remove.type = 'button';
      remove.addEventListener('click', async () => {
        remove.disabled = true;
        const deleted = await window.trackerRecordEditor.remove([record.row_id], recordName(record));
        if (deleted) reload();
        else remove.disabled = false;
      });
      actions.append(remove); cell.append(actions);
    }
    detail.append(cell);
    return detail;
  }

  function renderRowHead(columns) {
    const line = element('tr');
    [trackerMode ? '文档' : '采集对象', ...columns, '更新时间'].forEach((label, index) => line.append(element('th', index > 0 && index <= columns.length ? 'notranslate' : '', label)));
    $('feedKbRowsHead').replaceChildren(line);
  }

  function trendSuggestions() {
    const field = $('feedKbTrendField').value;
    const list = $('feedKbTrendSuggestions'); clear(list);
    const values = new Set();
    for (const row of state.visibleRows) {
      const value = field === '$source_id' ? row.source_id
        : field === '$document_name' ? row.document_name : row.values?.[field];
      if (value !== null && value !== undefined && typeof value !== 'object' && String(value).trim()) {
        values.add(String(value).slice(0, 200));
      }
      if (values.size >= 100) break;
    }
    values.forEach(value => list.append(new Option(value, value)));
  }

  function renderRows(data, append) {
    const body = $('feedKbRowsBody');
    if (!append) { clear(body); renderRowHead(data.columns || data.fields || []); state.selectedRows.clear(); }
    state.visibleRows = append ? [...state.visibleRows, ...(data.rows || [])] : (data.rows || []);
    trendSuggestions();
    for (const record of data.rows || []) {
      const line = element('tr');
      const title = recordName(record);
      const nameCell = element('td', 'fkd-name-cell');
      if (editable) {
        const select = element('input', 'fkd-row-select');
        select.type = 'checkbox'; select.value = String(record.row_id);
        select.setAttribute('aria-label', uiText(`选择记录：${valueText(title)}`));
        select.addEventListener('change', () => {
          if (select.checked) state.selectedRows.add(record.row_id);
          else state.selectedRows.delete(record.row_id);
          updateSelection();
        });
        nameCell.append(select);
      }
      const toggle = element('button', 'fkd-expand notranslate', valueText(title));
      toggle.type = 'button'; toggle.setAttribute('aria-expanded', 'false');
      nameCell.append(toggle); line.append(nameCell);
      for (const field of data.columns || []) line.append(element('td', 'notranslate', valueText(record.values?.[field])));
      line.append(element('td', 'fkd-time', formatTime(record.updated_at)));
      const detail = rowDetail(record, (data.columns || []).length + 2);
      toggle.addEventListener('click', async () => {
        detail.hidden = !detail.hidden;
        toggle.setAttribute('aria-expanded', String(!detail.hidden));
        if (detail.hidden || detail.dataset.loaded) return;
        detail.dataset.loaded = 'loading';
        try {
          const response = await fetch(`${endpoint}?${new URLSearchParams({kind: 'record', row: String(record.row_id)})}`);
          const full = await response.json();
          if (!response.ok || !full.ok) throw new Error(full.message || `HTTP ${response.status}`);
          const list = detail.querySelector('dl');
          detailFields(record, full.values, list, true);
          detail.dataset.loaded = 'true';
        } catch (error) {
          delete detail.dataset.loaded;
          const list = detail.querySelector('dl'); clear(list);
          const pair = element('div'); pair.append(element('dt', '', '完整字段读取失败'), element('dd', '', error.message));
          list.append(pair);
        }
      });
      body.append(line, detail);
    }
    if (!body.children.length) {
      const line = element('tr'); const cell = element('td', 'fkd-empty', trackerMode ? '当前范围没有追踪记录。' : '当前范围没有采集记录。');
      cell.colSpan = (data.columns || []).length + 2; line.append(cell); body.append(line);
    }
    $('feedKbMatchCount').textContent = `${data.total_matched} 条匹配`;
    $('feedKbPageInfo').textContent = `已显示 ${Math.min(data.total_matched, data.next_offset ?? data.total_matched)} / ${data.total_matched} 条`;
    $('feedKbMore').hidden = !data.has_more || data.next_offset === null;
    state.rowOffset = data.next_offset;
    updateSelection();
  }

  function chart(groups, groupName, totalGroups) {
    const host = $('feedKbChart'); clear(host);
    const values = groups.map(group => Number(group.metrics?.value));
    const max = Math.max(0, ...values.filter(Number.isFinite).map(Math.abs));
    if (!groups.length || !max) { host.append(element('p', 'fkd-empty', '当前范围没有可绘制的数值。')); return; }
    for (const group of groups) {
      const numeric = Number(group.metrics?.value);
      const label = groupName ? valueText(group.by?.[groupName]) : '总体';
      const item = element('div', 'fkd-chart-row');
      item.append(element('span', 'fkd-chart-label notranslate', label));
      const rail = element('div', 'fkd-chart-rail');
      const bar = element('div', 'fkd-chart-bar');
      bar.style.width = `${Number.isFinite(numeric) ? Math.max(1, Math.abs(numeric) / max * 100) : 0}%`;
      rail.append(bar); item.append(rail, element('strong', '', Number.isFinite(numeric) ? valueText(group.metrics.value) : '—'));
      host.append(item);
    }
    if (totalGroups > groups.length) host.append(element('p', 'fkd-subnote', '图表显示已加载的分组；点击“显示更多分组”继续查看。'));
  }

  function renderSummary(data, append) {
    const groupName = $('feedKbGroup').value;
    const body = $('feedKbSummaryBody');
    $('feedKbSummaryTableWrap').setAttribute('aria-label', '汇总表，可横向滚动');
    $('feedKbChart').setAttribute('aria-label', '汇总统计图');
    if (!append) {
      clear(body);
      const line = element('tr');
      [groupName || '范围', '统计值', '参与计算的数值条数'].forEach(label => line.append(element('th', '', label)));
      $('feedKbSummaryHead').replaceChildren(line);
    }
    for (const group of data.groups || []) {
      const line = element('tr');
      const valid = group.valid_numeric_values?.value;
      [groupName ? valueText(group.by?.[groupName]) : '总体', valueText(group.metrics?.value), valid === undefined ? '—' : String(valid)]
        .forEach((value, index) => line.append(element('td', index === 0 && groupName ? 'notranslate' : '', value)));
      body.append(line);
    }
    if (!body.children.length) {
      const line = element('tr'); const cell = element('td', 'fkd-empty', '当前范围没有可汇总的记录。');
      cell.colSpan = 3; line.append(cell); body.append(line);
    }
    const groups = state.summaryGroups = append ? [...(state.summaryGroups || []), ...(data.groups || [])] : (data.groups || []);
    chart(groups, groupName, data.total_groups || 0);
    $('feedKbSummaryInfo').textContent = `匹配 ${data.total_matched} 条 · 已显示 ${groups.length} / ${data.total_groups || 0} 组`;
    $('feedKbAggregateNote').textContent = $('feedKbOperation').value === 'count'
      ? '记录数基于全部匹配记录。' : '数值统计排除空值和无法解析的内容；表格列出实际参与计算的条数。';
    $('feedKbSummaryMore').hidden = !data.has_more || data.next_offset === null;
    state.summaryOffset = data.next_offset;
  }

  const numberText = value => value === null || value === undefined ? '—'
    : new Intl.NumberFormat(document.documentElement.lang || 'zh-CN', {maximumFractionDigits: 8}).format(value);

  function drawTrend(points) {
    const host = $('feedKbChart'); clear(host);
    const valid = points.map((point, index) => ({index, value: Number(point.value)}))
      .filter(({index, value}) => points[index].value !== null && Number.isFinite(value));
    if (!valid.length) {
      host.append(element('p', 'fkd-empty', '当前范围没有可绘制的数值。'));
      return;
    }
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 900 260');
    svg.setAttribute('aria-hidden', 'true');
    const add = (tag, attrs, parent = svg) => {
      const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
      Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
      parent.append(node);
      return node;
    };
    const minValue = Math.min(...valid.map(item => item.value));
    const maxValue = Math.max(...valid.map(item => item.value));
    const padding = maxValue === minValue ? Math.max(1, Math.abs(maxValue) * 0.1) : 0;
    const low = minValue - padding, high = maxValue + padding;
    const x = index => 52 + index / Math.max(1, points.length - 1) * 822;
    const y = value => 216 - (value - low) / Math.max(1e-9, high - low) * 182;
    [0, 0.5, 1].forEach(fraction => {
      const ordinate = 216 - fraction * 182;
      add('line', {x1: 52, y1: ordinate, x2: 874, y2: ordinate, class: 'fkd-trend-grid'});
      const label = add('text', {x: 47, y: ordinate + 4, 'text-anchor': 'end', class: 'fkd-trend-axis'});
      label.textContent = numberText(low + (high - low) * fraction);
    });
    let segment = [];
    const flush = () => {
      if (segment.length > 1) add('polyline', {points: segment.join(' '), class: 'fkd-trend-line'});
      segment = [];
    };
    points.forEach((point, index) => {
      if (point.value === null || !Number.isFinite(Number(point.value))) { flush(); return; }
      segment.push(`${x(index)},${y(Number(point.value))}`);
    });
    flush();
    valid.forEach(({index, value}) => {
      const dot = add('circle', {cx: x(index), cy: y(value), r: points.length > 150 ? 2.5 : 4, class: 'fkd-trend-dot'});
      const title = add('title', {}, dot);
      title.textContent = `${points[index].label}: ${numberText(value)}`;
    });
    [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])].forEach(index => {
      const label = add('text', {x: x(index), y: 248,
        'text-anchor': index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle',
        class: 'fkd-trend-axis'});
      label.textContent = points[index].label;
    });
    host.append(svg);
  }

  function renderTrend(data) {
    const points = data.points || [];
    const body = $('feedKbSummaryBody'); clear(body);
    const headings = element('tr');
    ['周期', '统计值', '较上期变化', '变化率', '记录数', '有效数值']
      .forEach(label => headings.append(element('th', '', label)));
    $('feedKbSummaryHead').replaceChildren(headings);
    for (const [index, point] of points.entries()) {
      const row = element('tr');
      const change = point.change === null ? '—' : `${point.change > 0 ? '+' : ''}${numberText(point.change)}`;
      let rate = point.change_pct === null ? '—' : `${point.change_pct > 0 ? '+' : ''}${numberText(point.change_pct)}%`;
      if (point.comparison === 'zero_baseline') rate = '—（上期为 0）';
      else if (point.comparison === 'no_previous_value' && index > 0) rate = '—（上期无数据）';
      const cells = [
        point.label, numberText(point.value), change, rate,
        String(point.records), point.valid_numeric_values === null ? '—' : String(point.valid_numeric_values),
      ];
      cells.forEach((value, cellIndex) => row.append(element('td', cellIndex > 0 && cellIndex < 4
        ? 'fkd-trend-number' : '', value)));
      body.append(row);
    }
    if (!points.length) {
      const row = element('tr'), cell = element('td', 'fkd-empty', '该对象在当前日期范围内没有已完成记录。');
      cell.colSpan = 6; row.append(cell); body.append(row);
    }
    drawTrend(points);
    const count = points.reduce((sum, point) => sum + point.records, 0);
    $('feedKbSummaryInfo').textContent = `${data.object_value} · ${points.length} 个周期 · ${count} 条记录 · ${data.timezone}`;
    $('feedKbAggregateNote').textContent = periodScopeNote;
    $('feedKbSummaryMore').hidden = true;
    $('feedKbSummaryTableWrap').setAttribute('aria-label', '周期对比表，可横向滚动');
    $('feedKbChart').setAttribute('aria-label', '周期趋势图');
  }

  function syncPeriodControls() {
    const period = periodMode();
    $('feedKbTrendFieldWrap').hidden = !period;
    $('feedKbTrendValueWrap').hidden = !period;
    $('feedKbMeasureWrap').hidden = $('feedKbOperation').value === 'count';
  }

  function promptForObject() {
    const headings = element('tr');
    ['周期', '统计值', '较上期变化', '变化率', '记录数', '有效数值']
      .forEach(label => headings.append(element('th', '', label)));
    $('feedKbSummaryHead').replaceChildren(headings);
    const row = element('tr'), cell = element('td', 'fkd-empty', '输入对象值后应用分析。');
    cell.colSpan = 6; row.append(cell); $('feedKbSummaryBody').replaceChildren(row);
    clear($('feedKbChart'));
    $('feedKbChart').append(element('p', 'fkd-empty', '输入对象值后应用分析。'));
    $('feedKbSummaryInfo').textContent = '';
    $('feedKbSummaryMore').hidden = true;
    $('feedKbAggregateNote').textContent = periodScopeNote;
    $('feedKbSummaryTableWrap').setAttribute('aria-label', '周期对比表，可横向滚动');
    $('feedKbChart').setAttribute('aria-label', '周期趋势图');
  }

  async function loadRows(append = false) {
    const token = ++state.rowToken;
    $('feedKbError').hidden = true;
    $('feedKbMore').disabled = true;
    try {
      const data = await request('rows', append ? state.rowOffset : 0);
      if (token !== state.rowToken) return;
      const analysisChanged = Array.isArray(data.fields) && syncFields(data.fields);
      showRun(data); renderRows(data, append);
      if (analysisChanged) void loadAnalysis();
    } catch (error) {
      if (token === state.rowToken) { $('feedKbError').textContent = error.message; $('feedKbError').hidden = false; }
    } finally { if (token === state.rowToken) $('feedKbMore').disabled = false; }
  }

  async function loadAnalysis(append = false) {
    const token = ++state.summaryToken;
    const period = periodMode();
    if (period && !$('feedKbTrendValue').value.trim()) {
      $('feedKbApply').disabled = false;
      $('feedKbSummaryMore').disabled = false;
      promptForObject();
      return;
    }
    const apply = $('feedKbApply'); apply.disabled = true;
    $('feedKbSummaryMore').disabled = true;
    $('feedKbAggregateNote').textContent = period ? '正在计算周期趋势…' : '正在计算汇总…';
    try {
      const data = await request(period ? 'trend' : 'summary', period ? 0 : append ? state.summaryOffset : 0);
      if (token !== state.summaryToken) return;
      if (Array.isArray(data.fields) && syncFields(data.fields)) {
        void loadAnalysis();
        return;
      }
      if (period) renderTrend(data);
      else renderSummary(data, append);
    } catch (error) {
      if (token === state.summaryToken) {
        $('feedKbAggregateNote').textContent = error.message;
        $('feedKbSummaryInfo').textContent = '';
        $('feedKbSummaryMore').hidden = true;
        clear($('feedKbSummaryBody')); clear($('feedKbChart'));
        const headings = element('tr');
        (period ? ['周期', '统计值', '较上期变化', '变化率', '记录数', '有效数值']
          : [$('feedKbGroup').value || '范围', '统计值', '参与计算的数值条数'])
          .forEach(label => headings.append(element('th', '', label)));
        $('feedKbSummaryHead').replaceChildren(headings);
        const row = element('tr'), cell = element('td', 'fkd-empty', '分析暂不可用，请检查上方提示。');
        cell.colSpan = period ? 6 : 3; row.append(cell); $('feedKbSummaryBody').append(row);
        $('feedKbChart').append(element('p', 'fkd-empty', '分析暂不可用，请检查上方提示。'));
      }
    } finally {
      if (token === state.summaryToken) {
        apply.disabled = false;
        $('feedKbSummaryMore').disabled = false;
      }
    }
  }

  function reload() { void loadRows(); void loadAnalysis(); }
  $('feedKbFilters').addEventListener('submit', event => { event.preventDefault(); reload(); });
  $('feedKbReset').addEventListener('click', () => { $('feedKbFilters').reset(); reload(); });
  $('feedKbRefresh')?.addEventListener('click', reload);
  $('feedKbMore').addEventListener('click', () => void loadRows(true));
  if (editable) {
    $('feedKbSelectLoaded').addEventListener('change', event => {
      document.querySelectorAll('.fkd-row-select').forEach(check => {
        check.checked = event.target.checked;
        if (check.checked) state.selectedRows.add(Number(check.value));
        else state.selectedRows.delete(Number(check.value));
      });
      updateSelection();
    });
    $('feedKbDeleteSelected').addEventListener('click', async () => {
      $('feedKbDeleteSelected').disabled = true;
      const deleted = await window.trackerRecordEditor.remove([...state.selectedRows]);
      if (deleted) reload();
      else updateSelection();
    });
  }
  $('feedKbColumns').addEventListener('change', () => void loadRows());
  $('feedKbSummaryMore').addEventListener('click', () => void loadAnalysis(true));
  $('feedKbGroup').addEventListener('change', () => { syncPeriodControls(); void loadAnalysis(); });
  $('feedKbOperation').addEventListener('change', () => {
    syncPeriodControls();
    void loadAnalysis();
  });
  $('feedKbMeasure').addEventListener('change', () => void loadAnalysis());
  $('feedKbApply').addEventListener('click', () => void loadAnalysis());
  $('feedKbTrendValue').addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); void loadAnalysis(); }
  });
  $('feedKbTrendField').addEventListener('change', () => {
    $('feedKbTrendValue').value = '';
    trendSuggestions();
    void loadAnalysis();
  });
  document.addEventListener('fos:languagechange', syncPeriodControls);
  document.querySelectorAll('[data-fkd-view]').forEach(button => button.addEventListener('click', () => {
    const chartView = button.dataset.fkdView === 'chart';
    $('feedKbChart').hidden = !chartView;
    $('feedKbSummaryTableWrap').hidden = chartView;
    document.querySelectorAll('[data-fkd-view]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
  }));

  const edit = $('feedKbEdit');
  if (edit) {
    const dialog = $('feedKbEditDialog'), form = $('feedKbEditForm');
    edit.addEventListener('click', () => dialog.showModal());
    $('feedKbEditCancel').addEventListener('click', () => dialog.close());
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const save = $('feedKbEditSave'); save.disabled = true; $('feedKbEditError').hidden = true;
      try {
        const response = await fetch(endpoint.replace('feed-data/', 'rename/'), {
          method: 'POST', body: new FormData(form),
          headers: {'X-Requested-With': 'XMLHttpRequest'},
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.message || '保存失败');
        $('feedKbName').textContent = data.name;
        $('feedKbDescription').textContent = data.description || '自动采集的数据会在这里持续更新。';
        $('feedKbAccess').textContent = data.access_label || data.department;
        [...$('feedKbFeed').options].filter(option => option.value).forEach(option => { option.textContent = data.name; });
        document.title = `${data.name} · 自动采集知识库`;
        dialog.close();
        if (window.fosToast) fosToast('已保存', 'ok');
      } catch (error) { $('feedKbEditError').textContent = error.message; $('feedKbEditError').hidden = false; }
      finally { save.disabled = false; }
    });
  }
  syncPeriodControls();
  reload();
})();
