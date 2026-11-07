const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function submit(fields,draft=''){
  const events={},composer={submitted:0,requestSubmit(){this.submitted++;}},prompt={value:draft,disabled:false,maxLength:4000,dispatchEvent(){delete composer.questionAnswers;}},hint={};
  const messages={addEventListener(name,fn){events[name]=fn;}};
  const elements={connectorPage:{},connectorMessages:messages,composerForm:composer,connectorPrompt:prompt,connectorSend:{disabled:false},connectorHint:hint};
  const scope={document:{getElementById:id=>elements[id]},window:{},Event:class Event{constructor(type){this.type=type;}}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/js/connector_reports.js'),'utf8'),scope);
  const form={dataset:{builderQuestionForm:'current-job'},querySelectorAll(){return fields.map(field=>({name:field.key,value:field.value,dataset:{questionLabel:field.label}}));}};
  events.submit({target:{closest(){return form;}},preventDefault(){}});
  return {composer,prompt,hint};
}

test('optional blank answers explicitly continue and retain the structured field',()=>{
  const {composer,prompt}=submit([{key:'channel_note',label:'Channel name (optional)',value:''}]);
  assert.equal(composer.submitted,1);assert.match(prompt.value,/继续/);
  assert.equal(composer.questionAnswers.answers[0].key,'channel_note');
  assert.equal(composer.questionAnswers.answers[0].value,'');
});

test('workflow answer preserves its field key and complete URL independently of the label',()=>{
  const value='https://tenant.environment.api.powerplatform.com/workflows/fixture/triggers/manual/paths/invoke?sig=fixture';
  const {composer,prompt}=submit([{key:'teams_workflow_url',label:'Teams link; email link cannot be used',value}], 'Keep this draft.');
  assert.equal(composer.submitted,1);assert.match(prompt.value,/Keep this draft/);
  assert.equal(composer.questionAnswers.job,'current-job');
  assert.equal(composer.questionAnswers.answers[0].key,'teams_workflow_url');
  assert.equal(composer.questionAnswers.answers[0].value,value);
});

test('overlong answers do not discard the existing draft or submit a partial URL',()=>{
  const {composer,prompt,hint}=submit([{key:'details',label:'Details',value:'x'.repeat(5000)}], 'Existing draft');
  assert.equal(composer.submitted,0);assert.equal(prompt.value,'Existing draft');
  assert.match(hint.textContent,/4000/);
});
