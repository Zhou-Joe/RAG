const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const runtime=require('../../static/js/report_chart_runtime.js');
const visuals=require('../../static/js/chat_visualizations.js');

class Node{
  constructor(tag='',doc=null){this.tagName=tag;this.ownerDocument=doc;this.children=[];this.attributes={};this.style={};this.events={};this.className='';this.hidden=false;this._text='';this.parentNode=null;
    this.classList={add:name=>{this.className=[...new Set([...this.className.split(' ').filter(Boolean),name])].join(' ')},remove:name=>{this.className=this.className.split(' ').filter(n=>n!==name).join(' ')}};}
  append(...nodes){for(const node of nodes){node.remove?.();node.parentNode=this;this.children.push(node);}}
  appendChild(node){this.append(node);return node;}
  replaceChildren(...nodes){this.children.forEach(child=>child.parentNode=null);this.children=[];this._text='';this.append(...nodes);}
  remove(){if(this.parentNode){this.parentNode.children=this.parentNode.children.filter(node=>node!==this);this.parentNode=null;}}
  set textContent(value){this.replaceChildren();this._text=String(value);}
  get textContent(){return this._text+this.children.map(node=>node.textContent).join('');}
  setAttribute(key,value){this.attributes[key]=String(value);if(key==='class')this.className=String(value);}
  getAttribute(key){return this.attributes[key]??null;}
  addEventListener(type,fn){(this.events[type]??=[]).push(fn);}
  removeEventListener(type,fn){this.events[type]=(this.events[type]||[]).filter(f=>f!==fn);}
  emit(type,extra={}){const event={target:this,key:'',preventDefault(){},...extra};for(const fn of [...(this.events[type]||[])])fn(event);}
  contains(node){return node===this||this.children.some(child=>child.contains(node));}
  all(){return this.children.flatMap(child=>[child,...child.all()]);}
  getBoundingClientRect(){return {left:100,top:120,bottom:138,right:118,width:18,height:18};}
}
function environment(){const document=new Node('document');document.createElement=tag=>new Node(tag,document);document.createElementNS=(_ns,tag)=>document.createElement(tag);
  const window=new Node('window');window.innerWidth=800;window.innerHeight=600;document.defaultView=window;document.body=document.createElement('body');
  const host=document.createElement('div');document.body.append(host);return {document,window,host};}
function chart(type='line',extra={}){return {type,title:'Exact <source>',labels:['A','B','C'],series:[{label:'Rate <b>',values:[0,null,2]}],options:{legend:true},...extra};}

