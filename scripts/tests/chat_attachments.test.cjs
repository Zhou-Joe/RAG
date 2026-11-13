const {test} = require('node:test');
const assert = require('node:assert/strict');
const {File} = require('node:buffer');
const attachments = require('../../static/js/chat_attachments.js');

const MIB = 1024 * 1024;
function file(name, size = 32, options = {}) {
  return {name, size, type: '', lastModified: 1, ...options};
}
function fixture() {
  const created = [], revoked = [], errors = [], changes = [];
  const urlAPI = {
    createObjectURL(value) { created.push(value); return 'blob:unit-' + created.length; },
    revokeObjectURL(value) { revoked.push(value); },
  };
  const queue = attachments.createQueue({urlAPI, onError: error => errors.push(error),
    onChange: (entries, uploading) => changes.push({entries, uploading})});
  return {queue, created, revoked, errors, changes, urlAPI};
}

test('accepted extensions are case-insensitive and image previews preserve original File objects', () => {
  for (const ext of ['jpg','jpeg','png','webp','pdf','docx','txt','md','csv','tsv','xlsx','xlsm']) {
    const f = fixture(), original = file('Report.' + ext.toUpperCase());
    assert.equal(f.queue.add([original]), true, ext);
    const entry = f.queue.items()[0];
    assert.equal(entry.file, original);
    assert.equal(entry.kind, ['jpg','jpeg','png','webp'].includes(ext) ? 'image' : 'document');
    assert.equal(f.created.length, entry.kind === 'image' ? 1 : 0);
  }
  const f = fixture();
  assert.equal(f.queue.add([file('vector.svg', 32, {type:'image/svg+xml'}), file('photo.png.exe', 32, {type:'image/png'})]), false);
  assert.equal(f.queue.items().length, 0);
  assert.equal(f.errors.length, 2);
});

test('per-file size includes exactly 10 MiB and rejects oversize, negative, and non-finite sizes', () => {
  const f = fixture(), boundary = file('boundary.pdf', 10 * MIB);
  f.queue.add([boundary, file('large.pdf', 10 * MIB + 1), file('negative.txt', -1), file('nan.txt', NaN)]);
  assert.deepEqual(f.queue.items().map(entry => entry.file), [boundary]);
  assert.equal(f.errors.length, 3);
});

test('selection enforces five files and a 25 MiB aggregate limit without discarding valid files', () => {
  const f = fixture();
  const originals = [file('first.pdf',10*MIB),file('second.pdf',10*MIB),file('third.pdf',5*MIB)];
  assert.equal(f.queue.add(originals), true);
  assert.equal(f.queue.add([file('extra.txt',1)]), false);
  assert.deepEqual(f.queue.items().map(entry => entry.file), originals);
  const g = fixture();
  const five = Array.from({length:5}, (_, index) => file('file-' + index + '.txt'));
  g.queue.add(five);
  assert.equal(g.queue.add([file('sixth.txt')]), false);
  assert.deepEqual(g.queue.items().map(entry => entry.file), five);
});

test('picker duplicates are deduplicated while distinct versions of a filename remain selectable', () => {
  const f = fixture(), first = file('same.png');
  f.queue.add([first, {...first}, file('same.png',32,{lastModified:2}), file('same.png',64)]);
  assert.equal(f.queue.items().length, 3);
  assert.equal(f.created.length, 3);
  assert.equal(f.queue.items()[0].file, first);
});

test('an active upload locks addition/removal and failed upload retains exact files and previews for retry', () => {
  const f = fixture(), original = file('photo.png');
  f.queue.add([original]);
  const before = f.queue.items()[0], batch = f.queue.beginUpload();
  assert.equal(f.queue.isUploading(), true);
  assert.equal(f.queue.beginUpload(), null);
  assert.equal(f.queue.add([file('other.txt')]), false);
  assert.equal(f.queue.remove(before.id), false);
  f.queue.finishUpload(batch);
  assert.equal(f.queue.isUploading(), false);
  assert.equal(f.queue.items()[0].file, original);
  assert.equal(f.queue.items()[0].previewURL, before.previewURL);
  assert.deepEqual(f.revoked, []);
  const retry = f.queue.beginUpload();
  assert.equal(retry.entries[0].file, original);
});

