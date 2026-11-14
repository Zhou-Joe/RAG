const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/chat_table_cards.js'),'utf8');

class Element {
  constructor(tag,document){this.tagName=tag.toUpperCase();this.ownerDocument=document;this.children=[];this.parentNode=null;this.className='';this.attributes={};this.listeners={};this._text='';
    this.classList={contains:name=>this.className.split(/\s+/).includes(name),toggle:(name,force)=>{const next=force===undefined?!this.classList.contains(name):!!force;this.className=this.className.split(/\s+/).filter(value=>value&&value!==name).concat(next?[name]:[]).join(' ');return next;}};}
  set innerHTML(_value){throw new Error('The table module must not interpret source markup');}
  set textContent(value){this.replaceChildren();this._text=String(value);}
  get textContent(){return this._text+this.children.map(node=>node.textContent).join('');}
  appendChild(node){node.remove();node.parentNode=this;this.children.push(node);return node;}
  insertBefore(node,before){node.remove();const index=this.children.indexOf(before);if(index<0)throw new Error('Missing insertion target');node.parentNode=this;this.children.splice(index,0,node);return node;}
  replaceChildren(...nodes){this.children.forEach(node=>node.parentNode=null);this.children=[];this._text='';nodes.forEach(node=>this.appendChild(node));}
  remove(){if(this.parentNode){const parent=this.parentNode;parent.children=parent.children.filter(node=>node!==this);this.parentNode=null;}}
  setAttribute(name,value){this.attributes[name]=String(value);}
  getAttribute(name){return this.attributes[name]??null;}
  matches(selector){return selector.startsWith('.')?this.classList.contains(selector.slice(1)):this.tagName===selector.toUpperCase();}
  closest(selector){let node=this;while(node){if(node.matches(selector))return node;node=node.parentNode;}return null;}
  querySelectorAll(selector){return this.children.flatMap(node=>(node.matches(selector)?[node]:[]).concat(node.querySelectorAll(selector)));}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
  contains(target){return this===target||this.children.some(node=>node.contains(target));}
  addEventListener(type,listener){(this.listeners[type]??=[]).push(listener);}
  removeEventListener(type,listener){this.listeners[type]=(this.listeners[type]||[]).filter(value=>value!==listener);}
  emit(type,extra={}){const event={target:this,stopped:false,stopPropagation(){this.stopped=true;},...extra};let node=this;do{for(const listener of [...(node.listeners[type]||[])])listener(event);node=event.stopped?null:node.parentNode;}while(node);}
  get isConnected(){return this.ownerDocument?.body?.contains(this)||false;}
}
function setup(){const document={listeners:{}};document.createElement=tag=>new Element(tag,document);document.body=document.createElement('body');document.addEventListener=(type,handler)=>{(document.listeners[type]??=[]).push(handler);};
  document.emit=(type,extra={})=>(document.listeners[type]||[]).forEach(listener=>listener(extra));
  const scope={document,module:{exports:{}}};vm.runInNewContext(source,scope,{filename:'chat_table_cards.js'});
  const host=document.createElement('div');document.body.appendChild(host);return {document,host,api:scope.module.exports};}
function table(h,caption='',cell='123'){const node=h.document.createElement('table');if(caption){const cap=h.document.createElement('caption');cap.textContent=caption;node.appendChild(cap);}const row=h.document.createElement('tr'),data=h.document.createElement('td');data.textContent=cell;row.appendChild(data);node.appendChild(row);return node;}
const card=h=>h.host.querySelector('.chat-table-card');
const button=h=>h.host.querySelector('.chat-table-toggle');

test('mount expands the actual table without parsing or replacing source cells',()=>{
  const h=setup(),sourceTable=table(h,'Revenue <script>alert(1)</script>','<img src=x onerror=alert(1)>');h.host.appendChild(sourceTable);
  assert.equal(h.api.mount(h.host,{language:'en'}),1);
  assert.equal(card(h).classList.contains('is-expanded'),true);assert.equal(button(h).getAttribute('aria-expanded'),'true');
  assert.equal(button(h).textContent,'Collapse');assert.equal(h.host.querySelector('table'),sourceTable);
  assert.equal(h.host.querySelector('.chat-table-title').textContent,'Revenue <script>alert(1)</script>');
  assert.equal(sourceTable.querySelector('td').textContent,'<img src=x onerror=alert(1)>');
  assert.equal(h.host.querySelector('script'),null);assert.equal(h.host.querySelector('img'),null);
  assert.equal(button(h).getAttribute('aria-controls'),h.host.querySelector('.chat-table-viewport').id);
});

