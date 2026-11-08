/* 关联群是无向连通分量；用户关系说明只作选库线索。 */
export function connectedComponents(nodes, links) {
  const neighbors=new Map(nodes.map(node=>[node.slug,new Set()]));
  for(const link of links) {
    if(link.left===link.right || !neighbors.has(link.left) || !neighbors.has(link.right))continue;
    neighbors.get(link.left).add(link.right);neighbors.get(link.right).add(link.left);
  }
  const seen=new Set(),components=[];
  for(const node of nodes) {
    if(seen.has(node.slug))continue;
    const group=[],queue=[node.slug];seen.add(node.slug);
    for(let index=0;index<queue.length;index++) {
      const slug=queue[index];group.push(slug);
      for(const neighbor of neighbors.get(slug))if(!seen.has(neighbor)){seen.add(neighbor);queue.push(neighbor);}
    }
    components.push(group);
  }
  return components;
}

function element(tag,className,text) {
  const node=document.createElement(tag);if(className)node.className=className;
  if(text!==undefined)node.textContent=String(text);return node;
}
async function init(root) {
  const {BubbleCanvas}=await import('./kb_connection_canvas.js?v=20261108-individual-4');
  const get=id=>document.getElementById(id),panel=get('klClusterView'),dialog=get('klDialog'),form=get('klForm');
  const left=get('klLeft'),right=get('klRight'),summary=get('klSummary'),error=get('klError'),save=get('klSave');
  let state=null,pending=null,view='category';
  const graph=new BubbleCanvas({host:get('klCanvas'),onConnect:(source,target)=>void openDialog(source,null,target),
    onEdit:id=>void openDialog('',id)});
  function toast(message,kind='info'){if(window.fosToast)window.fosToast(message,kind);}
  async function request(body) {
    const options={headers:{Accept:'application/json','X-Requested-With':'XMLHttpRequest'}};
    if(body) {
      options.method='POST';options.headers['Content-Type']='application/json';
      options.headers['X-CSRFToken']=form.querySelector('[name=csrfmiddlewaretoken]').value;
      options.body=JSON.stringify(body);
    }
    const response=await fetch(root.dataset.url,options);
    const data=await response.json().catch(()=>null);
    if(!response.ok || !data?.ok)throw new Error(data?.message || (response.status===404 ? '关联不存在或没有管理权限，请重新选择。' : '读取知识库关联失败，请重试。'));
    return data;
  }
  function paint() {
    if(!state)return;
    const map=new Map(state.nodes.map(node=>[node.slug,node]));
    const components=connectedComponents(state.nodes,state.links),groups=components.filter(group=>group.length>1);
    const query=get('klSearch').value.trim().toLocaleLowerCase();
    get('klCounts').textContent=`${state.nodes.length} 个知识库 · ${state.links.length} 条关联 · ${groups.length} 个关联群`;
    graph.setData(state.nodes,state.links,components);
    const matched=graph.setQuery(query),list=get('klLinkList');list.replaceChildren();
    for(const link of state.links) {
        const a=map.get(link.left),b=map.get(link.right);if(!a||!b)continue;
        if(query&&!(a.name+' '+b.name+' '+link.summary).toLocaleLowerCase().includes(query))continue;
        const row=element('div','kl-link-row'),edit=element('button','kl-link-edit');edit.type='button';edit.dataset.linkEdit=link.id;
        edit.append(element('strong','',`${a.name} ↔ ${b.name}`),element('p',link.summary ? '' : 'kl-empty-note',link.summary||'未添加备注 · 点击补充'));
        const remove=element('button','kl-delete','×');remove.type='button';remove.dataset.linkDelete=link.id;
        remove.setAttribute('aria-label',`删除「${a.name}」与「${b.name}」的关联`);
        row.append(edit,remove);list.append(row);
    }
    get('klRelationCount').textContent=`${list.children.length} 条关联`;
    get('klRelationDetails').hidden=!state.links.length;
    get('klStatus').textContent=!state.nodes.length ? '还没有知识库，创建后即可建立关联。' :
      query ? matched ? `找到 ${matched} 个相关知识库，已在画布中高亮。` : '没有匹配的知识库或关系说明。' : '';
  }
  async function ensureState() {
    if(state)return state;
    if(!pending) {
      get('klStatus').textContent='正在读取关联…';
      pending=request().then(data=>{state=data;paint();return data;}).finally(()=>{pending=null;});
    }
    return pending;
  }
  async function setView(next) {
    view=next;panel.hidden=view!=='cluster';
    root.querySelectorAll('[data-bubble-only]').forEach(node=>node.hidden=view!=='cluster');
    graph.setVisible(view==='cluster');
    document.querySelectorAll('[data-kb-category]').forEach(node=>node.hidden=view==='cluster');
    root.querySelectorAll('[data-kb-view]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.kbView===view)));
    try{sessionStorage.setItem('kb-manage-view',view);}catch{}
    if(view==='cluster')try{await ensureState();}catch(exc){get('klStatus').textContent=exc.message;}
  }
  function balancePair(changed) {
    if(left.value===right.value) {
      const other=changed===left ? right : left;
      other.value=[...other.options].find(option=>option.value!==changed.value)?.value || '';
    }
  }
  function matchingLink(a,b) {return state?.links.find(link=>(link.left===a&&link.right===b)||(link.left===b&&link.right===a));}
  function syncManualPair() {
    const link=matchingLink(left.value,right.value);summary.value=link?.summary||'';
    get('klDialogTitle').textContent=link ? '编辑关联备注' : '连接知识库';save.textContent=link ? '保存备注' : '保存关联';
  }
  async function openDialog(source='',linkId=null,target='') {
    if(view!=='cluster')return;
    try{await ensureState();}catch(exc){graph.clearDraft();toast(exc.message,'err');return;}
    if(state.nodes.length<2){toast('至少需要两个知识库才能建立关联。');return;}
    const link=linkId!==null ? state.links.find(item=>item.id===Number(linkId)) : target ? matchingLink(source,target) : null;
    const fixed=!!target||!!link;
    for(const select of [left,right]) {
      select.replaceChildren(...state.nodes.map(node=>{const option=element('option','',node.name);option.value=node.slug;return option;}));
      select.disabled=fixed;
    }
    left.value=link?.left || (state.nodes.some(node=>node.slug===source) ? source : state.nodes[0].slug);
    right.value=link?.right || target || state.nodes.find(node=>node.slug!==left.value).slug;
    summary.value=link?.summary || '';error.hidden=true;
    get('klDialogTitle').textContent=link ? '编辑关联备注' : '连接知识库';save.textContent=link ? '保存备注' : '保存关联';
    get('klPairFields').hidden=fixed;const preview=get('klPairPreview');preview.hidden=!fixed;preview.replaceChildren();
    if(fixed){const names=new Map(state.nodes.map(item=>[item.slug,item.name]));preview.append(element('strong','',names.get(left.value)),element('span','','↔'),element('strong','',names.get(right.value)));}
    else syncManualPair();
    if(!dialog.open)dialog.showModal();
    (matchMedia('(pointer:coarse)').matches ? save : summary).focus();
  }
  document.addEventListener('click',event=>{
    const connect=event.target.closest('[data-kb-connect]');if(connect){void openDialog(connect.dataset.kbConnect);return;}
    const edit=event.target.closest('[data-link-edit]');if(edit){void openDialog('',edit.dataset.linkEdit);return;}
    const remove=event.target.closest('[data-link-delete]');
    if(remove)void (async()=>{
      const confirmed=window.fosConfirm ? await window.fosConfirm({title:'删除知识库关联',message:'删除后，EnGenie 将不再通过这条关系联想到另一知识库。知识库内容保留。',confirmText:'删除关联',danger:true}) : window.confirm('删除这条知识库关联？');
      if(!confirmed)return;
      remove.disabled=true;
      try{state=await request({action:'delete',id:Number(remove.dataset.linkDelete)});paint();toast('关联已删除','ok');}
      catch(exc){remove.disabled=false;toast(exc.message,'err');}
    })();
  });
  root.querySelectorAll('[data-kb-view]').forEach(button=>button.addEventListener('click',()=>void setView(button.dataset.kbView)));
  get('klSearch').addEventListener('input',paint);
  get('klCancel').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>graph.clearDraft());
  left.addEventListener('change',()=>{balancePair(left);syncManualPair();});right.addEventListener('change',()=>{balancePair(right);syncManualPair();});
  get('klZoomIn').addEventListener('click',()=>graph.zoomBy(1.2));get('klZoomOut').addEventListener('click',()=>graph.zoomBy(1/1.2));
  get('klFit').addEventListener('click',()=>graph.fit());get('klArrange').addEventListener('click',()=>graph.arrange());
  form.addEventListener('submit',async event=>{
    event.preventDefault();error.hidden=true;
    save.disabled=true;save.textContent='保存中…';
    try {
      state=await request({action:'save',left:left.value,right:right.value,summary:summary.value.trim()});
      paint();dialog.close();toast('知识库关联已保存','ok');
    }catch(exc){error.textContent=exc.message;error.hidden=false;}
    finally{save.disabled=false;save.textContent='保存关联';}
  });
  document.addEventListener('kb-libraries-changed',()=>{state=null;if(view==='cluster')void setView(view);});
  try{if(sessionStorage.getItem('kb-manage-view')==='cluster')void setView('cluster');}catch{}
}

if(typeof document!=='undefined'){const root=document.getElementById('kbConnections');if(root)void init(root).catch(()=>{
  document.getElementById('klStatus').textContent='画布加载失败，请刷新页面重试。';
});}
