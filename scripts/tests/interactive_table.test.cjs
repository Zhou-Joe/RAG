const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = document; this.children = [];
    this.parentNode = null; this.className = ''; this.attributes = {}; this.events = {}; this._text = '';
    this.value = ''; this.disabled = false;
  }
  set innerHTML(value) { throw new Error('Source markup must never be interpreted'); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  appendChild(node) { node.remove(); node.parentNode = this; this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children.forEach(node => node.parentNode = null); this.children = []; this._text = ''; nodes.forEach(node => this.appendChild(node)); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(node => node !== this); this.parentNode = null; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  addEventListener(type, handler) { (this.events[type] ||= []).push(handler); }
  removeEventListener(type, handler) { this.events[type] = (this.events[type] || []).filter(value => value !== handler); }
  emit(type, extra = {}) { if (type === 'click' && this.disabled) return; for (const handler of [...(this.events[type] || [])]) handler({target: this, ...extra}); }
  matches(selector) { return selector.startsWith('.') ? this.className.split(' ').includes(selector.slice(1)) : this.tagName === selector.toUpperCase(); }
  querySelectorAll(selector) { return this.children.flatMap(node => [...(node.matches(selector) ? [node] : []), ...node.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function fixture() {
  const document = new Element('document', null); document.ownerDocument = document;
  document.createElement = tag => new Element(tag, document); document.body = document.createElement('body');
  const scope = {document, module: {exports: {}}};
  vm.runInNewContext(fs.readFileSync(require.resolve('../../static/js/interactive_table.js'), 'utf8'), scope);
  const host = document.createElement('div'); document.body.appendChild(host);
  return {document, host, api: scope.module.exports};
}
const data = (rows = [{Name: 'Ride A', Value: 0}], more = {}) => ({type: 'table', title: 'Operations',
  columns: [{key: 'Name', label: 'Equipment'}, {key: 'Value', label: 'Value'}], rows,
  total_rows: rows.length, truncated: false, language: 'en', ...more});
const rows = host => host.querySelector('tbody').children.map(tr => tr.children.map(td => td.textContent));
const sort = (host, column = 1) => host.querySelector('thead').querySelectorAll('button')[column].emit('click');

test('normalization bounds the payload, projects selected columns, preserves scalar types and keeps literal prototype-like keys', () => {
  const h = fixture(), original = data([{Name: '<img src=x onerror=x>', Value: false, hidden: 'UNSELECTED'}]);
  const normalized = h.api.normalize(original);
  assert.equal(normalized.rows[0].Value, false); assert.equal(normalized.rows[0].hidden, undefined);
  assert.equal(original.rows[0].hidden, 'UNSELECTED'); assert.ok(!JSON.stringify(normalized).includes('UNSELECTED'));
  const special = JSON.parse('{"__proto__":"literal","constructor":"source"}');
  const result = h.api.normalize(data([special], {columns: [{key: '__proto__', label: 'Literal'}, {key: 'constructor', label: 'Constructor'}]}));
  assert.equal(result.rows[0].__proto__, 'literal'); assert.equal(result.rows[0].constructor, 'source');
  assert.equal(h.api.normalize(data([{Name: null, Value: 0}])).rows[0].Value, 0);
});

test('normalization rejects malformed or oversized rows, mismatched totals and unsafe cell structures', () => {
  const h = fixture();
  for (const source of [data([], {columns: []}), data([], {columns: Array(13).fill({key: 'Name', label: 'Name'})}),
    data(Array(501).fill({Name: 'N'})), data([[]]), data([{Name: NaN}]), data([{Name: Infinity}]),
    data([{Name: {html: '<script>'}}]), data([{Name: 'x'.repeat(8001)}]),
    data([], {total_rows: 10, truncated: false}), data([], {total_rows: -1}), data([], {truncated: true}),
    data([], {columns: [{key: 'Name'}, {key: 'Name'}]}), data([], {columns: [{key: 'Name', type: 'script'}]})])
    assert.throws(() => h.api.normalize(source));
});

test('renderer uses text nodes only, preserving zeros, false, blanks, missing values and truncation markers', () => {
  const h = fixture(), source = data([{Name: '<script>alert(1)</script>', Value: 0}, {Name: false, Value: ''},
    {Value: {text: '<b>cut', truncated: true, original_chars: 9000}}]);
  h.api.render(h.host, source);
  assert.deepEqual(rows(h.host), [['<script>alert(1)</script>', '0'], ['false', ''], ['—', '<b>cutTruncated']]);
  assert.equal(h.host.querySelector('script'), null); assert.equal(h.host.querySelector('img'), null);
  assert.equal(h.host.querySelector('a'), null); assert.equal(h.host.querySelector('.eit-truncation').title, '9000 characters');
  assert.equal(h.host.querySelector('th').getAttribute('scope'), 'col');
  assert.equal(h.host.querySelector('.eit-table').getAttribute('translate'), 'no');
});

test('inferred numeric sorting preserves numeric strings and stable ties with missing values last in both directions', () => {
  const h = fixture(); h.api.render(h.host, data([{Name: 'Twelve', Value: '12'}, {Name: 'Three', Value: '3'},
    {Name: 'Zero', Value: 0}, {Name: 'Missing', Value: null}, {Name: 'First Two', Value: 2}, {Name: 'Second Two', Value: '2'}]));
  sort(h.host);
  assert.deepEqual(rows(h.host).map(row => row[1]), ['0', '2', '2', '3', '12', '—']);
  assert.deepEqual(rows(h.host).slice(1, 3).map(row => row[0]), ['First Two', 'Second Two']);
  assert.equal(h.host.querySelector('thead').children[0].children[1].getAttribute('aria-sort'), 'ascending');
  sort(h.host); assert.deepEqual(rows(h.host).map(row => row[1]), ['12', '3', '2', '2', '0', '—']);
});

test('date sorting uses actual timestamps and invalid numeric/date values stay after valid values', () => {
  const h = fixture(), columns = [{key: 'Name', label: 'Name'}, {key: 'Value', label: 'Occurred', type: 'date'}];
  h.api.render(h.host, data([{Name: 'Later', Value: '2026-11-13T12:00:00+08:00'},
    {Name: 'Earlier', Value: '2026-11-12T23:00:00Z'}, {Name: 'Invalid', Value: 'not a date'}], {columns}));
  sort(h.host); assert.deepEqual(rows(h.host).map(row => row[0]), ['Earlier', 'Later', 'Invalid']);
  h.api.render(h.host, data([{Name: 'Unknown', Value: 'not numeric'}, {Name: 'Amount', Value: '12'}, {Name: 'Zero', Value: 0}],
    {columns: [{key: 'Name', label: 'Name'}, {key: 'Value', label: 'Amount', type: 'number'}]}));
  assert.deepEqual(rows(h.host).map(row => row[0]), ['Zero', 'Amount', 'Unknown']);
});

test('loaded-row search, per-column filtering and pagination report their real scope and reset the current page', () => {
  const h = fixture(), source = data(Array.from({length: 23}, (_, index) => ({Name: index % 2 ? 'Odd' : 'Even', Value: index})),
    {total_rows: 2000, truncated: true}); h.api.render(h.host, source);
  assert.equal(rows(h.host).length, 10); assert.match(h.host.querySelector('.eit-status').textContent, /Loaded 23 \/ 2000 source records/);
  assert.equal(h.host.querySelector('.eit-subset').hidden, false);
  assert.ok(h.host.querySelector('.eit-subset').textContent.includes('only to loaded rows'));
  const next = h.host.querySelector('.eit-next'); next.emit('click'); next.emit('click');
  assert.equal(rows(h.host).length, 3); assert.equal(next.disabled, true); assert.equal(h.host._eitState.page, 2);
  const search = h.host.querySelector('.eit-search'); search.value = 'even'; search.emit('input');
  assert.equal(h.host._eitState.page, 0); assert.match(h.host.querySelector('.eit-status').textContent, /12 loaded rows match/);
  assert.equal(rows(h.host).length, 10);
  const column = h.host.querySelector('.eit-column-filter'); column.value = 'Value'; column.emit('change');
  assert.equal(rows(h.host).length, 1); assert.equal(rows(h.host)[0][0], 'No matches in the loaded rows.');
  search.value = '2'; search.emit('input');
  assert.deepEqual(rows(h.host).map(row => row[1]), ['2', '12', '20', '21', '22']);
  h.host.querySelector('.eit-page-size').value = '20'; h.host.querySelector('.eit-page-size').emit('change');
  assert.equal(h.host._eitState.pageSize, 20);
});

test('table controls localize without translating source labels or values and preference state is serializable only', () => {
  const h = fixture(); h.api.render(h.host, data([{Name: 'Previous', Value: 2}],
    {columns: [{key: 'Name', label: 'Previous'}, {key: 'Value', label: 'Amount'}]}));
  const search = h.host.querySelector('.eit-search'); search.value = 'previous'; search.emit('input'); sort(h.host);
  h.document.emit('fos:languagechange', {detail: {language: 'zh-hans'}});
  assert.equal(h.host.querySelector('.eit-previous').textContent, '\u4e0a\u4e00\u9875');
  assert.equal(h.host.querySelector('.eit-search').getAttribute('aria-label'), '\u641c\u7d22\u5df2\u8f7d\u5165\u7684\u884c');
  assert.equal(h.host.querySelector('.eit-column-label').textContent, 'Previous'); assert.equal(rows(h.host)[0][0], 'Previous');
  assert.deepEqual(Object.keys(h.host._eitState).sort(), ['column', 'columnsSignature', 'descending', 'page', 'pageSize', 'query', 'sort']);
  assert.doesNotThrow(() => JSON.stringify(h.host._eitState)); assert.equal(h.host._eitState.query, 'previous');
});

test('redraws preserve preferences while replacing data and teardown removes controls, document listeners and source references', () => {
  const h = fixture(); h.api.render(h.host, data([{Name: 'Visible Old Row', Value: 10}, {Name: 'Other', Value: 20}]));
  const oldSearch = h.host.querySelector('.eit-search'); oldSearch.value = 'visible'; oldSearch.emit('input'); sort(h.host);
  const saved = JSON.parse(JSON.stringify(h.host._eitState));
  h.api.render(h.host, data([{Name: 'Visible New Row', Value: 5}]));
  assert.equal(h.host.querySelector('.eit-search').value, 'visible'); assert.equal(rows(h.host)[0][0], 'Visible New Row');
  assert.ok(!h.host.textContent.includes('Old Row')); assert.equal(oldSearch.events.input.length, 0);
  assert.equal(h.document.events['fos:languagechange'].length, 1);
  const second = h.document.createElement('div'); second._eitState = saved;
  h.api.render(second, data([{Name: 'Visible Replacement', Value: 3}]));
  assert.equal(second.querySelector('.eit-search').value, 'visible');
  h.host._ercCleanup(); assert.equal(h.host.querySelector('.eit-table'), null); assert.equal(h.host._ercCleanup, undefined);
  assert.equal(h.document.events['fos:languagechange'].length, 1); h.api.destroy(second); h.api.destroy(second);
  assert.equal(h.document.events['fos:languagechange'].length, 0);
  assert.ok(!JSON.stringify(h.host._eitState).includes('Visible Old Row'));
});

test('empty data has truthful empty status and a new column schema cannot keep stale filters or sorting', () => {
  const h = fixture(); h.api.render(h.host, data([]));
  assert.equal(rows(h.host)[0][0], 'No loaded rows.'); assert.equal(h.host.querySelector('.eit-next').disabled, true);
  h.host._eitState.column = 'Value'; h.host._eitState.sort = 'Value';
  h.api.render(h.host, data([{Other: 1}], {columns: [{key: 'Other', label: 'Other'}]}));
  assert.equal(h.host._eitState.column, ''); assert.equal(h.host._eitState.sort, '');
  assert.equal(rows(h.host)[0][0], '1');
});
