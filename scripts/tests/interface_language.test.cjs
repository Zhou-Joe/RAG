const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../../static/js/interface_language.js'), 'utf8');
function load(catalog = {}) {
  const scope = {FOS_I18N_CATALOG: catalog, module: {exports: {}}};
  vm.runInNewContext(source, scope, {filename: 'interface_language.js'});
  return {...scope.module.exports, scope};
}
const catalog = {
  messages: {
    '保存': 'Save',
    '取消': 'Cancel',
    '知识库': 'Knowledge base',
    '正在读取 对象…': 'Loading items…',
    '特殊值 [a.b]+ (测试)?': 'Special value [a.b]+ (test)?',
  },
  englishSource: {'Download JSON': '下载 JSON'},
  patterns: [
    {source: '删除「{name}」？', target: 'Delete “{name}”?'},
    {source: '第 {page} 页，共 {total} 页', target: 'Page {page} of {total}'},
    {source: '{name} 有 {count} 条记录', target: '{count} records for {name}'},
    {source: '比较 {id} 与 {id}', target: 'Compare {id} with itself'},
    {source: '值 [x.y]+({id})？', target: 'Value [x.y]+({id})?'},
    {source: '数据源 {name}：{status}', target: 'Source {name}: {status}', translate: ['status']},
    {source: '{count} 个', target: '{count} items'},
    {source: '{count} 题', target: '{count} questions'},
  ],
};

test('exact UI labels translate both ways without changing surrounding whitespace', () => {
  const {translate} = load(catalog);
  assert.equal(translate(' \t保存\n', 'en'), ' \tSave\n');
  assert.equal(translate('\n Save \t', 'zh-hans'), '\n 保存 \t');
  assert.equal(translate('正在读取\n  对象…', 'en'), 'Loading items…');
  assert.equal(translate('保存', 'zh-hans'), '保存');
  assert.equal(translate('Save', 'en'), 'Save');
});

test('unknown source content and null input are safe and remain unchanged', () => {
  const {translate} = load(catalog);
  for (const text of ['DOC-中国-0042', 'The Save valve is installed.', '请保存我的原文，不要更改。', '  A\t B\nC  ', '<img src=x onerror=alert(1)>']) {
    assert.equal(translate(text, 'en'), text);
    assert.equal(translate(text, 'zh-hans'), text);
  }
  assert.equal(translate(null, 'en'), '');
  assert.equal(translate(undefined, 'en'), '');
  assert.equal(translate(0, 'en'), '0');
  assert.equal(translate('\n\t ', 'en'), '\n\t ');
});

test('patterns reorder named values and reverse the same sentence', () => {
  const {translate} = load(catalog);
  assert.equal(translate('设备 AB-12 有 1,024 条记录', 'en'), '1,024 records for 设备 AB-12');
  assert.equal(translate('1,024 records for 设备 AB-12', 'zh-hans'), '设备 AB-12 有 1,024 条记录');
  assert.equal(translate(' 第 3 页，共 20 页 ', 'en'), ' Page 3 of 20 ');
  assert.equal(translate('Page 3 of 20', 'zh-hans'), '第 3 页，共 20 页');
});

test('dynamic identifiers that resemble UI labels are preserved as data', () => {
  const {translate} = load(catalog);
  assert.equal(translate('删除「保存」？', 'en'), 'Delete “保存”?');
  assert.equal(translate('Delete “Save”?', 'zh-hans'), '删除「Save」？');
  assert.equal(translate('知识库 有 3 条记录', 'en'), '3 records for 知识库');
});

test('dynamic values retain their internal whitespace and punctuation', () => {
  const {translate} = load(catalog);
  assert.equal(translate('删除「AB  12\t中文\n报告」？', 'en'), 'Delete “AB  12\t中文\n报告”?');
  assert.equal(translate('Delete “AB  12\t中文\n报告”?', 'zh-hans'), '删除「AB  12\t中文\n报告」？');
});

test('only explicitly designated system-status values may be translated', () => {
  const {translate} = load(catalog);
  assert.equal(translate('数据源 保存：取消', 'en'), 'Source 保存: Cancel');
  assert.equal(translate('Source Save: Cancel', 'zh-hans'), '数据源 Save：取消');
});

