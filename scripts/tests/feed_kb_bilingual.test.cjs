const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.join(__dirname,'../..');

function translate(value,language){
  const catalogue={messages:{},patterns:[],englishSource:{}};
  const names=fs.readdirSync(path.join(root,'static/i18n')).filter(name=>name.endsWith('.json'))
    .sort((a,b)=>(a==='common.json')-(b==='common.json')||a.localeCompare(b));
  for(const name of names){
    const data=JSON.parse(fs.readFileSync(path.join(root,'static/i18n',name),'utf8'));
    Object.assign(catalogue.messages,data.messages);catalogue.patterns.push(...(data.patterns||[]));
    Object.assign(catalogue.englishSource,data.englishSource||{});
  }
  const scope={FOS_I18N_CATALOG:catalogue,module:{exports:{}}};
  vm.runInNewContext(fs.readFileSync(path.join(root,'static/js/interface_language.js'),'utf8'),scope);
  return scope.module.exports.translate(value,language);
}

test('multi-object trend headings preserve source names and translate only system labels',()=>{
  const source=fs.readFileSync(path.join(root,'static/js/feed_kb_detail.js'),'utf8');
  const render=source.slice(source.indexOf('  function renderTrend(data)'),source.indexOf('  function syncPeriodControls()'));
  const element=(tag,className='',textContent='')=>({tag,className,textContent,children:[],
    append(...nodes){this.children.push(...nodes)},replaceChildren(...nodes){this.children=nodes},setAttribute(){}});
  const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,element('div'));return nodes.get(id)};
  vm.runInNewContext(render+'\nrenderTrend(data)',{
    data:{series:[{object_value:'保存',points:[]},{object_value:'Ride B',points:[]}],timezone:'Asia/Shanghai'},
    state:{hiddenObjects:new Set()},$,element,clear:node=>node.replaceChildren(),drawTrend(){},
    uiText:value=>translate(value,'en'),periodScopeNote:'scope',
    document:{createTextNode:value=>element('text','',value)},
  });
  const headings=$('feedKbSummaryHead').children[0].children;
  for(const [offset,name] of [[1,'保存'],[3,'Ride B']]){
    for(const [index,label] of [[offset,'统计值'],[offset+1,'较上期变化']]){
      const heading=headings[index];
      assert.notEqual(heading.className,'notranslate');
      assert.equal(heading.children[0].className,'notranslate');
      assert.equal(heading.children[0].textContent,name);
      assert.equal(heading.children[2].textContent,translate(label,'en'));
      assert.notEqual(heading.children[2].textContent,label);
    }
  }
});
