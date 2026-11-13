const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');

class Element{
 constructor(tag='div'){this.tagName=tag;this.children=[];this.attrs={};this.dataset={};this.className='';this._text='';this.scrollTop=0;this.scrollHeight=0;this.clientHeight=0;this.classList={contains:name=>this.className.split(/\s+/).includes(name),toggle:(name,value)=>{if(value)this.className=(this.className+' '+name).trim();else this.className=this.className.split(/\s+/).filter(item=>item!==name).join(' ');},remove:name=>{this.className=this.className.split(/\s+/).filter(item=>item!==name).join(' ');}};}
 set innerHTML(_){throw Error('Digest rendering must not insert HTML');}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
 get childElementCount(){return this.children.length;}
 setAttribute(key,value){this.attrs[key]=String(value);}
 append(...nodes){this.children.push(...nodes);}
 insertBefore(node,before){const index=this.children.indexOf(before);if(index<0)this.children.push(node);else this.children.splice(index,0,node);}
 replaceChildren(...nodes){this._text='';this.children=nodes;}
}
const all=node=>[node,...node.children.flatMap(all)];
function setup(){
 let language='zh-hans';const labels={'发送时包含简要总结和汇总数字。':'Include a concise summary and aggregated figures when sending.',
 '摘要预览':'Digest Preview','摘要要求':'Digest Instructions','汇总数字':'Aggregated Figures','记录数':'Rows','汇总指标':'Aggregated Metrics',
 '分组汇总':'Grouped Figures','计算方式':'Calculation','字段':'Field','数值':'Value','指标':'Metric','数值字段统计':'Numeric Field Statistics','有效数值':'Valid Values','缺失值':'Missing Values','非数值':'Non-numeric Values','合计':'Sum','平均值':'Average','最小值':'Minimum','最大值':'Maximum','分组数':'Groups','未展开分组':'Groups Not Expanded','未展开记录':'Rows Not Expanded',
 '摘要预览未完成。':'Digest preview is incomplete.','当前数据未满足触发条件；本次不生成或发送摘要。':'The trigger condition is not met. No digest is generated or sent.',
 '当前没有可用于摘要的数据，本次未发送。':'No data is available for a digest. Nothing was sent.',
 '摘要将在报告发送前生成。可通过 EnGenie 对话预览和调整。':'The digest will be generated before delivery. Preview and adjust it through EnGenie.'};
 labels['报告结果行数']='Report Output Rows';
 Object.assign(labels,{'原文链接':'Source Links','附上原文链接':'Include Source Links','不附原文链接':'Do Not Include Source Links','报告展示方式':'Report Presentation','摘要正文':'Digest Body','HTML 报告链接':'HTML Report Link','HTML 邮件附件':'HTML Email Attachment','当前邮件工作流尚未确认支持 HTML 附件；发送前请升级工作流并确认附件支持。':'This email workflow has not confirmed HTML attachment support. Update the workflow and confirm attachment support before sending.'});
 Object.assign(labels,{'原文链接总数':'Total Source Links','未展示链接':'Links Not Shown','已排除链接':'Rejected Links','没有可展示的原文链接。':'No source links are available.','下载 HTML 预览':'Download HTML Preview'});
 const document={createElement:tag=>new Element(tag),getElementById:()=>null};
 const scope={document,URL,FOSI18n:{t:value=>language==='en'?labels[value]||value:value}};scope.window=scope;
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/js/connector_reports.js'),'utf8'),scope);
 return {render:scope.ConnectorReportDigest.render,scope,language:value=>language=value};
}

test('disabled digest renders nothing and enabling it shows concise pending guidance',()=>{
 const h=setup();assert.equal(h.render(undefined,undefined),null);assert.equal(h.render({enabled:false},{text:'must not show'}),null);
 h.language('en');const nodes=all(h.render({enabled:true},null));
 assert.ok(nodes.some(node=>node.textContent==='EnGenie Digest'));
 assert.ok(nodes.some(node=>node.textContent==='The digest will be generated before delivery. Preview and adjust it through EnGenie.'));
});

