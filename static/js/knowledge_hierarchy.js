/* 对象属性树：层级可展开，值与证据单独查看，审核始终针对一条内容。 */
(function(){
  'use strict';
  const states={candidate:'待确认',verified:'已确认',rejected:'已拒绝'};
  const el=(tag,text,style)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(style)node.className=style;return node;};
  window.createKnowledgeHierarchy=function({form,page}){
    const panel=document.getElementById('knowledgeHierarchy');if(!panel)return null;
    const objects=document.getElementById('knowledgeObjects'),tree=document.getElementById('knowledgeTree');
    const inspector=document.getElementById('knowledgeInspector'),notice=document.getElementById('knowledgeStatus');
    const count=document.getElementById('knowledgeCounts'),current=document.getElementById('knowledgeCurrentRun');
    let selected=null,selectedNode=null,sequence=0,detailSequence=0,request,detailRequest,disposed=false,fingerprint='';
    const expanded=new Map(),reviews=new Set();
    function params(){
      const data=new URLSearchParams({kb:page.dataset.hierarchyScope || form.elements.kb.value});
      if(form.elements.q.value)data.set('q',form.elements.q.value);
      if(form.elements.status.value)data.set('status',form.elements.status.value);
      if(current.checked && form.elements.run.value)data.set('run',form.elements.run.value);
      if(selected)data.set('object',selected);
      return data;
    }
    async function read(query,signal){
      const controller=new AbortController();let timedOut=false;
      const abort=()=>controller.abort();signal?.addEventListener('abort',abort,{once:true});if(signal?.aborted)abort();
      const timeout=setTimeout(()=>{timedOut=true;controller.abort();},15000);
      try{
        const response=await fetch(page.dataset.hierarchyUrl+'?'+query,{signal:controller.signal,headers:{'X-Requested-With':'XMLHttpRequest'}});
        if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('会话已失效，请重新登录。');
        const result=await response.json();if(!response.ok)throw new Error(result.error || '内容不存在或访问权限已变化。');return result;
      }catch(error){if(timedOut)throw new Error('读取层级内容超时，请重新选择节点或刷新结果；后台抽取不受影响。');throw error;}
      finally{clearTimeout(timeout);signal?.removeEventListener('abort',abort);}
    }
    async function showNode(node,navigate=false){
      selectedNode=node.id;
      detailRequest?.abort();const ticket=++detailSequence;
      if(!node.assertions.length){inspector.replaceChildren(el('h3',node.label),el('p','展开下级，选择具体内容查看取值、条件和原文。','graph-meta'));return;}
      detailRequest=new AbortController();
      inspector.replaceChildren(el('p','正在读取内容与原文…','graph-meta'));
      const query=params();query.set('node',node.id);
      try{const data=await read(query,detailRequest.signal);if(!disposed && ticket===detailSequence){inspector.innerHTML=data.html;if(navigate && matchMedia('(max-width:840px)').matches)inspector.scrollIntoView({block:'start',behavior:'auto'});}}
      catch(error){if(!disposed && ticket===detailSequence && error.name!=='AbortError')inspector.replaceChildren(el('p',error.message,'graph-stale'));}
    }
    function branch(node,depth){
      const holder=el('li'),hasChildren=node.children.length>0;
      const details=hasChildren?el('details'):null;
      const row=el(hasChildren?'summary':'div',undefined,'knowledge-node-row');
      const button=el('button',node.label,'knowledge-node-label');button.type='button';
      button.addEventListener('click',()=>{if(details)details.open=!details.open;showNode(node,true);});row.appendChild(button);
      if(node.assertions.length){
        const pending=node.assertions.filter(item=>item.status==='candidate').length;
        row.appendChild(el('span',node.assertions.length+' 条'+(pending?' · '+pending+' 待确认':''),'knowledge-node-count'));
      }
      const scope=Object.values(node.scope || {}).join(' · ');
      if(depth===0 && scope)row.appendChild(el('span',scope,'knowledge-node-scope'));
      if(details){
        details.open=expanded.has(node.id)?expanded.get(node.id):depth<2;
        details.addEventListener('toggle',()=>expanded.set(node.id,details.open));
        details.appendChild(row);const list=el('ul');node.children.forEach(child=>list.appendChild(branch(child,depth+1)));details.appendChild(list);holder.appendChild(details);
      }else holder.appendChild(row);
      if(node.assertions.length){
        const values=el('div',undefined,'knowledge-node-values');
        node.assertions.slice(0,2).forEach(item=>{
          const value=el('button',item.preview+' · '+(item.current?states[item.status]:'来源失效'),'knowledge-value-preview');
          value.type='button';value.addEventListener('click',()=>showNode(node,true));values.appendChild(value);
        });
        if(details)details.insertBefore(values,details.children[1]);else holder.appendChild(values);
      }
      return holder;
    }
    async function reload(){
      request?.abort();request=new AbortController();const ticket=++sequence;
      try{
        const data=await read(params(),request.signal);if(disposed || ticket!==sequence)return;
        selected=data.selected;
        count.textContent=data.objects.length+' 个对象 · 当前对象 '+data.count+' 条内容';
        if(data.truncated)notice.textContent='当前显示达到上限，请通过状态、关键词或本次结果缩小范围。';
        else {
          const scope=el('span',data.scope);scope.setAttribute('translate','no');
          notice.replaceChildren(scope,document.createTextNode(' · 跨提示累计的对象知识'));
        }
        objects.replaceChildren();
        data.objects.forEach(item=>{
          const button=el('button',item.label+' · '+item.count,'knowledge-object'+(item.id===selected?' selected':''));button.type='button';
          button.setAttribute('aria-pressed',String(item.id===selected));button.addEventListener('click',()=>{selected=item.id;selectedNode=null;++detailSequence;detailRequest?.abort();fingerprint='';reload();});objects.appendChild(button);
        });
        const next=JSON.stringify([selected,data.tree]);if(next===fingerprint)return;fingerprint=next;
        tree.replaceChildren();
        if(!data.tree.length){tree.appendChild(el('p','尚无层级内容。选择资料并输入关注方面，AI 会按对象、属性和子参数生成候选。','knowledge-empty'));inspector.replaceChildren(el('p','每条候选确认后才用于问答。','graph-meta'));return;}
        const list=el('ul',undefined,'knowledge-branches');data.tree.forEach(node=>list.appendChild(branch(node,0)));tree.appendChild(list);
        function find(nodes){for(const node of nodes){if(node.id===selectedNode)return node;const child=find(node.children);if(child)return child;}return null;}
        const chosen=find(data.tree);
        if(chosen)showNode(chosen);else{selectedNode=null;inspector.replaceChildren(el('h3',data.objects.find(item=>item.id===selected)?.label || '对象知识'),el('p','展开属性和分类项，点击具体内容查看取值、适用条件与原文。','graph-meta'));}
      }catch(error){if(!disposed && ticket===sequence && error.name!=='AbortError')notice.textContent=error.message;}
    }
    async function review(button){
      button.disabled=true;const data=new FormData();data.set('kb',page.dataset.hierarchyScope || form.elements.kb.value);
      data.set('assertion',button.dataset.knowledgeAssertion);data.set('status',button.dataset.status);
      const controller=new AbortController();reviews.add(controller);const timeout=setTimeout(()=>controller.abort(),30000);
      try{
        const response=await fetch(page.dataset.hierarchyAction,{method:'POST',body:data,signal:controller.signal,headers:{'X-CSRFToken':document.querySelector('[name=csrfmiddlewaretoken]').value,'X-Requested-With':'XMLHttpRequest'}});
        if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('会话已失效，请重新登录。');
        const result=await response.json();if(!response.ok)throw new Error(result.error || '审核未保存。');
        if(!disposed){window.fosToast?.(result.message,'ok');fingerprint='';await reload();}
      }catch(error){if(!disposed)notice.textContent=error.name==='AbortError'?'审核请求超时，请刷新结果核对保存状态。':error.message;}
      finally{clearTimeout(timeout);reviews.delete(controller);if(!disposed)button.disabled=false;}
    }
    const clicks=event=>{const button=event.target.closest('[data-knowledge-assertion]');if(button)review(button);};
    panel.addEventListener('click',clicks);current.addEventListener('change',()=>{selectedNode=null;fingerprint='';reload();});
    return {reload,destroy(){disposed=true;request?.abort();detailRequest?.abort();reviews.forEach(controller=>controller.abort());panel.removeEventListener('click',clicks);}};
  };
})();
