/* EnGenie Report Charts: a shared, offline SVG renderer for declarative reports. */
(function(root) {
  'use strict';
  const NS='http://www.w3.org/2000/svg';
  const COLORS=['#3574d5','#18a48e','#f09b45','#8b68cf','#de6583','#378da7','#b89a30','#697b95']
    .map((color,index)=>`var(--erc-series-${index+1},${color})`);
  const SURFACE='var(--surface,#fff)', TEXT='var(--text,#16324f)', OTHER='var(--erc-other,#a8b5c5)';
  const finite=value=>value!==null&&value!==undefined&&Number.isFinite(Number(value));
  const format=(value,language='en')=>{if(!finite(value))return '—';const number=Number(value);return number&&(Math.abs(number)>=1e12||Math.abs(number)<1e-9)?number.toExponential(6):new Intl.NumberFormat(language==='zh-hans'?'zh-CN':'en-US',{maximumFractionDigits:12}).format(number);};
  const short=(value,length=20)=>String(value).length>length?String(value).slice(0,length-1)+'…':String(value);
  function node(document,tag,attrs={},text){
    const n=document.createElementNS(NS,tag);for(const[key,value]of Object.entries(attrs))n.setAttribute(key,String(value));
    if(text!==undefined)n.textContent=String(text);return n;
  }
  function tooltip(host,document){
    const win=document.defaultView||root,tip=document.createElement('div');tip.className='erc-tooltip';tip.hidden=true;tip.setAttribute('role','tooltip');tip.setAttribute('translate','no');document.body.appendChild(tip);
    const listeners=[];let active=null,pinned=false;
    function listen(target,type,handler,capture=false){target.addEventListener(type,handler,capture);listeners.push(()=>target.removeEventListener(type,handler,capture));}
    function hide(){if(active)active.classList.remove('erc-point-active');active=null;pinned=false;tip.hidden=true;}
    function bind(target,label){
      function show(pin=false){hide();active=target;pinned=pin;tip.textContent=label;tip.hidden=false;target.classList.add('erc-point-active');
        const anchor=target.getBoundingClientRect(),box=tip.getBoundingClientRect(),width=win.innerWidth||900,height=win.innerHeight||700;
        tip.style.left=Math.max(8,Math.min(width-box.width-8,anchor.left+anchor.width/2-box.width/2))+'px';
        tip.style.top=Math.max(8,Math.min(height-box.height-8,anchor.top-box.height-10<8?anchor.bottom+10:anchor.top-box.height-10))+'px';}
      target.setAttribute('tabindex','0');target.setAttribute('role','img');target.setAttribute('aria-label',label);target.classList.add('erc-hit');
      listen(target,'pointerenter',()=>show());listen(target,'pointerleave',()=>{if(!pinned&&document.activeElement!==target)hide();});listen(target,'focus',()=>show());listen(target,'blur',hide);
      listen(target,'click',()=>{if(active===target&&pinned)hide();else show(true);});listen(target,'keydown',event=>{if(event.key==='Escape')hide();else if(event.key==='Enter'||event.key===' '){event.preventDefault();show(true);}});
    }
    listen(win,'resize',hide);listen(win,'scroll',hide,true);listen(document,'keydown',event=>{if(event.key==='Escape')hide();});
    listen(document,'pointerdown',event=>{if(active&&event.target!==active&&!active.contains(event.target))hide();},true);
    return {bind,hide,destroy(){hide();listeners.forEach(remove=>remove());tip.remove();}};
  }
  function bounds(values,zero=true){
    const valid=values.filter(finite).map(Number);let low=valid.length?Math.min(...valid):0,high=valid.length?Math.max(...valid):1;
    if(zero){low=Math.min(0,low);high=Math.max(0,high);}if(low===high){const pad=Math.max(.5,Math.abs(low)*.08);low-=pad;high+=pad;}
    return {low,high};
  }
  function linePath(points,smooth){
    if(!points.length)return '';let path='M'+points[0].join(',');
    if(!smooth||points.length<3)return path+points.slice(1).map(point=>'L'+point.join(',')).join('');
    for(let i=1;i<points.length;i++){const prev=points[i-1],point=points[i];path+=`Q${prev[0]},${prev[1]} ${(prev[0]+point[0])/2},${(prev[1]+point[1])/2}`;}
    return path+'L'+points.at(-1).join(',');
  }
  function render(host,data){
    const document=host.ownerDocument||root.document,language=data.language||'en',zh=language==='zh-hans';
    const hidden=host._ercHidden||new Set();host._ercHidden=hidden;host._ercTooltip?.destroy();host.replaceChildren();
    delete host._ercTooltip;
    if(data.type==='metric'){
      const metric=document.createElement('div');metric.className='erc-metric';metric.setAttribute('role','group');metric.setAttribute('translate','no');
      metric.setAttribute('aria-label',data.title||(zh?'指标':'Metric'));
      const main=document.createElement('div');main.className='erc-metric-main';
      const value=document.createElement('span');value.className='erc-metric-value';
      const formatted=typeof data.value==='number'&&Number.isFinite(data.value)?format(data.value,language):'—';
      const unit=typeof data.unit==='string'?data.unit.trim():'';
      value.textContent=formatted+(unit?(unit==='%'?'':' ')+unit:'');main.appendChild(value);
      metric.appendChild(main);
      host.appendChild(metric);return;
    }
    const tips=tooltip(host,document);host._ercTooltip=tips;
    const original=data.series||[],series=original.map((item,index)=>({...item,index})).filter(item=>!hidden.has(item.index));
    const labels=data.labels||[],kind=data.type,options=data.options||{};
    if(!labels.length||!series.some(item=>item.values.some(finite))){const p=document.createElement('p');p.className='erc-empty';p.textContent=zh?'没有可绘制的数值。':'No numeric values to plot.';host.appendChild(p);legend();return;}
    const svg=node(document,'svg',{viewBox:'0 0 560 310',role:'group','aria-label':data.title||kind});host.appendChild(svg);
    function add(tag,attrs,text){const n=node(document,tag,attrs,text);svg.appendChild(n);return n;}
    function label(item,index,value){const quality=item.quality?.[index];return [labels[index],item.label+': '+format(value,language),quality?(zh?'有效 / 记录':'Valid / records')+`: ${quality.valid} / ${quality.records}`:''].filter(Boolean).join('\n');}
    function mark(target,item,index,value){tips.bind(target,label(item,index,value));}
    if(kind==='pie'||kind==='doughnut'){
      const item=series[0],values=item.values.map(value=>finite(value)?Math.max(0,Number(value)):0),total=values.reduce((a,b)=>a+b,0);
      if(!total){add('text',{x:280,y:145,'text-anchor':'middle',class:'erc-axis'},zh?'没有正值可绘制占比。':'No positive values for a distribution.');legend();return;}
      const pieColor=index=>data.meta?.other_included&&index===labels.length-1?OTHER:COLORS[index%COLORS.length];
      const cx=280,cy=150,r=116,inner=kind==='doughnut'?64:0;let start=-Math.PI/2;
      values.forEach((value,index)=>{if(value===0)return;const angle=value/total*Math.PI*2,end=start+angle,large=angle>Math.PI?1:0;
        let path;
        if(angle>=Math.PI*2-1e-9){const circle=add('circle',{cx,cy,r,fill:pieColor(index),stroke:SURFACE,'stroke-width':2});mark(circle,item,index,value);if(inner)add('circle',{cx,cy,r:inner,fill:SURFACE,'pointer-events':'none'});start=end;return;}
        const x1=cx+r*Math.cos(start),y1=cy+r*Math.sin(start),x2=cx+r*Math.cos(end),y2=cy+r*Math.sin(end);
        if(inner){const a=cx+inner*Math.cos(end),b=cy+inner*Math.sin(end),c=cx+inner*Math.cos(start),d=cy+inner*Math.sin(start);path=`M${x1},${y1}A${r},${r} 0 ${large} 1 ${x2},${y2}L${a},${b}A${inner},${inner} 0 ${large} 0 ${c},${d}Z`;}
        else path=`M${cx},${cy}L${x1},${y1}A${r},${r} 0 ${large} 1 ${x2},${y2}Z`;
        const sector=add('path',{d:path,fill:pieColor(index),stroke:SURFACE,'stroke-width':2});mark(sector,item,index,value);start=end;
      });
      if(inner){add('text',{x:cx,y:cy-4,'text-anchor':'middle',fill:TEXT,'font-size':24,'font-weight':700},format(total,language));add('text',{x:cx,y:cy+20,'text-anchor':'middle',class:'erc-axis'},item.label);}
    }else if(kind==='bar'&&options.horizontal){
      const left=118,right=535,top=18,bottom=270,slot=(bottom-top)/Math.max(1,labels.length),barSeries=series;
      const totals=labels.flatMap((_,index)=>options.stacked?[barSeries.reduce((n,item)=>n+Math.min(0,Number(item.values[index])||0),0),barSeries.reduce((n,item)=>n+Math.max(0,Number(item.values[index])||0),0)]:barSeries.map(item=>item.values[index]));
      const range=bounds(totals),x=value=>left+(Number(value)-range.low)/(range.high-range.low)*(right-left);
      for(let i=0;i<=4;i++){const value=range.low+(range.high-range.low)*i/4,position=x(value);add('line',{x1:position,y1:top,x2:position,y2:bottom,class:'erc-grid'});add('text',{x:position,y:293,'text-anchor':'middle',class:'erc-axis'},format(value,language));}
      labels.forEach((category,index)=>{if(index%Math.max(1,Math.ceil(labels.length/12))===0)add('text',{x:left-9,y:top+(index+.5)*slot+3,'text-anchor':'end',class:'erc-axis'},short(category,19));let positive=0,negative=0;
        barSeries.forEach((item,sub)=>{const value=item.values[index];if(!finite(value))return;const width=slot*.72/(options.stacked?1:barSeries.length),base=options.stacked?(Number(value)>=0?positive:negative):0,end=base+Number(value);
          const bar=add('rect',{x:Math.min(x(base),x(end)),y:top+index*slot+slot*.14+(options.stacked?0:sub*width),width:Math.max(.5,Math.abs(x(end)-x(base))),height:Math.max(.5,width-1),rx:2,fill:COLORS[item.index%COLORS.length]});mark(bar,item,index,value);if(options.stacked){if(Number(value)>=0)positive=end;else negative=end;}});
      });
    }else{
      const hasRight=series.some(item=>item.axis==='right'),left=57,right=hasRight?502:539,top=19,bottom=247,slot=(right-left)/Math.max(1,labels.length);
      const numericX=['scatter','line','area'].includes(kind)&&Array.isArray(data.x_values)&&data.x_values.length===labels.length?bounds(data.x_values,false):null;
      const x=index=>numericX?left+(data.x_values[index]-numericX.low)/(numericX.high-numericX.low)*(right-left):left+(index+.5)*slot;
      const scales={};
      for(const axis of ['left','right']){
        const chosen=series.filter(item=>(item.axis||'left')===axis);let values=chosen.flatMap(item=>item.values);
        if(options.stacked){const bars=chosen.filter(item=>(item.type||kind)==='bar');values=values.concat(labels.flatMap((_,index)=>[bars.reduce((n,item)=>n+Math.min(0,Number(item.values[index])||0),0),bars.reduce((n,item)=>n+Math.max(0,Number(item.values[index])||0),0)]));}
        scales[axis]=bounds(values,true);
      }
      const y=(value,axis='left')=>bottom-(Number(value)-scales[axis].low)/(scales[axis].high-scales[axis].low)*(bottom-top);
      for(let i=0;i<=4;i++){
        const height=bottom-i*(bottom-top)/4;add('line',{x1:left,y1:height,x2:right,y2:height,class:'erc-grid'});
        add('text',{x:left-9,y:height+3,'text-anchor':'end',class:'erc-axis'},format(scales.left.low+(scales.left.high-scales.left.low)*i/4,language));
        if(hasRight)add('text',{x:right+8,y:height+3,'text-anchor':'start',class:'erc-axis'},format(scales.right.low+(scales.right.high-scales.right.low)*i/4,language));
      }
      if(numericX){for(let i=0;i<=4;i++){const value=numericX.low+(numericX.high-numericX.low)*i/4;add('text',{x:left+(right-left)*i/4,y:269,'text-anchor':'middle',class:'erc-axis'},format(value,language));}}
      else{const step=Math.max(1,Math.ceil(labels.length/6));labels.forEach((category,index)=>{if(index%step===0||index===labels.length-1)add('text',{x:x(index),y:269,'text-anchor':'middle',class:'erc-axis'},short(category,16));});}
      if(options.y_label)add('text',{x:left,y:301,'text-anchor':'start',class:'erc-axis'},short(options.y_label,35));
      if(hasRight&&options.right_y_label)add('text',{x:right,y:301,'text-anchor':'end',class:'erc-axis'},short(options.right_y_label,35));
      const stacked={left:{positive:labels.map(()=>0),negative:labels.map(()=>0)},right:{positive:labels.map(()=>0),negative:labels.map(()=>0)}};
      const bars=series.filter(item=>(kind==='combo'?item.type:kind)==='bar');
      for(const item of series){
        const type=kind==='combo'?item.type:kind,axis=item.axis||'left',color=COLORS[item.index%COLORS.length];
        if(type==='bar'){
          const barIndex=bars.indexOf(item),width=Math.min(38,slot*.74/(options.stacked?1:bars.length));
          item.values.forEach((value,index)=>{if(!finite(value))return;const pile=Number(value)>=0?'positive':'negative',base=options.stacked?stacked[axis][pile][index]:0,end=base+Number(value);
            const bar=add('rect',{x:x(index)-(options.stacked?width/2:bars.length*width/2-barIndex*width),y:Math.min(y(base,axis),y(end,axis)),width:Math.max(.5,width-1),height:Math.max(.5,Math.abs(y(end,axis)-y(base,axis))),rx:2,fill:color});mark(bar,item,index,value);if(options.stacked)stacked[axis][pile][index]=end;});
          continue;
        }
        let segment=[];function flush(){if(segment.length>1&&type!=='scatter'){
          const path=linePath(segment,options.smooth);if(type==='area')add('path',{d:path+`L${segment.at(-1)[0]},${y(0,axis)}L${segment[0][0]},${y(0,axis)}Z`,fill:color,'fill-opacity':'.12','pointer-events':'none'});
          add('path',{d:path,fill:'none',stroke:color,'stroke-width':2,'stroke-linecap':'round','stroke-linejoin':'round','pointer-events':'none'});}segment=[];}
        item.values.forEach((value,index)=>{if(!finite(value)){flush();return;}segment.push([x(index),y(value,axis)]);});flush();
        item.values.forEach((value,index)=>{if(!finite(value))return;add('circle',{cx:x(index),cy:y(value,axis),r:type==='scatter'?3:2.2,fill:color,'pointer-events':'none'});
          const hit=add('circle',{cx:x(index),cy:y(value,axis),r:8,fill:'transparent'});mark(hit,item,index,value);});
      }
    }
    legend();
    function legend(){
      if(options.legend===false)return;const list=document.createElement('div');list.className='erc-legend';
      const pie=kind==='pie'||kind==='doughnut',items=pie?labels.map((label,index)=>({label,index})):original.map((item,index)=>({label:item.label,index}));
      items.forEach(item=>{const entry=document.createElement(pie?'span':'button');entry.className='erc-legend-item';if(!pie){entry.type='button';entry.setAttribute('aria-pressed',String(!hidden.has(item.index)));entry.style.opacity=hidden.has(item.index)?'.45':'1';entry.addEventListener('click',()=>{if(hidden.has(item.index))hidden.delete(item.index);else hidden.add(item.index);render(host,data);});}
        const swatch=document.createElement('span');swatch.className='erc-swatch';swatch.style.background=pie&&data.meta?.other_included&&item.index===labels.length-1?OTHER:COLORS[item.index%COLORS.length];const text=document.createElement('span');text.textContent=item.label;
        if(pie)text.textContent+=' · '+format(original[0].values[item.index],language);entry.append(swatch,text);list.appendChild(entry);});host.appendChild(list);
    }
  }
  function mount(document=root.document){
    const data=document?.getElementById('engenie-chart-data');if(!data)return;
    let charts;try{charts=JSON.parse(data.textContent);}catch(_error){return;}
    if(!Array.isArray(charts))return;
    for(const host of document.querySelectorAll('[data-engenie-chart]')){const index=Number(host.getAttribute('data-engenie-chart'));if(Number.isInteger(index)&&charts[index])render(host,charts[index]);}
  }
  const api={render,mount,format,bounds,linePath};root.EnGenieReportCharts=api;
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  if(root.document){if(root.document.readyState==='loading')root.document.addEventListener('DOMContentLoaded',()=>mount());else mount();}
})(globalThis);
