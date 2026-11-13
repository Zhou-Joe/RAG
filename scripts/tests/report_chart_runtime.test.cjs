const {test}=require('node:test');
const assert=require('node:assert/strict');
const charts=require('../../static/js/report_chart_runtime.js');

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
  emit(type,extra={}){const e={target:this,key:'',preventDefault(){},...extra};for(const fn of [...(this.events[type]||[])])fn(e);}
  contains(node){return node===this||this.children.some(child=>child.contains(node));}
  all(){return this.children.flatMap(child=>[child,...child.all()]);}
  getBoundingClientRect(){return this.rect||{left:100,top:120,bottom:138,right:118,width:18,height:18};}
}
function environment(){const document=new Node('document');document.createElement=tag=>new Node(tag,document);document.createElementNS=(_ns,tag)=>document.createElement(tag);
  const window=new Node('window');window.innerWidth=800;window.innerHeight=600;document.defaultView=window;document.body=document.createElement('body');
  document.getElementById=id=>document.body.all().find(node=>node.getAttribute('id')===id);
  document.querySelectorAll=selector=>document.body.all().filter(node=>selector==='[data-engenie-chart]'&&node.getAttribute('data-engenie-chart')!==null);
  const host=document.createElement('div');document.body.append(host);return {document,window,host};}
function data(type='line',more={}){return {type,title:'Report <source>',language:'en',labels:['A','B','C','D'],series:[{label:'Wait <b>',values:[0,null,4,6],quality:[{valid:1,records:1},{valid:0,records:1},{valid:1,records:1},{valid:1,records:1}],type:'line',axis:'left'}],options:{legend:true},...more};}

test('line renderer retains zero and missing gaps with small marks and safe instant hover values',()=>{
  const h=environment();charts.render(h.host,data());const nodes=h.host.all();
  assert.equal(nodes.filter(node=>node.tagName==='path').length,1);
  const hits=nodes.filter(node=>node.getAttribute('fill')==='transparent');assert.equal(hits.length,3);
  assert.equal(nodes.filter(node=>node.tagName==='circle'&&node.getAttribute('r')==='2.2').length,3);
  assert.ok(hits[0].getAttribute('aria-label').includes('0'));hits[0].emit('pointerenter');
  const tip=h.document.body.all().find(node=>node.className==='erc-tooltip');assert.equal(tip.hidden,false);assert.ok(tip.textContent.includes('Wait <b>: 0'));
  assert.equal(tip.children.length,0);h.window.emit('scroll');assert.equal(tip.hidden,true);
  hits[0].emit('click');assert.equal(tip.hidden,false);h.host._ercTooltip.hide();assert.equal(tip.hidden,true);
});
test('combo uses bar and line marks, separate axes, and accessible legend toggles',()=>{
  const h=environment(),value=data('combo',{series:[{label:'Records',values:[100,200,300,400],type:'bar',axis:'left'},
    {label:'Average',values:[.1,.2,.3,.4],type:'line',axis:'right'}],options:{legend:true,y_label:'Records',right_y_label:'Average'}});
  charts.render(h.host,value);assert.equal(h.host.all().filter(n=>n.tagName==='rect').length,4);assert.equal(h.host.all().filter(n=>n.tagName==='path').length,1);
  assert.ok(h.host.textContent.includes('Average'));const first=h.host.all().find(n=>n.tagName==='button');assert.equal(first.getAttribute('aria-pressed'),'true');
  first.emit('click');assert.equal(h.host.all().filter(n=>n.tagName==='rect').length,0);assert.equal(h.document.body.all().filter(n=>n.className==='erc-tooltip').length,1);
  assert.equal((h.window.events.scroll||[]).length,1);
});
test('pie/doughnut share the runtime, handle zero totals, and expose exact values on focus',()=>{
  for(const type of ['pie','doughnut']){const h=environment();charts.render(h.host,data(type,{labels:['A','B'],series:[{label:'Records',values:[2,3]}]}));
    const slices=h.host.all().filter(n=>n.tagName==='path');assert.equal(slices.length,2);slices[1].emit('focus');assert.ok(h.document.body.all().find(n=>n.className==='erc-tooltip').textContent.includes('Records: 3'));
    charts.render(h.host,data(type,{labels:['A'],series:[{label:'Records',values:[0]}]}));assert.ok(h.host.textContent.includes('No positive values'));assert.equal(h.host.all().filter(n=>n.tagName==='path').length,0);}
});

