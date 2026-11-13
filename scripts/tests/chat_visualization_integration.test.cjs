const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../../templates/kb/ask.html'), 'utf8');
function between(start, end) {
  const first = template.indexOf(start);
  assert.notEqual(first, -1, 'Template marker: ' + start);
  const last = template.indexOf(end, first + start.length);
  assert.notEqual(last, -1, 'Template end marker: ' + end);
  return template.slice(first, last);
}
const renderSource = between('  function renderChatCharts(', '  function renderCitations(');
const sandboxSource = between('  async function runCodeInSandbox(', '\n  /* ----------');

class Element {
  constructor(tag, document) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = document;
    this.children = []; this.className = ''; this.style = {}; this.events = {}; this.attributes = {};
  }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  prepend(child) { child.parentNode = this; this.children.unshift(child); }
  replaceChildren(...children) { this.children = []; this.append(...children); this._html = ''; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, fn) { this.events[type] = fn; }
  contains(node) { return this === node || this.children.some(child => child.contains?.(node)); }
  matches(selector) { return selector.startsWith('.') ? this.className.split(/\s+/).includes(selector.slice(1)) : this.tagName === selector.toUpperCase(); }
  querySelectorAll(selector) { return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get innerHTML() { return this._html || ''; }
  set innerHTML(value) {
    this._html = String(value); this.children = [];
    // Only the pre-existing app shells are needed by these extracted functions.
    if (value.includes('class="cr-head"')) {
      const head = this.ownerDocument.createElement('div'); head.className = 'cr-head';
      const tag = this.ownerDocument.createElement('span'); tag.className = 'tag-ok'; head.appendChild(tag);
      const body = this.ownerDocument.createElement('div'); body.className = 'cr-body';
      const msg = this.ownerDocument.createElement('span'); msg.className = 'cr-msg'; body.appendChild(msg);
      this.append(head, body);
    } else if (value.includes('class="bubble"')) {
      const bubble = this.ownerDocument.createElement('div'); bubble.className = 'bubble';
      const answer = this.ownerDocument.createElement('div'); answer.className = 'answer'; bubble.appendChild(answer);
      this.appendChild(bubble);
    }
  }
}
function dom() {
  const document = {createElement(tag) { return new Element(tag, document); }};
  return document;
}
function chart(id = 'trend') { return {type:'line', title:id, labels:['A','B'], series:[{label:'Value',values:[0,2]}]}; }
function citation(id = 'one') { return {kind:'tracker',result_digest:id,tracker_snapshot:{chart:chart(id)}}; }
function chartFixture() {
  const document = dom(), mounts = [], destroyed = [];
  const api = {mount(...args) { mounts.push(args); return args[1].length; }, destroy(node) { destroyed.push(node); }};
  const window = {EnGenieChatVisualizations:api,FOSI18n:{language:'en'}};
  const scope = {window,EnGenieChatVisualizations:api,document};
  vm.createContext(scope); vm.runInContext(renderSource, scope);
  const ai = document.createElement('div'); ai.innerHTML = '<div class="bubble"><div class="answer"></div></div>';
  return {document,api,window,scope,ai,mounts,destroyed};
}

test('normal-chat template loads shared renderer before bridge and all inline scripts parse', () => {
  assert.ok(template.indexOf("'js/interactive_table.js'") < template.indexOf("'js/report_chart_runtime.js'"));
  assert.ok(template.indexOf("'js/extended_visualizations.js'") < template.indexOf("'js/report_chart_runtime.js'"));
  assert.ok(template.indexOf("'js/report_chart_runtime.js'") < template.indexOf("'js/chat_visualizations.js'"));
  assert.ok(template.includes("'css/chat_visualizations.css'"));
  let compiled = 0;
  for (const match of template.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
    const script = match[1].replace(/\{%[\s\S]*?%\}/g, '').replace(/\{\{[\s\S]*?\}\}/g, 'template_value');
    if (!script.trim()) continue;
    assert.doesNotThrow(() => new vm.Script(script)); compiled++;
  }
  assert.ok(compiled >= 1);
});

