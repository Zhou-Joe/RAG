/* 实际模型文本与抽取活动的 SSE 订阅；所有模型内容均以纯文本渲染。 */
(function () {
  'use strict';
  const labels = {pending:'排队中',running:'正在生成',completed:'已完成 · 待人工确认',failed:'抽取失败'};
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
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
      const update=()=>{waiting.textContent='第 '+turn+' 轮模型请求 · 已等待 '+Math.max(0,Math.floor((Date.now()-started)/1000))+' 秒';};
      update();waitTimer=setInterval(update,1000);
    }
    function applyStatus(data) {
      terminal=['completed','failed'].includes(data.status);
      state(statusText(data),data.status==='failed');
      if (failure) failure.hidden=data.status!=='failed';
      if (errorText) errorText.textContent=data.status==='failed' ? (data.error || '本次抽取未完成，已保存的候选保留。') : '';
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
        coverageLabel.textContent = '已读取 ' + data.chunks_read + ' 段 · 模型调用 ' + data.model_calls + '/' + data.limits.model_calls + ' 次 · 补查 ' + data.tool_calls + '/' + data.limits.tool_calls + ' 次';
        coverageMeter.max = data.limits.model_calls; coverageMeter.value = data.model_calls;
        coverageMeter.setAttribute('aria-label','本次已用模型调用次数');
        coverageBatches.replaceChildren(element('li','按关注主题召回，再按需补读邻文；本次读取范围不代表全文覆盖。'));
        const usage = data.usage_calls
          ? '服务端已回报 ' + data.usage_calls + '/' + data.model_calls + ' 轮用量：输入 ' + data.input_tokens + '、输出 ' + data.output_tokens + ' token。'
          : '服务端未回报 token 用量；已记录实际调用次数和累计输入、输出字符数。';
        coverageBatches.appendChild(element('li',usage),element('li','累计输入 ' + data.input_chars + ' 字符 · 输出 ' + data.output_chars + ' 字符。'));
        return;
      }
      if (!data || data.mode !== 'systematic') {
        coverageLabel.textContent = '此任务未记录读取范围。';
        return;
      }
      const fingerprint = JSON.stringify(data);
      if (fingerprint === coverageFingerprint) return;
      coverageFingerprint = fingerprint;
      coverageLabel.textContent = '已检查 ' + data.chunks_done + '/' + data.chunks_total + ' 段 · 完成 ' + data.batches_done + '/' + data.batches_total + ' 批';
      coverageMeter.max = data.chunks_total || 1; coverageMeter.value = data.chunks_done;
      coverageMeter.setAttribute('aria-label','已检查正文片段');
      coverageBatches.replaceChildren();
      (data.batches || []).forEach(batch => {
        const names = {pending:'等待检查',running:'正在抽取 / 查漏',complete:'已抽取并查漏',failed:'未完成'};
        const item = element('li','第 ' + batch.index + ' 批 · ' + names[batch.status] + ' · ' + batch.sections.join('、') + (batch.error ? ' · ' + batch.error : ''));
        if (batch.status === 'failed') item.classList.add('graph-stale');
        coverageBatches.appendChild(item);
      });
    }
    function statusText(data) {
      if (data.coverage?.mode === 'focused') {
        if (data.status === 'failed') return (data.coverage.stage === 'limited' ? '达到本次预算' : '抽取未完成') + ' · 已保存 ' + data.created + ' 条候选';
        if (data.status === 'completed') return '主题抽取已结束 · 待人工确认';
      }
      if (data.coverage?.mode === 'systematic') {
        if (data.status === 'failed') return '未全部完成 · 已保存 ' + data.created + ' 条候选';
        if (data.status === 'completed') return '全篇扫描与查漏完成 · 待人工确认';
      }
      return labels[data.status] || data.status;
    }
    function chunkLinks(parent, chunks) {
      (chunks || []).forEach(chunk => {
        const details = element(chunk.text ? 'details' : 'div');
        if (chunk.text) details.appendChild(element('summary',chunk.page || '正文片段'));
        if (chunk.text) details.appendChild(element('p',chunk.text,'graph-live-excerpt'));
        if (chunk.chunk_id) {
          const button = element('button',(chunk.page || '正文片段') + ' · 查看原文圈注','btn sm outline');
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
      if (event.kind === 'model_timing') stopWaiting(data.message);
      if (event.kind === 'tool_start') stopWaiting('正在按需读取原文…');
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
      const item = element('li');
      item.appendChild(element('time',new Date(event.at).toLocaleTimeString(window.FOSI18n?.locale || 'zh-CN',{hour12:false})));
      item.appendChild(element('span',data.message || event.kind));
      if (event.kind === 'tool_start') item.appendChild(element('pre',data.name + '(' + JSON.stringify(data.arguments) + ')'));
      chunkLinks(item,data.chunks);
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
      if (!window.EventSource) { state('浏览器不支持实时连接，请查看下方任务进度。',true); return; }
      state('正在连接…');
      const query = new URLSearchParams({kb:scope(),after:String(cursor)});
      const connection = new EventSource('/kb/graph/extraction/' + encodeURIComponent(selected) + '/events/?' + query);
      source = connection;
      const current = () => !disposed && source === connection;
      connection.addEventListener('snapshot', event => {
        if (!current()) return;
        const data = JSON.parse(event.data);
        applyStatus(data); showCoverage(data.coverage);
        document.getElementById('graphLiveFocus').textContent = data.document + ' · 关注：' + (data.focus || '旧版全书抽取');
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
          const item = element('li',data.error,'graph-stale'); activity.appendChild(item);
          failureMessage = data.error;
        }
        notifyComplete(data);
      });
      connection.addEventListener('unavailable', event => {
        if (current()) { close(); state(JSON.parse(event.data).message,true); }
      });
      connection.onerror = () => {
        if (!current()) return;
        if (connection.readyState === EventSource.CLOSED) { close(); if (!terminal) state('无法读取此任务，请检查登录状态及权限。',true); }
        else if (!terminal) state('日志连接中断，正在重连；后台任务独立运行。',true);
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
      if (coverageLabel) { coverageLabel.textContent = '正在加载本次读取范围…'; coverageMeter.value = 0; coverageBatches.replaceChildren(); }
      output.replaceChildren(); activity.replaceChildren(); previews.replaceChildren(); count.textContent = '0';
      steps.forEach(step => step.classList.remove('done','active'));
      note.textContent = id ? '正在等待模型输出…' : '生成任务后，可在这里查看模型输出和读取、校验活动。';
      reconnect.disabled = !id;
      if (id) connect(); else state('暂无抽取任务');
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
