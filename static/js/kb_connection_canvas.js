/* 可平移、缩放的气泡画布。位置只影响展示；连线在用户确认后才保存。 */
const clamp=(value,low,high)=>Math.max(low,Math.min(high,value));
const hash=text=>{let value=0;for(const character of text)value=(value*31+character.charCodeAt(0))>>>0;return value;};
const node=(tag,className,text)=>{
  const value=document.createElement(tag);if(className)value.className=className;
  if(text!==undefined)value.textContent=String(text);return value;
};
const svg=(tag,attributes={},text)=>{
  const value=document.createElementNS('http://www.w3.org/2000/svg',tag);
  Object.entries(attributes).forEach(([key,item])=>value.setAttribute(key,item));
  if(text!==undefined)value.textContent=String(text);return value;
};

export function bubbleDiameter(item) {
  const raw=Number(item.category==='business' ? item.record_count : item.document_count),count=Number.isFinite(raw) ? Math.max(0,raw) : 0;
  return Math.round(136+Math.min(96,Math.log10(1+count)*28));
}

export function initialBubblePositions(nodes,components) {
  const positions=new Map();let x=60,y=70,rowHeight=0;
  const sizes=new Map(nodes.map(item=>[item.slug,bubbleDiameter(item)]));
  const groups=[...components].sort((a,b)=>b.length-a.length);
  for(const group of groups) {
    const count=group.length,columns=count<=2 ? count : Math.ceil(Math.sqrt(count));
    const size=Math.max(...group.map(slug=>sizes.get(slug)||136)),spacing=size+80;
    const rows=Math.ceil(count/columns),width=columns*spacing+20,height=rows*spacing+20;
    if(x>60 && x+width>1260){x=60;y+=rowHeight+50;rowHeight=0;}
    group.forEach((slug,index)=>positions.set(slug,{
      x:x+(index%columns)*spacing+(Math.floor(index/columns)%2 ? 22 : 0)+(size-(sizes.get(slug)||136))/2,
      y:y+Math.floor(index/columns)*spacing+(index%columns%2 ? 16 : 0)+(size-(sizes.get(slug)||136))/2,
    }));
    x+=width+60;rowHeight=Math.max(rowHeight,height);
  }
  // 忽略不在节点清单中的非法端点，布局不创造额外知识库。
  const valid=new Set(nodes.map(item=>item.slug));
  for(const slug of positions.keys())if(!valid.has(slug))positions.delete(slug);
  return positions;
}

export function bubbleWire(a,b,index=0,obstacles=[]) {
  const ra=(a.size||160)/2,rb=(b.size||160)/2,ax=a.x+ra,ay=a.y+ra,bx=b.x+rb,by=b.y+rb;
  const dx=bx-ax,dy=by-ay,distance=Math.hypot(dx,dy)||1,ux=dx/distance,uy=dy/distance;
  const padding=Math.max(0,Math.min(2,(distance-ra-rb)/4));
  const start={x:ax+ux*(ra+padding),y:ay+uy*(ra+padding)},end={x:bx-ux*(rb+padding),y:by-uy*(rb+padding)};
  const gap=Math.max(0,distance-ra-rb),sign=index%2 ? 1 : -1;
  let bend=Math.min(22,gap*.09)/(1+gap/600)*sign;
  // 普通连线只保留很轻的弧度；确有其它气泡挡路时才绕行。
  for(const obstacle of obstacles) {
    const r=obstacle.size/2,cx=obstacle.x+r,cy=obstacle.y+r,t=((cx-ax)*dx+(cy-ay)*dy)/(distance*distance);
    if(t<=.12||t>=.88)continue;
    const cross=(cx-ax)*(-uy)+(cy-ay)*ux;
    if(Math.abs(cross)<r+12)bend=(cross>=0 ? -1 : 1)*Math.max(Math.abs(bend),(r+20-Math.abs(cross))/.7);
  }
  const vx=end.x-start.x,vy=end.y-start.y;
  return `M${start.x},${start.y} C${start.x+vx*.33-uy*bend},${start.y+vy*.33+ux*bend} ${end.x-vx*.33-uy*bend},${end.y-vy*.33+ux*bend} ${end.x},${end.y}`;
}

