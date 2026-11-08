(function (root) {
  'use strict';
  const LABELS = {query_plan:'理解问题',hybrid_search:'检索资料',answer_generation:'生成回答',answer_verification:'核对答案'};
  const TOOLS = {list_knowledge_bases:'查看知识库',kb_relationships:'查看知识库关联',kb_graph_search:'查询已确认事实',
    kb_search:'检索知识库',kb_read_chunks:'读取原文片段',kb_select_evidence:'选择引用依据',kb_image_search:'查找图片',
    kb_fetch_doc:'查找文档内容',kb_neighbor_chunk:'读取相邻片段',tracker_preview:'查看追踪表',tracker_lookup:'查询追踪数据',
    write_analysis:'生成分析',check_analysis:'等待分析结果',request_verified_export:'准备表格导出'};
  const CHECK = '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="m4 10 4 4 8-8" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const clean = value => String(value || '').replace(/[.…]+$/, '');

  class ActivityState {
    constructor(now = () => performance.now()) {
      this.now=now;this.started=now();this.rows=new Map();this.serial=0;this.serverElapsed=0;
      this.status='running';this.label='正在准备回答';this.note='';this.answering=false;
    }
    update(type, payload = {}) {
      if(this.status!=='running')return;
      if(type==='heartbeat') {
        this.label=clean(payload.stage)||'正在处理';
        const seconds=Number(payload.elapsed);if(Number.isFinite(seconds)){
          this.serverElapsed=Math.max(this.serverElapsed,seconds);
          this.started=Math.min(this.started,this.now()-this.serverElapsed*1000);
        }
      } else if(type==='reasoning') {
        this.note=String(payload.text||'').trim().split('\n')[0].slice(0,160);
      } else if(type==='step') {
        if(payload.status==='start')this.label=LABELS[payload.stage]||'正在处理';
        if(payload.status==='done'&&payload.ok===false)this.finish('error','答案核对未通过');
      } else if(type==='token') {
        if(String(payload.text||'').trim())this.answering=true;
        this.label='正在生成回答';this.note='';
      } else if(type==='tool_start'||type==='tool_end') {
        let id=payload.run_id;
        if(!id&&type==='tool_end')id=[...this.rows.values()].find(row=>row.name===payload.tool&&row.state==='running')?.id;
        id=id||'activity-'+(++this.serial);
        let row=this.rows.get(id);
        if(!row){row={id,name:payload.tool||'',label:clean(payload.label)||TOOLS[payload.tool]||'执行工具',
          started:this.now(),state:'running',note:'正在执行…',seconds:null};this.rows.set(id,row);}
        if(type==='tool_start') {
          this.answering=false;this.label=row.label;this.note='';
        } else if(row.state==='running') {
          row.state=payload.warning?'warning':payload.ok===false||['failed','error'].includes(payload.status)?'error':'done';
          const seconds=Number(payload.seconds);
          row.seconds=payload.seconds!=null&&Number.isFinite(seconds)?Math.max(0,seconds):Math.max(0,(this.now()-row.started)/1000);
          row.note=payload.message||({done:'执行完成',warning:'执行完成，请留意结果',error:'执行未完成'})[row.state];
          this.label=[...this.rows.values()].find(item=>item.state==='running')?.label||'正在整理结果';
        }
      } else if(type==='verify'&&payload.ok===false) this.finish('error','答案核对未通过');
    }
    elapsed(){return Math.max(this.serverElapsed,(this.now()-this.started)/1000);}
    finish(status='done', note='') {
      if(this.status!=='running')return;
      this.seconds=this.elapsed();this.status=status;this.note='';
      this.label=note||({done:'回答完成',error:'本轮未完成',stopped:'已停止回答'})[status];
      for(const row of this.rows.values())if(row.state==='running'){
        row.state=status==='error'?'error':'warning';row.seconds=Math.max(0,(this.now()-row.started)/1000);
        row.note=status==='done'?'本轮已结束，工具未返回完成结果':'本轮已中断，工具未返回完成结果';
      }
    }
  }

  function create(host, options = {}) {
    const doc=host.ownerDocument, state=new ActivityState(options.now);
    const el=(tag,cls,text)=>{const node=doc.createElement(tag);node.className=cls||'';if(text!=null)node.textContent=text;return node;};
    const rootEl=el('div','rag-activity'),status=el('div','rag-activity-status'),icon=el('span','rag-activity-icon'),
      label=el('span','rag-activity-label'),elapsed=el('span','rag-activity-time'),note=el('p','rag-activity-note'),
      work=el('details','rag-work'),summary=el('summary'),list=el('div','rag-tool-list');
    icon.setAttribute('aria-hidden','true');elapsed.setAttribute('aria-hidden','true');label.setAttribute('role','status');
    work.open=true;work.hidden=true;note.hidden=true;list.setAttribute('role','list');
    status.append(icon,label,elapsed);work.append(summary,list);rootEl.append(status,note,work);host.append(rootEl);
    const nodes=new Map(),disclosure=new root.FosToolDisclosure();let timer=null;
    const text=(node,value)=>{const translated=root.FOSI18n?.t(value)??value;if(node.textContent!==translated)node.textContent=translated;};
    const duration=seconds=>seconds<10?seconds.toFixed(1)+'s':Math.floor(seconds)+'s';
    list.addEventListener('animationend',event=>{if(event.animationName==='rag-tool-complete')event.target.closest('.rag-tool')?.classList.remove('is-fresh');});
    function render() {
      if(rootEl.dataset.state!==state.status){
        rootEl.dataset.state=state.status;icon.innerHTML=state.status==='done'?CHECK:state.status==='running'?'':state.status==='error'?'×':'!';
      }
      text(label,state.label);text(elapsed,duration(state.status==='running'?state.elapsed():state.seconds));
      text(note,state.note);note.hidden=!state.note;work.hidden=!state.rows.size;
      text(summary,'工具活动 · '+state.rows.size+' 次调用');
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
