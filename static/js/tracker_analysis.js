/* 业务数据参数分析：所有筛选无刷新；只绘制后端汇总，不向浏览器传全量历史。 */
const METRICS = {avg:'均值', latest:'期末有效值', sum:'合计', min:'最低值', max:'最高值'};
const NS = 'http://www.w3.org/2000/svg';

export function formatNumber(value) {
  if (value === null || value === undefined) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  if (n && (Math.abs(n) >= 1e12 || Math.abs(n) < .0001)) return n.toExponential(3);
  return new Intl.NumberFormat(globalThis.FOSI18n?.locale || 'zh-CN', {maximumFractionDigits:4}).format(n);
}

export function formatValue(value, kind, delta = false) {
  if (value === null || value === undefined) return '—';
  const n = Number(value);
  if (kind !== 'clock') return `${delta && n > 0 ? '+' : ''}${formatNumber(value)}`;
  if (!Number.isFinite(n)) return '—';
  const seconds = Math.round(Math.abs(n) * 60);
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60), rest = seconds % 60;
  if (delta) return `${n > 0 ? '+' : n < 0 ? '−' : ''}${hours ? hours+'小时 ' : ''}${minutes}分${rest ? ' '+rest+'秒' : ''}`;
  return `${String(hours).padStart(2,'0')}:${String(minutes).padStart(2,'0')}${rest ? ':'+String(rest).padStart(2,'0') : ''}`;
}

export function periodLabel(iso, grain, zone, compact = false) {
  if (!iso) return '—';
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
    timeZone:zone, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23',
  }).formatToParts(new Date(iso)).map(part => [part.type, part.value]));
  const date = `${parts.year}-${parts.month}-${parts.day}`;
  if (grain === 'year') return parts.year;
  if (grain === 'month') return `${parts.year}-${parts.month}`;
  if (grain === 'hour') return `${compact ? parts.month+'-'+parts.day : date} ${parts.hour}:00`;
  return `${compact ? parts.month+'-'+parts.day : date}${grain === 'week' ? ' 起' : ''}`;
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function svgNode(tag, attributes = {}, text) {
  const element = document.createElementNS(NS, tag);
  Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
  if (text !== undefined) element.textContent = String(text);
  return element;
}

function numericChart(data, metric, width = 924) {
  const chartWidth=Math.max(260,Math.min(900,width-24));
  const svg = svgNode('svg', {viewBox:`0 0 ${chartWidth} 250`, role:'img', 'aria-label':`${data.parameter}按采集时间的${METRICS[metric]}趋势`});
  const periods = data.periods;
  const values = periods.filter(p => p[metric] !== null && Number.isFinite(Number(p[metric]))).map(p => Number(p[metric]));
  if (!values.length) return node('p', 'ta-help', '本范围没有可绘制的有效数值。');
  const scale = Math.max(1, ...values.map(Math.abs));
  let low = Math.min(...values.map(v => v/scale)), high = Math.max(...values.map(v => v/scale));
  const pad = high === low ? Math.max(Math.abs(high)*.05,.05) : (high-low)*.12;
  low -= pad; high += pad;
  if(data.kind==='clock'){low=Math.max(0,low);high=Math.min(1440/scale,high);}
  const left=chartWidth<420 ? 60 : 80, right=chartWidth-15, top=20, bottom=210;
  const first = new Date(periods[0].start).getTime(), last = new Date(periods.at(-1).start).getTime();
  const x = p => first === last ? (left+right)/2 : left+(new Date(p.start).getTime()-first)/(last-first)*(right-left);
  const y = value => bottom-(Number(value)/scale-low)/(high-low)*(bottom-top);
  for (let i=0;i<5;i++) {
    const value = low+(high-low)*i/4, height = bottom-i*(bottom-top)/4;
    svg.append(svgNode('line',{x1:left,y1:height,x2:right,y2:height,class:'ta-grid'}));
    svg.append(svgNode('text',{x:left-12,y:height+4,'text-anchor':'end',class:'ta-axis-label'},formatValue(value*scale,data.kind)));
  }
  const indexes = [...new Set(chartWidth<420 ? [0,periods.length-1] : [0, Math.round((periods.length-1)/2), periods.length-1])];
  indexes.forEach(index => svg.append(svgNode('text',{x:x(periods[index]),y:239,
    'text-anchor':index===0 ? 'start' : index===periods.length-1 ? 'end' : 'middle',class:'ta-axis-label'},
    periodLabel(periods[index].start,data.grain,data.timezone,true))));
  let path='';
  let connected=false;
  for (const period of periods) {
    if (period[metric] === null || !Number.isFinite(Number(period[metric]))) {connected=false;continue;}
    path += `${connected ? 'L' : 'M'}${x(period).toFixed(2)},${y(period[metric]).toFixed(2)} `;
    connected=true;
  }
  svg.append(svgNode('path',{d:path,class:'ta-line'}));
  for (const period of periods) {
    if (period[metric] === null || !Number.isFinite(Number(period[metric]))) continue;
    const label = `${periodLabel(period.start,data.grain,data.timezone)} · ${METRICS[metric]} ${formatValue(period[metric],data.kind)} · ${period.valid_count} 个有效样本`;
    const dot=svgNode('circle',{cx:x(period),cy:y(period[metric]),r:3.5,class:'ta-dot',tabindex:'0','aria-label':label});
    dot.append(svgNode('title',{},label));svg.append(dot);
  }
  return svg;
}