test('actual chat citation renderer uses trusted tracker snapshots, reuses its region and clears removed charts', () => {
  const h = chartFixture();
  h.scope.renderChatCharts(h.ai, [{kind:'document',chart:chart('untrusted')},citation('first'),{kind:'tracker'},citation('second')]);
  assert.equal(h.mounts.length,1); assert.deepEqual(Array.from(h.mounts[0][1],c=>c.title),['first','second']);
  assert.equal(h.mounts[0][2].language,'en');
  const area = h.ai.querySelector('.chat-citation-charts');
  assert.equal(h.ai.querySelectorAll('.chat-citation-charts').length,1);
  h.scope.renderChatCharts(h.ai,[citation('replacement')]);
  assert.equal(h.mounts[1][0],area); assert.equal(h.mounts[1][1][0].title,'replacement');
  h.scope.renderChatCharts(h.ai,[]);
  assert.equal(h.mounts[2][0],area); assert.equal(h.mounts[2][1].length,0);
  const h2 = chartFixture(); h2.scope.renderChatCharts(h2.ai,[]); assert.equal(h2.mounts.length,0);
});

test('citation chart region bounds chart count and honors the active interface language', () => {
  const h = chartFixture(); h.window.FOSI18n.language='zh-hans';
  h.scope.renderChatCharts(h.ai,Array.from({length:12},(_,i)=>citation(String(i))));
  assert.equal(h.mounts[0][1].length,8); assert.equal(h.mounts[0][2].language,'zh-hans');
  delete h.window.EnGenieChatVisualizations;
  h.scope.renderChatCharts(h.ai,[citation('ignored')]); assert.equal(h.mounts.length,1);
});

test('a source-validated exact document quantity is rendered as a value card', () => {
  const h=chartFixture();
  const spec={type:'metric',title:'Rated load',value:2500,unit:'kg'};
  h.scope.renderChatCharts(h.ai,[{doc_id:'manual',value_cards:[{chart:spec}]}]);
  assert.equal(h.mounts.length,1);assert.equal(h.mounts[0][1][0],spec);
  h.scope.renderChatCharts(h.ai,[]);assert.equal(h.mounts[1][1].length,0);
});

test('explicit source-field selections replace their lookup auto-card while retaining several selected cards and all evidence', () => {
  const h=chartFixture(),source=citation('lookup-result');
  source.tracker_snapshot.chart={type:'metric',title:'Auto temperature',value:25};
  source.tracker_snapshot.result_digest=source.result_digest;
  const selected=(id,field,value,type)=>({kind:'tracker',result_digest:id,tracker_snapshot:{
    result_digest:id,presentation_query:{source_result_digest:'lookup-result',field},
    chart:{type:'metric',title:field,value,...(type?{value_type:type}:{})}}});
  const status=selected('status-selection','Status','Open','text'),temperature=selected('temperature-selection','Temperature',25);
  h.scope.renderChatCharts(h.ai,[source]);
  assert.equal(h.mounts[0][1][0].title,'Auto temperature');
  const citations=[source,status];h.scope.renderChatCharts(h.ai,citations);
  assert.deepEqual(Array.from(h.mounts[1][1],c=>[c.title,c.value,c.value_type]),[['Status','Open','text']]);
  assert.equal(citations.length,2);assert.equal(source.tracker_snapshot.chart.title,'Auto temperature');
  h.scope.renderChatCharts(h.ai,[source,status,temperature]);
  assert.deepEqual(Array.from(h.mounts[2][1],c=>c.title),['Status','Temperature']);
  h.scope.renderChatCharts(h.ai,[source,status,temperature,citation('other-source')]);
  assert.deepEqual(Array.from(h.mounts[3][1],c=>c.title),['Status','Temperature','other-source']);
  h.scope.renderChatCharts(h.ai,[source]);
  assert.deepEqual(Array.from(h.mounts[4][1],c=>c.title),['Auto temperature']);
});