test('button and header clicks collapse and expand independently without double toggles',()=>{
  const h=setup();h.host.appendChild(table(h));h.api.mount(h.host);
  button(h).emit('click');assert.equal(card(h).classList.contains('is-expanded'),false);
  assert.equal(button(h).textContent,'Expand');assert.equal(button(h).getAttribute('aria-expanded'),'false');
  h.host.querySelector('.chat-table-head').emit('click');assert.equal(card(h).classList.contains('is-expanded'),true);
  assert.equal(button(h).textContent,'Collapse');
  h.host.querySelector('.chat-table-title').emit('click');assert.equal(card(h).classList.contains('is-expanded'),false);
});

test('mount is idempotent and tables have independent expansion state',()=>{
  const h=setup();h.host.appendChild(table(h,'First'));h.host.appendChild(table(h,'Second'));
  assert.equal(h.api.mount(h.host),2);assert.equal(h.api.mount(h.host),2);
  const cards=h.host.querySelectorAll('.chat-table-card'),buttons=h.host.querySelectorAll('.chat-table-toggle');
  assert.equal(cards.length,2);assert.equal(buttons[0].listeners.click.length,1);
  buttons[1].emit('click');assert.equal(cards[0].classList.contains('is-expanded'),true);assert.equal(cards[1].classList.contains('is-expanded'),false);
  buttons[0].emit('click');buttons[1].emit('click');assert.equal(cards[0].classList.contains('is-expanded'),false);assert.equal(cards[1].classList.contains('is-expanded'),true);
});

test('streaming Markdown replacement retains expansion and does not retain old listeners',()=>{
  const h=setup();h.host.appendChild(table(h,'Quarterly','1'));h.api.mount(h.host);
  const oldButton=button(h);oldButton.emit('click');
  const updated=table(h,'Quarterly','1234');h.host.replaceChildren(updated);h.api.mount(h.host);
  assert.equal(card(h).classList.contains('is-expanded'),false);assert.equal(button(h).textContent,'Expand');
  assert.equal(h.host.querySelector('td').textContent,'1234');assert.equal(oldButton.listeners.click.length,0);
  button(h).emit('click');h.host.replaceChildren(table(h,'Quarterly','5678'));h.api.mount(h.host);
  assert.equal(card(h).classList.contains('is-expanded'),true);
});

test('language switching changes system controls while preserving source captions and expanded state',()=>{
  const h=setup();h.host.appendChild(table(h,'Expand <设备>'));h.host.appendChild(table(h));h.api.mount(h.host,{language:'en'});
  h.host.querySelectorAll('.chat-table-toggle')[1].emit('click');h.document.emit('fos:languagechange',{detail:{language:'zh-hans'}});
  const titles=h.host.querySelectorAll('.chat-table-title'),buttons=h.host.querySelectorAll('.chat-table-toggle');
  assert.equal(titles[0].textContent,'Expand <设备>');assert.equal(titles[1].textContent,'表格');
  assert.equal(buttons[0].textContent,'收起');assert.equal(buttons[0].getAttribute('aria-label'),'收起表格');assert.equal(buttons[1].textContent,'展开');
  assert.equal(card(h).classList.contains('is-expanded'),true);
  h.document.emit('fos:languagechange',{detail:{language:'en'}});assert.equal(button(h).textContent,'Collapse');assert.equal(titles[1].textContent,'Table');
});

test('ancestor cleanup unwraps exact original tables and releases table control listeners',()=>{
  const h=setup(),child=h.document.createElement('div'),original=table(h,'Source');h.host.appendChild(child);child.appendChild(original);h.api.mount(child);
  const control=child.querySelector('.chat-table-toggle');h.api.destroy(h.host);
  assert.equal(child.querySelector('.chat-table-card'),null);assert.equal(child.querySelector('table'),original);assert.equal(control.listeners.click.length,0);
  h.document.emit('fos:languagechange',{detail:{language:'zh-hans'}});assert.equal(child.querySelector('.chat-table-card'),null);
  assert.equal(h.api.mount(child),1);assert.equal(child.querySelector('.chat-table-toggle').getAttribute('aria-expanded'),'true');
});

test('nested source tables remain intact inside one expanded outer card',()=>{
  const h=setup(),outer=table(h,'Outer'),nested=table(h,'Nested');outer.querySelector('td').appendChild(nested);h.host.appendChild(outer);
  assert.equal(h.api.mount(h.host),1);assert.equal(h.host.querySelectorAll('.chat-table-card').length,1);assert.equal(outer.querySelector('table'),nested);
});

test('compact and expanded CSS constrain the card, allow scrolling, and retain print output',()=>{
  const css=fs.readFileSync(path.join(__dirname,'../../static/css/chat_table_cards.css'),'utf8');
  assert.match(css,/\.chat-table-card\{[^}]*max-width:340px/);assert.match(css,/\.chat-table-card\.is-expanded\{[^}]*max-width:none;[^}]*width:100%/);
  assert.match(css,/\.chat-table-viewport\{[^}]*max-height:220px;overflow:auto/);assert.match(css,/@media print/);
});

