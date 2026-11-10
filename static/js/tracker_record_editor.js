(() => {
  const dialog = document.getElementById('trackerCellDialog');
  if (!dialog) return;
  const form = document.getElementById('trackerCellForm');
  const field = document.getElementById('trackerCellField');
  const value = document.getElementById('trackerCellValue');
  const error = document.getElementById('trackerCellError');
  const warning = document.getElementById('trackerCellWarning');
  const save = document.getElementById('trackerCellSave');
  const cancel = document.getElementById('trackerCellCancel');
  let editing = null;
  let resolveEdit = null;
  const t = source => window.FOSI18n?.t(source) || source;

  const asText = item => item === null || item === undefined ? ''
    : typeof item === 'object' ? (item.truncated ? item.text : JSON.stringify(item)) : String(item);
  const csrf = () => form.querySelector('input[name=csrfmiddlewaretoken]').value;
  async function post(payload) {
    const response = await fetch(dialog.dataset.url, {
      method: 'POST',
      body: new URLSearchParams(payload),
      headers: {'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest'},
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.message || `请求失败（HTTP ${response.status}）`);
    return data;
  }

  function showValue() {
    if (!editing) return;
    const item = editing.values[field.value];
    const text = asText(item);
    value.value = text.slice(0, 2000);
    warning.hidden = !(item?.truncated || text.length > 2000);
  }
  field.addEventListener('change', showValue);
  cancel.addEventListener('click', () => dialog.close());
  dialog.addEventListener('cancel', event => { if (save.disabled) event.preventDefault(); });
  dialog.addEventListener('close', () => {
    const done = resolveEdit;
    resolveEdit = null;
    editing = null;
    if (done) done(null);
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!editing) return;
    save.disabled = true;
    cancel.disabled = true;
    error.hidden = true;
    try {
      const payload = {action: 'cell_edit', row_id: String(editing.rowId), field: field.value,
        value: value.value, expected_value: JSON.stringify(editing.values[field.value] ?? null)};
      if (editing.expectedUpdatedAt) payload.expected_updated_at = editing.expectedUpdatedAt;
      const data = await post(payload);
      const done = resolveEdit;
      resolveEdit = null;
      dialog.close();
      if (window.fosToast) window.fosToast(t(data.message || '字段已保存'), 'ok');
      if (done) done(data);
    } catch (reason) {
      error.textContent = t(reason.message);
      error.hidden = false;
    } finally {
      save.disabled = false;
      cancel.disabled = false;
    }
  });

  window.trackerRecordEditor = {
    open({rowId, recordName, fields, selectedField, expectedUpdatedAt}) {
      if (dialog.open || !Number.isSafeInteger(Number(rowId))) return Promise.resolve(null);
      const entries = (fields || []).filter(item => item && typeof item.name === 'string');
      if (!entries.length) return Promise.resolve(null);
      const values = Object.fromEntries(entries.map(item => [item.name, item.value]));
      editing = {rowId, values, expectedUpdatedAt};
      field.replaceChildren(...entries.map(item => new Option(item.name, item.name)));
      field.value = entries.some(item => item.name === selectedField) ? selectedField : entries[0].name;
      showValue();
      document.getElementById('trackerCellRecord').textContent = recordName || '';
      error.hidden = true;
      dialog.showModal();
      value.focus();
      return new Promise(resolve => { resolveEdit = resolve; });
    },
    async remove(rowIds, recordName = '') {
      const ids = [...new Set((rowIds || []).map(Number))].filter(id => Number.isSafeInteger(id) && id > 0);
      if (!ids.length) return null;
      if (ids.length > 500) {
        if (window.fosToast) window.fosToast(t('一次最多删除 500 条，请缩小选择范围。'), 'err');
        return null;
      }
      const message = ids.length === 1
        ? `删除「${recordName || '此记录'}」的追踪表记录？原始文档或采集源不会删除；后续采集可能再次生成这条记录。`
        : `删除选中的 ${ids.length} 条追踪表记录？原始文档或采集源不会删除；后续采集可能再次生成记录。`;
      const accepted = window.fosConfirm
        ? await window.fosConfirm({title: t('删除追踪表记录'), message: t(message), confirmText: t('删除记录'), danger: true})
        : window.confirm(t(message));
      if (!accepted) return null;
      try {
        const data = await post({action: 'rows_delete', row_ids: JSON.stringify(ids)});
        if (window.fosToast) window.fosToast(t(data.message || '记录已删除'), 'ok');
        return data;
      } catch (reason) {
        if (window.fosToast) window.fosToast(t(reason.message), 'err');
        return null;
      }
    },
  };
})();
