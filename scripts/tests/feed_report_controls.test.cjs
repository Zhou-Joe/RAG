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
 const form=make();form.elements={frequency};for(const name of ['time','weekday','interval','minute','cron','timezone','digest_enabled','digest_instructions','digest_language','digest_source_links','digest_email_format'])form.elements[name]=make();form.elements.digest_enabled.checked=false;form.elements.digest_source_links.checked=false;form.reset=()=>{};
 const attachmentOption=make();form.elements.digest_email_format.querySelector=()=>attachmentOption;
 const labels={};form.querySelector=selector=>labels[selector]||(labels[selector]=make());
 const dialog=make();dialog.showModal=()=>dialog.open=true;dialog.close=()=>{dialog.open=false;dialog.events.close?.();};
 const error=make(),save=make(),title=make(),list=make(),message=make(),digestFields=make(),emailFields=make(),attachmentWarning=make();digestFields.querySelectorAll=()=>[form.elements.digest_instructions,form.elements.digest_language,form.elements.digest_source_links,form.elements.digest_email_format];let refreshes=0;
 form.querySelector('[data-report-schedule-fields]').querySelectorAll=()=>['frequency','time','weekday','interval','minute','cron','timezone'].map(key=>form.elements[key]);
 const feed={id:'feed',can_edit:true,timezone:zone,report:{has_report:true,can_edit:true,enabled:true,configured:true,kind:'teams',title:'Report',control_url:'/feeds/feed/report/schedule/',schedule:{mode:'cron',cron:'0 9 * * *',timezone:zone}}};
 const calls=[],elements={feedReportEditor:dialog,feedReportForm:form,feedReportError:error,feedReportSave:save,feedReportTitle:title,feedReportDigestFields:digestFields,feedReportDigestEmailFields:emailFields,feedReportDigestAttachmentWarning:attachmentWarning};
 const document={getElementById:id=>elements[id],createElement:make};
 const scope={document,AbortController,setTimeout,clearTimeout,FormData:class{constructor(){this.values=Object.entries(form.elements).map(([key,input])=>[key,input.value]);} [Symbol.iterator](){return this.values[Symbol.iterator]();}},fetch:async(url,options)=>{calls.push({url,options});return {ok:true,headers:{get:()=> 'application/json'},json:async()=>({ok:true,report:feed.report,message:'Saved'})};}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/js/feed_report_controls.js'),'utf8'),scope);
 scope.FeedReportControls.init({list,message,getFeed:id=>id==='feed'?feed:null,csrf:()=> 'csrf',refresh:async()=>refreshes++});
 const click=action=>list.events.click({target:{closest:()=>({dataset:{reportAction:action,feedId:'feed'},disabled:false})}});
 return {feed,form,dialog,error,calls,click,labels,digestFields,emailFields,attachmentWarning,attachmentOption,render:()=>scope.FeedReportControls.render(feed),refreshes:()=>refreshes};
}
function buttons(node){return [node,...node.children.flatMap(buttons)].filter(node=>node.dataset.reportAction);}

test('feed cards show report pause/resume and frequency controls only for the creator',()=>{
 const h=setup();assert.deepEqual(buttons(h.render()).map(node=>node.dataset.reportAction),['pause','edit','digest']);
 h.feed.report.enabled=false;assert.deepEqual(buttons(h.render()).map(node=>node.dataset.reportAction),['resume','edit','digest']);
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
 assert.deepEqual(JSON.parse(h.calls[0].options.body),{action:'update_schedule',schedule:{mode:'cron',cron:'30 16 * * 5',timezone:'America/New_York'},digest:{enabled:false,instructions:'',language:'en',include_source_links:false,email_format:'body'}});
 assert.equal(h.dialog.open,false);assert.equal(h.refreshes(),1);
});

test('saved workflow links without a confirmed report show setup guidance rather than fake schedule controls',()=>{
 const h=setup();h.feed.report.has_report=false;h.feed.report.saved_workflow_types=['teams'];h.feed.ai_edit_url='/kb/feeds/builder/?feed=feed';
 const section=h.render();assert.ok(section);assert.equal(buttons(section).length,0);
 assert.ok(section.children.some(node=>node.href===h.feed.ai_edit_url));
});

test('conditional report frequency editor describes checks and preserves conditions on save',async()=>{
 const h=setup();h.feed.report.trigger={condition:{metric:'row_count',op:'gte',value:1}};
 assert.equal(buttons(h.render()).find(node=>node.dataset.reportAction==='edit').textContent,'调整检查频率');
 await h.click('edit');assert.equal(h.labels.h2.textContent,'调整条件检查频率');
 assert.equal(h.labels['label[for="feedReportTime"]'].textContent,'检查时间');
 h.form.elements.frequency.value='hourly';h.form.elements.interval.value='1';h.form.elements.minute.value='15';
 await h.form.events.submit({preventDefault(){}});
 assert.deepEqual(JSON.parse(h.calls[0].options.body),{action:'update_schedule',schedule:{mode:'cron',cron:'15 * * * *',timezone:zone},digest:{enabled:false,instructions:'',language:'en',include_source_links:false,email_format:'body'}});
 assert.equal(h.feed.report.trigger.condition.value,1);
});