test('server acceptance removes exactly the active batch and releases its previews once', () => {
  const f = fixture();
  f.queue.add([file('photo.png'), file('document.pdf')]);
  const batch = f.queue.beginUpload(), preview = batch.entries[0].previewURL;
  assert.equal(f.queue.acceptUpload({...batch}), false);
  assert.equal(f.queue.acceptUpload(batch), true);
  assert.equal(batch.accepted, true);
  assert.equal(f.queue.hasFiles(), false);
  assert.equal(f.queue.isUploading(), false);
  assert.deepEqual(f.revoked, [preview]);
  f.queue.add([file('next.png')]);
  f.queue.finishUpload(batch);
  assert.equal(f.queue.acceptUpload(batch), false);
  assert.equal(f.queue.items()[0].name, 'next.png');
  assert.deepEqual(f.revoked, [preview]);
});

test('late callbacks after a conversation clear cannot unlock or remove the new conversation selection', () => {
  const f = fixture();
  f.queue.add([file('old.png')]);
  const oldBatch = f.queue.beginUpload();
  f.queue.clear();
  const nextFile = file('new.png'); f.queue.add([nextFile]);
  const nextBatch = f.queue.beginUpload();
  f.queue.finishUpload(oldBatch);
  assert.equal(f.queue.acceptUpload(oldBatch), false);
  assert.equal(f.queue.isUploading(), true);
  assert.equal(f.queue.items()[0].file, nextFile);
  assert.equal(f.queue.acceptUpload(nextBatch), true);
  assert.deepEqual(f.revoked, ['blob:unit-1','blob:unit-2']);
});

test('removal and conversation clear revoke only local image object URLs', () => {
  const f = fixture();
  f.queue.add([file('photo.png'),file('document.pdf'),file('second.webp')]);
  const first = f.queue.items()[0];
  assert.equal(f.queue.remove(first.id), true);
  assert.equal(f.queue.remove(first.id), false);
  f.queue.clear(); f.queue.clear();
  assert.deepEqual(f.revoked, ['blob:unit-1','blob:unit-2']);
});

test('multipart body carries lossless message/thread and each original file reference, with attachment-only fallback', async () => {
  class CaptureFormData {
    constructor() { this.fields = []; }
    append(...args) { this.fields.push(args); }
  }
  const files = [new File(['hello'], 'report.csv', {type:'text/csv'}), new File(['image'], 'photo.png', {type:'image/png'})];
  const batch = {entries:files.map(value => ({file:value, name:value.name}))};
  const message = '\u8bf7\u5206\u6790 A&B + 2026\nsecond line', thread = 't_unicode-42';
  const body = attachments.buildBody(message, thread, batch, CaptureFormData);
  assert.deepEqual(body.fields.slice(0,2), [['message',message],['thread_id',thread]]);
  assert.equal(body.fields[2][0], 'files'); assert.equal(body.fields[2][1], files[0]);
  assert.equal(body.fields[3][0], 'files'); assert.equal(body.fields[3][1], files[1]);
  const real = attachments.buildBody(message, thread, batch, FormData);
  assert.equal(real.get('message'), message); assert.equal(real.get('thread_id'), thread);
  assert.deepEqual(real.getAll('files').map(value => value.name), ['report.csv','photo.png']);
  assert.equal(await real.getAll('files')[0].text(), 'hello');
  assert.equal(attachments.buildBody('', thread, batch, CaptureFormData).fields[0][1], 'Please review the attached files.');
});

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null;
    this.attributes = {}; this.handlers = {}; this.className = ''; this._text = '';
    this.hidden = false; this.disabled = false; this.clicks = 0;
    this.classList = {
      add: name => { if (!this.classList.contains(name)) this.className = (this.className + ' ' + name).trim(); },
      remove: name => { this.className = this.className.split(/\s+/).filter(value => value !== name).join(' '); },
      contains: name => this.className.split(/\s+/).includes(name),
    };
  }
  set innerHTML(_) { throw new Error('Untrusted content must not use innerHTML'); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  replaceChildren(...children) { for (const child of this.children) child.parentNode = null; this.children = []; this._text = ''; children.forEach(child => this.appendChild(child)); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this); this.parentNode = null; }
  querySelector(selector) {
    for (const child of this.children) {
      if (selector.startsWith('.') ? child.classList.contains(selector.slice(1)) : child.tagName.toLowerCase() === selector) return child;
      const found = child.querySelector(selector); if (found) return found;
    }
    return null;
  }
  addEventListener(name, handler) { (this.handlers[name] ||= []).push(handler); }
  fire(name, details = {}) {
    const event = {defaultPrevented:false, preventDefault(){this.defaultPrevented=true;}, ...details};
    for (const handler of this.handlers[name] || []) handler(event);
    return event;
  }
  click() { this.clicks++; if (!this.disabled) this.fire('click'); }
}
const document = {createElement: tag => new Element(tag)};
const UUID_PATH = '/kb/chat/attachments/12345678-1234-1234-1234-123456789abc/';

