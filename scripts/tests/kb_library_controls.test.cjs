const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/kb_access.js'),'utf8');

class Node {
  constructor(tag='',text='') {
    this.tagName=tag.toUpperCase();this.nodeType=tag?1:3;this.childNodes=[];this.parentNode=null;
    this.attributes={};this.events={};this.eventOptions={};this.className='';this.id='';this.hidden=false;this.open=false;
    this.value='';this._text=text;
    this.classList={add:(...names)=>{this.className=[...new Set([...this.className.split(/\s+/).filter(Boolean),...names])].join(' ')},contains:name=>this.className.split(/\s+/).includes(name)};
  }
  get firstChild(){return this.childNodes[0]||null}
  get textContent(){return this.nodeType===3?this._text:this.childNodes.map(n=>n.textContent).join('')}
  set textContent(value){this.childNodes=[];if(value)this.appendChild(new Node('',String(value)))}
  appendChild(node){node.remove();node.parentNode=this;this.childNodes.push(node);return node}
  append(...nodes){nodes.forEach(n=>this.appendChild(typeof n==='string'?new Node('',n):n))}
  remove(){if(this.parentNode){const children=this.parentNode.childNodes;children.splice(children.indexOf(this),1);this.parentNode=null}}
  replaceWith(node){const parent=this.parentNode,index=parent.childNodes.indexOf(this);this.remove();node.remove();node.parentNode=parent;parent.childNodes.splice(index,0,node)}
  setAttribute(name,value){this.attributes[name]=String(value);if(name==='id')this.id=String(value);if(name==='class')this.className=String(value)}
  getAttribute(name){return this.attributes[name]??null}
  addEventListener(type,fn,options){(this.events[type]??=[]).push(fn);(this.eventOptions[type]??=[]).push(options)}
  dispatch(type){for(const handler of this.events[type]||[])handler({type,target:this})}
  cloneNode(deep){const result=new Node(this.tagName);Object.assign(result,{attributes:{...this.attributes},id:this.id,className:this.className,value:this.value,name:this.name,type:this.type});if(deep)this.childNodes.forEach(n=>result.appendChild(n.nodeType===3?new Node('',n.textContent):n.cloneNode(true)));return result}
  matches(selector){
    if(selector.startsWith('.'))return this.classList.contains(selector.slice(1));
    const attr=selector.match(/^\[([^=\]]+)(?:=([^\]]+))?\]$/);
    if(attr)return attr[2]===undefined?Object.hasOwn(this.attributes,attr[1]):(this.attributes[attr[1]]??this[attr[1]])===attr[2];
    return this.tagName===selector.toUpperCase();
  }
  closest(selector){for(let node=this;node;node=node.parentNode)if(node.matches(selector))return node;return null}
  descendants(){return this.childNodes.flatMap(n=>[n,...n.descendants()])}
  querySelectorAll(selector){
    if(selector.startsWith(':scope > '))return this.childNodes.filter(n=>n.matches(selector.slice(9)));
    const groups=selector.split(',');
    return this.descendants().filter(node=>node.nodeType===1&&groups.some(group=>{
      group=group.trim();
      if(group==='.public-list > li')return node.tagName==='LI'&&node.parentNode.matches('.public-list');
      if(group==='.page-head-main h1')return node.tagName==='H1'&&Boolean(node.parentNode.closest('.page-head-main'));
      return node.matches(group);
    }));
  }
  querySelector(selector){return this.querySelectorAll(selector)[0]||null}
}
function element(tag,attrs={},text=''){const node=new Node(tag);for(const [name,value]of Object.entries(attrs))node.setAttribute(name,value);if(text)node.textContent=text;return node}
function environment({editor=false,opener=true,csrf=true,pathname='/kb/manage/rides/',stored=null,storageError=false}={}){
  const document=new Node('document');document.readyState='complete';document.body=element('body');document.appendChild(document.body);
  document.createElement=tag=>element(tag);document.getElementById=id=>document.descendants().find(n=>n.id===id)||null;
  const store=new Map(stored===null?[]:[['fos-library-sections',stored]]);
  const localStorage={getItem(key){if(storageError)throw Error('blocked');return store.get(key)||null},setItem(key,value){if(storageError)throw Error('blocked');store.set(key,value)}};
  const window={document,location:{pathname},localStorage,FOSI18n:{t:text=>text}};
  const fixture={document,window,store};
  if(editor){
    const head=element('header',{class:'page-head-main'}),heading=element('h1',{},'Saved library name');head.appendChild(heading);document.body.appendChild(head);
    if(opener)document.body.appendChild(element('button',{id:'descEditBtn'}));
    const dialog=element('dialog'),form=element('form',{id:'descForm'}),name=element('input',{name:'name'});name.value='Unsaved edit';
    form.appendChild(name);if(csrf){const token=element('input',{name:'csrfmiddlewaretoken',type:'hidden'});token.value='csrf-secret';form.appendChild(token)}
    const actions=element('div',{class:'dialog-actions'}),cancel=element('button',{id:'cancel'},'Cancel'),save=element('button',{id:'save'},'Save');
    actions.append(cancel,save);form.appendChild(actions);dialog.appendChild(form);document.body.appendChild(dialog);
    Object.assign(fixture,{dialog,form,actions,heading,name,cancel,save});
  }
  fixture.load=()=>vm.runInNewContext(source,{window,document});
  return fixture;
}
function section(id='kbManualSection'){
  const root=element('section',{id,'data-kb-category':''}),bar=element('div',{class:'section-bar'}),copy=element('div'),heading=element('h2',{},'Manual Libraries'),note=element('p',{},'Upload documents.');
  copy.append(heading,note);bar.appendChild(copy);
  const grid=element('div',{class:'kb-list-grid'}),card=element('article',{class:'kb-list-card'});grid.appendChild(card);root.append(bar,grid);
  return {root,heading,note,grid,card};
}

