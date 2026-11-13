const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const tooltip=require('../../static/js/chart_tooltip.js');

class Node {
  constructor(tag='',doc=null){this.tagName=tag;this.ownerDocument=doc;this.children=[];this.parentNode=null;this.attributes={};this.events={};this.style={};this.className='';this.hidden=false;this._text='';
    this.classList={add:name=>{this.className=[...new Set([...this.className.split(' ').filter(Boolean),name])].join(' ')},remove:name=>{this.className=this.className.split(' ').filter(n=>n!==name).join(' ')},contains:name=>this.className.split(' ').includes(name)};}
  append(...nodes){for(const child of nodes){child.remove?.();child.parentNode=this;this.children.push(child);}}
  appendChild(child){this.append(child);return child;}
  replaceChildren(...nodes){this.children.forEach(child=>child.parentNode=null);this.children=[];this._text='';this.append(...nodes);}
  remove(){if(this.parentNode){this.parentNode.children=this.parentNode.children.filter(child=>child!==this);this.parentNode=null;}}
  set textContent(value){this.replaceChildren();this._text=String(value);}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  setAttribute(key,value){this.attributes[key]=String(value);if(key==='class')this.className=String(value);}
  getAttribute(key){return this.attributes[key]??null;}
  removeAttribute(key){delete this.attributes[key];}
  addEventListener(type,handler){(this.events[type]??=[]).push(handler);}
  removeEventListener(type,handler){this.events[type]=(this.events[type]||[]).filter(item=>item!==handler);}
  emit(type,extra={}){const event={target:this,key:'',preventDefault(){this.prevented=true;},...extra};for(const handler of [...(this.events[type]||[])])handler(event);return event;}
  contains(other){return other===this||this.children.some(child=>child.contains(other));}
  all(){return this.children.flatMap(child=>[child,...child.all()]);}
  getBoundingClientRect(){if(this.rect)return this.rect;if(this.className.includes('fos-chart-tooltip'))return {left:0,top:0,width:240,height:72,right:240,bottom:72};return {left:100,top:150,width:18,height:18,right:118,bottom:168};}
}
function fixture(){
  const document=new Node('document');document.createElement=tag=>new Node(tag,document);document.createElementNS=(_ns,tag)=>document.createElement(tag);document.createTextNode=text=>{const n=document.createElement('text');n.textContent=text;return n;};
  document.body=document.createElement('body');document.documentElement={clientWidth:640,clientHeight:480};
  const window=new Node('window');window.innerWidth=640;window.innerHeight=480;window.FosChartTooltip=tooltip;document.defaultView=window;
  const host=document.createElement('div');document.body.append(host);
  return {document,window,host,tip:()=>document.body.all().find(node=>node.className.includes('fos-chart-tooltip'))};
}
test('hover displays exact safe values immediately and leaving hides them',()=>{
  const h=fixture(),target=h.document.createElement('circle'),marker=h.document.createElement('circle');h.host.append(target,marker);
  const controller=tooltip.create(h.host);controller.bind(target,['<script>source</script>','2026-11-13','Value: 0'],{marker});
  assert.equal(h.tip().hidden,true);target.emit('pointerenter');
  assert.equal(h.tip().hidden,false);assert.ok(h.tip().textContent.includes('<script>source</script>'));
  assert.ok(h.tip().textContent.includes('Value: 0'));assert.equal(h.tip().children.length,3);
  assert.ok(marker.classList.contains('is-chart-point-active'));assert.equal(target.getAttribute('aria-describedby'),h.tip().id);
  target.emit('pointerleave');assert.equal(h.tip().hidden,true);assert.equal(target.getAttribute('aria-describedby'),null);
});
test('keyboard focus, tap toggling, Escape and outside clicks are supported',()=>{
  const h=fixture(),point=h.document.createElement('circle');h.host.append(point);const controller=tooltip.create(h.host);controller.bind(point,()=>['Negative value','-4.5']);
  h.document.activeElement=point;point.emit('focus');point.emit('pointerleave');assert.equal(h.tip().hidden,false);
  h.document.activeElement=null;point.emit('blur');assert.equal(h.tip().hidden,true);
  point.emit('click');assert.equal(h.tip().hidden,false);point.emit('click');assert.equal(h.tip().hidden,true);
  assert.equal(point.emit('keydown',{key:'Enter'}).prevented,true);assert.equal(h.tip().hidden,false);
  h.document.emit('keydown',{key:'Escape'});assert.equal(h.tip().hidden,true);
  point.emit('click');h.document.emit('pointerdown',{target:h.host});assert.equal(h.tip().hidden,true);
});
test('tooltips stay inside viewport at chart edges and hide when scrolling or resizing',()=>{
  const h=fixture(),point=h.document.createElement('circle');point.rect={left:630,top:0,right:640,bottom:10,width:10,height:10};
  const controller=tooltip.create(h.host);controller.bind(point,['Edge point']);point.emit('pointerenter');
  assert.equal(parseFloat(h.tip().style.left),392);assert.equal(parseFloat(h.tip().style.top),20);
  h.window.emit('scroll');assert.equal(h.tip().hidden,true);point.emit('focus');h.window.emit('resize');assert.equal(h.tip().hidden,true);
});
test('redraw removes old portal/listeners and restores existing aria descriptions',()=>{
  const h=fixture(),point=h.document.createElement('circle');point.setAttribute('aria-describedby','existing');
  const old=tooltip.create(h.host);old.bind(point,['Old']);point.emit('focus');
  const current=tooltip.create(h.host);assert.equal(point.getAttribute('aria-describedby'),'existing');
  assert.equal(h.document.body.all().filter(n=>n.className.includes('fos-chart-tooltip')).length,1);
  assert.equal((point.events.focus||[]).length,0);current.destroy();assert.equal(h.tip(),undefined);
  assert.equal((h.window.events.scroll||[]).length,0);assert.equal(h.host._fosChartTooltip,undefined);
});
test('real trend renderer uses small marks, accessible hover targets and preserves missing-data gaps',()=>{
  const h=fixture(),mode={value:'value'},source=fs.readFileSync(path.join(__dirname,'../../static/js/feed_kb_detail.js'),'utf8');
  const start=source.indexOf('  function drawTrend() {'),end=source.indexOf('  function changeText(',start);
  const state={trendSeries:[{object_value:'Ride <A>',points:[{label:'Day 1',value:0},{label:'Day 2',value:null},{label:'Day 3',value:5},{label:'Day 4',value:6}]}],hiddenObjects:new Set()};
  function element(tag,cls,text){const node=h.document.createElement(tag);node.className=cls||'';if(text!==undefined)node.textContent=text;return node;}
  vm.runInNewContext(source.slice(start,end)+'drawTrend();',{document:h.document,window:h.window,state,
    $:id=>id==='feedKbChart'?h.host:mode,clear:node=>{node._fosChartTooltip?.destroy();node.replaceChildren();},renderLegend(){},
    number:value=>value===null?null:Number(value),pointMetric:point=>point.value,chartField:()=>mode.value,
    numberText:value=>String(value),modeNames:{value:'Value'},uiText:value=>value,element,SERIES_COLORS:['blue']});
  const marks=h.host.all().filter(node=>node.className==='fkd-trend-dot');
  const hits=h.host.all().filter(node=>node.className==='fkd-trend-hit');
  assert.equal(marks.length,3);assert.ok(marks.every(mark=>Number(mark.getAttribute('r'))<=2.2));
  assert.equal(hits.length,3);assert.ok(hits.every(hit=>hit.getAttribute('tabindex')==='0'));
  assert.equal(h.host.all().filter(node=>node.tagName==='polyline').length,1);
  assert.equal(h.host.children[0].getAttribute('aria-hidden'),null);
  hits[0].emit('pointerenter');assert.ok(h.tip().textContent.includes('Ride <A>'));assert.ok(h.tip().textContent.includes('Value: 0'));
});
