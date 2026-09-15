const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const scope = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../static/js/chat_input.js'), 'utf8'), scope);

function fixture(allowSend = () => true) {
  let time = 0, sent = 0;
  const handlers = {};
  const composing = scope.FosChatInput.bind({addEventListener: (name, callback) => handlers[name] = callback}, () => sent++, () => time, allowSend);
  return {handlers, composing, sent: () => sent, advance: () => time += 100,
    key(options = {}) { let prevented = false; handlers.keydown({key: 'Enter', preventDefault: () => prevented = true, ...options}); return prevented; }};
}
test('Chinese candidate confirmation never sends an unfinished question', () => {
  const f = fixture(); f.handlers.compositionstart();
  assert.equal(f.composing(), true); assert.equal(f.key(), false); assert.equal(f.sent(), 0);
  f.handlers.compositionend(); f.advance(); assert.equal(f.key(), true); assert.equal(f.sent(), 1);
});
test('WebKit compositionend-before-keydown is suppressed', () => {
  const f = fixture(); f.handlers.compositionstart(); f.handlers.compositionend();
  assert.equal(f.key(), false); assert.equal(f.sent(), 0);
  f.advance(); f.key(); assert.equal(f.sent(), 1);
});
test('IME 229 and isComposing flags work even without compositionstart', () => {
  const f = fixture(); f.key({keyCode:229}); f.key({isComposing:true}); assert.equal(f.sent(), 0);
});
test('Shift+Enter remains newline and normal Enter sends once', () => {
  const f = fixture(); assert.equal(f.key({shiftKey:true}), false); assert.equal(f.sent(), 0);
  assert.equal(f.key(), true); assert.equal(f.sent(), 1);
});
test('Chinese question form encoding is lossless', () => {
  const text = '请问：八号泵的扭矩是多少？\n图纸 AB-12，单位 N·m。';
  assert.equal(new URLSearchParams(new URLSearchParams({message:text}).toString()).get('message'), text);
});

test('Touch device Enter stays newline', () => {
  const f = fixture(() => false);
  assert.equal(f.key(), false); assert.equal(f.sent(), 0);
});
