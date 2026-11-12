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
