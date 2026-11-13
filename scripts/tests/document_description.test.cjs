const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const template = fs.readFileSync(path.join(__dirname, '../../templates/kb/manage_detail.html'), 'utf8');
const editor = template.slice(template.indexOf('// ── 文档描述编辑'), template.indexOf('// ── 视觉文档模式切换'));
const translations = {FOS_I18N_CATALOG: {messages: {'业务数据': 'Business data', '保存': 'Save'}}, module: {exports: {}}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../static/js/interface_language.js'), 'utf8'), translations);

function setup(original, answer = config => config.value) {
  // Exercise a translated display as well as the source value, so a future editor
  // cannot accidentally start treating the translated DOM as authoritative again.
  const span = {dataset: {docDescription: original}, textContent: translations.module.exports.translate(original, 'en')};
  let onClick;
  const prompts = [], posts = [];
  const button = {dataset: {docId: '42'}, closest: () => ({querySelector: () => span})};
  vm.runInNewContext(editor, {
    document: {getElementById: () => ({addEventListener: (_event, handler) => { onClick = handler; }})},
    KB_SLUG: 'fixture', getCSRFToken: () => 'fixture', URLSearchParams,
    window: {},
    fosPrompt: async config => { prompts.push(config); return answer(config); },
    fetch: async (url, options) => {
      const description = options.body.get('description');
      posts.push({url, description});
      return {json: async () => ({ok: true, description})};
    },
  });
  return {span, prompts, posts, click: () => onClick({target: {closest: () => button}})};
}

test('unchanged description saves its original Chinese text in English mode', async () => {
  const page = setup('业务数据');
  assert.equal(page.span.textContent, 'Business data');
  await page.click();
  assert.equal(page.prompts[0].value, '业务数据');
  assert.deepEqual(page.posts, [{url: '/kb/manage/fixture/doc/42/desc/', description: '业务数据'}]);
  assert.equal(page.span.textContent, '业务数据');
});

test('saving an edited description refreshes the raw value used by the next edit', async () => {
  let first = true;
  const page = setup('业务数据', config => { if (first) { first = false; return '保存'; } return config.value; });
  await page.click();
  assert.equal(page.span.dataset.docDescription, '保存');
  page.span.textContent = 'Save';
  await page.click();
  assert.equal(page.prompts[1].value, '保存');
  assert.equal(page.posts[1].description, '保存');
});

test('an explicitly empty description never falls back to a stale translated display', async () => {
  const page = setup('');
  page.span.textContent = 'Old translated description';
  await page.click();
  assert.equal(page.prompts[0].value, '');
  assert.equal(page.posts[0].description, '');
});
