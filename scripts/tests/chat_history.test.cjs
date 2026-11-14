const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../../templates/kb/ask.html'), 'utf8');
const start = template.indexOf('  function chatHistoryText(');
const end = template.indexOf('  async function deleteConversation(', start);
assert.ok(start !== -1 && end > start);
const source = template.slice(start, end).replace(/\{% url "kb:conv_clear" %\}/g, '/kb/conv/clear/');
const catalog = JSON.parse(fs.readFileSync(path.join(__dirname, '../../static/i18n/kb.json'), 'utf8')).messages;

function setup({confirm = true, response, failure, language = 'en'} = {}) {
  const button = {disabled:false, textContent:'Clear all chat history', addEventListener(type, listener) { this[type] = listener; }};
  const original = {dataset:{thread:'saved-chat'}};
  const list = {children:[original], replaceChildren(...children) { this.children = children; }};
  const calls = {fetch:[], confirm:[], errors:[], cancelled:0, fresh:0, restored:[], focused:0, closed:0, urls:[]};
  const scope = {
    clearingChatHistory:false, busy:false, currentThreadId:'saved-chat', sendBtn:{disabled:false}, URL,
    document:{
      getElementById(id) { return id === 'clearChatHistoryBtn' ? button : list; },
      createElement() { return {}; }, querySelectorAll() { return list.children.filter(item => item.dataset); },
    },
    window:{
      FOSI18n:{t(text) { return language === 'en' ? catalog[text] || text : text; }},
      location:{href:'https://engenie.test/kb/ask/?embed=1&conv=saved-chat'},
      history:{replaceState(_state, _title, url) { calls.urls.push(String(url)); }},
    },
    async fosConfirm(options) { calls.confirm.push(options); return confirm; },
    async fetch(url, options) {
      calls.fetch.push({url, options});
      if (failure) throw failure;
      return response ? await response : {ok:true, status:200, async json() { return {ok:true, deleted:2}; }};
    },
    getCSRFToken() { return 'csrf-token'; },
    cancelCurrentTurn() { calls.cancelled++; },
    newConversation() { assert.equal(scope.clearingChatHistory, false); calls.fresh++; scope.currentThreadId='new-chat'; scope.busy=false; scope.sendBtn.disabled=false; },
    restoreConversation(thread) { calls.restored.push(thread); scope.busy=false; scope.sendBtn.disabled=false; },
    closeDrawer() { calls.closed++; }, input:{focus() { calls.focused++; }},
    alert(message) { calls.errors.push(message); },
  };
  scope.window.location.origin = 'https://engenie.test';
  scope.window.parent = scope.window;
  vm.createContext(scope); vm.runInContext(source, scope);
  return {scope, button, list, original, calls};
}

test('clear history confirms, posts with CSRF, removes saved rows and opens a fresh chat', async () => {
  const h = setup();
  await h.scope.clearChatHistory();
  assert.equal(h.calls.confirm[0].title, 'Clear all chat history');
  assert.equal(h.calls.confirm[0].danger, true);
  assert.match(h.calls.confirm[0].message, /cannot be undone/);
  assert.equal(h.calls.fetch.length, 1);
  assert.equal(h.calls.fetch[0].url, '/kb/conv/clear/');
  assert.equal(h.calls.fetch[0].options.method, 'POST');
  assert.equal(h.calls.fetch[0].options.headers['X-CSRFToken'], 'csrf-token');
  assert.equal(h.calls.cancelled, 1); assert.equal(h.calls.fresh, 1);
  assert.equal(h.list.children.length, 1); assert.equal(h.list.children[0].className, 'conv-empty');
  assert.equal(h.list.children[0].textContent, 'No chats yet');
  assert.equal(h.calls.urls[0], 'https://engenie.test/kb/ask/?embed=1');
  assert.equal(h.calls.focused, 1); assert.equal(h.calls.closed, 1);
  assert.equal(h.button.disabled, false); assert.equal(h.scope.busy, false);
  assert.deepEqual(h.calls.errors, []);
});

test('cancelling the confirmation preserves chat history and the current answer', async () => {
  const h = setup({confirm:false});
  await h.scope.clearChatHistory();
  assert.equal(h.calls.fetch.length, 0); assert.equal(h.calls.cancelled, 0); assert.equal(h.calls.fresh, 0);
  assert.equal(h.list.children[0], h.original); assert.equal(h.button.disabled, false);
});

test('pending deletion prevents duplicate confirmations and requests', async () => {
  let finish;
  const response = new Promise(resolve => { finish = resolve; });
  const h = setup({response});
  const first = h.scope.clearChatHistory();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(h.scope.clearingChatHistory, true); assert.equal(h.scope.sendBtn.disabled, true);
  assert.equal(h.button.disabled, true); assert.equal(h.button.textContent, 'Clearing…');
  await h.scope.clearChatHistory();
  assert.equal(h.calls.confirm.length, 1); assert.equal(h.calls.fetch.length, 1);
  finish({ok:true, status:200, async json() { return {ok:true}; }});
  await first;
  assert.equal(h.scope.clearingChatHistory, false); assert.equal(h.button.disabled, false);
});

test('server and network failures preserve visible history and resume the saved conversation', async () => {
  for (const options of [
    {response:{ok:false, status:500, async json() { return {message:'Try again'}; }}},
    {failure:new Error('Disconnected')},
    {response:{ok:true, status:200, async json() { throw new Error('Not JSON'); }}},
  ]) {
    const h = setup(options);
    await h.scope.clearChatHistory();
    assert.equal(h.list.children[0], h.original); assert.equal(h.calls.fresh, 0);
    assert.deepEqual(h.calls.restored, ['saved-chat']); assert.equal(h.calls.errors.length, 1);
    assert.match(h.calls.errors[0], /^Could not clear chat history:/);
    assert.equal(h.scope.clearingChatHistory, false); assert.equal(h.button.disabled, false);
  }
});

test('clear history uses Chinese controls when the interface is Chinese', async () => {
  const h = setup({language:'zh-hans'});
  await h.scope.clearChatHistory();
  assert.equal(h.calls.confirm[0].title, '清空所有聊天记录');
  assert.equal(h.calls.confirm[0].confirmText, '全部清空');
  assert.equal(h.button.textContent, '清空所有聊天记录');
  assert.equal(h.list.children[0].textContent, '还没有会话');
});

test('embedded chat tells its parent to clear stale saved-conversation navigation and badges', async () => {
  const h = setup(), sent = [];
  h.scope.window.parent = {postMessage(data, origin) { sent.push({data, origin}); }};
  await h.scope.clearChatHistory();
  assert.equal(sent.length, 1); assert.equal(sent[0].data.type, 'rag-chat:history-cleared');
  assert.equal(sent[0].origin, 'https://engenie.test');
});