test('HTML and replacement metacharacters in dynamic values stay literal text', () => {
  const {translate, scope} = load(catalog);
  const value = '<img src=x onerror="globalThis.attack=1"> $& $1 {name}';
  assert.equal(translate('删除「' + value + '」？', 'en'), 'Delete “' + value + '”?');
  assert.equal(scope.attack, undefined);
});

test('literal punctuation in patterns cannot inject a regular expression', () => {
  const {translate, compile} = load(catalog);
  const rule = compile('值 [x.y]+({id})？', 'Value [x.y]+({id})?');
  assert.ok(rule.regex.test('值 [x.y]+(AB-12)？'));
  assert.equal(rule.regex.test('值 xayyyyyAB-12？'), false);
  assert.equal(rule.regex.test('prefix 值 [x.y]+(AB-12)？'), false);
  assert.equal(rule.regex.test('值 [x.y]+(AB-12)？ suffix'), false);
  assert.equal(translate('值 [x.y]+(AB-12)？', 'en'), 'Value [x.y]+(AB-12)?');
  assert.equal(translate('值 xayyyyyAB-12？', 'en'), '值 xayyyyyAB-12？');
});

test('exact matches take precedence and patterns require a complete sentence', () => {
  const {translate} = load({...catalog, messages: {...catalog.messages, '删除「default」？': 'Delete the default configuration?'}});
  assert.equal(translate('删除「default」？', 'en'), 'Delete the default configuration?');
  assert.equal(translate('提示：删除「AB-12」？后继续', 'en'), '提示：删除「AB-12」？后继续');
  assert.equal(translate('第 3 页，共 20 页；还没结束', 'en'), '第 3 页，共 20 页；还没结束');
});

test('a repeated placeholder must have the same value everywhere', () => {
  const {translate} = load(catalog);
  assert.equal(translate('比较 AB-12 与 AB-12', 'en'), 'Compare AB-12 with itself');
  assert.equal(translate('比较 AB-12 与 AB-13', 'en'), '比较 AB-12 与 AB-13');
});

test('empty and almost entirely wildcard patterns are rejected', () => {
  const {compile} = load(catalog);
  assert.equal(compile('{value}', '{value}'), null);
  assert.equal(compile(' {value} ', '{value}'), null);
  assert.equal(compile('{value}?', '{value}?'), null);
  assert.equal(compile('保存', 'Save'), null);
});

test('single-character count labels accept only explicit numeric fields', () => {
  const {translate, compile} = load(catalog);
  assert.ok(compile('{count} 个', '{count} items'));
  assert.equal(compile('{name} 个', '{name} items'), null);
  assert.equal(translate('0 个', 'en'), '0 items');
  assert.equal(translate('1,234 个', 'en'), '1,234 items');
  assert.equal(translate('14 题', 'en'), '14 questions');
  assert.equal(translate('1,234 items', 'zh-hans'), '1,234 个');
  assert.equal(translate('14 questions', 'zh-hans'), '14 题');
  assert.equal(translate('保存 个', 'en'), '保存 个');
  assert.equal(translate('.* 个', 'en'), '.* 个');
  assert.equal(translate('Save items', 'zh-hans'), 'Save items');
});

test('English-authored UI labels also switch to Chinese and back', () => {
  const {translate} = load(catalog);
  assert.equal(translate('Download JSON', 'zh-hans'), '下载 JSON');
  assert.equal(translate('下载 JSON', 'en'), 'Download JSON');
});

test('catalog lookups do not use inherited object properties', () => {
  const {translate} = load(catalog);
  for (const value of ['__proto__', 'constructor', 'toString', 'hasOwnProperty']) {
    assert.equal(translate(value, 'en'), value);
    assert.equal(translate(value, 'zh-hans'), value);
  }
});

test('the exported API works without a browser DOM', () => {
  const {api, scope} = load(catalog);
  assert.equal(api.language, 'zh-hans');
  assert.equal(api.locale, 'zh-CN');
  assert.equal(api.t('Save'), '保存');
  assert.equal(api.t('保存', 'en'), 'Save');
  assert.equal(scope.FOSI18n, api);
});
