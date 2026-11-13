const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/feeds.js'),'utf8');

function fixture(){
 const nodes=new Map(),node=()=>({value:'',textContent:'',checked:false,disabled:false,events:{},focus(){},addEventListener(name,handler){this.events[name]=handler;}});
 const form={elements:{},events:{},reset(){this.elements.html_attachments_supported.checked=false;},addEventListener(name,handler){this.events[name]=handler;}};
 for(const name of ['id','kb_name','mode','url','cron','timezone','write_mode','kb','html_attachments_supported','email_workflow_url','teams_workflow_url'])form.elements[name]=node();
 form.elements.kb.options=[{value:'fixture',textContent:'Library'}];
 const document={getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);}};
 let closes=0,refreshes=0;const calls=[];
 const scope={document,form,error:node(),message:node(),auth:{load(){},read:()=>({mode:'saved'})},filter:{value:'fixture',selectedOptions:[{value:'fixture',textContent:'Library'}],options:[{value:'fixture',textContent:'Library'}]},
  editor:{showModal(){},close(){closes++;}},fields(){},refresh:async()=>refreshes++,
  post:async data=>{calls.push(data);return {message:'Saved',feed:{kb:'fixture',kb_name:'Library',authentication:{}}};},
  FormData:class{constructor(){this.values=[['id','feed-id'],['kb','fixture'],['email_workflow_url',''],['teams_workflow_url','']];if(form.elements.html_attachments_supported.checked)this.values.push(['html_attachments_supported','on']);}[Symbol.iterator](){return this.values[Symbol.iterator]();}}};
 vm.createContext(scope);
 vm.runInContext(source.slice(source.indexOf(' function edit(feed){'),source.indexOf(" form.elements.kb.addEventListener('change'"))+'\nglobalThis.openFeed=edit;',scope);
 vm.runInContext(source.slice(source.indexOf(" form.addEventListener('submit',async event=>{"),source.indexOf(" list.addEventListener('click'")),scope);
 vm.runInContext(source.slice(source.indexOf(" form.elements.email_workflow_url.addEventListener"),source.indexOf(" filter.addEventListener('change'")),scope);
 return {scope,form,calls,open:feed=>scope.openFeed(feed),submit:()=>form.events.submit({preventDefault(){}}),closes:()=>closes,refreshes:()=>refreshes};
}

test('workflow capability hydrates from saved settings and resets for an unconfigured or new workflow',()=>{
 const h=fixture(),feed={id:'feed-id',kb_name:'Library',workflow_settings:{saved_workflow_types:['email'],html_attachments_supported:true}};
 h.open(feed);assert.equal(h.form.elements.html_attachments_supported.checked,true);
 h.open({...feed,workflow_settings:{saved_workflow_types:['email']}});assert.equal(h.form.elements.html_attachments_supported.checked,false);
 h.form.elements.html_attachments_supported.checked=true;h.open();assert.equal(h.form.elements.html_attachments_supported.checked,false);
});

test('workflow settings explicitly send both true and false booleans while blank stored URLs remain blank',async()=>{
 for(const supported of [true,false]){
  const h=fixture();h.form.elements.html_attachments_supported.checked=supported;await h.submit();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].html_attachments_supported,supported);assert.equal(typeof h.calls[0].html_attachments_supported,'boolean');
  assert.equal(h.calls[0].email_workflow_url,'');assert.equal(h.calls[0].teams_workflow_url,'');assert.equal(h.calls[0].action,'update');
  assert.equal(h.closes(),1);assert.equal(h.refreshes(),1);
 }
});

test('editing an email workflow URL requires attachment mapping to be confirmed again',()=>{
 const h=fixture();h.open({id:'feed-id',kb_name:'Library',workflow_settings:{saved_workflow_types:['email'],html_attachments_supported:true}});
 assert.equal(h.form.elements.html_attachments_supported.checked,true);
 h.form.elements.email_workflow_url.value='https://workflow.example/new';h.form.elements.email_workflow_url.events.input();
 assert.equal(h.form.elements.html_attachments_supported.checked,false);
});
