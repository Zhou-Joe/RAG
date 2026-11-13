(function (root) {
  'use strict';
  const MAX_FILES = 5, MAX_FILE_SIZE = 10 * 1024 * 1024, MAX_TOTAL_SIZE = 25 * 1024 * 1024;
  const EXTENSIONS = new Set(['jpg', 'jpeg', 'png', 'webp', 'pdf', 'docx', 'txt', 'md', 'csv', 'tsv', 'xlsx', 'xlsm']);
  const IMAGE_EXTENSIONS = new Set(['jpg', 'jpeg', 'png', 'webp']);
  const FALLBACK_MESSAGE = 'Please review the attached files.';

  function extension(file) { return String(file.name || '').split('.').pop().toLowerCase(); }
  function fileKey(file) { return [file.name, file.size, file.lastModified || 0, file.type || ''].join('\u0000'); }
  function sizeLabel(size) { return size >= 1024 * 1024 ? (size / (1024 * 1024)).toFixed(1) + ' MB' : Math.max(1, Math.ceil(size / 1024)) + ' KB'; }

  function createQueue({urlAPI = root.URL, onChange = () => {}, onError = () => {}} = {}) {
    let entries = [], serial = 0, activeUpload = null;
    function revoke(entry) { if (entry.previewURL) urlAPI.revokeObjectURL(entry.previewURL); }
    function changed() { onChange(entries.slice(), !!activeUpload); }
    function add(files) {
      if (activeUpload) return false;
      let added = false;
      for (const file of Array.from(files || [])) {
        if (!EXTENSIONS.has(extension(file))) { onError('不支持此文件类型。请选择图片、PDF、Word、文本或表格。'); continue; }
        if (!Number.isFinite(file.size) || file.size < 0 || file.size > MAX_FILE_SIZE) { onError('每个附件不能超过 10 MB。'); continue; }
        if (entries.some(entry => entry.key === fileKey(file))) continue;
        if (entries.length >= MAX_FILES) { onError('每次最多添加 5 个附件。'); continue; }
        if (entries.reduce((total, entry) => total + entry.size, 0) + file.size > MAX_TOTAL_SIZE) { onError('附件总大小不能超过 25 MB。'); continue; }
        const kind = IMAGE_EXTENSIONS.has(extension(file)) ? 'image' : 'document';
        entries.push({id: 'pending-' + (++serial), key: fileKey(file), file, name: file.name, size: file.size,
          mime_type: file.type || '', kind, previewURL: kind === 'image' ? urlAPI.createObjectURL(file) : ''});
        added = true;
      }
      if (added) changed();
      return added;
    }
    function remove(id) {
      if (activeUpload) return false;
      const entry = entries.find(item => item.id === id);
      if (!entry) return false;
      revoke(entry); entries = entries.filter(item => item !== entry); changed(); return true;
    }
    function clear() { entries.forEach(revoke); entries = []; activeUpload = null; changed(); }
    function beginUpload() {
      if (activeUpload) return null;
      const batch = {entries: entries.slice(), accepted: false};
      if (batch.entries.length) { activeUpload = batch; changed(); }
      return batch;
    }
    function acceptUpload(batch) {
      if (!batch || batch !== activeUpload) return false;
      batch.accepted = true;
      const ids = new Set(batch.entries.map(entry => entry.id));
      entries.filter(entry => ids.has(entry.id)).forEach(revoke);
      entries = entries.filter(entry => !ids.has(entry.id));
      activeUpload = null; changed(); return true;
    }
    function finishUpload(batch) { if (batch === activeUpload) { activeUpload = null; changed(); } }
    return {add, remove, clear, beginUpload, acceptUpload, finishUpload,
      items: () => entries.slice(), hasFiles: () => entries.length > 0, isUploading: () => !!activeUpload};
  }

  function buildBody(message, threadId, batch, FormDataType = root.FormData) {
    const body = new FormDataType();
    body.append('message', message || FALLBACK_MESSAGE);
    body.append('thread_id', threadId);
    for (const entry of batch?.entries || []) body.append('files', entry.file, entry.name);
    return body;
  }

  function protectedURL(value) {
    const url = String(value || '');
    return /^\/kb\/chat\/attachments\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/(?:\?[^#]*)?$/i.test(url) ? url : '';
  }

  function renderMessage(container, attachments, {pending = false, document = root.document} = {}) {
    container.querySelector('.chat-message-attachments')?.remove();
    if (!attachments?.length) return;
    const list = document.createElement('div'); list.className = 'chat-message-attachments';
    for (const attachment of attachments) {
      const url = pending ? attachment.previewURL || '' : protectedURL(attachment.url);
      const item = document.createElement(url ? 'a' : 'span'); item.className = 'chat-message-attachment';
      if (url) { item.href = url; item.target = '_blank'; item.rel = 'noopener'; }
      if (attachment.kind === 'image' && url) {
        const image = document.createElement('img'); image.src = url; image.alt = attachment.name || ''; image.loading = 'lazy';
        item.appendChild(image); item.classList.add('is-image');
      }
      const name = document.createElement('span'); name.className = 'chat-attachment-name'; name.textContent = attachment.name || '';
      name.setAttribute('translate', 'no'); item.appendChild(name);
      const size = document.createElement('small'); size.textContent = sizeLabel(Number(attachment.size) || 0); item.appendChild(size);
      list.appendChild(item);
    }
    container.appendChild(list);
  }

  function create({input, picker, button, pending, error, dropZone, document = root.document, urlAPI = root.URL,
    translate = value => root.FOSI18n?.t(value) || value} = {}) {
    function showError(message) { error.textContent = translate(message); error.hidden = !message; }
    const queue = createQueue({urlAPI, onError: showError, onChange(entries, uploading) {
      pending.replaceChildren(); pending.hidden = !entries.length;
      button.disabled = uploading; picker.disabled = uploading;
      button.setAttribute('aria-busy', String(uploading));
      for (const entry of entries) {
        const chip = document.createElement('div'); chip.className = 'chat-attachment-chip';
        if (entry.previewURL) {
          const image = document.createElement('img'); image.src = entry.previewURL; image.alt = ''; chip.appendChild(image);
        } else {
          const icon = document.createElement('span'); icon.className = 'chat-attachment-file-icon'; icon.textContent = '↥'; icon.setAttribute('aria-hidden', 'true'); chip.appendChild(icon);
        }
        const info = document.createElement('span'); info.className = 'chat-attachment-info';
        const name = document.createElement('span'); name.className = 'chat-attachment-name'; name.textContent = entry.name; name.setAttribute('translate', 'no'); name.title = entry.name;
        const size = document.createElement('small'); size.textContent = sizeLabel(entry.size);
        info.appendChild(name); info.appendChild(size); chip.appendChild(info);
        const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'chat-attachment-remove';
        remove.textContent = '×'; remove.disabled = uploading; remove.title = translate('移除附件'); remove.setAttribute('aria-label', translate('移除附件'));
        remove.addEventListener('click', () => queue.remove(entry.id)); chip.appendChild(remove); pending.appendChild(chip);
      }
    }});
    function add(files) { showError(''); return queue.add(files); }
    button.addEventListener('click', () => { if (!queue.isUploading()) picker.click(); });
    picker.addEventListener('change', () => { add(picker.files); picker.value = ''; });
    input.addEventListener('paste', event => {
      const files = Array.from(event.clipboardData?.files || []);
      if (!files.length) return;
      event.preventDefault(); add(files);
    });
    let dragDepth = 0;
    function hasFiles(event) { return Array.from(event.dataTransfer?.types || []).includes('Files'); }
    dropZone.addEventListener('dragenter', event => {
      if (!hasFiles(event)) return; event.preventDefault(); dragDepth++; dropZone.classList.add('is-file-drop');
    });
    dropZone.addEventListener('dragover', event => { if (hasFiles(event)) event.preventDefault(); });
    dropZone.addEventListener('dragleave', event => {
      if (dragDepth) { event.preventDefault(); if (--dragDepth === 0) dropZone.classList.remove('is-file-drop'); }
    });
    dropZone.addEventListener('drop', event => {
      if (!hasFiles(event) && !event.dataTransfer?.files?.length) return;
      event.preventDefault(); dragDepth = 0; dropZone.classList.remove('is-file-drop'); add(event.dataTransfer.files);
    });
    return {...queue, add, showError, clear() { queue.clear(); showError(''); dragDepth = 0; dropZone.classList.remove('is-file-drop'); },
      renderMessage(container, attachments, options) { renderMessage(container, attachments, {...options, document}); }};
  }

  const api = {createQueue, create, buildBody, renderMessage, protectedURL, FALLBACK_MESSAGE, MAX_FILES, MAX_FILE_SIZE, MAX_TOTAL_SIZE};
  root.FosChatAttachments = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
})(globalThis);
