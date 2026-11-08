const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const controls=require('../../static/js/feed_report_controls.js');
const zone='Asia/Shanghai';

test('daily weekly and hourly presets round trip without changing schedule meaning',()=>{
 for(const cron of ['30 9 * * *','15 23 * * 5','0 8 * * sun','10 * * * *','45 */3 * * *']){
  const value=controls.parseSchedule({mode:'cron',cron,timezone:zone}),schedule=controls.buildSchedule(value);
  assert.equal(schedule.timezone,zone);assert.equal(schedule.mode,'cron');
  assert.equal(schedule.cron,cron.replace('sun','0'));
 }
});
test('complex cron expressions remain custom rather than being silently simplified',()=>{
 for(const cron of ['*/15 8-17 * * mon-fri','0 9 1 * *','0 */5 * * *','5,35 9 * * *']){
  const value=controls.parseSchedule({mode:'cron',cron,timezone:zone});assert.equal(value.frequency,'custom');assert.equal(controls.buildSchedule(value).cron,cron);
 }
});
test('on-demand and legacy schedules remain distinct and invalid preset fields fail',()=>{
 for(const mode of ['manual','after_collection'])assert.deepEqual(controls.buildSchedule(controls.parseSchedule({mode,timezone:zone})),{mode,timezone:zone});
 for(const value of [{frequency:'daily',time:'25:00'},{frequency:'weekly',time:'09:00',weekday:'8'},{frequency:'hourly',interval:'5',minute:'0'},{frequency:'hourly',interval:'3',minute:'60'},{frequency:'custom',cron:'0 9 * *'}])assert.throws(()=>controls.buildSchedule({...value,timezone:zone}));
});

function setup(){
 const make=()=>({dataset:{},children:[],events:{},value:'',append(...nodes){this.children.push(...nodes);},addEventListener(type,fn){this.events[type]=fn;},focus(){},querySelectorAll(){return [];}});
 const frequency=make(),legacy=make();frequency.querySelector=()=>legacy;
 const form=make();form.elements={frequency};for(const name of ['time','weekday','interval','minute','cron','timezone'])form.elements[name]=make();form.reset=()=>{};
 const dialog=make();dialog.showModal=()=>dialog.open=true;dialog.close=()=>{dialog.open=false;dialog.events.close?.();};
 const error=make(),save=make(),title=make(),list=make(),message=make();let refreshes=0;
 const feed={id:'feed',can_edit:true,timezone:zone,report:{has_report:true,can_edit:true,enabled:true,configured:true,kind:'teams',title:'Report',control_url:'/feeds/feed/report/schedule/',schedule:{mode:'cron',cron:'0 9 * * *',timezone:zone}}};
 const calls=[],elements={feedReportEditor:dialog,feedReportForm:form,feedReportError:error,feedReportSave:save,feedReportTitle:title};
 const document={getElementById:id=>elements[id],createElement:make};
 const scope={document,AbortController,setTimeout,clearTimeout,FormData:class{constructor(){this.values=Object.entries(form.elements).map(([key,input])=>[key,input.value]);} [Symbol.iterator](){return this.values[Symbol.iterator]();}},fetch:async(url,options)=>{calls.push({url,options});return {ok:true,headers:{get:()=> 'application/json'},json:async()=>({ok:true,report:feed.report,message:'Saved'})};}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/js/feed_report_controls.js'),'utf8'),scope);
 scope.FeedReportControls.init({list,message,getFeed:id=>id==='feed'?feed:null,csrf:()=> 'csrf',refresh:async()=>refreshes++});
 const click=action=>list.events.click({target:{closest:()=>({dataset:{reportAction:action,feedId:'feed'},disabled:false})}});
 return {feed,form,dialog,error,calls,click,render:()=>scope.FeedReportControls.render(feed),refreshes:()=>refreshes};
}
function buttons(node){return [node,...node.children.flatMap(buttons)].filter(node=>node.dataset.reportAction);}

test('feed cards show report pause/resume and frequency controls only for the creator',()=>{
 const h=setup();assert.deepEqual(buttons(h.render()).map(node=>node.dataset.reportAction),['pause','edit']);
 h.feed.report.enabled=false;assert.deepEqual(buttons(h.render()).map(node=>node.dataset.reportAction),['resume','edit']);
 h.feed.can_edit=false;assert.equal(buttons(h.render()).length,0);
});
test('pause button calls only the report endpoint and refreshes the feed state',async()=>{
 const h=setup();await h.click('pause');assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,h.feed.report.control_url);
 assert.deepEqual(JSON.parse(h.calls[0].options.body),{action:'pause'});assert.equal(h.calls[0].options.headers['X-CSRFToken'],'csrf');assert.equal(h.refreshes(),1);
});
test('frequency editor submits an independent weekly schedule without collection fields',async()=>{
 const h=setup();await h.click('edit');assert.equal(h.dialog.open,true);
 Object.assign(h.form.elements.frequency,{value:'weekly'});h.form.elements.weekday.value='5';h.form.elements.time.value='16:30';h.form.elements.timezone.value='America/New_York';
 await h.form.events.submit({preventDefault(){}});
 assert.deepEqual(JSON.parse(h.calls[0].options.body),{action:'update_schedule',schedule:{mode:'cron',cron:'30 16 * * 5',timezone:'America/New_York'}});
 assert.equal(h.dialog.open,false);assert.equal(h.refreshes(),1);
});

test('saved workflow links without a confirmed report show setup guidance rather than fake schedule controls',()=>{
 const h=setup();h.feed.report.has_report=false;h.feed.report.saved_workflow_types=['teams'];h.feed.ai_edit_url='/kb/feeds/builder/?feed=feed';
 const section=h.render();assert.ok(section);assert.equal(buttons(section).length,0);
 assert.ok(section.children.some(node=>node.href===h.feed.ai_edit_url));
});