test('presentation defaults to body without source links and warns only for unconfirmed HTML email delivery',()=>{
 const h=setup();h.language('en');
 const defaults=all(h.render({enabled:true},null,{kind:'email'}));
 assert.ok(defaults.some(node=>node.className==='cx-digest-presentation'&&node.textContent==='Do Not Include Source Links · Report Presentation: Digest Body'));
 assert.equal(defaults.some(node=>node.className==='cx-digest-capability-warning'),false);
 for(const delivery of [undefined,{}, {html_attachments_supported:false},{html_attachments_supported:'true'}]){
  const nodes=all(h.render({enabled:true,email_format:'html_attachment',include_source_links:true},null,{kind:'email',delivery}));
  assert.equal(nodes.filter(node=>node.className==='cx-digest-capability-warning').length,1);
 }
 for(const [config,context]of [[{enabled:true,email_format:'body'},{kind:'email'}],[{enabled:true,email_format:'html_link'},{kind:'email'}],[{enabled:true,email_format:'html_link'},{kind:'teams'}],[{enabled:true,email_format:'html_attachment'},{kind:'teams'}],[{enabled:true,email_format:'html_attachment'},{kind:'email',delivery:{html_attachments_supported:true}}],[{enabled:true,email_format:'html_attachment'},{}]]){
  const nodes=all(h.render(config,null,context));assert.equal(nodes.some(node=>node.className==='cx-digest-capability-warning'),false);
  if(context.kind==='teams'&&config.email_format==='html_attachment')assert.ok(nodes.some(node=>node.className==='cx-digest-presentation'&&node.textContent.endsWith('Digest Body')));
 }
 assert.equal(h.render({enabled:false,email_format:'html_attachment'},null,{kind:'email'}),null);
});

test('source URLs permit explicit HTTP(S) only and reject credentials and executable schemes',()=>{
 const safe=setup().scope.ConnectorReportDigest.safeSourceURL;
 assert.equal(safe('https://docs.example.com/Annual Report.pdf?q=A&B#page=2'),'https://docs.example.com/Annual%20Report.pdf?q=A&B#page=2');
 assert.equal(safe('http://intranet.example/item/42'),'http://intranet.example/item/42');
 for(const value of ['javascript:alert(1)','JaVaScRiPt:alert(1)','data:text/html,<script>alert(1)</script>','blob:https://example.com/id','file:///C:/secret.txt','https://user:pass@example.com/file','https://user@example.com/','//example.com/doc','/kb/manage/example/','https://example.com/\nheader',null,{}])assert.equal(safe(value),'',String(value));
});

test('deterministic source metadata renders safe literal labels and explicit omitted and rejected counts',()=>{
 const h=setup();h.language('en');const label='<img src=x onerror=alert(1)> & Original';
 const preview={text:'Review [source](javascript:alert(1)) as plain narrative.',source_links:{links:[{label,url:'https://docs.example.com/item?id=7&rev=2'},{label:'Intranet',url:'http://intranet.example/report'}],total_links:7,omitted_links:5,rejected_links:3}};
 const original=JSON.stringify(preview),nodes=all(h.render({enabled:true,include_source_links:true},preview,{kind:'teams'})),links=nodes.filter(node=>node.tagName==='a');
 assert.equal(links.length,2);assert.equal(links[0].textContent,label);assert.equal(links[0].attrs.translate,'no');assert.equal(links[0].href,'https://docs.example.com/item?id=7&rev=2');
 assert.equal(links[0].target,'_blank');assert.equal(links[0].rel,'noopener noreferrer');
 assert.ok(nodes.some(node=>node.className==='cx-meta'&&node.textContent==='Total Source Links: 7 · Links Not Shown: 5 · Rejected Links: 3'));
 assert.equal(nodes.some(node=>node.tagName==='img'),false);assert.equal(JSON.stringify(preview),original);
 assert.equal(all(h.render({enabled:true,include_source_links:false},preview)).some(node=>node.className==='cx-digest-source-links'),false);
});

