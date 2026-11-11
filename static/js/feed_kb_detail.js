(() => {
  const panel = document.getElementById('feedKbData');
  if (!panel) return;
  const $ = id => document.getElementById(id);
  const endpoint = panel.dataset.url;
  const trackerMode = panel.dataset.mode === 'tracker';
  const state = {rowOffset: 0, summaryOffset: 0, rowToken: 0, summaryToken: 0, fields: [],
    visibleRows: [], selectedRows: new Set(), objects: [], trendSeries: [], trendMeta: {},
    hiddenObjects: new Set(), numericFields: [], objectChoices: [], objectOffset: 0,
    optionsToken: 0, optionsLoading: false, analysisLoading: false, optionsError: false};
  const MAX_SERIES = 8;
  const SERIES_COLORS = ['#2f6f9f', '#c2410c', '#3d7a3f', '#7b3fa0', '#a16207', '#0f766e', '#b91c1c', '#334155'];
  const editable = Boolean($('feedKbRecordActions') && window.trackerRecordEditor);
  const uiText = source => window.FOSI18n?.t(source) || source;
  const periodMode = () => $('feedKbAnalysisMode').value === 'trend';
  const periodUnit = () => $('feedKbGroup').value.slice('$period:'.length);
  const periodSpan = () => Math.max(1, parseInt($('feedKbPeriodSpan').value, 10) || 1);
  const periodScopeNote = '按分析时间范围比较已保存历史；对象精确匹配，空周期不补零。覆盖更新仅保留当前快照。';
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
  // 对象值多选：一个对象一条曲线，图例按颜色对应
  function renderChips() {
    const host = $('feedKbTrendChips');
    host.hidden = !state.objects.length;
    clear(host);
    state.objects.forEach((name, index) => {
      const chip = element('span', 'fkd-chip notranslate');
      const swatch = element('i', 'fkd-chip-color');
      swatch.style.background = SERIES_COLORS[index % SERIES_COLORS.length];
      const label = element('span', 'fkd-chip-name', name);
      label.title = name;
      const drop = element('button', 'fkd-chip-x');
      drop.type = 'button'; drop.textContent = '×'; drop.dataset.object = name;
      drop.setAttribute('aria-label', `${uiText('移除对象')}：${name}`);
      chip.append(swatch, label, drop);
      host.append(chip);
    });
    $('feedKbObjectCount').textContent = state.objects.length ? uiText(`已选 ${state.objects.length} / 8 个对象`) : uiText('选择对象');
    $('feedKbObjectClear').disabled = !state.objects.length;
    $('feedKbObjectStatus').textContent = state.objects.length === MAX_SERIES
      ? uiText('已达到 8 个对象上限，可取消已选项后继续选择。') : uiText('从已保存历史中选择对象。');
    updateApplyState();
  }
  function clearObjects() {
    state.objects = []; state.hiddenObjects.clear(); $('feedKbTrendValue').value = '';
    renderChips();
  }
  function toggleObject(name, selected) {
    if (selected && !state.objects.includes(name) && state.objects.length < MAX_SERIES) state.objects.push(name);
    else if (!selected) state.objects = state.objects.filter(value => value !== name);
    markAnalysisChanged(); renderChips(); renderObjectChoices(name);
  }
  const optionSync = (select, fields, blankLabel) => {
    const selected = select.value;
    clear(select);
    if (blankLabel !== null) select.append(new Option(blankLabel, ''));
    fields.forEach(field => { const option = new Option(field, field); option.setAttribute('translate', 'no'); select.append(option); });
    if (fields.includes(selected)) select.value = selected;
  };

  function syncFields(fields) {
    if (JSON.stringify(state.fields) === JSON.stringify(fields)) return false;
    markAnalysisChanged();
    state.fields = fields;
    optionSync($('feedKbFilterField'), fields, '不限字段');
    optionSync($('feedKbSummaryGroup'), fields, '不分组（总体）');
    const selectedMeasure = $('feedKbMeasure').value;
    const trendField = $('feedKbTrendField');
    const selectedTrendField = trendField.value;
    clear(trendField);
    trendField.append(new Option(trackerMode ? '文档名' : '来源 ID', trackerMode ? '$document_name' : '$source_id'));
    fields.forEach(field => { const option = new Option(field, field); option.setAttribute('translate', 'no'); trendField.append(option); });
    trendField.value = [trackerMode ? '$document_name' : '$source_id', ...fields].includes(selectedTrendField)
      ? selectedTrendField : (['景点', '设备', '标题'].find(field => fields.includes(field)) || trendField.options[0].value);
    if (trendField.value !== selectedTrendField) {
      clearObjects();
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
    if (['summary', 'trend', 'options'].includes(kind)) {
      q.set('scope', 'history'); q.delete('q'); q.delete('field'); q.delete('value'); q.delete('column_offset');
      q.set('from', $('feedKbAnalysisFrom').value); q.set('to', $('feedKbAnalysisTo').value);
    }
    if (kind === 'summary') {
      q.set('group', $('feedKbSummaryGroup').value);
      q.set('op', $('feedKbOperation').value);
      q.set('measure', $('feedKbMeasure').value);
    } else if (kind === 'options') {
      q.set('object_field', $('feedKbTrendField').value);
      q.set('object_query', $('feedKbTrendValue').value.trim());
      q.set('object_offset', String(offset));
    } else if (kind === 'trend') {
      q.set('object_field', $('feedKbTrendField').value);
      // 对象可多选：重复同名参数，值里的逗号不会被误拆
      state.objects.forEach(name => q.append('object_value', name));
      q.set('period', periodUnit());
      q.set('span', String(periodSpan()));
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

  function updateApplyState() {
    const needsNumber = $('feedKbOperation').value !== 'count';
    $('feedKbApply').disabled = state.analysisLoading || state.optionsLoading || state.optionsError
      || !$('feedKbAnalysisFrom').value || !$('feedKbAnalysisTo').value
      || (needsNumber && !$('feedKbMeasure').value) || (periodMode() && !state.objects.length);
    $('feedKbExport').disabled = state.exporting || state.analysisLoading || state.analysisDirty
      || !state.analysisValid || !state.appliedSignature;
  }

  function markAnalysisChanged() {
    ++state.summaryToken; state.analysisLoading = false;
    state.analysisDirty = true;
    $('feedKbSummaryMore').hidden = true; $('feedKbSummaryMore').disabled = true;
    $('feedKbAggregateNote').textContent = uiText('设置已修改，点击“应用分析”更新结果。');
    updateApplyState();
  }

  function renderObjectChoices(focusName) {
    const host = $('feedKbObjectOptions'); clear(host);
    state.objectChoices.forEach(name => {
      const row = element('label', 'fkd-object-option');
      const check = element('input'); check.type = 'checkbox'; check.checked = state.objects.includes(name);
      check.dataset.object = name;
      check.disabled = state.optionsLoading || state.optionsError
        || (!check.checked && state.objects.length >= MAX_SERIES);
      check.addEventListener('change', () => toggleObject(name, check.checked));
      const label = element('span', 'notranslate', name); label.title = name;
      row.append(check, label); host.append(row);
      if (name === focusName) check.focus();
    });
    if (!state.objectChoices.length) host.append(element('p', 'fkd-object-empty',
      uiText(state.optionsLoading ? '正在读取对象…' : '没有匹配对象，请调整搜索或时间范围。')));
  }

  function setObjectOpen(open, restoreFocus = false) {
    $('feedKbObjectPopover').hidden = !open;
    $('feedKbObjectToggle').setAttribute('aria-expanded', String(open));
    if (open) { $('feedKbTrendValue').focus(); void loadOptions(); }
    else if (restoreFocus) $('feedKbObjectToggle').focus();
  }

  async function loadOptions(append = false) {
    const token = ++state.optionsToken;
    const first = $('feedKbAnalysisFrom').value, last = $('feedKbAnalysisTo').value;
    state.optionsError = false;
    if (!append) state.objectChoices = [];
    if (first && last && first >= last) {
      state.optionsError = true; state.optionsLoading = false;
      $('feedKbAggregateNote').textContent = uiText('开始时间必须早于结束时间。'); renderObjectChoices(); updateApplyState(); return false;
    }
    state.optionsLoading = true; updateApplyState();
    renderObjectChoices();
    $('feedKbObjectOptions').setAttribute('aria-busy', 'true'); $('feedKbObjectMore').disabled = true;
    try {
      const data = await request('options', append ? state.objectOffset : 0);
      if (token !== state.optionsToken) return false;
      syncFields(data.fields || []);
      state.timezone = data.timezone;
      state.dataFrom = data.data_from; state.dataTo = data.data_to;
      state.numericFields = data.numeric_fields || [];
      const previous = $('feedKbMeasure').value;
      optionSync($('feedKbMeasure'), state.numericFields, state.numericFields.length ? null : '没有可用的数值字段');
      if (previous && previous !== $('feedKbMeasure').value) markAnalysisChanged();
      $('feedKbMeasure').disabled = !state.numericFields.length;
      $('feedKbNumericNote').textContent = uiText(state.numericFields.length
        ? '仅列出时间范围内含有效数字的字段；数字字符串也可参与统计。'
        : '当前时间范围内没有可用的数值字段，可改用记录数或调整时间范围。');
      $('feedKbNumericNote').hidden = $('feedKbOperation').value === 'count';
      $('feedKbRangeNote').replaceChildren(element('span', 'notranslate', data.timezone),
        document.createTextNode(' · '), element('span', '', uiText('按记录更新时间筛选，结束时间不包含在内。')));
      const feedName = $('feedKbFeed').selectedOptions?.[0]?.textContent || '';
      $('feedKbAnalysisScope').replaceChildren(...(feedName ? [element('span', 'notranslate', feedName), document.createTextNode(' · ')] : []),
        element('span', '', uiText('使用已保存历史；记录搜索和最新/历史切换不影响分析。')));
      state.objectChoices = [...new Set([...(append ? state.objectChoices : []), ...(data.object_values || [])])];
      state.objectOffset = data.object_next_offset;
      $('feedKbObjectMore').hidden = !data.object_has_more;
      renderObjectChoices(); return true;
    } catch (error) {
      if (token === state.optionsToken) {
        state.optionsError = true; $('feedKbAggregateNote').textContent = error.message;
        clear($('feedKbObjectOptions')); $('feedKbObjectOptions').append(element('p', 'fkd-object-empty', error.message));
      }
      return false;
    } finally {
      if (token === state.optionsToken) {
        state.optionsLoading = false; $('feedKbObjectOptions').setAttribute('aria-busy', 'false');
        $('feedKbObjectMore').disabled = false;
        if (!state.optionsError) renderObjectChoices();
        updateApplyState();
      }
    }
  }

  function dateInput(value) {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
      timeZone: state.timezone, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    }).formatToParts(value).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
  }

  function setRangePreset(preset) {
    if (preset === 'custom') return;
    if (preset === 'all' && state.dataFrom && state.dataTo) {
      $('feedKbAnalysisFrom').value = dateInput(new Date(Math.floor(new Date(state.dataFrom).getTime() / 60000) * 60000));
      $('feedKbAnalysisTo').value = dateInput(new Date(Math.floor(new Date(state.dataTo).getTime() / 60000) * 60000 + 60000));
      return;
    }
    const now = new Date(), to = dateInput(new Date(Math.floor(now.getTime() / 60000) * 60000 + 60000));
    const ago = new Date(now.getTime() - ({'24h': 1, '7d': 7, '30d': 30}[preset] || 0) * 86400000);
    $('feedKbAnalysisFrom').value = preset === 'today' ? to.slice(0, 10) + 'T00:00' : dateInput(ago);
    $('feedKbAnalysisTo').value = to;
  }

  function renderRows(data, append) {
    const body = $('feedKbRowsBody');
    if (!append) { clear(body); renderRowHead(data.columns || data.fields || []); state.selectedRows.clear(); }
    state.visibleRows = append ? [...state.visibleRows, ...(data.rows || [])] : (data.rows || []);
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
    const values = groups.map(group => number(group.metrics?.value));
    const max = Math.max(0, ...values.filter(value => value !== null).map(Math.abs));
    if (!groups.length || !values.some(value => value !== null)) { host.append(element('p', 'fkd-empty', '当前范围没有可绘制的数值。')); return; }
    for (const group of groups) {
      const numeric = number(group.metrics?.value);
      const label = groupName ? valueText(group.by?.[groupName]) : '总体';
      const item = element('div', 'fkd-chart-row');
      item.append(element('span', 'fkd-chart-label notranslate', label));
      const rail = element('div', 'fkd-chart-rail');
      const bar = element('div', 'fkd-chart-bar');
      bar.style.width = `${numeric === null || numeric === 0 ? 0 : Math.max(1, Math.abs(numeric) / max * 100)}%`;
      rail.append(bar); item.append(rail, element('strong', '', numeric !== null ? valueText(group.metrics.value) : '—'));
      host.append(item);
    }
    if (totalGroups > groups.length) host.append(element('p', 'fkd-subnote', '图表显示已加载的分组；点击“显示更多分组”继续查看。'));
  }

  function renderSummary(data, append, snapshot) {
    const groupName = snapshot.group;
    state.renderedMode = 'summary'; state.trendSeries = []; $('feedKbLegend').hidden = true;
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
    $('feedKbAggregateNote').textContent = snapshot.operation === 'count'
      ? '记录数基于全部匹配记录。' : '数值统计排除空值和无法解析的内容；表格列出实际参与计算的条数。';
    $('feedKbSummaryMore').hidden = !data.has_more || data.next_offset === null;
    state.summaryOffset = data.next_offset;
  }

  const numberText = value => value === null || value === undefined ? '—'
    : new Intl.NumberFormat(document.documentElement.lang || 'zh-CN', {maximumFractionDigits: 8}).format(value);

  const chartField = () => $('feedKbChartMode').value;
  const modeNames = {value: '数值', change: '较上期变化', change_pct: '变化率（%）'};
  const pointMetric = point => {
    const mode = chartField();
    if (mode === 'change') return point.change;
    if (mode === 'change_pct') return point.change_pct;
    return point.value;
  };
  const number = value => (value === null || value === undefined || !Number.isFinite(Number(value))
    ? null : Number(value));

  function renderLegend() {
    const legend = $('feedKbLegend');
    clear(legend);
    const series = state.trendSeries || [];
    legend.hidden = state.renderedMode !== 'trend' || !series.length || $('feedKbChart').hidden;
    if (legend.hidden) return;
    series.forEach((item, index) => {
      const off = state.hiddenObjects.has(item.object_value);
      const chip = element('button', `fkd-legend-item${off ? ' is-off' : ''}`);
      chip.type = 'button';
      chip.dataset.object = item.object_value;
      chip.setAttribute('aria-pressed', String(!off));
      chip.title = off ? uiText('点击在图里显示该对象') : uiText('点击从图里隐藏该对象');
      const swatch = element('i', 'fkd-legend-color');
      swatch.style.background = SERIES_COLORS[index % SERIES_COLORS.length];
      const last = [...item.points].reverse().find(point => point.value !== null);
      chip.append(swatch, element('span', 'fkd-legend-name notranslate', item.object_value),
                  element('span', 'fkd-legend-value', last ? numberText(last.value) : '—'));
      legend.append(chip);
    });
  }

  function drawTrend() {
    const host = $('feedKbChart'); clear(host);
    renderLegend();
    const all = state.trendSeries || [];
    const shown = all.filter(item => !state.hiddenObjects.has(item.object_value));
    const labels = all.length ? all[0].points.map(point => point.label) : [];
    const lines = shown.map(item => item.points.map(point => number(pointMetric(point))));
    const values = lines.flat().filter(value => value !== null);
    if (!labels.length || !values.length) {
      host.append(element('p', 'fkd-empty', uiText('当前范围或口径没有可绘制的数值。')));
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
    const minValue = Math.min(...values), maxValue = Math.max(...values);
    const padding = maxValue === minValue ? Math.max(1, Math.abs(maxValue) * 0.1) : 0;
    const low = minValue - padding, high = maxValue + padding;
    const x = index => 52 + index / Math.max(1, labels.length - 1) * 822;
    const y = value => 216 - (value - low) / Math.max(1e-9, high - low) * 182;
    [0, 0.5, 1].forEach(fraction => {
      const ordinate = 216 - fraction * 182;
      add('line', {x1: 52, y1: ordinate, x2: 874, y2: ordinate, class: 'fkd-trend-grid'});
      const tick = add('text', {x: 47, y: ordinate + 4, 'text-anchor': 'end', class: 'fkd-trend-axis'});
      tick.textContent = numberText(low + (high - low) * fraction);
    });
    if (low < 0 && high > 0) {
      add('line', {x1: 52, y1: y(0), x2: 874, y2: y(0), class: 'fkd-trend-zero'});
    }
    lines.forEach((points, seriesIndex) => {
      const color = SERIES_COLORS[(all.findIndex(item => item === shown[seriesIndex])) % SERIES_COLORS.length];
      let segment = [];
      const flush = () => {
        if (segment.length > 1) {
          const line = add('polyline', {points: segment.join(' '), class: 'fkd-trend-line'});
          line.style.stroke = color;          // 类里的 stroke 优先级比表现属性高，写进 style
        }
        segment = [];
      };
      points.forEach((value, index) => {
        if (value === null) { flush(); return; }
        segment.push(`${x(index)},${y(value)}`);
      });
      flush();
      points.forEach((value, index) => {
        if (value === null) return;
        const dot = add('circle', {cx: x(index), cy: y(value), r: labels.length > 150 ? 2.5 : 4,
          class: 'fkd-trend-dot'});
        dot.style.fill = color; dot.style.stroke = color;
        const title = add('title', {}, dot);
        title.textContent = `${labels[index]} · ${shown[seriesIndex].object_value}: ${numberText(value)}`;
      });
    });
    [...new Set([0, Math.floor((labels.length - 1) / 2), labels.length - 1])].forEach(index => {
      const label = add('text', {x: x(index), y: 248,
        'text-anchor': index === 0 ? 'start' : index === labels.length - 1 ? 'end' : 'middle',
        class: 'fkd-trend-axis'});
      label.textContent = labels[index];
    });
    host.append(svg);
    const note = element('p', 'fkd-subnote');
    note.append(element('span', '', uiText('图表口径')), document.createTextNode(': '),
      element('span', '', uiText(modeNames[chartField()] || '')), document.createTextNode(' · '),
      element('span', '', uiText('点击图例可显示或隐藏某个对象。')));
    host.append(note);
  }

  function changeText(point) {
    if (point.change === null) return '—';
    const rate = point.change_pct === null ? '' : ` (${point.change_pct > 0 ? '+' : ''}${numberText(point.change_pct)}%)`;
    return `${point.change > 0 ? '+' : ''}${numberText(point.change)}${rate}`;
  }

  function renderTrend(data) {
    state.renderedMode = 'trend';
    state.trendSeries = data.series && data.series.length
      ? data.series : [{object_value: data.object_value || '对象', points: data.points || []}];
    state.trendMeta = data;
    state.hiddenObjects = new Set([...state.hiddenObjects]
      .filter(name => state.trendSeries.some(item => item.object_value === name)));
    const series = state.trendSeries;
    const single = series.length === 1;
    const points = series[0].points;
    const body = $('feedKbSummaryBody'); clear(body);
    const headings = element('tr');
    headings.append(element('th', '', '周期'));
    series.forEach(item => {
      headings.append(element('th', single ? '' : 'notranslate', single ? '统计值' : `${item.object_value} 值`));
      headings.append(element('th', '', single ? '较上期变化' : `${item.object_value} 环比`));
    });
    if (single) ['变化率', '记录数', '有效数值'].forEach(label => headings.append(element('th', '', label)));
    $('feedKbSummaryHead').replaceChildren(headings);
    for (const [index, point] of points.entries()) {
      const row = element('tr');
      row.append(element('td', '', point.label));
      series.forEach(item => {
        const current = item.points[index] || {};
        row.append(element('td', 'fkd-trend-number', numberText(current.value)));
        row.append(element('td', 'fkd-trend-number', changeText(current)));
      });
      if (single) {
        let rate = point.change_pct === null ? '—' : `${point.change_pct > 0 ? '+' : ''}${numberText(point.change_pct)}%`;
        if (point.comparison === 'zero_baseline') rate = '—（上期为 0）';
        else if (point.comparison === 'no_previous_value' && index > 0) rate = '—（上期无数据）';
        row.append(element('td', 'fkd-trend-number', rate));
        row.append(element('td', 'fkd-trend-number', String(point.records)));
        row.append(element('td', 'fkd-trend-number',
          point.valid_numeric_values === null ? '—' : String(point.valid_numeric_values)));
      }
      body.append(row);
    }
    if (!points.length) {
      const row = element('tr'), cell = element('td', 'fkd-empty',
        uiText('这些对象在当前日期范围内没有已完成记录。'));
      cell.colSpan = single ? 6 : 1 + series.length * 2; row.append(cell); body.append(row);
    }
    drawTrend();
    const records = series.reduce((sum, item) => sum + item.points.reduce((total, point) => total + point.records, 0), 0);
    const info = [data.period_label || '', `${series.length} 个对象`, `${points.length} 个周期`,
      `${records} 条记录`, data.timezone];
    $('feedKbSummaryInfo').replaceChildren(...info.flatMap((part, index) =>
      [...(index ? [document.createTextNode(' · ')] : []), element('span', '', uiText(part))]));
    $('feedKbAggregateNote').textContent = periodScopeNote;
    $('feedKbSummaryMore').hidden = true;
    $('feedKbSummaryTableWrap').setAttribute('aria-label', '周期对比表，可横向滚动');
    $('feedKbChart').setAttribute('aria-label', '周期趋势图');
  }

  function syncPeriodControls() {
    const period = periodMode();
    $('feedKbObjectControls').hidden = !period;
    $('feedKbSummaryGroupWrap').hidden = period;
    $('feedKbPeriodWrap').hidden = !period;
    $('feedKbMeasureWrap').hidden = $('feedKbOperation').value === 'count';
    $('feedKbNumericNote').hidden = $('feedKbOperation').value === 'count';
    $('feedKbChartModeWrap').hidden = !period;
    const maximum = {minute: 180, hour: 72, day: 92, week: 26, month: 60, year: 20}[periodUnit()];
    $('feedKbPeriodSpan').max = maximum;
    if (periodSpan() > maximum) $('feedKbPeriodSpan').value = maximum;
    document.querySelectorAll('[data-fkd-mode]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.fkdMode === $('feedKbAnalysisMode').value)));
    if (!period) setObjectOpen(false);
    updateApplyState();
  }

  function promptForObject() {
    const headings = element('tr');
    ['周期', '统计值', '较上期变化', '变化率', '记录数', '有效数值']
      .forEach(label => headings.append(element('th', '', label)));
    $('feedKbSummaryHead').replaceChildren(headings);
    const row = element('tr'), cell = element('td', 'fkd-empty',
      uiText('输入或点选至少一个对象值，再应用分析。'));
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
      if (Array.isArray(data.fields)) syncFields(data.fields);
      showRun(data); renderRows(data, append);
    } catch (error) {
      if (token === state.rowToken) { $('feedKbError').textContent = error.message; $('feedKbError').hidden = false; }
    } finally { if (token === state.rowToken) $('feedKbMore').disabled = false; }
  }

  async function loadAnalysis(append = false) {
    const token = ++state.summaryToken;
    const period = periodMode();
    const signature = params(period ? 'trend' : 'summary', 0).toString();
    if (append && (state.analysisDirty || state.appliedSignature !== signature)) return;
    if (!$('feedKbAnalysisForm').reportValidity() || state.optionsLoading || state.optionsError) return;
    if (period && !state.objects.length) { promptForObject(); updateApplyState(); return; }
    if ($('feedKbOperation').value !== 'count' && !$('feedKbMeasure').value) { updateApplyState(); return; }
    const snapshot = {group: $('feedKbSummaryGroup').value, operation: $('feedKbOperation').value};
    state.analysisLoading = true; updateApplyState();
    $('feedKbSummaryMore').disabled = true;
    $('feedKbAggregateNote').textContent = period ? '正在计算周期趋势…' : '正在计算汇总…';
    try {
      const data = await request(period ? 'trend' : 'summary', period ? 0 : append ? state.summaryOffset : 0);
      if (token !== state.summaryToken) return;
      if (period) renderTrend(data);
      else renderSummary(data, append, snapshot);
      state.analysisDirty = false; state.appliedSignature = signature;
      state.analysisValid = true;
    } catch (error) {
      if (token === state.summaryToken) {
        state.analysisValid = false;
        $('feedKbAggregateNote').textContent = error.message;
        $('feedKbSummaryInfo').textContent = '';
        $('feedKbSummaryMore').hidden = true;
        clear($('feedKbSummaryBody')); clear($('feedKbChart'));
        const headings = element('tr');
        (period ? ['周期', '统计值', '较上期变化', '变化率', '记录数', '有效数值']
          : [snapshot.group || '范围', '统计值', '参与计算的数值条数'])
          .forEach(label => headings.append(element('th', '', label)));
        $('feedKbSummaryHead').replaceChildren(headings);
        const row = element('tr'), cell = element('td', 'fkd-empty', '分析暂不可用，请检查上方提示。');
        cell.colSpan = period ? 6 : 3; row.append(cell); $('feedKbSummaryBody').append(row);
        $('feedKbChart').append(element('p', 'fkd-empty', '分析暂不可用，请检查上方提示。'));
      }
    } finally {
      if (token === state.summaryToken) {
        state.analysisLoading = false; updateApplyState();
        $('feedKbSummaryMore').disabled = false;
      }
    }
  }

  async function exportAnalysis() {
    if ($('feedKbExport').disabled) return;
    const query = new URLSearchParams(state.appliedSignature);
    query.set('export', 'xlsx'); query.set('offset', '0');
    state.exporting = true; updateApplyState();
    $('feedKbExport').textContent = uiText('正在导出…');
    try {
      const response = await fetch(`${endpoint}?${query}`);
      if (!response.ok || !response.headers.get('Content-Type')?.includes('spreadsheetml')) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.message || uiText('导出失败，请重新登录后重试。'));
      }
      const disposition = response.headers.get('Content-Disposition') || '';
      const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
      const plainName = disposition.match(/filename="([^"]+)"/i)?.[1];
      const name = encodedName ? decodeURIComponent(encodedName) : plainName || 'EnGenie-analysis.xlsx';
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a'); link.href = url; link.download = name;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (error) {
      $('feedKbAggregateNote').textContent = error.message;
    } finally {
      state.exporting = false; $('feedKbExport').textContent = uiText('导出 Excel'); updateApplyState();
    }
  }

  async function reload() {
    await loadRows();
    if (await loadOptions()) {
      if ($('feedKbAnalysisPreset').value !== 'custom') {
        setRangePreset($('feedKbAnalysisPreset').value);
        if (!await loadOptions()) return;
      }
      await loadAnalysis();
    }
  }
  $('feedKbFilters').addEventListener('submit', event => { event.preventDefault(); void loadRows(); });
  $('feedKbReset').addEventListener('click', () => {
    const previousFeed = $('feedKbFeed').value;
    $('feedKbFilters').reset(); void loadRows();
    if (previousFeed !== $('feedKbFeed').value) $('feedKbFeed').dispatchEvent(new Event('change'));
  });
  $('feedKbRefresh')?.addEventListener('click', () => void reload());
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
      if (deleted) void reload(); else updateSelection();
    });
  }
  $('feedKbColumns').addEventListener('change', () => void loadRows());
  $('feedKbSummaryMore').addEventListener('click', () => void loadAnalysis(true));
  $('feedKbExport').addEventListener('click', () => void exportAnalysis());
  document.querySelectorAll('[data-fkd-mode]').forEach(button => button.addEventListener('click', () => {
    $('feedKbAnalysisMode').value = button.dataset.fkdMode;
    syncPeriodControls(); markAnalysisChanged();
  }));
  ['feedKbGroup', 'feedKbPeriodSpan', 'feedKbSummaryGroup', 'feedKbOperation', 'feedKbMeasure']
    .forEach(id => $(id).addEventListener('change', () => { syncPeriodControls(); markAnalysisChanged(); }));
  $('feedKbChartMode').addEventListener('change', () => { if (state.renderedMode === 'trend') drawTrend(); });
  $('feedKbTrendChips').addEventListener('click', event => {
    const drop = event.target.closest('.fkd-chip-x');
    if (drop) toggleObject(drop.dataset.object, false);
  });
  $('feedKbObjectToggle').addEventListener('click', () => setObjectOpen($('feedKbObjectPopover').hidden));
  $('feedKbObjectDone').addEventListener('click', () => setObjectOpen(false, true));
  $('feedKbObjectClear').addEventListener('click', () => {
    clearObjects(); markAnalysisChanged(); renderObjectChoices(); $('feedKbTrendValue').focus();
  });
  $('feedKbObjectMore').addEventListener('click', () => void loadOptions(true));
  let searchTimer;
  $('feedKbTrendValue').addEventListener('input', () => {
    clearTimeout(searchTimer); ++state.optionsToken;
    searchTimer = setTimeout(() => void loadOptions(), 250);
  });
  $('feedKbTrendValue').addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); return; }
    if (event.key === 'ArrowDown') { event.preventDefault(); $('feedKbObjectOptions').querySelector('input:not(:disabled)')?.focus(); }
  });
  $('feedKbObjectOptions').addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    event.preventDefault();
    const checks = [...$('feedKbObjectOptions').querySelectorAll('input:not(:disabled)')];
    const index = checks.indexOf(document.activeElement);
    const next = index + (event.key === 'ArrowDown' ? 1 : -1);
    if (next < 0) $('feedKbTrendValue').focus(); else checks[Math.min(next, checks.length - 1)]?.focus();
  });
  document.addEventListener('pointerdown', event => {
    if (!$('feedKbTrendValueWrap').contains(event.target)) setObjectOpen(false);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !$('feedKbObjectPopover').hidden) { event.preventDefault(); setObjectOpen(false, true); }
  });
  $('feedKbTrendField').addEventListener('change', () => {
    clearObjects(); markAnalysisChanged(); void loadOptions();
  });
  $('feedKbFeed').addEventListener('change', async () => {
    clearObjects(); markAnalysisChanged();
    if (await loadOptions() && $('feedKbAnalysisPreset').value !== 'custom') {
      setRangePreset($('feedKbAnalysisPreset').value); void loadOptions();
    }
  });
  $('feedKbAnalysisPreset').addEventListener('change', () => {
    setRangePreset($('feedKbAnalysisPreset').value); markAnalysisChanged(); void loadOptions();
  });
  ['feedKbAnalysisFrom', 'feedKbAnalysisTo'].forEach(id => $(id).addEventListener('change', () => {
    $('feedKbAnalysisPreset').value = 'custom'; markAnalysisChanged(); void loadOptions();
  }));
  $('feedKbAnalysisForm').addEventListener('submit', event => { event.preventDefault(); setObjectOpen(false); void loadAnalysis(); });
  $('feedKbLegend').addEventListener('click', event => {
    const chip = event.target.closest('.fkd-legend-item');
    if (!chip) return;
    const name = chip.dataset.object;
    if (state.hiddenObjects.has(name)) state.hiddenObjects.delete(name); else state.hiddenObjects.add(name);
    drawTrend();
  });
  document.addEventListener('fos:languagechange', () => { syncPeriodControls(); renderChips(); renderObjectChoices(); });
  document.querySelectorAll('[data-fkd-view]').forEach(button => button.addEventListener('click', () => {
    const chartView = button.dataset.fkdView === 'chart';
    $('feedKbChart').hidden = !chartView;
    $('feedKbSummaryTableWrap').hidden = chartView;
    renderLegend();
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
  renderChips();
  void (async () => { await loadRows(); if (await loadOptions()) { setRangePreset('7d'); if (await loadOptions()) await loadAnalysis(); } })();
})();