test('normalization preserves zeros, gaps, numeric strings, 8 series and 500 exact points',()=>{
  const data=visuals.normalize(chart('line',{labels:Array.from({length:500},(_,i)=>String(i)),
    series:Array.from({length:8},(_,i)=>({label:'Series '+i,values:Array(500).fill(i===0?'0':null)}))}));
  assert.equal(data.labels.length,500);assert.equal(data.series.length,8);assert.equal(data.series[0].values[0],0);
  assert.equal(data.series[1].values[499],null);
  const long='源 <script>'+ 'x'.repeat(1200);assert.equal(visuals.normalize(chart('line',{labels:[long],series:[{label:long,values:[1]}]})).labels[0],long);
});
test('normalization rejects malformed, executable, nonfinite and silently mismatched chart values',()=>{
  for(const data of [chart('script'),chart('line',{labels:[]}),chart('line',{series:[{label:'X',values:[1,2]}]}),
    chart('line',{series:[{label:'X',values:[0,NaN,2]}]}),chart('line',{series:[{label:'X',values:[0,true,2]}]}),
    chart('line',{options:{legend:'true'}}),chart('pie',{series:[{label:'X',values:[0,-1,2]}]}),
    chart('combo'),chart('line',{labels:Array(501).fill('X')}),chart('line',{labels:['x'.repeat(4001)]})])
    assert.throws(()=>visuals.normalize(data));
  const clean=visuals.normalize(chart('line',{options:{onClick:'alert(1)',legend:true},script:'alert(1)'}));
  assert.equal(clean.script,undefined);assert.equal(clean.options.onClick,undefined);
});
test('numeric horizontal values are accepted for line and area without changing source labels',()=>{
  for(const type of ['line','area']){
    const data=visuals.normalize(chart(type,{x_values:[0,.9,100]}));
    assert.deepEqual(data.x_values,[0,.9,100]);assert.deepEqual(data.labels,['A','B','C']);
  }
  assert.throws(()=>visuals.normalize(chart('bar',{x_values:[0,1,2]})));
});
test('mount uses the shared renderer with tiny line markers and safe hover/focus/tap values',()=>{
  const h=environment();assert.equal(visuals.mount(h.host,[chart()]),1);
  const nodes=h.host.all();assert.equal(nodes.filter(n=>n.tagName==='script'||n.tagName==='b').length,0);
  assert.ok(h.host.textContent.includes('Exact <source>'));
  assert.equal(nodes.filter(n=>n.tagName==='circle'&&n.getAttribute('r')==='2.2').length,2);
  const hit=nodes.find(n=>n.getAttribute('fill')==='transparent');hit.emit('pointerenter');
  const tip=h.document.body.all().find(n=>n.className==='erc-tooltip');assert.equal(tip.hidden,false);
  assert.ok(tip.textContent.includes('Rate <b>: 0'));assert.equal(tip.children.length,0);
  hit.emit('keydown',{key:'Escape'});assert.equal(tip.hidden,true);
  hit.emit('focus');assert.equal(tip.hidden,false);hit.emit('click');hit.emit('pointerleave');assert.equal(tip.hidden,false);
  visuals.destroy(h.host);assert.equal(h.document.body.all().filter(n=>n.className==='erc-tooltip').length,0);
});
test('bar, pie, combo, area, scatter and doughnut share the same inline rendering dependency',()=>{
  for(const type of ['bar','pie','combo','area','scatter','doughnut']){
    const h=environment(),data=chart(type,{series:[{label:'First',values:[1,2,3],type:'bar'}]});
    if(type==='combo')data.series.push({label:'Second',values:[.1,.2,.3],type:'line',axis:'right'});
    if(type==='scatter')data.x_values=[0,10,100];
    assert.equal(visuals.mount(h.host,[data]),1,type);
    assert.ok(h.host.all().some(n=>n.tagName==='svg'),type);
    visuals.destroy(h.host);
  }
});
test('redrawing and ancestor cleanup remove portal tooltips and global listeners',()=>{
  const h=environment(),child=h.document.createElement('div');h.host.appendChild(child);
  visuals.mount(child,[chart()]);assert.equal(h.document.body.all().filter(n=>n.className==='erc-tooltip').length,1);
  visuals.mount(child,[chart('bar')]);assert.equal(h.document.body.all().filter(n=>n.className==='erc-tooltip').length,1);
  assert.equal(h.window.events.scroll.length,1);
  visuals.destroy(h.host);assert.equal(h.document.body.all().filter(n=>n.className==='erc-tooltip').length,0);
  assert.equal(h.window.events.scroll.length,0);
  assert.equal(visuals.mount(child,[chart('unsafe')]),0);assert.equal(child.children.length,0);
});
test('UI language changes rerender controlled axis labels without translating source names',()=>{
  const h=environment(),context={document:h.document,EnGenieReportCharts:runtime,module:{exports:{}}};
  vm.runInNewContext(fs.readFileSync(require.resolve('../../static/js/chat_visualizations.js'),'utf8'),context);
  const api=context.module.exports,data=chart('line',{title:'Value',series:[{label:'Change',values:[1,2,3]}],
    options:{y_label:'Value'},meta:{system_metric:'value',omitted_points:2}});
  api.mount(h.host,[data],{language:'en'});assert.ok(h.host.textContent.includes('additional'));
  h.document.emit('fos:languagechange',{detail:{language:'zh-hans'}});
  assert.ok(h.host.textContent.includes('数值'));assert.ok(h.host.textContent.includes('未绘制'));
  assert.ok(h.host.textContent.includes('Value'));assert.ok(h.host.textContent.includes('Change'));
  h.document.emit('fos:languagechange',{detail:{language:'en'}});
  assert.ok(h.host.textContent.includes('additional'));assert.ok(!h.host.textContent.includes('数值'));
  api.destroy(h.host);assert.equal(h.document.body.all().filter(n=>n.className==='erc-tooltip').length,0);
  const summary=api.normalize(chart('bar',{series:[{label:'Value',values:[1,2,3]}],meta:{system_metric:'value',system_summary:true}}),'zh-hans');
  assert.equal(summary.series[0].label,'数值');assert.deepEqual(Array.from(summary.labels),['A','B','C']);
});
test('invalid and absent chart lists preserve fallback and do not allocate tooltip portals',()=>{
  const h=environment();assert.equal(visuals.mount(h.host,[]),0);
  assert.equal(visuals.mount(h.host,Array(9).fill(chart())),0);
  assert.equal(visuals.mount(h.host,[chart('html')]),0);
  assert.equal(h.document.body.all().filter(n=>n.className==='erc-tooltip').length,0);
});