test('unsafe source metadata cannot create navigable links and its discarded entries are reported',()=>{
 const h=setup();h.language('en');const source_links={links:[{label:'Script',url:'javascript:alert(1)'},{label:'Data',url:'data:text/html,Hi'},{label:'Credentials',url:'https://user:pass@example.com/'}],total_links:3,omitted_links:0,rejected_links:2};
 const nodes=all(h.render({enabled:true,include_source_links:true},{source_links}));
 assert.equal(nodes.some(node=>node.tagName==='a'),false);
 assert.ok(nodes.some(node=>node.textContent==='No source links are available.'));
 assert.ok(nodes.some(node=>node.className==='cx-meta'&&node.textContent==='Total Source Links: 3 · Rejected Links: 5'));
});

test('HTML preview download is limited to configured email mode and the protected relative route',()=>{
 const h=setup();h.language('en');const url='/kb/feeds/reports/12345678-1234-1234-1234-123456789abc/preview.xlsx?format=html';
 const preview={html:{filename:'EnGenie-report.html',download_url:url}};
 const nodes=all(h.render({enabled:true,email_format:'html_attachment'},preview,{kind:'email',delivery:{html_attachments_supported:true}})),link=nodes.find(node=>node.tagName==='a');
 assert.equal(link.href,url);assert.equal(link.textContent,'Download HTML Preview');assert.equal(link.attrs.download,'EnGenie-report.html');
 for(const [config,context]of [[{enabled:true,email_format:'body'},{kind:'email'}],[{enabled:true,email_format:'html_attachment'},{kind:'teams'}],[{enabled:true,email_format:'html_attachment'},{}]])assert.equal(all(h.render(config,preview,context)).some(node=>node.className==='cx-digest-html-preview'),false);
 for(const unsafe of ['https://evil.example'+url,'//evil.example'+url,'javascript:alert(1)',url+'&redirect=https://evil.example','/kb/feeds/reports/not-a-uuid/preview.xlsx?format=html','/kb/reports/12345678-1234-1234-1234-123456789abc/preview/?format=html']){
  const values=all(h.render({enabled:true,email_format:'html_attachment'},{html:{filename:'<b>file</b>.html',download_url:unsafe}},{kind:'email'}));
  assert.equal(values.some(node=>node.className==='cx-digest-html-preview'),false);
 }
});

test('HTML report links work for Email and Teams without attachment capability warnings and all presentation labels are explicit',()=>{
 const h=setup();h.language('en');const url='/kb/feeds/reports/12345678-1234-1234-1234-123456789abc/preview.xlsx?format=html',preview={html:{filename:'EnGenie-report.html',download_url:url}};
 for(const kind of ['email','teams']){
  const nodes=all(h.render({enabled:true,email_format:'html_link'},preview,{kind,delivery:{html_attachments_supported:false}}));
  assert.ok(nodes.some(node=>node.className==='cx-digest-presentation'&&node.textContent==='Do Not Include Source Links · Report Presentation: HTML Report Link'));
  assert.ok(nodes.some(node=>node.tagName==='a'&&node.href===url&&node.textContent==='Download HTML Preview'));
  assert.equal(nodes.some(node=>node.className==='cx-digest-capability-warning'),false);
 }
 for(const [format,label]of [['body','Digest Body'],['html_link','HTML Report Link'],['html_attachment','HTML Email Attachment']]){
  const nodes=all(h.render({enabled:true,email_format:format},null,{kind:'email',delivery:{html_attachments_supported:true}}));
  assert.ok(nodes.some(node=>node.className==='cx-digest-presentation'&&node.textContent.endsWith('Report Presentation: '+label)));
 }
 h.language('zh-hans');
 for(const [format,label]of [['body','摘要正文'],['html_link','HTML 报告链接'],['html_attachment','HTML 邮件附件']])assert.ok(all(h.render({enabled:true,email_format:format},null,{kind:'email'})).some(node=>node.className==='cx-digest-presentation'&&node.textContent.endsWith('报告展示方式: '+label)));
});