test('delete belongs to its own POST form, with CSRF, outside the required edit fields',()=>{
  const h=environment({editor:true,pathname:'/prefix/kb/manage/rides/'});h.load();
  const deletion=h.document.getElementById('kbDeleteForm'),button=h.document.querySelector('.kb-edit-delete');
  assert.equal(deletion.parentNode,h.dialog);assert.notEqual(deletion,h.form);assert.equal(deletion.method,'post');
  assert.equal(deletion.action,'/prefix/kb/manage/rides/delete/');assert.equal(deletion.hidden,true);
  assert.equal(deletion.querySelector('[name=csrfmiddlewaretoken]').value,'csrf-secret');
  assert.equal(button.getAttribute('form'),deletion.id);assert.equal(button.type,'submit');
  assert.ok(deletion.getAttribute('data-confirm').includes('Saved library name'));
  assert.ok(!deletion.getAttribute('data-confirm').includes('Unsaved edit'));
  assert.equal(h.cancel.closest('form'),h.form);assert.equal(h.save.closest('form'),h.form);
});
test('delete controls stay absent for read-only views, missing CSRF, and non-library routes',()=>{
  for(const options of [{opener:false},{csrf:false},{pathname:'/accounts/settings/'}]){
    const h=environment({editor:true,...options});h.load();assert.equal(h.document.getElementById('kbDeleteForm'),null);assert.equal(h.document.querySelector('.kb-edit-delete'),null);
  }
});
test('confirmation follows the saved heading after rename, and repeated initialization stays idempotent',()=>{
  const h=environment({editor:true});h.load();h.window.KBAccessFields.initDelete();
  assert.equal(h.document.querySelectorAll('.kb-edit-delete').length,1);
  h.heading.textContent='Renamed library';h.document.querySelector('.kb-edit-delete').dispatch('click');
  assert.ok(h.document.getElementById('kbDeleteForm').getAttribute('data-confirm').includes('Renamed library'));
  assert.equal(h.cancel.events.click,undefined);
});
test('sections become native details without replacing their cards or action listeners',()=>{
  const h=environment(),s=section();let clicks=0;s.card.addEventListener('click',()=>clicks++);h.document.body.appendChild(s.root);h.load();
  const panel=s.root.querySelector(':scope > .kb-library-panel');assert.equal(panel.tagName,'DETAILS');assert.equal(panel.open,true);
  assert.equal(panel.querySelector('summary').querySelector('h2'),s.heading);
  assert.equal(panel.querySelector('.kb-list-card'),s.card);assert.ok(panel.textContent.includes('Upload documents.'));
  assert.equal(panel.querySelector('.count-chip').textContent,'1 个');s.card.dispatch('click');assert.equal(clicks,1);
  h.window.KBAccessFields.initSections();assert.equal(s.root.querySelectorAll('details').length,1);
});
test('fold state survives pin/create AJAX section replacement',()=>{
  const h=environment(),s=section();h.document.body.appendChild(s.root);h.load();
  const panel=s.root.querySelector('details');panel.open=false;panel.dispatch('toggle');
  assert.equal(JSON.parse(h.store.get('fos-library-sections')).kbManualSection,false);
  const fresh=section();s.root.replaceWith(fresh.root);h.document.dispatch('kb-libraries-changed');
  assert.equal(fresh.root.querySelector('details').open,false);assert.equal(fresh.root.querySelectorAll('details').length,1);
});
test('saved states are section-specific, while blocked or malformed storage does not break the page',()=>{
  const h=environment({stored:'{"kbManualSection":false}'}),manual=section(),favorite=section('kbFavoriteSection');
  h.document.body.append(manual.root,favorite.root);h.load();assert.equal(manual.root.querySelector('details').open,false);assert.equal(favorite.root.querySelector('details').open,true);
  for(const options of [{stored:'bad JSON'},{stored:'[]'},{stored:'null'},{storageError:true}]){
    const page=environment(options),s=section();page.document.body.appendChild(s.root);page.load();assert.equal(s.root.querySelector('details').open,true);
    s.root.querySelector('details').dispatch('toggle');
  }
});