export function connectionAttraction(positions,links,connection,preferredSource,sizes=new Map()) {
  const degree=new Map();for(const link of links)for(const slug of [link.left,link.right])degree.set(slug,(degree.get(slug)||0)+1);
  const a=connection.left,b=connection.right,da=degree.get(a)||0,db=degree.get(b)||0;
  const lighter=da===db ? ([a,b].includes(preferredSource) ? preferredSource : a) : da<db ? a : b;
  const heavier=lighter===a ? b : a,from=positions.get(lighter),hub=positions.get(heavier);if(!from||!hub)return null;
  const lightRadius=(sizes.get(lighter)||160)/2,hubRadius=(sizes.get(heavier)||160)/2;
  const lightCenter={x:from.x+lightRadius,y:from.y+lightRadius},hubCenter={x:hub.x+hubRadius,y:hub.y+hubRadius};
  const radius=lightRadius+hubRadius+35+Math.min(35,(degree.get(heavier)||0)*4),distance=Math.hypot(lightCenter.x-hubCenter.x,lightCenter.y-hubCenter.y);
  if(distance<=radius+22)return {lighter,heavier,target:{...from},degree};
  const angle=Math.atan2(lightCenter.y-hubCenter.y,lightCenter.x-hubCenter.x),ideal={x:hubCenter.x+Math.cos(angle)*radius-lightRadius,y:hubCenter.y+Math.sin(angle)*radius-lightRadius};
  let target=null,best=Infinity;
  for(const extra of [0,75,150])for(let step=0;step<12;step++) {
    const candidateAngle=angle+step*Math.PI/6,candidate={x:hubCenter.x+Math.cos(candidateAngle)*(radius+extra)-lightRadius,y:hubCenter.y+Math.sin(candidateAngle)*(radius+extra)-lightRadius};
    if(candidate.x<25||candidate.y<25)continue;
    let score=extra+Math.hypot(candidate.x-ideal.x,candidate.y-ideal.y)*.3;
    for(const [slug,point] of positions)if(slug!==lighter&&slug!==heavier){const r=(sizes.get(slug)||160)/2;
      score+=Math.max(0,lightRadius+r+30-Math.hypot(candidate.x+lightRadius-point.x-r,candidate.y+lightRadius-point.y-r))**2*30;}
    if(score<best){best=score;target=candidate;}
  }
  return {lighter,heavier,target:target||{...from},degree};
}

