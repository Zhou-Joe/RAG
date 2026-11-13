const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function setup(){
  const scope={document:{createElement(tag){return {tag,children:[],attrs:{},textContent:'',append(...children){this.children.push(...children)},setAttribute(key,value){this.attrs[key]=value}}},createTextNode(value){return {textContent:value,children:[]}}},FOSI18n:{t:value=>({'条件触发':'Condition-based delivery','大于':'greater than','且':'AND','有值':'is present'})[value]||value},module:{exports:{}}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/js/report_trigger.js'),'utf8'),scope);
  return scope.module.exports;
}
function flatten(node){return [node,...node.children.flatMap(flatten)];}
test('trigger preview explains nested comparisons while preserving data labels and literals',()=>{
  const {render}=setup();
  const node=render({condition:{all:[{field:'保存',op:'gt',value:45},{field:'Status',op:'eq',value:'<img src=x onerror=alert(1)>'}]},match:'any',notify:'on_enter',cooldown_minutes:15},{matched:false});
  const nodes=flatten(node),raw=nodes.filter(n=>n.attrs?.translate==='no').map(n=>n.textContent);
  assert.ok(raw.includes('保存'));assert.ok(raw.includes('45'));assert.ok(raw.includes('"<img src=x onerror=alert(1)>"'));
  assert.ok(nodes.some(n=>n.textContent==='greater than'));assert.ok(nodes.some(n=>n.textContent==='AND'));
  assert.ok(nodes.every(n=>n.tag!=='img'));assert.ok(nodes.some(n=>n.textContent==='当前预览未满足触发条件；不会发送。'));
});
test('missing-value rule has a useful label without displaying an undefined comparison',()=>{
  const node=setup().render({condition:{field:'Reading',op:'is_present'},notify:'every_match',match:'all'});
  const texts=flatten(node).map(n=>n.textContent);
  assert.ok(texts.includes('is present'));assert.ok(!texts.includes('undefined'));
  assert.ok(texts.includes('每次检查满足条件时发送。'));assert.ok(texts.includes('所有输出行满足条件时触发。'));
});

test('shipped condition labels and check schedules translate in both directions',()=>{
  const catalog={messages:{},patterns:[],englishSource:{}};
  const directory=path.join(__dirname,'../../static/i18n');
  const names=fs.readdirSync(directory).filter(name=>name.endsWith('.json')).sort((a,b)=>(a==='common.json')-(b==='common.json')||a.localeCompare(b));
  for(const name of names){const part=JSON.parse(fs.readFileSync(path.join(directory,name),'utf8'));Object.assign(catalog.messages,part.messages);catalog.patterns.push(...(part.patterns||[]));Object.assign(catalog.englishSource,part.englishSource||{});}
  const scope={FOS_I18N_CATALOG:catalog,module:{exports:{}}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/js/interface_language.js'),'utf8'),scope);
  const {translate}=scope.module.exports;
  for(const [zh,en] of [
    ['条件触发','Condition-based delivery'],
    ['确认并启用条件触发','Confirm and enable conditional delivery'],
    ['当前数据未满足触发条件，本次未发送。','The current data does not meet the condition; nothing was sent.'],
    ['条件检查计划：每天 09:00（Asia/Shanghai）','Condition check schedule: Daily at 09:00 (Asia/Shanghai)'],
  ]){assert.equal(translate(zh,'en'),en);assert.equal(translate(en,'zh-hans'),zh);}
});