function compactAnalysis(){
  const page=environment(),document=page.document;
  const root=element('section',{id:'feedKbAggregate'}),header=element('div',{class:'fkd-analysis-heading'}),form=element('form',{id:'feedKbAnalysisForm'});
  const modes=element('div',{class:'fkd-mode-switch'}),fields=element('div'),footer=element('div',{class:'fkd-analysis-footer'});
  const mode=element('input',{id:'feedKbAnalysisMode'}),from=element('input',{id:'feedKbAnalysisFrom'}),measure=element('select',{id:'feedKbMeasure'});
  mode.value='trend';from.value='2026-11-01T00:00';measure.value='Wait time';
  for(const input of [mode,from,measure])input.disabled=false;
  fields.append(mode,from,measure);
  const apply=element('button',{id:'feedKbApply'}),exportButton=element('button',{id:'feedKbExport'});exportButton.disabled=true;
  footer.append(exportButton,apply);form.append(modes,fields,footer);root.append(header,form);document.body.appendChild(root);
  const analysisSource=fs.readFileSync(path.join(__dirname,'../../static/js/feed_kb_detail.js'),'utf8');
  const functions=analysisSource.slice(analysisSource.indexOf('  function setAnalysisControls('),analysisSource.indexOf('  function updateApplyState()'));
  const popup={open:true,calls:0};
  const scope={document,$:id=>document.getElementById(id),uiText:text=>text,
    element:(tag,className,text)=>element(tag,{class:className},text),
    setObjectOpen(open){popup.open=open;popup.calls++}};
  vm.createContext(scope);vm.runInContext(functions,scope);
  return {...page,scope,root,header,form,modes,fields,footer,mode,from,measure,apply,exportButton,popup,
    mount:()=>vm.runInContext('initCompactAnalysis()',scope)};
}

test('compact analysis mounts once, preserving backing values and listeners while placing Export in the header',()=>{
  const h=compactAnalysis();let applications=0;h.apply.addEventListener('click',()=>applications++);h.mount();h.mount();
  const toolbar=h.document.getElementById('feedKbAnalysisToolbar'),controls=h.document.getElementById('feedKbAnalysisControls');
  assert.equal(h.document.querySelectorAll('.fkd-analysis-toolbar').length,1);assert.equal(controls.hidden,true);
  assert.equal(h.modes.parentNode,toolbar);assert.equal(h.apply.parentNode,toolbar);
  assert.equal(h.fields.parentNode,controls);assert.equal(h.footer.parentNode,null);
  assert.equal(h.exportButton.parentNode,h.header);assert.equal(h.exportButton.disabled,true);
  for(const [input,value] of [[h.mode,'trend'],[h.from,'2026-11-01T00:00'],[h.measure,'Wait time']]){
    assert.equal(input.closest('form'),h.form);assert.equal(input.value,value);assert.equal(input.disabled,false);
  }
  h.apply.dispatch('click');assert.equal(applications,1);
});

test('compact analysis toggle and validation reveal settings without disabling inputs or leaving its object picker open',()=>{
  const h=compactAnalysis();h.mount();
  const toggle=h.document.getElementById('feedKbAnalysisControlsToggle'),controls=h.document.getElementById('feedKbAnalysisControls');
  assert.equal(toggle.getAttribute('aria-expanded'),'false');
  toggle.dispatch('click');assert.equal(controls.hidden,false);assert.equal(toggle.getAttribute('aria-expanded'),'true');
  toggle.dispatch('click');assert.equal(controls.hidden,true);assert.equal(h.popup.open,false);assert.equal(h.popup.calls,1);
  assert.equal(h.form.eventOptions.invalid[0],true);h.form.dispatch('invalid');
  assert.equal(controls.hidden,false);assert.equal(toggle.getAttribute('aria-expanded'),'true');
  assert.equal(h.from.value,'2026-11-01T00:00');assert.equal(h.from.disabled,false);assert.equal(h.measure.disabled,false);
});
