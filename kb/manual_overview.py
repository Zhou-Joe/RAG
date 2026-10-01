"""Bounded, chapter-balanced evidence for whole-manual questions."""
import re


def is_manual_overview(question):
    text = re.sub(r'\s+', '', question or '').lower()
    # Follow-up summaries of checks/tables must keep the ordinary retrieval path.
    return bool(re.search(r'手册|说明书|manual|document', text) and
                re.search(r'总结|概览|概述|有啥|有什么|哪些内容|内容介绍|summary|summari[sz]e|overview', text) and
                not re.search(r'检查.*频率|频率.*检查|以上|上述', text))


def select_overview(rows, limit=24, budget=24000):
    """Use original FTS order, one complete chunk per section, chapters first.

    Never truncate a chunk: conditions/units may be at its end. Return coverage
    separately so the generator cannot claim it has read the whole manual.
    """
    sections = {}
    for row in rows:
        section = row.get('section') or '未标注章节'
        sections.setdefault(section, row)
    items = list(sections.items())
    # Legacy section metadata removed numbers and shortened headings ("第章",
    # "目"). Retain these real parser forms, without naming any equipment.
    priority = lambda item: (0 if re.search(r'^目$|目\s*录|contents', item[0], re.I) else
                             1 if re.search(r'第\s*\d*\s*章|\d+\s*第\s*章|chapter\s*\d+', item[0], re.I) else 2)
    chosen, size = [], 0
    for section, row in sorted(items, key=priority):
        length = len(row.get('text') or '')
        if len(chosen) >= limit or size + length > budget:
            continue
        chosen.append(row)
        size += length
    chosen_ids = {id(row) for row in chosen}
    return [row for row in rows if id(row) in chosen_ids], list(sections)


def outline_answer(evidence):
    """Extract current chapter titles verbatim; never reuse a rejected draft."""
    from .models import Document
    from .publication import digest
    documents = {}
    for item in evidence:
        documents.setdefault(item.get('doc_id'), item.get('document_digest'))
    blocks = []
    for doc_id, expected in documents.items():
        doc = Document.objects.filter(pk=doc_id, status=Document.Status.COMPLETED).first()
        if doc is None or digest(doc.md_content) != expected:
            return ''
        headings = re.findall(r'^#{1,6}\s+(.+)$', doc.md_content or '', re.M)
        titles = list(dict.fromkeys(title.strip() for title in headings
                                   if re.search(r'第\s*\d+\s*章|\d+\s*第\s*章|chapter\s*\d+', title, re.I)))
        if not titles:
            return ''
        blocks.append('《' + doc.original_name + '》主要包含以下内容（原文章节标题）：\n\n' +
                      '\n'.join('- ' + title for title in titles))
    return '\n\n'.join(blocks) + ('\n\n这是手册内容概览；具体参数、检查频率和操作条件需按对应章节另行核对。' if blocks else '')
