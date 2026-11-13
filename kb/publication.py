"""Source snapshots for enhanced answers. No generated draft is a fact source."""
from __future__ import annotations

import hashlib
from pathlib import Path


def digest(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def citation_key(citation):
    """文档和图谱引用使用独立身份，多个结构化来源不能因缺少 doc_id 被折叠。"""
    if citation.get('kind') == 'graph':
        return ('graph', str(citation.get('relation_id')), citation.get('fact_digest'))
    if citation.get('kind') == 'tracker':
        return ('tracker', citation.get('result_digest'))
    return ('document', citation.get('doc_id'))


def bind_evidence(results, kb_slug):
    from .models import ChunkProvenance, Document
    bound = []
    for row in results:
        cid = row.get('chunk_id') or ''
        prov = ChunkProvenance.objects.select_related('document__kb').filter(chunk_id=cid).first() if cid else None
        doc = prov.document if prov else None
        if doc is None and row.get('doc_id'):
            from django.core.exceptions import ValidationError
            try:
                doc = Document.objects.select_related('kb').filter(pk=row['doc_id']).first()
            except (ValueError, TypeError, ValidationError):
                pass
        if doc is None:
            candidates = Document.objects.filter(kb__slug=kb_slug, original_name=row.get('source', ''))
            if candidates.count() == 1:
                doc = candidates.first()
        text = row.get('text') or ''
        bound.append({
            'source': row.get('source', ''), 'page': row.get('page_label', ''),
            'text': text, 'chunk_id': cid,
            'doc_id': str(doc.id) if doc else '',
            'kb_slug': doc.kb.slug if doc else kb_slug,
            'document_digest': digest(doc.md_content) if doc else '',
            'chunk_digest': digest(text),
            'kind': 'image' if row.get('type') == 'image' else 'text',
        })
    return bound


def image_source_digest(document,name):
    """Fingerprint the actual image, or the source PDF for a rendered page."""
    import re
    from django.core.exceptions import SuspiciousFileOperation
    from .pipeline import doc_image_dir,_DOC_IMG_NAME_RE
    if not isinstance(name,str):return None
    try:
        if re.fullmatch(r'page[1-9][0-9]*',name):
            if not document.file:return None
            path=Path(document.file.path)
        elif _DOC_IMG_NAME_RE.fullmatch(name):path=doc_image_dir(document.pk)/name
        else:return None
        checksum=hashlib.sha256()
        with path.open('rb') as source:
            for block in iter(lambda:source.read(1024*1024),b''):checksum.update(block)
        return checksum.hexdigest()
    except (OSError,ValueError,SuspiciousFileOperation,NotImplementedError):return None


def image_snapshot_current(snapshot,user):
    from .models import Document
    from .access import kb_accessible
    from django.core.exceptions import ValidationError
    try:doc=Document.objects.select_related('kb__parent').filter(pk=snapshot.get('doc_id'),status='completed').first()
    except (ValueError,TypeError,ValidationError):return False
    current = bool(doc and kb_accessible(user,doc.kb) and doc.kb.slug==snapshot.get('kb_slug')
        and digest(doc.md_content)==snapshot.get('document_digest') and snapshot.get('image_digest')
        and image_source_digest(doc,snapshot.get('image'))==snapshot['image_digest'])
    if current and 'image_metadata_digest' in snapshot:
        from .chunk_images import image_metadata_current
        current = image_metadata_current(doc, snapshot)
    return current


def validate_sources(evidence, user_id):
    """Re-read current permissions, original text and chunk identity before release."""
    from django.contrib.auth import get_user_model
    from .access import kb_accessible
    from .models import Document
    from .keyword_index import get_chunks
    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None or not evidence:
        return False
    for ev in evidence:
        if ev.get('kind') == 'chat_attachment':
            from .chat_attachments import snapshot_current
            if not snapshot_current(ev, user.pk):
                return False
            continue
        if ev.get('kind')=='image':
            if not image_snapshot_current(ev,user):return False
            continue
        if ev.get('kind') == 'tracker':
            from .tracker_query import validate_snapshot
            if not validate_snapshot(ev, user):
                return False
            continue
        if ev.get('kind') == 'graph':
            from .knowledge_graph import validate_fact
            if not validate_fact(ev, user):
                return False
            continue
        if not ev.get('doc_id') or ev.get('kind') != 'text':
            return False
        doc = Document.objects.select_related('kb').filter(pk=ev['doc_id'], status=Document.Status.COMPLETED).first()
        if doc is None or not kb_accessible(user, doc.kb) or digest(doc.md_content) != ev.get('document_digest'):
            return False
        if not ev.get('chunk_id'):
            # Cron-generated source files intentionally have no keyword or
            # vector chunks. Validate the exact selected text against the
            # currently published file instead of bypassing verification.
            from .models import FeedRecord
            text = ev.get('text')
            if (not isinstance(text, str) or not text
                    or digest(text) != ev.get('chunk_digest')
                    or text not in (doc.md_content or '')
                    or ev.get('source') != doc.original_name
                    or ev.get('kb_slug') != doc.kb.slug
                    or not FeedRecord.objects.filter(
                        document=doc, deleted=False,
                        feed__adapter_spec__ingest_mode='documents',
                    ).exists()):
                return False
            continue
        rows = get_chunks(doc.kb.slug, doc.original_name)
        if not any(row.get('chunk_id') == ev['chunk_id'] and digest(row['text']) == ev['chunk_digest'] for row in rows):
            return False
        if ev.get('value_cards'):
            from .value_cards import grounded_card_current
            if not isinstance(ev['value_cards'], list) or any(not grounded_card_current(card, ev) for card in ev['value_cards']):
                return False
    return True


def visible_citations(citations, user):
    from .access import kb_accessible
    from .models import Document
    from django.core.exceptions import ValidationError
    from .keyword_index import get_chunks
    for citation in citations:
        if citation.get('kind') == 'tracker':
            from .tracker_query import validate_snapshot
            snapshot = citation.get('tracker_snapshot')
            if (not isinstance(snapshot,dict) or citation.get('result_digest')!=snapshot.get('result_digest')
                    or citation.get('kb_slug')!=snapshot.get('query',{}).get('kb_slug')
                    or not validate_snapshot(snapshot,user)):
                return False
            continue
        if citation.get('kind') == 'graph':
            from .knowledge_graph import validate_fact
            snapshot = citation.get('graph_snapshot')
            if (not isinstance(snapshot, dict)
                    or str(citation.get('relation_id')) != str(snapshot.get('relation_id'))
                    or citation.get('fact_digest') != snapshot.get('fact_digest')
                    or not validate_fact(snapshot, user)):
                return False
            continue
        try:
            doc = Document.objects.select_related('kb').filter(pk=citation.get('doc_id'), status=Document.Status.COMPLETED).first()
        except (ValueError, TypeError, ValidationError):
            return False
        if doc is None or not kb_accessible(user, doc.kb):
            return False
        if citation.get('document_digest') and digest(doc.md_content) != citation['document_digest']:
            return False
        if 'value_cards' in citation:
            from .value_cards import grounded_card_current
            cards = citation['value_cards']
            if not isinstance(cards, list) or len(cards) > 8:
                return False
            rows = get_chunks(doc.kb.slug, doc.original_name)
            for card in cards:
                if not isinstance(card, dict):
                    return False
                row = next((row for row in rows if row.get('chunk_id') == card.get('chunk_id')), None)
                if row is None or not grounded_card_current(card, {'kind': 'text', 'doc_id': str(doc.pk),
                        'chunk_id': row['chunk_id'], 'document_digest': digest(doc.md_content),
                        'chunk_digest': digest(row['text']), 'text': row['text']}):
                    return False
        image_snapshots=citation.get('image_snapshots',[])
        if not isinstance(image_snapshots,list) or any(not isinstance(ev,dict) or ev.get('doc_id')!=str(doc.pk)
                or not image_snapshot_current(ev,user) for ev in image_snapshots):return False
        if citation.get('chunks') or citation.get('selected_chunk_ids'):
            live_ids = {row['chunk_id'] for row in get_chunks(doc.kb.slug, doc.original_name)}
            live_ids.update(ev['chunk_id'] for ev in image_snapshots if ev.get('chunk_id'))
            if (any(chunk.get('chunk_id') not in live_ids for chunk in citation.get('chunks',[]))
                    or any(cid not in live_ids for cid in citation.get('selected_chunk_ids',[]))):
                return False
    return True


def verified_table_export(answer, filename='verified.csv'):
    """Fixed CSV writer from the exact table that was verified; never model code."""
    import re
    tables = []
    current = []
    for line in answer.splitlines() + ['']:
        if line.strip().startswith('|') and line.strip().endswith('|'):
            current.append([cell.strip().replace(r'\|', '|') for cell in re.split(r'(?<!\\)\|', line.strip()[1:-1])])
        else:
            if len(current) >= 3 and all(re.fullmatch(r':?-{3,}:?', cell) for cell in current[1]):
                rows = [current[0]] + current[2:]
                if all(len(row) == len(rows[0]) for row in rows):
                    tables.append(rows)
            current = []
    if len(tables) != 1:
        return None
    # Avoid spreadsheet formula execution on opening the generated CSV.
    rows = [["'" + cell if cell.lstrip().startswith(('=', '+', '-', '@')) else cell for cell in row] for row in tables[0]]
    filename = re.sub(r'[^\w.-]', '_', filename.rsplit('/', 1)[-1])[:80]
    filename = (filename.rsplit('.', 1)[0] or 'verified') + '.csv'
    return {'filename': filename, 'code': 'import csv\nwith open(' + repr(filename) + ', "w", encoding="utf-8-sig", newline="") as output:\n    csv.writer(output).writerows(' + repr(rows) + ')\n'}
