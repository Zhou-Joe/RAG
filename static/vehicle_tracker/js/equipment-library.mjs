const version = new URL(import.meta.url).search;
const [{FORMAT, FILE_LIMIT, normalizeManifest, componentOrder, openModelStore, readModels, saveModel},
  {createDocumentExplorer}] = await Promise.all(
  ['./equipment-model.mjs', './vehicle-tracker-documents.mjs'].map(path => import(path + version)));

const $ = id => document.getElementById(id);
const t = text => globalThis.FOSI18n?.t(text) || text;
const registry = JSON.parse($('equipment-registry').textContent);
const localModels = new Map();
const published = new Map();
const viewerRoutes = new Map();
let database;
let active = null;
let selectedPart = '';
let openDrawing = '';
let commit;

function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

function status(message) {
  $('eq-status').textContent = message;
  $('eq-status').hidden = !message;
}

function failure(error) {
  return error.name === 'QuotaExceededError'
    ? '本机存储空间不足，请先导出模型。'
    : error.message || '操作未完成，请重试。';
}

const explorer = createDocumentExplorer({onUpdate(code) {
  if (code === openDrawing) $('vt-drawing').innerHTML = explorer.html(code);
}, emptyMessage: '请核对图号，或在部件说明与学习笔记中记录原件信息。'});

function modelLink(source, id) {
  return '#' + new URLSearchParams({source, model: id}).toString();
}

function card(model, subtitle, link, action, builtin = false) {
  const node = element('a', '');
  node.className = 'kb-card eq-card';
  node.href = link;
  const heading = element('span', '');
  const category = element('span', builtin ? t(subtitle[0]) : subtitle[0]);
  category.setAttribute('translate', 'no');
  heading.append(category, document.createTextNode(' · ' + t(subtitle[1])));
  const name = element('h3', builtin ? t(model.name) : model.name);
  name.setAttribute('translate', 'no');
  const description = element('p', model.description ? (builtin ? t(model.description) : model.description) : t('可在模型中查看部件结构、说明与追踪记录。'));
  description.setAttribute('translate', 'no');
  node.append(heading, name, description, element('b', t(action) + ' →'));
  return node;
}

function displayCatalog() {
  const cards = $('eq-cards');
  cards.replaceChildren();
  for (const item of registry.models.filter(item => item.viewer === 'sdmt')) {
    cards.append(card(item, [item.category, '三维模型'], item.url, '打开模型', true));
  }
  for (const [source, models] of [['published', published], ['local', localModels]]) {
    for (const model of models.values()) {
      const origin = source === 'local' ? '个人模型' : '共享模型';
      cards.append(card(model, [model.category, origin],
        modelLink(source, model.id), '查看 ' + model.components.length + ' 个部件'));
    }
  }
}

function renderParts() {
  const nodes = document.createDocumentFragment();
  for (const {part, depth} of componentOrder(active.model.components)) {
    const item = element('li', '');
    const button = element('button', part.name);
    button.setAttribute('translate', 'no');
    button.type = 'button';
    button.dataset.part = part.id;
    button.style.setProperty('--indent', Math.min(depth, 6));
    button.setAttribute('aria-current', String(selectedPart === part.id));
    item.append(button);
    nodes.append(item);
  }
  $('eq-parts').replaceChildren(nodes);
}

function renderPart({keepDrawing = false} = {}) {
  const model = active.model;
  const part = model.components.find(part => part.id === selectedPart);
  $('eq-part-name').textContent = part.name;
  $('eq-part-name').setAttribute('translate', 'no');
  $('eq-part-id').textContent = '部件 ' + part.id;
  $('eq-part-description').textContent = part.description || '尚未填写部件说明。';
  $('eq-part-description').setAttribute('translate', part.description ? 'no' : 'yes');
  $('eq-drawing-row').hidden = !part.drawing;
  $('eq-drawing').textContent = part.drawing;
  const entries = model.entries.filter(item => item.component === part.id)
    .sort((a, b) => b.at.localeCompare(a.at));
  const nodes = document.createDocumentFragment();
  for (const entry of entries) {
    const row = element('li', '');
    const text = element('p', entry.text);
    text.setAttribute('translate', 'no');
    row.append(element('b', entry.kind === 'tracking' ? '追踪记录' : '学习笔记'),
      element('time', new Date(entry.at).toLocaleString(window.FOSI18n?.locale || 'zh-CN')), text);
    nodes.append(row);
  }
  if (!entries.length) nodes.append(element('li', active.source === 'local'
    ? '此部件尚无记录。可在这里记下学习要点、核对结果或维护经过。' : '创建学习副本后，可为此部件添加笔记和追踪记录。'));
  $('eq-entries').replaceChildren(nodes);
  if (!keepDrawing) {
    openDrawing = '';
    $('vt-drawing').replaceChildren();
  }
}

