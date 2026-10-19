const {test} = require('node:test');
const assert = require('node:assert/strict');
require('../../static/js/chat_recovery.js');
const tick = ms => new Promise(resolve => setTimeout(resolve, ms));
test('restore polls active task until complete without posting a new question', async () => {
  let calls = 0; const updates = [];
  const watcher = FosAnswerRecovery.watch('my thread', {interval:1,
    fetchImpl: async (url, options) => {
      assert.equal(url, '/kb/conv/my%20thread/messages/');
      assert.equal(options.method, undefined);
      return {ok:true, json:async()=>({active_answer: ++calls < 2 ? {id:1} : null})};
    }, onUpdate: data => updates.push(data)});
  await tick(30); watcher.stop();
  assert.equal(calls,2); assert.equal(updates.length,2);
});
test('leaving only cancels snapshot reads and ignores late responses', async () => {
  let resolve, calls=0, signal;
  const watcher=FosAnswerRecovery.watch('a',{fetchImpl:(url,options)=>{
    calls++; signal=options.signal;
    return new Promise(r=>resolve=r);
  },onUpdate:()=>assert.fail('stale page update')});
  watcher.stop();
  resolve({ok:true,json:async()=>({active_answer:{id:1}})});
  await tick(10);
  assert.equal(calls,1);assert.equal(signal.aborted,true);
});
test('transient read error retries and then finishes', async () => {
  let calls=0, errors=0;
  const watcher=FosAnswerRecovery.watch('a',{interval:1,fetchImpl:async()=>{
    if (++calls===1) throw new Error('offline');
    return {ok:true,json:async()=>({active_answer:null})};
  },onError:()=>errors++,onUpdate:()=>{}});
  await tick(30);watcher.stop();
  assert.equal(calls,2);assert.equal(errors,1);
});
test('deleted or inaccessible conversation stops polling', async () => {
  let calls=0, status;
  const watcher=FosAnswerRecovery.watch('a',{interval:1,fetchImpl:async()=>{
    calls++;return {ok:false,status:404};
  },onError:error=>status=error.status,onUpdate:()=>assert.fail()});
  await tick(20);watcher.stop();assert.equal(calls,1);assert.equal(status,404);
});