test('narrative and instructions stay literal text while system labels follow UI language',()=>{
 const h=setup(),config={enabled:true,instructions:'重点分析 <img src=x onerror=alert(1)> & anomalies'},preview={text:'<script>alert(1)</script>\nSummary & figures',facts:{row_count:3}};
 const original=JSON.stringify([config,preview]);
 for(const language of ['zh-hans','en']){
  h.language(language);const nodes=all(h.render(config,preview));
  const narrative=nodes.find(node=>node.className==='cx-digest-text');assert.equal(narrative.textContent,preview.text);assert.equal(narrative.attrs.translate,'no');
  const instructions=nodes.find(node=>node.textContent===config.instructions);assert.equal(instructions.attrs.translate,'no');
  assert.equal(nodes.some(node=>node.tagName==='script'||node.tagName==='img'),false);
  assert.ok(nodes.some(node=>node.tagName==='h5'&&node.textContent===(language==='en'?'Digest Preview':'摘要预览')));
 }
 assert.equal(JSON.stringify([config,preview]),original);
});

test('full-result metrics and explicitly bounded groups use compact readable facts without interpreting names as HTML',()=>{
 const h=setup();h.language('en');const metric={label:'<svg onload=alert(1)>',field:'Amount',op:'sum',value:25,quality:{count:29,missing:1,non_numeric:0}};
 const facts={scope:'final_report_output',row_count:30,metrics:[metric],numeric_fields:{},group_by:['Department'],
  groups:Array.from({length:20},(_,index)=>({keys:{Department:index===0?'Engineering':'Group '+index},row_count:1,metrics:[{...metric,value:1,quality:{count:1,missing:0,non_numeric:0}}],numeric_fields:{}})),
  total_groups:25,omitted_groups:5,omitted_row_count:10};
 const nodes=all(h.render({enabled:true},{text:'Four records reviewed.',facts}));
 const tables=nodes.filter(node=>node.tagName==='table');assert.equal(tables.length,21);
 assert.ok(nodes.some(node=>node.className==='cx-digest-overview'&&node.textContent==='Report Output Rows: 30 · Groups: 25'));
 const rawField=nodes.find(node=>node.tagName==='td'&&node.textContent==='<svg onload=alert(1)>');assert.equal(rawField.attrs.translate,'no');
 assert.ok(nodes.some(node=>node.tagName==='td'&&node.textContent==='25'));
 assert.ok(nodes.some(node=>node.tagName==='span'&&node.textContent==='Department: Engineering'&&node.attrs.translate==='no'));
 assert.ok(nodes.some(node=>node.className==='cx-digest-quality'&&node.textContent.includes('Missing Values: 1')));
 assert.ok(nodes.some(node=>node.className==='cx-digest-omission'&&node.textContent==='Groups Not Expanded: 5 · Rows Not Expanded: 10'));
 const groups=nodes.filter(node=>node.className==='cx-digest-fact-group');assert.equal(groups.length,20);
 for(const group of groups){assert.equal(group.tagName,'details');assert.equal(group.open,undefined);}
 assert.equal(nodes.some(node=>node.textContent==='final_report_output'),false);
});

test('default numeric field statistics preserve exact decimals behind a collapsed details control',()=>{
 const h=setup();h.language('en');const facts={scope:'final_report_output',row_count:6,metrics:[],numeric_fields:{'Unit Cost':{count:4,missing:1,non_numeric:1,sum:'12345678901234567890.123',avg:'3086419725308641972.53075',min:'1.001',max:'12345678901234567886.12'}},group_by:[],groups:[],total_groups:0,omitted_groups:0,omitted_row_count:0};
 const nodes=all(h.render({enabled:true},{text:'Reviewed.',facts})),detail=nodes.find(node=>node.className==='cx-digest-numeric');
 assert.equal(detail.tagName,'details');assert.equal(detail.open,undefined);
 assert.ok(nodes.some(node=>node.tagName==='td'&&node.textContent==='12345678901234567890.123'&&node.attrs.translate==='no'));
 for(const heading of ['Field','Valid Values','Missing Values','Non-numeric Values','Sum','Average','Minimum','Maximum'])assert.ok(nodes.some(node=>node.tagName==='th'&&node.textContent===heading));
 assert.equal(nodes.some(node=>node.className==='cx-digest-groups'),false);
});

