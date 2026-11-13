(function(root){
 'use strict';
 const t=value=>root.FOSI18n?.t(value)||value;
 const labels={count:'有效数值',missing:'缺失值',non_numeric:'非数值',sum:'合计',avg:'平均值',min:'最小值',max:'最大值'};
 const node=(tag,text,cls,raw=false)=>{const value=root.document.createElement(tag);if(text!==undefined)value.textContent=String(text);if(cls)value.className=cls;if(raw)value.setAttribute('translate','no');return value;};
 const display=value=>value===null||value===undefined?'—':typeof value==='object'?Array.isArray(value)?value.map(display).join(', '):Object.entries(value).map(([key,item])=>key+': '+display(item)).join('; '):String(value);
 function safeSourceURL(value){
  if(typeof value!=='string'||/[\u0000-\u001f\u007f]/.test(value))return '';
  try{const url=new root.URL(value.trim());return ['http:','https:'].includes(url.protocol)&&!url.username&&!url.password?url.href:'';}catch(_error){return '';}
 }
 function safeHTMLPreviewURL(value){
  return typeof value==='string'&&/^\/kb\/feeds\/reports\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/preview\.xlsx\?format=html$/i.test(value)?value:'';
 }
 function sourceLinksView(metadata){
  if(!metadata||typeof metadata!=='object')return null;
  const section=node('details',undefined,'cx-digest-source-links'),list=node('ul');section.append(node('summary',t('原文链接')));
  let invalid=0;
  for(const source of Array.isArray(metadata.links)?metadata.links:[]){
   const url=safeSourceURL(source?.url);if(!url){invalid++;continue;}
   const link=node('a',typeof source.label==='string'&&source.label?source.label:url,undefined,true);link.href=url;link.target='_blank';link.rel='noopener noreferrer';
   const item=node('li');item.append(link);list.append(item);
  }
  if(list.children.length)section.append(list);else section.append(node('p',t('没有可展示的原文链接。'),'cx-meta'));
  const counters=[t('原文链接总数')+': '+display(metadata.total_links??list.children.length)];
  if(metadata.omitted_links)counters.push(t('未展示链接')+': '+display(metadata.omitted_links));
  const rejected=(Number(metadata.rejected_links)||0)+invalid;if(rejected)counters.push(t('已排除链接')+': '+rejected);
  section.append(node('p',counters.join(' · '),'cx-meta'));return section;
 }
 function factTable(columns,rows){
  const result=node('table',undefined,'cx-digest-fact-table'),head=node('thead'),heading=node('tr'),body=node('tbody');
  for(const column of columns)heading.append(node('th',t(column)));head.append(heading);result.append(head,body);
  for(const values of rows){const tr=node('tr');for(const value of values)tr.append(node('td',display(value),undefined,true));body.append(tr);}return result;
 }
 function numericView(fields){
  if(!fields||!Object.keys(fields).length)return null;
  const details=node('details',undefined,'cx-digest-numeric'),keys=Object.keys(labels);
  details.append(node('summary',t('数值字段统计')),factTable(['字段',...keys.map(key=>labels[key])],Object.entries(fields).map(([field,stats])=>[field,...keys.map(key=>stats[key])])));return details;
 }
 function metricsView(metrics){
  if(!metrics?.length)return null;
  const content=node('div',undefined,'cx-digest-metrics');content.append(factTable(['指标','数值'],metrics.map(metric=>[metric.label||metric.field||metric.op,metric.value])));
  for(const metric of metrics){const quality=metric.quality||{},warnings=[];
   for(const key of ['missing','non_numeric'])if(quality[key])warnings.push(t(labels[key])+': '+quality[key]);
   if(warnings.length){const note=node('p',undefined,'cx-digest-quality');note.append(node('span',(metric.label||metric.field||metric.op)+': ',undefined,true),node('span',warnings.join(' · ')));content.append(note);}
  }return content;
 }
 function factsView(facts){
  const content=node('div',undefined,'cx-digest-fact-content');
  const overview=node('p',undefined,'cx-digest-overview');overview.append(node('span',t('报告结果行数')+': '),node('strong',display(facts.row_count),undefined,true));
  if(facts.group_by?.length)overview.append(node('span',' · '+t('分组数')+': '),node('strong',display(facts.total_groups),undefined,true));content.append(overview);
  const metrics=metricsView(facts.metrics),numeric=numericView(facts.numeric_fields);if(metrics)content.append(metrics);if(numeric)content.append(numeric);
  if(facts.group_by?.length){
   const groups=node('details',undefined,'cx-digest-groups');groups.append(node('summary',t('分组汇总')));
   groups.append(node('p',t('分组预览最多展示 20 组；总计覆盖全部报告结果。'),'cx-meta'));
   for(const group of facts.groups||[]){
    const details=node('details',undefined,'cx-digest-fact-group'),heading=node('summary');
    heading.append(node('span',Object.entries(group.keys||{}).map(([key,value])=>key+': '+display(value)).join(' · '),undefined,true),node('span',' · '+t('记录数')+': '+display(group.row_count)));
    details.append(heading);const groupMetrics=metricsView(group.metrics),groupNumeric=numericView(group.numeric_fields);if(groupMetrics)details.append(groupMetrics);if(groupNumeric)details.append(groupNumeric);groups.append(details);
   }
   if(facts.omitted_groups){const omitted=node('p',undefined,'cx-digest-omission');omitted.append(node('span',t('未展开分组')+': '+facts.omitted_groups+' · '+t('未展开记录')+': '+facts.omitted_row_count));groups.append(omitted);}
   content.append(groups);
  }
  return content;
 }
 function render(config,preview,context={}){
  if(!config?.enabled)return null;
  const section=node('section',undefined,'cx-report-digest');section.append(node('h4','EnGenie Digest'));
  section.append(node('p',t('发送时包含简要总结和汇总数字。'),'cx-meta'));
  section.append(node('p',t(config.include_source_links?'附上原文链接':'不附原文链接')+' · '+t('报告展示方式')+': '+t(config.email_format==='html_link'?'HTML 报告链接':config.email_format==='html_attachment'&&context.kind!=='teams'?'HTML 邮件附件':'摘要正文'),'cx-digest-presentation'));
  if(config.email_format==='html_attachment'&&context.kind==='email'&&context.delivery?.html_attachments_supported!==true){
   const warning=node('p',t('当前邮件工作流尚未确认支持 HTML 附件；发送前请升级工作流并确认附件支持。'),'cx-digest-capability-warning');warning.setAttribute('role','status');section.append(warning);
  }
  if((config.email_format==='html_link'&&['email','teams'].includes(context.kind)||config.email_format==='html_attachment'&&context.kind==='email')&&preview?.html){
   const url=safeHTMLPreviewURL(preview.html.download_url);
   if(url){const download=node('p',undefined,'cx-digest-html-preview'),link=node('a',t('下载 HTML 预览'));link.href=url;link.setAttribute('download',preview.html.filename||'EnGenie-report.html');download.append(link,node('small',' '+(preview.html.filename||'EnGenie-report.html'),undefined,true));section.append(download);}
  }
  if(preview?.status==='skipped'){
   section.append(node('p',t(preview.reason==='condition_not_met'?'当前数据未满足触发条件；本次不生成或发送摘要。':preview.reason==='no_data'?'当前没有可用于摘要的数据，本次未发送。':'本次未生成摘要。'),'cx-digest-status'));
  }else if(preview?.text){section.append(node('h5',t('摘要预览')),node('p',preview.text,'cx-digest-text',true));}
  else if(preview?.error){section.append(node('p',t('摘要预览未完成。'),'cx-digest-status'),node('p',preview.error,'cx-digest-text',true));}
  else section.append(node('p',t('摘要将在报告发送前生成。可通过 EnGenie 对话预览和调整。'),'cx-digest-status'));
  if(config.instructions){const instructions=node('details',undefined,'cx-digest-instructions');instructions.append(node('summary',t('摘要要求')),node('p',config.instructions,undefined,true));section.append(instructions);}
  if(config.include_source_links){const links=sourceLinksView(preview?.source_links);if(links)section.append(links);}
  if(preview?.facts&&typeof preview.facts==='object'&&Object.keys(preview.facts).length){
   const facts=node('section',undefined,'cx-digest-facts');facts.append(node('h5',t('汇总数字')),factsView(preview.facts));section.append(facts);
  }
  return section;
 }
 function renderCard(title,preview){
  if(!preview||typeof preview!=='object')return null;
  const section=node('section',undefined,'cx-report-digest cx-teams-card-preview');
  section.append(node('h4',t('Teams 卡片预览')),node('strong',title,undefined,true),
   node('p',t('最多三个参数；相同行序的值对应同一项目。'),'cx-meta'));
  const facts=Array.isArray(preview.facts)?preview.facts.slice(0,3):[];
  for(const fact of facts){const item=node('p');item.append(node('strong',fact.title+': ',undefined,true),node('span',fact.value,undefined,true));section.append(item);}
  if(preview.omitted_rows)section.append(node('p',t('已展示 '+preview.shown_rows+' / '+preview.row_count+' 项')+' · '+t('卡片空间有限，完整清单保留在 Excel 报告中。'),'cx-note'));
  else if(!preview.row_count&&!preview.will_send)section.append(node('p',t('当前没有满足条件的项目，本次不会发送。'),'cx-note'));
  return section;
 }
 root.ConnectorReportDigest={render,renderCard,safeSourceURL,safeHTMLPreviewURL};
})(typeof window!=='undefined'?window:globalThis);

