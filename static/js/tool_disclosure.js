/* Fold once when tools give way to an answer; keep subsequent user toggles. */
(function (root) {
  'use strict';
  const toolNames = {
    list_knowledge_bases:'List knowledge bases',kb_relationships:'Inspect knowledge base connections',kb_graph_search:'Search verified facts',
    kb_search:'Search knowledge bases',kb_read_chunks:'Read source passages',kb_select_evidence:'Select supporting evidence',kb_image_search:'Find images',
    kb_fetch_doc:'Read document content',kb_neighbor_chunk:'Read adjacent passages',tracker_preview:'Preview tracker',tracker_lookup:'Search tracker records',
    tracker_analyze:'Analyze records and prepare report',tracker_visualize:'Create interactive data view',write_analysis:'Prepare analysis',check_analysis:'Check analysis progress',request_verified_export:'Prepare verified export',
    browser_open:'Open webpage',browser_snapshot:'Inspect page structure',browser_action:'Interact with webpage',browser_network:'Inspect network data',
    browser_read_response:'Read API response',browser_extract:'Extract page fields',test_browser_connector:'Replay and validate collection',
    probe_api:'Test API data',search_api_docs:'Search documentation',read_api_documentation:'Read documentation',inspect_api_sample:'Inspect API sample',
    inspect_connector_environment:'Check collection environment',test_mapping:'Validate data mapping',show_data_preview:'Preview collected data',
    ask_user_questions:'Request missing information',set_source_access:'Configure source access',preview_calculation:'Preview calculation',
    configure_report_delivery:'Configure report delivery',get_current_time:'Check current time',inspect_task_history:'Inspect task history',
    get_report_site_address:'Check report site address',inspect_report_data:'Inspect report data',preview_report_calculation:'Preview report calculation',
    preview_scheduled_report:'Preview scheduled report',preview_triggered_report:'Preview conditional delivery',
    preview_engenie_digest:'Preview EnGenie Digest'
  };
  const progress = {
    '正在执行…':'Running…','正在执行':'Running','执行完成':'Completed','执行未完成':'Failed',
    '执行完成，请留意结果':'Completed with a warning','完成':'Completed','未通过':'Failed','请留意':'Warning',
    '本轮已结束，工具未返回完成结果':'Round ended without a tool completion result',
    '本轮已中断，工具未返回完成结果':'Round interrupted before the tool returned',
    '正在准备回答':'Preparing answer','正在处理':'Processing','正在整理结果':'Organizing results',
    '正在生成回答':'Generating answer','答案核对未通过':'Answer verification failed',
    '回答完成':'Answer complete','本轮未完成':'This round did not complete','已停止回答':'Answer stopped'
  };
  function english(value, fallback = 'Processing') {
    const source=String(value??'').trim();
    if(!source)return fallback;
    const translated=progress[source]||root.FOSI18n?.t(source,'en')||source;
    // Free-form diagnostics remain in the task log; controlled activity chrome
    // must stay English even when a server returns an untranslated message.
    return /[\u3400-\u9fff]/u.test(translated)?fallback:translated;
  }
  root.FosToolText = {message:english,name:(name,label)=>toolNames[name]||english(label||name,'Run tool')};
  class ToolDisclosure {
    constructor(){this.phase=null;}
    update(phase, open=true){
      if(phase!==this.phase){
        if(phase==='answer')open=false;
        else if(this.phase==='answer')open=true;
        this.phase=phase;
      }
      return open;
    }
    static builderPhase(job){
      const pending=new Set();let answering=false;
      for(const event of job.events||[]){
        const key=(event.turn||0)+':'+event.call_id;
        if(event.kind==='tool_start'){pending.add(key);answering=false;}
        else if(event.kind==='tool_end')pending.delete(key);
        else if(['phase','model_request'].includes(event.kind))answering=false;
        else if(event.kind==='token'&&String(event.text||'').trim())answering=true;
      }
      const completed=['answered','ready','needs_info','enabled','revised'].includes(job.status);
      return completed||(answering&&!pending.size)?'answer':'tools';
    }
  }
  root.FosToolDisclosure=ToolDisclosure;
})(globalThis);
