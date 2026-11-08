(() => {
  'use strict';
  const page = document.getElementById('connectorPage'); if (!page) return;
  const messages = document.getElementById('connectorMessages'), welcome = document.getElementById('connectorWelcome');
  const composer = document.getElementById('composerForm');
  const prompt = document.getElementById('connectorPrompt'), send = document.getElementById('connectorSend'), hint = document.getElementById('connectorHint');
  // 内网/本机地址默认开放（规则在 collector_api），页面上不再有开关。static 代码不能当安全边界。
  const newTask = document.getElementById('connectorNew');
  const clearContext = document.getElementById('connectorClearContext');
  let thread = [], timer = null, generation = 0, submitting=false, stopping=false, confirming=false, clearing=false;
  const detailStates=new Map();
  const writeModes=new Map();
  const fieldSelections=new Map();
  const availableFields=job=>[...new Set([...(job.spec.body_fields||[]).map(field=>field.label),'采集日期'])];
  const selectedFields=job=>fieldSelections.get(job.id)||new Set(availableFields(job));
  messages.addEventListener('change',event=>{if(event.target.matches('[data-cx-write-mode]'))writeModes.set(event.target.dataset.cxWriteMode,event.target.value);});
  messages.addEventListener('change',event=>{
    const input=event.target;if(!input.matches('[data-cx-field]'))return;
    const job=thread.find(turn=>turn.id===input.dataset.cxField);if(!job||job.status!=='ready')return;
    const selected=new Set(selectedFields(job));
    if(input.checked)selected.add(input.value);else selected.delete(input.value);
    selected.add('采集日期');fieldSelections.set(job.id,selected);render();
  });
  const completedTools=new Set();
  messages.addEventListener('animationend',event=>{if(event.animationName==='cx-tool-complete')event.target.closest('.cx-tool')?.classList.remove('is-fresh');});
  messages.addEventListener('toggle',event=>{const details=event.target;if(details.matches?.('details[data-detail-key]')&&details.isConnected)detailStates.set(details.dataset.detailKey,details.open);},true);
  const eventCache=new Map();
  function mergeJob(job){
    const stored=eventCache.get(job.id)||new Map();
    for(const [index,event] of (job.events||[]).entries())stored.set(event.id||'legacy-'+index,event);
    eventCache.set(job.id,stored);return {...job,events:[...stored.values()].sort((a,b)=>(a.id||0)-(b.id||0))};
  }

  const el = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined && text !== null) node.textContent = text; return node; };
  const escape = (text) => { const div = el('div'); div.textContent = text ?? ''; return div.innerHTML; };
  const md = (text) => window.marked && window.DOMPurify
    ? DOMPurify.sanitize(marked.parse(text || ''), { FORBID_TAGS: ['img','iframe','object','form','input','video','audio','style'] })
    : escape(text);
  const csrf = () => composer.querySelector('[name=csrfmiddlewaretoken]').value;

  async function request(url, data) {
    const headers = { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest',
      'X-Interface-Language': window.FOSI18n?.language || document.documentElement.dataset.language || 'zh-hans' };
    const options = { headers, cache: 'no-store', signal: AbortSignal.timeout(15000) };
    if (data !== undefined) { options.method = 'POST'; headers['Content-Type'] = 'application/json'; headers['X-CSRFToken'] = csrf(); options.body = JSON.stringify(data); }
    const response = await fetch(url, options), payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || '请求失败，请重试。');
    return payload;
  }
  function schedule(spec) {
    const cron = spec.cron || '0 * * * *', zone = spec.timezone || 'Asia/Shanghai', parts = cron.trim().split(/\s+/);
    let label = '按自定义时间计划';
    if (cron === '0 * * * *') label = '每小时';
    else if (cron === '* * * * *') label = '每分钟';
    else if (/^\*\/\d+ \* \* \* \*$/.test(cron)) label = '每' + parts[0].slice(2) + '分钟';
    else if (/^\d+ \d+ \* \* \*$/.test(cron)) label = '每天 ' + parts[1].padStart(2, '0') + ':' + parts[0].padStart(2, '0');
    else if (/^\d+ \d+ \* \* (?:[0-7]|sun|mon|tue|wed|thu|fri|sat)$/i.test(cron)){const days={'0':'日','7':'日','1':'一','2':'二','3':'三','4':'四','5':'五','6':'六',sun:'日',mon:'一',tue:'二',wed:'三',thu:'四',fri:'五',sat:'六'};label='每周'+days[parts[4].toLowerCase()]+' '+parts[1].padStart(2,'0')+':'+parts[0].padStart(2,'0');}
    else if (/^\d+ \d+ \d+ \* \*$/.test(cron))label='每月'+parts[2]+'日 '+parts[1].padStart(2,'0')+':'+parts[0].padStart(2,'0');
    return label + '（' + zone + '）';
  }
  const STATUS = { answered:'已回复',running: '正在思考…', ready: '样例已就绪，等待你确认', needs_info: '需要你补充信息', enabled: '已启用自动更新', enabling: '正在启用…', failed: '未完成', cancelled: '已停止', revised: '此预览已有新版', context_cleared:'上下文已清空，请重新打开脚本编辑' };

  function toolTrail(job) {
    const open = new Map();
    for (const ev of job.events || []) {
      const id=(ev.turn||0)+':'+ev.call_id;
      if (ev.kind === 'tool_start') open.set(id, {id,name:ev.tool,note:'正在执行…',seconds:null,state:'running'});
      else if (ev.kind === 'tool_end') { const row=open.get(id)||{id,name:ev.tool};row.note=ev.message||'';row.seconds=ev.seconds??null;row.state=ev.warning?'warning':ev.ok===false?'error':'done';open.set(id,row); }
    }
    const rows=[...open.values()];
    if(job.status!=='running')for(const row of rows)if(row.state==='running'){row.state='warning';row.note='本轮已结束，工具未返回完成结果';}
    if (!rows.length) return null;
    const work = el('details', 'cx-work');work.dataset.nodeKey='tools';work.dataset.detailKey=job.id+':tools';work.open=detailStates.get(work.dataset.detailKey)??true;work.append(el('summary', null, `工具活动 · ${rows.length} 次调用`));
    const list = el('div', 'cx-tool-list');
    for (const row of rows) {
      const key=job.id+':'+row.id,fresh=row.state!=='running'&&!completedTools.has(key);
      if(row.state!=='running')completedTools.add(key);
      const line=el('div','cx-tool is-'+row.state+(fresh?' is-fresh':''));line.dataset.toolState=row.state;
      line.dataset.nodeKey='tool:'+row.id;
      const icon=el('span','cx-tool-icon');icon.setAttribute('aria-hidden','true');
      if(row.state==='done')icon.innerHTML='<svg viewBox="0 0 20 20" fill="none"><path d="m4 10 4 4 8-8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      else if(row.state==='error')icon.textContent='×';else if(row.state==='warning')icon.textContent='!';
      const title=el('span','cx-tool-name',row.name);title.title=row.name;
      line.append(icon,title,el('span','cx-tool-note',row.note||'执行完成'),el('span','cx-tool-time',row.seconds!=null?row.seconds+'s':row.state==='running'?'进行中':'已结束'));
      list.append(line);
    }
    work.append(list); return work;
  }

  function previewCard(job) {
    const card = el('div', 'cx-preview');card.dataset.nodeKey='preview';
    if(job.spec?.kind==='report'){
      card.classList.add('cx-report');card.append(el('h3',null,job.spec.title),el('p','cx-meta',job.status==='enabled'?(job.report_delivery&&!job.report_delivery.enabled?'报告发送已暂停，等待确认发送设置。':job.spec.schedule?.mode==='manual'?'报告设置已保存，可按需发送。':job.spec.schedule?.mode==='after_collection'||!job.spec.schedule?'报告已启用，采集成功后发送（原有设置）。':'报告已启用，按独立发送计划运行。'):`报告预览 · ${job.preview.items} 条 · ${job.preview.source_feed||''}`));
      const table=el('table'),head=el('tr');table.dataset.i18nIgnore='';card.querySelector('h3').dataset.i18nIgnore='';for(const label of job.preview.headers||[])head.append(el('th',null,label));table.append(head);
      for(const row of job.preview.preview||[]){const tr=el('tr');for(const label of job.preview.headers||[])tr.append(el('td',null,String(row[label]??'')));table.append(tr);}const scroll=el('div');scroll.style.overflowX='auto';scroll.append(table);card.append(scroll);
      const delivery=job.report_delivery||job.preview.delivery;
      if(delivery&&!delivery.configured){card.append(el('p','cx-note',delivery.missing?.includes('workflow_url')?delivery.workflow_setup_hint:'发送设置尚未齐全，请在对话中补充收件人或发送方式。'));}
      if(job.status==='enabled'&&delivery&&!delivery.enabled){card.append(el('p','cx-note','报告发送计划当前未启用，请补充发送设置并确认。'));}
      if(job.preview.preview_as_of){const zone=job.preview.preview_timezone||job.spec.schedule?.timezone||'Asia/Shanghai';const dateText=value=>new Intl.DateTimeFormat(window.FOSI18n?.locale||'zh-CN',{timeZone:zone,dateStyle:'medium',timeStyle:'short'}).format(new Date(value));card.append(el('p','cx-meta','预览数据截至：'+dateText(job.preview.preview_as_of)+'（'+zone+'）'));if(job.preview.next_send_at)card.append(el('p','cx-meta','下次发送：'+dateText(job.preview.next_send_at)));}
      for(const [name,audit] of Object.entries(job.preview.transformation_audit||{})){const details=el('details');details.append(el('summary',null,'后处理 · '+name+'：'+audit.converted+'条有效，'+audit.missing+'条缺失/未解析（原值保留）'));for(const sample of audit.examples||[])details.append(el('p','cx-note',JSON.stringify(sample.input)+' → '+(sample.output===null?'缺失':String(sample.output))));card.append(details);}
      if(job.preview.warning)card.append(el('p','cx-note',job.preview.warning));
      const reportSchedule=job.spec.schedule||{mode:'after_collection'};
      card.append(el('p','cx-meta','发送计划：'+(reportSchedule.mode==='cron'?schedule(reportSchedule):reportSchedule.mode==='manual'?'仅按需发送':'每次采集成功后发送（原有设置）')));
      const scope=job.spec.range||{mode:'latest'},ranges={all:'全部已保存历史',latest:'最新一次',today:'当天',yesterday:'昨天',last_week:'上一个完整自然周',last_month:'上一个完整自然月'};
      card.append(el('p','cx-meta','数据范围：'+(ranges[scope.mode]||(scope.mode==='days'?'最近'+scope.days+'天'+(scope.include_today===false?'（不含当天）':''):scope.start+' 至 '+scope.end))+' · '+(job.spec.aggregates?.length?'汇总计算':'原始明细/所选字段')));
      const actions=el('div','cx-actions'),download=el('a','btn outline','下载 Excel 预览');download.href=job.report_download_url;download.setAttribute('download','');actions.append(download);
      if(job.status==='enabled'&&delivery&&!delivery.configured){const configure=el('button','btn primary','补充发送设置');configure.type='button';configure.dataset.cxContinue='report';actions.append(configure);}
      if(job.status==='ready'&&!job.spec.schedule){const continueReport=el('button','btn primary','补充报告发送计划');continueReport.type='button';continueReport.dataset.cxContinue='report';actions.append(continueReport);}
      if(job.status==='ready'&&job.spec.schedule){const enable=el('button','btn primary',job.spec.schedule?.mode==='manual'?'确认报告设置':'确认并启用报告计划');enable.type='button';enable.dataset.cxEnable=job.id;actions.append(enable);}
      const test=el('button','btn outline','发送测试报告');test.type='button';test.dataset.reportTest=job.report_test_url;test.disabled=!job.preview.valid||job.preview.items===0||!['ready','enabled','revised'].includes(job.status);actions.append(test);card.append(actions,el('p','cx-note','工作流链接在编辑配置中保存，报告内容和发送时间可在对话中调整。发送测试仅发送一次，不会启用定时报告。工作流接收请求后仍需查看邮件/Teams或工作流运行记录。'));
      return card;
    }
    if (job.status === 'enabled' || job.first_run) {
      const run = job.first_run;
      card.append(el('p', 'cx-meta', run?.status === 'completed' ? '✓ 数据已入库到知识库追踪表，后续按计划自动更新。'
        : run?.status === 'running' ? '已确认，正在首次入库…'
        : run?.status === 'failed' ? '已启用，但首次入库失败：' + (run.error || '') + ' 可在数据源页面重试。' : '已启用自动更新。'));
      if(run?.counts?.warnings)card.append(el('p','cx-badge is-warning',`${run.counts.warnings} 条记录已保留原文，请在追踪表查看采集提示。`));
      const actions = el('div', 'cx-actions');
      const again=el('button','btn primary','继续与 EnGenie 调整任务');again.type='button';again.dataset.cxContinue=job.id;actions.append(again);
      const link = el('a', 'btn outline', '查看数据源'); link.href = '/kb/feeds/'; actions.append(link); card.append(actions);
      if(job.tracker_url){const table=el('a','btn outline','打开追踪表');table.href=job.tracker_url;actions.prepend(table);}
      card.append(el('p','cx-note','此任务的对话与 AI 上下文会保留。以后可从「自动更新」页面继续修改；新方案经你确认后才替换当前脚本。'));
      return card;
    }
    if (!job.preview?.valid) return card;
    card.append(el('h3', null, job.spec.name), el('p', 'cx-meta', `${job.spec.url} · ${schedule(job.spec)} · 试采 ${job.preview.items} 条`));
    const modeField=el('div','field');modeField.dataset.nodeKey='write-mode';
    const modeLabel=el('label',null,'数据写入方式');modeLabel.htmlFor='cx-write-mode-'+job.id;
    const modeSelect=el('select');modeSelect.id=modeLabel.htmlFor;modeSelect.dataset.cxWriteMode=job.id;
    const selectedMode=writeModes.get(job.id)||job.spec.write_mode||'append';
    for(const [value,label] of [['append','追加新记录，保留全部历史（推荐）'],['upsert','按相同 ID 覆盖当前记录']]){const option=el('option',null,label);option.value=value;if(value===selectedMode)option.setAttribute('selected','');modeSelect.append(option);}
    modeSelect.disabled=job.status!=='ready';modeField.append(modeLabel,modeSelect,el('small',null,'AI 已按需求选择，可直接调整。每次采集自动记录时间和来源 ID；切换方式保留已有历史。'));card.append(modeField);
    const selection=el('fieldset','cx-field-selection');selection.dataset.nodeKey='field-selection';
    selection.append(el('legend',null,'选择要保留的字段'),el('p','cx-note','默认全部保留，可多选。采集日期按任务时区自动生成，始终保留；名称、来源 ID 等系统信息仍会保留。'));
    const choices=el('div','cx-field-choices'),selected=selectedFields(job);
    for(const label of availableFields(job)){
      const choice=el('label','cx-field-choice'),input=el('input');input.type='checkbox';input.value=label;input.dataset.cxField=job.id;
      if(selected.has(label))input.setAttribute('checked','');
      input.disabled=job.status!=='ready'||label==='采集日期';
      choice.append(input,el('span',null,label+(label==='采集日期'?' · 必留':'')));choices.append(choice);
    }
    selection.append(choices);card.append(selection);
    const coverage = el('div', 'cx-coverage');
    for (const field of job.preview.field_coverage || []) {
      const badge = el('span', 'cx-badge' + (field.present === field.total ? ' is-complete' : ' is-warning'),
        `${field.label} ${field.present}/${field.total}${field.not_applicable?' · '+field.not_applicable+'条不适用':''}`);
      coverage.append(badge);
    }
    if (job.preview.warning) coverage.append(el('span', 'cx-badge is-warning', job.preview.warning));
    card.append(coverage);
    for (const item of job.preview.preview || []) {
      const sample = el('div', 'cx-sample'); sample.append(el('h4', null, item.title));
      if(item.fields&&Object.keys(item.fields).length){const table=el('table'),body=el('tbody');for(const [label,value] of Object.entries(item.fields)){if(availableFields(job).includes(label)&&!selected.has(label))continue;const row=el('tr');const unit=(job.spec.body_fields||[]).find(field=>field.label===label)?.unit;row.append(el('th',null,label),el('td',null,String(value)+(unit?' '+unit:'')));body.append(row);}table.append(body);sample.append(table);}
      else{const body = el('div'); body.innerHTML = md(item.content); sample.append(body);}card.append(sample);
    }
    const config = el('details');config.dataset.detailKey=job.id+':config';config.open=detailStates.get(config.dataset.detailKey)??false;config.append(el('summary', null, '采集字段与更新计划'));
    const info = el('div', 'cx-config');
    info.append(el('p', null, '采集字段：' + (job.spec.body_fields || []).map(f => f.label + (f.unit ? '（' + f.unit + '）' : '')).join('、')));
    info.append(el('p', null, '记录识别字段：' + (job.spec.id_paths || []).join('、')));
    info.append(el('p', null, '更新计划：' + schedule(job.spec)));
    config.append(info); card.append(config);
    const actions = el('div', 'cx-actions');
    const updating=thread.some(turn=>turn.feed_id);
    const enable = el('button', 'btn primary', updating?'确认修改并更新原脚本':'确认数据并开始自动更新'); enable.type = 'button'; enable.dataset.cxEnable = job.id; enable.disabled = job.status !== 'ready';
    const download = el('a', 'btn outline', '下载采集脚本'); download.href = job.download_url; download.setAttribute('download', '');
    actions.append(enable, download);
    card.append(actions, el('p', 'cx-note', updating?'确认后更新原脚本，按所选方式写入追踪表；已有历史保留。':'确认后在「业务数据」建立采集库与追踪表，并按所选方式定时写入。'));
    return card;
  }

  function turn(job, index) {
    const wrap = el('div', 'cx-turn');
    const user = el('div', 'cx-msg-user');
    user.append(el('div', 'cx-user-text', index === 0 ? job.prompt : (job.feedback || '按原需求重试')));
    const bot = el('div', 'cx-msg-bot'); bot.dataset.status = job.status;
    const head = el('div', 'cx-turn-head');head.dataset.nodeKey='head';
    const avatar=el('span','cx-avatar');avatar.setAttribute('aria-hidden','true');avatar.append(document.getElementById('connectorAvatarTemplate').content.cloneNode(true));if(job.status==='running')avatar.classList.add('is-live');
    head.append(avatar, el('span', 'cx-turn-name', 'EnGenie'), el('span', 'cx-turn-status', job.preview?.answer?'已回复':STATUS[job.status] || job.status));
    bot.append(head);
    const metrics=job.metrics||{};
    if(metrics.context_resumed){const note=el('div','cx-meta',metrics.context_source==='saved'?'已恢复保存的上下文':'已接续上下文与浏览器');note.dataset.nodeKey='context';bot.append(note);}
    if(metrics.calls){const stats=el('div','cx-metrics');stats.dataset.nodeKey='metrics';stats.append(el('span',null,`模型 ${metrics.calls}/${job.limits?.model_calls||50}`),el('span',null,`工具 ${metrics.tools||0}`),el('span',null,`缓存 ${metrics.cache_reported&&metrics.input_tokens?Math.round(metrics.cached_tokens/metrics.input_tokens*100)+'%':'未上报'}`));bot.append(stats);}
    const trail = toolTrail(job); if (trail) bot.append(trail);
    if(job.preview?.answer){const answer=el('div','cx-answer');answer.dataset.nodeKey='answer';answer.innerHTML=md(job.preview.answer);bot.append(answer);}
    else if(job.preview?.setup_required){
      const box=el('div','cx-questions');box.dataset.nodeKey='workflow-settings';box.append(el('p',null,job.preview.setup_hint));
      const url=job.report_delivery?.settings_url||job.preview.settings_url;if(url){const link=el('a','btn primary','打开编辑配置');link.href=url;link.target='_blank';link.rel='noopener';box.append(link);}
      bot.append(box);
    }
    else if (job.questions?.length) {
      const box = el('div', 'cx-questions');box.dataset.nodeKey='questions';
      const hasForm=job.status==='needs_info'&&job.preview?.question_fields?.length;
      box.append(el('p', 'cx-questions-title', hasForm?'还需要补充以下信息：':'我需要补充以下信息，请直接在下方输入框回答：'));
      if(!hasForm){const list=el('ol');for(const q of job.questions||[])list.append(el('li',null,q));box.append(list);}
      if(hasForm){
        const form=el('form','cx-question-form');form.dataset.builderQuestionForm=job.id;
        for(const field of job.preview.question_fields){
          const row=el('div','cx-question-field'),label=el('label',null,field.label),input=el(field.type==='select'?'select':field.type==='textarea'?'textarea':'input');
          input.id='cx-question-'+job.id+'-'+field.key;label.htmlFor=input.id;
          input.name=field.key;input.dataset.questionLabel=field.label;input.required=field.required!==false;
          if(input.tagName==='INPUT'){input.type=field.type||'text';if(field.type==='number')input.step='any';}
          if(!['select','number','date'].includes(field.type))input.maxLength=4000;
          if(field.type==='select'){const empty=el('option',null,'请选择');empty.value='';input.append(empty);for(const value of field.options||[]){const option=el('option',null,value);option.value=value;input.append(option);}}
          row.append(label,input);form.append(row);
        }
        const submit=el('button','btn primary','提交回答');submit.type='submit';form.append(submit,el('p','cx-note','也可以直接在下方聊天框回答。'));box.append(form);
      }
      bot.append(box);
    } else if (job.status === 'failed' || job.status === 'cancelled') {
      const error=el('div', 'cx-error', job.error || '任务已停止。可以继续对话重试。');error.dataset.nodeKey='error';bot.append(error);
    } else if (job.status==='enabled' || job.preview?.valid || job.first_run) {
      bot.append(previewCard(job));
    } else {
      const text = (job.events || []).filter(ev => ev.kind === 'token').map(ev => ev.text).join('').trim();
      if (text) { const answer = el('div', 'cx-answer');answer.dataset.nodeKey='answer'; answer.innerHTML = md(text); bot.append(answer); }
      else if (job.status === 'running') {
        const phase = [...(job.events || [])].reverse().find(ev => ev.kind === 'phase' || ev.kind === 'notice');
        const answer=el('div', 'cx-answer cx-plain', phase ? phase.message : '正在读取页面并分析取数方式…');answer.dataset.nodeKey='answer';answer.dataset.i18nUi='';bot.append(answer);
      }
    }
    if (job.browser_preview_url) {
      const shot = el('a', 'cx-shot');shot.dataset.nodeKey='browser'; shot.href = job.browser_preview_url; shot.target = '_blank'; shot.rel = 'noopener noreferrer';
      const img = el('img'); img.src = job.browser_preview_url; img.alt = '浏览器采集画面'; img.loading = 'lazy'; shot.append(img); bot.append(shot);
    }
    wrap.dataset.jobId=job.id;wrap.append(user, bot); return wrap;
  }

  const nodeKey=node=>node.nodeType===1?(node.dataset.nodeKey||node.dataset.jobId||''):'';
  function patchNode(current,next){
    if(current.nodeType===3){if(current.data!==next.data)current.data=next.data;return;}
    if(current.nodeType!==1)return;
    // 不反复设置相同class，不移动现有节点，让正在执行的动画连续运行。
    for(const attr of [...current.attributes])if(!next.hasAttribute(attr.name))current.removeAttribute(attr.name);
    for(const attr of [...next.attributes]){
      let value=attr.value;
      if(attr.name==='class'&&current.classList.contains('is-fresh')&&current.dataset.toolState===next.dataset.toolState&&!next.classList.contains('is-fresh'))value+=' is-fresh';
      if(current.getAttribute(attr.name)!==value)current.setAttribute(attr.name,value);
    }
    patchChildren(current,[...next.childNodes]);
  }
  function patchChildren(parent,desired){
    const old=[...parent.childNodes],keyed=new Map(old.filter(node=>nodeKey(node)).map(node=>[nodeKey(node),node])),used=new Set();
    desired.forEach((next,index)=>{
      const key=nodeKey(next);let current=key?keyed.get(key):old[index];
      if(current&&(used.has(current)||(!key&&nodeKey(current))||current.nodeType!==next.nodeType||current.nodeName!==next.nodeName))current=null;
      if(current){patchNode(current,next);used.add(current);}else current=next;
      if(parent.childNodes[index]!==current)parent.insertBefore(current,parent.childNodes[index]||null);
    });
    for(const node of old)if(!used.has(node)&&node.parentNode===parent)node.remove();
  }
  function render() {
    const nearBottom=messages.scrollHeight-messages.scrollTop-messages.clientHeight<90,scroll=messages.scrollTop;
    for(const detail of messages.querySelectorAll('details[data-detail-key]'))detailStates.set(detail.dataset.detailKey,detail.open);
    if (!thread.length) { messages.replaceChildren(welcome); welcome.querySelectorAll('.cx-prompt').forEach(b => { b.hidden = false; }); return; }
    const nodes=thread.map((job,index)=>{const signature=JSON.stringify([job.status,job.event_cursor,job.first_run,job.error,job.metrics,job.report_delivery,window.FOSI18n?.language,[...selectedFields(job)]]);const previous=[...messages.children].find(node=>node.dataset.jobId===job.id);if(previous?.dataset.signature===signature)return previous;const node=turn(job,index);node.dataset.signature=signature;return node;});
    const old=[...messages.children];
    nodes.forEach((node,index)=>{const previous=old.find(item=>item.dataset.jobId===node.dataset.jobId);if(previous&&previous!==node)patchNode(previous,node);const current=previous||node;if(messages.children[index]!==current)messages.insertBefore(current,messages.children[index]||null);});
    for(const node of old)if(!thread.some(job=>job.id===node.dataset.jobId))node.remove();
    messages.scrollTop=nearBottom?messages.scrollHeight:scroll;
  }
  document.addEventListener('fos:languagechange',()=>{render();syncComposer(thread.at(-1));});
  function syncComposer(job) {
    const running = job?.status === 'running';
    send.disabled=submitting||stopping||confirming||clearing;prompt.disabled=running||submitting||confirming||clearing;newTask.disabled=running||submitting||stopping||confirming||clearing;
    clearContext.hidden=!page.dataset.feedId&&!thread.some(turn=>turn.feed_id);
    clearContext.disabled=running||job?.status==='enabling'||submitting||stopping||confirming||clearing;
    clearContext.textContent=clearing?'正在清空…':'清空上下文';clearContext.setAttribute('aria-busy',String(clearing));
    const state=stopping?'stopping':submitting||confirming?'submitting':running?'running':'idle';send.dataset.state=state;
    const label=stopping?'正在停止…':confirming?'正在确认…':submitting?'正在发送…':running?'停止任务':'发送';send.setAttribute('aria-label',label);send.title=label;send.querySelector('.cx-sr-only').textContent=label;send.setAttribute('aria-busy',String(submitting||stopping||confirming));
    prompt.maxLength=job?4000:10000;
    prompt.placeholder = running ? '任务执行中，点击右侧停止按钮可中止'
      : job?.status === 'needs_info' ? '回答上面 EnGenie 的问题，Enter 发送…'
      : job?.status === 'answered' ? '继续提问，或告诉 EnGenie 想怎样调整任务…'
      : job?.status === 'ready' ? '想调整哪里？直接说，例如：标题改用设备名；加一个持续时长字段…'
      : job?.status === 'enabled' ? '告诉 EnGenie 想怎样调整这个任务…'
      : '告诉 EnGenie 数据在哪、要采集什么、多久更新一次…';
    hint.textContent = job?.status==='answered'?'可以直接提问；只有要求创建或修改任务时才生成新方案。':job?.spec?.kind==='report' ? '报告预览已就绪。确认数据范围、处理规则和独立发送计划，也可先发送一次测试。' : job?.status === 'needs_info' ? '你的回答会连同原始需求一起交给 EnGenie，继续完善任务。'
      : job?.status === 'ready' ? '确认无误后点击样例卡片中的「确认数据并开始自动更新」。'
      : job?.status === 'enabled' ? '可继续修改当前脚本；新版本验证并经你确认后更新原数据源，不重复创建知识库。'
      : '先查看样例，再确认启用 · Enter 发送，Shift+Enter 换行';
  }
  function activate(jobs) {
    for(const job of jobs||[])for(const event of job.events||[])if(event.kind==='tool_end')completedTools.add(job.id+':'+(event.turn||0)+':'+event.call_id);
    thread = (jobs||[]).map(mergeJob); generation++; clearTimeout(timer); render();
    const last = jobs.at(-1); syncComposer(last);
    if(last)poll(thread.at(-1),generation);
  }
  async function poll(job, gen) {
    clearTimeout(timer);
    try {
      const url=new URL(job.status_url,location.origin);if(job.event_cursor)url.searchParams.set('after',job.event_cursor);
      const data = await request(url);
      if (gen !== generation) return;
      thread[thread.length - 1] = mergeJob(data.job); render(); syncComposer(data.job);
      if(data.job.status==='enabled'&&data.job.feed_id){page.dataset.feedId=data.job.feed_id;const url=new URL(location.href);url.searchParams.set('feed',data.job.feed_id);history.replaceState(null,'',url);}
      timer = setTimeout(() => poll(data.job, gen), data.job.has_more_events?100:data.job.status==='running'||data.job.first_run?.status==='running'?1500:15000);
    } catch (error) { if (gen === generation){hint.textContent='进度连接中断，正在重连；不会重新启动任务。'+error.message;timer = setTimeout(() => poll(job, gen), 4000);} }
  }

  composer.addEventListener('submit', async event => {
    event.preventDefault();
    if(thread.at(-1)?.status==='running'){await stopJob();return;}
    const text = prompt.value.trim(); if (!text || send.disabled) return;
    const submittedGeneration=generation;let failure='';
    submitting=true;syncComposer(thread.at(-1));
    try {
      const last = thread.at(-1);
      if (last && !['answered','ready', 'needs_info', 'failed', 'cancelled','enabled'].includes(last.status)) throw new Error('当前任务尚未结束，请稍候或先停止。');
      const data = last ? { feedback: text } : { prompt: text };
      if(last&&composer.questionAnswers?.job===last.id)data.question_answers=composer.questionAnswers.answers;
      const response = await request(last ? last.refine_url : page.dataset.createUrl, data);
      if(submittedGeneration!==generation)return;
      if(last&&last.status!=='enabled')last.status='revised';
      delete composer.questionAnswers;prompt.value = ''; prompt.style.height = 'auto';
      thread.push(mergeJob(response.job)); generation++; clearTimeout(timer); render(); syncComposer(response.job);
      poll(response.job, generation);
    } catch (error) { failure=error.message; } finally { submitting=false;syncComposer(thread.at(-1));if(failure)hint.textContent=failure; }
  });
  prompt.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode!==229) { event.preventDefault(); composer.requestSubmit(); } });
  prompt.addEventListener('input', () => { delete composer.questionAnswers;prompt.style.height = 'auto'; prompt.style.height = Math.min(prompt.scrollHeight, 160) + 'px'; });
  messages.addEventListener('click', async event => {
    const again=event.target.closest('[data-cx-continue]');
    if(again){prompt.focus();prompt.placeholder=again.dataset.cxContinue==='report'?'告诉 EnGenie 报告的数据范围、处理方式和发送时间…':'告诉 EnGenie 想调整哪些字段、采集方式或更新时间…';return;}
    const enable = event.target.closest('[data-cx-enable]'); if (!enable) return;
    const job = thread.at(-1); if (!job||enable.dataset.cxEnable!==job.id) return;
    if(confirming)return;
    const confirmedGeneration=generation;let failure='';confirming=true;clearTimeout(timer);enable.disabled=true;enable.textContent='正在确认…';syncComposer(job);
    try {
      await request(job.enable_url, {write_mode:writeModes.get(job.id)||job.spec.write_mode||'append',selected_fields:[...selectedFields(job)]});
      if(confirmedGeneration===generation)await poll(job,confirmedGeneration);
    } catch (error) { failure=error.message;enable.disabled=false;enable.textContent='重新确认'; }
    finally{confirming=false;syncComposer(thread.at(-1));if(failure)hint.textContent=failure;}
  });
  messages.addEventListener('click', event => {
    const card = event.target.closest('.cx-prompt'); if (!card) return;
    prompt.value = card.dataset.prompt || ''; prompt.focus(); prompt.dispatchEvent(new Event('input'));
  });
  async function stopJob(){
    const job=thread.at(-1);if(!job||stopping)return;stopping=true;syncComposer(job);
    try{const data=await request(job.cancel_url,{});generation++;clearTimeout(timer);thread[thread.length-1]=mergeJob(data.job);render();}
    catch(error){hint.textContent=error.message;}finally{stopping=false;syncComposer(thread.at(-1));}
  }
  function forget(){
    if(!thread.length||thread.some(job=>job.status==='running'))return Promise.resolve();
    return fetch(page.dataset.forgetUrl,{method:'POST',keepalive:true,headers:{'Content-Type':'application/json','X-CSRFToken':csrf(),'X-Requested-With':'XMLHttpRequest'},body:JSON.stringify({ids:thread.map(job=>job.id)})});
  }
  newTask.addEventListener('click',async()=>{
    if(newTask.disabled)return;newTask.disabled=true;
    try{const response=await forget();if(response&&!response.ok)throw new Error('临时记录清理失败，请稍后重试。');generation++;clearTimeout(timer);thread=[];eventCache.clear();detailStates.clear();completedTools.clear();writeModes.clear();page.dataset.feedId='';prompt.value='';const url=new URL(location.href);url.searchParams.delete('feed');history.replaceState(null,'',url);page.querySelector('.cx-heading h1').textContent='EnGenie · 自动任务';page.querySelector('.cx-heading p').textContent='告诉我数据在哪，剩下的我们一起完成。';render();syncComposer(null);prompt.focus();}
    catch(error){hint.textContent=error.message;newTask.disabled=false;}
  });
  clearContext.addEventListener('click',async()=>{
    if(clearContext.disabled||clearing)return;
    const feed=thread.findLast(turn=>turn.feed_id)?.feed_id||page.dataset.feedId;if(!feed)return;
    clearing=true;generation++;clearTimeout(timer);syncComposer(thread.at(-1));hint.textContent='正在清空历史对话和工具上下文…';
    let message='',failure='';
    try{
      const data=await request(page.dataset.clearContextUrl,{feed});
      eventCache.clear();detailStates.clear();completedTools.clear();writeModes.clear();
      page.dataset.feedId=feed;activate(data.thread);message=data.message;
    }catch(error){failure=error.message;const last=thread.at(-1);if(last)poll(last,generation);}
    finally{clearing=false;syncComposer(thread.at(-1));hint.textContent=failure||message;prompt.focus();}
  });
  window.addEventListener('pagehide',()=>{forget().catch(()=>{});});
  syncComposer(null);
  if(page.dataset.feedId){
    submitting=true;syncComposer(null);hint.textContent='正在恢复任务对话与采集脚本…';
    let failure='';
    request(page.dataset.resumeUrl,{feed:page.dataset.feedId}).then(data=>activate(data.thread))
      .catch(error=>{failure=error.message;}).finally(()=>{submitting=false;syncComposer(thread.at(-1));if(failure)hint.textContent=failure;});
  }
})();
