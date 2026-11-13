const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const moduleSource=fs.readFileSync(path.join(__dirname,'../../kb/report_presentation.py'),'utf8');
const script=moduleSource.match(/_TABLE_SCRIPT = '''([\s\S]*?)'''/)[1];

function eventNode(extra={}){
  const listeners=new Map();
  return {...extra,addEventListener(name,handler){listeners.set(name,handler)},emit(name){listeners.get(name)?.({target:this})}};
}
function setup(values){
  const rows=values.map(([name,value])=>({hidden:false,cells:[{textContent:name},{textContent:value}],get textContent(){return this.cells.map(cell=>cell.textContent).join(' ')}}));
  const body={rows:rows.slice(),appendChild(row){this.rows.splice(this.rows.indexOf(row),1);this.rows.push(row)}};
  const headers=[{},{}].map(()=>({attributes:{},setAttribute(name,value){this.attributes[name]=value},removeAttribute(name){delete this.attributes[name]}}));
  const buttons=headers.map((header,index)=>eventNode({dataset:{column:String(index)},parentNode:header}));
  const table={tBodies:[body],tHead:{querySelectorAll(selector){return selector==='th'?headers:buttons}}};
  const nodes={
    'report-table':table,'report-search':eventNode({value:''}),
    'report-table-status':{dataset:{template:'{matched} of {total} rows'}},
    'report-page-label':{},'report-prev':eventNode(),'report-next':eventNode(),'report-print':eventNode(),
  };
  const ready=new Set();let prints=0;
  vm.runInNewContext(script,{document:{documentElement:{classList:{add:value=>ready.add(value)}},getElementById:id=>nodes[id]},
    window:{print(){prints++}},fetch(){throw Error('An offline table must not make requests')}});
  return {rows,body,headers,buttons,nodes,ready,prints:()=>prints};
}

test('offline table paginates full retained rows and resets pagination for search',()=>{
  const h=setup(Array.from({length:31},(_,index)=>['Item '+index,String(index)]));
  assert.equal(h.rows.filter(row=>!row.hidden).length,25);
  assert.equal(h.nodes['report-table-status'].textContent,'31 of 31 rows');
  h.nodes['report-next'].emit('click');
  assert.equal(h.rows.filter(row=>!row.hidden).length,6);
  assert.equal(h.nodes['report-page-label'].textContent,'2 / 2');
  h.nodes['report-search'].value='Item 30';h.nodes['report-search'].emit('input');
  assert.equal(h.rows.filter(row=>!row.hidden).length,1);
  assert.equal(h.nodes['report-page-label'].textContent,'1 / 1');
  assert.equal(h.nodes['report-prev'].disabled,true);
  assert.equal(h.nodes['report-next'].disabled,true);
});

test('column sorting preserves zero and uses natural numerical ordering both directions',()=>{
  const h=setup([['A','12'],['B','0'],['C','5']]);
  h.buttons[1].emit('click');
  assert.deepEqual(h.body.rows.map(row=>row.cells[1].textContent),['0','5','12']);
  assert.equal(h.headers[1].attributes['aria-sort'],'ascending');
  h.buttons[1].emit('click');
  assert.deepEqual(h.body.rows.map(row=>row.cells[1].textContent),['12','5','0']);
  assert.equal(h.headers[1].attributes['aria-sort'],'descending');
});

test('search treats punctuation and markup as literal data and printing is explicit',()=>{
  const h=setup([['<img src=x>','1'],['[draft]','2'],['普通数据','0']]);
  h.nodes['report-search'].value='[draft]';h.nodes['report-search'].emit('input');
  assert.deepEqual(h.rows.filter(row=>!row.hidden).map(row=>row.cells[0].textContent),['[draft]']);
  assert.equal(h.prints(),0);
  h.nodes['report-print'].emit('click');
  assert.equal(h.prints(),1);
  assert.equal(h.ready.has('report-ready'),true);
});
