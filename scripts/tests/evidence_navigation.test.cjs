const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = document; this.children = [];
    this.parentNode = null; this.className = ''; this.attributes = {}; this.events = {}; this.style = {};
    this._text = ''; this._html = ''; this.disabled = false;
    this.classList = {toggle: (name, on) => {
      const has = this.className.split(' ').includes(name), enabled = on === undefined ? !has : !!on;
      this.className = this.className.split(' ').filter(value => value && value !== name).concat(enabled ? [name] : []).join(' ');
    }};
  }
  appendChild(node) { node.remove(); node.parentNode = this; this.children.push(node); return node; }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  prepend(node) { node.remove(); node.parentNode = this; this.children.unshift(node); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(node => node !== this); this.parentNode = null; }
  set textContent(value) { this.children.forEach(node => node.parentNode = null); this.children = []; this._text = String(value); }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  set innerHTML(value) { this.textContent = ''; this._html = String(value); }
  get innerHTML() { return this._html; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  addEventListener(type, handler) { (this.events[type] ||= []).push(handler); }
  removeEventListener(type, handler) { this.events[type] = (this.events[type] || []).filter(value => value !== handler); }
  emit(type, extra = {}) { if (type === 'click' && this.disabled) return; for (const handler of [...(this.events[type] || [])]) handler({target: this, ...extra}); }
  matches(selector) { return selector.startsWith('.') ? this.className.split(' ').includes(selector.slice(1)) : this.tagName === selector.toUpperCase(); }
  querySelectorAll(selector) { return this.children.flatMap(node => [...(node.matches(selector) ? [node] : []), ...node.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  contains(target) { return this === target || this.children.some(node => node.contains(target)); }
  get isConnected() { return this.ownerDocument.body.contains(this); }
  showModal() { this.open = true; }
  close() { this.open = false; this.emit('close'); }
}
const preview = (source, extra = {}) => ({ok: true, source, doc_id: source.replace('.pdf', ''),
  pages: [42, 42], source_pages: [42], page_label: 'Page 43', has_pdf: true,
  quote: source + ' exact quote', blocks: [], ...extra});
const tick = () => new Promise(resolve => setImmediate(resolve));
function fixture(responses) {
  const document = new Element('document', null); document.ownerDocument = document;
  document.body = new Element('body', document); document.createElement = tag => new Element(tag, document);
  const calls = [], notices = [], window = {FOSI18n: {language: 'en'}, fosToast: (...args) => notices.push(args)};
  const fetch = async (url, options) => {
    const chunk = decodeURIComponent(url.split('/')[3]); calls.push({chunk, ...options});
    let data = responses[chunk]; if (typeof data === 'function') data = await data(options);
    return {ok: !data?.status, status: data?.status || 200, json: async () => data};
  };
  vm.runInNewContext(fs.readFileSync(require.resolve('../../static/js/evidence_panel.js'), 'utf8'),
    {document, window, fetch, AbortController, fosToast: window.fosToast});
  return {document, window, calls, notices, dialog: () => document.body.querySelector('dialog')};
}

test('single-page citations can navigate forward and backward across documents with correct boundaries', async () => {
  const h = fixture({a: preview('First.pdf'), b: preview('Second.pdf')});
  const citations = [{chunk_id: 'a', source: 'First.pdf', page: 'Page 43', citation_number: 2},
    {chunk_id: 'b', source: 'Second.pdf', page: 'Page 8', citation_number: 5}];
  await h.window.fosEvidencePanel('a', {citations});
  let panel = h.dialog();
  assert.equal(panel.querySelector('.ev-prev-citation').disabled, true);
  assert.equal(panel.querySelector('.ev-next-citation').disabled, false);
  assert.equal(panel.querySelector('.ev-next-citation').textContent, 'Next Citation \u203a');
  assert.match(panel.querySelector('.ev-page-idx').textContent, /Citation 1 \/ 2.*\[2\]/);
  panel.querySelector('.ev-next-citation').emit('click'); await tick(); panel = h.dialog();
  assert.equal(h.calls[1].chunk, 'b'); assert.equal(h.document.body.querySelectorAll('dialog').length, 1);
  assert.equal(panel.querySelector('.ev-prev-citation').disabled, false);
  assert.equal(panel.querySelector('.ev-next-citation').disabled, true);
  assert.match(panel.querySelector('.ev-page-idx').textContent, /Citation 2 \/ 2.*\[5\]/);
  assert.match(panel.querySelector('.ev-quote').innerHTML, /Second.pdf exact quote/);
  assert.equal(panel.querySelectorAll('a')[0].href, '/kb/doc/Second/html/');
  panel.querySelector('.ev-prev-citation').emit('click'); await tick();
  assert.equal(h.calls[2].chunk, 'a'); assert.equal(h.calls[1].cache, 'no-store');
});

test('existing chunk callers navigate deduplicated evidence and a lone citation disables only its boundaries', async () => {
  const h = fixture({a: preview('First.pdf'), b: preview('Second.pdf')});
  await h.window.fosEvidencePanel('a', {chunks: [{chunk_id: 'a'}, null, {}, {chunk_id: 'a'}, {chunk_id: 'b'}]});
  assert.equal(h.dialog().querySelector('.ev-citation-select').children.length, 2);
  h.dialog().querySelector('.ev-next-citation').emit('click'); await tick(); assert.equal(h.calls[1].chunk, 'b');
  await h.window.fosEvidencePanel('a');
  assert.equal(h.dialog().querySelector('.ev-prev-citation').disabled, true);
  assert.equal(h.dialog().querySelector('.ev-next-citation').disabled, true);
  assert.equal(h.dialog().querySelector('.ev-citation-select'), null);
});

test('multi-page evidence retains a bounded page selector separate from citation navigation', async () => {
  const h = fixture({a: preview('First.pdf', {pages: [0, 7], source_pages: [0, 7], blocks: [
    {page: 7, bbox: [10, 20, 200, 300], kind: 'image'},
  ]})});
  await h.window.fosEvidencePanel('a'); const panel = h.dialog(), select = panel.querySelector('.ev-citation-page-select');
  assert.equal(select.getAttribute('aria-label'), 'Page Within Citation');
  assert.equal(select.children[1].textContent, 'Page 8');
  assert.equal(panel.querySelector('.ev-next-citation').disabled, true);
  select.value = '7'; select.emit('change');
  assert.equal(panel.querySelector('img').src, '/kb/evidence/a/page.png?page=7');
  assert.equal(panel.querySelector('.ev-box').style.left, '1%');
  select.value = '99'; select.emit('change'); assert.match(panel.querySelector('img').src, /page=7$/);
  assert.equal(h.calls.length, 1);
});

test('non-PDF citations retain original quotes and can navigate using the citation selector', async () => {
  const h = fixture({a: preview('First.txt', {has_pdf: false}), b: preview('Second.pdf')});
  await h.window.fosEvidencePanel('a', {citations: [{chunk_id: 'a'}, {chunk_id: 'b', source: '<img onerror=x>.pdf'}]});
  const panel = h.dialog(), select = panel.querySelector('.ev-citation-select');
  assert.equal(panel.querySelector('img'), null); assert.match(panel.querySelector('.ev-quote').innerHTML, /First.txt exact quote/);
  assert.ok(select.children[1].textContent.includes('<img onerror=x>.pdf')); assert.equal(select.querySelector('img'), null);
  select.value = 'b'; select.emit('change'); await tick(); assert.equal(h.calls[1].chunk, 'b');
});

test('citation navigation localizes while open and releases its language listener on close', async () => {
  const h = fixture({a: preview('First.pdf')}); await h.window.fosEvidencePanel('a');
  h.window.FOSI18n.language = 'zh-hans'; h.document.emit('fos:languagechange');
  assert.equal(h.dialog().querySelector('.ev-prev-citation').getAttribute('aria-label'), '\u4e0a\u4e00\u6761\u5f15\u7528');
  assert.match(h.dialog().querySelector('.ev-page-idx').textContent, /\u5f15\u7528 1 \/ 1/);
  h.dialog().close(); assert.equal(h.document.events['fos:languagechange'].length, 0); assert.equal(h.dialog(), null);
});

test('a failed citation read retains the open evidence and a superseded response cannot replace newer evidence', async () => {
  let finishOld;
  const h = fixture({a: preview('First.pdf'), denied: {status: 403},
    slow: () => new Promise(resolve => { finishOld = resolve; }), b: preview('Second.pdf')});
  await h.window.fosEvidencePanel('a'); const original = h.dialog();
  await h.window.fosEvidencePanel('denied'); assert.equal(h.dialog(), original); assert.equal(h.notices.length, 1);
  const slow = h.window.fosEvidencePanel('slow'); await h.window.fosEvidencePanel('b');
  finishOld(preview('STALE.pdf')); await slow;
  assert.match(h.dialog().querySelector('.ev-quote').innerHTML, /Second.pdf/);
  assert.equal(h.calls.find(call => call.chunk === 'slow').signal.aborted, true);
});

test('closing the viewer while another citation loads cancels that navigation without reopening it', async () => {
  let finish;
  const h = fixture({a: preview('First.pdf'), b: () => new Promise(resolve => { finish = resolve; })});
  await h.window.fosEvidencePanel('a');
  const pending = h.window.fosEvidencePanel('b'); h.dialog().close();
  finish(preview('Second.pdf')); await pending;
  assert.equal(h.dialog(), null); assert.equal(h.calls[1].signal.aborted, true);
});
