(function (root) {
  'use strict';
  const LABELS = {query_plan:'Understanding your question',hybrid_search:'Searching sources',answer_generation:'Generating answer',answer_verification:'Checking answer'};
  const TOOLS = {list_knowledge_bases:'List knowledge bases',kb_relationships:'Read knowledge base relationships',kb_graph_search:'Search verified facts',
    kb_search:'Search knowledge base',kb_read_chunks:'Read source passages',kb_select_evidence:'Select citation evidence',kb_image_search:'Search images',
    kb_fetch_doc:'Read document content',kb_neighbor_chunk:'Read neighboring passages',tracker_preview:'Preview tracksheet',tracker_lookup:'Query tracker records',
    tracker_analyze:'Analyze tracker records',tracker_visualize:'Create interactive data view',write_analysis:'Generate analysis',check_analysis:'Check analysis result',request_verified_export:'Prepare table export'};
  const CHECK = '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="m4 10 4 4 8-8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const clean = value => String(value || '').replace(/[.…]+$/, '');
  const english = (value, fallback) => {
    const source = clean(value);
    if (!source) return fallback;
    const translated = root.FosToolText?.message
      ? root.FosToolText.message(source, fallback) : (root.FOSI18n?.t(source, 'en') ?? source);
    return translated && !/[\u3400-\u9fff\uf900-\ufaff]/.test(translated) ? translated : fallback;
  };
  const toolName = (tool, label) => root.FosToolText?.name
    ? english(root.FosToolText.name(tool, label), TOOLS[tool] || 'Run tool')
    : (TOOLS[tool] || english(label, 'Run tool'));

  class ActivityState {
    constructor(now = () => performance.now()) {
      this.now=now;this.started=now();this.rows=new Map();this.serial=0;this.serverElapsed=0;
      this.status='running';this.label='Preparing answer';this.note='';this.answering=false;
    }
    update(type, payload = {}) {
      if(this.status!=='running')return;
      if(type==='heartbeat') {
        this.label=english(payload.stage,'Processing request');
        const seconds=Number(payload.elapsed);if(Number.isFinite(seconds)){
          this.serverElapsed=Math.max(this.serverElapsed,seconds);
          this.started=Math.min(this.started,this.now()-this.serverElapsed*1000);
        }
      } else if(type==='reasoning') {
        const note=String(payload.text||'').trim().split('\n')[0].slice(0,160);
        this.note=note ? english(note,'Processing request') : '';
      } else if(type==='step') {
        if(payload.status==='start')this.label=LABELS[payload.stage]||'Processing request';
        if(payload.status==='done'&&payload.ok===false)this.finish('error','Answer verification failed');
      } else if(type==='token') {
        if(String(payload.text||'').trim())this.answering=true;
        this.label='Generating answer';this.note='';
      } else if(type==='tool_start'||type==='tool_end') {
        let id=payload.run_id;
        if(!id&&type==='tool_end')id=[...this.rows.values()].find(row=>row.name===payload.tool&&row.state==='running')?.id;
        id=id||'activity-'+(++this.serial);
        let row=this.rows.get(id);
        if(!row){row={id,name:payload.tool||'',label:toolName(payload.tool,payload.label),
          started:this.now(),state:'running',note:'Running…',seconds:null};this.rows.set(id,row);}
        if(type==='tool_start') {
          this.answering=false;this.label=row.label;this.note='';
        } else if(row.state==='running') {
          row.state=payload.warning?'warning':payload.ok===false||['failed','error'].includes(payload.status)?'error':'done';
          const seconds=Number(payload.seconds);
          row.seconds=payload.seconds!=null&&Number.isFinite(seconds)?Math.max(0,seconds):Math.max(0,(this.now()-row.started)/1000);
          row.note=english(payload.message,({done:'Completed',warning:'Completed with warnings',error:'Tool execution failed'})[row.state]);
          this.label=[...this.rows.values()].find(item=>item.state==='running')?.label||'Organizing results';
        }
      } else if(type==='verify'&&payload.ok===false) this.finish('error','Answer verification failed');
    }
    elapsed(){return Math.max(this.serverElapsed,(this.now()-this.started)/1000);}
    finish(status='done', note='') {
      if(this.status!=='running')return;
      this.seconds=this.elapsed();this.status=status;this.note='';
      this.label=english(note,({done:'Answer complete',error:'Response incomplete',stopped:'Response stopped'})[status]||'Response complete');
      for(const row of this.rows.values())if(row.state==='running'){
        row.state=status==='error'?'error':'warning';row.seconds=Math.max(0,(this.now()-row.started)/1000);
        row.note=status==='done'?'Response ended; the tool did not return a completion result':'Response interrupted; the tool did not return a completion result';
      }
    }
  }

  function create(host, options = {}) {
    const doc=host.ownerDocument, state=new ActivityState(options.now);
    const el=(tag,cls,text)=>{const node=doc.createElement(tag);node.className=cls||'';if(text!=null)node.textContent=text;return node;};
    const rootEl=el('div','rag-activity'),status=el('div','rag-activity-status'),icon=el('span','rag-activity-icon'),
      label=el('span','rag-activity-label'),elapsed=el('span','rag-activity-time'),note=el('p','rag-activity-note'),
      work=el('details','rag-work'),summary=el('summary'),list=el('div','rag-tool-list');
    rootEl.setAttribute('translate','no');rootEl.setAttribute('lang','en');
    icon.setAttribute('aria-hidden','true');elapsed.setAttribute('aria-hidden','true');label.setAttribute('role','status');
    work.open=true;work.hidden=true;note.hidden=true;list.setAttribute('role','list');
    status.append(icon,label,elapsed);work.append(summary,list);rootEl.append(status,note,work);host.append(rootEl);
    const nodes=new Map(),disclosure=new root.FosToolDisclosure();let timer=null;
    const text=(node,value)=>{if(node.textContent!==value)node.textContent=value;};
    const duration=seconds=>seconds<10?seconds.toFixed(1)+'s':Math.floor(seconds)+'s';
    list.addEventListener('animationend',event=>{if(event.animationName==='rag-tool-complete')event.target.closest('.rag-tool')?.classList.remove('is-fresh');});
    function render() {
      if(rootEl.dataset.state!==state.status){
        rootEl.dataset.state=state.status;icon.innerHTML=state.status==='done'?CHECK:state.status==='running'?'':state.status==='error'?'×':'!';
      }
      text(label,state.label);text(elapsed,duration(state.status==='running'?state.elapsed():state.seconds));
      text(note,state.note);note.hidden=!state.note;work.hidden=!state.rows.size;
      text(summary,'Tool activity · '+state.rows.size+(state.rows.size===1?' call':' calls'));
      const pending=[...state.rows.values()].some(row=>row.state==='running');
      const answering=(state.status==='done'||state.answering)&&!pending&&!['error','stopped'].includes(state.status);
      const open=disclosure.update(answering?'answer':'tools',work.open);if(work.open!==open)work.open=open;
      for(const row of state.rows.values()){
        let entry=nodes.get(row.id);
        if(!entry){
          const node=el('div','rag-tool'),mark=el('span','rag-tool-icon'),name=el('span','rag-tool-name',row.label),
            detail=el('span','rag-tool-note'),time=el('span','rag-tool-time');
          node.setAttribute('role','listitem');mark.setAttribute('aria-hidden','true');time.setAttribute('aria-hidden','true');
          node.append(mark,name,detail,time);list.append(node);entry={node,mark,detail,time};nodes.set(row.id,entry);
        }
        if(entry.node.dataset.state!==row.state){
          entry.node.dataset.state=row.state;entry.node.className='rag-tool is-'+row.state+(row.state!=='running'?' is-fresh':'');
          entry.mark.innerHTML=row.state==='done'?CHECK:row.state==='running'?'':row.state==='error'?'×':'!';
        }
        text(entry.detail,row.note);
        text(entry.time,duration(row.seconds==null?Math.max(0,(state.now()-row.started)/1000):row.seconds));
      }
      if(state.status!=='running'&&timer!=null){clearInterval(timer);timer=null;}
    }
    render();
    if(options.ticking!==false)timer=setInterval(()=>{if(!rootEl.isConnected){clearInterval(timer);timer=null;return;}render();},250);
    return {state,update(type,payload){state.update(type,payload);render();},
      finish(status,note){state.finish(status,note);render();},dispose(){if(timer!=null)clearInterval(timer);timer=null;}};
  }
  root.FosToolActivity={create,ActivityState};
})(globalThis);