function route() {
  const hash = new URLSearchParams(location.hash.slice(1));
  const source = hash.get('source');
  const models = source === 'published' ? published : localModels;
  const model = models.get(hash.get('model'));
  if ($('eq-editor').open) $('eq-editor').close();
  if ($('vt-source-dialog').open) $('vt-source-dialog').close();
  $('eq-detail').hidden = !model;
  $('eq-catalog').hidden = !!model;
  if (!model) {
    active = null;
    selectedPart = '';
    displayCatalog();
    if (hash.has('model')) status('此设备模型未在本账号的目录中找到。');
    return;
  }
  if (!active || active.model.id !== model.id || active.source !== source) selectedPart = model.components[0].id;
  active = {source, model};
  $('eq-name').textContent = model.name;
  $('eq-category').textContent = model.category;
  $('eq-description').textContent = model.description;
  $('eq-description').hidden = !model.description;
  for (const id of ['eq-name', 'eq-category', 'eq-description']) $(id).setAttribute('translate', 'no');
  $('eq-copy').hidden = source !== 'published';
  $('eq-add-part').hidden = source !== 'local';
  $('eq-add-entry').hidden = source !== 'local';
  const viewer = $('eq-viewer');
  const viewerPath = source === 'published' ? viewerRoutes.get(model.id) : null;
  if (viewer) {
    viewer.hidden = !viewerPath;
    if (viewerPath) viewer.href = viewerPath;
    else viewer.removeAttribute('href');
  }
  renderParts();
  renderPart();
}

function newId() {
  // randomUUID is absent on plain HTTP intranet origins.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}

async function storeNew(model) {
  const stored = await saveModel(database, model.id, previous => {
    if (previous) throw new Error('本账号已存在相同编号的模型。请核对模型编号。');
    return model;
  });
  localModels.set(stored.id, stored);
  location.hash = modelLink('local', stored.id);
  status('模型已保存到本机。');
}

function editor(title, fields, callback) {
  const container = $('eq-fields');
  container.replaceChildren();
  for (const field of fields) {
    const label = element('label', field.label);
    const input = document.createElement(field.tag || 'input');
    input.name = field.key;
    input.required = !!field.required;
    if (field.max) input.maxLength = field.max;
    if (field.hint) input.placeholder = field.hint;
    for (const [value, text] of field.options || []) input.add(new Option(text, value));
    label.append(input);
    container.append(label);
  }
  commit = callback;
  $('eq-editor-title').textContent = title;
  $('eq-form-error').hidden = true;
  $('eq-editor').showModal();
}
$('eq-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter || $('eq-form').querySelector('[type=submit]');
  button.disabled = true;
  try {
    await commit(Object.fromEntries(new FormData($('eq-form'))));
    $('eq-editor').close();
  } catch (error) {
    $('eq-form-error').hidden = false;
    $('eq-form-error').textContent = failure(error);
  } finally {
    button.disabled = false;
  }
});
$('eq-cancel').addEventListener('click', () => $('eq-editor').close());
$('eq-back').addEventListener('click', () => { location.hash = ''; status(''); });

$('eq-create').addEventListener('click', () => editor('新建设备模型', [
  {key: 'name', label: '设备名称', required: true, max: 160},
  {key: 'category', label: '设备分类', hint: '例如：泵组、输送设备', max: 80},
  {key: 'description', label: '设备说明', tag: 'textarea', max: 8000},
], values => storeNew({
  format: FORMAT, version: 1, id: newId(), ...values,
  components: [{id: 'assembly', name: values.name, description: values.description}], entries: [],
})));

