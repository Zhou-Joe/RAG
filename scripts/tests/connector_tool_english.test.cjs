const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function setup(){
  let language='zh-hans';const translations=[];
  const make=(tag,cls='',text='')=>({tag,className:cls,textContent:text,children:[],dataset:{},attrs:{},append(...nodes){this.children.push(...nodes)},setAttribute(k,v){this.attrs[k]=v}});
  const scope={el:make,toolDisclosures:new Map(),detailStates:new Map(),completedTools:new Set(),FOSI18n:{t(value,selected=language){translations.push(selected);return selected==='en'?(value==='正在读取已保存数据。'?'Reading saved data.':value):'中文界面';}}};
  scope.window=scope;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/js/tool_disclosure.js'),'utf8'),scope);
  const source=fs.readFileSync(path.join(__dirname,'../../static/js/connector.js'),'utf8');
  vm.runInNewContext(source.slice(source.indexOf('  function toolTrail(job)'),source.indexOf('  function previewCard(job)'))+'\nglobalThis.renderTrail=toolTrail;',scope);
  return {render:scope.renderTrail,language:value=>language=value,translations,helper:scope.FosToolText};
}
function all(node){return [node,...node.children.flatMap(all)];}
test('new trigger tool and historical completion notes stay English under both UI languages',()=>{
  const h=setup(),job={id:'fixture',status:'ready',events:[{kind:'tool_start',turn:1,call_id:'a',tool:'preview_triggered_report'},{kind:'tool_end',turn:1,call_id:'a',tool:'preview_triggered_report',ok:true,seconds:1.5,message:'正在读取已保存数据。'}]};
  const original=JSON.stringify(job);
  for(const language of ['zh-hans','en','zh-hans']){
    h.language(language);const work=h.render(job),nodes=all(work);
    assert.equal(work.attrs.translate,'no');
    assert.ok(nodes.some(n=>n.textContent==='Preview conditional delivery'));
    assert.ok(nodes.some(n=>n.textContent==='Reading saved data.'));
    assert.ok(nodes.some(n=>n.textContent==='Tool activity · 1 call'));
    assert.ok(nodes.every(n=>!/[\u3400-\u9fff]/.test(String(n.textContent))));
  }
  assert.equal(JSON.stringify(job),original);assert.ok(h.translations.every(value=>value==='en'));
});
test('untranslated errors get an English status while source events retain original diagnostics',()=>{
  const h=setup(),job={id:'failed',status:'failed',events:[{kind:'tool_end',turn:1,call_id:'a',tool:'probe_api',ok:false,message:'没有翻译词条的接口错误'}]};
  const nodes=all(h.render(job));assert.ok(nodes.some(n=>n.textContent==='Failed'));
  assert.equal(job.events[0].message,'没有翻译词条的接口错误');
  assert.equal(h.helper.message('HTTP 429: retry later','Failed'),'HTTP 429: retry later');
});
test('unfinished live and restored tools keep distinct truthful English states',()=>{
  const h=setup(),job={id:'pending',status:'running',events:[{kind:'tool_start',call_id:'a',tool:'inspect_report_data'}]};
  assert.ok(all(h.render(job)).some(n=>n.textContent==='Running…'));
  job.status='cancelled';assert.ok(all(h.render(job)).some(n=>n.textContent==='Round ended without a tool completion result'));
});
