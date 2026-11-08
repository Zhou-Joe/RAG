/* Fold once when tools give way to an answer; keep subsequent user toggles. */
(function (root) {
  'use strict';
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