$('eq-copy').addEventListener('click', async () => {
  try { await storeNew({...active.model, id: newId()}); }
  catch (error) { status(failure(error)); }
});

async function updateLocal(id, change) {
  const saved = await saveModel(database, id, previous => {
    if (!previous) throw new Error('模型已移除，请返回设备目录。');
    change(previous);
    return previous;
  });
  localModels.set(id, saved);
  if (active?.source === 'local' && active.model.id === id) {
    active.model = saved;
    renderParts();
    renderPart();
  }
  status('已保存。');
}

$('eq-add-part').addEventListener('click', () => {
  const model = active.model;
  editor('添加部件', [
    {key: 'name', label: '部件名称', required: true, max: 160},
    {key: 'id', label: '固定编号', required: true, max: 80, hint: '例如 PUMP-01'},
    {key: 'parent', label: '上级部件', tag: 'select',
      options: model.components.map(part => [part.id, part.name + ' · ' + part.id])},
    {key: 'drawing', label: '图号', max: 40, hint: '可选，用于定位知识库原文'},
    {key: 'description', label: '部件说明', tag: 'textarea', max: 8000},
  ], async values => {
    await updateLocal(model.id, previous => previous.components.push(values));
    selectedPart = values.id.trim();
    renderParts();
    renderPart();
  });
  $('eq-fields').querySelector('[name=parent]').value = selectedPart;
});
$('eq-parts').addEventListener('click', event => {
  const button = event.target.closest('[data-part]');
  if (!button) return;
  $('eq-parts').querySelector('[aria-current=true]')?.setAttribute('aria-current', 'false');
  button.setAttribute('aria-current', 'true');
  selectedPart = button.dataset.part;
  renderPart();
});
$('eq-add-entry').addEventListener('click', () => {
  const id = active.model.id;
  const component = selectedPart;
  editor('添加部件记录', [
    {key: 'kind', label: '记录类型', tag: 'select', options: [['learning', '学习笔记'], ['tracking', '追踪记录']]},
    {key: 'text', label: '内容', tag: 'textarea', required: true, max: 8000},
  ], values => updateLocal(id, previous =>
    previous.entries.push({...values, component, at: new Date().toISOString()})));
});

$('eq-drawing').addEventListener('click', () => {
  openDrawing = $('eq-drawing').textContent;
  $('vt-drawing').innerHTML = explorer.html(openDrawing);
  explorer.load(openDrawing);
});
$('eq-import').addEventListener('click', () => $('eq-file').click());
$('eq-file').addEventListener('change', async event => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  try {
    if (file.size > FILE_LIMIT) throw new Error('模型文件不能超过 2 MB。');
    let input;
    try { input = JSON.parse((await file.text()).replace(/^\uFEFF/, '')); }
    catch { throw new Error('文件不是有效的 JSON。'); }
    await storeNew(normalizeManifest(input));
  } catch (error) { status(failure(error)); }
});
$('eq-export').addEventListener('click', () => {
  const blob = new Blob([JSON.stringify(active.model)], {type: 'application/json'});
  const link = document.createElement('a');
  link.download = active.model.id + '.equipment.json';
  link.href = URL.createObjectURL(blob);
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 10000);
});
window.addEventListener('hashchange', route);
document.addEventListener('fos:languagechange', () => {
  // Do not route again: an editor may contain unsaved changes.
  if (active) renderPart({keepDrawing: true});
  else displayCatalog();
});

for (const item of registry.models.filter(item => item.viewer === 'components')) {
  try {
    const model = normalizeManifest(item.manifest);
    published.set(model.id, model);
    if (item.viewer_url && new URL(item.viewer_url, location.origin).origin === location.origin) {
      viewerRoutes.set(model.id, item.viewer_url);
    }
  } catch (error) { status('部分共享模型未能加载。' + failure(error)); }
}
try {
  database = await openModelStore($('equipment-library').dataset.account);
  for (const model of await readModels(database)) localModels.set(model.id, normalizeManifest(model));
} catch (error) { status('本机模型存储不可用。' + failure(error)); }
route();
