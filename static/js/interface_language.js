/* Local, reversible UI translation; source content never leaves this browser. */
(function (global) {
  'use strict';
  const catalog = global.FOS_I18N_CATALOG || {messages:{},patterns:[],englishSource:{}};
  const messages = Object.assign(Object.create(null), catalog.messages), reverse = Object.create(null);
  Object.entries(messages).forEach(([source,target])=>{if(!reverse[target])reverse[target]=source;});
  Object.entries(catalog.englishSource || {}).forEach(([source,target])=>{reverse[source]=target;if(!messages[target])messages[target]=source;});
  const normalize = value=>String(value == null ? '' : value).replace(/\s+/g,' ').trim();
  const normalized = Object.create(null), reversed = Object.create(null);
  Object.entries(messages).forEach(([source,target])=>{normalized[normalize(source)]=target;});
  Object.entries(reverse).forEach(([source,target])=>{reversed[normalize(source)]=target;});
  const escape = value=>value.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  function compile(source,target){
    const names=[];let last=0,expression='';
    const numeric=new Set(['count','total','current','page','pages','index']);
    const tokens=/\{([A-Za-z_]\w*)\}/g;let match;
    while((match=tokens.exec(source))){expression+=escape(source.slice(last,match.index)).replace(/\s+/g,'\\s+')+(numeric.has(match[1])?'([0-9][0-9,.]*)':'([\\s\\S]+?)');names.push(match[1]);last=tokens.lastIndex;}
    expression+=escape(source.slice(last)).replace(/\s+/g,'\\s+');
    const fixed=source.replace(tokens,'').trim().length;
    if(!names.length || !fixed || (fixed<2&&!names.every(name=>numeric.has(name))))return null;
    return {regex:new RegExp('^'+expression+'$','u'),names,target};
  }
  const patterns={en:[], 'zh-hans':[]};
  (catalog.patterns||[]).slice().sort((a,b)=>b.source.replace(/\{\w+\}/g,'').length-a.source.replace(/\{\w+\}/g,'').length).forEach(row=>{
    const forward=compile(normalize(row.source),row.target),backward=compile(normalize(row.target),row.source);
    if(forward){forward.translated=row.translate||[];patterns.en.push(forward);}
    if(backward){backward.translated=row.translate||[];patterns['zh-hans'].push(backward);}
  });
  let language=global.document?.documentElement.dataset.language==='en'?'en':'zh-hans';
  function translate(value,selected=language,depth=0){
    const original=String(value==null?'':value),key=normalize(original);
    if(!key)return original;
    const table=selected==='en'?normalized:reversed;let result=table[key];
    if(result===undefined&&key.length<4000&&(selected!=='en'||/[\u3400-\u9fff]/.test(key))){
      for(const rule of patterns[selected==='en'?'en':'zh-hans']){
        const match=rule.regex.exec(original.trim());if(!match)continue;
        const values={};let valid=true;
        rule.names.forEach((name,i)=>{if(name in values&&values[name]!==match[i+1])valid=false;values[name]=match[i+1];});
        if(!valid)continue;
        result=rule.target.replace(/\{([A-Za-z_]\w*)\}/g,(_,name)=>depth<2&&rule.translated.includes(name)?translate(values[name]||'',selected,depth+1):(values[name]||''));break;
      }
    }
    if(result===undefined)return original;
    return (original.match(/^\s*/)||[''])[0]+result+(original.match(/\s*$/)||[''])[0];
  }
  const api={t:translate,get language(){return language;},get locale(){return language==='en'?'en-US':'zh-CN';}};
  if(typeof module!=='undefined'&&module.exports)module.exports={translate,compile,api};
  global.FOSI18n=api;if(!global.document)return;
  const document=global.document;
  const protectedSelector='script,style,noscript,code,pre,[translate="no"],[data-i18n-ignore],.notranslate,[contenteditable="true"],.answer:not([data-i18n-ui]),.msg.user .bubble,.rag-chat-message-content,.cx-user-text,.cx-answer,.cx-sample,.graph-live-excerpt,.vt-ai-text,.ev-quote,.slice-snip,.slice-content,.doc-body,.doc-viewer,.markdown-body,.kb-mono,.conv-title,.conv-meta,.dash-kb-name,.da-title,.da-meta,.slice-doc,.doc-name-text,.eval-q-text,.eval-text,.eval-kb';
  const attributes=['title','placeholder','aria-label','aria-description','alt','data-confirm','data-confirm-title','data-confirm-ok'];
  const originals=new WeakMap(),attributeOriginals=new WeakMap();
  function isProtected(element){return !element||!!element.closest(protectedSelector);}
  function applyText(node){
    if(isProtected(node.parentElement)||node.parentElement.closest('textarea')||node.parentElement.matches('.kb-name,[data-i18n-preserve-text]'))return;
    const current=node.nodeValue;let saved=originals.get(node);
    if(!saved||current!==saved.rendered)saved={source:current,rendered:current};
    const result=translate(saved.source);saved.rendered=result;originals.set(node,saved);
    if(current!==result)node.nodeValue=result;
  }
  function applyAttributes(element){
    if(isProtected(element))return;
    let saved=attributeOriginals.get(element);if(!saved){saved={};attributeOriginals.set(element,saved);}
    for(const name of attributes){
      if(!element.hasAttribute(name))continue;
      const current=element.getAttribute(name);let row=saved[name];
      if(!row||current!==row.rendered)row={source:current,rendered:current};
      row.rendered=translate(row.source);saved[name]=row;
      if(current!==row.rendered)element.setAttribute(name,row.rendered);
    }
  }
  function render(root=document.documentElement){
    if(root.nodeType===3){applyText(root);return;}
    if(root.nodeType!==1&&root.nodeType!==9&&root.nodeType!==11)return;
    if(root.nodeType===1){if(isProtected(root))return;applyAttributes(root);}
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT,{acceptNode(node){
      return node.nodeType===1&&isProtected(node)?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT;
    }});
    while(walker.nextNode()){const node=walker.currentNode;if(node.nodeType===3)applyText(node);else applyAttributes(node);}
  }
  function updateButtons(){
    document.querySelectorAll('[data-language-toggle]').forEach(button=>{
      button.setAttribute('aria-label',language==='en'?'Switch to Chinese':'切换到英文');
      button.setAttribute('title',language==='en'?'Switch to Chinese':'切换到英文');
      button.querySelectorAll('[data-language]').forEach(span=>span.classList.toggle('language-current',span.dataset.language===language));
    });
  }
  function persist(){
    try{global.localStorage.setItem('fos-language',language);}catch(e){}
    document.cookie='fos_language='+language+'; Path=/; Max-Age=31536000; SameSite=Lax'+(global.location.protocol==='https:'?'; Secure':'');
  }
  function setLanguage(value,save=true){
    language=value==='en'?'en':'zh-hans';
    document.documentElement.lang=language==='en'?'en':'zh-CN';document.documentElement.dataset.language=language;
    if(save)persist();render();updateButtons();
    document.dispatchEvent(new CustomEvent('fos:languagechange',{detail:{language,locale:api.locale}}));
  }
  api.setLanguage=setLanguage;api.render=render;api.isProtected=isProtected;
  api.originalText=element=>Array.from(element.childNodes).map(node=>node.nodeType===3?(originals.get(node)?.source||node.nodeValue):node.textContent).join('');
  api.untranslated=(root=document.body)=>{
    const missing=new Set(),walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
    while(walker.nextNode()){
      const node=walker.currentNode,parent=node.parentElement;
      if(!isProtected(parent)&&!parent.closest('textarea')&&!parent.matches('.kb-name,[data-i18n-preserve-text]')&&parent.getClientRects().length&&/[\u3400-\u9fff]/.test(node.nodeValue))missing.add(normalize(node.nodeValue));
    }
    root.querySelectorAll('[placeholder],[title],[aria-label]').forEach(element=>{
      if(!isProtected(element)&&element.getClientRects().length)for(const key of attributes){const value=element.getAttribute(key);if(value&&/[\u3400-\u9fff]/.test(value))missing.add(normalize(value));}
    });return Array.from(missing);
  };
  const pending=new Set();let scheduled=false;
  const observer=new MutationObserver(records=>{
    for(const record of records){if(record.type==='childList')record.addedNodes.forEach(node=>pending.add(node));else pending.add(record.target);}
    if(!scheduled){scheduled=true;queueMicrotask(()=>{scheduled=false;const nodes=Array.from(pending);pending.clear();nodes.forEach(node=>{if(node.isConnected)render(node);});});}
  });
  observer.observe(document.documentElement,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:attributes});
  document.addEventListener('click',event=>{if(event.target.closest('[data-language-toggle]'))setLanguage(language==='en'?'zh-hans':'en');});
  global.addEventListener('storage',event=>{if(event.key==='fos-language')setLanguage(event.newValue,false);});
  for(const name of ['alert','confirm','prompt']){
    const native=global[name];if(typeof native==='function')global[name]=function(message,...rest){return native.call(global,translate(message),...rest);};
  }
  function ready(){persist();render();updateButtons();delete document.documentElement.dataset.interfaceLoading;}
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',ready,{once:true});else ready();
})(typeof window!=='undefined'?window:globalThis);
