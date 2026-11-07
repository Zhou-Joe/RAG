(()=>{
 'use strict';const page=document.getElementById('connectorPage');if(!page)return;
 const messages=document.getElementById('connectorMessages'),composer=document.getElementById('composerForm');
 const prompt=document.getElementById('connectorPrompt'),send=document.getElementById('connectorSend'),hint=document.getElementById('connectorHint');
 const csrf=()=>composer.querySelector('[name=csrfmiddlewaretoken]').value;
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
  button.disabled=true;button.dataset.requestKey ||= crypto.randomUUID();
  try{
   const reply=await fetch(button.dataset.reportTest,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':csrf()},body:JSON.stringify({request_key:button.dataset.requestKey})});
   const data=await reply.json();if(!reply.ok||!data.ok){if(data.status==='failed')delete button.dataset.requestKey;throw Error(data.error||'发送未完成');}
   button.textContent='工作流已接收测试报告';hint.textContent=data.message;
  }catch(error){hint.textContent=error.message;button.textContent='查看发送状态 / 重试';button.disabled=false;}
 });
})();
