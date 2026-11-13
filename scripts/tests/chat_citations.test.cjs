const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class FakeNode {
  constructor(tag, document, type = 1, value = '') {
    this.tagName = tag.toUpperCase(); this.ownerDocument = document; this.nodeType = type;
    this.nodeValue = type === 3 ? value : null; this.childNodes = []; this.parentNode = null;
    this.attributes = {}; this.className = ''; this.events = {}; this.id = '';
    this.classList = {add: value => { this.className = [...new Set(this.className.split(' ').filter(Boolean).concat(value))].join(' '); },
      remove: value => { this.className = this.className.split(' ').filter(item => item !== value).join(' '); },
      toggle: (value, force) => {
        const next = force === undefined ? !this.className.split(' ').includes(value) : !!force;
        if (next) this.classList.add(value); else this.classList.remove(value); return next;
      }};
  }
  get children() { return this.childNodes.filter(child => child.nodeType === 1); }
  get textContent() { return this.nodeType === 3 ? this.nodeValue : this.childNodes.map(child => child.textContent).join(''); }
  set textContent(value) { this.replaceChildren(this.ownerDocument.createTextNode(String(value))); }
  appendChild(child) {
    if (child.nodeType === 11) { for (const item of [...child.childNodes]) this.appendChild(item); return child; }
    child.remove(); child.parentNode = this; this.childNodes.push(child); return child;
  }
  append(...children) { for (const child of children) this.appendChild(child); }
  insertBefore(child, before) {
    child.remove(); child.parentNode = this;
    const position = this.childNodes.indexOf(before); if (position < 0) throw new Error('Missing insertion target');
    this.childNodes.splice(position, 0, child); return child;
  }
  replaceChildren(...children) { for (const child of this.childNodes) child.parentNode = null; this.childNodes = []; this.append(...children); }
  replaceWith(...replacements) {
    if (!this.parentNode) return;
    const parent = this.parentNode, index = parent.childNodes.indexOf(this), flat = [];
    for (const replacement of replacements) flat.push(...(replacement.nodeType === 11 ? [...replacement.childNodes] : [replacement]));
    for (const replacement of flat) replacement.remove();
    parent.childNodes.splice(index, 1, ...flat); this.parentNode = null;
    for (const replacement of flat) replacement.parentNode = parent;
  }
  remove() { if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter(child => child !== this); this.parentNode = null; }
  setAttribute(key, value) { this.attributes[key] = String(value); if (key === 'class') this.className = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  hasAttribute(key) { return Object.hasOwn(this.attributes, key); }
  matches(selector) {
    if (selector.startsWith('.')) return this.className.split(' ').includes(selector.slice(1));
    if (selector.startsWith('[')) return this.hasAttribute(selector.slice(1, -1));
    return this.tagName.toLowerCase() === selector;
  }
  all() { return this.childNodes.flatMap(child => [child, ...child.all()]); }
  querySelectorAll(selector) { const selectors = selector.split(',').map(value => value.trim()); return this.all().filter(node => node.nodeType === 1 && selectors.some(value => node.matches(value))); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  contains(node) { return node === this || this.childNodes.some(child => child.contains(node)); }
  addEventListener(type, handler) { (this.events[type] ||= []).push(handler); }
  emit(type, extra = {}) { const event = {target: this, preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }, ...extra}; for (const handler of this.events[type] || []) handler(event); return event; }
  focus() { this.focused = true; }
  scrollIntoView(options) { this.scrolled = options; }
}
function environment(options = {}) {
  const document = new FakeNode('document', null); document.ownerDocument = document;
  document.createElement = tag => new FakeNode(tag, document);
  document.createTextNode = text => new FakeNode('', document, 3, String(text));
  document.createDocumentFragment = () => new FakeNode('', document, 11);
  document.body = document.createElement('body'); document.appendChild(document.body);
  const evidence = [], scope = {document, module: {exports: {}}, fosEvidencePanel: (...args) => evidence.push(args)};
  if (options.visuals) {
    scope.window = scope;
    scope.EnGenieReportCharts = {render: (canvas, chart) => { canvas.textContent = chart.type + ': ' + chart.title; }};
    vm.runInNewContext(fs.readFileSync(require.resolve('../../static/js/chat_visualizations.js'), 'utf8'), scope);
    const template = fs.readFileSync(require.resolve('../../templates/kb/ask.html'), 'utf8');
    vm.runInNewContext(template.slice(template.indexOf('  function renderChatCharts('), template.indexOf('  function renderCitations(')), scope);
  }
  vm.runInNewContext(fs.readFileSync(require.resolve('../../static/js/chat_citations.js'), 'utf8'), scope);
  function message(content = '') {
    const ai = document.createElement('div'), bubble = document.createElement('div'), answer = document.createElement('div');
    bubble.className = 'bubble'; answer.className = 'answer'; answer.textContent = content;
    bubble.appendChild(answer); ai.appendChild(bubble); document.body.appendChild(ai); return {ai, bubble, answer};
  }
  return {document, api: scope.module.exports, message, evidence, scope};
}
const tracker = (number = 1, extra = {}) => ({kind: 'tracker', kb_slug: 'ride-waits', kb_name: 'Ride Wait Times', result_digest: 'query-a', citation_number: number, ...extra});
const doc = (number = 2, extra = {}) => ({doc_id: 'doc-1', source: 'Maintenance.pdf', citation_number: number, ...extra});

test('same-KB tracker citations are grouped once, named and retain distinct query counts', () => {
  const h = environment(), m = h.message('Wait time [1].');
  const citations = [tracker(), tracker(1, {result_digest: 'query-b'}), tracker(), doc()];
  assert.equal(h.api.render(m.ai, citations), 2);
  const chips = m.bubble.querySelectorAll('.chat-source-chip'); assert.equal(chips.length, 2);
  assert.ok(chips[0].textContent.includes('[1]')); assert.ok(chips[0].textContent.includes('Ride Wait Times'));
  assert.equal(chips[0].querySelector('.chat-source-count').textContent, '2');
  assert.equal(chips[0].querySelector('.chat-source-count').title, '2 queries');
  assert.equal(chips[0].href, '/kb/manage/ride-waits/'); assert.equal(chips[0].target, '_blank');
  assert.equal(h.api.grouped(citations)[0].items.length, 3);
  assert.equal(m.answer.querySelectorAll('.chat-inline-citation').length, 1);
});

test('only actual assigned numbers become inline links; code, existing links and unknown markers remain text', () => {
  const h = environment(), m = h.message('Fact [1] and document [2], unknown [9].');
  for (const tag of ['code', 'pre', 'a', 'button']) { const node = h.document.createElement(tag); node.textContent = 'Literal [1]'; m.answer.appendChild(node); }
  h.api.render(m.ai, [tracker(), doc()]);
  const links = m.answer.querySelectorAll('.chat-inline-citation'); assert.equal(links.length, 2);
  assert.equal(links[0].textContent, '[1]'); assert.equal(links[1].textContent, '[2]');
  assert.ok(m.answer.textContent.includes('unknown [9]')); assert.ok(m.answer.querySelector('code').textContent.includes('[1]'));
  const chip = m.bubble.querySelectorAll('.chat-source-chip')[0], event = links[0].emit('click');
  assert.equal(event.prevented, true); assert.equal(links[0].href, '#' + chip.id);
  assert.equal(chip.focused, true); assert.equal(chip.scrolled.block, 'nearest'); assert.ok(chip.className.includes('is-citation-focused'));
  assert.ok(links[0].getAttribute('aria-label').includes('Ride Wait Times'));
});

test('new streaming text is enhanced without rebuilding a stable source list or inspecting chart payloads', () => {
  const h = environment(), m = h.message('First [1]');
  const citation = tracker(); Object.defineProperty(citation, 'tracker_snapshot', {get() { throw new Error('Do not hash nested chart data'); }});
  const citations = [citation]; h.api.render(m.ai, citations);
  const row = m.bubble.querySelector('.chat-source-list'), firstId = row.querySelector('.chat-source-chip').id;
  for (let index = 0; index < 8; index++) {
    m.answer.textContent = 'A long streamed claim '.repeat(index + 1) + '[1]';
    h.api.render(m.ai, citations);
    assert.equal(m.bubble.querySelector('.chat-source-list'), row);
    assert.equal(m.answer.querySelectorAll('.chat-inline-citation').length, 1);
    assert.equal(row.querySelector('.chat-source-chip').id, firstId);
  }
});

test('citation replacement clears stale references and unknown or legacy list numbers do not assert source support', () => {
  const h = environment(), m = h.message('Old [1] and new [2].');
  h.api.render(m.ai, [tracker()]); assert.equal(m.answer.querySelectorAll('.chat-inline-citation').length, 1);
  h.api.render(m.ai, [doc()]); assert.equal(m.answer.querySelectorAll('.chat-inline-citation').length, 1);
  assert.equal(m.answer.querySelector('.chat-inline-citation').textContent, '[2]');
  h.api.render(m.ai, []); assert.equal(m.answer.querySelectorAll('.chat-inline-citation').length, 0);
  assert.equal(m.bubble.querySelectorAll('.cite-row').length, 0); assert.equal(m.answer.textContent, 'Old [1] and new [2].');
  h.api.render(m.ai, [tracker(undefined, {citation_number: undefined})]);
  assert.equal(m.bubble.querySelectorAll('.chat-source-chip').length, 1); assert.equal(m.answer.querySelectorAll('.chat-inline-citation').length, 0);
});

test('document evidence buttons preserve merged chunks and safe literal source and page names', () => {
  const h = environment(), name = '<img src=x onerror=alert(1)>.pdf', m = h.message('A documented claim [2].');
  h.api.render(m.ai, [doc(2, {source: name, chunks: [{chunk_id: 'c-1', page: '第 4 页'}]}),
    doc(2, {chunks: [{chunk_id: 'c-1', page: '第 4 页'}, {chunk_id: 'c-2', page: '5'}]})]);
  const chip = m.bubble.querySelector('.chat-source-chip'); assert.equal(chip.tagName, 'BUTTON');
  assert.ok(chip.textContent.includes(name)); assert.equal(chip.querySelectorAll('img').length, 0);
  assert.ok(chip.querySelector('.cite-page').textContent.includes('P4'));
  chip.emit('click'); assert.equal(h.evidence[0][0], 'c-1'); assert.equal(h.evidence[0][1].chunks.length, 2);
});

test('evidence navigation receives all cited document chunks and source numbers from this answer only', () => {
  const h = environment(), m = h.message('Claims [2] and [4].');
  h.api.render(m.ai, [doc(2, {chunks: [{chunk_id: 'a', page: 'Page 1'}, {chunk_id: 'a', page: 'Page 1'}]}),
    doc(4, {doc_id: 'doc-2', source: 'Second.pdf', chunks: [{chunk_id: 'b', page: 'Page 8'}]}), tracker()]);
  m.bubble.querySelector('.chat-source-chip').emit('click');
  const navigation = h.evidence[0][1].citations;
  assert.deepEqual(Array.from(navigation, item => [item.chunk_id, item.source, item.citation_number]),
    [['a', 'Maintenance.pdf', 2], ['b', 'Second.pdf', 4]]);
});

test('document fallback, graph links and source images keep protected destinations and deduplicate previews', () => {
  const h = environment(), m = h.message();
  h.api.render(m.ai, [doc(2, {highlights: ['quoted & literal'], selected_chunk_ids: ['chunk-1'],
    images: ['/kb/doc/doc-1/img/a.png', '/kb/doc/doc-1/img/a.png', 'https://evil.example/track', 'javascript:alert(1)', '/kb/doc/doc-1/img/../x']}),
    {kind: 'graph', relation_id: 'rel-1', fact_digest: 'f&1', source: '<b>Fact</b>', citation_number: 3}]);
  const chips = m.bubble.querySelectorAll('.chat-source-chip');
  assert.equal(chips[0].href, '/kb/doc/doc-1/slices/?chunk=chunk-1&h=quoted%20%26%20literal');
  assert.equal(chips[1].href, '/kb/graph/fact/rel-1/?v=f%261'); assert.ok(chips[1].textContent.includes('<b>Fact</b>'));
  const images = m.bubble.querySelectorAll('img'); assert.equal(images.length, 1); assert.equal(images[0].src, '/kb/doc/doc-1/img/a.png');
  assert.equal(h.api.protectedImage('/kb/evidence/chunk-1/page.png?page=2'), '/kb/evidence/chunk-1/page.png?page=2');
  assert.equal(h.api.protectedImage('/kb/doc/doc-1/img/%2e%2e/x'), '');
});

test('source IDs are scoped to each message and language switching changes controls while preserving names and markers', () => {
  const h = environment(), first = h.message('Source [1]'), second = h.message('Source [1]');
  const citations = [tracker()]; h.api.render(first.ai, citations); h.api.render(second.ai, citations);
  assert.notEqual(first.bubble.querySelector('.chat-source-chip').id, second.bubble.querySelector('.chat-source-chip').id);
  h.document.emit('fos:languagechange', {detail: {language: 'zh-hans'}});
  assert.equal(first.bubble.querySelector('.cite-label').textContent, '来源');
  assert.ok(first.bubble.querySelector('.chat-source-chip').textContent.includes('Ride Wait Times'));
  assert.equal(first.bubble.querySelector('.chat-source-kind').textContent, '追踪表');
  assert.equal(first.answer.querySelectorAll('.chat-inline-citation').length, 1);
  h.document.emit('fos:languagechange', {detail: {language: 'en'}});
  assert.equal(first.bubble.querySelector('.cite-label').textContent, 'Sources');
  h.api.destroy(h.document.body); assert.equal(first.answer.textContent, 'Source [1]');
  assert.equal(first.bubble.querySelector('.chat-source-list'), null);
});

test('invalid and conflicting source assignments never turn literal answer numbers into trusted links', () => {
  const h = environment(), m = h.message('Maybe [1] [2] [3] [4].');
  h.api.render(m.ai, [tracker(1), doc(1), {kind: 'tracker', kb_slug: 'other', kb_name: 'Other', citation_number: NaN}]);
  assert.equal(m.answer.querySelectorAll('.chat-inline-citation').length, 0);
  const shown = m.bubble.querySelectorAll('.chat-source-number').map(node => node.textContent);
  assert.equal(new Set(shown).size, shown.length); assert.ok(!shown.includes('[1]'));
  h.api.render(m.ai, [tracker(1), tracker(2)]);
  assert.equal(m.answer.querySelectorAll('.chat-inline-citation').length, 0);
  assert.equal(h.api.sourceKey(null), ''); assert.equal(h.api.sourceKey({kind: 'unknown'}), '');
});

test('legacy answer numbers remain literal unless the model received an inline citation assignment', () => {
  const h = environment(), m = h.message('Old incidental [1].');
  h.api.render(m.ai, [tracker(1, {citation_inline: false})]);
  assert.equal(m.bubble.querySelector('.chat-source-number').textContent, '[1]');
  assert.equal(m.answer.querySelectorAll('.chat-inline-citation').length, 0);
  assert.equal(m.answer.textContent, 'Old incidental [1].');
  h.api.render(m.ai, [tracker(1, {citation_inline: true})]);
  assert.equal(m.answer.querySelectorAll('.chat-inline-citation').length, 1);
  h.api.render(m.ai, [tracker(1, {citation_inline: true}), tracker(1, {result_digest: 'legacy-query', citation_inline: false})]);
  assert.equal(m.answer.querySelectorAll('.chat-inline-citation').length, 0);
  assert.equal(m.bubble.querySelectorAll('.chat-source-chip').length, 1);
});

test('selected KB images are compact answer cards before the source list with exact per-image pages', () => {
  const h = environment(), m = h.message('Here are two diagrams [2].');
  const citations = [doc(2, {
    images: ['/kb/doc/doc-1/img/a.png', '/kb/doc/doc-1/img/b.png', '/kb/doc/doc-1/img/a.png'],
    chunks: [{chunk_id: 'text-chunk', page: 'Page 99'}],
    image_snapshots: [
      {doc_id: 'doc-1', image: 'a.png', page: 'Page 3', visual_confirmed: false},
      {doc_id: 'doc-1', image: 'b.png', page: 'Page 9', visual_confirmed: true},
    ],
  })];
  h.api.render(m.ai, citations);
  const area = m.bubble.querySelector('.chat-answer-images'), row = m.bubble.querySelector('.chat-source-list');
  assert.ok(m.bubble.children.indexOf(area) > m.bubble.children.indexOf(m.answer));
  assert.ok(m.bubble.children.indexOf(area) < m.bubble.children.indexOf(row));
  assert.equal(area.getAttribute('aria-label'), 'Knowledge Base Images');
  assert.equal(m.bubble.querySelectorAll('.cite-imgs').length, 0);
  assert.equal(row.querySelectorAll('img').length, 0);
  const cards = area.querySelectorAll('.chat-kb-image-card'); assert.equal(cards.length, 2);
  for (const card of cards) {
    assert.ok(!card.className.includes('is-expanded'));
    assert.equal(card.querySelector('.chat-table-toggle').getAttribute('aria-expanded'), 'false');
    assert.equal(card.querySelector('.chat-kb-image-source').textContent, '[2] Maintenance.pdf');
    assert.equal(card.querySelector('img').loading, 'lazy');
    const original = card.querySelector('.chat-kb-image-original');
    assert.equal(original.href, card.querySelector('img').src); assert.equal(original.target, '_blank');
    assert.equal(original.rel, 'noopener');
  }
  assert.equal(cards[0].querySelector('.chat-kb-image-page').textContent, 'Page 3');
  assert.equal(cards[1].querySelector('.chat-kb-image-page').textContent, 'Page 9');
  assert.ok(!area.textContent.includes('Page 99')); assert.ok(!area.textContent.includes('verified'));
  cards[1].querySelector('.chat-kb-image-source').emit('click');
  assert.equal(row.querySelector('.chat-source-chip').focused, true);
  assert.equal(m.answer.textContent, 'Here are two diagrams [2].');
});

test('KB image controls preserve independent sizes through streaming, reselection and language changes', () => {
  const h = environment(), m = h.message('A diagram [2].');
  const a = '/kb/doc/doc-1/img/a.png', b = '/kb/doc/doc-1/img/b.png';
  const citations = [doc(2, {images: [a, b]})]; h.api.render(m.ai, citations);
  let cards = m.bubble.querySelectorAll('.chat-kb-image-card');
  cards[0].querySelector('.chat-kb-image-preview').emit('click');
  assert.equal(cards[0].querySelector('.chat-table-toggle').textContent, 'Collapse');
  assert.ok(cards[0].className.includes('is-expanded')); assert.ok(!cards[1].className.includes('is-expanded'));
  assert.equal(cards[0].querySelector('.chat-kb-image-preview').getAttribute('aria-expanded'), 'true');
  m.answer.textContent = 'A streamed diagram [2].'; h.api.render(m.ai, citations);
  assert.equal(m.bubble.querySelectorAll('.chat-kb-image-card')[0], cards[0]);
  h.api.render(m.ai, [doc(2, {images: [b, a]})]);
  cards = m.bubble.querySelectorAll('.chat-kb-image-card');
  assert.ok(!cards[0].className.includes('is-expanded')); assert.ok(cards[1].className.includes('is-expanded'));
  h.document.emit('fos:languagechange', {detail: {language: 'zh-hans'}});
  cards = m.bubble.querySelectorAll('.chat-kb-image-card');
  assert.equal(cards[1].querySelector('.chat-table-toggle').textContent, '\u6536\u8d77');
  assert.equal(cards[1].querySelector('.chat-table-title').textContent, 'Maintenance.pdf');
  cards[1].querySelector('.chat-table-head').emit('click');
  assert.equal(cards[1].querySelector('.chat-table-toggle').getAttribute('aria-expanded'), 'false');
  cards[1].querySelector('.chat-table-toggle').emit('click');
  assert.equal(cards[1].querySelector('.chat-table-toggle').getAttribute('aria-expanded'), 'true');
  assert.equal(cards[1].querySelector('.chat-table-toggle').getAttribute('aria-controls'), cards[1].querySelector('.chat-table-viewport').id);
  h.api.render(m.ai, []); assert.equal(m.bubble.querySelector('.chat-answer-images'), null);
  h.api.render(m.ai, citations); h.api.destroy(m.ai); assert.equal(m.bubble.querySelector('.chat-answer-images'), null);
});

test('page images use their exact chunk location, never guess missing pages, and tolerate unavailable images', () => {
  const h = environment(), m = h.message();
  h.api.render(m.ai, [doc(2, {
    images: ['/kb/evidence/image-chunk/page.png?page=2', '/kb/doc/doc-1/img/other.png'],
    chunks: [{chunk_id: 'text-chunk', page: 'Page 99'}, {chunk_id: 'image-chunk', page: 'Page 7', is_image: true}],
  })]);
  const cards = m.bubble.querySelectorAll('.chat-kb-image-card');
  assert.equal(cards[0].querySelector('.chat-kb-image-page').textContent, 'Page 7');
  assert.equal(cards[1].querySelector('.chat-kb-image-page'), null);
  const image = cards[0].querySelector('img'), unavailable = cards[0].querySelector('.chat-kb-image-unavailable');
  assert.equal(unavailable.hidden, true); image.emit('error');
  assert.equal(image.hidden, true); assert.equal(unavailable.hidden, false);
  image.emit('load'); assert.equal(image.hidden, false); assert.equal(unavailable.hidden, true);
  assert.equal(unavailable.textContent, 'Image Unavailable');
});

test('KB image source labels remain literal and card layout supports compact and expanded widths', () => {
  const h = environment(), m = h.message(), source = '<img src=x onerror=alert(1)>.pdf';
  h.api.render(m.ai, [doc(2, {source, images: ['/kb/doc/doc-1/img/a.png', 'data:image/svg+xml,<svg onload=alert(1)>']})]);
  const card = m.bubble.querySelector('.chat-kb-image-card');
  assert.equal(card.querySelector('.chat-table-title').textContent, source);
  assert.equal(card.querySelector('.chat-kb-image-source').textContent, '[2] ' + source);
  assert.equal(card.querySelectorAll('img').length, 1); assert.equal(card.querySelectorAll('script').length, 0);
  const css = fs.readFileSync(require.resolve('../../static/css/chat_citations.css'), 'utf8');
  assert.match(css, /\.chat-answer-images \.chat-kb-image-card\{[^}]*width:320px;max-width:100%/);
  assert.match(css, /\.chat-answer-images \.chat-kb-image-card\.is-expanded\{[^}]*flex-basis:100%;width:100%/);
});

test('actual tracker metric and chart cards share one wrapping row with KB photos and retain independent sizes', () => {
  const h = environment({visuals: true}), m = h.message('Metrics and photos [1] [2].');
  const citations = [tracker(1, {tracker_snapshot: {chart: {type: 'metric', title: 'Total', value: 42}}}),
    tracker(1, {result_digest: 'trend', tracker_snapshot: {chart: {type: 'line', title: 'Trend',
      labels: ['A', 'B'], series: [{label: 'Count', values: [0, 42]}]}}}),
    doc(2, {images: ['/kb/doc/doc-1/img/a.png']})];
  h.api.render(m.ai, citations); h.scope.renderChatCharts(m.ai, citations);
  const row = m.bubble.querySelector('.chat-visual-row'), photos = row.querySelector('.chat-answer-images');
  assert.equal(m.bubble.querySelectorAll('.chat-visual-row').length, 1);
  assert.equal(photos.parentNode, row); assert.equal(row.querySelector('.chat-citation-charts').parentNode, row);
  assert.equal(row.querySelectorAll('.chat-chart-card').length, 2); assert.equal(row.querySelectorAll('.chat-kb-image-card').length, 1);
  row.querySelector('.chat-chart-size').emit('click'); photos.querySelector('.chat-table-toggle').emit('click');
  assert.equal(row.querySelector('.chat-chart-size').getAttribute('aria-expanded'), 'true');
  assert.equal(photos.querySelector('.chat-table-toggle').getAttribute('aria-expanded'), 'true');
  h.document.emit('fos:languagechange', {detail: {language: 'zh-hans'}});
  assert.equal(row.querySelector('.chat-chart-size').getAttribute('aria-expanded'), 'true');
  assert.equal(row.querySelector('.chat-table-toggle').getAttribute('aria-expanded'), 'true');
  h.api.render(m.ai, [tracker()]); assert.equal(row.querySelectorAll('.chat-kb-image-card').length, 0);
  assert.equal(row.querySelectorAll('.chat-chart-card').length, 2);
  h.scope.renderChatCharts(m.ai, []); assert.equal(row.querySelectorAll('.chat-chart-card').length, 0);
  h.api.destroy(m.ai); h.scope.EnGenieChatVisualizations.destroy(m.ai);
});
