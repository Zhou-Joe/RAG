const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/feed_kb_detail.js'),'utf8');

function harness(){
  const make=(id='')=>({id,dataset:{},value:'',hidden:false,disabled:false,open:false,children:[],events:{},attributes:{},options:[],
    get selectedOptions(){return this.options.filter(item=>item.value===this.value)},
    setAttribute(name,value){this.attributes[name]=value},
    append(...nodes){this.children.push(...nodes)},replaceChildren(...nodes){this.children=nodes},
    addEventListener(type,handler){(this.events[type]??=[]).push(handler)},
    dispatchEvent(event){for(const handler of this.events[event.type]||[])handler(event)},
    focus(){this.focused=true}});
  const elements=new Map(),$=id=>{if(!elements.has(id))elements.set(id,make(id));return elements.get(id)};
  const groups={};
  for(const [name,values] of Object.entries({Mode:['summary','trend'],Operation:['count','avg','sum','min','max'],
    Range:['24h','7d','30d','today','all','custom'],Period:['day','week','month','custom'],View:['table','chart']})){
    groups[`[data-fkd-${name.toLowerCase()}]`]=values.map(value=>{
      const button=make();button.dataset['fkd'+name]=value;return button;
    });
  }
  const values={feedKbAnalysisMode:'summary',feedKbOperation:'count',feedKbGroup:'$period:day',feedKbPeriodSpan:'1',
    feedKbAnalysisPreset:'7d',feedKbAnalysisFrom:'2026-11-01T00:00',feedKbAnalysisTo:'2026-11-08T00:00',
    feedKbMeasure:'等待时间',feedKbSummaryGroup:'',feedKbChartMode:'value',feedKbScope:'latest',feedKbColumns:'0',
    feedKbFeed:'feed-1',feedKbSearch:'Ride',feedKbFilterField:'景点',feedKbFilterValue:'A',feedKbFrom:'',feedKbTo:'',feedKbTrendField:'景点',feedKbTrendValue:''};
  for(const [id,value] of Object.entries(values))$(id).value=value;
  $('feedKbChart').hidden=true;
  for(const [id,options] of Object.entries({
    feedKbOperation:[['count','记录数'],['avg','平均值'],['sum','合计'],['min','最小值'],['max','最大值']],
    feedKbGroup:[['$period:minute','分钟'],['$period:hour','小时'],['$period:day','天'],['$period:week','周'],['$period:month','月']],
    feedKbChartMode:[['value','数值'],['change','较上期变化'],['change_pct','变化率（%）']],
  }))$(id).options=options.map(([value,textContent])=>({value,textContent}));
  const counts={dirty:0,requests:0,draws:0};
  const state={objects:['Ride A','Ride,B'],timezone:'Asia/Shanghai',dataFrom:'2026-01-01T00:00:00Z',dataTo:'2026-11-01T00:00:00Z'};
  const translations={'分组字段':'Group field','统计方式':'Metric','对比周期':'Interval','图表口径':'Chart metric','最小值':'Minimum','最大值':'Maximum'};
  const scope={$,state,URLSearchParams,Date,Intl,Event,
    document:{querySelectorAll:selector=>groups[selector]||[],createTextNode:textContent=>({textContent})},
    window:{FOSI18n:{originalText:node=>node.textContent}},
    element:(tag,className,textContent)=>Object.assign(make(),{tag,className,textContent}),
    clear:node=>node.replaceChildren(),uiText:value=>translations[value]||value,
    periodMode:()=>$('feedKbAnalysisMode').value==='trend',periodUnit:()=>$('feedKbGroup').value.slice(8),
    periodSpan:()=>Math.max(1,parseInt($('feedKbPeriodSpan').value,10)||1),
    setObjectOpen(){},updateApplyState(){},markAnalysisChanged(){state.analysisDirty=true;counts.dirty++},
    loadOptions:async()=>{counts.requests++},drawTrend(){counts.draws++},renderLegend(){},
  };
  vm.createContext(scope);
  const functions=source.slice(source.indexOf('  function syncPeriodControls()'),source.indexOf('  function promptForObject()'));
  const rangeFunctions=source.slice(source.indexOf('  function dateInput('),source.indexOf('  function renderRows('));
  const params=source.slice(source.indexOf('  function params('),source.indexOf('  async function request('));
  const handlers=source.slice(source.indexOf("  document.querySelectorAll('[data-fkd-mode]').forEach(button => button.addEventListener"),source.indexOf("  $('feedKbTrendChips').addEventListener"));
  const rangeHandlers=source.slice(source.indexOf("  $('feedKbAnalysisPreset').addEventListener('change'"),source.indexOf("  $('feedKbAnalysisForm').addEventListener"));
  const viewHandlers=source.slice(source.indexOf("  document.querySelectorAll('[data-fkd-view]').forEach(button => button.addEventListener"),source.indexOf("  const edit = $('feedKbEdit');"));
  vm.runInContext(functions+rangeFunctions+params+handlers+rangeHandlers+viewHandlers+'\nsyncPeriodControls();',scope);
  const button=(group,value)=>groups[`[data-fkd-${group}]`].find(node=>Object.values(node.dataset).includes(value));
  const click=(group,value)=>button(group,value).dispatchEvent(new Event('click'));
  const change=(id,value)=>{$(id).value=value;$(id).dispatchEvent(new Event('change'))};
  return {$,state,counts,button,click,change,params:kind=>scope.params(kind,0)};
}

