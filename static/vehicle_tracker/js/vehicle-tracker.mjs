import {PARTS,PART_MAP,CHILDREN,TYPES,KINDS,COACHES,CARRIERS,SPEC_DOC,LINKS,linkable,latestRecord,drawingSummary,within,uid,log,newProject,addRecord,bindRecord,unbindRecord,guessMapping,importRows,backup,restoredCopies,csv,ProjectStore,assertCapacity,MAX_BACKUP_BYTES} from './vehicle-tracker-core.mjs';
import {REPORT,reportProject} from './vehicle-tracker-report.mjs';

const $=id=>document.getElementById('vt-'+id);
const t=text=>globalThis.FOSI18n?.t(text)||text;
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=value=>new Date(value).toLocaleString(window.FOSI18n?.locale || 'zh-CN',{hour12:false});
const fileSize=size=>size<1024*1024?`${(size/1024).toFixed(1)} KB`:`${(size/1024/1024).toFixed(1)} MB`;
const label=id=>project?.parts[id]?.name||PART_MAP.get(id)?.name||id;
const where=id=>`${PART_MAP.get(id).position} / ${label(id)}`;
// Translate the built-in structure without changing editable names or persisted records.
const displayLabel=id=>{const original=PART_MAP.get(id)?.name,value=label(id);return value===original?t(value):value;};
const displayPosition=part=>t(typeof part==='string'?PART_MAP.get(part).position:part.position);
const displayWhere=id=>`${displayPosition(id)} / ${displayLabel(id)}`;
const sourceText=r=>`${r.source==='手工登记'?t(r.source):r.source}${r.sourceRow?` · ${t(r.sourceUnit==='页'?`第 ${r.sourceRow} 页`:`行 ${r.sourceRow}`)}`:''}`;
const woButton=(r,wonum=r.wonum)=>linkable(wonum)?`<button type="button" class="vt-wo" data-wo="${escape(wonum)}" data-record="${escape(r.id)}" title="打开 Maximo / MRB 工单">${escape(wonum)}</button>`:escape(wonum||'—');
const drawingCode=value=>String(value??'').trim().replace(/\s+Item\s*\d+\s*$/i,'');
const partNoButton=label=>{const code=drawingCode(label);return linkable(code)?`<button type="button" class="vt-dwg" data-part-no="${escape(code)}" title="查看图号出处、原PDF位置与安装记录">${escape(label)}</button>`:escape(label||'—');};
let store,projects=[],project,model,assistant,sources,recordsView,selected='TRAIN',query='',page=0,detail='info',view='model',referenceId='',hotspotId='',annotating=false;
let drawing='',drawingTab='sources',drawingFilter='',drawingMarked=false;const qaqcCache=new Map();
let checked=new Set(),expanded=new Set(['TRAIN','C1','C1-LF']),saveQueue=Promise.resolve(),saveVersion=0,toastTimer;
const PAGE_SIZE=50;
function toast(message,error=false) {$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,error?9000:4500);}
function safe(fn) {return async(...args)=>{try{await fn(...args);}catch(e){console.error(e);toast(e.message||'操作失败，请重试',true);}};}
function on(id,event,fn) {$(id).addEventListener(event,safe(fn));}
function persist() {
  const version=++saveVersion,snapshot=structuredClone(project);
  $('save-status').textContent='正在保存…';$('save-status').classList.remove('error');
  saveQueue=saveQueue.catch(()=>{}).then(()=>store.put(snapshot)).then(()=>{
    if(version===saveVersion)$('save-status').textContent=`✓ 已保存到本机浏览器 · ${new Date().toLocaleTimeString(window.FOSI18n?.locale || 'zh-CN',{hour12:false})}`;
  }).catch(e=>{
    $('save-status').textContent='保存失败，请立即导出完整备份';$('save-status').classList.add('error');toast('本地保存失败（空间不足或浏览器限制），请使用「完整备份」导出当前数据。',true);throw e;
  });
  // Preserve the rejection for callers while avoiding an unhandled promise.
  saveQueue.catch(()=>{});return saveQueue;
}
function dialog(title,html,sourceTitle=false) {$('dialog-title').setAttribute('translate',sourceTitle?'no':'yes');$('dialog').dispatchEvent(new Event('vt:preview-reset'));$('dialog-title').textContent=title;$('dialog-body').innerHTML=html;if(!$('dialog').open)$('dialog').showModal();}
function closeDialog() {$('dialog').dispatchEvent(new Event('vt:preview-reset'));$('dialog').close();$('dialog-body').innerHTML='';}
function dialogError(e) {let el=$('dialog-body').querySelector('.vt-dialog-error');if(!el){el=document.createElement('p');el.className='vt-dialog-error';el.setAttribute('role','alert');$('dialog-body').append(el);}el.textContent=e.message;}
function dialogAction(id,fn) {document.getElementById(id).addEventListener('click',async e=>{const button=e.currentTarget;button.disabled=true;try{await fn();}catch(error){dialogError(error);}finally{if(button.isConnected)button.disabled=false;}});}
function download(content,name,type='application/json') {const blob=content instanceof Blob?content:new Blob([content],{type}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);}
function projectOptions() {$('project').innerHTML=projects.map(p=>`<option value="${escape(p.id)}" translate="no">${escape(p.name)}</option>`).join('');$('project').value=project.id;}
function stats() {
  const bound=project.records.filter(r=>r.partId).length;
  $('record-count').textContent=project.records.length;
  $('table-stats').textContent=`${bound} 已绑定 / ${project.records.length-bound} 待绑定`;
  $('project-stats').textContent=`${PARTS.length} 个部件 · ${project.references.length} 张参考图 · ${project.attachments.length} 个附件`;
  $('ref-count').textContent=project.references.length;
  $('part-count').textContent=PARTS.length;
  $('overview-records').textContent=project.records.length;$('overview-bound').textContent=bound;
}
function matching(text) {return String(text).normalize('NFKC').toLowerCase().includes(query.normalize('NFKC').toLowerCase());}
function recordMatches(r) {return matching([r.wonum,r.serial,r.drawing,r.inspector,r.slot,r.reportWo,r.description,r.date,r.partId,r.partId?label(r.partId):'',r.partId?PART_MAP.get(r.partId)?.position:'',r.source].join(' '));}
function partMatches(p) {const k=KINDS[p.kind];return matching([p.id,p.name,label(p.id),displayLabel(p.id),p.position,displayPosition(p),p.code,k.en,k.dwg,k.shdr].join(' '));}
function latestByPart() {
  const latest=new Map();
  for(const r of project.records) if(r.partId){const best=latest.get(r.partId);if(!best||(r.date||'')>=(best.date||''))latest.set(r.partId,r);}
  return latest;
}
function renderTree() {
  const scroll=$('tree').scrollTop,latest=latestByPart(),tracked=new Set();
  for(const id of latest.keys()){let p=PART_MAP.get(id);while(p){tracked.add(p.id);p=PART_MAP.get(p.parent);}}
  let matches;
  if(query) {
    const hits=new Set(PARTS.filter(partMatches).map(p=>p.id));
    project.records.filter(recordMatches).forEach(r=>{if(r.partId)hits.add(r.partId);});
    matches=new Set(hits);for(const id of hits){let p=PART_MAP.get(id);while(p?.parent){matches.add(p.parent);p=PART_MAP.get(p.parent);}}
  }
  const icons={train:'▰',coach:'▱',body:'◫',chassis:'═',towbar:'⊸',dog:'⌖',carrier:'⊟',frame:'⊏',bracket:'⊓',wheel:'◉',bearing:'◎',pin:'┃'};
  const row=(id,depth)=>{
    if(matches&&!matches.has(id))return '';
    const p=PART_MAP.get(id),children=CHILDREN.get(id),open=expanded.has(id)||!!query,bound=tracked.has(id),serial=latest.get(id)?.serial;
    return `<div class="vt-tree-row ${selected===id?'selected':''} type-${p.type}" role="treeitem" aria-level="${depth+1}" aria-selected="${selected===id}" ${children.length?`aria-expanded="${open}"`:''} data-part="${id}" style="padding-left:${depth*13+3}px"><button class="vt-tree-caret" data-expand="${id}" ${children.length?'':'disabled tabindex="-1"'} aria-label="${t(open?'折叠':'展开')} ${escape(displayLabel(id))}">${children.length?(open?'⌄':'›'):''}</button><span class="vt-tree-type" aria-hidden="true">${icons[p.type]}</span><button class="vt-tree-label" data-select="${id}" title="${escape(displayPosition(p)+' · '+t(KINDS[p.kind].en)+' · '+id)}"><span class="vt-tree-name" translate="no">${escape(displayLabel(id))}</span>${serial?`<small class="vt-tree-sn">${escape(serial)}</small>`:''}</button><i class="vt-dot ${bound?'teal':'gray'}" title="${bound?'此部件或下级已有追踪记录':'暂无追踪记录'}"></i></div>`+(open?children.map(c=>row(c,depth+1)).join(''):'');
  };
  $('tree').innerHTML=row('TRAIN',0)||'<p class="vt-empty">没有匹配的部件</p>';$('tree').scrollTop=scroll;
  renderNav(latest);
}
const TAGGED=new Set(['carrierPin','loadPin','guidePin','upstopPin','loadSpindle','guideSpindle','upstopSpindle']);
let navCollapsed=(()=>{try{return localStorage.getItem('shdr-vehicle-nav')==='collapsed';}catch{return false;}})();
const CARRIER_TAGS=PARTS.filter(p=>p.carrier&&TAGGED.has(p.kind)).length/(COACHES.length*Object.keys(CARRIERS).length);
/** Train plan, C5 → C1 left to right as in the default 3D view: far row = left carriers, near row = right. */
function renderNav(latest=latestByPart()) {
  const cell=(c,wc)=>{
    const id=`${c}-${wc}`,tags=[...latest.keys()].filter(k=>within(k,id)&&TAGGED.has(PART_MAP.get(k).kind)).length;
    const state=tags>=CARRIER_TAGS?'full':tags?'part':'none',active=within(selected,id);
    return `<button type="button" class="vt-nav-cell ${state}${active?' active':''}" data-nav="${id}" title="${escape(displayPosition(id))} · 绿标 ${tags}/${CARRIER_TAGS}">${wc}</button>`;
  };
  $('nav').innerHTML=`<button type="button" class="vt-nav-title" data-nav-toggle aria-expanded="${!navCollapsed}">轮架速查 <small>${navCollapsed?'展开 ▴':'行进方向 → · 收起 ▾'}</small></button><div class="vt-nav-train"${navCollapsed?' hidden':''}>${[...COACHES].reverse().map(({no})=>{const c=`C${no}`;return `<div class="vt-nav-coach${within(selected,c)?' active':''}"><button type="button" class="vt-nav-name" data-nav="${c}">${c}</button><div class="vt-nav-grid">${cell(c,'LR')}${cell(c,'LF')}${cell(c,'RR')}${cell(c,'RF')}</div></div>`;}).join('')}</div>`;
}
function crumbs(id) {
  const path=[];for(let p=PART_MAP.get(id);p;p=PART_MAP.get(p.parent))path.unshift(p.id);
  return path.map((pid,i)=>i===path.length-1?`<strong translate="no">${escape(pid==='TRAIN'?t('全车'):displayLabel(pid))}</strong>`:`<button type="button" data-crumb="${pid}" translate="no">${escape(pid==='TRAIN'?t('全车'):displayLabel(pid))}</button>`).join('<i>›</i>');
}
function selectPart(id,{reveal=false,fly=false}={}) {
  if(!PART_MAP.has(id))return;
  if(reveal&&query){query='';$('search').value='';page=0;}
  selected=id;let p=PART_MAP.get(id);while(p?.parent){expanded.add(p.parent);p=PART_MAP.get(p.parent);}
  model?.select(id);renderTree();renderDetails();renderTable();renderHotspots();
  $('selection-label').innerHTML=crumbs(id);
  if(fly&&model)id==='TRAIN'?model.fit():model.focus(id);
  $('binding-target').textContent=t(`绑定目标：${displayWhere(id)}`);
  assistant?.refresh();
  if(reveal)$('tree').querySelector(`[data-part="${id}"]`)?.scrollIntoView({block:'nearest'});
}
function specCard(id) {
  const p=PART_MAP.get(id),k=KINDS[p.kind],current=latestRecord(project,id);
  const rows=[['英文名称',escape(k.en)],['WDI 图号',k.dwg?partNoButton(k.dwg):'—'],['WDC No.',escape(k.wdc||'—')]];
  if(k.shdr)rows.push(['SHDR 编码',partNoButton(k.shdr)]);
  if(p.code)rows.push(['报告位置码',escape(p.code)]);
  if(k.req)rows.push(['检验要求',`${escape(k.req.method)}<br><small>${escape(k.req.frequency)} · Job Plan <button type="button" class="vt-dwg" data-jp="${escape(k.req.jobPlan)}">${escape(k.req.jobPlan)}</button></small>`]);
  const serial=current?`<div class="vt-current"><span>当前序列号</span><strong>${escape(current.serial)}</strong><small>绿标工单 ${woButton(current)} · ${escape(current.inspector||'—')} · ${escape(current.date||'—')}</small></div>`:(k.req?'<div class="vt-current empty"><span>当前序列号</span><strong>未登记</strong><small>在「追踪」页登记工单与序列号</small></div>':'');
  return `${serial}<dl class="vt-spec">${rows.map(([t,d])=>`<dt>${t}</dt><dd>${d}</dd>`).join('')}</dl><p class="vt-subtle vt-spec-source">名称、图号与检验要求依据 ${SPEC_DOC}（部件清单 / 第 4 节）。</p>`;
}
function recordCard(r) {
  const links=[['工单资料','maximo-workorder'],['MRB / 检验','qaqc-workorder'],['工单 PDF','workorder-pdf']].filter(()=>linkable(r.wonum));
  return `<article class="vt-record-card"><header>${woButton(r)}<span>${escape([r.inspector,r.date].filter(Boolean).join(' · ')||'—')}</span></header><dl><dt>序列号</dt><dd><strong class="vt-serial">${escape(r.serial||'—')}</strong></dd><dt>图号</dt><dd>${partNoButton(r.drawing)}</dd>${r.slot?`<dt>位置码</dt><dd translate="no">${escape(r.slot)}</dd>`:''}<dt>部件</dt><dd>${escape(displayWhere(r.partId))}</dd>${r.reportWo?`<dt>总装报告</dt><dd>${woButton(r,r.reportWo)}</dd>`:''}${r.description&&r.source==='手工登记'?`<dt>说明</dt><dd translate="no">${escape(r.description)}</dd>`:''}<dt>来源</dt><dd>${escape(sourceText(r))}</dd></dl><div class="vt-card-actions">${links.map(([name,kind])=>`<button type="button" data-inline-kind="${kind}" data-inline-code="${escape(r.wonum)}" data-jump="${escape(name)}" data-record="${escape(r.id)}">${name}</button>`).join('')}<button data-locate-record="${r.id}">在表中核对</button>${r.source==='手工登记'?`<button data-delete-record="${r.id}">删除</button>`:''}</div></article>`;
}
function registerForm(id) {
  const current=latestRecord(project,id);
  return `<form id="vt-register" class="vt-register"><strong>为此部件登记工单</strong><p class="vt-subtle">例如年度销轴 MT/UT 检验或更换后的检验工单。序列号默认沿用当前序列号，更换后请改为新件序列号。</p><div class="vt-register-grid"><label>工单号 *<input name="wonum" maxlength="40" required placeholder="如 3281713"></label><label>序列号 *<input name="serial" maxlength="200" required value="${escape(current?.serial||'')}"></label><label>检验日期<input name="date" type="date" value="${new Date().toISOString().slice(0,10)}"></label><label>检验员<input name="inspector" maxlength="60" placeholder="如 QA16"></label></div><label>说明<input name="description" maxlength="500" placeholder="如 年度 MT + UT，合格"></label><button class="vt-primary" type="submit">登记到 ${escape(displayLabel(id))}</button></form>`;
}
function renderDetails() {
  const p=PART_MAP.get(selected),meta=project.parts[selected]||{},records=project.records.filter(r=>within(r.partId,selected)),files=project.attachments.filter(f=>within(f.partId,selected));
  $('selected').innerHTML=`<span class="vt-type-pill">${escape(t(TYPES[p.type]))} / ${p.id}</span><h3 translate="no">${escape(displayLabel(selected))}</h3><p translate="no">${escape(displayPosition(p))}</p>`;
  $('spec').innerHTML=specCard(selected);
  $('part-name').value=meta.name||p.name;$('part-id').value=p.id;$('part-position').value=displayPosition(p);$('part-notes').value=meta.notes||'';
  $('bound-count').textContent=records.length;$('file-count').textContent=files.length;
  records.sort((a,b)=>(b.date||'').localeCompare(a.date||''));
  $('detail-records').innerHTML=(selected==='TRAIN'||p.type==='coach'?'':registerForm(selected))+(records.length?records.slice(0,100).map(recordCard).join('')+(records.length>100?'<p class="vt-subtle">仅显示前 100 条记录，请在下方表格核对全部记录。</p>':''):'<div class="vt-empty"><h3>尚未关联追踪记录</h3><p>登记工单，或在下方追踪表勾选记录后点击「绑定到所选部件」。</p></div>');
  $('files').innerHTML=files.map(f=>`<div class="vt-file"><div><strong translate="no">${escape(f.name)}</strong><small>${fileSize(f.size)} · ${escape(displayLabel(f.partId))}</small></div><button data-preview="${f.id}">查看</button><button data-download="${f.id}" aria-label="下载 ${escape(f.name)}">↓</button></div>`).join('');
  const history=project.history.filter(h=>!h.partId||within(h.partId,selected)).slice().reverse();
  if(drawing)renderDrawing();
  $('history').innerHTML=history.length?history.slice(0,200).map(h=>`<div class="vt-history-item"><time>${escape(fmt(h.at))}</time><strong>${escape(h.action)}</strong><p translate="no">${escape(h.detail)}</p></div>`).join('')+(history.length>200?'<p class="vt-subtle">显示最近 200 条；完整履历包含在项目备份中。</p>':''):'<p class="vt-empty">暂无操作记录</p>';
}
function filteredRecords() {
  const filter=$('record-filter').value;
  return project.records.filter(r=>(!query||recordMatches(r))&&(filter==='all'||(filter==='unbound'?!r.partId:within(r.partId,selected))));
}
function renderTable() {
  const rows=filteredRecords(),pages=Math.max(1,Math.ceil(rows.length/PAGE_SIZE));page=Math.max(0,Math.min(page,pages-1));
  const current=rows.slice(page*PAGE_SIZE,(page+1)*PAGE_SIZE);
  $('records').innerHTML=current.map(r=>`<tr class="${checked.has(r.id)?'checked':''}"><td><input type="checkbox" data-check="${r.id}" aria-label="选择记录 ${escape(r.wonum)} / ${escape(r.serial)}" ${checked.has(r.id)?'checked':''}></td><td>${woButton(r)}</td><td><strong class="vt-serial">${escape(r.serial||'—')}</strong></td><td>${partNoButton(r.drawing)}</td><td>${r.inspector||r.date?`<span class="vt-qqc">${escape([r.inspector,r.date].filter(Boolean).join(' · '))}</span>`:'—'}</td><td>${r.partId?`<button data-select="${r.partId}" title="${escape(r.partId)}">${escape(displayWhere(r.partId))}</button>`:'<span class="vt-unbound">○ 待人工绑定</span>'}</td><td translate="no">${escape(r.slot||'—')}</td><td translate="no" title="${escape(sourceText(r)+(r.description?' · '+r.description:''))}">${escape(sourceText(r))}</td></tr>`).join('');
  $('records-empty').hidden=rows.length>0;
  $('page').textContent=`${rows.length? page*PAGE_SIZE+1:0}–${Math.min((page+1)*PAGE_SIZE,rows.length)} / ${rows.length}`;
  $('prev').disabled=page===0;$('next').disabled=page===pages-1;
  const count=current.filter(r=>checked.has(r.id)).length;
  $('check-page').checked=current.length>0&&count===current.length;$('check-page').indeterminate=count>0&&count<current.length;
  $('checked-count').textContent=checked.size?`已勾选 ${checked.size} 条`:'未勾选记录';
  $('bind').disabled=!checked.size;$('unbind').disabled=!project.records.some(r=>checked.has(r.id)&&r.partId);
  stats();
}
function reference() {return project.references.find(r=>r.id===referenceId);}
function renderReferences() {
  if(!reference())referenceId=project.references[0]?.id||'';
  $('reference-select').innerHTML=project.references.length?project.references.map(r=>`<option value="${r.id}" translate="no">${escape(r.name)}</option>`).join(''):'<option value="">尚无参考图</option>';
  $('reference-select').value=referenceId;
  const ref=reference();$('reference-empty').hidden=!!ref;$('reference-image-wrap').hidden=!ref;$('annotate').disabled=!ref;
  if(ref){$('reference-image').src=ref.data;$('reference-image').alt=ref.name;}
  fitReference();renderHotspots();stats();
}
function fitReference() {
  const ref=reference();if(!ref)return;
  const host=$('reference-drop'),w=Math.max(1,host.clientWidth-24),h=Math.max(1,host.clientHeight-24),scale=Math.min(w/ref.width,h/ref.height);
  $('reference-image-wrap').style.width=`${ref.width*scale}px`;$('reference-image-wrap').style.height=`${ref.height*scale}px`;
}
function renderHotspots() {
  $('hotspots').innerHTML=(reference()?.hotspots||[]).map((h,i)=>`<button class="vt-hotspot ${h.partId===selected?'selected':''}" data-hotspot="${h.id}" style="left:${h.x*100}%;top:${h.y*100}%" aria-label="热点 ${i+1}：${escape(h.label)}，${escape(displayPosition(h.partId))}" title="${escape(h.label+' · '+displayPosition(h.partId))}">${i+1}</button>`).join('');
  $('delete-hotspot').disabled=!reference()?.hotspots.some(h=>h.id===hotspotId);
}
function setView(next) {view=next;document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===next));$('model').hidden=next!=='model';$('reference').hidden=next!=='reference';$('camera').disabled=next!=='model';$('focus').disabled=next!=='model';$('fit').disabled=next!=='model';if(next==='reference')fitReference();else model?.resize();}
function setDetail(next) {detail=next;document.querySelectorAll('[data-detail]').forEach(b=>{b.classList.toggle('active',b.dataset.detail===next);b.setAttribute('aria-selected',String(b.dataset.detail===next));});document.querySelectorAll('[data-detail-panel]').forEach(p=>p.hidden=p.dataset.detailPanel!==next);}
async function switchProject(id) {
  await saveQueue;
  project=projects.find(p=>p.id===id)||projects[0];checked.clear();page=0;referenceId='';hotspotId='';query='';$('search').value='';$('record-filter').value='all';annotating=false;$('annotate').setAttribute('aria-pressed','false');$('reference-image-wrap').classList.remove('annotating');
  try{localStorage.setItem('shdr-vehicle-active',project.id);}catch{}
  projectOptions();selectPart('TRAIN');renderReferences();stats();$('save-status').textContent='✓ 已打开本机浏览器中的项目';
}
function manageProjects() {
  dialog('项目管理',`<p class="vt-subtle">每个项目对应一列车，单独保存追踪记录、参考图、热点与附件。数据保存在当前浏览器，跨电脑使用请导出完整备份。</p><label>当前项目名称<input id="vt-edit-project-name" maxlength="200" value="${escape(project.name)}"></label><label>项目说明<textarea id="vt-edit-project-description" maxlength="10000" rows="3">${escape(project.description)}</textarea></label><div class="vt-dialog-actions"><button id="vt-create-empty">新建空白列车项目</button><button id="vt-create-report">新建 5# 车总装报告项目</button><button id="vt-save-project" class="vt-primary">保存项目信息</button></div>`);
  dialogAction('vt-save-project',async()=>{const name=document.getElementById('vt-edit-project-name').value.trim();if(!name)throw Error('请输入项目名称');project.name=name;project.description=document.getElementById('vt-edit-project-description').value;log(project,'修改项目信息');await persist();projectOptions();closeDialog();toast('项目信息已保存');});
  for(const [id,seeded] of [['vt-create-empty',false],['vt-create-report',true]])dialogAction(id,async()=>{if(projects.length>=100)throw Error('本浏览器最多保存 100 个项目');const p=seeded?reportProject():newProject('新建列车项目');await store.put(p);projects.push(p);await switchProject(p.id);closeDialog();toast('项目已创建，可在「项目管理」中修改名称');});
}
function confirmBinding(unbind=false) {
  const records=project.records.filter(r=>checked.has(r.id)),target=selected,projectId=project.id;
  const affected=unbind?records.filter(r=>r.partId):records.filter(r=>!r.partId),conflicts=records.filter(r=>r.partId&&r.partId!==target);
  if(!affected.length){toast(unbind?'所选记录没有绑定关系':'所选记录均已绑定；调整关联前请先解除原绑定',true);return;}
  dialog(unbind?'确认解除绑定':'确认人工绑定',`<p>${unbind?'将解除以下记录与部件之间的关联。':'请核对目标部件与记录后确认。'}</p><div class="vt-summary"><strong>${unbind?'解除绑定的记录数：'+affected.length:escape(displayWhere(target))}</strong><p class="vt-subtle">${unbind?'工单、绿标与序列号仍保留在追踪表中。':escape(target)+' · '+affected.length+' 条未绑定记录'}</p></div>${!unbind&&records.length!==affected.length?`<p class="vt-warning">另有 ${records.length-affected.length} 条已绑定记录将跳过${conflicts.length?'（含绑定其他部件的记录）':''}。</p>`:''}<div class="vt-import-preview"><table><thead><tr><th>工单号</th><th>序列号</th><th>图号</th><th>原绑定</th></tr></thead><tbody>${affected.map(r=>`<tr><td>${escape(r.wonum)}</td><td>${escape(r.serial)}</td><td>${escape(r.drawing)}</td><td>${escape(r.partId||'未绑定')}</td></tr>`).join('')}</tbody></table></div><div class="vt-dialog-actions"><button id="vt-cancel-binding">取消</button><button id="vt-confirm-binding" class="vt-primary">${unbind?'确认解除':'确认绑定'}</button></div>`);
  document.getElementById('vt-cancel-binding').onclick=closeDialog;
  dialogAction('vt-confirm-binding',async()=>{if(project.id!==projectId)throw Error('项目已变化，请重新选择');for(const r of affected)unbind?unbindRecord(project,r.id):bindRecord(project,r.id,target);checked.clear();await persist();renderTree();renderDetails();renderTable();closeDialog();toast(`${affected.length} 条记录已${unbind?'解除绑定':'绑定到 '+label(target)}`);});
}
function bindDrop(id,handler) {
  const el=$(id);let depth=0;
  el.addEventListener('dragenter',e=>{if(!e.dataTransfer.types.includes('Files'))return;e.preventDefault();depth++;el.classList.add('dragover');});
  el.addEventListener('dragover',e=>{if(e.dataTransfer.types.includes('Files')){e.preventDefault();e.dataTransfer.dropEffect='copy';}});
  el.addEventListener('dragleave',()=>{if(--depth<=0){depth=0;el.classList.remove('dragover');}});
  el.addEventListener('drop',safe(async e=>{e.preventDefault();depth=0;el.classList.remove('dragover');if(!project)throw Error('请等待本地项目打开');await handler([...e.dataTransfer.files]);}));
}
function readData(file) {return new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=()=>reject(Error('文件读取失败'));r.readAsDataURL(file);});}
async function loadTable(file) {
  if(!file)return;if(file.size>20*1024*1024)throw Error('追踪表不能超过 20 MB');
  if(!/\.(xlsx|xls|csv|tsv)$/i.test(file.name))throw Error('请选择 Excel、CSV 或 TSV 文件');
  if(!window.XLSX)throw Error('本地表格读取组件未加载，请刷新页面');
  const bytes=await file.arrayBuffer();let workbook;
  if(/\.(csv|tsv)$/i.test(file.name)) {
    const utf8=new TextDecoder('utf-8',{fatal:false}).decode(bytes),text=utf8.includes('\uFFFD')?new TextDecoder('gb18030').decode(bytes):utf8;
    workbook=XLSX.read(text,{type:'string',raw:true,FS:/\.tsv$/i.test(file.name)?'\t':undefined});
  } else workbook=XLSX.read(bytes,{type:'array',cellText:true,cellDates:false});
  if(!workbook.SheetNames.length)throw Error('文件没有可读取的工作表');
  let matrix,headers,mapping;
  dialog('导入追踪表',`<p><strong>${escape(file.name)}</strong></p><p class="vt-subtle">导入后均为未绑定记录，需人工确认关联。标识符按单元格显示文字读取；源文件中的长序列号和前导零应使用文本格式。</p><div class="vt-mapping"><label>工作表<select id="vt-import-sheet">${workbook.SheetNames.map(n=>`<option>${escape(n)}</option>`).join('')}</select></label><label>表头所在行<input id="vt-import-header" type="number" min="1" max="100" value="1"></label><button id="vt-template">下载导入模板</button></div><div id="vt-import-mapping" class="vt-mapping"></div><p id="vt-import-summary" class="vt-subtle"></p><div id="vt-import-preview" class="vt-import-preview"></div><div class="vt-dialog-actions"><button id="vt-import-confirm" class="vt-primary">确认导入为未绑定记录</button></div>`);
  const update=()=>{
    const sheet=document.getElementById('vt-import-sheet').value,header=Number(document.getElementById('vt-import-header').value);
    if(!Number.isInteger(header)||header<1||header>100)throw Error('表头行号需为 1–100 的整数');
    const table=XLSX.utils.sheet_to_json(workbook.Sheets[sheet],{header:1,raw:false,defval:'',blankrows:true});
    headers=(table[header-1]||[]).map(String);matrix=table.slice(header);mapping=guessMapping(headers);
    if(!headers.length)throw Error('此行没有表头，请调整表头所在行');
    document.getElementById('vt-import-mapping').innerHTML=Object.entries({wonum:'工单号 *',serial:'序列号 *',drawing:'图号',inspector:'检验员',date:'检验日期',slot:'报告位置码',description:'描述'}).map(([key,name])=>`<label>${name}<select data-map="${key}"><option value="-1">请选择列</option>${headers.map((h,i)=>`<option value="${i}" ${mapping[key]===i?'selected':''}>${i+1}. ${escape(h||'空表头')}</option>`).join('')}</select></label>`).join('');
    document.getElementById('vt-import-summary').textContent=`${matrix.length} 行数据，预览前 5 行。相同工单号 + 序列号 + 图号的记录会跳过。`;
    document.getElementById('vt-import-preview').innerHTML=`<table><thead><tr>${headers.map(h=>`<th>${escape(h)}</th>`).join('')}</tr></thead><tbody>${matrix.slice(0,5).map(r=>`<tr>${headers.map((_,i)=>`<td>${escape(r[i]??'')}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
  };
  document.getElementById('vt-import-sheet').addEventListener('change',safe(update));document.getElementById('vt-import-header').addEventListener('change',safe(update));
  document.getElementById('vt-template').onclick=()=>download('\uFEFF工单号,序列号,图号,检验员,检验日期,位置码,描述\r\n示例工单号,示例序列号,SDLDWARMV0239,QA16,2026-11-01,#1LFRF,行走轮心轴 年度 MT\r\n','追踪表模板.csv','text/csv;charset=utf-8');
  update();
  dialogAction('vt-import-confirm',async()=>{
    const m=Object.fromEntries([...document.querySelectorAll('[data-map]')].map(el=>[el.dataset.map,Number(el.value)]));
    const result=importRows(project,matrix,m,file.name+' / '+document.getElementById('vt-import-sheet').value);
    // Keep source row numbers accurate when the header is below row one.
    const offset=Number(document.getElementById('vt-import-header').value)-1;
    if(offset&&result.added)for(const r of project.records.slice(-result.added))r.sourceRow+=offset;
    checked.clear();page=0;await persist();renderTable();renderDetails();renderTree();closeDialog();toast(`新增 ${result.added} 条，跳过 ${result.skipped} 条重复记录和 ${result.empty} 行空数据`);
  });
}
function checkFileCapacity(files) {
  const existing=[...project.references,...project.attachments].reduce((n,f)=>n+f.data.length,0),incoming=files.reduce((n,f)=>n+f.size,0)*4/3;
  if(existing+incoming>198*1024*1024)throw Error('上传资料将超过项目容量，请拆分到新项目');
}
async function uploadReferences(files) {
  if(project.references.length+files.length>100)throw Error('每个项目最多 100 张参考图');
  checkFileCapacity(files);
  const projectId=project.id,refs=[];
  for(const file of files) {
    if(!['image/png','image/jpeg','image/webp'].includes(file.type))throw Error('参考图仅支持 PNG、JPG 和 WebP');
    if(file.size>20*1024*1024)throw Error('单张参考图不能超过 20 MB');
    const data=await readData(file),image=new Image();image.src=data;await image.decode();
    if(image.naturalWidth*image.naturalHeight>60e6)throw Error('参考图像素过大，请缩小后上传');
    refs.push({id:uid(),name:file.name,mime:file.type,data,width:image.naturalWidth,height:image.naturalHeight,hotspots:[]});
  }
  if(project.id!==projectId)throw Error('项目已切换，请重新上传');
  assertCapacity({...project,references:[...project.references,...refs]});
  project.references.push(...refs);referenceId=refs.at(-1)?.id||referenceId;log(project,'上传参考图','',refs.map(r=>r.name).join('、'));await persist();renderReferences();renderDetails();setView('reference');toast(`已保存 ${refs.length} 张参考图`);
}
async function uploadAttachments(files) {
  if(project.attachments.length+files.length>500)throw Error('每个项目最多 500 个附件');
  checkFileCapacity(files);
  const target=selected,projectId=project.id,attachments=[];
  for(const file of files) {
    if(file.size>20*1024*1024)throw Error(`${file.name} 超过 20 MB`);
    let data=await readData(file);const mime=file.type||'application/octet-stream';data=data.replace(/^data:[^;]*;base64,/,`data:${mime};base64,`);
    attachments.push({id:uid(),partId:target,name:file.name,size:file.size,mime,data});
  }
  if(project.id!==projectId)throw Error('项目已切换，请重新上传');
  assertCapacity({...project,attachments:[...project.attachments,...attachments]});
  project.attachments.push(...attachments);log(project,'上传部件附件',target,attachments.map(f=>f.name).join('、'));await persist();renderDetails();stats();setDetail('files');toast(`${attachments.length} 个附件已关联 ${label(target)}`);
}
function asBlob(file) {const binary=atob(file.data.slice(file.data.indexOf(',')+1)),bytes=Uint8Array.from(binary,c=>c.charCodeAt(0));return new Blob([bytes],{type:file.mime});}
function previewAttachment(id) {
  const f=project.attachments.find(f=>f.id===id);if(!f)return;
  const safeImages=['image/png','image/jpeg','image/webp','image/gif'];
  dialog(f.name,`<p class="vt-subtle">${escape(displayWhere(f.partId))} · ${fileSize(f.size)}</p><div id="vt-preview-content"></div><div class="vt-dialog-actions"><button id="vt-preview-download" class="vt-primary">下载原始附件</button></div>`,true);
  const content=document.getElementById('vt-preview-content');
  if(safeImages.includes(f.mime)){const img=document.createElement('img');img.className='vt-preview-image';img.src=f.data;img.alt=f.name;content.append(img);}
  else if(f.mime==='application/pdf') {
    const blob=asBlob(f);
    blob.slice(0,5).text().then(async signature=>{
      if(signature!=='%PDF-'){content.textContent='文件内容不是有效 PDF，请下载原始附件核对。';return;}
      if(!content.isConnected)return;
      const {previewPdf}=await import('./vehicle-tracker-pdf.mjs');await previewPdf(content,blob,$('dialog'));
    }).catch(()=>{
      if(content.isConnected)content.textContent='PDF 预览组件未加载，请下载原始附件查看。';
    });
  }
  else if(f.mime.startsWith('text/')||/\.(txt|csv|md|log)$/i.test(f.name)){const pre=document.createElement('pre');asBlob(f).slice(0,200000).text().then(text=>{pre.textContent=text+(f.size>200000?'\n…预览已截断，请下载完整附件':'');});content.append(pre);}
  else content.innerHTML=`<div class="vt-summary"><strong translate="no">${escape(f.name)}</strong><p>此格式暂不支持内容预览。文件已保存在本页部件附件中，可继续核对名称、类型和大小。</p><small>${escape(f.mime)} · ${fileSize(f.size)}</small></div>`;
  document.getElementById('vt-preview-download').onclick=()=>download(asBlob(f),f.name);
}
function showBackup() {
  dialog('完整项目备份',`<p>备份包含追踪记录、人工绑定、部件资料、参考图原图与热点、所有附件和操作履历。</p><div class="vt-summary"><strong>${escape(project.name)}</strong><p class="vt-subtle">${project.records.length} 条记录 · ${project.references.length} 张参考图 · ${project.attachments.length} 个附件</p></div><p class="vt-subtle">恢复时会创建独立副本。CSV 仅供资料核对，完整恢复请使用这里的 JSON 备份。</p><div class="vt-dialog-actions"><button id="vt-backup-all">备份全部 ${projects.length} 个项目</button><button id="vt-backup-current" class="vt-primary">备份当前项目</button></div>`);
  for(const [id,all] of [['vt-backup-all',true],['vt-backup-current',false]])document.getElementById(id).onclick=safe(()=>{const content=JSON.stringify(backup(all?projects:[project])),name=all?'全部车辆项目':project.name.replace(/[\\/:*?"<>|]/g,'_');if(new Blob([content]).size>MAX_BACKUP_BYTES)throw Error('全部项目备份超过 250 MB，请分别备份每个项目');download(content,`${name}-${new Date().toISOString().slice(0,10)}.json`);closeDialog();toast('完整备份已生成，请保存下载的 JSON 文件');});
}
async function restoreBackup(file) {
  if(!file)return;if(file.size>MAX_BACKUP_BYTES)throw Error('备份超过 250 MB，请按项目拆分备份');
  let value;try{value=JSON.parse(await file.text());}catch{throw Error('备份文件不是有效 JSON');}
  const copies=restoredCopies(value);
  if(projects.length+copies.length>100)throw Error('本浏览器最多保存 100 个项目，请在其他浏览器恢复或减少本次备份的项目数量');
  dialog('恢复完整备份',`<p>已验证备份结构、部件关联与热点坐标，将恢复为 ${copies.length} 个独立副本。</p><div class="vt-summary">${copies.map(p=>`<p><strong>${escape(p.name)}</strong><br><span class="vt-subtle">${p.records.length} 条记录 · ${p.references.length} 张图 · ${p.attachments.length} 个附件</span></p>`).join('')}</div><p class="vt-subtle">当前浏览器中的已有项目会保留。</p><div class="vt-dialog-actions"><button id="vt-restore-confirm" class="vt-primary">确认恢复副本</button></div>`);
  dialogAction('vt-restore-confirm',async()=>{await saveQueue;await store.putAll(copies);projects.push(...copies);await switchProject(copies[0].id);closeDialog();toast(`已恢复 ${copies.length} 个项目，记录、热点和附件关联均已保留`);});
}
function help() {
  dialog('电脑端操作说明',`<dl class="vt-help-grid"><dt>旋转 / 缩放 / 平移</dt><dd>左键拖动 / 滚轮 / 右键拖动</dd><dt>选择部件</dt><dd>左键单击模型或结构树。拖动结束不会触发选择。</dd><dt>聚焦部件</dt><dd>双击模型部件，或点击「聚焦」。快捷键 F 同样可用。</dd><dt>查看车底</dt><dd>点击「透视车底」，可直接选择车轮和车轴；「隔离所选」隐藏其他部件。</dd><dt>调整工作区</dt><dd>拖动两侧分隔线；折叠按钮收起面板。分隔线也可用左右方向键调整。</dd><dt>追踪表</dt><dd>支持 Excel / CSV / TSV 和列映射。记录导入后需勾选并人工确认绑定。</dd><dt>参考图热点</dt><dd>选中部件 → 切换参考图 → 添加热点 → 点击图片定位。调整窗口后热点位置保持一致。</dd><dt>附件</dt><dd>选择部件后进入「附件」，拖入或选择文件。图片、PDF 和文本支持预览。</dd><dt>工单跳转</dt><dd>点击任意工单号：在本页查看工单、QAQC 检验 / MRB 记录或工单 PDF；每次查看连同序列号与位置记入履历。</dd><dt>图号面板</dt><dd>点击任意图号，右侧打开图号面板：全部安装位置与当前序列号、追踪记录、QAQC 检验与 MRB 案件；「标出全部位置」在三维中高亮同图号零件。Esc 返回部件详情。</dd><dt>登记工单</dt><dd>选中销轴后进入「追踪」，登记年度检验或更换工单，序列号默认沿用当前件。</dd></dl><p class="vt-subtle">资料仅保存在当前浏览器和网站地址下，不同步到服务器或其他浏览器。清理网站数据会移除本地项目；迁移或清理前请先导出完整备份。全部三维与表格依赖均由本机提供。</p><p class="vt-subtle">部件结构与名称依据 ${SPEC_DOC}：5 节车厢、每车 4 个轮架，每个轮架含行走 / 导向 / 防脱轮各前后两组及其心轴和枢轴销，共 ${PARTS.length} 个部件。三维几何为示意，不代表实际尺寸。「${escape(REPORT.title)}」项目的绿标信息逐张转录自报告扫描件。</p>`);
}
const coachOf=id=>id.split('-')[0];
const localPosition=p=>displayPosition(p).split(' · ').slice(1).join(' · ')||displayLabel(p.id);
function openDrawing(code) {
  code=drawingCode(code);if(!linkable(code))return;
  if(drawing!==code){drawing=code;drawingTab='sources';drawingFilter='';drawingMarked=false;model?.mark([]);}
  closeLinkMenu();panel('right',false);renderDrawing();loadQaqc(code);sources?.load(code);
  $('drawing').querySelector('[data-drawing-close]')?.focus({preventScroll:true});
}
function closeDrawing() {drawing='';drawingMarked=false;model?.mark([]);renderDrawing();}
async function loadQaqc(code) {
  const cached=qaqcCache.get(code);if(cached&&!cached.error)return;
  qaqcCache.set(code,{loading:true});if(drawing===code)renderDrawing();
  let entry;
  try {
    const res=await fetch('/tools/vehicle-tracker/api/qaqc/parts/detail',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':document.querySelector('meta[name="csrf-token"]').content},body:JSON.stringify({part_no:code})});
    const body=await res.json().catch(()=>({}));
    if(res.ok)entry={data:body};
    else {const d=body.detail,message=typeof d==='string'?d:d?.message;entry=res.status===400?{empty:message||'QAQC 本地记录中没有此图号'}:{error:message||`QAQC 服务返回 ${res.status}`};}
  } catch {entry={error:'QAQC 本地记录读取失败，请稍后重试'};}
  qaqcCache.set(code,entry);if(drawing===code)renderDrawing();
}
function drawingParts(s) {
  const q=drawingFilter.normalize('NFKC').toLowerCase(),hit=({part,record})=>!q||[part.position,label(part.id),displayPosition(part),displayLabel(part.id),part.code,record?.serial,record?.wonum].join(' ').normalize('NFKC').toLowerCase().includes(q);
  const rows=s.parts.filter(hit);if(!rows.length)return '<p class="vt-empty">没有匹配的位置</p>';
  const groups=new Map();for(const row of rows){const c=coachOf(row.part.id);groups.has(c)||groups.set(c,[]);groups.get(c).push(row);}
  return [...groups].map(([c,list])=>`<div class="vt-dgroup"><h4><button type="button" data-drawing-part="${c}">${escape(displayLabel(c))}</button><span>${list.filter(x=>x.record).length}/${list.length}</span></h4>${list.map(({part,record})=>`<div class="vt-drow${within(selected,part.id)?' selected':''}"><button type="button" class="vt-drow-pos" data-drawing-part="${part.id}" title="${escape(displayWhere(part.id))}"><span>${escape(localPosition(part))}</span><small>${escape(part.code||part.id)}</small></button><strong class="vt-serial${record?'':' none'}">${escape(record?.serial||'未登记')}</strong><span class="vt-drow-wo">${record?woButton(record):''}</span></div>`).join('')}</div>`).join('');
}
function drawingRecords(s) {
  if(!s.records.length)return '<p class="vt-empty">追踪表中没有此图号的记录</p>';
  return s.records.slice().sort((a,b)=>(b.date||'').localeCompare(a.date||'')).map(r=>`<div class="vt-drec"><header>${woButton(r)}<strong class="vt-serial">${escape(r.serial||'—')}</strong><span>${escape([r.inspector,r.date].filter(Boolean).join(' · ')||'—')}</span></header>${r.partId?`<button type="button" data-drawing-part="${r.partId}">${escape(displayWhere(r.partId))}</button>`:'<span class="vt-unbound">○ 待人工绑定</span>'}<small translate="no">${escape(sourceText(r))}${r.slot?` · ${escape(r.slot)}`:''}</small></div>`).join('');
}
function drawingQaqc(s) {
  const q=qaqcCache.get(drawing);
  if(!q||q.loading)return '<p class="vt-empty">正在读取 QAQC 本地记录…</p>';
  if(q.error)return `<div class="vt-empty"><p>${escape(q.error)}</p><button type="button" data-drawing-retry>重试</button></div>`;
  if(q.empty)return `<div class="vt-empty"><p>${escape(q.empty)}</p><p class="vt-subtle">QAQC 模块同步后的工单中没有引用此图号。</p></div>`;
  const d=q.data,t=d.totals||{},serials=new Set(s.records.map(r=>r.serial).filter(sn=>sn&&sn.length>=4)),trackedWo=new Set(s.records.map(r=>r.wonum));
  const rate=t.mrb_rate==null?'—':`${Math.round(t.mrb_rate*100)}%`;
  const rows=(d.workorders||[]).slice(0,40).map(w=>{
    const text=[w.description,w.description_en].filter(Boolean).join(' '),mentioned=[...serials].filter(sn=>text.includes(sn));
    return `<article class="vt-qwo${w.is_mrb?' mrb':''}"><header>${linkable(w.wonum)?`<button type="button" class="vt-wo" data-wo="${escape(w.wonum)}" title="打开 Maximo / MRB 工单">${escape(w.wonum)}</button>`:escape(w.wonum)}<time>${escape(String(w.report_local||'').slice(0,10))}</time>${w.is_mrb?`<span class="vt-tag mrb">MRB · ${escape(w.disposition_label||'无意见')}</span>`:`<span class="vt-tag">${escape(w.status||'')}</span>`}${trackedWo.has(w.wonum)?'<span class="vt-tag own">本车记录</span>':''}</header><p${text?' translate="no"':''}>${escape(text||'（无描述）')}</p>${mentioned.length?`<small>提及序列号 ${mentioned.map(escape).join('、')}</small>`:''}</article>`;
  }).join('');
  return `<div class="vt-qstats"><div><strong>${t.inspections??0}</strong><span>检验次数</span></div><div><strong>${t.mrb_cases??0}</strong><span>MRB 案件</span></div><div><strong>${rate}</strong><span>MRB 率</span></div></div>${(d.dispositions||[]).length?`<p class="vt-qdisp">${d.dispositions.map(x=>`<span class="vt-tag">${escape(x.label)} ${x.count}</span>`).join('')}</p>`:''}${rows||'<p class="vt-empty">没有工单</p>'}${(d.workorders||[]).length>40?'<p class="vt-subtle">仅显示最近 40 张工单，以上为最近40张工单，可在本页逐张查看。</p>':''}<p class="vt-subtle">数据来自 QAQC 模块本地同步的 Maximo 记录 · ${escape(String(d.queried_at||'').slice(0,16).replace('T',' '))}</p>`;
}
function renderDrawing() {
  const el=$('drawing');el.hidden=!drawing;if(!drawing){el.innerHTML='';return;}
  const s=drawingSummary(project,drawing),kinds=s.kinds.map(k=>KINDS[k]),k=kinds[0],q=qaqcCache.get(drawing);
  const unique=f=>[...new Set(kinds.map(f).filter(Boolean))];
  const mrb=q?.data?q.data.totals?.mrb_cases??0:q?.empty?0:'…';
  const spec=[['英文名称',unique(x=>x.en).map(escape).join('<br>')],['WDI 图号',unique(x=>x.dwg).map(escape).join(' / ')],['WDC No.',unique(x=>x.wdc).map(escape).join(' / ')],['SHDR 编码',unique(x=>x.shdr).map(escape).join(' / ')]].filter(([,v])=>v);
  if(k?.req)spec.push(['检验要求',`${escape(k.req.method)}<br><small>${escape(k.req.frequency)} · Job Plan <button type="button" class="vt-dwg" data-jp="${escape(k.req.jobPlan)}">${escape(k.req.jobPlan)}</button></small>`]);
  const tabs=[['sources','文档出处',sources?.count(drawing)??'…'],['parts','安装位置',s.parts.length],['records','追踪记录',s.records.length],['qaqc','QAQC / MRB',mrb]];
  const scroll=el.querySelector('.vt-drawing-body')?.scrollTop||0;
  el.innerHTML=`<div class="vt-drawing-head"><div><span class="vt-eyebrow">DRAWING · 图号</span><h2>${escape(drawing)}</h2><p>${escape(unique(x=>t(x.zh)).join(' / ')||'结构清单中未定义此图号')}</p></div><button type="button" class="vt-icon" data-drawing-close aria-label="关闭图号面板，返回部件详情" title="返回部件详情（Esc）">×</button></div>
  <div class="vt-dstats"><div><strong>${s.parts.length}</strong><span>安装位置</span></div><div><strong>${s.serials}<small>/${s.parts.length}</small></strong><span>已有序列号</span></div><div><strong>${s.records.length}</strong><span>追踪记录</span></div><div class="${mrb&&mrb!=='…'?'warn':''}"><strong>${mrb}</strong><span>MRB 案件</span></div></div>
  <div class="vt-dactions"><button type="button" data-drawing-mark aria-pressed="${drawingMarked}" ${s.parts.length?'':'disabled'}>◎ 标出全部位置</button><button type="button" data-drawing-table>筛选追踪表</button><button type="button" data-drawing-qaqc>QAQC 记录</button><button type="button" data-copy-text="${escape(drawing)}" aria-label="复制图号">复制</button></div>
  ${spec.length?`<details class="vt-dspec"><summary>图纸资料</summary><dl class="vt-spec">${spec.map(([t,d])=>`<dt>${t}</dt><dd>${d}</dd>`).join('')}</dl></details>`:''}
  <div class="vt-detail-tabs" role="tablist" aria-label="图号资料">${tabs.map(([id,name,n])=>`<button type="button" role="tab" data-drawing-tab="${id}" aria-selected="${drawingTab===id}" class="${drawingTab===id?'active':''}">${name} <span>${n}</span></button>`).join('')}</div>
  ${drawingTab==='parts'&&s.parts.length>6?`<div class="vt-dsearch"><input type="search" data-drawing-search value="${escape(drawingFilter)}" placeholder="筛选位置、位置码、序列号…" aria-label="筛选安装位置"></div>`:''}
  <div class="vt-drawing-body">${drawingTab==='sources'?(sources?.html(drawing)||'<p class="vt-empty">正在准备文档出处…</p>'):drawingTab==='parts'?(s.parts.length?drawingParts(s):'<p class="vt-empty">结构树中没有使用此图号的部件</p>'):drawingTab==='records'?drawingRecords(s):drawingQaqc(s)}</div>`;
  el.querySelector('.vt-drawing-body').scrollTop=scroll;
}
function linkMenu(anchor,items,context) {
  const menu=$('linkmenu');
  menu.innerHTML=`<p>${escape(context.title)}</p>${items.filter(i=>i.kind||i.copy).map(i=>i.kind?`<button type="button" data-inline-kind="${i.kind}" data-inline-code="${escape(i.code)}" data-jump="${escape(i.name)}">${escape(i.name)}</button>`:`<button data-copy="${escape(i.copy)}">${escape(i.name)}</button>`).join('')}`;
  menu.dataset.context=JSON.stringify(context);menu.hidden=false;
  const r=anchor.getBoundingClientRect(),w=menu.offsetWidth,h=menu.offsetHeight;
  menu.style.left=Math.max(8,Math.min(r.left,innerWidth-w-8))+'px';menu.style.top=(r.bottom+h+8>innerHeight?r.top-h-4:r.bottom+4)+'px';
  menu.querySelector('a,button')?.focus();
}
function closeLinkMenu() {$('linkmenu').hidden=true;}
function openWoMenu(button) {
  const r=project.records.find(x=>x.id===button.dataset.record),wonum=button.dataset.wo;
  const title=`工单 ${wonum}${r?` · 序列号 ${r.serial}`:''}`;
  linkMenu(button,[{name:'工单资料',kind:'maximo-workorder',code:wonum},{name:'QAQC 检验 / MRB 工单',kind:'qaqc-workorder',code:wonum},{name:'工单 PDF（中文）',kind:'workorder-pdf',code:wonum},{name:'复制工单号',copy:wonum},...(r?.serial?[{name:'复制序列号',copy:r.serial}]:[])],{title,wonum,recordId:r?.id||'',partId:r?.partId||''});
}
function openJobPlanMenu(button) {
  const jp=button.dataset.jp;
  linkMenu(button,[{name:'作业计划详情',kind:'job-plan',code:jp},{name:'复制 Job Plan',copy:jp}],{title:`Job Plan ${jp}`,jobPlan:jp,partId:selected});
}
function recordJump(name,context) {
  const r=project.records.find(x=>x.id===context.recordId),partId=r?.partId||context.partId||'';
  const target=context.wonum?`工单 ${context.wonum}`:context.partNo?`图号 ${context.partNo}`:`Job Plan ${context.jobPlan}`;
  log(project,`页内查看：${name}`,PART_MAP.has(partId)?partId:'',`${target}${r?` · 序列号 ${r.serial} · 图号 ${r.drawing}`:''}${partId&&PART_MAP.has(partId)?` · ${where(partId)}`:''}`);
  persist().then(()=>{if(detail==='history')renderDetails();}).catch(()=>{});
}
async function copyText(text) {
  try{await navigator.clipboard.writeText(text);}catch{const t=document.createElement('textarea');t.value=text;document.body.append(t);t.select();document.execCommand('copy');t.remove();}
  toast(`已复制：${text}`);
}
function panel(side,collapse) {$('app').classList.toggle(side+'-collapsed',collapse);$('show-'+side).hidden=!collapse;}
function resizePanel(side) {
  const handle=$('resize-'+side),property='--vt-'+side;
  const apply=value=>{const available=$('workspace').clientWidth,other=side==='left'?$('right').clientWidth:$('left').clientWidth,max=Math.min(side==='left'?430:520,available-other-270);$('app').style.setProperty(property,Math.max(side==='left'?180:230,Math.min(value,max))+'px');};
  handle.addEventListener('pointerdown',e=>{if(e.button!==0)return;e.preventDefault();const start=e.clientX,width=$(side).clientWidth;handle.setPointerCapture(e.pointerId);const move=e=>apply(width+(e.clientX-start)*(side==='left'?1:-1)),end=()=>{handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',end);handle.removeEventListener('pointercancel',end);};handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',end);handle.addEventListener('pointercancel',end);});
  handle.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();apply($(side).clientWidth+(e.key==='ArrowRight'?20:-20)*(side==='left'?1:-1));}});
}
function setup() {
  on('dialog-close','click',closeDialog);$('dialog').addEventListener('cancel',()=>{$('dialog-body').innerHTML='';});
  on('projects','click',manageProjects);on('project','change',async e=>{try{await switchProject(e.target.value);}catch(error){projectOptions();throw error;}});
  on('focus-mode','click',e=>{const active=document.documentElement.classList.toggle('vehicle-focus');e.currentTarget.setAttribute('aria-pressed',String(active));e.currentTarget.textContent=active?'退出专注':'专注模式';model?.resize();});
  document.querySelectorAll('[data-rag-chat-open]').forEach(button=>button.addEventListener('click',()=>document.getElementById('vt-ai-open').click()));
  on('help','click',help);on('backup','click',showBackup);on('restore','click',()=>$('input-restore').click());
  on('search','input',e=>{query=e.target.value.trim();page=0;checked.clear();renderTree();renderTable();});
  on('expand-tree','click',()=>{PARTS.forEach(p=>expanded.add(p.id));renderTree();});on('fold-tree','click',()=>{expanded=new Set(['TRAIN']);renderTree();});
  on('tree','click',e=>{const toggle=e.target.closest('[data-expand]'),select=e.target.closest('[data-select]');if(toggle){const id=toggle.dataset.expand;expanded.has(id)?expanded.delete(id):expanded.add(id);renderTree();}else if(select)selectPart(select.dataset.select,{fly:true});});
  on('tree','keydown',e=>{const button=e.target.closest('[data-select]');if(!button)return;const buttons=[...$('tree').querySelectorAll('[data-select]')],index=buttons.indexOf(button),id=button.dataset.select;if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();buttons[Math.max(0,Math.min(buttons.length-1,index+(e.key==='ArrowDown'?1:-1)))]?.focus();}else if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();e.key==='ArrowRight'?expanded.add(id):expanded.delete(id);renderTree();$('tree').querySelector(`[data-select="${id}"]`)?.focus();}});
  for(const side of ['left','right']){on('collapse-'+side,'click',()=>panel(side,true));on('show-'+side,'click',()=>panel(side,false));resizePanel(side);}
  document.querySelectorAll('[data-detail]').forEach(b=>b.addEventListener('click',()=>setDetail(b.dataset.detail)));
  document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
  on('info','submit',async e=>{e.preventDefault();const name=$('part-name').value.trim();if(!name)throw Error('请输入部件名称');project.parts[selected]={name,notes:$('part-notes').value};log(project,'修改部件资料',selected,name);await persist();renderTree();renderDetails();renderTable();renderHotspots();toast('部件资料已保存');});
  on('detail-body','submit',async e=>{
    if(e.target.id!=='vt-register')return;e.preventDefault();
    const data=Object.fromEntries(new FormData(e.target)),record=addRecord(project,selected,data);
    await persist();renderTree();renderDetails();renderTable();toast(`已登记工单 ${record.wonum} · 序列号 ${record.serial}`);
  });
  document.addEventListener('click',e=>{
    const wo=e.target.closest('[data-wo]'),partNo=e.target.closest('[data-part-no]'),jp=e.target.closest('[data-jp]'),menu=e.target.closest('#vt-linkmenu');
    if(wo){e.preventDefault();openWoMenu(wo);return;}
    if(partNo){e.preventDefault();openDrawing(partNo.dataset.partNo);return;}
    if(jp){e.preventDefault();openJobPlanMenu(jp);return;}
    const inline=e.target.closest('[data-inline-kind]');
    if(inline){e.preventDefault();const context=menu?JSON.parse(menu.dataset.context||'{}'):(inline.dataset.inlineKind==='job-plan'?{jobPlan:inline.dataset.inlineCode,partId:selected}:{wonum:inline.dataset.inlineCode,recordId:inline.dataset.record||'',partId:selected});recordJump(inline.dataset.jump||'资料',context);recordsView?.open(inline.dataset.inlineKind,inline.dataset.inlineCode);closeLinkMenu();return;}
    if(menu){
      const link=e.target.closest('[data-jump]'),copy=e.target.closest('[data-copy]'),context=JSON.parse(menu.dataset.context||'{}');
      if(link){recordJump(link.dataset.jump,context);closeLinkMenu();}
      if(copy){safe(copyText)(copy.dataset.copy);closeLinkMenu();}
      return;
    }
    const direct=e.target.closest('.vt-card-actions [data-jump]');
    if(direct)recordJump(direct.dataset.jump,{wonum:direct.dataset.jumpWo,recordId:direct.dataset.record});
    closeLinkMenu();
  });
  document.addEventListener('keydown',e=>{if(e.key!=='Escape'||document.querySelector('.vt-scope dialog[open]')||e.target.closest?.('#vt-ai-panel'))return;if(!$('linkmenu').hidden){closeLinkMenu();e.stopPropagation();}else if(drawing&&!annotating){closeDrawing();e.stopPropagation();}},true);
  on('drawing','click',e=>{
    const t=e.target,tab=t.closest('[data-drawing-tab]'),part=t.closest('[data-drawing-part]'),jump=t.closest('a[data-jump]'),copy=t.closest('[data-copy-text]');
    if(t.closest('[data-drawing-close]'))closeDrawing();
    else if(tab){drawingTab=tab.dataset.drawingTab;renderDrawing();}
    else if(t.closest('[data-drawing-qaqc]')){drawingTab='qaqc';renderDrawing();}
    else if(part)selectPart(part.dataset.drawingPart,{reveal:true,fly:true});
    else if(t.closest('[data-drawing-mark]')){drawingMarked=!drawingMarked;const ids=drawingSummary(project,drawing).parts.map(x=>x.part.id);model?.mark(drawingMarked?ids:[]);if(drawingMarked){setView('model');model?.focus(ids);}renderDrawing();}
    else if(t.closest('[data-drawing-table]')){query=drawing;$('search').value=drawing;$('record-filter').value='all';page=0;checked.clear();renderTree();renderTable();toast(`追踪表与结构树已按图号 ${drawing} 筛选`);}
    else if(t.closest('[data-drawing-retry]')){qaqcCache.delete(drawing);loadQaqc(drawing);}
    else if(copy)copyText(copy.dataset.copyText);
    else if(jump)recordJump(jump.dataset.jump,{partNo:drawing,partId:selected});
  });
  on('drawing','input',e=>{if(!e.target.matches('[data-drawing-search]'))return;drawingFilter=e.target.value.trim();$('drawing').querySelector('.vt-drawing-body').innerHTML=drawingParts(drawingSummary(project,drawing));});
  window.addEventListener('resize',closeLinkMenu);
  on('detail-body','click',async e=>{
    const preview=e.target.closest('[data-preview]'),downloadButton=e.target.closest('[data-download]'),locate=e.target.closest('[data-locate-record]'),remove=e.target.closest('[data-delete-record]');
    if(remove){
      const r=project.records.find(x=>x.id===remove.dataset.deleteRecord);if(!r||r.source!=='手工登记')return;
      const projectId=project.id;
      dialog('删除手工登记',`<p>删除工单 <strong>${escape(r.wonum)}</strong> / 序列号 <strong>${escape(r.serial)}</strong> 的手工登记？</p><p class="vt-subtle">确认前可先导出完整备份。</p><div class="vt-dialog-actions"><button id="vt-cancel-delete">取消</button><button id="vt-confirm-delete" class="vt-primary">确认删除</button></div>`);
      document.getElementById('vt-cancel-delete').onclick=closeDialog;
      dialogAction('vt-confirm-delete',async()=>{if(project.id!==projectId)throw Error('项目已变化，请重新选择');
        project.records=project.records.filter(x=>x.id!==r.id);checked.delete(r.id);log(project,'删除登记工单',r.partId,`${r.wonum} / 序列号 ${r.serial}`);
        await persist();renderTree();renderDetails();renderTable();closeDialog();toast('登记记录已删除');});return;
    }
    if(preview)previewAttachment(preview.dataset.preview);
    if(downloadButton){const f=project.attachments.find(f=>f.id===downloadButton.dataset.download);download(asBlob(f),f.name);}
    if(locate){$('record-filter').value='selected';query='';$('search').value='';page=Math.floor(filteredRecords().findIndex(r=>r.id===locate.dataset.locateRecord)/PAGE_SIZE);checked=new Set([locate.dataset.locateRecord]);renderTree();renderTable();$('records').querySelector(`[data-check="${locate.dataset.locateRecord}"]`)?.scrollIntoView({block:'nearest'});}
  });
  on('camera','change',e=>model?.orientation(e.target.value));on('focus','click',()=>model?.focus(selected));on('fit','click',()=>selectPart('TRAIN',{fly:true}));
  on('xray','click',e=>{const on=e.target.getAttribute('aria-pressed')!=='true';e.target.setAttribute('aria-pressed',on);model?.setXray(on);});
  on('isolate','click',e=>{const on=e.target.getAttribute('aria-pressed')!=='true';e.target.setAttribute('aria-pressed',on);model?.isolate(on);});
  on('zoom-in','click',()=>model?.zoom(.8));on('zoom-out','click',()=>model?.zoom(1.25));
  document.addEventListener('keydown',e=>{if(e.target.closest?.('input,textarea,select')||document.querySelector('.vt-scope dialog[open]'))return;if(e.key.toLowerCase()==='f'){e.preventDefault();model?.focus(selected);}if(e.key==='Escape'&&annotating){annotating=false;$('annotate').setAttribute('aria-pressed','false');$('reference-image-wrap').classList.remove('annotating');return;}
    if(e.key==='Escape'&&view==='model'&&selected!=='TRAIN'){e.preventDefault();selectPart(PART_MAP.get(selected).parent,{reveal:true,fly:true});}});
  on('table-expand','click',()=>{const expanded=$('app').classList.toggle('table-expanded');$('table-expand').textContent=expanded?'⛶ 收起核对':'⛶ 展开核对';$('table-expand').setAttribute('aria-expanded',expanded);});
  on('record-filter','change',()=>{page=0;checked.clear();renderTable();});on('prev','click',()=>{page--;renderTable();});on('next','click',()=>{page++;renderTable();});
  on('records','change',e=>{if(e.target.dataset.check){e.target.checked?checked.add(e.target.dataset.check):checked.delete(e.target.dataset.check);renderTable();}});
  on('records','click',e=>{const b=e.target.closest('[data-select]');if(b)selectPart(b.dataset.select,{reveal:true,fly:true});});
  on('nav','click',e=>{if(e.target.closest('[data-nav-toggle]')){navCollapsed=!navCollapsed;try{localStorage.setItem('shdr-vehicle-nav',navCollapsed?'collapsed':'open');}catch{}renderNav();return;}const b=e.target.closest('[data-nav]');if(b)selectPart(b.dataset.nav,{reveal:true,fly:true});});
  on('selection-label','click',e=>{const b=e.target.closest('[data-crumb]');if(b)selectPart(b.dataset.crumb,{reveal:true,fly:true});});
  on('check-page','change',e=>{for(const r of filteredRecords().slice(page*PAGE_SIZE,(page+1)*PAGE_SIZE))e.target.checked?checked.add(r.id):checked.delete(r.id);renderTable();});
  on('bind','click',()=>confirmBinding(false));on('unbind','click',()=>confirmBinding(true));
  on('csv','click',()=>download(csv(filteredRecords()),project.name.replace(/[\\/:*?"<>|]/g,'_')+'-追踪表.csv','text/csv;charset=utf-8'));
  for(const [button,input,handler,multiple] of [['import','input-table',loadTable,false],['reference-upload','input-reference',uploadReferences,true],['attachment-upload','input-attachment',uploadAttachments,true],['restore','input-restore',restoreBackup,false]]) {
    if(button!=='restore')on(button,'click',()=>$(input).click());
    on(input,'change',async e=>{const files=[...e.target.files];e.target.value='';if(files.length)await handler(multiple?files:files[0]);});
  }
  bindDrop('table-drop',async files=>{if(files.length!==1)throw Error('请一次导入一张追踪表');await loadTable(files[0]);});
  bindDrop('reference-drop',uploadReferences);bindDrop('attachment-drop',uploadAttachments);
  // Prevent a dropped file from navigating away when it misses an upload area.
  document.addEventListener('dragover',e=>{if(e.dataTransfer.types.includes('Files'))e.preventDefault();});document.addEventListener('drop',e=>{if(e.dataTransfer.types.includes('Files'))e.preventDefault();});
  on('reference-select','change',e=>{referenceId=e.target.value;hotspotId='';renderReferences();});
  on('annotate','click',()=>{annotating=!annotating;$('annotate').setAttribute('aria-pressed',annotating);$('reference-image-wrap').classList.toggle('annotating',annotating);$('refhint').textContent=annotating?`点击图片，为「${displayWhere(selected)}」添加热点；Esc 取消。`:'热点坐标相对于图片保存，调整窗口和侧栏后仍保持定位。';});
  on('reference-image-wrap','click',async e=>{
    const button=e.target.closest('[data-hotspot]');
    if(button){hotspotId=button.dataset.hotspot;const h=reference().hotspots.find(h=>h.id===hotspotId);selectPart(h.partId,{reveal:true,fly:view==='model'});return;}
    if(!annotating)return;
    const ref=reference(),rect=$('reference-image').getBoundingClientRect();if(!ref||!rect.width||!rect.height)return;
    if(ref.hotspots.length>=2000)throw Error('每张参考图最多 2,000 个热点');
    const h={id:uid(),partId:selected,label:label(selected),x:Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)),y:Math.max(0,Math.min(1,(e.clientY-rect.top)/rect.height))};
    ref.hotspots.push(h);hotspotId=h.id;annotating=false;$('annotate').setAttribute('aria-pressed','false');$('reference-image-wrap').classList.remove('annotating');log(project,'添加参考图热点',selected,ref.name);await persist();renderHotspots();renderDetails();$('refhint').textContent='热点已关联到所选部件。点击热点可同步选择结构树。';toast('热点已保存');
  });
  on('delete-hotspot','click',async()=>{const ref=reference(),h=ref?.hotspots.find(h=>h.id===hotspotId);if(!h)return;log(project,'删除参考图热点',h.partId,ref.name);ref.hotspots=ref.hotspots.filter(h=>h.id!==hotspotId);hotspotId='';await persist();renderHotspots();renderDetails();});
  new ResizeObserver(fitReference).observe($('reference-drop'));
  window.addEventListener('beforeunload',e=>{if(['正在保存…',window.FOSI18n?.t('正在保存…')].includes($('save-status').textContent)){e.preventDefault();e.returnValue='';}});
}
async function boot() {
  try {
    store=await new ProjectStore().open();projects=await store.all();
    if(!projects.length){const p=reportProject();await store.put(p);projects=[p];}
    let active;try{active=localStorage.getItem('shdr-vehicle-active');}catch{}
    project=projects.find(p=>p.id===active)||projects[0];setup();await switchProject(project.id);$('app').setAttribute('aria-busy','false');
    try {
      const [{createDocumentExplorer},{createInlineRecords}]=await Promise.all([import('./vehicle-tracker-documents.mjs'),import('./vehicle-tracker-inline.mjs')]);
      sources=createDocumentExplorer({onUpdate:code=>{if(drawing===code)renderDrawing();}});
      recordsView=createInlineRecords({getProject:()=>project,locate:id=>{setView('model');selectPart(id,{reveal:true,fly:true});},openDrawing});
    } catch(error){console.warn('Vehicle document panels unavailable',error);}
    try {
      const {mountAssistant}=await import('./vehicle-tracker-ai.mjs');
      assistant=mountAssistant({getProject:()=>project,getSelection:()=>selected,locate:id=>{setView('model');selectPart(id,{reveal:true,fly:true});}});
    } catch(error) {
      $('ai-open').disabled=true;$('ai-open').title='车辆AI暂不可用，请刷新后重试';console.warn('Vehicle AI unavailable',error);
    }
    try {
      const {VehicleModel}=await import('./vehicle-tracker-model.mjs');
      model=new VehicleModel($('canvas'),{select:id=>selectPart(id,{reveal:true}),focusRequest:()=>selectPart('TRAIN',{fly:true}),xrayChanged:on=>$('xray').setAttribute('aria-pressed',on),hover:(part,e)=>{
        const el=$('hover');el.hidden=!part;if(!part)return;const rect=$('model').getBoundingClientRect(),current=latestRecord(project,part.id);
        el.innerHTML=`${escape(displayLabel(part.id))}${current?`<b class="vt-serial">${escape(current.serial)}</b>`:''}<small>${escape(displayPosition(part))}${part.code?` · ${escape(part.code)}`:''}</small><small class="vt-hover-hint">单击选择 · 双击聚焦</small>`;el.style.left=Math.max(5,Math.min(e.clientX-rect.left+15,rect.width-260))+'px';el.style.top=Math.max(5,Math.min(e.clientY-rect.top+15,rect.height-65))+'px';
      }});model.select(selected);
    } catch(error) {
      console.error(error);$('model-error').hidden=false;$('model-error').textContent='三维画布无法启动。请确认浏览器已启用 WebGL / 硬件加速后刷新。结构树、参考图、追踪表和备份仍可使用。';
    }
    const initial=new URLSearchParams(location.search);if(initial.has('inspect')&&linkable(initial.get('code'))){const kind=initial.get('inspect'),code=initial.get('code');if(kind==='qaqc-part')openDrawing(code);else recordsView?.open(kind,code);}
  } catch(error) {
    console.error(error);$('save-status').textContent='本地项目存储无法打开';$('save-status').classList.add('error');$('app').setAttribute('aria-busy','false');
    dialog('无法打开本地项目',error.blocked?`<p>${escape(error.message)}</p>`:`<p>浏览器未允许访问本地项目存储。请使用普通窗口（非 InPrivate / 无痕）并允许此网站保存数据，然后刷新页面。</p><p class="vt-subtle">${escape(error?.message||String(error))}</p>`);$('dialog-close').onclick=closeDialog;
  }
}
document.addEventListener('fos:languagechange',()=>{
  if(!project)return;
  const drafts=[];
  for(const root of ['info','detail-records','drawing']){
    $(root).querySelectorAll('input,textarea,select').forEach((input,index)=>{
      drafts.push({root,index,value:input.value,checked:input.checked,focused:input===document.activeElement,
        start:input.selectionStart,end:input.selectionEnd});
    });
  }
  renderTree();renderDetails();renderTable();renderHotspots();
  $('selection-label').innerHTML=crumbs(selected);
  $('binding-target').textContent=t(`绑定目标：${displayWhere(selected)}`);
  $('hover').hidden=true;
  for(const draft of drafts){
    const input=$(draft.root).querySelectorAll('input,textarea,select')[draft.index];
    if(!input||input.id==='vt-part-position')continue;
    input.value=draft.value;if(typeof draft.checked==='boolean')input.checked=draft.checked;
    if(draft.focused){input.focus({preventScroll:true});if(draft.start!=null)input.setSelectionRange(draft.start,draft.end);}
  }
  if(annotating)$('refhint').textContent=t(`点击图片，为「${displayWhere(selected)}」添加热点；Esc 取消。`);
});
boot();
