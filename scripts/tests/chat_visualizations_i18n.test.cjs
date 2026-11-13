const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.join(__dirname,'../..');

function shippedTranslations(){
  const catalog={messages:{},patterns:[],englishSource:{}};
  const directory=path.join(root,'static/i18n');
  const files=fs.readdirSync(directory).filter(name=>name.endsWith('.json'))
    .sort((a,b)=>(a==='common.json')-(b==='common.json')||a.localeCompare(b));
  for(const filename of files){
    const part=JSON.parse(fs.readFileSync(path.join(directory,filename),'utf8'));
    Object.assign(catalog.messages,part.messages);
    catalog.patterns.push(...(part.patterns||[]));
    Object.assign(catalog.englishSource,part.englishSource||{});
  }
  const scope={FOS_I18N_CATALOG:catalog,module:{exports:{}}};
  vm.runInNewContext(fs.readFileSync(path.join(root,'static/js/interface_language.js'),'utf8'),scope);
  return scope.module.exports.translate;
}

test('every tracker chat chart validation error translates through the shipped catalog in both directions',()=>{
  const backend=fs.readFileSync(path.join(root,'kb/analysis_exports.py'),'utf8');
  const builder=backend.slice(backend.indexOf('def interactive_chart('),backend.indexOf('\ndef excel('));
  const errors=[...builder.matchAll(/raise ValueError\('([^'\n]+)'\)/g)].map(match=>match[1]);
  assert.ok(errors.length>=14,'expected the actual interactive chart validation branches');
  const translate=shippedTranslations();
  for(const english of errors){
    const chinese=translate(english,'zh-hans');
    assert.notEqual(chinese,english,`missing Chinese translation for ${english}`);
    assert.match(chinese,/[\u3400-\u9fff]/u);
    assert.equal(translate(chinese,'en'),english);
    assert.equal(translate(english,'en'),english);
  }
});

test('adding chart errors does not rewrite arbitrary source labels or chart data as interface text',()=>{
  const translate=shippedTranslations();
  for(const source of ['Ride A <img src=x onerror=alert(1)> 设备', 'Value <script>中文数据</script>', '自定义图表系列 ABC-12']){
    assert.equal(translate(source,'zh-hans'),source);
    assert.equal(translate(source,'en'),source);
  }
});