test('message renderer treats malicious filenames as literal text and allows only protected attachment URLs', () => {
  const container = new Element(), malicious = '<img src=x onerror=alert(1)>.png';
  attachments.renderMessage(container, [
    {name:malicious,kind:'image',size:32,url:UUID_PATH},
    {name:'external.png',kind:'image',url:'https://evil.example/x.png'},
    {name:'script.pdf',kind:'document',url:'javascript:alert(1)'},
    {name:'data.png',kind:'image',url:'data:image/png;base64,abc'},
  ], {document});
  const list = container.querySelector('.chat-message-attachments');
  assert.equal(list.children[0].tagName, 'A');
  assert.equal(list.children[0].href, UUID_PATH);
  assert.equal(list.children[0].querySelector('img').alt, malicious);
  assert.equal(list.children[0].querySelector('.chat-attachment-name').textContent, malicious);
  assert.equal(list.children[0].querySelector('.chat-attachment-name').getAttribute('translate'), 'no');
  for (const child of list.children.slice(1)) { assert.equal(child.tagName, 'SPAN'); assert.equal(child.querySelector('img'), null); }
  attachments.renderMessage(container, [{name:'replacement.txt',kind:'document',url:UUID_PATH+'?download=1'}], {document});
  assert.equal(container.children.length, 1);
  assert.equal(container.children[0].children.length, 1);
  assert.equal(attachments.protectedURL(UUID_PATH+'?download=1'), UUID_PATH+'?download=1');
  for (const bad of ['//evil.example'+UUID_PATH, UUID_PATH+'#fragment', '/kb/chat/attachments/not-a-uuid/', 'https://example.test'+UUID_PATH]) assert.equal(attachments.protectedURL(bad), '');
});

function uiFixture() {
  const f = fixture(), input = new Element('textarea'), picker = new Element('input'), button = new Element('button');
  const pending = new Element(), error = new Element(), dropZone = new Element();
  const ui = attachments.create({input,picker,button,pending,error,dropZone,document,urlAPI:f.urlAPI,translate:value=>'translated:'+value});
  return {...f,ui,input,picker,button,pending,error,dropZone};
}

test('picker and paste select actual files; normal text paste remains native; pending remove releases image preview', () => {
  const f = uiFixture(), photo = file('paste.png'), doc = file('report.pdf');
  f.button.click(); assert.equal(f.picker.clicks, 1);
  f.picker.files = [doc]; f.picker.value = 'selected'; f.picker.fire('change');
  assert.equal(f.picker.value, ''); assert.equal(f.ui.items()[0].file, doc);
  const plain = f.input.fire('paste', {clipboardData:{files:[]}});
  assert.equal(plain.defaultPrevented, false);
  const pasted = f.input.fire('paste', {clipboardData:{files:[photo]}});
  assert.equal(pasted.defaultPrevented, true); assert.equal(f.ui.items()[1].file, photo);
  assert.equal(f.pending.hidden, false); assert.equal(f.pending.children.length, 2);
  f.pending.children[1].querySelector('.chat-attachment-remove').click();
  assert.deepEqual(f.ui.items().map(entry => entry.file), [doc]);
  assert.deepEqual(f.revoked, ['blob:unit-1']);
});

