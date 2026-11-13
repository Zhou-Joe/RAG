/* Bounded, text-only interactive tables shared by chat and offline reports. */
(function(root) {
  'use strict';
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const mounted = new WeakMap();
  const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const numeric = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
  const isoDate = /^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/;
  const words = language => language === 'zh-hans' ? {
    table: '交互表格', search: '搜索已载入的行', all: '全部列', column: '筛选列',
    previous: '上一页', next: '下一页', page: '页', perPage: '每页行数',
    loaded: '已载入', total: '条源记录', matching: '条已载入记录符合筛选',
    showing: '显示', noRows: '没有已载入的记录。', noMatches: '已载入的记录中没有匹配项。',
    subset: '筛选和排序仅适用于已载入的记录。', truncated: '内容已截断', characters: '字符',
    ascending: '升序排列', descending: '降序排列',
  } : {
    table: 'Interactive Table', search: 'Search Loaded Rows', all: 'All Columns', column: 'Filter Column',
    previous: 'Previous', next: 'Next', page: 'Page', perPage: 'Rows per Page',
    loaded: 'Loaded', total: 'source records', matching: 'loaded rows match the filter',
    showing: 'Showing', noRows: 'No loaded rows.', noMatches: 'No matches in the loaded rows.',
    subset: 'Filtering and sorting apply only to loaded rows.', truncated: 'Truncated', characters: 'characters',
    ascending: 'Sort Ascending', descending: 'Sort Descending',
  };
  function text(value, limit, fallback = '') {
    if (value === undefined) return fallback;
    if (typeof value !== 'string' || value.length > limit) throw new TypeError('Invalid table text');
    return value;
  }
  function cell(value) {
    if (value === undefined || value === null) return null;
    if (typeof value === 'string') return text(value, 8000);
    if (typeof value === 'boolean' || typeof value === 'number' && Number.isFinite(value)) return value;
    if (plain(value) && value.truncated === true && typeof value.text === 'string' && value.text.length <= 8000 &&
        Number.isSafeInteger(value.original_chars) && value.original_chars >= value.text.length) {
      return {text: value.text, truncated: true, original_chars: value.original_chars};
    }
    throw new TypeError('Invalid table cell');
  }
  function normalize(data, language) {
    if (!plain(data) || data.type !== 'table' || !Array.isArray(data.columns) || !data.columns.length ||
        data.columns.length > 12 || !Array.isArray(data.rows) || data.rows.length > 500)
      throw new TypeError('Invalid interactive table');
    const keys = new Set();
    const columns = data.columns.map(column => {
      if (!plain(column)) throw new TypeError('Invalid table column');
      const key = text(column.key, 200), label = text(column.label, 500, key);
      if (!key || keys.has(key)) throw new TypeError('Invalid table column');
      keys.add(key);
      const result = {key, label};
      if (column.type !== undefined) {
        if (!['text', 'number', 'date'].includes(column.type)) throw new TypeError('Invalid table column type');
        result.type = column.type;
      }
      return result;
    });
    const rows = data.rows.map(row => {
      if (!plain(row)) throw new TypeError('Invalid table row');
      const result = Object.create(null);
      for (const column of columns) result[column.key] = cell(own(row, column.key) ? row[column.key] : null);
      return result;
    });
    if (!Number.isSafeInteger(data.total_rows) || data.total_rows < rows.length || typeof data.truncated !== 'boolean' ||
        data.truncated !== (data.total_rows > rows.length)) throw new TypeError('Invalid table row count');
    const result = {type: 'table', title: text(data.title, 1200), columns, rows,
      total_rows: data.total_rows, truncated: data.truncated,
      language: (language || data.language) === 'zh-hans' ? 'zh-hans' : 'en'};
    if (data.meta !== undefined) {
      if (!plain(data.meta)) throw new TypeError('Invalid table metadata');
      const meta = {};
      for (const key of ['row_count', 'displayed_rows', 'omitted_row_count']) {
        if (data.meta[key] !== undefined) {
          if (!Number.isSafeInteger(data.meta[key]) || data.meta[key] < 0) throw new TypeError('Invalid table metadata');
          meta[key] = data.meta[key];
        }
      }
      result.meta = meta;
    }
    return result;
  }
  const rawCell = value => plain(value) && value.truncated === true ? value.text : value;
  const display = value => value === null || value === undefined ? '—' : String(rawCell(value));
  const numericValue = value => typeof value === 'number' && Number.isFinite(value) ? value
    : typeof value === 'string' && numeric.test(value.trim()) && Number.isFinite(Number(value)) ? Number(value) : null;
  function typeFor(column, rows) {
    if (column.type) return column.type;
    const values = rows.map(row => rawCell(row[column.key])).filter(value => value !== null && value !== '');
    if (values.length && values.every(value => numericValue(value) !== null)) return 'number';
    if (values.length && values.every(value => typeof value === 'string' && isoDate.test(value) && Number.isFinite(Date.parse(value)))) return 'date';
    return 'text';
  }
  function destroy(host) {
    if (!host) return;
    const state = mounted.get(host);
    if (!state) return;
    for (const remove of state.listeners || []) remove();
    state.listeners = []; state.region?.remove(); mounted.delete(host);
    if (host._ercCleanup === state.cleanup) delete host._ercCleanup;
  }
  function render(host, input) {
    if (!host?.ownerDocument?.createElement) throw new TypeError('Invalid table host');
    destroy(host);
    const data = normalize(input), document = host.ownerDocument;
    const previousState = host._eitState || {};
    const state = {query: typeof previousState.query === 'string' ? previousState.query.slice(0, 1000) : '',
      column: typeof previousState.column === 'string' ? previousState.column : '',
      sort: typeof previousState.sort === 'string' ? previousState.sort : '', descending: previousState.descending === true,
      page: Number.isSafeInteger(previousState.page) && previousState.page >= 0 ? previousState.page : 0,
      pageSize: [10, 20, 50].includes(previousState.pageSize) ? previousState.pageSize : 10,
      columnsSignature: JSON.stringify(data.columns)};
    host._eitState = state;
    let language = data.language;
    const listeners = [];
    const keys = new Set(data.columns.map(column => column.key));
    if (!keys.has(state.column)) state.column = '';
    if (!keys.has(state.sort)) { state.sort = ''; state.descending = false; }
    const make = (tag, className, value) => {
      const element = document.createElement(tag); element.className = className;
      if (value !== undefined) element.textContent = value;
      return element;
    };
    const listen = (element, type, callback) => {
      element.addEventListener(type, callback);
      listeners.push(() => element.removeEventListener(type, callback));
    };
    const region = make('div', 'eit-table notranslate'); region.setAttribute('translate', 'no');
    region.setAttribute('role', 'group'); region.setAttribute('aria-label', data.title || words(language).table);
    const toolbar = make('div', 'eit-toolbar'), search = make('input', 'eit-search'); search.type = 'search'; search.value = state.query;
    const columnFilter = make('select', 'eit-column-filter');
    const all = make('option', '', ''); all.value = ''; columnFilter.appendChild(all);
    for (const column of data.columns) {
      const option = make('option', '', column.label); option.value = column.key; columnFilter.appendChild(option);
    }
    columnFilter.value = state.column; toolbar.appendChild(search); toolbar.appendChild(columnFilter);
    const viewport = make('div', 'eit-viewport'); viewport.setAttribute('tabindex', '0');
    const table = make('table', 'eit-data'), head = make('thead', ''), header = make('tr', ''), body = make('tbody', '');
    const headers = [];
    for (const column of data.columns) {
      const th = make('th', ''); th.setAttribute('scope', 'col');
      const button = make('button', 'eit-sort'), label = make('span', 'eit-column-label', column.label), arrow = make('span', 'eit-sort-arrow');
      button.type = 'button'; button.title = column.label; arrow.setAttribute('aria-hidden', 'true');
      button.appendChild(label); button.appendChild(arrow); th.appendChild(button); header.appendChild(th);
      headers.push({column, th, button, arrow});
      listen(button, 'click', () => {
        state.descending = state.sort === column.key ? !state.descending : false;
        state.sort = column.key; state.page = 0; update();
      });
    }
    head.appendChild(header); table.appendChild(head); table.appendChild(body); viewport.appendChild(table);
    const status = make('div', 'eit-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const subset = make('div', 'eit-subset'); subset.hidden = !data.truncated;
    const pagination = make('div', 'eit-pagination'), previous = make('button', 'eit-previous'), next = make('button', 'eit-next');
    previous.type = 'button'; next.type = 'button';
    const page = make('span', 'eit-page'), pageSize = make('select', 'eit-page-size');
    for (const size of [10, 20, 50]) { const option = make('option', '', String(size)); option.value = String(size); pageSize.appendChild(option); }
    pageSize.value = String(state.pageSize); pagination.appendChild(previous); pagination.appendChild(page); pagination.appendChild(next); pagination.appendChild(pageSize);
    region.appendChild(toolbar); region.appendChild(viewport); region.appendChild(status); region.appendChild(subset); region.appendChild(pagination);
    host.appendChild(region);
    listen(search, 'input', () => { state.query = String(search.value || '').slice(0, 1000); state.page = 0; update(); });
    listen(columnFilter, 'change', () => { state.column = keys.has(columnFilter.value) ? columnFilter.value : ''; state.page = 0; update(); });
    listen(previous, 'click', () => { if (state.page > 0) { state.page--; update(); } });
    listen(next, 'click', () => { if (!next.disabled) { state.page++; update(); } });
    listen(pageSize, 'change', () => { const size = Number(pageSize.value); if ([10, 20, 50].includes(size)) { state.pageSize = size; state.page = 0; update(); } });
    const types = new Map(data.columns.map(column => [column.key, typeFor(column, data.rows)]));
    function update() {
      const labels = words(language), query = state.query.toLocaleLowerCase(language === 'zh-hans' ? 'zh-CN' : 'en');
      search.placeholder = labels.search; search.setAttribute('aria-label', labels.search);
      columnFilter.setAttribute('aria-label', labels.column); all.textContent = labels.all;
      previous.textContent = labels.previous; next.textContent = labels.next;
      pageSize.setAttribute('aria-label', labels.perPage); subset.textContent = labels.subset;
      let rows = data.rows.map((row, index) => ({row, index}));
      if (query) rows = rows.filter(({row}) => data.columns.some(column => (!state.column || column.key === state.column) &&
        display(row[column.key]).toLocaleLowerCase(language === 'zh-hans' ? 'zh-CN' : 'en').includes(query)));
      if (state.sort) {
        const type = types.get(state.sort), collator = new Intl.Collator(language === 'zh-hans' ? 'zh-CN' : 'en', {numeric: true, sensitivity: 'base'});
        const comparable = value => {
          const source = rawCell(value);
          if (source === null || source === undefined || source === '') return null;
          if (type === 'number') return numericValue(source);
          if (type === 'date') return typeof source === 'string' && isoDate.test(source) && Number.isFinite(Date.parse(source)) ? Date.parse(source) : null;
          return String(source);
        };
        rows.sort((left, right) => {
          const a = comparable(left.row[state.sort]), b = comparable(right.row[state.sort]);
          if (a === null || b === null) return a === b ? left.index - right.index : a === null ? 1 : -1;
          const order = type === 'text' ? collator.compare(a, b) : a < b ? -1 : a > b ? 1 : 0;
          return order ? order * (state.descending ? -1 : 1) : left.index - right.index;
        });
      }
      const pages = Math.max(1, Math.ceil(rows.length / state.pageSize)); state.page = Math.min(state.page, pages - 1);
      body.replaceChildren();
      const from = state.page * state.pageSize, shown = rows.slice(from, from + state.pageSize);
      for (const {row} of shown) {
        const tr = make('tr', '');
        for (const column of data.columns) {
          const value = row[column.key], td = make('td', '', display(value));
          if (plain(value) && value.truncated === true) {
            const marker = make('span', 'eit-truncation', labels.truncated);
            marker.title = value.original_chars + ' ' + labels.characters; td.appendChild(marker);
          }
          tr.appendChild(td);
        }
        body.appendChild(tr);
      }
      if (!shown.length) { const tr = make('tr', ''), td = make('td', 'eit-empty', query ? labels.noMatches : labels.noRows); td.setAttribute('colspan', String(data.columns.length)); tr.appendChild(td); body.appendChild(tr); }
      for (const entry of headers) {
        const selected = state.sort === entry.column.key;
        entry.th.setAttribute('aria-sort', selected ? state.descending ? 'descending' : 'ascending' : 'none');
        entry.arrow.textContent = selected ? state.descending ? '↓' : '↑' : '↕';
        entry.button.setAttribute('aria-label', entry.column.label + ': ' + (selected && !state.descending ? labels.descending : labels.ascending));
      }
      const range = shown.length ? String(from + 1) + '–' + (from + shown.length) : '0';
      status.textContent = labels.showing + ' ' + range + ' / ' + rows.length
        + (query ? ' · ' + rows.length + ' ' + labels.matching : '')
        + ' · ' + labels.loaded + ' ' + data.rows.length + ' / ' + data.total_rows + ' ' + labels.total;
      page.textContent = labels.page + ' ' + (state.page + 1) + ' / ' + pages;
      previous.disabled = state.page === 0; next.disabled = state.page === pages - 1;
    }
    listen(document, 'fos:languagechange', event => {
      if (['en', 'zh-hans'].includes(event.detail?.language)) { language = event.detail.language; update(); }
    });
    const cleanup = () => destroy(host); host._ercCleanup = cleanup;
    mounted.set(host, {listeners, region, cleanup});
    update(); return region;
  }
  const api = {normalize, render, destroy}; root.EnGenieInteractiveTable = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
