const {test} = require('node:test');
const assert = require('node:assert/strict');
const extended = require('../../static/js/extended_visualizations.js');
const runtime = require('../../static/js/report_chart_runtime.js');

class Node {
  constructor(tag = '', document = null) {
    this.tagName = tag; this.ownerDocument = document; this.children = []; this.attributes = {};
    this.events = {}; this.style = {}; this.className = ''; this.parentNode = null; this.hidden = false; this._text = '';
    this.classList = {
      add: name => { this.className = [...new Set(this.className.split(' ').filter(Boolean).concat(name))].join(' '); },
      remove: name => { this.className = this.className.split(' ').filter(value => value !== name).join(' '); },
    };
  }
  appendChild(node) { node.remove(); node.parentNode = this; this.children.push(node); return node; }
  append(...nodes) { for (const node of nodes) this.appendChild(node); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(node => node !== this); this.parentNode = null; }
  replaceChildren(...nodes) { for (const node of this.children) node.parentNode = null; this.children = []; this._text = ''; this.append(...nodes); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  setAttribute(key, value) { this.attributes[key] = String(value); if (key === 'class') this.className = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  addEventListener(type, fn) { (this.events[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this.events[type] = (this.events[type] || []).filter(value => value !== fn); }
  emit(type, extra = {}) { for (const fn of [...(this.events[type] || [])]) fn({target: this, preventDefault() {}, ...extra}); }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  all() { return this.children.flatMap(child => [child, ...child.all()]); }
  getBoundingClientRect() { return {left: 100, top: 120, bottom: 138, right: 118, width: 18, height: 18}; }
}
function fixture() {
  const document = new Node('document'); document.createElement = tag => new Node(tag, document);
  document.createElementNS = (_ns, tag) => document.createElement(tag);
  const window = new Node('window'); window.innerWidth = 800; window.innerHeight = 600;
  document.defaultView = window; document.body = document.createElement('body');
  const host = document.createElement('div'); document.body.appendChild(host);
  return {document, window, host, nodes: () => host.all(), tips: () => document.body.all().filter(node => node.className === 'erc-tooltip')};
}
const spark = extra => ({type: 'sparkline', title: 'Requests', labels: ['A', 'B', 'C', 'D'],
  series: [{label: 'Count', values: [0, null, 4, 6]}], ...extra});

test('progress shows actual and target values, caps only the physical bar, and exposes actual overachievement', () => {
  const h = fixture(); runtime.render(h.host, {type: 'progress', title: 'Completion', value: 125, target: 100, unit: 'jobs'});
  assert.ok(h.host.textContent.includes('125 jobs')); assert.ok(h.host.textContent.includes('/ 100 jobs'));
  assert.ok(h.host.textContent.includes('125%'));
  assert.equal(h.nodes().find(node => node.className === 'erc-progress-fill').style.width, '100%');
  const track = h.nodes().find(node => node.getAttribute('role') === 'progressbar');
  assert.equal(track.getAttribute('aria-valuenow'), '100'); assert.ok(track.getAttribute('aria-valuetext').includes('125 jobs'));
  track.emit('focus'); assert.equal(h.tips()[0].hidden, false); assert.ok(h.tips()[0].textContent.includes('125%'));
  assert.equal(track.getAttribute('tabindex'), '0');
});

test('progress distinguishes zero from missing and rejects invalid or overflowing targets', () => {
  const h = fixture(); runtime.render(h.host, {type: 'progress', value: 0, target: 10});
  assert.ok(h.host.textContent.includes('0%')); assert.equal(h.nodes().find(node => node.className === 'erc-progress-fill').style.width, '0%');
  runtime.render(h.host, {type: 'progress', value: null, target: 10});
  assert.ok(h.host.textContent.includes('Actual value unavailable')); assert.ok(h.host.textContent.includes('—'));
  assert.equal(h.nodes().some(node => node.getAttribute('aria-valuenow') !== null), false);
  for (const value of [{value: 1, target: 0}, {value: -1, target: 10}, {value: 1, target: Infinity}, {value: 1e308, target: 1e-308}])
    assert.throws(() => extended.normalize({type: 'progress', ...value}));
});

test('sparkline retains true zeros, missing gaps, small markers and irregular numeric spacing', () => {
  const h = fixture(); runtime.render(h.host, spark());
  assert.equal(h.nodes().filter(node => node.tagName === 'path').length, 1);
  assert.equal(h.nodes().filter(node => node.tagName === 'circle' && node.getAttribute('r') === '2.2').length, 3);
  const hits = h.nodes().filter(node => node.getAttribute('fill') === 'transparent');
  assert.equal(hits.length, 3); hits[0].emit('pointerenter'); assert.ok(h.tips()[0].textContent.includes('Count: 0'));
  runtime.render(h.host, spark({labels: ['0', '1', '100'], series: [{label: 'Count', values: [1, 2, 3]}], x_values: [0, 1, 100]}));
  const dots = h.nodes().filter(node => node.tagName === 'circle' && node.getAttribute('r') === '2.2');
  assert.ok(Number(dots[1].getAttribute('cx')) - Number(dots[0].getAttribute('cx')) < 10);
  assert.ok(Number(dots[2].getAttribute('cx')) - Number(dots[1].getAttribute('cx')) > 500);
  assert.throws(() => extended.normalize(spark({x_values: [0, 2, 1, 4]})));
  assert.throws(() => extended.normalize(spark({labels: Array(501).fill('A')})));
});

test('sparkline preserves finite geometry for extreme but finite values and treats all missing data honestly', () => {
  const h = fixture(); runtime.render(h.host, spark({labels: ['A', 'B'], series: [{label: 'Count', values: [-1e308, 1e308]}]}));
  const path = h.nodes().find(node => node.tagName === 'path'); assert.doesNotMatch(path.getAttribute('d'), /NaN|Infinity/);
  runtime.render(h.host, spark({series: [{label: 'Count', values: [null, null, null, null]}]}));
  assert.ok(h.host.textContent.includes('No numeric values')); assert.equal(h.nodes().filter(node => node.tagName === 'svg').length, 0);
});

test('heatmap draws bounded categories and distinguishes missing coordinates, explicit nulls and true zero', () => {
  const h = fixture(); runtime.render(h.host, {type: 'heatmap', title: 'Failures', x_labels: ['Monday', 'Tuesday'], y_labels: ['Pump A', 'Pump B'],
    cells: [{x: 0, y: 0, value: 0}, {x: 1, y: 0, value: 9}, {x: 0, y: 1, value: null}]});
  const rects = h.nodes().filter(node => node.tagName === 'rect'); assert.equal(rects.length, 4);
  rects[0].emit('focus'); assert.ok(h.tips()[0].textContent.includes('Pump A: 0'));
  rects[3].emit('focus'); assert.ok(h.tips()[0].textContent.includes('Pump B: —'));
  assert.equal(rects[3].getAttribute('fill'), 'var(--surface-2,#edf2f8)');
  assert.ok(h.host.textContent.includes('Range: 0 – 9'));
  assert.throws(() => extended.normalize({type: 'heatmap', x_labels: ['A'], y_labels: ['B'], cells: [{x: 1, y: 0, value: 1}]}));
  assert.throws(() => extended.normalize({type: 'heatmap', x_labels: ['A'], y_labels: ['B'], cells: [{x: 0, y: 0, value: 1}, {x: 0, y: 0, value: 2}]}));
});

test('Pareto sorts exact category values and derives cumulative percentage against the complete total', () => {
  const h = fixture(); runtime.render(h.host, {type: 'pareto', title: 'Downtime causes', labels: ['Small', 'Large', 'Medium'],
    series: [{label: 'Minutes', values: [1, 6, 3]}]});
  const bars = h.nodes().filter(node => node.tagName === 'rect'); assert.equal(bars.length, 3);
  assert.ok(bars[0].getAttribute('aria-label').startsWith('Large\n')); assert.ok(bars[0].getAttribute('aria-label').includes('60%'));
  assert.ok(bars[1].getAttribute('aria-label').includes('90%')); assert.ok(bars[2].getAttribute('aria-label').includes('100%'));
  const hits = h.nodes().filter(node => node.getAttribute('fill') === 'transparent'); hits[2].emit('click');
  assert.ok(h.tips()[0].textContent.includes('Cumulative: 100%'));
  assert.ok(h.nodes().some(node => node.getAttribute('stroke-dasharray') === '4 4'));
  assert.throws(() => extended.normalize({type: 'pareto', labels: ['A'], series: [{values: [-1]}]}));
  runtime.render(h.host, {type: 'pareto', labels: ['A', 'B'], series: [{values: [0, 0]}]});
  assert.ok(h.host.textContent.includes('All values are zero')); assert.equal(h.nodes().filter(node => node.tagName === 'path').length, 0);
});

test('timeline uses actual time positions and durations, handles milestones, and keeps original source labels in tooltips', () => {
  const h = fixture(), start = Date.UTC(2026, 0, 1);
  runtime.render(h.host, {type: 'timeline', title: 'Maintenance', items: [
    {label: 'Short task', start, end: start + 3600000, group: 'Team A', start_label: '2026-01-01 08:00 +08:00'},
    {label: 'Long task', start: start + 3600000, end: start + 10800000, group: 'Team B'},
    {label: 'Milestone', start: start + 10800000, end: start + 10800000},
  ]});
  const bars = h.nodes().filter(node => node.tagName === 'rect'); assert.equal(bars.length, 2);
  assert.ok(Math.abs(Number(bars[1].getAttribute('width')) / Number(bars[0].getAttribute('width')) - 2) < .0001);
  assert.ok(Number(bars[1].getAttribute('x')) > Number(bars[0].getAttribute('x')));
  assert.equal(h.nodes().filter(node => node.tagName === 'circle' && node.getAttribute('r') === '3').length, 1);
  assert.equal(h.nodes().filter(node => node.tagName === 'circle' && node.getAttribute('r') === '8').length, 1);
  bars[0].emit('focus'); assert.ok(h.tips()[0].textContent.includes('2026-01-01 08:00 +08:00'));
  assert.ok(h.host.textContent.includes('UTC'));
  assert.notEqual(bars[0].getAttribute('fill'), bars[1].getAttribute('fill'));
  assert.throws(() => extended.normalize({type: 'timeline', items: [{label: 'Bad', start: 10, end: 1}]}));
  assert.throws(() => extended.normalize({type: 'timeline', items: Array(101).fill({label: 'Too many', start: 0, end: 1})}));
});

test('all extended source labels remain literal, common omitted-data metadata is retained, and system text localizes', () => {
  const malicious = '<img src=x onerror=alert(1)>', h = fixture();
  const normalized = extended.normalize({type: 'heatmap', title: malicious, x_labels: [malicious], y_labels: ['Device'],
    cells: [{x: 0, y: 0, value: 1}], meta: {omitted_groups: 2, other_included: false, script: 'ignored'}}, 'zh-hans');
  assert.deepEqual(normalized.meta, {omitted_groups: 2, other_included: false});
  runtime.render(h.host, normalized); assert.equal(h.nodes().filter(node => node.tagName === 'img' || node.tagName === 'script').length, 0);
  h.nodes().find(node => node.tagName === 'rect').emit('focus'); assert.ok(h.tips()[0].textContent.includes(malicious));
  assert.ok(h.host.textContent.includes('范围'));
  assert.throws(() => extended.normalize({...normalized, meta: {omitted_groups: -1}}));
});

test('switching extended charts, metrics and tables cleans tooltip portals and table-owned resources', () => {
  const h = fixture(); runtime.render(h.host, spark()); assert.equal(h.tips().length, 1);
  runtime.render(h.host, {type: 'progress', value: 0, target: 10}); assert.equal(h.tips().length, 1);
  runtime.render(h.host, {type: 'metric', value: 42}); assert.equal(h.tips().length, 0);
  let disposed = 0;
  h.host._ercCleanup = () => { disposed++; };
  runtime.render(h.host, spark()); assert.equal(disposed, 1);
  extended.destroy(h.host); assert.equal(h.tips().length, 0); assert.equal(h.window.events.scroll.length, 0);
});
