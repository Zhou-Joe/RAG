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
