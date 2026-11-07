const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
function setup(crypto,reply){
 const events={},hint={},calls=[];
 const elements={connectorPage:{},connectorMessages:{addEventListener:(name,fn)=>events[name]=fn},composerForm:{querySelector:()=>({value:'csrf'})},connectorPrompt:{},connectorSend:{},connectorHint:hint};
 const scope={document:{getElementById:id=>elements[id]},crypto,AbortController,setTimeout,clearTimeout,fetch:async(url,options)=>{calls.push({url,options});return reply?await reply(calls.length):{ok:true,json:async()=>({ok:true,message:'Accepted'})};}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/js/connector_reports.js'),'utf8'),scope);
 const button={disabled:false,dataset:{reportTest:'/kb/feeds/reports/job/test/'}};
 const click=(target=button)=>events.click({target:{closest:()=>target},preventDefault(){}});
 return {button,hint,calls,click};
}

test('HTTP deployment without crypto.randomUUID sends once with a valid request ID',async()=>{
 const h=setup({getRandomValues:bytes=>{bytes.fill(42);return bytes;}});
 await h.click();assert.equal(h.calls.length,1);
 const key=JSON.parse(h.calls[0].options.body).request_key;assert.ok(key.length>=8&&key.length<=100);
 assert.equal(h.hint.textContent,'Accepted');assert.equal(h.button.disabled,true);
});

test('missing crypto does not leave a dead disabled button',async()=>{
 const h=setup(undefined);await h.click();assert.equal(h.calls.length,1);assert.equal(h.hint.textContent,'Accepted');
});

test('concurrent clicks and re-rendered buttons cannot duplicate a test send',async()=>{
 let finish;const waiting=new Promise(resolve=>finish=resolve);
 const h=setup({randomUUID:()=> 'fixture-uuid'},async()=>{await waiting;return {ok:true,json:async()=>({ok:true})};});
 const first=h.click();await h.click();assert.equal(h.calls.length,1);finish();await first;
 const rebuilt={disabled:false,dataset:{reportTest:h.button.dataset.reportTest}};await h.click(rebuilt);
 assert.equal(h.calls.length,1);assert.equal(rebuilt.disabled,true);
});

test('unknown delivery retries use the same key and restore the button',async()=>{
 const h=setup({randomUUID:()=> 'fixture-uuid'},async()=>({ok:false,json:async()=>({ok:false,status:'unknown',error:'Unconfirmed'})}));
 await h.click();assert.equal(h.button.disabled,false);await h.click();
 assert.equal(h.calls.length,2);assert.equal(h.calls[0].options.body,h.calls[1].options.body);
});

test('a failure before sending can use a fresh key after correction',async()=>{
 let counter=0;const h=setup({randomUUID:()=> 'fixture-uuid-'+ ++counter},async()=>({ok:false,json:async()=>({ok:false,status:'failed',error:'No rows'})}));
 await h.click();await h.click();assert.notEqual(h.calls[0].options.body,h.calls[1].options.body);assert.equal(h.button.disabled,false);
});

test('request ID failure is caught before any HTTP request and restores the button',async()=>{
 const h=setup({randomUUID:()=>{throw Error('ID failed');}});await h.click();
 assert.equal(h.calls.length,0);assert.equal(h.button.disabled,false);assert.equal(h.hint.textContent,'ID failed');
});

test('an expired HTML session response reports a useful error and permits retry',async()=>{
 const h=setup(undefined,async()=>({ok:false,headers:{get:()=> 'text/html'}}));await h.click();
 assert.equal(h.button.disabled,false);assert.match(h.hint.textContent,/刷新页面/);
});