test('pie and doughnut surfaces and center text follow live theme tokens while retaining report fallbacks',()=>{
  for(const type of ['pie','doughnut'])for(const values of [[2,3],[5]]){
    const h=environment();charts.render(h.host,data(type,{labels:values.map((_,i)=>String(i)),series:[{label:'Requests',values}]}));
    const marks=h.host.all().filter(n=>['path','circle'].includes(n.tagName)&&n.getAttribute('stroke')!==null);
    assert.equal(marks.length,values.length);
    for(const mark of marks)assert.equal(mark.getAttribute('stroke'),'var(--surface,#fff)');
    if(type==='doughnut'){
      const total=h.host.all().find(n=>n.tagName==='text'&&n.getAttribute('font-size')==='24');
      assert.equal(total.textContent,'5');assert.equal(total.getAttribute('fill'),'var(--text,#16324f)');
      if(values.length===1){
        const hole=h.host.all().find(n=>n.tagName==='circle'&&n.getAttribute('r')==='64');
        assert.equal(hole.getAttribute('fill'),'var(--surface,#fff)');
      }
    }
  }
});

test('chart marks and legend swatches share overridable theme colors including the Other group',()=>{
  const h=environment();charts.render(h.host,data('bar',{labels:['A','B'],series:[{label:'Requests',values:[2,3]}]}));
  const bars=h.host.all().filter(n=>n.tagName==='rect'),swatch=h.host.all().find(n=>n.className==='erc-swatch');
  assert.equal(bars[0].getAttribute('fill'),'var(--erc-series-1,#3574d5)');
  assert.equal(bars[1].getAttribute('fill'),swatch.style.background);
  charts.render(h.host,data('pie',{labels:['A','Other'],series:[{label:'Requests',values:[2,3]}],meta:{other_included:true}}));
  const slices=h.host.all().filter(n=>n.tagName==='path'),swatches=h.host.all().filter(n=>n.className==='erc-swatch');
  assert.equal(slices[1].getAttribute('fill'),'var(--erc-other,#a8b5c5)');
  assert.equal(swatches[1].style.background,slices[1].getAttribute('fill'));
  assert.equal(swatches[0].style.background,slices[0].getAttribute('fill'));
});
test('horizontal stacked bars and area/scatter configurations render using the same dependency',()=>{
  const horizontal=environment();charts.render(horizontal.host,data('bar',{series:[{label:'One',values:[1,2,3,4]},{label:'Two',values:[2,3,4,5]}],options:{horizontal:true,stacked:true}}));
  assert.equal(horizontal.host.all().filter(n=>n.tagName==='rect').length,8);
  const area=environment();charts.render(area.host,data('area',{options:{smooth:true}}));assert.equal(area.host.all().filter(n=>n.tagName==='path').length,2);
  const scatter=environment();charts.render(scatter.host,data('scatter',{x_values:[0,10,100,1000]}));
  assert.equal(scatter.host.all().filter(n=>n.tagName==='path').length,0);const dots=scatter.host.all().filter(n=>n.tagName==='circle'&&n.getAttribute('r')==='3');
  assert.equal(dots.length,3);assert.ok(Number(dots[1].getAttribute('cx'))-Number(dots[0].getAttribute('cx'))<Number(dots[2].getAttribute('cx'))-Number(dots[1].getAttribute('cx')));
});
test('mount reads only inert JSON and never interprets source labels as markup',()=>{
  const h=environment(),payload=h.document.createElement('script');payload.setAttribute('id','engenie-chart-data');payload.textContent=JSON.stringify([data()]);h.document.body.append(payload);h.host.setAttribute('data-engenie-chart','0');
  charts.mount(h.document);assert.ok(h.host.all().some(n=>n.tagName==='svg'));assert.equal(h.host.all().filter(n=>n.tagName==='b').length,0);
  assert.ok(h.host.textContent.includes('Wait <b>'));
  payload.textContent='invalid';assert.doesNotThrow(()=>charts.mount(h.document));
});
test('formatter and scale retain zero, negatives and precision',()=>{
  assert.equal(charts.format(0),'0');assert.equal(charts.format(null),'—');assert.equal(charts.format(.123456789012),'0.123456789012');
  assert.ok(charts.format(1e-100).includes('e-100'));assert.ok(charts.format(1e100).includes('e+100'));
  assert.deepEqual(charts.bounds([-2,0,3]),{low:-2,high:3});assert.ok(charts.linePath([[0,0],[1,1],[2,0]],true).includes('Q'));
});
test('line and area charts preserve irregular numeric horizontal spacing',()=>{
  for(const type of ['line','area']){
    const h=environment();charts.render(h.host,data(type,{labels:['0','10','100'],x_values:[0,10,100],
      series:[{label:'Actual',values:[1,2,3]}]}));
    const dots=h.host.all().filter(n=>n.tagName==='circle'&&n.getAttribute('r')==='2.2');
    const xs=dots.map(dot=>Number(dot.getAttribute('cx')));
    assert.equal(dots.length,3);
    assert.ok(Math.abs((xs[1]-xs[0])/(xs[2]-xs[0])-.1)<1e-12,type);
  }
});

