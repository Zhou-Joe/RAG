(function(){
 'use strict';
 const page=document.getElementById('feedPage');if(!page)return;
 const list=document.getElementById('feedList'),message=document.getElementById('feedMessage'),form=document.getElementById('feedForm');
 const editor=document.getElementById('feedEditor'),filter=document.getElementById('feedFilter'),error=document.getElementById('feedFormError');
 const auth=new FeedAuthForm(form.querySelector('[data-auth-widget]'));
 const labels={running:'更新中',completed:'已完成',failed:'失败',manual:'手动',cron:'定时',push:'脚本推送',import:'本地导入'};
 let feeds=[],timer,controller,disposed=false,sequence=0,fingerprint='';
 const node=(tag,text,cls)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;return el;};
 const date=(value,zone)=>value?new Date(value).toLocaleString(window.FOSI18n?.locale || 'zh-CN',zone?{timeZone:zone}:undefined):'—';
 async function request(url,options={}){
  const abort=new AbortController(),timeout=setTimeout(()=>abort.abort(),15000);controller=abort;
  try{
   const response=await fetch(url,{cache:'no-store',...options,signal:abort.signal,headers:{'X-Requested-With':'XMLHttpRequest',...options.headers}});
   if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('会话已失效，请重新登录。');
   const data=await response.json();if(!response.ok||!data.ok)throw new Error(data.error||'操作失败。');return data;
  }catch(exc){throw new Error(exc.name==='AbortError'?'请求超时，请稍后重试。':exc.message);}
  finally{clearTimeout(timeout);if(controller===abort)controller=null;}
 }
 const post=data=>request(page.dataset.actionUrl,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':form.querySelector('[name=csrfmiddlewaretoken]').value},body:JSON.stringify(data)});
 function button(label,action,id){const el=node('button',label,'btn sm outline');el.type='button';el.dataset.action=action;el.dataset.id=id;return el;}
 function render(){
  const opened=new Set([...list.querySelectorAll('.feed-card')].filter(card=>card.querySelector('details')?.open).map(card=>card.dataset.feedId));
  list.replaceChildren();
  if(!feeds.length){list.append(node('p','暂无自动任务。点击「用 EnGenie 创建自动任务」，提供网页或 API 地址即可开始。','feed-empty'));return;}
  for(const feed of feeds){
   const card=node('section',undefined,'feed-card');card.dataset.feedId=feed.id;
   card.append(node('h2',feed.name));const link=node('a',feed.kb_name);link.href='/kb/manage/'+encodeURIComponent(feed.kb)+'/';card.append(link);
   card.append(node('p',(feed.mode==='pull'?'定时采集 · '+feed.cron+' · '+feed.timezone:'外部脚本推送')+' · '+(feed.enabled?'已启用':'已暂停')+' · '+(feed.write_mode==='upsert'?'覆盖相同 ID':'追加保留历史')+' · '+feed.items+' 条记录','feed-meta'));
   if(feed.mode==='pull')card.append(node('p','下次运行：'+date(feed.next_run_at,feed.timezone),'feed-meta'));
   const actions=node('div',undefined,'feed-actions');
   if(feed.ai_edit_url){const ai=node('a','与 EnGenie 调整任务','btn sm primary');ai.href=feed.ai_edit_url;actions.append(ai);}
   if(feed.mode==='pull')actions.append(button('立即更新','run',feed.id));else actions.append(button('生成／轮换推送令牌','rotate_token',feed.id));
   actions.append(button('编辑配置','edit',feed.id),button(feed.enabled?'暂停后续更新':'启用更新',feed.enabled?'pause':'resume',feed.id));
   const tracker=node('a','查看追踪表','btn sm outline');tracker.href='/kb/manage/'+encodeURIComponent(feed.kb)+'/tracker/';actions.append(tracker);card.append(actions);
   const latest=feed.runs[0];if(latest?.error)card.append(node('p',latest.error,'feed-error'));
   const details=node('details');details.open=opened.has(feed.id);details.append(node('summary','运行记录（最近10次）'));
   const wrap=node('div',undefined,'feed-runs'),table=node('table'),head=node('tr');
   for(const text of ['开始时间','触发','状态','新增／更新／删除／未变化','错误'])head.append(node('th',text));table.append(head);
   for(const run of feed.runs){const row=node('tr');for(const text of [date(run.started_at),labels[run.trigger]||run.trigger,labels[run.status]||run.status,[run.counts.created||0,run.counts.updated||0,run.counts.deleted||0,run.counts.unchanged||0].join(' / '),run.error||(run.counts.warnings?run.counts.warnings+'条记录格式变化，已保留原文，请查看追踪表的采集提示。':'—')])row.append(node('td',text));table.append(row);}
   wrap.append(table);details.append(wrap);card.append(details);list.append(card);
  }
 }
 async function refresh(){
  clearTimeout(timer);if(disposed||document.hidden)return;const ticket=++sequence;
  try{
   const data=await request(page.dataset.stateUrl+(filter.value?'?kb='+encodeURIComponent(filter.value):''));if(disposed||ticket!==sequence)return;
   feeds=data.feeds;const next=JSON.stringify(feeds);if(next!==fingerprint){fingerprint=next;render();}
   document.getElementById('feedScheduler').textContent=data.scheduler_recent?'调度服务运行正常，已启用的数据源将按计划更新。':'调度服务最近未响应，请联系管理员检查。现有数据仍可查看，也可手动更新。';
   if(data.truncated)message.textContent='当前仅显示最近100个数据源，请按资料集合筛选。';
  }catch(exc){if(!disposed&&ticket===sequence)message.textContent=exc.message;}
  finally{if(!disposed&&!document.hidden&&ticket===sequence)timer=setTimeout(refresh,feeds.some(feed=>feed.runs[0]?.status==='running')?2000:15000);}
 }
 function fields(){
  document.getElementById('feedNewLibrary').hidden=!!form.elements.kb.value;
  form.querySelector('.feed-pull-fields').hidden=form.elements.mode.value!=='pull';
  form.querySelector('.feed-push-hint').hidden=form.elements.mode.value!=='push';
  form.elements.url.required=form.elements.mode.value==='pull';
 }
 function edit(feed){
  form.reset();error.textContent='';form.elements.id.value=feed?.id||'';
  auth.load(feed?.authentication);
  document.getElementById('feedUrlLabel').textContent=feed?.source_type==='browser'?'网页来源地址':'标准数据 API 地址';
  document.getElementById('feedSourceHint').textContent=feed?.source_type==='browser'?'此来源使用 EnGenie 已试采的网页操作步骤。要调整采集内容，请与 EnGenie 对话并查看新样例。':'API 返回 fos-rag-feed/v1 JSON。网页或其他接口格式可通过 EnGenie 创建自动采集任务。';
  document.getElementById('feedEditorTitle').textContent=feed?'编辑数据源':'新建自动更新知识库';
  document.getElementById('feedLibraryFields').hidden=!!feed;
  if(feed){for(const key of ['name','mode','url','cron','timezone','write_mode'])form.elements[key].value=feed[key];if(form.elements.header_env)form.elements.header_env.value=JSON.stringify(feed.header_env,null,2);if(form.elements.allow_private)form.elements.allow_private.checked=feed.allow_private;}
  else form.elements.kb.value=filter.value;
  fields();editor.showModal();form.elements.name.focus();
 }
 form.elements.kb.addEventListener('change',fields);form.elements.mode.addEventListener('change',fields);
 filter.addEventListener('change',refresh);
 document.addEventListener('click',e=>{const close=e.target.closest('[data-close]');if(close){const dialog=document.getElementById(close.dataset.close);dialog.close();if(dialog.id==='feedToken')document.getElementById('feedTokenValue').value='';}});
 form.addEventListener('submit',async event=>{
  event.preventDefault();const save=document.getElementById('feedSave');save.disabled=true;error.textContent='';
  try{
   const data=Object.fromEntries(new FormData(form));delete data.csrfmiddlewaretoken;data.action=data.id?'update':'create';data.allow_private=!!form.elements.allow_private?.checked;
   data.authentication=auth.read();const result=await post(data);message.textContent=result.message;auth.load(result.feed.authentication);
   if(![...filter.options].some(option=>option.value===result.feed.kb)){const option=node('option',result.feed.kb_name);option.value=result.feed.kb;filter.append(option);form.elements.kb.append(option.cloneNode(true));}
   filter.value=result.feed.kb;
   editor.close();await refresh();
  }catch(exc){error.textContent=exc.message;}finally{save.disabled=false;}
 });
 list.addEventListener('click',async event=>{
  const target=event.target.closest('[data-action]');if(!target)return;const feed=feeds.find(f=>f.id===target.dataset.id);if(!feed)return;
  if(target.dataset.action==='edit'){edit(feed);return;}
  target.disabled=true;
  try{
   const result=await post({action:target.dataset.action,id:feed.id});message.textContent=result.message||'';
   if(result.token){document.getElementById('feedIngestUrl').value=new URL(result.ingest_url,location.origin).href;document.getElementById('feedTokenValue').value=result.token;document.getElementById('feedToken').showModal();}
   await refresh();
  }catch(exc){message.textContent=exc.message;}finally{target.disabled=false;}
 });
 document.getElementById('feedToken').addEventListener('close',()=>{document.getElementById('feedTokenValue').value='';});
 document.addEventListener('visibilitychange',()=>{clearTimeout(timer);if(!document.hidden)refresh();});
 window.addEventListener('pagehide',()=>{disposed=true;clearTimeout(timer);controller?.abort();});
 refresh();
})();