export class BubbleCanvas {
  constructor({host,onConnect,onEdit}) {
    this.host=host;this.world=document.getElementById('klWorld');this.edges=document.getElementById('klEdges');
    this.bubbles=document.getElementById('klBubbles');this.chips=document.getElementById('klClusterChips');
    this.wireControls=document.getElementById('klWireControls');this.toolbars=new Map();this.selectedEdge=null;this.hoveredEdge=null;this.edgeHideTimer=null;
    this.status=document.getElementById('klConnectStatus');this.selection=document.getElementById('klSelection');
    this.onConnect=onConnect;this.onEdit=onEdit;this.nodes=[];this.links=[];this.components=[];
    this.positions=new Map();this.displayPositions=new Map();this.elements=new Map();this.edgeElements=new Map();this.map=new Map();this.sizes=new Map();
    this.glides=new Map();this.reactions=new Map();this.degree=new Map();this.motionClock=0;this.motionFrame=null;this.lastMotion=null;this.lastConnection=null;
    this.motionPreference=matchMedia('(prefers-reduced-motion: reduce)');this.reduced=this.motionPreference.matches;
    this.motionPreference.addEventListener('change',()=>{this.reduced=this.motionPreference.matches;if(this.reduced){this.glides.clear();this.reactions.clear();}this.draw();this.startMotion();});
    this.camera={x:0,y:0,zoom:1};this.fitted=false;this.visible=false;this.selected=null;
    this.gesture=null;this.activeSource=null;this.draft=null;this.target=null;this.frame=null;this.panFrame=null;this.suppressClick=0;
    this.host.addEventListener('pointerdown',event=>this.pointerDown(event));
    this.host.addEventListener('pointermove',event=>this.pointerMove(event));
    this.host.addEventListener('pointerup',event=>this.pointerUp(event));
    this.host.addEventListener('pointercancel',()=>this.cancelGesture());
    this.host.addEventListener('lostpointercapture',()=>{if(this.gesture)this.cancelGesture();});
    // 触屏抬起后会补发兼容 click；弹窗出现时不能让这次 click 穿透到取消/保存按钮。
    document.addEventListener('pointerdown',()=>{this.ignoreClickUntil=0;},true);
    document.addEventListener('click',event=>{if(event.detail>0&&performance.now()<(this.ignoreClickUntil||0)){
      event.preventDefault();event.stopImmediatePropagation();this.ignoreClickUntil=0;
    }},true);
    this.host.addEventListener('click',event=>{
      if(event.detail!==0&&performance.now()<this.suppressClick)return;
      const port=event.target.closest('[data-kb-port]');
      if(port){this.choosePort(port.dataset.kbPort);return;}
      const body=event.target.closest('[data-kb-body]');if(body)this.select(body.dataset.kbBody);
    });
    this.host.addEventListener('keydown',event=>{
      if(event.key==='Escape'){this.cancelGesture();this.selectEdge(null);event.preventDefault();}
      if(event.target===this.host && ['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(event.key)) {
        event.preventDefault();this.camera.x+=event.key==='ArrowLeft' ? 40 : event.key==='ArrowRight' ? -40 : 0;
        this.camera.y+=event.key==='ArrowUp' ? 40 : event.key==='ArrowDown' ? -40 : 0;this.applyCamera();
      }
    });
    this.host.addEventListener('wheel',event=>{
      if(this.gesture)return;event.preventDefault();
      if(event.ctrlKey||event.metaKey)this.zoomBy(Math.exp(-event.deltaY*.008),{x:event.clientX,y:event.clientY});
      else {const unit=event.deltaMode===1 ? 16 : event.deltaMode===2 ? this.host.clientHeight : 1;
        this.camera.x-=event.deltaX*unit;this.camera.y-=event.deltaY*unit;this.applyCamera();}
    },{passive:false});
    this.observer=new ResizeObserver(()=>{
      const width=this.host.clientWidth;
      if(width && this.visible && !this.gesture && Math.abs(width-(this.lastWidth||0))>1){this.lastWidth=width;this.fit();}
    });this.observer.observe(this.host);
    window.addEventListener('blur',()=>this.cancelGesture());
    this.intersection=new IntersectionObserver(entries=>{this.onScreen=entries[0].isIntersecting;if(this.onScreen)this.startMotion();else this.stopMotion();});this.intersection.observe(this.host);
    document.addEventListener('visibilitychange',()=>{if(document.hidden)this.stopMotion();else this.startMotion();});
    window.addEventListener('pagehide',()=>{this.cancelGesture();this.stopMotion();this.observer.disconnect();this.intersection.disconnect();});
  }

  setData(nodes,links,components) {
    if(nodes===this.nodes && links===this.links)return;
    const hadData=this.nodes.length>0,previous=new Set(this.links.map(link=>[link.left,link.right].sort().join('\0'))),preferred=this.lastConnection?.source;
    this.cancelGesture();this.nodes=nodes;this.links=links;this.components=components;
    this.map=new Map(nodes.map(item=>[item.slug,item]));
    const oldSizes=this.sizes;this.sizes=new Map(nodes.map(item=>[item.slug,bubbleDiameter(item)]));
    for(const [slug,size] of this.sizes)if(oldSizes.has(slug)&&this.positions.has(slug)){
      const difference=(oldSizes.get(slug)-size)/2,p=this.positions.get(slug);this.positions.set(slug,{x:p.x+difference,y:p.y+difference});
      const glide=this.glides.get(slug);if(glide){glide.from.x+=difference;glide.from.y+=difference;}
    }
    const layout=initialBubblePositions(nodes,components);
    const newNodes=nodes.filter(item=>!this.positions.has(item.slug));
    if(!this.positions.size)this.positions=layout;
    else {
      const bottom=Math.max(0,...[...this.positions].map(([slug,p])=>p.y+this.sizes.get(slug)))+80;
      newNodes.forEach((item,index)=>this.positions.set(item.slug,{x:60+index%4*310,y:bottom+Math.floor(index/4)*310}));
    }
    for(const slug of this.positions.keys())if(!this.map.has(slug)){this.positions.delete(slug);this.glides.delete(slug);this.reactions.delete(slug);}
    this.degree=new Map();for(const link of links)for(const slug of [link.left,link.right])this.degree.set(slug,(this.degree.get(slug)||0)+1);
    const newLinks=hadData ? links.filter(link=>!previous.has([link.left,link.right].sort().join('\0'))) : [];
    const settling=new Set();
    for(const link of newLinks) {
      const plan=connectionAttraction(this.positions,links,link,preferred,this.sizes);if(!plan)continue;
      const from={...this.positions.get(plan.lighter)},hub=this.positions.get(plan.heavier);
      this.positions.set(plan.lighter,plan.target);settling.add(plan.lighter);settling.add(plan.heavier);
      if(!this.reduced) {
        this.glides.set(plan.lighter,{from,start:this.motionClock,duration:clamp(1050+Math.hypot(plan.target.x-from.x,plan.target.y-from.y)*.45,1100,1700)});
        const dx=from.x-hub.x,dy=from.y-hub.y,length=Math.hypot(dx,dy)||1;
        this.reactions.set(plan.heavier,{x:dx/length*5,y:dy/length*5,start:this.motionClock,duration:1250});
      }
    }
    this.bubbles.replaceChildren();this.elements.clear();
    for(const item of nodes) {
      const size=this.sizes.get(item.slug),bubble=node('div',`kl-bubble ${item.category==='business' ? 'business' : ''}${size>185 ? ' is-large' : ''}`);
      bubble.dataset.kbNode=item.slug;
      bubble.style.width=size+'px';bubble.style.height=size+'px';
      if(settling.has(item.slug)){bubble.classList.add('is-settling');setTimeout(()=>bubble.classList.remove('is-settling'),1800);}
      const body=node('button','kl-bubble-body');body.type='button';body.dataset.kbBody=item.slug;
      const count=Math.max(0,Number(item.category==='business' ? item.record_count : item.document_count)||0),volume=new Intl.NumberFormat(globalThis.FOSI18n?.locale || 'zh-CN').format(count)+(item.category==='business' ? ' 条记录' : ' 份文档');
      body.title=item.name+' · '+volume;body.setAttribute('aria-label',`选择知识库：${item.name}，${volume}`);body.setAttribute('aria-pressed','false');
      const icon=svg('svg',{viewBox:'0 0 24 24','aria-hidden':'true'});
      icon.append(svg('path',{d:item.category==='business' ? 'M4 4h16v16H4zM4 9h16M4 14h16M10 4v16' : 'M4 5c3-1 5-1 8 1 3-2 5-2 8-1v14c-3-1-5-1-8 1-3-2-5-2-8-1zM12 6v14'}));
      body.append(icon,node('span','kl-bubble-name',item.name),node('span','kl-bubble-count',volume));
      const port=node('button','kl-port');port.type='button';port.dataset.kbPort=item.slug;
      port.style.left=(size+8)+'px';port.style.top=(size/2)+'px';
      port.setAttribute('aria-label',`连接「${item.name}」到另一知识库`);port.title='拖动圆点连线，也可依次点击两个圆点';
      port.append(node('span','','＋'));bubble.append(body,port);this.bubbles.append(bubble);this.elements.set(item.slug,bubble);
    }
    this.edges.replaceChildren();this.edgeElements.clear();this.wireControls.replaceChildren();this.toolbars.clear();this.selectedEdge=null;this.hoveredEdge=null;
    for(const link of links) {
      if(!this.map.has(link.left)||!this.map.has(link.right))continue;
      const names=`${this.map.get(link.left).name} ↔ ${this.map.get(link.right).name}`;
      const group=svg('g',{class:`kl-edge${newLinks.includes(link) ? ' is-new' : ''}`,tabindex:'0',role:'button','data-link-edit':link.id,'aria-label':`编辑关联：${names}`});
      if(newLinks.includes(link))setTimeout(()=>group.classList.remove('is-new'),1800);
      const line=svg('path',{class:'kl-edge-line','vector-effect':'non-scaling-stroke',pathLength:'1'}),hit=svg('path',{class:'kl-edge-hit','vector-effect':'non-scaling-stroke'});
      const glow=svg('path',{class:'kl-edge-glow','vector-effect':'non-scaling-stroke'});
      group.append(svg('title',{},`${names}${link.summary ? '\n'+link.summary : '\n可添加关联备注'}`),glow,line,hit);
      const toolbar=node('div','kl-wire-toolbar');toolbar.hidden=true;toolbar.dataset.wireToolbar=link.id;toolbar.setAttribute('role','group');toolbar.setAttribute('aria-label',`${names} 的关联操作`);
      const edit=node('button','kl-wire-edit');edit.type='button';edit.dataset.linkEdit=link.id;edit.title='编辑关联备注';edit.setAttribute('aria-label',`编辑「${names}」的关联备注`);
      const pen=svg('svg',{viewBox:'0 0 20 20','aria-hidden':'true'});pen.append(svg('path',{d:'M12.5 3.5l4 4M4 12l9-9 4 4-9 9-5 1z'}));edit.append(pen);
      const remove=node('button','kl-wire-delete');remove.type='button';remove.dataset.linkDelete=link.id;remove.title='删除这条关联';remove.setAttribute('aria-label',`删除「${names}」的关联`);
      const trash=svg('svg',{viewBox:'0 0 20 20','aria-hidden':'true'});trash.append(svg('path',{d:'M3.5 5h13M7 5V3h6v2M5 5l1 12h8l1-12M8 8v6M12 8v6'}));remove.append(trash);
      toolbar.append(edit,remove);this.wireControls.append(toolbar);this.toolbars.set(link.id,toolbar);
      group.addEventListener('click',event=>{event.stopPropagation();this.selectEdge(link.id);});
      group.addEventListener('dblclick',event=>{event.stopPropagation();this.onEdit(link.id);});
      group.addEventListener('pointerenter',()=>this.showEdgeTools(link.id));group.addEventListener('pointerleave',()=>this.deferHideEdgeTools());
      group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();event.stopPropagation();this.selectEdge(link.id);edit.focus({preventScroll:true});}});
      toolbar.addEventListener('pointerenter',()=>this.showEdgeTools(link.id));toolbar.addEventListener('pointerleave',()=>this.deferHideEdgeTools());
      toolbar.addEventListener('focusin',()=>this.selectEdge(link.id));
      this.edges.append(group);this.edgeElements.set(link.id,{line,hit,glow});
    }
    this.preview=svg('path',{class:'kl-draft-wire','vector-effect':'non-scaling-stroke',hidden:'true'});this.edges.append(this.preview);
    this.chips.replaceChildren();
    const all=node('button','is-active','全部');all.type='button';all.addEventListener('click',()=>this.focusGroup(all));this.chips.append(all);
    components.filter(group=>group.length>1).forEach((group,index)=>{
      const chip=node('button','',`关联群 ${index+1} · ${group.length}`);chip.type='button';
      chip.addEventListener('click',()=>this.focusGroup(chip,group));this.chips.append(chip);
    });
    const singles=components.filter(group=>group.length===1).flat();
    if(singles.length){const chip=node('button','',`未关联 · ${singles.length}`);chip.type='button';chip.addEventListener('click',()=>this.focusGroup(chip,singles));this.chips.append(chip);}
    this.draw();this.startMotion();if(this.selected && this.map.has(this.selected))this.select(this.selected);else this.selection.hidden=true;
    if(!this.fitted && this.visible)requestAnimationFrame(()=>this.fit());
  }

  setVisible(value) {this.visible=value;if(!value){this.cancelGesture();this.stopMotion();}else {this.startMotion();requestAnimationFrame(()=>{if(!this.fitted)this.fit();});}}
  focusGroup(chip,slugs) {this.chips.querySelectorAll('button').forEach(button=>button.classList.toggle('is-active',button===chip));this.fit(slugs);}
  arrange() {this.glides.clear();this.reactions.clear();this.positions=initialBubblePositions(this.nodes,this.components);this.draw();this.fit();}
  fit(slugs) {
    if(!this.visible || !this.host.clientWidth || !this.positions.size)return;
    const points=(slugs||this.nodes.map(item=>item.slug)).map(slug=>this.positions.get(slug)).filter(Boolean);if(!points.length)return;
    const minX=Math.min(...points.map(p=>p.x))-50,minY=Math.min(...points.map(p=>p.y))-50;
    const sizes=[...this.sizes.values()],maxSize=Math.max(136,...sizes);
    const width=Math.max(...points.map(p=>p.x))+maxSize+70-minX,height=Math.max(...points.map(p=>p.y))+maxSize+60-minY;
    const zoom=clamp(Math.min((this.host.clientWidth-50)/width,(this.host.clientHeight-80)/height),.15,1.1);
    this.camera={zoom,x:(this.host.clientWidth-width*zoom)/2-minX*zoom,y:(this.host.clientHeight-height*zoom)/2-minY*zoom};
    this.fitted=true;this.applyCamera();
  }
  zoomBy(factor,anchor) {
    const rect=this.host.getBoundingClientRect(),x=anchor ? anchor.x-rect.left : rect.width/2,y=anchor ? anchor.y-rect.top : rect.height/2;
    const old=this.camera.zoom,next=clamp(old*factor,.15,2);
    this.camera.x=x-(x-this.camera.x)/old*next;this.camera.y=y-(y-this.camera.y)/old*next;this.camera.zoom=next;this.applyCamera();
  }
  applyCamera() {
    const {x,y,zoom}=this.camera;this.world.style.transform=`translate(${x}px,${y}px) scale(${zoom})`;
    this.world.style.setProperty('--kl-port-scale',1/zoom);this.world.style.setProperty('--kl-label-scale',Math.min(1.55,Math.max(1,1/Math.sqrt(zoom))));
    document.getElementById('klZoomLevel').textContent=Math.round(zoom*100)+'%';
  }
  point(x,y) {const rect=this.host.getBoundingClientRect();return {x:(x-rect.left-this.camera.x)/this.camera.zoom,y:(y-rect.top-this.camera.y)/this.camera.zoom};}
  portPoint(slug) {const p=this.displayPositions.get(slug)||this.positions.get(slug),size=this.sizes.get(slug)||136;return p ? {x:p.x+size+8,y:p.y+size/2} : null;}
  moveBubble(slug,p) {
    const position={x:Math.max(25,p.x),y:Math.max(25,p.y)};this.positions.set(slug,position);
  }
  scheduleDraw() {if(!this.frame)this.frame=requestAnimationFrame(()=>{this.frame=null;this.draw();});}
  draw() {
    this.displayPositions.clear();
    for(const [slug,p] of this.positions) {
      const drift=this.floatOffset(slug),glide=this.glides.get(slug),reaction=this.reactions.get(slug);let x=p.x,y=p.y;
      if(glide){const progress=clamp((this.motionClock-glide.start)/glide.duration,0,1),t=progress-1,ease=1+1.65*t*t*t+.65*t*t;
        x=glide.from.x+(p.x-glide.from.x)*ease;y=glide.from.y+(p.y-glide.from.y)*ease;if(progress===1)this.glides.delete(slug);}
      if(reaction){const progress=clamp((this.motionClock-reaction.start)/reaction.duration,0,1),amount=Math.sin(progress*Math.PI)*(1-progress);
        x+=reaction.x*amount;y+=reaction.y*amount;if(progress===1)this.reactions.delete(slug);}
      const position={x:x+drift.x,y:y+drift.y};this.displayPositions.set(slug,position);
      const bubble=this.elements.get(slug);if(bubble)bubble.style.transform=`translate(${position.x.toFixed(2)}px,${position.y.toFixed(2)}px)`;
    }
    const points=[...this.positions.values()],width=Math.max(1000,...points.map(p=>p.x+300)),height=Math.max(650,...points.map(p=>p.y+300));
    this.world.style.width=width+'px';this.world.style.height=height+'px';this.edges.setAttribute('width',width);this.edges.setAttribute('height',height);
    this.links.forEach((link,index)=>{
      const a=this.displayPositions.get(link.left),b=this.displayPositions.get(link.right),edge=this.edgeElements.get(link.id);if(!a||!b||!edge)return;
      const obstacles=[...this.displayPositions].filter(([slug])=>slug!==link.left&&slug!==link.right).map(([slug,p])=>({...p,size:this.sizes.get(slug)}));
      const d=bubbleWire({...a,size:this.sizes.get(link.left)},{...b,size:this.sizes.get(link.right)},index,obstacles);
      edge.line.setAttribute('d',d);edge.hit.setAttribute('d',d);edge.glow.setAttribute('d',d);
      const control=this.toolbars.get(link.id);
      if(control&&!control.hidden){const v=d.match(/[-+]?(?:\d*\.)?\d+(?:e[-+]?\d+)?/gi).map(Number);
        const x=(v[0]+3*v[2]+3*v[4]+v[6])/8,y=(v[1]+3*v[3]+3*v[5]+v[7])/8;control.style.left=x+'px';control.style.top=y+'px';}
    });
    if(!this.preview)return;
    const start=this.draft && this.portPoint(this.draft.source),end=this.draft && (this.draft.target ? this.portPoint(this.draft.target) : this.draft.point);
    this.preview.toggleAttribute('hidden',!start||!end);
    if(start&&end){const bend=Math.max(70,Math.abs(end.x-start.x)*.28);this.preview.setAttribute('d',`M${start.x},${start.y} C${start.x+bend},${start.y-75} ${end.x+bend},${end.y-75} ${end.x},${end.y}`);}
  }

  floatOffset(slug) {
    if(this.reduced)return {x:0,y:0};
    const seed=hash(slug),phase=seed%997/997*Math.PI*2,weight=1/Math.sqrt(1+(this.degree.get(slug)||0)*.3);
    return {x:Math.sin(this.motionClock/(7800+seed%2300)*Math.PI*2+phase)*4*weight,
      y:Math.cos(this.motionClock/(9200+seed%2900)*Math.PI*2+phase)*6*weight};
  }
  startMotion() {
    if(this.motionFrame||!this.visible||this.onScreen===false||document.hidden||this.reduced||this.gesture||this.activeSource||this.draft?.target)return;
    this.lastMotion=null;
    const tick=now=>{
      this.motionFrame=null;
      if(!this.visible||this.onScreen===false||document.hidden||this.reduced||this.gesture||this.activeSource||this.draft?.target){this.lastMotion=null;return;}
      if(this.lastMotion!==null)this.motionClock+=Math.min(40,now-this.lastMotion);this.lastMotion=now;this.draw();this.motionFrame=requestAnimationFrame(tick);
    };this.motionFrame=requestAnimationFrame(tick);
  }
  stopMotion() {if(this.motionFrame)cancelAnimationFrame(this.motionFrame);this.motionFrame=null;this.lastMotion=null;}

  showEdgeTools(id) {
    clearTimeout(this.edgeHideTimer);this.hoveredEdge=id;
    for(const [key,toolbar] of this.toolbars)toolbar.hidden=key!==id&&key!==this.selectedEdge;
    this.draw();
  }
  deferHideEdgeTools() {
    clearTimeout(this.edgeHideTimer);this.edgeHideTimer=setTimeout(()=>{
      this.hoveredEdge=null;for(const [id,toolbar] of this.toolbars)toolbar.hidden=id!==this.selectedEdge;
    },180);
  }
  selectEdge(id) {
    clearTimeout(this.edgeHideTimer);this.selectedEdge=id;this.hoveredEdge=null;
    for(const [key,toolbar] of this.toolbars){toolbar.hidden=key!==id;
      this.edgeElements.get(key)?.line.parentElement.classList.toggle('is-selected',key===id);}
    this.draw();
  }

  select(slug) {
    if(this.selected)this.elements.get(this.selected)?.classList.remove('is-selected');this.selected=slug;
    const item=this.map.get(slug);if(!item)return;
    for(const [key,bubble] of this.elements)bubble.querySelector('[data-kb-body]').setAttribute('aria-pressed',String(key===slug));
    this.elements.get(slug)?.classList.add('is-selected');
    this.selection.replaceChildren();this.selection.hidden=false;
    const text=node('div');text.append(node('strong','',item.name),node('span','',item.description||'拖动圆点连接其他知识库。'));
    const actions=node('div','kl-selection-actions'),open=node('a','btn sm outline','打开知识库');open.href=item.url;
    const connect=node('button','btn sm outline','连接…');connect.type='button';connect.dataset.kbConnect=slug;
    actions.append(open,connect);this.selection.append(text,actions);
  }
  setQuery(query) {
    const text=query.trim().toLocaleLowerCase(),matches=new Set();
    for(const item of this.nodes)if(!text||(item.name+' '+item.description).toLocaleLowerCase().includes(text))matches.add(item.slug);
    if(text)for(const link of this.links)if(link.summary.toLocaleLowerCase().includes(text)){matches.add(link.left);matches.add(link.right);}
    for(const [slug,bubble] of this.elements){bubble.classList.toggle('is-muted',!!text&&!matches.has(slug));bubble.classList.toggle('is-match',!!text&&matches.has(slug));}
    if(text&&matches.size)this.fit([...matches]);
    return matches.size;
  }
  choosePort(slug) {
    if(this.activeSource && this.activeSource!==slug){this.complete(this.activeSource,slug);return;}
    if(this.activeSource===slug){this.clearDraft();return;}
    this.clearDraft();this.activeSource=slug;this.elements.get(slug)?.classList.add('is-source');
    const start=this.portPoint(slug);this.draft={source:slug,point:{x:start.x+100,y:start.y-40}};
    this.status.textContent=`已选择「${this.map.get(slug).name}」，拖动或点击另一个知识库的圆点完成连接。`;this.scheduleDraw();
  }
  complete(source,target) {
    if(source===target){this.clearDraft();return;}
    this.activeSource=null;this.gesture=null;this.stopAutoPan();this.setTarget(null);
    this.elements.get(source)?.classList.add('is-source');this.elements.get(target)?.classList.add('is-target');
    this.lastConnection={source,target};this.draft={source,target};this.draw();this.onConnect(source,target);
  }
  clearDraft() {
    this.activeSource=null;this.draft=null;this.setTarget(null);
    for(const bubble of this.elements.values())bubble.classList.remove('is-source','is-target');
    this.status.textContent='';this.scheduleDraw();
    this.lastConnection=null;this.startMotion();
  }
  setTarget(slug) {if(this.target)this.elements.get(this.target)?.classList.remove('is-target');this.target=slug;if(slug)this.elements.get(slug)?.classList.add('is-target');}
  hit(x,y,source) {
    for(const element of document.elementsFromPoint(x,y)) {
      const bubble=element.closest?.('[data-kb-node]');
      if(bubble&&this.host.contains(bubble)&&bubble.dataset.kbNode!==source)return bubble.dataset.kbNode;
    }
    return null;
  }
  pointerDown(event) {
    if(event.button!==0 || !event.isPrimary || this.gesture || event.target.closest('.kl-edge'))return;
    const port=event.target.closest('[data-kb-port]'),body=event.target.closest('[data-kb-body]');
    if(!port&&!body&&event.target.closest('button,a'))return;
    event.preventDefault();(port||body||this.host).focus({preventScroll:true});
    const world=this.point(event.clientX,event.clientY);
    this.gesture={kind:port ? 'connect' : body ? 'move' : 'pan',pointer:event.pointerId,slug:port?.dataset.kbPort||body?.dataset.kbBody,
      x:event.clientX,y:event.clientY,world,moved:false,camera:{...this.camera},position:body ? {...this.positions.get(body.dataset.kbBody)} : null};
    this.host.classList.add('is-gesturing');
    try{this.host.setPointerCapture(event.pointerId);}catch{}
  }
  pointerMove(event) {
    const gesture=this.gesture;
    if(!gesture) {
      if(this.activeSource){const target=this.hit(event.clientX,event.clientY,this.activeSource);this.setTarget(target);
        this.draft={source:this.activeSource,target,point:this.point(event.clientX,event.clientY)};this.scheduleDraw();}
      return;
    }
    if(event.pointerId!==gesture.pointer)return;
    gesture.last={x:event.clientX,y:event.clientY};
    if(!gesture.moved&&Math.hypot(event.clientX-gesture.x,event.clientY-gesture.y)>5) {
      gesture.moved=true;
      if(gesture.kind==='connect'){this.clearDraft();this.elements.get(gesture.slug)?.classList.add('is-source');this.host.classList.add('is-connecting');}
      if(gesture.kind==='move')this.elements.get(gesture.slug)?.classList.add('is-dragging');
      if(gesture.kind==='move'){
        const current=this.displayPositions.get(gesture.slug),drift=this.floatOffset(gesture.slug);
        if(current){gesture.position={x:current.x-drift.x,y:current.y-drift.y};this.positions.set(gesture.slug,gesture.position);}
        this.glides.delete(gesture.slug);this.reactions.delete(gesture.slug);
      }
    }
    if(!gesture.moved)return;
    if(gesture.kind==='connect') {
      const target=this.hit(event.clientX,event.clientY,gesture.slug);this.setTarget(target);
      this.draft={source:gesture.slug,target,point:this.point(event.clientX,event.clientY)};this.scheduleDraw();this.startAutoPan();
    } else if(gesture.kind==='move') {
      const p=this.point(event.clientX,event.clientY);this.moveBubble(gesture.slug,{x:gesture.position.x+p.x-gesture.world.x,y:gesture.position.y+p.y-gesture.world.y});this.draw();
    } else {this.camera.x=gesture.camera.x+event.clientX-gesture.x;this.camera.y=gesture.camera.y+event.clientY-gesture.y;this.applyCamera();}
  }
  pointerUp(event) {
    const gesture=this.gesture;if(!gesture||event.pointerId!==gesture.pointer)return;
    this.gesture=null;this.stopAutoPan();this.host.classList.remove('is-gesturing','is-connecting');
    this.elements.get(gesture.slug)?.classList.remove('is-dragging');this.suppressClick=performance.now()+400;
    this.ignoreClickUntil=performance.now()+500;
    try{this.host.releasePointerCapture(event.pointerId);}catch{}
    if(gesture.kind==='connect') {
      const target=this.hit(event.clientX,event.clientY,gesture.slug);
      if(gesture.moved){if(target)this.complete(gesture.slug,target);else this.clearDraft();}
      else this.choosePort(gesture.slug);
    } else if(gesture.kind==='move'&&!gesture.moved)this.select(gesture.slug);
    else if(gesture.kind==='pan'&&!gesture.moved)this.clearDraft();
    this.startMotion();
  }
  cancelGesture() {
    const gesture=this.gesture;this.gesture=null;this.stopAutoPan();this.host.classList.remove('is-gesturing','is-connecting');
    if(gesture){this.elements.get(gesture.slug)?.classList.remove('is-dragging');try{this.host.releasePointerCapture(gesture.pointer);}catch{}}
    this.clearDraft();
  }
  startAutoPan() {
    if(this.panFrame)return;
    const tick=()=>{
      this.panFrame=null;const gesture=this.gesture;if(gesture?.kind!=='connect'||!gesture.last)return;
      const rect=this.host.getBoundingClientRect(),{x,y}=gesture.last;
      if(x>=rect.left&&x<=rect.right&&y>=rect.top&&y<=rect.bottom) {
        const dx=x<rect.left+30 ? 6 : x>rect.right-30 ? -6 : 0,dy=y<rect.top+30 ? 6 : y>rect.bottom-30 ? -6 : 0;
        if(dx||dy){this.camera.x+=dx;this.camera.y+=dy;this.applyCamera();
          const target=this.hit(x,y,gesture.slug);this.setTarget(target);this.draft={source:gesture.slug,target,point:this.point(x,y)};this.scheduleDraw();}
      }
      this.panFrame=requestAnimationFrame(tick);
    };this.panFrame=requestAnimationFrame(tick);
  }
  stopAutoPan() {if(this.panFrame)cancelAnimationFrame(this.panFrame);this.panFrame=null;}
}
