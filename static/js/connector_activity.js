(() => {
  'use strict';
  const names={browser_open:'打开网页',browser_snapshot:'观察页面结构',browser_action:'操作网页',browser_network:'检查网络数据',browser_read_response:'读取接口响应',browser_extract:'提取页面字段',test_browser_connector:'重放脚本并试采',probe_api:'测试接口数据',search_api_docs:'搜索文档',read_api_documentation:'阅读文档',inspect_connector_environment:'检查采集环境',test_mapping:'验证数据格式',show_data_preview:'展示真实样例'};
  const node=(tag,className,text)=>{const el=document.createElement(tag);if(className)el.className=className;if(text!==undefined)el.textContent=text;return el;};
  const number=value=>new Intl.NumberFormat(globalThis.FOSI18n?.locale || 'zh-CN',{notation:value>99999?'compact':'standard',maximumFractionDigits:1}).format(value || 0);
  const duration=value=>value<60?Math.round(value)+'秒':Math.floor(value/60)+'分'+Math.round(value%60)+'秒';
  class ConnectorActivity {
    constructor(){
      this.timeline=document.getElementById('connectorTimeline');this.log=document.getElementById('connectorLog');this.job=null;this.seen=new Set();this.turns=new Map();this.tools=new Map();this.cursor=0;this.older=null;
      this.clock=setInterval(()=>{if(this.current?.status==='running')this.metric('elapsed',duration((Date.now()-Date.parse(this.current.created_at))/1000));},1000);
    }
    metric(name,text){document.querySelector(`[data-metric="${name}"]`).textContent=text;}
    turn(id){
      if(this.turns.has(id))return this.turns.get(id);
      const card=node('article','agent-turn');card.dataset.turn=id;
      const heading=node('div','agent-turn-heading');heading.append(node('span','agent-turn-number',String(id || '·').padStart(2,'0')),node('strong','',id?'模型第 '+id+' 轮':'准备与通知'));
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
        if(event.kind==='model_request')turn.timing.textContent='等待模型响应…';
        else if(event.kind==='model_activity')turn.timing.textContent=`首响应 ${event.first_response_seconds}秒 · 生成中`;
        else if(event.kind==='model_timing'){turn.timing.textContent=`${event.failed?'请求中断':'模型完成'} · ${event.seconds}秒`;turn.card.classList.toggle('is-model-failed',!!event.failed);}
        else if(event.kind==='tool_start'){
          const details=node('details','agent-tool is-running');const summary=node('summary');const icon=node('span','agent-tool-icon','↗'),label=node('span','agent-tool-name',names[event.tool] || event.tool),state=node('span','agent-tool-state','执行中');const message=node('span','agent-tool-result','正在执行…');summary.append(icon,label,state,message);details.append(summary);
          const args=node('pre','agent-tool-arguments',JSON.stringify(event.arguments || {},null,2));details.append(args);turn.body.append(details);
          this.tools.set(event.call_id || `${event.turn}:${event.tool}`,{details,message,state});
          if(event.tool==='test_browser_connector')this.validating=true;
        } else if(event.kind==='tool_end'){
          const tool=this.tools.get(event.call_id || `${event.turn}:${event.tool}`);
          if(tool){tool.details.className='agent-tool '+(!event.ok?'is-failed':event.warning?'is-warning':'is-done');tool.message.textContent=event.message || '';tool.state.textContent=`${!event.ok?'未通过':event.warning?'请留意':'完成'}${event.seconds!==undefined?' · '+event.seconds+'秒':''}`;if(event.details&&Object.keys(event.details).length)tool.details.append(node('pre','agent-tool-diagnostic',JSON.stringify(event.details,null,2)));}
          else turn.body.append(node('p','agent-notice '+(!event.ok?'is-failed':''),(names[event.tool] || event.tool)+' · '+event.message));
        } else if(event.kind==='browser_view'){
          document.getElementById('connectorBrowserAddress').textContent=event.url || job.api_url;
          document.getElementById('connectorObservation').textContent=event.observation==='verified_sample'?'已验证的试采画面':event.image_pending?(running?'DOM + 截图待发送':'网页截图 · 本轮已结束'):'清洗 DOM';
        } else if(event.kind==='vision_received')document.getElementById('connectorObservation').textContent='模型已接收 DOM + 截图';
        else if(['notice','validation','context','preview','complete'].includes(event.kind))turn.body.append(node('p','agent-notice'+(event.kind==='validation'?' is-warning':''),event.message));
      }
      if(!running)for(const tool of this.tools.values())if(tool.details.classList.contains('is-running')){tool.details.classList.remove('is-running');tool.state.textContent='任务已结束';}
      for(const turn of this.turns.values())turn.card.hidden=!turn.body.childElementCount&&!turn.timing.textContent;
      if(atBottom&&running)this.timeline.scrollTop=this.timeline.scrollHeight;
      document.getElementById('connectorOutputCount').textContent=number(this.log.textContent.length)+' 字';
      const stage=job.status==='enabled'?3:job.preview?.valid?2:this.validating?1:0;
      document.querySelectorAll('#connectorStages li').forEach((el,index)=>{el.classList.toggle('is-current',index===stage);el.classList.toggle('is-complete',index<stage);});
    }
  }
  window.ConnectorActivity=ConnectorActivity;
})();
