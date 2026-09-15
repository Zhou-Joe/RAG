"""Source snapshots for enhanced answers. No generated draft is a fact source."""
from __future__ import annotations

import hashlib


def digest(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


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
        if not ev.get('doc_id') or not ev.get('chunk_id') or ev.get('kind') != 'text':
            return False
        doc = Document.objects.select_related('kb').filter(pk=ev['doc_id'], status=Document.Status.COMPLETED).first()
        if doc is None or not kb_accessible(user, doc.kb) or digest(doc.md_content) != ev.get('document_digest'):
            return False
        rows = get_chunks(doc.kb.slug, doc.original_name)
        if not any(row.get('chunk_id') == ev['chunk_id'] and digest(row['text']) == ev['chunk_digest'] for row in rows):
            return False
    return True


def visible_citations(citations, user):
    from .access import kb_accessible
    from .models import Document
    from django.core.exceptions import ValidationError
    from .keyword_index import get_chunks
    for citation in citations:
        try:
            doc = Document.objects.select_related('kb').filter(pk=citation.get('doc_id'), status=Document.Status.COMPLETED).first()
        except (ValueError, TypeError, ValidationError):
            return False
        if doc is None or not kb_accessible(user, doc.kb):
            return False
        if citation.get('document_digest') and digest(doc.md_content) != citation['document_digest']:
            return False
        if citation.get('chunks'):
            live_ids = {row['chunk_id'] for row in get_chunks(doc.kb.slug, doc.original_name)}
            if any(chunk.get('chunk_id') not in live_ids for chunk in citation['chunks']):
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
