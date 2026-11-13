const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag, attrs={}) { this.tagName=tag;this.attrs=attrs;this.children=[];this.dataset={};this.value='';this.events={};this.hidden=false;this.disabled=false; }
  matches(selector) {
    if(selector==='input')return this.tagName==='input';
    const match=selector.match(/^\[([^=\]]+)(?:=([^\]]+))?\]$/);
    return !!match&&Object.hasOwn(this.attrs,match[1])&&(match[2]===undefined||this.attrs[match[1]]===match[2]);
  }
  querySelectorAll(selector) { return this.children.flatMap(child=>[...(child.matches(selector)?[child]:[]),...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0]||null; }
  closest(selector) { return this.matches(selector)?this:this.parent?.closest(selector)||null; }
  append(...children) { for(const child of children){if(child.fragment){this.append(...child.children.slice());continue;}child.remove();child.parent=this;this.children.push(child);} }
  replaceChildren(...children) { for(const child of this.children)child.parent=null;this.children=[];this.append(...children); }
  remove() { if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);this.parent=null; }
  setAttribute(key,value) { this.attrs[key]=value; }
  addEventListener(name,handler) { (this.events[name]||=[]).push(handler); }
  click() { if(!this.disabled)for(const handler of this.events.click||[])handler(); }
}
const el=(tag,key)=>new Element(tag,key?{[key]:''}:{});
function widget(multiple=true) {
  const root=el('section','data-auth-widget'),type=el('select','data-auth-type');
  type.value='none';type.append(new Element('option',{value:'saved'}));root.append(type);
  for(const kind of ['bearer','api_key','basic login','headers',...(multiple?['multiple']:[])]){
    const panel=el('div','data-auth-panel');panel.dataset.authPanel=kind;root.append(panel);
    const inputs={bearer:['token'],api_key:['key-name','key-value'],'basic login':['username','password']}[kind]||[];
    for(const key of inputs)panel.append(el('input','data-auth-'+key));
    if(kind==='headers')panel.append(el('div','data-auth-header-list'),el('button','data-auth-add'));
    if(kind==='multiple'){
      panel.append(el('div','data-auth-profile-list'),el('button','data-auth-add-profile'));
      const template=el('template','data-auth-profile-template');
      template.content={cloneNode(){
        const fragment=el('fragment');fragment.fragment=true;
        const row=el('fieldset');row.append(el('input','data-auth-profile-name'),el('input','data-auth-profile-url'),widget(false),el('button','data-auth-remove-profile'));
        fragment.append(row);fragment.firstElementChild=row;return fragment;
      }};panel.append(template);
    }
  }
  root.append(el('p','data-auth-saved'));return root;
}
function setup(){
  const window={},document={createElement:tag=>el(tag)};
  vm.runInNewContext(fs.readFileSync(require.resolve('../../static/js/feed_auth.js'),'utf8'),{window,document});
  const root=widget();return {root,form:new window.FeedAuthForm(root)};
}
const plain=value=>JSON.parse(JSON.stringify(value));
const saved={configured:true,managed:true,type:'multiple',label:'多组访问凭据',headers:['Authorization'],profiles:[
  {name:'api',url:'https://api.example.test',configured:true,type:'bearer',label:'Bearer Token',headers:['Authorization']},
  {name:'portal',url:'https://portal.example.test',configured:true,type:'login',label:'网页登录账号与密码',headers:[]}
]};

test('saved profile groups keep names and destinations while rotating only one credential',()=>{
  const {form}=setup();form.load(saved);assert.equal(form.type.value,'saved');assert.equal(form.profiles.length,2);
  form.type.value='multiple';form.render();
  assert.deepEqual(plain(form.read()),{type:'multiple',profiles:saved.profiles.map(profile=>({name:profile.name,url:profile.url,authentication:{type:'saved'}}))});
  const portal=form.profiles[1].auth;portal.type.value='login';portal.render();
  portal.root.querySelector('[data-auth-username]').value='new-user';portal.root.querySelector('[data-auth-password]').value='new-secret';
  assert.deepEqual(plain(form.read()).profiles[0].authentication,{type:'saved'});
  assert.deepEqual(plain(form.read()).profiles[1].authentication,{type:'login',username:'new-user',password:'new-secret'});
  form.load(saved);assert.equal(form.profiles[1].auth.root.querySelector('[data-auth-password]').value,'');
});

test('adding and removing groups enforces the eight-credential limit and leaves nested panels intact',()=>{
  const {form}=setup();form.type.value='multiple';form.render();
  for(let i=0;i<10;i++)form.profileAdd.click();assert.equal(form.profiles.length,8);assert.equal(form.profileAdd.disabled,true);
  const last=form.profiles[7];last.row.querySelector('[data-auth-remove-profile]').click();assert.equal(form.profiles.length,7);assert.equal(form.profileAdd.disabled,false);
  form.profileAdd.click();assert.equal(form.profiles.length,8);
  assert.equal(new Set(form.profiles.map(profile=>profile.name.value)).size,8);
  assert.equal(form.profiles[0].auth.root.querySelector('[data-auth-panel]').hidden,false);
});

test('single saved credentials can be retained as the primary group when adding a service',()=>{
  const {form}=setup();form.load({configured:true,managed:true,type:'bearer',label:'Bearer Token',headers:['Authorization'],url:'https://api.example.test'});
  form.type.value='multiple';form.addProfile();
  const result=plain(form.read());assert.equal(result.profiles[0].name,'primary');
  assert.deepEqual(result.profiles[0].authentication,{type:'saved'});
  assert.equal(result.profiles[1].authentication.type,'bearer');
});