test('metric chips drive backing selects and advanced metrics remain active when collapsed',()=>{
  const h=harness();
  h.click('operation','avg');
  assert.equal(h.$('feedKbOperation').value,'avg');
  assert.equal(h.button('operation','avg').attributes['aria-pressed'],'true');
  assert.equal(h.$('feedKbMeasureWrap').hidden,false);
  assert.equal(h.params('summary').get('op'),'avg');
  assert.equal(h.params('summary').get('measure'),'等待时间');
  h.click('operation','min');h.$('feedKbAdvanced').open=false;
  h.$('feedKbAdvanced').dispatchEvent(new Event('toggle'));
  assert.equal(h.params('summary').get('op'),'min');
  assert.equal(h.$('feedKbAdvancedSummary').hidden,false);
  assert.ok(h.$('feedKbAdvancedSummary').children.some(node=>node.textContent==='Minimum'));
  assert.equal(h.counts.requests,0);
});

test('range chips keep datetime values enabled and show them only for custom ranges',()=>{
  const h=harness();h.click('range','24h');
  assert.equal(h.$('feedKbCustomRange').hidden,true);
  assert.ok(h.$('feedKbAnalysisFrom').value);assert.ok(h.$('feedKbAnalysisTo').value);
  assert.equal(h.$('feedKbAnalysisFrom').disabled,false);
  assert.equal(h.$('feedKbAnalysisTo').disabled,false);
  const from=h.$('feedKbAnalysisFrom').value,to=h.$('feedKbAnalysisTo').value;
  h.click('range','custom');
  assert.equal(h.$('feedKbCustomRange').hidden,false);
  assert.equal(h.$('feedKbAnalysisFrom').value,from);assert.equal(h.$('feedKbAnalysisTo').value,to);
  assert.equal(h.$('feedKbAnalysisFrom').focused,true);
  h.change('feedKbAnalysisFrom','2026-02-01T00:00');
  assert.equal(h.button('range','custom').attributes['aria-pressed'],'true');
  assert.equal(h.params('summary').get('from'),'2026-02-01T00:00');
  h.state.dataFrom=null;h.state.dataTo=null;h.click('range','all');
  assert.ok(h.$('feedKbAnalysisFrom').value<h.$('feedKbAnalysisTo').value);
  assert.equal(h.$('feedKbCustomRange').hidden,true);
});

test('custom period expansion preserves exact span and values without fetching data',()=>{
  const h=harness();h.click('mode','trend');
  h.change('feedKbGroup','$period:week');h.change('feedKbPeriodSpan','3');
  const requests=h.counts.requests,dirty=h.counts.dirty;
  h.click('period','custom');
  assert.equal(h.$('feedKbAdvanced').open,true);assert.equal(h.$('feedKbPeriodSpan').focused,true);
  assert.equal(h.$('feedKbCustomPeriod').hidden,false);
  assert.equal(h.counts.requests,requests);assert.equal(h.counts.dirty,dirty);
  const query=h.params('trend');
  assert.equal(query.get('period'),'week');assert.equal(query.get('span'),'3');
  assert.deepEqual(query.getAll('object_value'),['Ride A','Ride,B']);
  h.$('feedKbAdvanced').open=false;h.$('feedKbAdvanced').dispatchEvent(new Event('toggle'));
  assert.equal(h.params('trend').get('span'),'3');assert.equal(h.counts.requests,requests);
  h.click('period','month');
  assert.equal(h.params('trend').get('period'),'month');assert.equal(h.params('trend').get('span'),'1');
  assert.equal(h.button('period','month').attributes['aria-pressed'],'true');
  assert.equal(h.button('period','custom').attributes['aria-pressed'],'false');
});

test('hidden advanced selections persist across modes and chart controls follow chart visibility',()=>{
  const h=harness();h.change('feedKbSummaryGroup','保存');h.$('feedKbAdvanced').open=false;
  h.$('feedKbAdvanced').dispatchEvent(new Event('toggle'));
  const name=h.$('feedKbAdvancedSummary').children.find(node=>node.textContent==='保存');
  assert.equal(name.className,'notranslate');assert.equal(h.params('summary').get('group'),'保存');
  h.click('mode','trend');assert.equal(h.$('feedKbSummaryGroupWrap').hidden,true);
  assert.equal(h.$('feedKbChartModeWrap').hidden,true);
  h.click('view','chart');assert.equal(h.$('feedKbChartModeWrap').hidden,false);
  h.change('feedKbChartMode','change_pct');h.click('view','table');
  assert.equal(h.$('feedKbChartModeWrap').hidden,true);
  assert.equal(h.$('feedKbChartMode').value,'change_pct');
  h.click('mode','summary');assert.equal(h.params('summary').get('group'),'保存');
  assert.equal(h.$('feedKbCustomPeriod').hidden,true);
  assert.equal(h.$('feedKbChartModeWrap').hidden,true);assert.equal(h.counts.requests,0);
});