function imageGroup(h,filename='plot.png') {
  const group=h.document.createElement('details');group.className='chat-chart-images';
  const figure=h.document.createElement('figure'),image=h.document.createElement('img');image.alt=filename;image.src='blob:trusted-generated';figure.appendChild(image);group.appendChild(figure);
  return {group,figure,image};
}

test('only generated plot images gain compact cards; source image, filename and other photos remain intact',()=>{
  const h=setup(),generated=imageGroup(h,'plot <source>.png'),photo=h.document.createElement('img'),citations=h.document.createElement('div'),citationImage=h.document.createElement('img');
  citations.className='cite-imgs';citations.appendChild(citationImage);h.host.appendChild(photo);h.host.appendChild(citations);h.host.appendChild(generated.group);
  assert.equal(h.api.mountImages(h.host,{language:'en'}),1);
  const wrapper=h.host.querySelector('.chat-image-card'),control=wrapper.querySelector('.chat-table-toggle');
  assert.equal(wrapper.classList.contains('is-expanded'),false);assert.equal(control.getAttribute('aria-expanded'),'false');assert.equal(control.getAttribute('aria-label'),'Expand Image');
  assert.equal(wrapper.querySelector('img'),generated.image);assert.equal(generated.image.src,'blob:trusted-generated');assert.equal(generated.image.alt,'plot <source>.png');
  assert.equal(wrapper.querySelector('.chat-table-title').textContent,'plot <source>.png');assert.equal(photo.parentNode,h.host);assert.equal(citationImage.parentNode,citations);
  generated.image.emit('click');assert.equal(wrapper.classList.contains('is-expanded'),true);assert.equal(control.textContent,'Collapse');
  control.emit('click');assert.equal(wrapper.classList.contains('is-expanded'),false);
});

test('image and table cards coexist idempotently and image expansion survives replacement',()=>{
  const h=setup(),generated=imageGroup(h);h.host.appendChild(table(h,'Data'));h.host.appendChild(generated.group);
  assert.equal(h.api.mount(h.host),1);assert.equal(h.api.mountImages(h.host),1);assert.equal(h.api.mount(h.host),1);assert.equal(h.api.mountImages(h.host),1);
  assert.equal(h.host.querySelectorAll('.chat-table-card').length,2);
  const imageCard=h.host.querySelector('.chat-image-card'),oldControl=imageCard.querySelector('.chat-table-toggle');oldControl.emit('click');
  h.host.querySelector('.chat-table-card').querySelector('.chat-table-toggle').emit('click');
  assert.equal(imageCard.classList.contains('is-expanded'),true);
  const replacement=imageGroup(h);generated.group.replaceChildren(replacement.figure);h.api.mountImages(h.host);
  assert.equal(h.host.querySelector('.chat-image-card').classList.contains('is-expanded'),true);assert.equal(oldControl.listeners.click.length,0);assert.equal(generated.image.listeners.click.length,0);
  assert.equal(h.host.querySelectorAll('.chat-table-card').length,2);
  const plainTableCard=h.host.querySelectorAll('.chat-table-card').find(node=>!node.classList.contains('chat-image-card'));
  assert.equal(plainTableCard.classList.contains('is-expanded'),false);
});

test('image controls localize without rewriting filenames and cleanup restores source figures',()=>{
  const h=setup(),generated=imageGroup(h,'source.png'),anonymous=imageGroup(h,'');h.host.appendChild(generated.group);h.host.appendChild(anonymous.group);h.api.mountImages(h.host);
  h.document.emit('fos:languagechange',{detail:{language:'zh-hans'}});
  const imageCard=h.host.querySelector('.chat-image-card'),control=imageCard.querySelector('.chat-table-toggle');
  assert.equal(control.textContent,'展开');assert.equal(control.getAttribute('aria-label'),'展开图片');assert.equal(imageCard.querySelector('.chat-table-title').textContent,'source.png');
  assert.equal(h.host.querySelectorAll('.chat-table-title')[1].textContent,'图片');
  imageCard.querySelector('.chat-table-head').emit('click');assert.equal(control.getAttribute('aria-label'),'收起图片');
  h.api.destroy(h.host);assert.equal(h.host.querySelector('.chat-image-card'),null);assert.equal(generated.figure.parentNode,generated.group);
  assert.equal(generated.image.listeners.click.length,0);assert.equal(control.listeners.click.length,0);assert.equal(h.host.querySelectorAll('img').length,2);
});
