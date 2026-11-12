/* 实际模型文本与抽取活动的 SSE 订阅；所有模型内容均以纯文本渲染。 */
(function () {
  'use strict';
  const labels = {pending:'Queued',running:'Generating',completed:'Completed · awaiting review',failed:'Extraction failed'};
  const activityMessage = (value, fallback='Activity update') => window.FosToolText?.message(value,fallback)
    || (/[\u3400-\u9fff]/.test(String(value || '')) ? fallback : String(value || fallback));
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function activityElement(tag, text, className) {
    const node = element(tag,text,className);node.setAttribute('translate','no');return node;
  }
  window.createGraphLive = function ({scope, run, onComplete, onRetry}) {
    const panel = document.getElementById('graphLive');
    if (!panel) return null;
    const select = document.getElementById('graphLiveRun');
    const badge = document.getElementById('graphLiveState');
    const activity = document.getElementById('graphLiveActivity');
    const output = document.getElementById('graphLiveOutput');
    const note = document.getElementById('graphLiveOutputNote');
    const previews = document.getElementById('graphLiveCandidates');
    const count = document.getElementById('graphLiveCandidateCount');
    const reconnect = document.getElementById('graphLiveReconnect');
    const retry = document.getElementById('graphLiveRetry');
    const failure = document.getElementById('graphLiveFailure');
    const errorText = document.getElementById('graphLiveError');
    const waiting = document.getElementById('graphLiveWaiting');
    const follow = document.getElementById('graphLiveFollow');
    const steps = [...panel.querySelectorAll('.graph-live-steps li')];
    const coverageLabel = document.getElementById('graphLiveCoverage');
    const coverageMeter = document.getElementById('graphLiveCoverageMeter');
    const coverageBatches = document.getElementById('graphLiveCoverageBatches');
    [badge,waiting,errorText,coverageLabel,coverageMeter,coverageBatches].filter(Boolean)
      .forEach(node=>node.setAttribute('translate','no'));
    let coverageFingerprint = '';
    let source, selected = null, cursor = 0, lastTurn = null, candidates = 0, events = 0, failureMessage = null, disposed = false;
    let waitTimer, waitStarted = null, waitTurn = null, terminal = false;
    const reported = new Set();
    function close() { if (source) source.close(); source = null; }
    function stopWaiting(text='') { clearInterval(waitTimer); waitTimer=null; waitStarted=null; if (waiting) waiting.textContent=text; }
    function startWaiting(at, turn) {
      const started = Date.parse(at);
      if (!waiting || !Number.isFinite(started)) return;
      if (waitStarted===started && waitTurn===turn) return;
      stopWaiting();waitStarted=started;waitTurn=turn;
      const update=()=>{waiting.textContent='Model request '+turn+' · waiting '+Math.max(0,Math.floor((Date.now()-started)/1000))+' s';};
      update();waitTimer=setInterval(update,1000);
    }
    function applyStatus(data) {
      terminal=['completed','failed'].includes(data.status);
      state(statusText(data),data.status==='failed');
      if (failure) failure.hidden=data.status!=='failed';
      if (errorText) errorText.textContent=data.status==='failed' ? activityMessage(data.error,'Extraction failed; saved candidates are retained. See the activity log for details.') : '';
      if (data.status==='failed' && data.error && data.error!==failureMessage) {
        const item=activityElement('li',activityMessage(data.error,'Extraction failed'),'graph-stale');
        const details=element('details');details.appendChild(element('summary','Diagnostic details'));
        details.appendChild(element('pre',data.error));item.appendChild(details);activity.appendChild(item);failureMessage=data.error;
      }
      if (terminal) stopWaiting();
      else if (data.coverage?.request_in_flight) startWaiting(data.coverage.request_started_at,data.coverage.model_calls);
      else if (data.coverage?.request_in_flight===false) stopWaiting();
    }
    function notifyComplete(data) {
      const key=selected+':'+data.status;
      if (!reported.has(key)) {reported.add(key);onComplete?.();}
    }
    function state(text, failed) { badge.textContent = text; badge.classList.toggle('graph-stale',!!failed); }
    function scroll(node) { if (follow.checked) node.scrollTop = node.scrollHeight; }
    function stage(name) {
      const index = {retrieve:0,generate:1,validate:2,save:2,complete:3}[name];
      if (index === undefined) return;
      steps.forEach((step,i) => { step.classList.toggle('done',i < index); step.classList.toggle('active',i === index); });
    }
    function showCoverage(data) {
      if (!coverageLabel) return;
      if (data?.mode === 'focused') {
        coverageLabel.textContent = 'Read ' + data.chunks_read + ' passages · model calls ' + data.model_calls + '/' + data.limits.model_calls + ' · follow-up reads ' + data.tool_calls + '/' + data.limits.tool_calls;
        coverageMeter.max = data.limits.model_calls; coverageMeter.value = data.model_calls;
        coverageMeter.setAttribute('aria-label','Model calls used');
        coverageBatches.replaceChildren(activityElement('li','Relevant passages and neighboring context are read on demand; this does not establish full-document coverage.'));
        const usage = data.usage_calls
          ? 'Usage reported for ' + data.usage_calls + '/' + data.model_calls + ' calls: input ' + data.input_tokens + ', output ' + data.output_tokens + ' tokens.'
          : 'Token usage was not reported; actual calls and cumulative input/output characters are recorded.';
        coverageBatches.appendChild(activityElement('li',usage));
        coverageBatches.appendChild(activityElement('li','Total input ' + data.input_chars + ' characters · output ' + data.output_chars + ' characters.'));
        return;
      }
      if (!data || data.mode !== 'systematic') {
        coverageLabel.textContent = 'No reading coverage was recorded for this task.';
        return;
      }
      const fingerprint = JSON.stringify(data);
      if (fingerprint === coverageFingerprint) return;
      coverageFingerprint = fingerprint;
      coverageLabel.textContent = 'Checked ' + data.chunks_done + '/' + data.chunks_total + ' passages · completed ' + data.batches_done + '/' + data.batches_total + ' batches';
      coverageMeter.max = data.chunks_total || 1; coverageMeter.value = data.chunks_done;
      coverageMeter.setAttribute('aria-label','Checked source passages');
      coverageBatches.replaceChildren();
      (data.batches || []).forEach(batch => {
        const names = {pending:'Awaiting review',running:'Extracting / checking gaps',complete:'Extracted and checked',failed:'Incomplete'};
        const item = activityElement('li','Batch ' + batch.index + ' · ' + (names[batch.status] || batch.status) + ' · ' + batch.sections.join(' / ') + (batch.error ? ' · ' + activityMessage(batch.error,'Batch failed') : ''));
        if (batch.status === 'failed') item.classList.add('graph-stale');
        coverageBatches.appendChild(item);
      });
    }
    function statusText(data) {
      if (data.coverage?.mode === 'focused') {
        if (data.status === 'failed') return (data.coverage.stage === 'limited' ? 'Budget reached' : 'Extraction incomplete') + ' · ' + data.created + ' candidates saved';
        if (data.status === 'completed') return 'Topic extraction completed · awaiting review';
      }
      if (data.coverage?.mode === 'systematic') {
        if (data.status === 'failed') return 'Partially completed · ' + data.created + ' candidates saved';
        if (data.status === 'completed') return 'Document scan and gap check completed · awaiting review';
      }
      return labels[data.status] || data.status;
    }
    function chunkLinks(parent, chunks, activityMode=false) {
      (chunks || []).forEach(chunk => {
        const details = element(chunk.text ? 'details' : 'div');
        if (chunk.text) details.appendChild(element('summary',chunk.page || (activityMode ? 'Source passage' : '正文片段')));
        if (chunk.text) details.appendChild(element('p',chunk.text,'graph-live-excerpt'));
        if (chunk.chunk_id) {
          const button = element('button',(chunk.page || (activityMode ? 'Source passage' : '正文片段')) + (activityMode ? ' · View source annotation' : ' · 查看原文圈注'),'btn sm outline');
          button.type = 'button'; button.dataset.graphChunk = chunk.chunk_id;
          details.appendChild(button);
        }
        parent.appendChild(details);
      });
    }
    function progress(event) {
      if (Number(event.id) <= cursor) return;
      cursor = Number(event.id); ++events;
      const data = event.payload;
      if (event.kind === 'coverage') { showCoverage(data); return; }
      if (event.kind === 'model_request') startWaiting(event.at,data.turn);
      if (event.kind === 'model_timing') stopWaiting(activityMessage(data.message,'Model request completed'));
      if (event.kind === 'tool_start') stopWaiting('Reading source passages on demand…');
      if (event.kind === 'token') {
        if (lastTurn !== data.turn) {
          output.appendChild(document.createTextNode((lastTurn === null ? '' : '\n\n') + '—— 第 ' + data.turn + ' 轮模型输出 ——\n'));
          lastTurn = data.turn;
        }
        output.appendChild(document.createTextNode(data.text));
        note.textContent = '这是模型返回的原始文本，尚未人工确认。';
        scroll(output); return;
      }
      if (event.kind === 'candidate') {
        ++candidates; count.textContent = String(candidates);
        const card = element('article',undefined,'graph-live-candidate');
        card.appendChild(element('h4',data.subject + ' → ' + data.predicate + ' → ' + data.object));
        if (Object.keys(data.conditions || {}).length) card.appendChild(element('p',JSON.stringify(data.conditions,null,2),'graph-live-excerpt'));
        card.appendChild(element('p',data.quote,'graph-live-excerpt'));
        chunkLinks(card,data.chunks); previews.appendChild(card);
        return;
      }
      const item = activityElement('li');
      item.appendChild(element('time',new Date(event.at).toLocaleTimeString('en-US',{hour12:false})));
      const fallback={phase:'Processing',model_request:'Waiting for model',model_timing:'Model request completed',tool_end:'Tool completed',complete:'Extraction completed',failed:'Extraction failed'}[event.kind] || 'Activity update';
      const message=event.kind==='tool_start' ? (window.FosToolText?.name(data.name,data.message) || data.name || 'Using tool') : activityMessage(data.message,fallback);
      item.appendChild(element('span',message));
      if (data.message && message!==data.message && /[\u3400-\u9fff]/.test(data.message) && (event.kind==='failed' || data.ok===false)) {
        const details=element('details');details.appendChild(element('summary','Diagnostic details'));
        details.appendChild(element('pre',data.message));item.appendChild(details);
      }
      if (event.kind === 'tool_start') item.appendChild(element('pre',data.name + '(' + JSON.stringify(data.arguments) + ')'));
      chunkLinks(item,data.chunks,true);
      activity.appendChild(item);
      while (activity.children.length > 120) activity.firstElementChild.remove();
      scroll(activity);
      if (event.kind === 'phase') stage(data.stage);
      if (event.kind === 'complete') stage('complete');
      if (event.kind === 'failed') { failureMessage = data.message; applyStatus({status:'failed',error:data.message}); }
    }
    function connect() {
      close();
      if (!selected || disposed) return;
      if (!window.EventSource) { state('Live connections are unavailable in this browser. Check task progress below.',true); return; }
      state('Connecting…');
      const query = new URLSearchParams({kb:scope(),after:String(cursor)});
      const connection = new EventSource('/kb/graph/extraction/' + encodeURIComponent(selected) + '/events/?' + query);
      source = connection;
      const current = () => !disposed && source === connection;
      connection.addEventListener('snapshot', event => {
        if (!current()) return;
        const data = JSON.parse(event.data);
        applyStatus(data); showCoverage(data.coverage);
        const focus=document.getElementById('graphLiveFocus');focus.setAttribute('translate','no');
        focus.textContent = data.document + ' · Focus: ' + (data.focus || 'Legacy full-document extraction');
      });
      connection.addEventListener('progress', event => { if (current()) progress(JSON.parse(event.data)); });
      connection.addEventListener('done', event => {
        if (!current()) return;
        const data = JSON.parse(event.data);
        close(); applyStatus(data); showCoverage(data.coverage);
        if (data.status === 'completed') stage('complete');
        if (!events) note.textContent = '此任务没有保存实时输出；升级前的任务仅保留抽取结果。';
        else if (lastTurn === null) note.textContent = '模型未返回公开文本输出。请查看左侧工具活动与任务状态。';
        if (data.error && data.error !== failureMessage) {
          const item = activityElement('li',activityMessage(data.error,'Extraction failed'),'graph-stale');
          const details=element('details');details.appendChild(element('summary','Diagnostic details'));details.appendChild(element('pre',data.error));item.appendChild(details);activity.appendChild(item);
          failureMessage = data.error;
        }
        notifyComplete(data);
      });
      connection.addEventListener('unavailable', event => {
        if (current()) { close(); state(activityMessage(JSON.parse(event.data).message,'This task is unavailable.'),true); }
      });
      connection.onerror = () => {
        if (!current()) return;
        if (connection.readyState === EventSource.CLOSED) { close(); if (!terminal) state('Unable to read this task. Check your session and access.',true); }
        else if (!terminal) state('Log connection interrupted; reconnecting. The background task is still running.',true);
      };
    }
    function choose(id) {
      id = String(id || '');
      if (id === selected) return;
      close(); selected = id;
      terminal=false;stopWaiting();if (failure) failure.hidden=true;
      panel.hidden = !id;
      if (id && ![...select.options].some(option => option.value === id)) select.add(new Option('#' + id,id));
      select.value = id;
      cursor = candidates = events = 0; lastTurn = null; failureMessage = null;
      coverageFingerprint = '';
      if (coverageLabel) { coverageLabel.textContent = 'Loading reading coverage…'; coverageMeter.value = 0; coverageBatches.replaceChildren(); }
      output.replaceChildren(); activity.replaceChildren(); previews.replaceChildren(); count.textContent = '0';
      steps.forEach(step => step.classList.remove('done','active'));
      note.textContent = id ? '正在等待模型输出…' : '生成任务后，可在这里查看模型输出和读取、校验活动。';
      reconnect.disabled = !id;
      if (id) connect(); else state('No extraction task');
    }
    function updateJobs(jobs) {
      const previous = selected;
      select.replaceChildren(new Option('请选择任务',''));
      jobs.forEach(job => {
        const option = new Option(job.label,String(job.id)); option.dataset.status = job.status; select.add(option);
      });
      select.disabled = !jobs.length;
      const requested = run?.();
      const next = requested && jobs.some(job=>String(job.id)===requested) ? requested : jobs.some(job => String(job.id) === previous) ? previous :
        String((jobs.find(job => ['pending','running'].includes(job.status)) || jobs[0])?.id || '');
      select.value = next; choose(next); reconnect.disabled = !next;
      const job=jobs.find(job=>String(job.id)===next);
      if (job && ['completed','failed'].includes(job.status)) {applyStatus(job);notifyComplete(job);}
    }
    select.addEventListener('change', () => choose(select.value));
    reconnect.addEventListener('click', connect);
    retry?.addEventListener('click',async()=>{
      if (!selected || disposed || retry.disabled) return;
      retry.disabled=true;
      try {await onRetry?.(selected);} finally {if (!disposed) retry.disabled=false;}
    });
    const first = [...select.options].find(option=>option.value && option.value===run?.()) || [...select.options].find(option => ['pending','running'].includes(option.dataset.status)) || select.options[1];
    choose(first?.value || ''); reconnect.disabled = !selected;
    return {updateJobs,select:choose,active:()=>!!selected && !terminal,
      destroy() { disposed = true; close(); stopWaiting(); }};
  };
})();
