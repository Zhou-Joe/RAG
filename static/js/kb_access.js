(function(global){
 'use strict';
 function sync(form){
  const field=form.querySelector('[name=access_level]');if(!field)return;
  for(const group of form.querySelectorAll('[data-access-department]')){
   const shown=field.value==='department';group.hidden=!shown;
   for(const input of group.querySelectorAll('input,select'))input.disabled=!shown||field.disabled;
  }
 }
 function initDelete(){
  if(document.getElementById('kbDeleteForm'))return;
  const opener=document.getElementById('descEditBtn')||document.getElementById('feedKbEdit');
  const editForm=document.getElementById('descForm')||document.getElementById('feedKbEditForm');
  const dialog=editForm?.closest('dialog'),actions=editForm?.querySelector('.dialog-actions');
  const csrf=editForm?.querySelector('[name=csrfmiddlewaretoken]');
  const libraryPath=global.location.pathname;
  if(!opener||!dialog||!actions||!csrf||!/\/kb\/manage\/[-\w]+\/$/.test(libraryPath))return;

  // Separate form: deleting must never submit or validate unsaved edits.
  const deletion=document.createElement('form');
  deletion.id='kbDeleteForm';deletion.method='post';deletion.action=libraryPath+'delete/';deletion.hidden=true;
  deletion.appendChild(csrf.cloneNode(true));
  deletion.setAttribute('data-confirm-title','删除知识库');
  dialog.appendChild(deletion);
  const button=document.createElement('button');
  button.type='submit';button.className='btn danger kb-edit-delete';button.setAttribute('form',deletion.id);
  button.textContent='删除知识库';
  function confirmMessage(){
   const heading=document.getElementById('feedKbName')||document.querySelector('.page-head-main h1')?.firstChild;
   const name=heading?.textContent.trim()||editForm.querySelector('[name=name]')?.value||'';
   const message=`确定删除「${name}」？该知识库的所有文档、记录、索引和关联自动任务将被永久删除，不可恢复。`;
   deletion.setAttribute('data-confirm',global.FOSI18n?global.FOSI18n.t(message):message);
  }
  confirmMessage();button.addEventListener('click',confirmMessage);
  const saves=document.createElement('div');saves.className='kb-edit-save-actions';
  while(actions.firstChild)saves.appendChild(actions.firstChild);
  actions.classList.add('kb-edit-actions');actions.append(button,saves);
 }
 const foldsKey='fos-library-sections';
 function readFolds(){try{const value=JSON.parse(global.localStorage.getItem(foldsKey)||'{}');return value&&typeof value==='object'&&!Array.isArray(value)?value:{};}catch(e){return {};}}
 function initSections(){
  const folds=readFolds();
  for(const section of document.querySelectorAll('[data-kb-category]')){
   if(section.querySelector(':scope > .kb-library-panel'))continue;
   const bar=section.querySelector(':scope > .section-bar'),heading=bar?.querySelector('h2');
   if(!bar||!heading)continue;
   const panel=document.createElement('details');panel.className='kb-library-panel';panel.open=folds[section.id]!==false;
   const summary=document.createElement('summary');summary.className='kb-library-summary';
   const copy=document.createElement('span');copy.className='kb-library-copy';
   const title=document.createElement('span');title.className='kb-library-title';
   while(heading.firstChild)title.appendChild(heading.firstChild);
   copy.appendChild(title);
   const description=bar.querySelector('p');
   if(description){const note=document.createElement('span');note.className='kb-library-description';while(description.firstChild)note.appendChild(description.firstChild);copy.appendChild(note);}
   const chevron=document.createElement('span');chevron.className='kb-library-chevron';chevron.setAttribute('aria-hidden','true');
   chevron.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m9 5 7 7-7 7"/></svg>';
   let count=bar.querySelector('.count-chip');
   if(!count){count=document.createElement('span');count.className='count-chip';const total=section.querySelectorAll('.kb-list-card,.public-list > li').length;count.textContent=global.FOSI18n?global.FOSI18n.t(`${total} 个`):`${total} 个`;}
   heading.append(chevron,copy,count);summary.appendChild(heading);panel.appendChild(summary);bar.remove();
   const body=document.createElement('div');body.className='kb-library-body';
   while(section.firstChild)body.appendChild(section.firstChild);
   panel.appendChild(body);section.appendChild(panel);
   panel.addEventListener('toggle',()=>{const saved=readFolds();saved[section.id]=panel.open;try{global.localStorage.setItem(foldsKey,JSON.stringify(saved));}catch(e){}});
  }
 }
 function init(){for(const root of document.querySelectorAll('[data-kb-access]')){const form=root.closest('form'),field=root.querySelector('[data-access-level]');if(!form||!field)continue;field.addEventListener('change',()=>sync(form));sync(form);}initDelete();initSections();}
 global.KBAccessFields={sync,initDelete,initSections};
 document.addEventListener('kb-libraries-changed',initSections);
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})(window);
