const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const helpers=import('data:text/javascript;base64,'+fs.readFileSync(path.join(__dirname,'../../static/js/tracker_analysis.js')).toString('base64'));

test('zero, missing and large numbers stay distinct in metrics',async()=>{
  const {formatNumber,formatValue}=await helpers;
  assert.equal(formatNumber(null),'—');assert.equal(formatNumber(0),'0');
  assert.equal(formatNumber('1.25'),'1.25');assert.equal(formatValue(3,'number',true),'+3');
  assert.equal(formatValue(-3,'number',true),'-3');assert.equal(formatNumber(1e100),'1.000e+100');
});
test('clock parameters display time and deltas display elapsed differences',async()=>{
  const {formatValue}=await helpers;
  assert.equal(formatValue(570,'clock'),'09:30');assert.equal(formatValue(0,'clock'),'00:00');
  assert.equal(formatValue(585.5,'clock'),'09:45:30');assert.equal(formatValue(-30,'clock',true),'−30分');
  assert.equal(formatValue(90,'clock',true),'+1小时 30分');assert.equal(formatValue(null,'clock'),'—');
});
test('period labels use the task timezone rather than the browser timezone',async()=>{
  const {periodLabel}=await helpers;
  assert.equal(periodLabel('2026-01-01T16:00:00Z','day','Asia/Shanghai'),'2026-01-02');
  assert.equal(periodLabel('2026-01-01T16:00:00Z','hour','Asia/Shanghai'),'2026-01-02 00:00');
  assert.equal(periodLabel('2026-01-01T16:00:00Z','month','Asia/Shanghai'),'2026-01');
  assert.equal(periodLabel('2026-01-01T16:00:00Z','year','Asia/Shanghai'),'2026');
});

class ChartNode {
  constructor(tag) { this.tag = tag; this.attributes = {}; this.children = []; this.textContent = ''; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  append(...children) { this.children.push(...children); }
}

function chartData(kind='number') {
  return {parameter:'Pressure <sensor>',kind,grain:'day',timezone:'Asia/Shanghai',periods:[
    {start:'2026-01-01T00:00:00Z',avg:0,count:3,valid_count:2},
    {start:'2026-01-02T00:00:00Z',avg:null,count:1,valid_count:0},
    {start:'2026-01-03T00:00:00Z',avg:570,count:4,valid_count:4},
  ]};
}

test('legacy numeric chart binds hover data to generous targets around small points and preserves gaps',async()=>{
  const {numericChart}=await helpers;
  const previous={document:globalThis.document,tooltip:globalThis.FosChartTooltip,i18n:globalThis.FOSI18n};
  const bindings=[],host={},controller={bind(target,lines,options){bindings.push({target,lines,options});}};
  try {
    globalThis.document={createElement:tag=>new ChartNode(tag),createElementNS:(_namespace,tag)=>new ChartNode(tag)};
    globalThis.FosChartTooltip={create(parent){assert.equal(parent,host);return controller;}};
    const svg=numericChart(chartData(),'avg',360,host);
    assert.equal(host._fosChartTooltip,controller);
    assert.equal(svg.attributes.role,'group');
    const dots=svg.children.filter(child=>child.attributes.class==='ta-dot');
    const hits=svg.children.filter(child=>child.attributes.class==='ta-point-hit');
    assert.equal(dots.length,2);assert.equal(hits.length,2);
    assert.ok(dots.every(dot=>Number(dot.attributes.r)===2&&dot.attributes['aria-hidden']==='true'));
    assert.ok(hits.every(hit=>Number(hit.attributes.r)===9&&hit.attributes.tabindex==='0'));
    assert.equal(bindings[0].target,hits[0]);assert.equal(bindings[0].options.marker,dots[0]);
    assert.equal(bindings[0].lines()[0],'Pressure <sensor>');
    assert.ok(bindings[0].lines().some(line=>line.includes(': 0')));
    assert.ok(bindings[0].lines().some(line=>line.includes('2 / 3')));
    const line=svg.children.find(child=>child.tag==='path');
    assert.equal((line.attributes.d.match(/M/g)||[]).length,2);
    assert.equal((line.attributes.d.match(/L/g)||[]).length,0);
    globalThis.FOSI18n={t:value=>({'均值':'Average','有效 / 总样本':'Valid / Total Samples'}[value]||value)};
    assert.equal(bindings[0].lines()[2],'Average: 0');
    assert.equal(bindings[0].lines()[3],'Valid / Total Samples: 2 / 3');
  } finally {
    globalThis.document=previous.document;globalThis.FosChartTooltip=previous.tooltip;globalThis.FOSI18n=previous.i18n;
  }
});

test('legacy chart hover values retain clock formatting and missing charts create no tooltip',async()=>{
  const {numericChart}=await helpers;
  const previous={document:globalThis.document,tooltip:globalThis.FosChartTooltip};
  const bindings=[];let created=0;
  try {
    globalThis.document={createElement:tag=>new ChartNode(tag),createElementNS:(_namespace,tag)=>new ChartNode(tag)};
    globalThis.FosChartTooltip={create(){created++;return {bind(_target,lines){bindings.push(lines);}};}};
    numericChart(chartData('clock'),'avg',900,{});
    assert.equal(created,1);
    assert.ok(bindings[1]().some(line=>line.includes('09:30')));
    const empty=chartData();empty.periods.forEach(period=>{period.avg=null;});
    assert.equal(numericChart(empty,'avg',900,{}).tag,'p');
    assert.equal(created,1);
  } finally {
    globalThis.document=previous.document;globalThis.FosChartTooltip=previous.tooltip;
  }
});