test('digest normalization preserves user instructions and custom statistics while validating inputs',()=>{
 const previous={enabled:true,instructions:'Original',language:'zh-hans',group_by:['设备'],metrics:[{label:'总时长',op:'sum',field:'duration'}]};
 const raw='  Please compare A&B\n保留原文 <draft>  ';
 const value=controls.buildDigest({enabled:false,instructions:raw,language:'en'},previous);
 assert.deepEqual(value,{...previous,enabled:false,instructions:raw,language:'en',include_source_links:false,email_format:'body'});
 assert.equal(previous.instructions,'Original');assert.notEqual(value.group_by,previous.group_by);assert.notEqual(value.metrics,previous.metrics);
 assert.throws(()=>controls.buildDigest({enabled:true,instructions:'x'.repeat(2001),language:'en'}),/2000/);
 assert.throws(()=>controls.buildDigest({enabled:true,instructions:'',language:'fr'}));
});

test('digest editor starts off, reveals its two controls when enabled, and resets from the next report',async()=>{
 const h=setup();await h.click('digest');
 assert.equal(h.form.elements.digest_enabled.checked,false);assert.equal(h.digestFields.hidden,true);
 assert.equal(h.form.elements.digest_instructions.disabled,true);assert.equal(h.labels['[data-report-schedule-fields]'].hidden,true);
 h.form.elements.digest_enabled.checked=true;h.form.elements.digest_enabled.events.change();
 assert.equal(h.digestFields.hidden,false);assert.equal(h.form.elements.digest_instructions.disabled,false);
 h.form.elements.digest_instructions.value='Draft';h.dialog.close();await h.click('digest');
 assert.equal(h.form.elements.digest_enabled.checked,false);assert.equal(h.form.elements.digest_instructions.value,'');assert.equal(h.digestFields.hidden,true);
});

test('digest-only save leaves paused delivery and manual or custom schedules untouched',async()=>{
 for(const schedule of [{mode:'manual',timezone:zone},{mode:'cron',cron:'*/15 8-17 * * mon-fri',timezone:zone}]){
  const h=setup();h.feed.report.enabled=false;h.feed.report.schedule=schedule;
  h.feed.report.digest={enabled:true,instructions:'  原始要求\nRaw prompt  ',language:'zh-hans',include_source_links:false,email_format:'body',group_by:['设备'],metrics:[{label:'Sum',op:'sum',field:'Value'}]};
  await h.click('digest');h.form.elements.frequency.value='invalid';h.form.elements.timezone.value='';
  h.form.elements.digest_instructions.value+='Updated';
  await h.form.events.submit({preventDefault(){}});
  const request=JSON.parse(h.calls[0].options.body);
  assert.deepEqual(request,{action:'update_digest',digest:{...h.feed.report.digest,instructions:'  原始要求\nRaw prompt  Updated'}});
  assert.equal(h.feed.report.enabled,false);assert.equal(h.feed.report.schedule,schedule);assert.equal(h.dialog.open,false);
 }
});

test('schedule and digest updates share one request and retain chat-configured grouping and metrics',async()=>{
 const h=setup();h.feed.report.digest={enabled:false,instructions:'',language:'en',group_by:['设备'],metrics:[{label:'Average',op:'avg',field:'Value'}]};
 await h.click('edit');h.form.elements.frequency.value='weekly';h.form.elements.weekday.value='2';h.form.elements.time.value='10:15';
 h.form.elements.digest_enabled.checked=true;h.form.elements.digest_instructions.value='Summarize exceptions';h.form.elements.digest_language.value='zh-hans';
 await h.form.events.submit({preventDefault(){}});
 assert.equal(h.calls.length,1);assert.deepEqual(JSON.parse(h.calls[0].options.body),{action:'update_schedule',schedule:{mode:'cron',cron:'15 10 * * 2',timezone:zone},
  digest:{enabled:true,instructions:'Summarize exceptions',language:'zh-hans',include_source_links:false,email_format:'body',group_by:['设备'],metrics:[{label:'Average',op:'avg',field:'Value'}]}});
});

test('enabled digest instructions render as raw data and creator restrictions apply to the digest action',async()=>{
 const h=setup(),raw='业务数据 <img src=x> & Keep This';h.feed.report.digest={enabled:true,instructions:raw,language:'en'};
 const section=h.render(),details=section.children.find(node=>node.className==='feed-digest-summary');
 assert.ok(details);assert.equal(details.children[1].textContent,raw);assert.equal(details.children[1].translate,'no');
 h.feed.report.can_edit=false;assert.equal(buttons(h.render()).length,0);await h.click('digest');assert.equal(h.calls.length,0);assert.notEqual(h.dialog.open,true);
});