test('drop only intercepts files, clears nested drag state, and keeps upload-locked controls immutable', () => {
  const f = uiFixture(), image = file('drop.webp'), transfer = {types:['Files'],files:[image]};
  assert.equal(f.dropZone.fire('dragover',{dataTransfer:{types:['text/plain']}}).defaultPrevented,false);
  f.dropZone.fire('dragenter',{dataTransfer:transfer});f.dropZone.fire('dragenter',{dataTransfer:transfer});
  f.dropZone.fire('dragleave'); assert.equal(f.dropZone.classList.contains('is-file-drop'),true);
  assert.equal(f.dropZone.fire('drop',{dataTransfer:transfer}).defaultPrevented,true);
  assert.equal(f.dropZone.classList.contains('is-file-drop'),false); assert.equal(f.ui.items()[0].file,image);
  const batch = f.ui.beginUpload();
  assert.equal(f.button.disabled,true); assert.equal(f.picker.disabled,true);
  assert.equal(f.pending.querySelector('.chat-attachment-remove').disabled,true);
  f.input.fire('paste',{clipboardData:{files:[file('ignored.txt')]}});
  f.dropZone.fire('drop',{dataTransfer:{types:['Files'],files:[file('also-ignored.txt')]}});
  f.pending.querySelector('.chat-attachment-remove').fire('click');
  assert.deepEqual(f.ui.items().map(entry=>entry.file),[image]);
  f.ui.finishUpload(batch); assert.equal(f.button.disabled,false);
  f.ui.clear(); assert.equal(f.pending.hidden,true);assert.equal(f.pending.children.length,0);
});

function submitFixture(run) {
  const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
  const source = fs.readFileSync(path.join(__dirname, '../../templates/kb/ask.html'), 'utf8');
  const start = source.indexOf('  async function submit() {');
  const submit = source.slice(start, source.indexOf('  // 向侧边栏插入', start));
  class ChatElement extends Element {
    constructor(tag) { super(tag); this.style = {}; this.dataset = {}; }
    set innerHTML(value) {
      assert.ok(value.includes('tool-area') && value.includes('answer thinking'));
      const bubble = new ChatElement('div'); bubble.className = 'bubble';
      const tool = new ChatElement('div'); tool.className = 'tool-area';
      const answer = new ChatElement('div'); answer.className = 'answer thinking';
      bubble.appendChild(tool); bubble.appendChild(answer); this.replaceChildren(bubble);
    }
  }
  const f = fixture(), messages = new ChatElement('div'), requests = [], errors = [], recoveries = [];
  const input = {value:'Review this photo', dispatchEvent(){}}, stopBtn = {style:{}};
  const dom = {createElement:tag=>new ChatElement(tag), querySelector:()=>({})};
  const attachmentUI = {...f.queue, showError:message=>errors.push(message),
    renderMessage:(container,items,options)=>attachments.renderMessage(container,items,{...options,document:dom})};
  const context = {input, sendBtn:{}, stopBtn, messages, attachmentUI, FosChatAttachments:attachments,
    busy:false, currentThreadId:'t_attachment-test', recovery:null, historyRevision:0, activeTurn:null,
    recoveryKey:'unit', genThreadId:()=> 'generated', getCSRFToken:()=> 'csrf', AI_WHO:'', document:dom,
    FosAnswerRecovery:{remember(){}}, window:{}, URLSearchParams, Event, sessionTokens:{in:0,out:0},sessionMax:{in:0,out:0},
    FosToolActivity:{create:()=>({update(){},finish(){}})}, addConvItem(){throw new Error('Unexpected phantom conversation');},
    restoreConversation:thread=>recoveries.push(thread), renderCitations(){},
    FosChatStream:{create(callbacks){
      return {signal:{aborted:false}, async run(url,options){requests.push({url,...options});await run(callbacks,context,requests.length);}};
    }}};
  vm.createContext(context); vm.runInContext(submit,context);
  return {...f,context,input,messages,requests,errors,recoveries,submit:()=>context.submit()};
}