test('actual citation entry point updates charts even when source chips already exist or citations become empty', () => {
  const h=chartFixture();
  vm.runInContext(between('  function renderCitations(', '  window.renderCitations ='),h.scope);
  const row=h.document.createElement('div'); row.className='cite-row'; h.ai.querySelector('.bubble').appendChild(row);
  h.scope.renderCitations(h.ai,[citation('updated')]);
  assert.equal(h.mounts[0][1][0].title,'updated');
  h.scope.renderCitations(h.ai,[]); assert.equal(h.mounts[1][1].length,0);
  assert.equal(h.ai.querySelectorAll('.cite-row').length,1);
});

test('actual streamed citation branch mounts charts and removes them on replacement', () => {
  const h = chartFixture(), marker="} else if (etype === 'citations') {";
  vm.runInContext(between('  function renderCitations(', '  window.renderCitations ='), h.scope);
  const branch = between(marker,"\n      } else if (etype === 'step') {").slice(marker.length);
  Object.assign(h.scope,{turnCitations:[],am:h.ai});
  vm.runInContext('function receive(payload) {'+branch+'\n}',h.scope);
  h.scope.receive({citations:[citation('a')]});
  h.scope.receive({citations:[citation('b')]});
  assert.deepEqual(Array.from(h.mounts[1][1],c=>c.title),['a','b']);
  h.scope.receive({replace:true,citations:[]});
  assert.equal(h.mounts[2][1].length,0); assert.equal(h.scope.turnCitations.length,0);
});

test('actual citation entry point delegates matching numbered sources while retaining chart rendering', () => {
  const h = chartFixture(), calls = [];
  const api = {render(...args) { calls.push(args); return args[1].length; }};
  h.window.EnGenieChatCitations = api; h.scope.EnGenieChatCitations = api;
  vm.runInContext(between('  function renderCitations(', '  window.renderCitations ='), h.scope);
  const sources = [{...citation('numbered'), kb_slug:'operations', kb_name:'Operations', citation_number:1}];
  assert.equal(h.scope.renderCitations(h.ai, sources), 1);
  assert.equal(calls[0][0], h.ai); assert.equal(calls[0][1], sources);
  assert.equal(calls[0][2].language, 'en'); assert.equal(h.mounts[0][1][0].title, 'numbered');
  h.scope.renderCitations(h.ai, []);
  assert.equal(calls[1][1].length, 0); assert.equal(h.mounts[1][1].length, 0);
});

test('actual streamed Markdown is sanitized before compact tables and numbered references are enhanced', () => {
  const h = chartFixture(), order = [], answer = h.ai.querySelector('.answer');
  const citations = [{...citation('numbered'), citation_number:1}];
  h.window.EnGenieChatTableCards = {destroy() { throw new Error('Streaming redraw must retain compact-card size preferences'); },
    mount(node) { assert.equal(node, answer); order.push('tables'); }};
  h.window.marked = {parse(value) { assert.equal(value, 'Supported claim [1]'); order.push('markdown'); return '<p>Supported claim [1]</p>'; }};
  h.window.DOMPurify = {sanitize(value) { order.push('sanitize'); return value; }};
  h.scope.DOMPurify = h.window.DOMPurify;
  h.window.EnGenieChatCitations = {render(message, sources) {
    assert.equal(message, h.ai); assert.equal(sources, citations); order.push('citations');
  }};
  Object.assign(h.scope, {answerEl:answer, am:h.ai, turnCitations:citations,
    mdBuffer:'Supported claim [1]', messages:{scrollHeight:50}});
  const render = vm.runInContext(between('    const renderMd = () => {', '\n    const stream = FosChatStream.create') + '\nrenderMd;', h.scope);
  render();
  assert.deepEqual(order, ['markdown','sanitize','tables','citations']);
  assert.equal(h.scope.messages.scrollTop, 50);
});

