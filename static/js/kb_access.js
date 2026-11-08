(function(global){
 'use strict';
 function sync(form){
  const field=form.querySelector('[name=access_level]');if(!field)return;
  for(const group of form.querySelectorAll('[data-access-department]')){
   const shown=field.value==='department';group.hidden=!shown;
   for(const input of group.querySelectorAll('input,select'))input.disabled=!shown||field.disabled;
  }
 }
 function init(){for(const root of document.querySelectorAll('[data-kb-access]')){const form=root.closest('form'),field=root.querySelector('[data-access-level]');if(!form||!field)continue;field.addEventListener('change',()=>sync(form));sync(form);}}
 global.KBAccessFields={sync};
 if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})(window);
