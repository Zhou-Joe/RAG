(() => {
  'use strict';
  const names={browser_open:'Open webpage',browser_snapshot:'Inspect page structure',browser_action:'Interact with webpage',browser_network:'Inspect network data',browser_read_response:'Read API response',browser_extract:'Extract page fields',test_browser_connector:'Replay and validate collection',probe_api:'Test API data',search_api_docs:'Search documentation',read_api_documentation:'Read documentation',inspect_connector_environment:'Check collection environment',test_mapping:'Validate data mapping',show_data_preview:'Preview collected data',preview_triggered_report:'Preview conditional delivery',preview_scheduled_report:'Preview scheduled report'};
  const english=(value,fallback)=>window.FosToolText?.message(value,fallback)||(/[\u3400-\u9fff]/u.test(String(value||''))?fallback:String(value||fallback));
  const toolName=tool=>tool==='preview_engenie_digest'?'Preview EnGenie Digest':window.FosToolText?.name(tool)||names[tool]||tool||'Run tool';
  const node=(tag,className,text)=>{const el=document.createElement(tag);el.setAttribute('translate','no');if(className)el.className=className;if(text!==undefined)el.textContent=text;return el;};
  const number=value=>new Intl.NumberFormat('en-US',{notation:value>99999?'compact':'standard',maximumFractionDigits:1}).format(value || 0);
  const duration=value=>value<60?Math.round(value)+'s':Math.floor(value/60)+'m '+Math.round(value%60)+'s';
  class ConnectorActivity {
    constructor(){
      this.timeline=document.getElementById('connectorTimeline');this.log=document.getElementById('connectorLog');this.job=null;this.seen=new Set();this.turns=new Map();this.tools=new Map();this.cursor=0;this.older=null;
      this.clock=setInterval(()=>{if(this.current?.status==='running')this.metric('elapsed',duration((Date.now()-Date.parse(this.current.created_at))/1000));},1000);
    }
    metric(name,text){document.querySelector(`[data-metric="${name}"]`).textContent=text;}
    turn(id){
      if(this.turns.has(id))return this.turns.get(id);
      const card=node('article','agent-turn');card.dataset.turn=id;
      const heading=node('div','agent-turn-heading');heading.append(node('span','agent-turn-number',String(id || '·').padStart(2,'0')),node('strong','',id?'Model round '+id:'Preparation and notices'));
      const timing=node('span','agent-turn-timing');heading.append(timing);const body=node('div','agent-turn-body');card.append(heading,body);
      const next=[...this.turns.entries()].filter(([key])=>Number(key)>Number(id)).sort((a,b)=>a[0]-b[0])[0]?.[1].card;
      this.timeline.insertBefore(card,next || null);const value={card,body,timing};this.turns.set(id,value);return value;
    }
    render(job){
      if(this.job!==job.id){this.job=job.id;this.seen.clear();this.turns.clear();this.tools.clear();this.cursor=0;this.older=null;this.oldestLoaded=null;this.validating=false;this.timeline.replaceChildren();this.log.textContent='';document.getElementById('connectorOlder').hidden=true;document.getElementById('connectorObservation').textContent='清洗 DOM';}
      this.current=job;this.cursor=Math.max(this.cursor,job.event_cursor || 0);
      if(job.older_event_cursor)this.older=this.older?Math.min(this.older,job.older_event_cursor):job.older_event_cursor;
      if(job.older_event_cursor!=null&&(this.oldestLoaded==null || job.older_event_cursor<=this.oldestLoaded)){document.getElementById('connectorOlder').hidden=!job.has_older_events;this.oldestLoaded=job.older_event_cursor;}
      const metrics=job.metrics || {}, running=job.status==='running';
      document.getElementById('connectorSignal').classList.toggle('is-running',running);
      document.getElementById('connectorLiveState').textContent=({running:'正在执行',ready:'等待你的审核',enabled:'已启用',needs_info:'需要补充信息',failed:'未完成',cancelled:'已停止',revised:'已有新版'})[job.status] || job.status;
      this.metric('calls',`${metrics.calls ?? '—'} / ${job.limits.model_calls}`);this.metric('tools',`${metrics.tools ?? '—'} / ${job.limits.tool_calls}`);
      this.metric('elapsed',metrics.elapsed_seconds!==undefined?duration(metrics.elapsed_seconds):running?duration((Date.now()-Date.parse(job.created_at))/1000):'历史任务未记录');
      this.metric('tokens',metrics.input_tokens?`${number(metrics.input_tokens)} / ${number(metrics.output_tokens)}`:'服务未上报');
      this.metric('cache',metrics.cache_reported?`${metrics.input_tokens?Math.round(100*metrics.cached_tokens/metrics.input_tokens):0}% · ${number(metrics.cached_tokens)} token`:'服务未上报');
      this.metric('compactions',`${metrics.context_compactions || 0} / ${metrics.tool_cache_hits || 0}`);
      const atBottom=this.timeline.scrollHeight-this.timeline.scrollTop-this.timeline.clientHeight<70;
      let fallback=0;
      for(const event of job.events || []){
        const key=event.id || 'legacy-'+fallback++;if(this.seen.has(key))continue;this.seen.add(key);
        if(event.kind==='token'){this.log.append(document.createTextNode(event.text || ''));continue;}
        const turn=this.turn(event.turn || 0);
        if(event.kind==='model_request')turn.timing.textContent='Waiting for model response…';
        else if(event.kind==='model_activity')turn.timing.textContent=`First response ${event.first_response_seconds}s · Generating`;
        else if(event.kind==='model_timing'){turn.timing.textContent=`${event.failed?'Request interrupted':'Model completed'} · ${event.seconds}s`;turn.card.classList.toggle('is-model-failed',!!event.failed);}
        else if(event.kind==='tool_start'){
          const details=node('details','agent-tool is-running');const summary=node('summary');const icon=node('span','agent-tool-icon','↗'),label=node('span','agent-tool-name',toolName(event.tool)),state=node('span','agent-tool-state','Running');const message=node('span','agent-tool-result','Running…');summary.append(icon,label,state,message);details.append(summary);
          const args=node('pre','agent-tool-arguments',JSON.stringify(event.arguments || {},null,2));details.append(args);turn.body.append(details);
          this.tools.set(event.call_id || `${event.turn}:${event.tool}`,{details,message,state});
          if(event.tool==='test_browser_connector')this.validating=true;
        } else if(event.kind==='tool_end'){
          const tool=this.tools.get(event.call_id || `${event.turn}:${event.tool}`);
          if(tool){tool.details.className='agent-tool '+(!event.ok?'is-failed':event.warning?'is-warning':'is-done');tool.message.textContent=english(event.message,!event.ok?'Failed':event.warning?'Completed with a warning':'Completed');tool.state.textContent=`${!event.ok?'Failed':event.warning?'Warning':'Completed'}${event.seconds!==undefined?' · '+event.seconds+'s':''}`;if(event.details&&Object.keys(event.details).length)tool.details.append(node('pre','agent-tool-diagnostic',JSON.stringify(event.details,null,2)));}
          else turn.body.append(node('p','agent-notice '+(!event.ok?'is-failed':''),toolName(event.tool)+' · '+english(event.message,!event.ok?'Failed':'Completed')));
        } else if(event.kind==='browser_view'){
          document.getElementById('connectorBrowserAddress').textContent=event.url || job.api_url;
          document.getElementById('connectorObservation').textContent=event.observation==='verified_sample'?'已验证的试采画面':event.image_pending?(running?'DOM + 截图待发送':'网页截图 · 本轮已结束'):'清洗 DOM';
        } else if(event.kind==='vision_received')document.getElementById('connectorObservation').textContent='模型已接收 DOM + 截图';
        else if(['notice','validation','context','preview','complete'].includes(event.kind))turn.body.append(node('p','agent-notice'+(event.kind==='validation'?' is-warning':''),english(event.message,event.kind==='validation'?'Check the task validation result':'Task updated')));
      }
      if(!running)for(const tool of this.tools.values())if(tool.details.classList.contains('is-running')){tool.details.classList.remove('is-running');tool.state.textContent='Task ended';}
      for(const turn of this.turns.values())turn.card.hidden=!turn.body.childElementCount&&!turn.timing.textContent;
      if(atBottom&&running)this.timeline.scrollTop=this.timeline.scrollHeight;
      document.getElementById('connectorOutputCount').textContent=number(this.log.textContent.length)+' 字';
      const stage=job.status==='enabled'?3:job.preview?.valid?2:this.validating?1:0;
      document.querySelectorAll('#connectorStages li').forEach((el,index)=>{el.classList.toggle('is-current',index===stage);el.classList.toggle('is-complete',index<stage);});
    }
  }
  window.ConnectorActivity=ConnectorActivity;
})();