test('actual history rendering restores AI citation charts and destroys old chart state before replacing messages', () => {
  const h = chartFixture(), messages=h.document.createElement('div'), calls=[];
  const start='        window.EnGenieChatVisualizations?.destroy(messages);';
  const historyBody = between(start,'\n        if (!messages.children.length) {');
  Object.assign(h.scope,{messages,AI_WHO:'',attachmentUI:{renderMessage(){throw new Error('Unexpected user attachment');}},
    renderCitations(message,citations){calls.push(citations);h.scope.renderChatCharts(message,citations);}});
  vm.runInContext('function history(data) {'+historyBody+'\n}',h.scope);
  h.scope.history({messages:[{role:'ai',content:'Answer',citations:[citation('saved')],completion_status:'complete'}]});
  assert.equal(h.destroyed[0],messages); assert.equal(messages.children.length,1);
  assert.equal(calls.length,1); assert.equal(h.mounts[0][1][0].title,'saved');
});

function sandboxFixture(options = {}) {
  const document=dom(), container=document.createElement('div'), python=[],mounts=[],posts=[],warnings=[],pdf=[];
  const charts=options.charts || [chart()], filename='analysis.xlsx', code='plot_this_data()';
  const api={pythonCaptureSetup:'CAPTURE_SETUP',pythonCaptureRead:'CAPTURE_READ',mount(...args){mounts.push(args);return options.mountCount ?? args[1].length;}};
  const py={runPython(value){python.push(value);if(value===api.pythonCaptureSetup&&options.setupError)throw new Error('capture setup unavailable');
    if(value===api.pythonCaptureRead){if(options.readError)throw new Error('capture read unavailable');return options.malformed?'not json':JSON.stringify(charts);}
    if(value==='sys.stdout.getvalue()')return options.stdout ?? 'Rows: 2';return null;},
    FS:{readdir(){return ['.','..','chart.png',filename];},readFile(name){if(name==='chart.png'||name===filename)return new Uint8Array([1,2,3]);throw new Error('Missing file');}}};
  const window={EnGenieChatVisualizations:options.noBridge?undefined:api,FOSI18n:{language:'en'},jspdf:{}};
  let blobs=0;
  const scope={document,window,EnGenieChatVisualizations:api,Uint8Array,Blob,
    URL:{createObjectURL(){return 'blob:plot-'+(++blobs);}},
    loadPyodideOnce:async()=>py,ensurePackages:async()=>{},getCSRFToken:()=> 'csrf',
    fetch:async(url,config)=>{posts.push({url,...config});return {ok:true};},
    console:{warn(...args){warnings.push(args);}},exportResultToPDF(...args){pdf.push(args);}};
  vm.createContext(scope); vm.runInContext(sandboxSource,scope);
  return {scope,container,python,mounts,posts,warnings,pdf,filename,code};
}

test('actual sandbox captures real plotting code in order, renders charts, and retains PNG, workbook and PDF downloads', async () => {
  const h=sandboxFixture(); await h.scope.runCodeInSandbox(h.code,h.filename,h.container,'answer-1');
  assert.ok(h.python.indexOf('CAPTURE_SETUP')<h.python.indexOf(h.code));
  assert.ok(h.python.indexOf(h.code)<h.python.indexOf('CAPTURE_READ'));
  assert.equal(h.mounts.length,1);assert.equal(h.mounts[0][1][0].title,'trend');
  const card=h.container.children[0],body=card.querySelector('.cr-body');
  assert.match(body.innerHTML,/Rows: 2/); assert.equal(body.querySelector('details').className,'chat-chart-images');
  assert.equal(body.querySelector('img').alt,'chart.png'); assert.match(body.querySelector('img').src,/^blob:/);
  const actions=card.querySelector('.cr-actions'),downloads=actions.querySelectorAll('a').map(a=>a.download);
  assert.deepEqual(downloads,['analysis.xlsx','chart.png']);
  actions.querySelector('button').events.click(); assert.equal(h.pdf.length,1);assert.equal(h.pdf[0][1][0].fn,'chart.png');
  assert.equal(h.posts[0].url,'/kb/analysis/report/');
  assert.deepEqual(JSON.parse(h.posts[0].body),{aid:'answer-1',ok:true,error:'',stdout:'Rows: 2'});
});

