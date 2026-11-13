(function(global){
 'use strict';
 const intervals=[1,2,3,4,6,8,12],weekdays=['星期日','星期一','星期二','星期三','星期四','星期五','星期六'];
 const t=text=>global.FOSI18n?.t(text)||text;
 function parseSchedule(schedule={}){
  const result={frequency:'custom',time:'09:00',weekday:'1',interval:'1',minute:'0',cron:schedule.cron||'',timezone:schedule.timezone||'Asia/Shanghai'};
  if(['manual','after_collection'].includes(schedule.mode))return {...result,frequency:schedule.mode};
  const parts=result.cron.trim().toLowerCase().split(/\s+/);
  if(parts.length!==5||parts[2]!=='*'||parts[3]!=='*'||!/^\d+$/.test(parts[0])||Number(parts[0])>59)return result;
  const minute=Number(parts[0]),hour=Number(parts[1]);result.minute=String(minute);
  if(/^\d+$/.test(parts[1])&&hour<=23){
   result.time=String(hour).padStart(2,'0')+':'+String(minute).padStart(2,'0');
   if(parts[4]==='*')return {...result,frequency:'daily'};
   const names=['sun','mon','tue','wed','thu','fri','sat'];
   const day=names.includes(parts[4])?names.indexOf(parts[4]):/^\d$/.test(parts[4])?Number(parts[4]):NaN;
   if(day>=0&&day<=7)return {...result,frequency:'weekly',weekday:String(day%7)};
  }
  const every=parts[1]==='*'?1:/^\*\/\d+$/.test(parts[1])?Number(parts[1].slice(2)):NaN;
  if(parts[4]==='*'&&intervals.includes(every))return {...result,frequency:'hourly',interval:String(every)};
  return result;
 }
 function buildSchedule(values){
  const timezone=(values.timezone||'').trim();if(!timezone)throw Error('请填写报告时区。');
  if(['manual','after_collection'].includes(values.frequency))return {mode:values.frequency,timezone};
  let cron;
  if(values.frequency==='custom'){
   cron=(values.cron||'').trim();if(cron.split(/\s+/).length!==5)throw Error('Cron 必须是五字段：分 时 日 月 星期。');
  }else if(values.frequency==='hourly'){
   const every=Number(values.interval),minute=Number(values.minute);
   if(!intervals.includes(every)||!/^\d+$/.test(String(values.minute))||minute>59)throw Error('请选择有效的小时数和发送分钟。');
   cron=`${minute} ${every===1?'*':'*/'+every} * * *`;
  }else if(['daily','weekly'].includes(values.frequency)){
   if(!/^\d{2}:\d{2}$/.test(values.time||''))throw Error('请选择发送时间。');
   const [hour,minute]=values.time.split(':').map(Number);if(hour>23||minute>59)throw Error('请选择发送时间。');
   let day='*';if(values.frequency==='weekly'){
    if(!/^[0-6]$/.test(String(values.weekday)))throw Error('请选择星期。');day=String(values.weekday);
   }
   cron=`${minute} ${hour} * * ${day}`;
  }else throw Error('请选择报告频率。');
  return {mode:'cron',cron,timezone};
 }
 function scheduleText(schedule){
  const value=parseSchedule(schedule);
  if(value.frequency==='daily')return `${t('每天')} ${value.time}`;
  if(value.frequency==='weekly')return `${t(weekdays[Number(value.weekday)])} ${value.time}`;
  if(value.frequency==='hourly')return `${t('间隔小时数')}: ${value.interval} · ${t('发送分钟')}: ${value.minute}`;
  if(value.frequency==='manual')return t('仅按需发送');
  if(value.frequency==='after_collection')return t('采集后发送（原有设置）');
  return 'Cron: '+value.cron;
 }
 function parseDigest(digest={}){
  return {enabled:!!digest.enabled,instructions:typeof digest.instructions==='string'?digest.instructions:'',language:digest.language==='zh-hans'?'zh-hans':'en',include_source_links:digest.include_source_links===true,email_format:['body','html_link','html_attachment'].includes(digest.email_format)?digest.email_format:'body',
   ...(Array.isArray(digest.group_by)?{group_by:digest.group_by.slice()}:{}),...(Array.isArray(digest.metrics)?{metrics:digest.metrics.slice()}:{}),...(Array.isArray(digest.charts)?{charts:digest.charts.slice()}:{})};
 }
 function buildDigest(values,previous={}){
  const instructions=String(values.instructions??'');if(instructions.length>2000)throw Error('Digest 要求不能超过 2000 字符。');
  if(!['en','zh-hans'].includes(values.language))throw Error('请选择 Digest 语言。');
  const current=parseDigest(previous),emailFormat=values.email_format??current.email_format;
  if(!['body','html_link','html_attachment'].includes(emailFormat))throw Error('请选择有效的报告展示方式。');
  return {...current,enabled:!!values.enabled,instructions,language:values.language,include_source_links:values.include_source_links??current.include_source_links,email_format:emailFormat};
 }
 const pending=new Set();
 const el=(tag,text,cls)=>{const node=global.document.createElement(tag);if(text!==undefined)node.textContent=text;if(cls)node.className=cls;return node;};
 function render(feed){
  const report=feed.report;if(!report)return null;
  if(!report.has_report&&!report.saved_workflow_types?.length)return null;
  const section=el('section',undefined,'feed-report-controls');section.append(el('h3','M365 报告发送计划'));
  if(!report.has_report){
   section.append(el('p','尚未确认报告计划。请先在对话中确认报告，再管理发送频率。','feed-meta'));
   if(feed.can_edit&&feed.ai_edit_url){const link=el('a','打开报告对话','btn sm outline');link.href=feed.ai_edit_url;section.append(link);}
   return section;
  }
  const title=el('p',report.title,'feed-meta');title.translate='no';section.append(title);
  section.append(el('p',t(report.kind==='teams'?'Teams 频道':'邮件')+' · '+t(report.enabled?'报告计划已启用':report.schedule?.mode==='manual'?'仅按需发送':'报告已暂停'),'feed-report-status'));
  section.append(el('p',(report.trigger?t('条件检查计划')+': ':'')+scheduleText(report.schedule||{})+' · '+(report.schedule?.timezone||feed.timezone),'feed-meta'));
  if(report.digest?.enabled){
   const digest=el('details',undefined,'feed-digest-summary');digest.append(el('summary',t('EnGenie Digest 已启用')));
   const instructions=el('p',report.digest.instructions||t('自动概述报告数据并附上汇总数字。'),'feed-meta');if(report.digest.instructions)instructions.translate='no';digest.append(instructions);
   digest.append(el('small',t('Digest 语言')+': '+t(report.digest.language==='zh-hans'?'简体中文':'英语')));section.append(digest);
   if(report.digest.include_source_links)digest.append(el('p',t('包含来源链接'),'feed-meta'));
   if(report.digest.email_format==='html_link')digest.append(el('p',t('HTML 报告链接'),'feed-meta'));
   if(report.kind==='email'&&report.digest.email_format==='html_attachment'){
    digest.append(el('p',t('HTML 邮件附件'),'feed-meta'));
    if(!report.html_attachments_supported)section.append(el('p',t('邮件工作流尚未确认附件映射；完成配置后才能发送 HTML 附件。'),'feed-digest-capability-note'));
   }
  }
  if(report.trigger&&global.ReportTriggerUI)section.append(global.ReportTriggerUI.render(report.trigger));
  if(report.enabled&&report.next_run_at){
   let next=report.next_run_at;try{next=new Date(next).toLocaleString(global.FOSI18n?.locale||'zh-CN',{timeZone:report.schedule.timezone});}catch(_error){}
   section.append(el('p',t(report.trigger?'下次条件检查':'下次报告发送')+': '+next,'feed-meta'));
  }
  if(!report.configured){const note=el('p','需要补充发送设置','feed-meta');section.append(note);}
  if(feed.can_edit&&report.can_edit){
   const actions=el('div',undefined,'feed-actions');
   const button=(text,action)=>{const node=el('button',text,'btn sm outline');node.type='button';node.dataset.reportAction=action;node.dataset.feedId=feed.id;node.disabled=pending.has(feed.id);return node;};
   if(report.enabled)actions.append(button('暂停报告发送','pause'));
   else if(report.schedule?.mode!=='manual')actions.append(button('启用报告发送','resume'));
   actions.append(button(report.trigger?'调整检查频率':'调整发送频率','edit'));
   actions.append(button(report.digest?.enabled?'编辑 EnGenie Digest':'添加 EnGenie Digest','digest'));section.append(actions);
  }else section.append(el('p','仅创建者可编辑','feed-meta'));
  return section;
 }
 function init(options){
  const document=global.document,dialog=document.getElementById('feedReportEditor'),form=document.getElementById('feedReportForm'),error=document.getElementById('feedReportError'),save=document.getElementById('feedReportSave');
  if(!dialog||!form)return;
  let editing=null,digestOnly=false;
  function fields(){
   const scheduleFields=form.querySelector('[data-report-schedule-fields]');
   if(scheduleFields){scheduleFields.hidden=digestOnly;for(const input of scheduleFields.querySelectorAll('input,select'))input.disabled=digestOnly;}
   for(const group of form.querySelectorAll('[data-report-frequency]')){
    const visible=group.dataset.reportFrequency.split(' ').includes(form.elements.frequency.value);group.hidden=!visible;
    for(const input of group.querySelectorAll('input,select'))input.disabled=!visible||digestOnly;
   }
   const digestFields=document.getElementById('feedReportDigestFields');
   if(digestFields){
    const enabled=form.elements.digest_enabled.checked,email=editing?.report.kind==='email';digestFields.hidden=!enabled;for(const input of digestFields.querySelectorAll('textarea,select,input'))input.disabled=!enabled;
    const emailFields=document.getElementById('feedReportDigestEmailFields');if(emailFields)emailFields.hidden=false;
    form.elements.digest_email_format.disabled=!enabled;
    const attachmentOption=form.elements.digest_email_format.querySelector('[value=html_attachment]');if(attachmentOption){attachmentOption.hidden=!email;attachmentOption.disabled=!email;}
    if(!email&&form.elements.digest_email_format.value==='html_attachment')form.elements.digest_email_format.value='body';
    const warning=document.getElementById('feedReportDigestAttachmentWarning');if(warning)warning.hidden=!(enabled&&email&&form.elements.digest_email_format.value==='html_attachment'&&!editing.report.html_attachments_supported);
   }
  }
  function edit(feed,onlyDigest=false){
   editing=feed;digestOnly=onlyDigest;form.reset();error.textContent='';document.getElementById('feedReportTitle').textContent=feed.report.title;
   const conditional=Boolean(feed.report.trigger);
   const label=(selector,value)=>{const node=form.querySelector(selector);if(node)node.textContent=t(value);};
   label('h2',digestOnly?'编辑 EnGenie Digest':conditional?'调整条件检查频率':'调整报告发送频率');
   label('label[for="feedReportFrequency"]',conditional?'检查频率':'报告频率');
   label('label[for="feedReportTime"]',conditional?'检查时间':'发送时间');
   label('label[for="feedReportMinute"]',conditional?'检查分钟':'发送分钟');
   save.textContent=t(digestOnly?'保存 Digest':'保存发送设置');
   const values=parseSchedule(feed.report.schedule||{mode:'manual',timezone:feed.timezone});
   for(const [key,value] of Object.entries(values))form.elements[key].value=value;
   form.elements.frequency.querySelector('[value=after_collection]').hidden=values.frequency!=='after_collection';
   const digest=parseDigest(feed.report.digest);form.elements.digest_enabled.checked=digest.enabled;form.elements.digest_instructions.value=digest.instructions;form.elements.digest_language.value=digest.language;
   form.elements.digest_source_links.checked=digest.include_source_links;form.elements.digest_email_format.value=digest.email_format;
   fields();dialog.showModal();(digestOnly?form.elements.digest_enabled:form.elements.frequency).focus();
  }
  async function post(feed,action,data={}){
   if(pending.has(feed.id))throw Error('报告操作正在保存，请稍候。');
   pending.add(feed.id);const abort=new AbortController(),timeout=setTimeout(()=>abort.abort(),15000);
   try{
    const response=await fetch(feed.report.control_url,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-CSRFToken':options.csrf(),'X-Requested-With':'XMLHttpRequest'},body:JSON.stringify({action,...data}),signal:abort.signal});
    if(!response.headers.get('content-type')?.includes('application/json'))throw Error('会话已失效，请重新登录。');
    const result=await response.json();if(!response.ok||!result.ok)throw Error(result.error||'报告操作失败。');
    feed.report=result.report;options.message.textContent=result.message;return result;
   }catch(exc){throw Error(exc.name==='AbortError'?'请求超时，请刷新查看报告计划状态。':exc.message);}
   finally{clearTimeout(timeout);pending.delete(feed.id);}
  }
  form.elements.frequency.addEventListener('change',fields);
  form.elements.digest_enabled.addEventListener('change',fields);
  form.elements.digest_email_format.addEventListener('change',fields);
  options.list.addEventListener('click',async event=>{
   const button=event.target.closest('[data-report-action]');if(!button)return;
   const feed=options.getFeed(button.dataset.feedId);if(!feed||pending.has(feed.id))return;
   if(!feed.can_edit||!feed.report?.can_edit){options.message.textContent='仅此自动任务的创建者可编辑配置。';return;}
   if(['edit','digest'].includes(button.dataset.reportAction)){edit(feed,button.dataset.reportAction==='digest');return;}
   button.disabled=true;
   try{await post(feed,button.dataset.reportAction);await options.refresh();}
   catch(exc){options.message.textContent=exc.message;}finally{button.disabled=false;}
  });
  form.addEventListener('submit',async event=>{
   event.preventDefault();if(!editing)return;const feed=editing;save.disabled=true;error.textContent='';
   try{
    const digest=buildDigest({enabled:form.elements.digest_enabled.checked,instructions:form.elements.digest_instructions.value,language:form.elements.digest_language.value,include_source_links:form.elements.digest_source_links.checked,email_format:form.elements.digest_email_format.value},feed.report.digest);
    if(digestOnly)await post(feed,'update_digest',{digest});
    else{const schedule=buildSchedule(Object.fromEntries(new FormData(form)));await post(feed,'update_schedule',{schedule,digest});}
    dialog.close();await options.refresh();
   }
   catch(exc){error.textContent=exc.message;}finally{save.disabled=false;}
  });
  dialog.addEventListener('close',()=>{editing=null;digestOnly=false;form.reset();error.textContent='';});
 }
 const api={parseSchedule,buildSchedule,scheduleText,parseDigest,buildDigest,render,init};
 if(typeof module!=='undefined'&&module.exports)module.exports=api;
 global.FeedReportControls=api;
})(typeof window!=='undefined'?window:globalThis);
