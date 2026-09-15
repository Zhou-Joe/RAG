const {test} = require('node:test');
const assert = require('node:assert/strict');
require('../../static/js/chat_stream.js');
const encode = text => new TextEncoder().encode(text);
function response(parts, keepOpen = false) {
  let cancelled = false;
  const body = new ReadableStream({start(controller) {
    parts.forEach(p => controller.enqueue(encode(p)));
    if (!keepOpen) controller.close();
  }, cancel() { cancelled = true; }});
  return {response: new Response(body, {headers: {'content-type': 'text/event-stream'}}), cancelled: () => cancelled};
}
test('done ends a still-open connection and cancels its reader', async () => {
  const f = response(['event: token\ndata: {"text":"完成"}\n\nevent: done\ndata: {}\n\n'], true);
  const events = [];
  await FosChatStream.create({fetchImpl: async () => f.response, onEvent: (...args) => events.push(args)}).run('/');
  assert.equal(f.cancelled(), true);
  assert.deepEqual(events, [['token', {text: '完成'}]]);
});
test('EOF without done is an incomplete answer', async () => {
  const f = response(['event: token\ndata: {"text":"半截表格"}\n\n']);
  await assert.rejects(FosChatStream.create({fetchImpl: async () => f.response}).run('/'), /完成前中断/);
});
test('idle connection has a bounded wait and aborts fetch', async () => {
  let signal;
  const stream = FosChatStream.create({idleMs: 15, fetchImpl: async (_url, opts) => {
    signal = opts.signal;
    return new Promise(() => {});
  }});
  await assert.rejects(stream.run('/'), /没有响应/);
  assert.equal(signal.aborted, true);
});
test('stop cancels a pending read and releases the caller', async () => {
  const f = response([], true);
  const stream = FosChatStream.create({fetchImpl: async () => f.response});
  const running = stream.run('/');
  setTimeout(() => stream.abort(), 5);
  await assert.rejects(running, /已停止/);
  assert.equal(f.cancelled(), true);
});
test('heartbeats keep idle timer alive but cannot defeat total timeout', async () => {
  let interval;
  const body = new ReadableStream({start(c) {
    interval = setInterval(() => c.enqueue(encode(': heartbeat\n\n')), 5);
  }, cancel() { clearInterval(interval); }});
  const r = new Response(body, {headers: {'content-type': 'text/event-stream'}});
  await assert.rejects(FosChatStream.create({idleMs: 25, totalMs: 45, fetchImpl: async () => r}).run('/'), /等待时间过长/);
});
test('heartbeat progress reaches the UI without replacing answer tokens', async () => {
  const f = response([
    'event: token\ndata: {"text":"已显示内容"}\n\n',
    'event: heartbeat\ndata: {"elapsed":20,"stage":"正在检索"}\n\n',
    'event: token\ndata: {"text":"后续内容"}\n\nevent: done\ndata: {}\n\n',
  ], true);
  const events = [];
  await FosChatStream.create({fetchImpl: async () => f.response, onEvent: (...args) => events.push(args)}).run('/');
  assert.deepEqual(events, [
    ['token', {text: '已显示内容'}],
    ['heartbeat', {elapsed: 20, stage: '正在检索'}],
    ['token', {text: '后续内容'}],
  ]);
});
test('server error terminates an open stream immediately', async () => {
  const f = response(['event: error\ndata: {"message":"回答达到长度上限"}\n\n'], true);
  await assert.rejects(FosChatStream.create({fetchImpl: async () => f.response}).run('/'), /长度上限/);
  assert.equal(f.cancelled(), true);
});
test('split CRLF frames, multiline data and comments parse correctly', async () => {
  const f = response([': ping\r\n\r\nevent: token\r\ndata: {"text":\r\n', 'data: "片段"}\r\n\r', '\nevent: done\r\ndata: {}\r\n\r\n'], true);
  const events = [];
  await FosChatStream.create({fetchImpl: async () => f.response, onEvent: (...args) => events.push(args)}).run('/');
  assert.deepEqual(events, [['token', {text: '片段'}]]);
});
test('HTML login response is not accepted as an empty successful answer', async () => {
  await assert.rejects(FosChatStream.create({fetchImpl: async () => new Response('login', {headers: {'content-type': 'text/html'}})}).run('/'), /登录状态/);
});