test('actual sandbox sends fallback image figures through compact-card controls after preserving the image', async () => {
  const h = sandboxFixture({readError:true, stdout:''}), calls = [];
  h.scope.window.EnGenieChatTableCards = {mountImages(body, options) {
    assert.equal(body.querySelector('img').alt, 'chart.png');
    calls.push([body, options]); return 1;
  }};
  await h.scope.runCodeInSandbox(h.code, h.filename, h.container, 'answer-images');
  assert.equal(calls.length, 1); assert.equal(calls[0][1].language, 'en');
  assert.equal(JSON.parse(h.posts[0].body).ok, true);
});

test('Python metrics and charts join the same answer row as existing photos without moving exports', async () => {
  const h = sandboxFixture(), bubble = h.scope.document.createElement('div'); bubble.className = 'bubble';
  h.container.appendChild(bubble);
  h.scope.window.EnGenieChatVisualizations.rowFor = require('../../static/js/chat_visualizations.js').rowFor;
  const row = h.scope.window.EnGenieChatVisualizations.rowFor(bubble);
  const photos = h.scope.document.createElement('div'); photos.className = 'chat-answer-images'; row.appendChild(photos);
  await h.scope.runCodeInSandbox(h.code, h.filename, h.container, 'mixed-answer');
  assert.equal(bubble.querySelectorAll('.chat-visual-row').length, 1);
  assert.equal(h.mounts[0][0].parentNode, row); assert.equal(photos.parentNode, row);
  assert.equal(h.container.querySelectorAll('.cr-actions').length, 1);
  assert.equal(h.container.querySelector('.code-result').querySelectorAll('a').length, 2);
});

test('unsupported Python images join the shared answer row while keeping their compact-card and download controls', async () => {
  const h = sandboxFixture({readError: true, stdout: ''}), calls = [], bubble = h.scope.document.createElement('div');
  bubble.className = 'bubble'; h.container.appendChild(bubble);
  h.scope.window.EnGenieChatVisualizations.rowFor = require('../../static/js/chat_visualizations.js').rowFor;
  h.scope.window.EnGenieChatTableCards = {mountImages(node) { calls.push(node); }};
  await h.scope.runCodeInSandbox(h.code, h.filename, h.container, 'mixed-fallback');
  const row = bubble.querySelector('.chat-visual-row');
  assert.equal(row.querySelector('img').alt, 'chart.png'); assert.equal(calls[0], row);
  assert.equal(row.querySelector('.chat-chart-images').className, 'chat-chart-images chat-visual-slot');
  assert.equal(h.container.querySelector('.cr-actions').querySelectorAll('a').length, 2);
});

for (const mode of ['readError','malformed']) test('sandbox '+mode+' keeps static chart fallback and successful execution report', async () => {
  const h=sandboxFixture({[mode]:true,stdout:''}); await h.scope.runCodeInSandbox(h.code,h.filename,h.container,'answer-2');
  assert.ok(h.python.includes(h.code)); assert.equal(h.mounts[0][1].length,0);
  const body=h.container.querySelector('.cr-body'); assert.equal(body.querySelector('details'),null);
  assert.equal(body.querySelector('img').alt,'chart.png'); assert.equal(h.warnings.length,1);
  assert.equal(JSON.parse(h.posts[0].body).ok,true);
});

test('optional capture setup failure does not prevent existing Python analysis or static image downloads', async () => {
  const h=sandboxFixture({setupError:true}); await h.scope.runCodeInSandbox(h.code,h.filename,h.container,'answer-3');
  assert.ok(h.python.includes(h.code),'The original analysis still executes when chart capture is unavailable');
  assert.equal(JSON.parse(h.posts[0].body).ok,true);
  assert.equal(h.container.querySelector('img').alt,'chart.png');
});

test('normal chat remains usable without the optional chart bridge', async () => {
  const h=sandboxFixture({noBridge:true,stdout:''}); await h.scope.runCodeInSandbox(h.code,h.filename,h.container,'answer-4');
  assert.ok(h.python.includes(h.code));assert.ok(!h.python.includes('CAPTURE_SETUP'));
  assert.equal(h.mounts.length,0);assert.equal(h.container.querySelector('img').alt,'chart.png');
  assert.equal(JSON.parse(h.posts[0].body).ok,true);
});