test('metric cards highlight one formatted value with inline units and omit source and record metadata',()=>{
  const h=environment();
  charts.render(h.host,{type:'metric',title:'Average wait <b>',value:1234.125,unit:'min <img src=x onerror=alert(1)>',
    subtitle:'Latest <script>alert(1)</script>',quality:{valid:7,records:10,missing:3},language:'en'});
  const find=name=>h.host.all().find(n=>n.className===name);
  assert.equal(find('erc-metric-value').textContent,'1,234.125 min <img src=x onerror=alert(1)>');
  assert.equal(find('erc-metric-unit'),undefined);
  assert.equal(find('erc-metric-subtitle'),undefined);
  assert.equal(find('erc-metric').getAttribute('aria-label'),'Average wait <b>');
  assert.equal(find('erc-metric-quality'),undefined);
  assert.ok(!h.host.textContent.includes('Latest'));assert.ok(!h.host.textContent.includes('records'));
  assert.equal(h.host.all().filter(n=>['img','script','svg'].includes(n.tagName)).length,0);
  assert.equal(h.document.body.all().filter(n=>n.className==='erc-tooltip').length,0);
  charts.render(h.host,{type:'metric',title:'Count',value:0});
  assert.equal(find('erc-metric-value').textContent,'0');
  assert.equal(find('erc-metric-unit'),undefined);
  assert.equal(find('erc-metric-quality'),undefined);
});

test('metric cards distinguish missing values from zero and keep signed percentage values exact',()=>{
  const h=environment(),value=()=>h.host.all().find(n=>n.className==='erc-metric-value').textContent;
  for(const missing of [null,undefined,NaN,Infinity,false,'']){
    charts.render(h.host,{type:'metric',title:'Missing',value:missing});
    assert.equal(value(),'—');
  }
  charts.render(h.host,{type:'metric',title:'Change',value:-12.5,unit:'%',language:'zh-hans',quality:{valid:0,records:2,missing:2}});
  assert.equal(value(),'-12.5%');
  assert.equal(h.host.all().find(n=>n.className==='erc-metric-unit'),undefined);
  assert.equal(h.host.all().find(n=>n.className==='erc-metric-quality'),undefined);
});

test('switching between charts and scalar cards removes old tooltip resources and supports inert report JSON',()=>{
  const h=environment();charts.render(h.host,data());
  assert.equal((h.window.events.scroll||[]).length,1);
  charts.render(h.host,{type:'metric',title:'Total',value:99});
  assert.equal((h.window.events.scroll||[]).length,0);
  assert.equal((h.document.events.keydown||[]).length,0);
  assert.equal(h.document.body.all().filter(n=>n.className==='erc-tooltip').length,0);
  assert.equal(h.host._ercTooltip,undefined);
  const payload=h.document.createElement('script');payload.setAttribute('id','engenie-chart-data');
  payload.textContent=JSON.stringify([{type:'metric',title:'Total',value:0,unit:'visits'}]);
  h.document.body.append(payload);h.host.setAttribute('data-engenie-chart','0');charts.mount(h.document);
  assert.equal(h.host.all().find(n=>n.className==='erc-metric-value').textContent,'0 visits');
  assert.equal(h.host.all().find(n=>n.className==='erc-metric-unit'),undefined);
  charts.render(h.host,data());
  assert.equal((h.window.events.scroll||[]).length,1);
});