function categoryChart(data) {
  const container=node('div','ta-distribution');
  const max=data.distribution[0]?.count || 1;
  for (const group of data.distribution) {
    const row=node('div','ta-distribution-row');
    const label=node('span','ta-distribution-label',group.value);label.title=group.value;
    const track=node('div','ta-distribution-track'),fill=node('div','ta-distribution-fill');
    fill.style.width=`${group.count/max*100}%`;track.append(fill);
    row.append(label,track,node('span','',`${formatNumber(group.count)} 次`));container.append(row);
  }
  return container;
}

function init(panel) {
  const get=id=>document.getElementById(id);
  const controls={item:get('taItem'),parameter:get('taParameter'),grain:get('taGrain'),start:get('taStart'),end:get('taEnd')};
  const status=get('taStatus'), error=get('taError'), empty=get('taEmpty'), result=get('taResult');
  const metric=get('taMetric'), search=get('taSearch');
  const initialRow=new URLSearchParams(location.search).get('item');
  let data=null, controller=null, version=0, optionsVersion=0, searchTimer=null;
  let view='table';
  let ready;
  const queryURL=params=>{const url=new URL(panel.dataset.url,location.href);Object.entries(params).forEach(([k,v])=>{if(v)url.searchParams.set(k,v);});return url;};
  async function request(url, signal) {
    const response=await fetch(url,{signal,headers:{'X-Requested-With':'XMLHttpRequest',Accept:'application/json'}});
    if (!response.ok) {
      const body=await response.json().catch(()=>null);
      throw new Error(body?.message || (response.status===404 ? '记录已删除或无权访问，请选择其他采集对象。' : `读取失败（${response.status}）`));
    }
    return response.json();
  }
  function showError(message) {error.textContent=message;error.hidden=false;result.hidden=true;empty.hidden=true;}
  function drawChart() {
    if(!data || result.hidden || view!=='chart')return;
    const chart=get('taChart');
    chart.replaceChildren(data.kind==='category' ? categoryChart(data) : numericChart(data,metric.value,chart.clientWidth));
  }
  function setView(next) {
    view=next;
    get('taChartWrap').hidden=view!=='chart';get('taTableWrap').hidden=view!=='table';
    panel.querySelectorAll('[data-ta-view]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.taView===view)));
    if(!data)return;
    const category=data.kind==='category';
    get('taChartTitle').textContent=category ? `${data.parameter} · ${view==='table' ? '周期分析' : '取值分布'}` :
      `${data.parameter} · ${METRICS[metric.value]}${view==='table' ? '分析' : '趋势'}`;
    get('taChartHint').textContent=(view==='table' ? '每行汇总一个采集周期，比较样本统计与取值变化。' :
      category ? '文本和状态按出现频次分析，展示最常见的 8 个取值。' : '按周期汇总；悬停或聚焦数据点查看数值与样本数。')+
      (data.summary.sample_count===1 ? ' 当前仅一次采集，后续采集会增加趋势点。' : '');
    drawChart();
  }
  function render() {
    if(!data)return;
    const {summary:s,kind}=data;
    if(kind==='clock' && metric.value==='sum')metric.value='avg';
    metric.querySelector('[value="sum"]').disabled=kind==='clock';
    metric.closest('label').hidden=kind==='category';
    setView(view);
    const headings=kind==='category' ? ['采集周期','有值 / 总样本','期末有效值','取值变化次数'] :
      ['采集周期','有效 / 总样本',METRICS[metric.value],'最低值','最高值','较上一有值周期'];
    const head=node('tr');headings.forEach(title=>head.append(node('th','',title)));get('taTableHead').replaceChildren(head);
    const body=get('taTableBody');body.replaceChildren();
    for(const period of [...data.periods].reverse()) {
      const tr=node('tr');tr.append(node('td','',periodLabel(period.start,data.grain,data.timezone)));
      tr.append(node('td','',`${kind==='category' ? period.present_count : period.valid_count} / ${period.count}`));
      if(kind==='category') {
        const text=node('td','',period.latest_text||'—');text.title=period.latest_text;
        tr.append(text,node('td','',formatNumber(period.changes)));
      } else {
        tr.append(node('td','',formatValue(period[metric.value],kind)),node('td','',formatValue(period.min,kind)),node('td','',formatValue(period.max,kind)));
        const comparison=period.comparison[metric.value];const cell=node('td',comparison.delta===null ? '' : 'ta-delta',formatValue(comparison.delta,kind,true));
        if(comparison.percent!==null)cell.append(node('span','ta-cell-hint',`${formatValue(comparison.percent,'number',true)}%`));
        tr.append(cell);
      }
      body.append(tr);
    }
    get('taTableHint').textContent=`共 ${data.period_count} 个有采样的周期${data.truncated_periods ? ` · 展示最近 ${data.period_limit} 个` : ''} · 最新在前`;
    const notes=['同一数据源与稳定 ID 的采集记录合并分析；时间以采集批次为准，旧记录回退到保存的采集时间。'];
    if(kind==='category') notes.push('变化次数按相邻有值采样的原始取值比较；空值不算变化。');
    else notes.push(`空值、非${kind==='clock' ? '时刻' : '数值'}原文不计入指标，共 ${s.sample_count-s.valid_count} 条。周期变化使用上一有有效值的周期；没有样本的周期不补零。`);
    if(kind==='clock')notes.push('时刻按 00:00–23:59 线性统计，不代表跨午夜时长；时长请选对应的派生参数。');
    if(data.truncated_periods)notes.push(`采样总数覆盖全部筛选样本；趋势图和分析表展示最近 ${data.period_limit} 个周期，可缩小时间范围或调大时间粒度。`);
    if(data.item.write_mode==='upsert')notes.push('当前任务使用覆盖更新，覆盖前未保存的历史无法还原；保留的历史快照仍参与分析。');
    get('taFootnote').textContent=notes.join(' ');
  }
  async function load() {
    if(!controls.item.value || !controls.parameter.value)return;
    if(controls.start.value && controls.end.value && controls.start.value>controls.end.value) {
      controller?.abort();version++;panel.removeAttribute('aria-busy');status.textContent='请调整时间范围';showError('开始时间不能晚于结束时间。');return;
    }
    const current=++version;controller?.abort();controller=new AbortController();
    panel.setAttribute('aria-busy','true');status.textContent='正在计算分析…';error.hidden=true;empty.hidden=true;
    try {
      const response=await request(queryURL({row:controls.item.value,parameter:controls.parameter.value,grain:controls.grain.value,start:controls.start.value,end:controls.end.value}),controller.signal);
      if(current!==version)return;
      data=response;get('taTimezone').textContent=`采集时区：${data.timezone} · 起止时间均包含边界`;
      status.textContent=`${data.item.title} · ${formatNumber(data.summary.sample_count)} 个采集样本`;
      result.hidden=!data.summary.sample_count;empty.hidden=!!data.summary.sample_count;
      if(data.summary.sample_count)render();
    } catch(exc) {
      if(exc.name!=='AbortError' && current===version){status.textContent='分析未完成';showError(exc.message);}
    } finally {if(current===version)panel.removeAttribute('aria-busy');}
  }
  async function loadOptions(query='',initial=false) {
    const current=++optionsVersion;
    const response=await request(queryURL({q:query}));
    if(current!==optionsVersion)return;
    if(initial) {
      controls.parameter.replaceChildren(...response.parameters.map(name=>{const option=node('option','',name);option.value=name;return option;}));
      controls.parameter.value=response.default_parameter;
    }
    const previous=controls.item.value;
    const previousOption=controls.item.selectedOptions[0]?.cloneNode(true);
    controls.item.replaceChildren(...response.items.map(item=>{
      const option=node('option','',item.title);option.value=item.row_id;return option;
    }));
    // 搜索不应替用户切换正在分析的对象；初始对象支持行内深链。
    if(initial && initialRow && /^\d+$/.test(initialRow) && !response.items.some(item=>String(item.row_id)===initialRow)) {
      const option=node('option','','指定采集对象');option.value=initialRow;controls.item.prepend(option);
    } else if(!initial && previousOption && !response.items.some(item=>String(item.row_id)===previous)) {
      controls.item.prepend(previousOption);
    }
    if(initial && initialRow)controls.item.value=initialRow;
    else if(previous)controls.item.value=previous;
    get('taSearchHint').hidden=!response.has_more;
    if(!controls.item.value || !controls.parameter.value) {
      empty.textContent=!response.parameters.length ? '还没有可分析的业务参数。请先完成采集，或在任务中保留所需参数。' : '还没有已完成的采集数据。';
      empty.hidden=false;status.textContent='暂无分析数据';
    }
  }
  get('taFilters').addEventListener('submit',event=>{event.preventDefault();void load();});
  Object.values(controls).forEach(control=>control.addEventListener('change',()=>void load()));
  get('taReset').addEventListener('click',()=>{controls.start.value='';controls.end.value='';void load();});
  metric.addEventListener('change',render);
  panel.querySelectorAll('[data-ta-view]').forEach(button=>button.addEventListener('click',()=>setView(button.dataset.taView)));
  if(typeof ResizeObserver!=='undefined') {
    let lastWidth=0;
    new ResizeObserver(entries=>{
      const width=entries[0].contentRect.width;
      if(Math.abs(width-lastWidth)>1){lastWidth=width;drawChart();}
    }).observe(get('taChart'));
  }
  search.addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{
    void loadOptions(search.value.trim()).catch(exc=>{error.textContent=exc.message;error.hidden=false;});
  },300);});
  search.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();clearTimeout(searchTimer);void loadOptions(search.value.trim()).catch(exc=>showError(exc.message));}});
  document.addEventListener('click',async event=>{
    const button=event.target.closest('.trk-analyze');if(!button)return;
    await ready;
    const id=button.dataset.rowId;
    if(![...controls.item.options].some(option=>option.value===id)) {
      const option=node('option','',button.closest('td')?.getAttribute('title')||'所选采集对象');option.value=id;controls.item.append(option);
    }
    controls.item.value=id;
    panel.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth',block:'start'});
    controls.item.focus({preventScroll:true});void load();
  });
  ready=loadOptions('',true).then(load).catch(exc=>{status.textContent='分析未完成';showError(exc.message);});
}

if(typeof document!=='undefined') {
  const panel=document.getElementById('trackerAnalysis');if(panel)init(panel);
}