test('single-value cards normalize zero, missing values and quality without fabricated series',()=>{
  const card=visuals.normalize({type:'metric',title:'Total <b>',value:0,unit:'items',subtitle:'All records',
    quality:{valid:0,records:4,missing:4}});
  assert.equal(card.value,0);assert.equal(card.labels,undefined);assert.equal(card.series,undefined);
  assert.deepEqual(card.quality,{valid:0,records:4,missing:4});
  assert.equal(visuals.normalize({type:'metric',title:'Average',value:null}).value,null);
  assert.equal(visuals.normalize({type:'metric',title:'Change',value:'-12.5',unit:'%'}).value,-12.5);
  for(const value of [NaN,Infinity,true,undefined,''])
    assert.throws(()=>visuals.normalize({type:'metric',title:'Bad',value}));
  assert.throws(()=>visuals.normalize({type:'metric',title:'Bad',value:1,quality:{valid:2,records:1}}));
});

test('metric cards show a highlighted scalar and safe literal title, unit and subtitle',()=>{
  for(const [value,expected] of [[0,'0'],[null,'—'],[-12.5,'-12.5']]){
    const h=environment();
    assert.equal(visuals.mount(h.host,[{type:'metric',title:'Total <script>',value,unit:'<img>',subtitle:'Source <b>'}]),1);
    const scalar=h.host.all().find(node=>node.className==='erc-metric-value');
    assert.equal(scalar.textContent,expected);assert.ok(h.host.textContent.includes('Total <script>'));
    assert.ok(h.host.textContent.includes('<img>'));assert.ok(h.host.textContent.includes('Source <b>'));
    assert.equal(h.host.all().filter(node=>node.tagName==='svg'||node.tagName==='script'||node.tagName==='img').length,0);
    assert.equal(h.document.body.all().filter(node=>node.className==='erc-tooltip').length,0);
    visuals.destroy(h.host);
  }
});

test('cards begin compact and independently expand to full width and collapse again',()=>{
  const h=environment();visuals.mount(h.host,[chart(),{type:'metric',title:'Records',value:40}]);
  const cards=()=>h.host.all().filter(node=>node.className.split(' ').includes('chat-chart-card'));
  const controls=()=>h.host.all().filter(node=>node.className==='chat-chart-size');
  assert.equal(controls().length,2);assert.ok(cards().every(node=>!node.className.includes('is-expanded')));
  assert.ok(controls().every(node=>node.getAttribute('aria-expanded')==='false'&&node.textContent==='Expand'));
  controls()[0].emit('click');assert.ok(cards()[0].className.includes('is-expanded'));
  assert.ok(!cards()[1].className.includes('is-expanded'));assert.equal(controls()[0].textContent,'Collapse');
  const firstHeader=h.host.all().find(node=>node.className==='chat-chart-header');
  firstHeader.emit('click');assert.ok(!cards()[0].className.includes('is-expanded'));
  controls()[1].emit('click');assert.ok(cards()[1].className.includes('is-expanded'));
  visuals.destroy(h.host);
});

test('expanded states survive citation redraws and language switches while data interactions stay separate',()=>{
  const h=environment(),context={document:h.document,EnGenieReportCharts:runtime,module:{exports:{}}};
  vm.runInNewContext(fs.readFileSync(require.resolve('../../static/js/chat_visualizations.js'),'utf8'),context);
  const api=context.module.exports,data=[chart()];api.mount(h.host,data,{language:'en'});
  const control=()=>h.host.all().find(node=>node.className==='chat-chart-size');
  control().emit('click');assert.equal(control().getAttribute('aria-expanded'),'true');
  api.mount(h.host,data,{language:'en'});assert.equal(control().getAttribute('aria-expanded'),'true');
  h.document.emit('fos:languagechange',{detail:{language:'zh-hans'}});
  assert.equal(control().textContent,'收起');assert.equal(control().getAttribute('aria-expanded'),'true');
  const legend=h.host.all().find(node=>node.getAttribute('aria-pressed')!==null);legend.emit('click');
  assert.equal(control().getAttribute('aria-expanded'),'true');
  control().emit('click');assert.equal(control().textContent,'展开');
  assert.equal(control().getAttribute('aria-expanded'),'false');api.destroy(h.host);
});
