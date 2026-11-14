(()=>{
 'use strict';const root=document.getElementById('ragAssistant');if(!root)return;
 const panel=document.getElementById('ragChatPanel'),launcher=document.getElementById('ragChatLauncher'),backdrop=document.getElementById('ragChatBackdrop');
 const close=document.getElementById('ragChatClose'),expand=document.getElementById('ragChatExpand'),body=document.getElementById('ragChatBody'),loading=document.getElementById('ragChatLoading');
 const badge=document.getElementById('ragChatBadge'),live=document.getElementById('ragChatLive'),key='rag-assistant-ui-'+root.dataset.user;
 let saved={};try{saved=JSON.parse(sessionStorage.getItem(key)||'{}')||{};}catch(_){}
 let opened=false,expanded=saved.expanded===true,busy=saved.busy===true,unread=saved.unread===true,frame=null,ready=false,pendingThread='',pendingNew=false,evidencePromise=null,pollTimer;
 let panelMotion=null,motionVersion=0;
 const mobile=()=>matchMedia('(max-width:620px)').matches,modal=()=>expanded||mobile();
 function store(){try{sessionStorage.setItem(key,JSON.stringify({open:opened,expanded,busy,unread}));}catch(_){}}
 function tell(data){if(frame&&ready)frame.contentWindow.postMessage(data,location.origin);}
 function signal(){launcher.classList.toggle('is-busy',busy);badge.hidden=!unread;launcher.setAttribute('aria-label',busy?'EnGenie 正在回答，点击打开':unread?'EnGenie 有新回答，点击打开':'打开 EnGenie 工程助手');document.getElementById('ragChatLauncherStatus').textContent=busy?'正在回答…':unread?'有一条新回答':'随时提问';store();}
 function theme(){tell({type:'rag-chat:theme',theme:document.documentElement.dataset.theme||'light'});}
 function viewport(){const view=window.visualViewport;root.style.setProperty('--rag-chat-viewport-height',(view?.height||innerHeight)+'px');root.style.setProperty('--rag-chat-viewport-top',(view?.offsetTop||0)+'px');}
 function layout(){panel.classList.toggle('is-expanded',expanded);panel.setAttribute('aria-modal',String(opened&&modal()));expand.setAttribute('aria-pressed',String(expanded));expand.setAttribute('aria-label',expanded?'恢复紧凑聊天面板':'展开聊天面板');expand.title=expand.getAttribute('aria-label');backdrop.hidden=!opened||!modal();document.body.classList.toggle('rag-chat-modal-open',opened&&modal());tell({type:'rag-chat:mode',modal:modal()});viewport();store();}
 function ensureFrame(){
  if(frame)return;
  frame=document.createElement('iframe');frame.title='EnGenie 工程助手对话';frame.id='ragChatFrame';frame.setAttribute('allow','clipboard-write');
  const url=new URL(root.dataset.chatUrl,location.origin);url.searchParams.set('embed','1');if(pendingThread){url.searchParams.set('conv',pendingThread);pendingThread='';}
  frame.src=url.href;body.append(frame);
  frame.addEventListener('load',()=>{
   try{if(!frame.contentDocument.getElementById('messages')){loading.hidden=false;loading.replaceChildren(document.createTextNode('对话暂时无法打开，请检查登录状态。'));return;}}catch(_){loading.hidden=false;loading.textContent='对话加载失败，请重新打开。';return;}
   loading.hidden=true;
  });
 }
 function revealPanel(value){
  const version=++motionVersion,visible=!panel.hidden;
  const current=visible?{opacity:getComputedStyle(panel).opacity,transform:getComputedStyle(panel).transform}:null;
  panelMotion?.cancel();panelMotion=null;
  panel.hidden=false;panel.inert=!value;panel.setAttribute('aria-hidden',String(!value));
  if(matchMedia('(prefers-reduced-motion: reduce)').matches||typeof panel.animate!=='function'){
   panel.hidden=!value;return;
  }
  const rest={opacity:1,transform:'none'},fold={opacity:0,transform:'translateY(18px) scale(.965)'};
  const motion=panel.animate([current||(value?fold:rest),value?rest:fold],
   {duration:value?320:180,easing:value?'cubic-bezier(.2,.85,.25,1)':'cubic-bezier(.4,0,1,1)',fill:'both'});
  panelMotion=motion;
  motion.finished.then(()=>{
   if(version!==motionVersion)return;
   panel.hidden=!opened;panelMotion=null;motion.cancel();
  }).catch(()=>{});
 }
 function setOpen(value,thread=''){
  if(thread)pendingThread=thread;
  const changed=value!==opened;opened=value;
  if(changed)revealPanel(value);
  if(changed&&value){window.fosGenieArrival?.replay(panel);tell({type:'rag-chat:opened'});}
  launcher.hidden=value;launcher.setAttribute('aria-expanded',String(value));
  if(value){unread=false;ensureFrame();if(ready&&pendingThread){tell({type:'rag-chat:thread',thread:pendingThread});pendingThread='';}if(mobile())close.focus();else tell({type:'rag-chat:focus'});}
  else launcher.focus();layout();signal();
 }
 function openEvidence(data){
  if(typeof data.chunkId!=='string'||!data.chunkId||data.chunkId.length>200)return;
  if(!getComputedStyle(root).getPropertyValue('--surface').trim()){expanded=true;layout();tell({type:'rag-chat:evidence-local',chunkId:data.chunkId,chunks:data.chunks});return;}
  if(!window.fosEvidencePanel&&!evidencePromise)evidencePromise=new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='/static/js/evidence_panel.js';script.onload=resolve;script.onerror=()=>{evidencePromise=null;reject(new Error('原文面板加载失败'));};document.head.append(script);});
  Promise.resolve(evidencePromise).then(()=>window.fosEvidencePanel(data.chunkId,{chunks:(Array.isArray(data.chunks)?data.chunks:[]).slice(0,100).map(row=>({chunk_id:String(row.chunk_id||'').slice(0,200),page:String(row.page||'').slice(0,100)}))})).catch(error=>window.fosToast?.(error.message,'err'));
 }
 window.addEventListener('message',event=>{
  if(!frame||event.origin!==location.origin||event.source!==frame.contentWindow)return;const data=event.data||{};
  if(data.type==='rag-chat:ready'){
   ready=true;loading.hidden=true;theme();layout();if(pendingNew){tell({type:'rag-chat:new'});pendingNew=false;}if(pendingThread){tell({type:'rag-chat:thread',thread:pendingThread});pendingThread='';}if(opened&&!mobile())tell({type:'rag-chat:focus'});
  }else if(data.type==='rag-chat:state'){
   if(typeof data.busy!=='boolean')return;const was=busy;busy=data.busy;if(was&&!busy&&!opened){unread=true;live.textContent='EnGenie 已完成回答';}signal();
  }else if(data.type==='rag-chat:history-cleared'){
   pendingThread='';root.dataset.thread='';busy=false;unread=false;live.textContent='';
   const url=new URL(location.href),chat=new URL(root.dataset.chatUrl,location.origin);
   if(url.pathname===chat.pathname){url.searchParams.delete('conv');history.replaceState(null,'',url);}
   signal();
  }else if(data.type==='rag-chat:close')setOpen(false);
  else if(data.type==='rag-chat:evidence')openEvidence(data);
  else if(data.type==='rag-chat:focus-header'&&opened&&modal()){
   const buttons=[...panel.querySelectorAll('.rag-chat-actions button')].filter(button=>button.getClientRects().length);(data.edge==='last'?buttons.at(-1):buttons[0])?.focus();
  }
 });
 launcher.addEventListener('click',()=>setOpen(true));close.addEventListener('click',()=>setOpen(false));backdrop.addEventListener('click',()=>setOpen(false));
 expand.addEventListener('click',()=>{expanded=!expanded;layout();});
 document.getElementById('ragChatNew').addEventListener('click',()=>{if(ready)tell({type:'rag-chat:new'});else pendingNew=true;tell({type:'rag-chat:focus'});});
 document.addEventListener('click',event=>{
  const trigger=event.target.closest('[data-rag-chat-open]');if(trigger){event.preventDefault();setOpen(true);return;}
  const link=event.target.closest('a[href]');if(!link||event.defaultPrevented||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey||link.target==='_blank'||link.hasAttribute('download'))return;
  const url=new URL(link.href,location.origin),chat=new URL(root.dataset.chatUrl,location.origin);
  if(url.origin===location.origin&&url.pathname===chat.pathname&&!url.searchParams.has('embed')){event.preventDefault();setOpen(true,url.searchParams.get('conv')||'');}
 });
 document.addEventListener('keydown',event=>{
  if(!opened||document.querySelector('dialog[open]'))return;
  if(event.key==='Escape'){event.preventDefault();setOpen(false);}
  else if(event.key==='Tab'&&event.shiftKey&&modal()&&event.target===document.getElementById('ragChatNew')){event.preventDefault();frame?.focus();tell({type:'rag-chat:focus',edge:'last'});}
 });
 new MutationObserver(theme).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
 window.addEventListener('resize',layout);window.visualViewport?.addEventListener('resize',viewport);window.visualViewport?.addEventListener('scroll',viewport);
 async function watchBackground(){
  if(frame||!busy)return;
  try{const response=await fetch('/kb/conv/states/',{cache:'no-store',signal:AbortSignal.timeout(10000)});if(!response.ok||response.redirected)return;const data=await response.json();let thread='';try{thread=sessionStorage.getItem('rag-ask-'+root.dataset.user)||'';}catch(_){}
   const row=(data.conversations||[]).find(item=>item.thread_id===thread);if(row){busy=row.running===true;if(!busy)unread=row.unread===true;signal();}
  }catch(_){}finally{if(busy&&!frame)pollTimer=setTimeout(watchBackground,3000);}
 }
 window.addEventListener('pagehide',()=>{clearTimeout(pollTimer);store();});window.addEventListener('pageshow',()=>{if(opened)layout();});
 window.fosOpenAssistant=()=>setOpen(true);
 viewport();signal();
 if(root.dataset.autoOpen==='true'||saved.open===true)setOpen(true,root.dataset.thread||'');else watchBackground();
})();
