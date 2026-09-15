/* 证据面板：chunk 级定位视图（引用芯片 / 综合搜索命中点击打开）。
 *
 * 数据 GET /kb/evidence/<chunk_id>/preview/ →
 *   {source, pages:[s,e], page_label, blocks:[{page,bbox:[x0,y0,x1,y1],kind,rows}], has_pdf, quote}
 * 页图 GET /kb/evidence/<chunk_id>/page.png?page=N（PyMuPDF 2x 渲染）
 * bbox 是 MinerU 归一化坐标（0-1000，左上原点）→ 直接换算成百分比定位，
 * 与渲染尺寸无关；红框用圆角边框 + 半透明底色实现（跨页块可翻页）。 */
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function shortPage(label) {
    // 「第 43 页」→「43页」，「第 43-45 页」→「43-45页」
    return String(label || '').replace(/[第\s页]/g, '') || '';
  }

  let pendingPreview;
  let activeDialog;
  let requestSequence = 0;

  window.fosEvidencePanel = async function (chunkId, opts) {
    opts = opts || {};
    chunkId = String(chunkId || '');
    if (!chunkId) return;

    const sequence = ++requestSequence;
    if (pendingPreview) pendingPreview.abort();
    pendingPreview = new AbortController();
    if (activeDialog) activeDialog.close();
    let data;
    try {
      const r = await fetch('/kb/evidence/' + encodeURIComponent(chunkId) + '/preview/', {signal: pendingPreview.signal, cache: 'no-store'});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      data = await r.json();
      if (sequence !== requestSequence) return;
    } catch (e) {
      if (e.name === 'AbortError' || sequence !== requestSequence) return;
      if (window.fosToast) fosToast('证据加载失败：' + e.message, 'err');
      return;
    }
    if (!data.ok) {
      if (window.fosToast) fosToast('该片段暂无定位数据（旧版入库文档，重新入库后可用）', 'warn');
      return;
    }

    const pages = data.pages || [0, 0];
    const sourcePages = data.source_pages && data.source_pages.length ? data.source_pages : [pages[0]];
    const multi = sourcePages.length > 1;
    let pagePosition = 0;
    let cur = sourcePages[0];
    let zoomed = false;

    const dlg = document.createElement('dialog');
    dlg.className = 'fos-dialog ev-dialog';
    activeDialog = dlg;

    const head = document.createElement('div');
    head.className = 'ev-head';
    head.innerHTML =
      '<div class="ev-title">' + esc(data.source) +
      ' <span class="ev-page-tag">' + esc(shortPage(data.page_label)) + '</span></div>';
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button'; closeBtn.className = 'btn outline sm'; closeBtn.textContent = '关闭';
    head.appendChild(closeBtn);

    const toolbar = document.createElement('div');
    toolbar.className = 'ev-toolbar';
    const prev = document.createElement('button');
    prev.type = 'button'; prev.className = 'btn outline sm'; prev.textContent = '‹ 上一页';
    const pageIdx = document.createElement('span'); pageIdx.className = 'ev-page-idx';
    const next = document.createElement('button');
    next.type = 'button'; next.className = 'btn outline sm'; next.textContent = '下一页 ›';
    const zoom = document.createElement('button');
    zoom.type = 'button'; zoom.className = 'btn outline sm'; zoom.textContent = '放大';
    const docLink = document.createElement('a');
    docLink.className = 'btn outline sm'; docLink.target = '_blank'; docLink.rel = 'noopener';
    docLink.href = '/kb/doc/' + encodeURIComponent(data.doc_id) + '/html/';
    docLink.textContent = '整篇文档';
    toolbar.append(prev, pageIdx, next, zoom, docLink);
    const alternatives = opts.chunks || [];
    if (alternatives.length > 1) {
      const select = document.createElement('select');
      select.setAttribute('aria-label', '选择出处片段');
      alternatives.forEach(function (item, i) {
        const option = document.createElement('option');
        option.value = item.chunk_id;
        option.textContent = '片段 ' + (i + 1) + ' · ' + (item.page || '页码未知');
        option.selected = item.chunk_id === chunkId;
        select.appendChild(option);
      });
      select.addEventListener('change', function () { window.fosEvidencePanel(select.value, opts); });
      toolbar.prepend(select);
    }
    const precision = document.createElement('span');
    precision.textContent = '红圈为解析区域定位，非精确单元格';
    toolbar.appendChild(precision);

    // 表格行号提示（溯源给出该 chunk 覆盖的行区间时）
    const rowsInfo = (data.blocks || []).map(function (b) {
      return b.kind === 'table' && b.rows ? b : null;
    }).filter(Boolean)[0];
    if (rowsInfo && rowsInfo.rows) {
      const r0 = rowsInfo.rows[0], r1 = rowsInfo.rows[1];
      const tag = document.createElement('span');
      tag.className = 'ev-rows-tag';
      tag.textContent = r0 === r1 ? ('原表第 ' + r0 + ' 行') : ('原表第 ' + r0 + '-' + r1 + ' 行');
      toolbar.appendChild(tag);
    }

    const view = document.createElement('div');
    view.className = 'ev-view';
    const page = document.createElement('div');
    page.className = 'ev-page';
    view.appendChild(page);

    const quote = document.createElement('div');
    quote.className = 'ev-quote';
    quote.innerHTML = '<div class="ev-quote-label">命中片段原文</div>' +
      '<div class="ev-quote-text">' + esc(data.quote || '（片段原文不可用）') + '</div>';

    const body = document.createElement('div');
    body.className = 'ev-body';
    body.append(toolbar, view, quote);
    dlg.append(head, body);
    document.body.appendChild(dlg);
    closeBtn.addEventListener('click', function () { dlg.close(); });
    dlg.addEventListener('close', function () { dlg.remove(); });
    dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });

    function renderPage() {
      pageIdx.textContent = multi ? ((cur + 1) + ' / ' + (pages[1] + 1) + ' 页')
                                 : '第 ' + (cur + 1) + ' 页';
      prev.disabled = !multi || pagePosition === 0;
      next.disabled = !multi || pagePosition === sourcePages.length - 1;
      page.innerHTML = '';
      const renderedPage = cur;
      if (!data.has_pdf) {
        page.innerHTML = '<div class="ev-noimg">原文 PDF 不可用（非 PDF 文档或原文件已删除）。' +
          '下方为该片段的嵌入文本与定位页码，可点「整篇文档」查看全文。</div>';
        return;
      }
      const img = document.createElement('img');
      img.className = 'ev-img';
      img.alt = '原 PDF 第 ' + (cur + 1) + ' 页';
      img.src = '/kb/evidence/' + encodeURIComponent(chunkId) + '/page.png?page=' + cur;
      const spin = document.createElement('span');
      spin.className = 'spinner ev-spin';
      page.append(spin, img);
      img.addEventListener('load', function () { spin.remove(); });
      img.addEventListener('error', function () {
        if (cur !== renderedPage || !img.isConnected) return;
        spin.remove();
        img.remove();
        page.innerHTML = '<div class="ev-noimg">原页渲染失败（服务端 PyMuPDF 不可用或文件损坏），' +
          '可点「整篇文档」查看全文。</div>';
      });
      // bbox 红圈：当前页上的块（0-1000 归一化 → 百分比）
      (data.blocks || []).forEach(function (b) {
        if (b.page !== cur || !Array.isArray(b.bbox) || b.bbox.length !== 4) return;
        if (!b.bbox.every(Number.isFinite) || b.bbox.some(v => v < 0 || v > 1000) || b.bbox[2] <= b.bbox[0] || b.bbox[3] <= b.bbox[1]) return;
        const box = document.createElement('div');
        box.className = 'ev-box' + (b.kind === 'table' ? ' is-table' : '');
        box.style.left = (b.bbox[0] / 10) + '%';
        box.style.top = (b.bbox[1] / 10) + '%';
        box.style.width = ((b.bbox[2] - b.bbox[0]) / 10) + '%';
        box.style.height = ((b.bbox[3] - b.bbox[1]) / 10) + '%';
        if (b.kind) box.title = b.kind === 'table' ? '表格块' : (b.kind === 'image' ? '图片块' : '文本块');
        page.appendChild(box);
      });
    }

    prev.addEventListener('click', function () { if (pagePosition > 0) { cur = sourcePages[--pagePosition]; renderPage(); } });
    next.addEventListener('click', function () { if (pagePosition < sourcePages.length - 1) { cur = sourcePages[++pagePosition]; renderPage(); } });
    zoom.addEventListener('click', function () {
      zoomed = !zoomed;
      page.classList.toggle('zoomed', zoomed);
      zoom.textContent = zoomed ? '适宽' : '放大';
    });

    renderPage();
    dlg.showModal();
  };
})();
