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
  section.append(el('p',scheduleText(report.schedule||{})+' · '+(report.schedule?.timezone||feed.timezone),'feed-meta'));
  if(report.enabled&&report.next_run_at){
   let next=report.next_run_at;try{next=new Date(next).toLocaleString(global.FOSI18n?.locale||'zh-CN',{timeZone:report.schedule.timezone});}catch(_error){}
   section.append(el('p',t('下次报告发送')+': '+next,'feed-meta'));
  }
  if(!report.configured){const note=el('p','需要补充发送设置','feed-meta');section.append(note);}
  if(feed.can_edit&&report.can_edit){
   const actions=el('div',undefined,'feed-actions');
   const button=(text,action)=>{const node=el('button',text,'btn sm outline');node.type='button';node.dataset.reportAction=action;node.dataset.feedId=feed.id;node.disabled=pending.has(feed.id);return node;};
   if(report.enabled)actions.append(button('暂停报告发送','pause'));
   else if(report.schedule?.mode!=='manual')actions.append(button('启用报告发送','resume'));
   actions.append(button('调整发送频率','edit'));section.append(actions);
  }else section.append(el('p','仅创建者可编辑','feed-meta'));
  return section;
 }
 function init(options){
  const document=global.document,dialog=document.getElementById('feedReportEditor'),form=document.getElementById('feedReportForm'),error=document.getElementById('feedReportError'),save=document.getElementById('feedReportSave');
  if(!dialog||!form)return;
  let editing=null;
  function fields(){
   for(const group of form.querySelectorAll('[data-report-frequency]')){
    const visible=group.dataset.reportFrequency.split(' ').includes(form.elements.frequency.value);group.hidden=!visible;
    for(const input of group.querySelectorAll('input,select'))input.disabled=!visible;
   }
  }
  function edit(feed){
   editing=feed;form.reset();error.textContent='';document.getElementById('feedReportTitle').textContent=feed.report.title;
   const values=parseSchedule(feed.report.schedule||{mode:'manual',timezone:feed.timezone});
   for(const [key,value] of Object.entries(values))form.elements[key].value=value;
   form.elements.frequency.querySelector('[value=after_collection]').hidden=values.frequency!=='after_collection';
   fields();dialog.showModal();form.elements.frequency.focus();
  }
  async function post(feed,action,schedule){
   if(pending.has(feed.id))throw Error('报告操作正在保存，请稍候。');
   pending.add(feed.id);const abort=new AbortController(),timeout=setTimeout(()=>abort.abort(),15000);
   try{
    const response=await fetch(feed.report.control_url,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-CSRFToken':options.csrf(),'X-Requested-With':'XMLHttpRequest'},body:JSON.stringify({action,...(schedule?{schedule}:{})}),signal:abort.signal});
    if(!response.headers.get('content-type')?.includes('application/json'))throw Error('会话已失效，请重新登录。');
    const result=await response.json();if(!response.ok||!result.ok)throw Error(result.error||'报告操作失败。');
    feed.report=result.report;options.message.textContent=result.message;return result;
   }catch(exc){throw Error(exc.name==='AbortError'?'请求超时，请刷新查看报告计划状态。':exc.message);}
   finally{clearTimeout(timeout);pending.delete(feed.id);}
  }
  form.elements.frequency.addEventListener('change',fields);
  options.list.addEventListener('click',async event=>{
   const button=event.target.closest('[data-report-action]');if(!button)return;
   const feed=options.getFeed(button.dataset.feedId);if(!feed||pending.has(feed.id))return;
   if(!feed.can_edit||!feed.report?.can_edit){options.message.textContent='仅此自动任务的创建者可编辑配置。';return;}
   if(button.dataset.reportAction==='edit'){edit(feed);return;}
   button.disabled=true;
   try{await post(feed,button.dataset.reportAction);await options.refresh();}
   catch(exc){options.message.textContent=exc.message;}finally{button.disabled=false;}
  });
  form.addEventListener('submit',async event=>{
   event.preventDefault();if(!editing)return;const feed=editing;save.disabled=true;error.textContent='';
   try{const schedule=buildSchedule(Object.fromEntries(new FormData(form)));await post(feed,'update_schedule',schedule);dialog.close();await options.refresh();}
   catch(exc){error.textContent=exc.message;}finally{save.disabled=false;}
  });
  dialog.addEventListener('close',()=>{editing=null;form.reset();error.textContent='';});
 }
 const api={parseSchedule,buildSchedule,scheduleText,render,init};
 if(typeof module!=='undefined'&&module.exports)module.exports=api;
 global.FeedReportControls=api;
})(typeof window!=='undefined'?window:globalThis);