test('link and email-format defaults are conservative and existing presentation options survive unrelated edits',()=>{
 assert.equal(controls.parseDigest().include_source_links,false);assert.equal(controls.parseDigest().email_format,'body');
 const previous={enabled:true,instructions:'Raw',language:'en',include_source_links:true,email_format:'html_attachment',group_by:['Unit'],metrics:[],charts:[{kind:'bar',x:'Unit',y:'Total'}]};
 const next=controls.buildDigest({enabled:true,instructions:'Revised',language:'zh-hans'},previous);
 assert.equal(next.include_source_links,true);assert.equal(next.email_format,'html_attachment');assert.deepEqual(next.group_by,['Unit']);assert.deepEqual(next.charts,previous.charts);assert.notEqual(next.charts,previous.charts);
 assert.throws(()=>controls.buildDigest({enabled:true,instructions:'',language:'en',email_format:'unsafe'}));
});

test('both channels show presentation options while only email allows attachments and capability warnings',async()=>{
 const h=setup();h.feed.report.digest={enabled:true,instructions:'',language:'en',include_source_links:true,email_format:'body'};
 await h.click('digest');assert.equal(h.form.elements.digest_source_links.checked,true);assert.equal(h.form.elements.digest_source_links.disabled,false);
 assert.equal(h.emailFields.hidden,false);assert.equal(h.form.elements.digest_email_format.disabled,false);assert.equal(h.attachmentOption.hidden,true);assert.equal(h.attachmentOption.disabled,true);assert.equal(h.attachmentWarning.hidden,true);
 h.dialog.close();h.feed.report.kind='email';await h.click('digest');assert.equal(h.emailFields.hidden,false);assert.equal(h.form.elements.digest_email_format.disabled,false);assert.equal(h.attachmentOption.hidden,false);assert.equal(h.attachmentOption.disabled,false);
 assert.equal(h.attachmentWarning.hidden,true);h.form.elements.digest_email_format.value='html_attachment';h.form.elements.digest_email_format.events.change();assert.equal(h.attachmentWarning.hidden,false);
 h.feed.report.html_attachments_supported=true;h.form.elements.digest_email_format.events.change();assert.equal(h.attachmentWarning.hidden,true);
 h.feed.report.html_attachments_supported=false;h.form.elements.digest_enabled.checked=false;h.form.elements.digest_enabled.events.change();assert.equal(h.attachmentWarning.hidden,true);
});

test('Teams HTML report links round-trip without requiring email attachment mapping or altering charts',async()=>{
 const h=setup(),charts=[{kind:'bar',x:'Unit',y:'Total'}];h.feed.report.kind='teams';h.feed.report.digest={enabled:true,instructions:'Dashboard',language:'en',email_format:'html_link',include_source_links:true,charts};
 await h.click('digest');assert.equal(h.form.elements.digest_email_format.value,'html_link');assert.equal(h.attachmentWarning.hidden,true);assert.equal(h.attachmentOption.hidden,true);
 await h.form.events.submit({preventDefault(){}});
 const request=JSON.parse(h.calls[0].options.body);assert.equal(request.digest.email_format,'html_link');assert.deepEqual(request.digest.charts,charts);assert.equal('schedule' in request,false);
 assert.equal(controls.parseDigest({email_format:'html_link'}).email_format,'html_link');
 assert.equal(controls.buildDigest({enabled:true,instructions:'',language:'en',email_format:'html_link'}).email_format,'html_link');
});

test('HTML email and source-link selection are saved without changing the paused schedule',async()=>{
 const h=setup();h.feed.report.kind='email';h.feed.report.enabled=false;await h.click('digest');
 h.form.elements.digest_enabled.checked=true;h.form.elements.digest_source_links.checked=true;h.form.elements.digest_email_format.value='html_attachment';
 await h.form.events.submit({preventDefault(){}});
 const request=JSON.parse(h.calls[0].options.body);assert.equal(request.action,'update_digest');assert.equal(request.digest.include_source_links,true);assert.equal(request.digest.email_format,'html_attachment');
 assert.equal('schedule' in request,false);assert.equal('enabled' in request,false);assert.equal(h.feed.report.enabled,false);
});

test('card capability guidance appears only for enabled HTML email digests',()=>{
 const h=setup();h.feed.report.digest={enabled:true,email_format:'html_attachment'};
 const hasWarning=()=>h.render().children.some(node=>node.className==='feed-digest-capability-note');
 assert.equal(hasWarning(),false);h.feed.report.kind='email';assert.equal(hasWarning(),true);
 h.feed.report.html_attachments_supported=true;assert.equal(hasWarning(),false);
 h.feed.report.html_attachments_supported=false;h.feed.report.digest.email_format='body';assert.equal(hasWarning(),false);
 h.feed.report.digest.email_format='html_attachment';h.feed.report.digest.enabled=false;assert.equal(hasWarning(),false);
});