test('skipped and failed previews show truthful states while retaining available facts',()=>{
 const h=setup();h.language('en');
 for(const [reason,message]of [['condition_not_met','The trigger condition is not met. No digest is generated or sent.'],['no_data','No data is available for a digest. Nothing was sent.']]){
  const nodes=all(h.render({enabled:true},{status:'skipped',reason,facts:{row_count:0}}));
  assert.ok(nodes.some(node=>node.textContent===message));assert.ok(nodes.some(node=>node.tagName==='strong'&&node.textContent==='0'));
  assert.equal(nodes.some(node=>node.className==='cx-digest-text'),false);
 }
 const nodes=all(h.render({enabled:true},{error:'Model <b>unavailable</b>',facts:{row_count:2}}));
 assert.ok(nodes.some(node=>node.textContent==='Digest preview is incomplete.'));
 assert.ok(nodes.some(node=>node.className==='cx-digest-text'&&node.textContent==='Model <b>unavailable</b>'&&node.attrs.translate==='no'));
});

test('digest activity label stays English even if the shared helper falls back to its unknown tool name',()=>{
 const nodes=new Map(),document={createElement:tag=>new Element(tag),getElementById:id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);},querySelector:()=>new Element(),querySelectorAll:()=>[]};
 const scope={document,setInterval:()=>0,FosToolText:{name:name=>name,message:(_message,fallback)=>fallback}};scope.window=scope;
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/js/connector_activity.js'),'utf8'),scope);
 const activity=new scope.ConnectorActivity();activity.render({id:'digest',status:'ready',limits:{model_calls:10,tool_calls:20},events:[{id:1,kind:'tool_start',tool:'preview_engenie_digest',call_id:'digest-call'}]});
 const label=all(nodes.get('connectorTimeline')).find(node=>node.className==='agent-tool-name');
 assert.equal(label.textContent,'Preview EnGenie Digest');assert.equal(label.attrs.translate,'no');
});

test('real report preview card includes the digest and refreshes its narrative without changing report table data',()=>{
 const h=setup();
 h.scope.el=(tag,cls,text)=>{const value=new Element(tag);value.style={};value.classList.add=name=>{value.className=(value.className+' '+name).trim();};
  value.querySelector=selector=>all(value).find(node=>node.tagName===selector)||null;if(cls)value.className=cls;if(text!==undefined&&text!==null)value.textContent=text;return value;};
 h.scope.schedule=()=> 'Manual';
 const source=fs.readFileSync(path.join(__dirname,'../../static/js/connector.js'),'utf8');
 vm.runInNewContext(source.slice(source.indexOf('  function previewCard(job)'),source.indexOf('  function turn(job, index)'))+'\nglobalThis.renderReportCard=previewCard;',h.scope);
 const job={id:'report',status:'ready',spec:{kind:'report',title:'Report',schedule:{mode:'manual'},digest:{enabled:true,instructions:'Raw <plan>'}},
  preview:{items:1,headers:['Amount'],preview:[{Amount:'123.45'}],valid:true,digest_preview:{text:'First <digest>',facts:{row_count:1}}}};
 const original=JSON.stringify(job);let nodes=all(h.scope.renderReportCard(job));
 assert.ok(nodes.some(node=>node.className==='cx-digest-text'&&node.textContent==='First <digest>'));
 assert.ok(nodes.some(node=>node.tagName==='td'&&node.textContent==='123.45'));assert.equal(JSON.stringify(job),original);
 job.preview.digest_preview.text='Revised digest';nodes=all(h.scope.renderReportCard(job));
 assert.ok(nodes.some(node=>node.className==='cx-digest-text'&&node.textContent==='Revised digest'));
 job.spec.digest.enabled=false;assert.equal(all(h.scope.renderReportCard(job)).some(node=>node.className==='cx-report-digest'),false);
});

