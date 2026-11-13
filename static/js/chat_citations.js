/* Numbered source references for sanitized EnGenie answers. */
(function(root) {
  'use strict';
  const states = new WeakMap(), active = new Set(), messageIds = new WeakMap();
  let serial = 0;
  const words = language => language === 'zh-hans' ? {
    sources: '来源', tracker: '追踪表', document: '文档', graph: '知识图谱',
    open: '查看来源', evidence: '查看证据', image: '来源图片', queries: '次查询',
    source: '来源', pages: '页码', reference: '查看来源',
    images: '知识库图片', expand: '展开', collapse: '收起', expandImage: '展开图片',
    collapseImage: '收起图片', original: '查看原图', unavailable: '图片暂不可用',
  } : {
    sources: 'Sources', tracker: 'Tracker', document: 'Document', graph: 'Knowledge Graph',
    open: 'Open Source', evidence: 'View Evidence', image: 'Source Image', queries: 'queries',
    source: 'Source', pages: 'Pages', reference: 'View Source',
    images: 'Knowledge Base Images', expand: 'Expand', collapse: 'Collapse', expandImage: 'Expand Image',
    collapseImage: 'Collapse Image', original: 'Open Original', unavailable: 'Image Unavailable',
  };
  const string = value => typeof value === 'string' ? value : value === null || value === undefined ? '' : String(value);
  const validNumber = value => Number.isSafeInteger(value) && value > 0 && value <= 9999;
  function sourceKey(citation) {
    if (!citation || typeof citation !== 'object') return '';
    if (citation.kind === 'tracker') return citation.kb_slug ? 'tracker:' + string(citation.kb_slug) : '';
    if (citation.kind === 'graph') return citation.relation_id && citation.fact_digest
      ? 'graph:' + string(citation.relation_id) + ':' + string(citation.fact_digest) : '';
    if (citation.doc_id) return 'document:' + string(citation.doc_id);
    return '';
  }
  function grouped(citations) {
    const result = [], byKey = new Map(), occupied = new Set(), assignments = new Map();
    for (const citation of Array.isArray(citations) ? citations : []) {
      const key = sourceKey(citation); if (!key) continue;
      let group = byKey.get(key);
      if (!group) {
        group = {key, items: [], explicit: [], number: null};
        byKey.set(key, group); result.push(group);
      }
      group.items.push(citation);
      if (validNumber(citation.citation_number) && !group.explicit.includes(citation.citation_number))
        group.explicit.push(citation.citation_number);
    }
    for (const group of result) for (const number of group.explicit) {
      occupied.add(number); assignments.set(number, (assignments.get(number) || 0) + 1);
    }
    for (const group of result) if (group.explicit.length === 1 && assignments.get(group.explicit[0]) === 1)
      group.number = group.explicit[0];
    let next = 1;
    for (const group of result) if (!group.number) {
      while (occupied.has(next)) next++;
      group.number = next++; occupied.add(group.number);
    }
    return result;
  }
  function protectedImage(value) {
    if (typeof value !== 'string' || !value.startsWith('/kb/') || /[\\\s\u0000-\u001f]/.test(value)) return '';
    try {
      if (decodeURIComponent(value.split('?')[0]).split('/').some(part => part === '.' || part === '..')) return '';
    } catch (_error) { return ''; }
    return /^\/kb\/(?:doc\/[A-Za-z0-9_-]+\/img\/[^?#]+|evidence\/[A-Za-z0-9_-]+\/page\.png)(?:\?[^#]*)?$/.test(value) ? value : '';
  }
  function element(document, tag, className, text) {
    const result = document.createElement(tag); result.className = className;
    if (text !== undefined) result.textContent = string(text);
    return result;
  }
  function clearInline(answer) {
    if (!answer) return;
    for (const marker of answer.querySelectorAll('[data-chat-cite-marker]'))
      marker.replaceWith(answer.ownerDocument.createTextNode(marker.textContent));
  }
  function cleanup(state, inline = true) {
    if (inline) clearInline(state.aiMsg.querySelector('.answer'));
    state.row?.remove(); state.images?.remove(); active.delete(state);
  }
  function uniqueValues(items, key) {
    const result = [], seen = new Set();
    for (const item of items) for (const value of Array.isArray(item[key]) ? item[key] : []) {
      const id = typeof value === 'object' && value ? string(value.chunk_id) : string(value);
      if (id && !seen.has(id)) { seen.add(id); result.push(value); }
    }
    return result;
  }
  function focusSource(state, chip) {
    for (const source of state.references.values()) source.classList.remove('is-citation-focused');
    chip.classList.add('is-citation-focused');
    chip.scrollIntoView?.({block: 'nearest', behavior: 'smooth'}); chip.focus?.({preventScroll: true});
  }
  function imagePage(item, url) {
    const base = url.split('?')[0];
    const snapshots = Array.isArray(item.image_snapshots) ? item.image_snapshots : [];
    const snapshot = snapshots.find(value => value && typeof value.image === 'string' && base === (
      value.image.startsWith('page') && value.chunk_id
        ? '/kb/evidence/' + value.chunk_id + '/page.png'
        : '/kb/doc/' + value.doc_id + '/img/' + value.image));
    if (snapshot) return string(snapshot.page);
    const chunks = (Array.isArray(item.chunks) ? item.chunks : []).filter(value => value && value.page);
    const exact = chunks.find(value => base === '/kb/evidence/' + value.chunk_id + '/page.png');
    // A document can contain images on several pages. Never assign an unrelated
    // text chunk's page or another image's location to this preview.
    if (exact) return string(exact.page);
    if (Array.isArray(item.images) && item.images.length === 1 && chunks.length === 1 && chunks[0].is_image)
      return string(chunks[0].page);
    return '';
  }
  function imageCard(document, state, group, item, url, chip, id) {
    const labels = words(state.language), page = imagePage(item, url);
    const card = element(document, 'figure', 'chat-table-card chat-image-card chat-kb-image-card');
    const header = element(document, 'div', 'chat-table-head');
    const title = element(document, 'span', 'chat-table-title', chip.title); title.title = chip.title;
    const toggle = element(document, 'button', 'chat-table-toggle'); toggle.type = 'button';
    const viewport = element(document, 'div', 'chat-table-viewport'); viewport.id = id;
    const preview = element(document, 'button', 'chat-kb-image-preview'); preview.type = 'button';
    const image = document.createElement('img');
    image.src = url; image.loading = 'lazy'; image.decoding = 'async';
    image.alt = labels.image + ': ' + chip.title + (page ? ' (' + page + ')' : '');
    const unavailable = element(document, 'span', 'chat-kb-image-unavailable', labels.unavailable);
    unavailable.hidden = true; unavailable.setAttribute('role', 'status');
    image.addEventListener('error', () => { image.hidden = true; unavailable.hidden = false; });
    image.addEventListener('load', () => { image.hidden = false; unavailable.hidden = true; });
    function update() {
      const expanded = state.expanded.get(url) === true;
      card.classList.toggle('is-expanded', expanded);
      for (const button of [toggle, preview]) {
        button.setAttribute('aria-expanded', String(expanded)); button.setAttribute('aria-controls', id);
        button.setAttribute('aria-label', (expanded ? labels.collapseImage : labels.expandImage) + ': ' + chip.title);
      }
      toggle.textContent = expanded ? labels.collapse : labels.expand;
    }
    function resize(event) {
      event.stopPropagation(); state.expanded.set(url, state.expanded.get(url) !== true); update();
    }
    toggle.addEventListener('click', resize); header.addEventListener('click', resize); preview.addEventListener('click', resize);
    const caption = element(document, 'figcaption', 'chat-kb-image-caption');
    const source = element(document, 'button', 'chat-kb-image-source', '[' + group.number + '] ' + chip.title);
    source.type = 'button'; source.title = labels.reference + ': ' + chip.title;
    source.addEventListener('click', () => focusSource(state, chip));
    caption.appendChild(source);
    if (page) caption.appendChild(element(document, 'span', 'chat-kb-image-page', page));
    const original = element(document, 'a', 'chat-kb-image-original', labels.original);
    original.href = url; original.target = '_blank'; original.rel = 'noopener'; caption.appendChild(original);
    header.appendChild(title); header.appendChild(toggle); preview.appendChild(image); preview.appendChild(unavailable);
    viewport.appendChild(preview); card.appendChild(header); card.appendChild(viewport); card.appendChild(caption);
    update(); return card;
  }
  function sourceChip(document, group, id, language, citations) {
    const labels = words(language), first = group.items[0], chunks = uniqueValues(group.items, 'chunks').filter(item => item && item.chunk_id);
    const kind = first.kind === 'tracker' ? 'tracker' : first.kind === 'graph' ? 'graph' : 'document';
    let name, chip;
    if (kind === 'tracker') {
      name = string(group.items.find(item => item.kb_name)?.kb_name || first.kb_slug);
      chip = element(document, 'a', 'cite-chip chat-source-chip');
      chip.href = '/kb/manage/' + encodeURIComponent(string(first.kb_slug)) + '/';
    } else if (kind === 'graph') {
      name = string(first.source || first.label || labels.graph);
      chip = element(document, 'a', 'cite-chip chat-source-chip');
      chip.href = '/kb/graph/fact/' + encodeURIComponent(string(first.relation_id)) + '/?v=' + encodeURIComponent(string(first.fact_digest));
    } else {
      name = string(first.source || labels.document);
      if (chunks.length) {
        chip = element(document, 'button', 'cite-chip chat-source-chip has-ev'); chip.type = 'button';
        chip.addEventListener('click', () => { if (typeof root.fosEvidencePanel === 'function') root.fosEvidencePanel(chunks[0].chunk_id, {chunks, citations}); });
      } else {
        chip = element(document, 'a', 'cite-chip chat-source-chip');
        const highlights = uniqueValues(group.items, 'highlights').filter(value => typeof value === 'string' && value.length >= 3).slice(0, 12);
        const selected = uniqueValues(group.items, 'selected_chunk_ids').filter(value => typeof value === 'string').slice(0, 16);
        const query = selected.map(value => 'chunk=' + encodeURIComponent(value)).concat(highlights.map(value => 'h=' + encodeURIComponent(value))).join('&');
        chip.href = '/kb/doc/' + encodeURIComponent(string(first.doc_id)) + '/slices/' + (query ? '?' + query : '');
      }
    }
    if (chip.tagName.toLowerCase() === 'a') { chip.target = '_blank'; chip.rel = 'noopener'; }
    chip.id = id; chip.setAttribute('data-chat-source-number', String(group.number));
    chip.setAttribute('aria-label', '[' + group.number + '] ' + name + ' — ' + labels[kind] + ', ' + (chunks.length ? labels.evidence : labels.open));
    chip.title = name;
    chip.appendChild(element(document, 'span', 'chat-source-number', '[' + group.number + ']'));
    chip.appendChild(element(document, 'span', 'chat-source-name', name));
    chip.appendChild(element(document, 'span', 'chat-source-kind', labels[kind]));
    if (kind === 'tracker') {
      const count = new Set(group.items.map(item => item.result_digest || item.tracker_snapshot?.result_digest).filter(Boolean)).size;
      if (count > 1) { const detail = element(document, 'span', 'chat-source-count', String(count)); detail.title = count + ' ' + labels.queries; chip.appendChild(detail); }
    }
    const pages = [...new Set(chunks.map(item => string(item.page).replace(/[第\s页]/g, '')).filter(Boolean))].slice(0, 4);
    if (pages.length) {
      const badge = element(document, 'span', 'cite-page', pages.map(value => 'P' + value).join(' · '));
      badge.title = labels.pages; chip.appendChild(badge);
    }
    return chip;
  }
  function enhance(answer, state) {
    if (!answer) return;
    const document = answer.ownerDocument, labels = words(state.language), skip = new Set(['A', 'BUTTON', 'CODE', 'PRE', 'SCRIPT', 'STYLE', 'TEXTAREA', 'INPUT', 'SELECT', 'SVG', 'MATH']);
    function walk(parent) {
      if (skip.has(string(parent.tagName).toUpperCase()) || parent.hasAttribute?.('data-chat-cite-marker')) return;
      for (const node of Array.from(parent.childNodes || [])) {
        if (node.nodeType === 1) { walk(node); continue; }
        if (node.nodeType !== 3) continue;
        const value = node.nodeValue || '', regex = /\[([1-9][0-9]{0,3})\]/g; let match, cursor = 0, replaced = false;
        const fragment = document.createDocumentFragment();
        while ((match = regex.exec(value))) {
          const chip = state.references.get(Number(match[1])); if (!chip) continue;
          if (match.index > cursor) fragment.appendChild(document.createTextNode(value.slice(cursor, match.index)));
          const link = element(document, 'a', 'chat-inline-citation', match[0]);
          link.href = '#' + chip.id; link.setAttribute('data-chat-cite-marker', match[1]);
          link.setAttribute('aria-label', labels.reference + ' ' + match[0] + ': ' + chip.title);
          link.title = chip.title;
          link.addEventListener('click', event => {
            event.preventDefault();
            focusSource(state, chip);
          });
          fragment.appendChild(link); cursor = regex.lastIndex; replaced = true;
        }
        if (replaced) { if (cursor < value.length) fragment.appendChild(document.createTextNode(value.slice(cursor))); node.replaceWith(fragment); }
      }
    }
    walk(answer);
  }
  function render(aiMsg, citations, options = {}) {
    const bubble = aiMsg?.querySelector?.('.bubble'); if (!bubble) return 0;
    const language = options.language || root.FOSI18n?.language || 'en';
    let state = states.get(aiMsg);
    if (!state || state.citations !== citations || state.language !== language || state.row?.parentNode !== bubble) {
      const expanded = state?.expanded || new Map();
      if (state) cleanup(state);
      for (const stale of bubble.querySelectorAll('.cite-row, .cite-imgs')) stale.remove();
      const document = bubble.ownerDocument, groups = grouped(citations);
      state = {aiMsg, citations, language, options, expanded, references: new Map(), row: null, images: null};
      states.set(aiMsg, state);
      if (!groups.length) { const answer=aiMsg.querySelector('.answer'); clearInline(answer); if(answer)answer.hidden=false; return 0; }
      let messageId = messageIds.get(aiMsg); if (!messageId) { messageId = 'engenie-message-' + (++serial); messageIds.set(aiMsg, messageId); }
      const row = element(document, 'div', 'cite-row chat-source-list notranslate'); row.setAttribute('translate', 'no');
      row.appendChild(element(document, 'span', 'cite-label', words(language).sources));
      const images = element(document, 'div', 'chat-answer-images notranslate'), imageUrls = new Set();
      images.setAttribute('translate', 'no'); images.setAttribute('role', 'group');
      images.setAttribute('aria-label', words(language).images);
      const evidence = [], evidenceIds = new Set();
      for (const group of groups) {
        if (!group.items[0].doc_id || ['tracker', 'graph'].includes(group.items[0].kind)) continue;
        for (const chunk of uniqueValues(group.items, 'chunks')) {
          if (!chunk?.chunk_id || evidenceIds.has(chunk.chunk_id)) continue;
          evidenceIds.add(chunk.chunk_id);
          evidence.push({chunk_id: chunk.chunk_id, page: chunk.page,
            source: string(group.items[0].source), citation_number: group.number});
        }
      }
      const counts = new Map(); for (const group of groups) counts.set(group.number, (counts.get(group.number) || 0) + 1);
      for (const group of groups) {
        const chip = sourceChip(document, group, messageId + '-source-' + group.number + '-' + row.children.length, language, evidence);
        row.appendChild(chip);
        // Legacy/fallback list numbers remain visible, but only unambiguous server
        // assignments can turn answer text into a source assertion.
        if (group.explicit.length === 1 && group.explicit[0] === group.number && counts.get(group.number) === 1
            && !group.items.some(item => item.citation_inline === false))
          state.references.set(group.number, chip);
        for (const item of group.items) for (const value of Array.isArray(item.images) ? item.images : []) {
          const url = protectedImage(value); if (!url || imageUrls.has(url)) continue; imageUrls.add(url);
          images.appendChild(imageCard(document, state, group, item, url, chip, messageId + '-image-' + imageUrls.size));
        }
      }
      if (images.children.length) (root.EnGenieChatVisualizations?.rowFor?.(bubble) || bubble).appendChild(images);
      bubble.appendChild(row);
      state.row = row; state.images = images; active.add(state);
    }
    const answer=aiMsg.querySelector('.answer');
    enhance(answer, state);
    if(answer){
      const content=answer.textContent.trim();
      const numbers=[...content.matchAll(/\[([1-9][0-9]*)\]/g)].map(match=>Number(match[1]));
      answer.hidden=!!bubble.querySelector('.chat-chart-metric') && /^(?:\s*\[[1-9][0-9]*\])+\s*$/.test(content)
        && numbers.every(number=>state.references.has(number));
    }
    return state.row?.children.length ? state.row.children.length - 1 : 0;
  }
  function destroy(container) {
    for (const state of [...active]) if (state.aiMsg === container || container?.contains?.(state.aiMsg)) {
      cleanup(state); states.delete(state.aiMsg);
    }
  }
  root.document?.addEventListener('fos:languagechange', event => {
    const language = event.detail?.language; if (!['en', 'zh-hans'].includes(language)) return;
    for (const state of [...active]) {
      if (state.aiMsg.isConnected === false) { cleanup(state); states.delete(state.aiMsg); }
      else render(state.aiMsg, state.citations, {...state.options, language});
    }
  });
  const api = {render, destroy, grouped, sourceKey, protectedImage};
  root.EnGenieChatCitations = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(globalThis);
