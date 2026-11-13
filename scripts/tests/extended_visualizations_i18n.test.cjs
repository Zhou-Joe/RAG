const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '../..');

function translations() {
  const catalog = {messages: {}, patterns: [], englishSource: {}};
  const directory = path.join(root, 'static/i18n');
  for (const filename of fs.readdirSync(directory).filter(name => name.endsWith('.json'))
    .sort((a,b) => (a === 'common.json') - (b === 'common.json') || a.localeCompare(b))) {
    const part = JSON.parse(fs.readFileSync(path.join(directory, filename), 'utf8'));
    Object.assign(catalog.messages, part.messages);
    catalog.patterns.push(...(part.patterns || []));
    Object.assign(catalog.englishSource, part.englishSource || {});
  }
  const scope = {FOS_I18N_CATALOG: catalog, module: {exports: {}}};
  vm.runInNewContext(fs.readFileSync(path.join(root, 'static/js/interface_language.js'), 'utf8'), scope);
  return scope.module.exports.translate;
}

test('new visualization validation errors from both actual backend builders translate in both directions', () => {
  const translate = translations();
  const errors = new Set();
  for (const filename of ['kb/tracker_visualizations.py', 'kb/report_extended_specs.py']) {
    const source = fs.readFileSync(path.join(root, filename), 'utf8');
    for (const match of source.matchAll(/raise (?:ValueError|TypeError|OverflowError)\('([^'\r\n]+)'\)/g))
      errors.add(match[1]);
  }
  assert.ok(errors.size >= 45, 'Exercise the actual builder validations, not a duplicated error list');
  for (const english of errors) {
    const chinese = translate(english, 'zh-hans');
    assert.notEqual(chinese, english, 'Missing Chinese translation: ' + english);
    assert.match(chinese, /[\u3400-\u9fff]/u);
    assert.equal(translate(chinese, 'en'), english);
    assert.equal(translate(english, 'en'), english);
  }
});

test('dynamic field and bound errors preserve literal names, punctuation and numbers', () => {
  const translate = translations();
  const source = fs.readFileSync(path.join(root, 'kb/tracker_visualizations.py'), 'utf8');
  const patterns = [...source.matchAll(/raise ValueError\(f'([^'\r\n]+)'\)/g)].map(match => match[1]);
  const field = '业务日期 <img src=x onerror="alert(1)"> & $root.Value';
  const values = {name: field, limit: '500', maximum: '1024'};
  assert.ok(patterns.length >= 5);
  for (const template of patterns) {
    const english = template.replace(/\{(name|limit|maximum)\}/g, (_, key) => values[key]);
    const chinese = translate(english, 'zh-hans');
    assert.notEqual(chinese, english, english);
    if (template.includes('{name}')) assert.ok(chinese.includes(field));
    for (const key of ['limit', 'maximum']) if (template.includes('{' + key + '}')) assert.ok(chinese.includes(values[key]));
    assert.equal(translate(chinese, 'en'), english);
  }
});

test('activity labels translate while arbitrary source fields, labels and data remain unchanged', () => {
  const translate = translations();
  assert.equal(translate('创建交互式数据视图…', 'en'), 'Create Interactive Data View…');
  assert.equal(translate('Create Interactive Data View…', 'zh-hans'), '创建交互式数据视图…');
  for (const label of ['设备 A <svg onload="alert(1)">', 'Heatmap: Custom Device Data', 'Value <script>业务数据</script>',
    'Title; Name; 0; 2026-11-13; 42%']) {
    assert.equal(translate(label, 'en'), label);
    assert.equal(translate(label, 'zh-hans'), label);
  }
});