test('digest settings and fact labels use the shipped bilingual catalog in both directions',()=>{
 const catalog=JSON.parse(fs.readFileSync(path.join(__dirname,'../../static/i18n/report_digest.json'),'utf8'));
 const scope={FOS_I18N_CATALOG:catalog,module:{exports:{}}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/js/interface_language.js'),'utf8'),scope);
 const translate=scope.module.exports.translate;
 for(const source of ['添加 EnGenie Digest','摘要要求','Digest 语言','保存发送设置','报告结果行数','汇总数字','未展开分组','Digest 要求不能超过 2000 字符。','EnGenie Digest 配置无效。','EnGenie Digest 生成失败或超时，本次未发送；请检查模型或调整摘要要求。','EnGenie 摘要设置已保存；发送计划保持不变。','此邮件工作流已映射附件','包含来源链接','Digest 邮件格式','下载 HTML 预览','已排除链接','报告展示方式','摘要正文','HTML 报告链接','HTML 邮件附件']){
  assert.equal(translate(source,'en'),catalog.messages[source]);
  assert.equal(translate(catalog.messages[source],'zh'),source);
 }
 const label='业务数据 <span>& Amount',message='EnGenie Digest 指标「'+label+'」包含未解析的非数值，请先在报告中配置数值转换。';
 const formatError='EnGenie Digest 邮件格式仅支持正文、HTML 链接或 HTML 附件。';
 assert.equal(translate(formatError,'en'),catalog.messages[formatError]);assert.equal(translate(catalog.messages[formatError],'zh'),formatError);
 assert.equal(translate(message,'en'),'EnGenie Digest metric “'+label+'” contains unparsed non-numeric values. Configure numeric conversion in the report first.');
 assert.equal(translate(translate(message,'en'),'zh'),message);
});

test('HTML chart and stale-preview validation errors translate through the shipped catalog without altering field names',()=>{
 const catalog=JSON.parse(fs.readFileSync(path.join(__dirname,'../../static/i18n/report_digest.json'),'utf8'));
 const scope={FOS_I18N_CATALOG:catalog,module:{exports:{}}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../static/js/interface_language.js'),'utf8'),scope);
 const translate=scope.module.exports.translate;
 const chartSource=fs.readFileSync(path.join(__dirname,'../../kb/report_chart_specs.py'),'utf8');
 const errors=[...chartSource.matchAll(/raise ValueError\('([^'\r\n]+)'\)/g)].map(match=>match[1]);
 assert.ok(errors.length>=20,'The chart validation errors must be read from the real backend source');
 const previewSource=fs.readFileSync(path.join(__dirname,'../../kb/report_views.py'),'utf8');
 for(const message of ['请先启用 EnGenie Digest 并生成预览。','摘要预览尚未生成，请返回 EnGenie 重新预览。','报告数据已变化，请重新生成摘要预览。']){
  assert.ok(previewSource.includes(message));errors.push(message);
 }
 for(const source of errors){
  const target=catalog.messages[source];assert.equal(typeof target,'string',source);
  assert.ok(target.length>0);assert.doesNotMatch(target,/[\u3400-\u9fff]/u);
  assert.equal(translate(source,'en'),target);assert.equal(translate(target,'zh'),source);
 }
 const dynamic=[...chartSource.matchAll(/raise ValueError\(f'([^'\r\n]+)'\)/g)].map(match=>match[1]);
 assert.equal(dynamic.length,1);assert.ok(catalog.patterns.some(pattern=>pattern.source===dynamic[0]));
 const field='业务数据 <span onclick="alert(1)">& Amount',message=dynamic[0].replace('{field}',field);
 assert.equal(translate(message,'en'),'HTML chart numeric field “'+field+'” contains values that could not be converted. Configure a report conversion first.');
 assert.equal(translate(translate(message,'en'),'zh'),message);
});