test('composer acknowledges saved files before releasing previews and never reattaches the prior turn', async () => {
  const first = new File(['first'], 'first.png', {type:'image/png'}), next = new File(['next'], 'next.csv', {type:'text/csv'});
  const f = submitFixture(async (callbacks,context,turn) => {
    const saved = {message_id:turn,attachments:[{id:String(turn),name:turn===1?first.name:next.name,
      kind:turn===1?'image':'document',size:4,url:UUID_PATH}]};
    callbacks.onEvent('attachments',saved);
    assert.equal(context.attachmentUI.isUploading(),false);
    if(turn===1)context.attachmentUI.add([next]);
    callbacks.onEvent('attachments',saved); // A duplicate event must not reset the accepted flag.
    callbacks.onEvent('token',{text:'Reviewed'});
  });
  f.queue.add([first]); await f.submit();
  assert.deepEqual(f.requests[0].body.getAll('files').map(value=>value.name),['first.png']);
  assert.equal(f.queue.items()[0].file,next);
  assert.equal(f.messages.children[0].querySelector('img').src,UUID_PATH);
  assert.deepEqual(f.revoked,['blob:unit-1']);
  await f.submit();
  assert.deepEqual(f.requests[1].body.getAll('files').map(value=>value.name),['next.csv']);
  assert.equal(f.requests[1].body.get('message'),'Please review the attached files.');
  assert.equal(f.queue.hasFiles(),false); assert.equal(f.context.busy,false);
});

test('composer retains files and text after pre-acceptance HTTP failure without creating ghost history', async () => {
  const f = submitFixture(async () => {throw new Error('Vision is disabled');});
  const original = new File(['photo'],'photo.png',{type:'image/png'}); f.queue.add([original]);
  await f.submit();
  assert.equal(f.queue.items()[0].file,original); assert.equal(f.queue.isUploading(),false);
  assert.equal(f.input.value,'Review this photo'); assert.equal(f.messages.children.length,0);
  assert.deepEqual(f.revoked,[]); assert.deepEqual(f.recoveries,[]);
  assert.equal(f.errors.at(-1),'Vision is disabled'); assert.equal(f.context.busy,false);
});

test('socket completion without attachment acknowledgement keeps the selection for retry', async () => {
  const f = submitFixture(async () => {}), original = new File(['data'],'data.csv',{type:'text/csv'});
  f.queue.add([original]); await f.submit();
  assert.equal(f.queue.items()[0].file,original); assert.equal(f.queue.isUploading(),false);
  assert.equal(f.messages.children.length,0); assert.deepEqual(f.recoveries,[]);
  assert.match(f.errors.at(-1),/保留/);
});

test('upload failure preserves a new draft typed while waiting and restores the original prompt only once', async () => {
  const f = submitFixture(async (callbacks,context) => {
    context.input.value = 'A new draft typed during upload';
    const error = new Error('Upload timed out'); callbacks.onAbort(error); throw error;
  });
  f.queue.add([new File(['photo'],'photo.png',{type:'image/png'})]); await f.submit();
  assert.equal(f.input.value,'Review this photo\n\nA new draft typed during upload');
  assert.equal(f.queue.items().length,1); assert.equal(f.queue.isUploading(),false);
});

test('attachment labels and upload errors translate both ways while filenames remain unchanged', () => {
  const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
  const catalog = JSON.parse(fs.readFileSync(path.join(__dirname,'../../static/i18n/chat_attachments.json'),'utf8'));
  const scope = {FOS_I18N_CATALOG:catalog,module:{exports:{}}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/js/interface_language.js'),'utf8'),scope);
  const translate = scope.module.exports.translate;
  for(const source of ['添加照片或文件','待发送附件','移除附件','图片附件需要启用支持图片的多模态模型。','此会话附件已超过 20 个，请新建对话后继续上传。']){
    assert.equal(translate(source,'en'),catalog.messages[source]);
    assert.equal(translate(catalog.messages[source],'zh'),source);
  }
  const filename = '业务数据 <draft>&.csv', source = '附件「'+filename+'」为空。';
  assert.equal(translate(source,'en'),'Attachment “'+filename+'” is empty.');
  assert.equal(translate(translate(source,'en'),'zh'),source);
});