(()=>{
 'use strict';const page=document.getElementById('connectorPage');if(!page)return;
 const messages=document.getElementById('connectorMessages'),composer=document.getElementById('composerForm');
 const prompt=document.getElementById('connectorPrompt'),send=document.getElementById('connectorSend'),hint=document.getElementById('connectorHint');
 const csrf=()=>composer.querySelector('[name=csrfmiddlewaretoken]').value;
 const requests=new Map();
 const requestKey=()=>{
  const crypto=globalThis.crypto;
  if(typeof crypto?.randomUUID==='function')return crypto.randomUUID();
  if(typeof crypto?.getRandomValues==='function')return Array.from(crypto.getRandomValues(new Uint8Array(16)),byte=>byte.toString(16).padStart(2,'0')).join('');
  return Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)+'-'+Math.random().toString(36).slice(2);
 };
 messages.addEventListener('submit',event=>{
  const form=event.target.closest('[data-builder-question-form]');if(!form)return;
  event.preventDefault();if(prompt.disabled||send.disabled)return;
  const answers=[...form.querySelectorAll('[data-question-label]')].map(input=>({key:input.name,label:input.dataset.questionLabel,value:input.value.trim()}));
  if(!answers.length)return;
  const text=[prompt.value.trim(),answers.filter(answer=>answer.value).map(answer=>answer.label+'：'+answer.value).join('\n')||(window.FOSI18n?.t('可选信息暂不补充，请继续。')||'可选信息暂不补充，请继续。')].filter(Boolean).join('\n\n');
  if(text.length>prompt.maxLength){hint.textContent='回答内容过长，请缩短后再提交（最多'+prompt.maxLength+'字）。';return;}
  prompt.value=text;
  prompt.dispatchEvent(new Event('input'));composer.questionAnswers={job:form.dataset.builderQuestionForm,answers:answers.map(({key,value})=>({key,value}))};composer.requestSubmit();
 });
 messages.addEventListener('click',async event=>{
  const button=event.target.closest('[data-report-test]');if(!button)return;
  event.preventDefault();const url=button.dataset.reportTest;
  if(!url){hint.textContent='请刷新页面后重试发送报告。';return;}
  let state=requests.get(url);
  if(state?.pending)return;
  if(state?.accepted){button.disabled=true;button.textContent='工作流已接收测试报告';return;}
  button.disabled=true;let timeout;
  try{
   if(!state){state={key:button.dataset.requestKey||requestKey()};requests.set(url,state);}
   state.pending=true;button.dataset.requestKey=state.key;
   const controller=new AbortController();timeout=setTimeout(()=>controller.abort(),60000);
   const reply=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf(),'X-Requested-With':'XMLHttpRequest'},body:JSON.stringify({request_key:state.key}),signal:controller.signal});
   const type=reply.headers?.get('content-type');
   if(type&&!type.includes('application/json'))throw Error('会话已失效或请求被拒绝，请刷新页面后重试。');
   const data=await reply.json();
   if(!reply.ok||!data.ok){if(data.status==='failed'){requests.delete(url);delete button.dataset.requestKey;}throw Error(data.error||'发送未完成');}
   if(data.status==='skipped'){
    requests.delete(url);delete button.dataset.requestKey;button.disabled=false;
    button.textContent='重新检查触发条件';hint.textContent=data.message||'当前数据未满足触发条件，本次未发送。';
   }else{state.accepted=true;button.textContent='工作流已接收测试报告';hint.textContent=data.message;}
  }catch(error){hint.textContent=error.name==='AbortError'?'发送结果尚未确认，请检查工作流记录；使用同一按钮重试不会重复发送。':error.message;button.textContent='查看发送状态 / 重试';button.disabled=false;}
  finally{clearTimeout(timeout);if(state)state.pending=false;}
 });
})();
