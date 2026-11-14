/* Expandable cards around sanitized chat tables and generated images. */
(function(root) {
  'use strict';
  const states = new WeakMap(), active = new Set();
  let sequence = 0;
  const wording = language => language === 'zh-hans'
    ? {table: '表格', image: '图片', expand: '展开', collapse: '收起', expandTable: '展开表格', collapseTable: '收起表格', expandImage: '展开图片', collapseImage: '收起图片'}
    : {table: 'Table', image: 'Image', expand: 'Expand', collapse: 'Collapse', expandTable: 'Expand Table', collapseTable: 'Collapse Table', expandImage: 'Expand Image', collapseImage: 'Collapse Image'};
  const languageOf = value => value === 'zh-hans' ? 'zh-hans' : 'en';

  function update(entry, language) {
    const labels = wording(language), expanded = entry.state.expanded.get(entry.key) === true;
    entry.card.classList.toggle('is-expanded', expanded);
    entry.button.setAttribute('aria-expanded', String(expanded));
    const control = entry.kind === 'image'
      ? (expanded ? labels.collapseImage : labels.expandImage)
      : (expanded ? labels.collapseTable : labels.expandTable);
    entry.button.setAttribute('aria-label', control);
    entry.button.title = control;
    entry.button.textContent = expanded ? labels.collapse : labels.expand;
    entry.title.textContent = entry.caption || labels[entry.kind];
  }

  function unlisten(entry) {
    entry.button.removeEventListener('click', entry.buttonClick);
    entry.header.removeEventListener('click', entry.headerClick);
    if (entry.image) entry.image.removeEventListener('click', entry.imageClick);
  }

  function stateFor(container, options) {
    let state = states.get(container);
    if (!state) {
      state = {container, language: 'en', expanded: new Map(), cards: []};
      states.set(container, state); active.add(state);
    }
    state.language = languageOf(options.language || root.FOSI18n?.language || state.language);
    return state;
  }

  function wrap(state, content, caption, key, kind, image) {
    if (!state.expanded.has(key)) state.expanded.set(key, kind === 'table');
    const document = state.container.ownerDocument || root.document;
    const card = document.createElement('figure');
    card.className = 'chat-table-card notranslate' + (kind === 'image' ? ' chat-image-card' : '');
    card.setAttribute('translate', 'no');
    const header = document.createElement('div'); header.className = 'chat-table-head';
    const title = document.createElement('span'); title.className = 'chat-table-title';
    if (caption) title.title = caption;
    const button = document.createElement('button'); button.type = 'button'; button.className = 'chat-table-toggle';
    const viewport = document.createElement('div'); viewport.className = 'chat-table-viewport';
    viewport.id = 'engenie-chat-' + kind + '-' + (++sequence);
    button.setAttribute('aria-controls', viewport.id);
    const entry = {state, content, card, header, title, button, viewport, caption, key, kind, image};
    const toggle = () => { state.expanded.set(key, state.expanded.get(key) !== true); update(entry, state.language); };
    entry.buttonClick = event => { event.stopPropagation(); toggle(); };
    entry.headerClick = () => toggle();
    button.addEventListener('click', entry.buttonClick); header.addEventListener('click', entry.headerClick);
    if (image) {
      entry.imageClick = event => { event.stopPropagation(); toggle(); };
      image.addEventListener('click', entry.imageClick);
    }
    header.appendChild(title); header.appendChild(button); card.appendChild(header); card.appendChild(viewport);
    content.parentNode.insertBefore(card, content); viewport.appendChild(content);
    update(entry, state.language);
    return entry;
  }

  function mount(container, options = {}) {
    if (!container?.querySelectorAll) return 0;
    const state = stateFor(container, options);
    const old = new Map(state.cards.filter(entry => entry.kind === 'table').map(entry => [entry.content, entry])), cards = [];
    let index = 0;
    for (const table of container.querySelectorAll('table')) {
      // A nested table is part of its parent table's source content.
      if (table.parentNode?.closest?.('table')) continue;
      const position = index++, existing = old.get(table);
      if (existing) { old.delete(table); update(existing, state.language); cards.push(existing); continue; }
      if (table.closest?.('.chat-table-card')) continue;
      const caption = String(table.querySelector('caption')?.textContent || '').trim();
      cards.push(wrap(state, table, caption, 'table:' + position + ':' + caption, 'table'));
    }
    for (const entry of old.values()) unlisten(entry);
    state.cards = state.cards.filter(entry => entry.kind !== 'table').concat(cards);
    return cards.length;
  }

  function mountImages(container, options = {}) {
    if (!container?.querySelectorAll) return 0;
    const state = stateFor(container, options);
    const old = new Map(state.cards.filter(entry => entry.kind === 'image').map(entry => [entry.card, entry])), cards = [];
    let index = 0;
    for (const group of container.querySelectorAll('.chat-chart-images')) {
      for (const content of [...group.children]) {
        const existing = old.get(content);
        if (existing) { old.delete(content); update(existing, state.language); cards.push(existing); index++; continue; }
        if (!['FIGURE', 'IMG'].includes(content.tagName?.toUpperCase()) || content.closest?.('.chat-table-card')) continue;
        const image = content.tagName.toUpperCase() === 'IMG' ? content : content.querySelector('img');
        if (!image) continue;
        const caption = String(image.alt || image.getAttribute('alt') || '').trim();
        cards.push(wrap(state, content, caption, 'image:' + (index++) + ':' + caption, 'image', image));
      }
    }
    for (const entry of old.values()) unlisten(entry);
    state.cards = state.cards.filter(entry => entry.kind !== 'image').concat(cards);
    return cards.length;
  }

  function destroy(container) {
    for (const state of [...active]) {
      if (state.container !== container && !container?.contains?.(state.container)) continue;
      for (const entry of state.cards) {
        unlisten(entry);
        if (entry.card.parentNode && entry.card.contains(entry.content)) {
          entry.card.parentNode.insertBefore(entry.content, entry.card); entry.card.remove();
        }
      }
      states.delete(state.container); active.delete(state);
    }
  }

  root.document?.addEventListener('fos:languagechange', event => {
    if (!['en', 'zh-hans'].includes(event.detail?.language)) return;
    for (const state of [...active]) {
      if (state.container.isConnected === false) { destroy(state.container); continue; }
      state.language = event.detail.language;
      for (const entry of state.cards) update(entry, state.language);
    }
  });
  const api = {mount, mountImages, destroy};
  root.EnGenieChatTableCards = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
