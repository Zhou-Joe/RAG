from __future__ import annotations

"""单轮 RAG 候选目录、按需读取与最终证据选择；未选候选不进入引用。"""

import json
import re
import threading

from django.core.exceptions import ValidationError

from .models import Document
from .publication import digest

MAX_CANDIDATES = 120
MAX_CACHED_CHARS = 300000
MAX_DISCOVERY_CHARS = 12000
MAX_READ_CHARS = 18000
MAX_RESPONSE_CHARS = 10000


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _association_key(row):
    if not row.get('relation'):
        return ''
    return digest(encode({key: row.get(key) for key in (
        'relation', 'position', 'parent_chunk_id', 'parent_chunk_digest', 'page_label', 'image_metadata_digest')}))


class RagSelection:
    def __init__(self, allowed, evidence, citations, state=None, image_reader=None, relationship_scope=None, saved=None):
        self.allowed = allowed
        self.evidence = evidence
        self.citations = citations
        self.state = state if state is not None else {}
        self.image_reader = image_reader
        from .kb_links import RelationshipScope
        self.relationship_scope = relationship_scope or RelationshipScope(allowed)
        self.candidates = {}
        self.identities = {}
        self.loaded = set()
        self.presented = set()
        self.cached_chars = 0
        self.discovery_chars = 0
        self.read_chars = 0
        self.images_read = 0
        self.lock = threading.RLock()
        saved = saved or {}
        self.candidates = saved.get('candidates', {})
        self.loaded = set(saved.get('loaded', []))
        self.presented = set(saved.get('presented', []))
        # Reading a caption does not mean the model received the image pixels.
        # Older saved conversations have no delivery stamp and may upgrade on
        # their next explicit image read.
        self.image_inputs = (set(saved.get('image_inputs', [])) & self.loaded) if image_reader else set()
        self.initial_candidates = len(self.candidates)
        for cid, item in self.candidates.items():
            snap = item['snapshot']; row = item['row']
            identity = (snap['doc_id'], snap['document_digest'], snap['chunk_id'] or snap['chunk_digest'],
                        snap['chunk_digest'], row.get('image', ''), _association_key(row))
            self.identities[identity] = cid

    def snapshot(self):
        return {'candidates': self.candidates, 'loaded': sorted(self.loaded), 'presented': sorted(self.presented),
                'image_inputs': sorted(self.image_inputs)}

    @property
    def pending(self):
        return bool(self.state.get('pending'))

    def _document(self, row, scope):
        try:
            doc = Document.objects.select_related('kb').filter(pk=row.get('doc_id'),status='completed').first() if row.get('doc_id') else None
        except (ValidationError,ValueError,TypeError):
            doc = None
        if doc is None:
            docs = list(Document.objects.select_related('kb').filter(kb__slug__in=scope,
                original_name=row.get('source',''),status='completed')[:2])
            doc = docs[0] if len(docs)==1 else None
        return doc if doc and doc.kb.slug in scope and self.allowed(doc.kb) else None

    def discover(self, results, scope, query='', limit=20):
        with self.lock:
            if self.discovery_chars >= MAX_DISCOVERY_CHARS-800:
                return encode({'error':'本轮候选预览预算已用完，请读取已有候选或明确缩小文档范围。'})
            found = []; seen = set(); omitted = 0; docs = {}; checksums = {}
            for row in results:
                key = (str(row.get('doc_id') or ''),row.get('source',''))
                if key not in docs:docs[key] = self._document(row,scope)
                doc = docs[key]
                if doc is None:
                    continue
                text = row.get('text') or ''
                if doc.pk not in checksums:checksums[doc.pk] = digest(doc.md_content)
                identity = (str(doc.pk),checksums[doc.pk],row.get('chunk_id') or digest(text),digest(text),
                            row.get('image',''),_association_key(row))
                if identity in seen:
                    continue
                seen.add(identity)
                cid = self.identities.get(identity)
                if cid is None:
                    if len(self.candidates)-self.initial_candidates>=MAX_CANDIDATES or self.cached_chars+len(text)>MAX_CACHED_CHARS:
                        omitted += 1
                        continue
                    cid = 'C'+str(len(self.candidates)+1)
                    snapshot = {'kind':'image' if row.get('type')=='image' else 'text',
                        'source':doc.original_name,'page':row.get('page_label',''),'text':text,
                        'chunk_id':row.get('chunk_id',''),'doc_id':str(doc.pk),'kb_slug':doc.kb.slug,
                        'document_digest':checksums[doc.pk],'chunk_digest':digest(text),'rag_selected':True}
                    if row.get('image_metadata_digest'):
                        snapshot['image_metadata_digest'] = row['image_metadata_digest']
                    self.candidates[cid] = {'row':dict(row,source=doc.original_name,doc_id=str(doc.pk),kb_slug=doc.kb.slug),
                        'snapshot':snapshot,'kb_name':doc.kb.name}
                    self.identities[identity] = cid
                    self.cached_chars += len(text)
                found.append(cid)
            documents = {}
            cards = []
            cap = min(MAX_RESPONSE_CHARS, MAX_DISCOVERY_CHARS-self.discovery_chars)-100
            for cid in found:
                item = self.candidates[cid]; row = item['row']; snap = item['snapshot']
                key = snap['doc_id']
                group = documents.setdefault(key,{'doc_id':key,'source':snap['source'],'kb_slug':snap['kb_slug'],
                    'kb_name':item['kb_name'],'candidate_count':0,'candidate_ids':[]})
                group['candidate_count'] += 1
                group['candidate_ids'].append(cid)
                text = snap['text']; pos = text.lower().find(query.lower()) if query else -1
                start = max(0,pos-40) if pos>=0 else 0
                cards.append({'candidate_id':cid,'doc_id':key,'kb_slug':snap['kb_slug'],
                    'section':str(row.get('section',''))[:120],'page':str(snap['page'])[:80],
                    'type':snap['kind'],'chars':len(text),'preview':text[start:start+160],
                    'preview_only':True})
            result = {'kind':'rag_candidates','candidate_count':len(found),'documents':list(documents.values()),
                'candidates':cards,'returned_candidates':len(cards),'total_matching_chunks':None,
                'retrieval_limit':limit,'limit_reached':len(results)>=limit,'omitted_candidates':omitted,
                'notice':'数量仅指本次有界召回候选，不是全库精确命中总数。按KB、文档、章节选择candidate_id，'
                         '用kb_read_chunks读取正文；预览不能作为回答依据。最后用kb_select_evidence选择已读依据，未选片段不引用。'}
            while len(encode(result))>cap and result['candidates']:
                result['candidates'].pop()
                result['returned_candidates'] = len(result['candidates'])
            while len(encode(result))>cap and result['documents']:
                result['documents'].pop()
            if len(encode(result))>cap:
                return encode({'error':'候选目录过长，请指定更小的知识库范围。'})
            result['preview_truncated'] = len(result['candidates'])<len(found)
            result['omitted_documents'] = len(documents)-len(result['documents'])
            self.presented.update(c['candidate_id'] for c in result['candidates'])
            self.presented.update(cid for doc in result['documents'] for cid in doc['candidate_ids'])
            text = encode(result)
            self.discovery_chars += len(text)
            if found:self.state.update(used=True,finalized=False)
            return text

    def _ids(self, ids, read=False):
        if not isinstance(ids,list) or len(ids)>16 or any(not isinstance(cid,str) for cid in ids):
            raise ValueError('candidate_ids 使用最多16个候选编号的数组。')
        ids = list(dict.fromkeys(ids))
        for cid in ids:
            if cid not in self.presented or read and cid not in self.loaded:
                raise ValueError('只能选择本会话已'+('读取' if read else '召回')+'的候选编号；不可猜测编号。')
        return ids

    def _current(self, cid):
        snapshot = self.candidates[cid]['snapshot']
        doc = Document.objects.select_related('kb__parent').filter(pk=snapshot['doc_id'],status='completed').first()
        if (not doc or not self.allowed(doc.kb) or doc.kb.slug!=snapshot['kb_slug']
                or digest(doc.md_content)!=snapshot['document_digest']):
            raise ValueError('文档权限或内容已变化，请重新检索后选择。')
        row = self.candidates[cid]['row']
        if row.get('image_metadata_digest'):
            from .chunk_images import image_metadata_current
            if not image_metadata_current(doc, row):
                raise ValueError('The image metadata changed. Read the text chunk again to refresh its image directory.')
        if snapshot.get('kind') == 'image' and row.get('relation') and row.get('parent_chunk_digest'):
            # Captions and page labels can come from extraction metadata even
            # for explicit Markdown refs. Recheck before reading or selecting.
            from .chunk_images import linked_images
            parents = [item for item in self.candidates.values()
                       if item['snapshot']['kind'] == 'text'
                       and item['snapshot']['doc_id'] == snapshot['doc_id']
                       and item['snapshot']['chunk_digest'] == row.get('parent_chunk_digest')
                       and item['snapshot']['chunk_id'] == row.get('parent_chunk_id', '')]
            if not any(image.get('image') == row.get('image')
                       and image.get('position') == row.get('position')
                       and image.get('relation') == row.get('relation')
                       and image.get('text', '') == snapshot['text']
                       and image.get('page_label', '') == snapshot['page']
                       for parent in parents for image in linked_images(doc, parent['row'], limit=8)):
                raise ValueError('The image association changed. Read the text chunk again to refresh its image directory.')
        if cid in self.loaded and snapshot.get('kind')=='image':
            from .publication import image_source_digest
            if not snapshot.get('image_digest') or image_source_digest(doc,snapshot.get('image'))!=snapshot['image_digest']:
                raise ValueError('图片文件已变化或不可读，请重新检索后选择。')
        return doc

    def _linked_image_hints(self, result, used, current_docs, *, cap):
        """Offer associated images as independent, unread candidates, never evidence."""
        from .chunk_images import linked_images

        associations = []
        rows = []
        for parent_id in used:
            item = self.candidates[parent_id]
            if item['snapshot']['kind'] != 'text':
                continue
            document = current_docs[parent_id]
            for row in linked_images(document, item['row'], limit=8):
                # Resolver output remains bound to the exact read document.
                if str(row.get('doc_id', document.pk)) != str(document.pk):
                    continue
                row = dict(row, doc_id=str(document.pk), source=document.original_name,
                           kb_slug=document.kb.slug, type='image')
                associations.append((parent_id, row))
                rows.append(row)
                if len(rows) >= 16:
                    break
            if len(rows) >= 16:
                break
        if not rows:
            return result

        previous_presented = set(self.presented)
        scopes = list(dict.fromkeys(row['kb_slug'] for row in rows))
        directory = json.loads(self.discover(rows, scopes, limit=len(rows)))
        offered = {card['candidate_id'] for card in directory.get('candidates', [])}
        offered.update(cid for document in directory.get('documents', [])
                       for cid in document.get('candidate_ids', []))
        hints = []
        seen = set()
        for parent_id, row in associations:
            cid = next((cid for cid in offered
                        if self.candidates[cid]['row'].get('image') == row.get('image')
                        and self.candidates[cid]['snapshot']['doc_id'] == row['doc_id']
                        and self.candidates[cid]['snapshot']['chunk_digest'] == digest(row.get('text') or '')
                        and self.candidates[cid]['snapshot']['chunk_id'] == row.get('chunk_id', '')
                        and _association_key(self.candidates[cid]['row']) == _association_key(row)), None)
            if cid is None or (parent_id, cid) in seen:
                continue
            seen.add((parent_id, cid))
            hint = {'parent_candidate_id': parent_id, 'candidate_id': cid,
                    'source': row['source'], 'page': str(row.get('page_label') or '')[:80],
                    'caption': str(row.get('text') or '')[:240],
                    'relation': str(row.get('relation') or 'embedded_reference')[:80]}
            if isinstance(row.get('position'), dict):
                hint['position'] = row['position']
            trial = {**result, 'linked_images': hints + [hint]}
            if len(encode(trial)) <= cap:
                hints.append(hint)
        # Unshown identifiers must not become guessable read targets.
        self.presented = previous_presented | {hint['candidate_id'] for hint in hints}
        if hints:
            result['linked_images'] = hints
        return result

    def read(self, candidate_ids, *, include_images=True):
        with self.lock:
            try:
                ids = self._ids(candidate_ids)
                if not ids:
                    raise ValueError('Choose at least one candidate_id to read.')
                current_docs = {cid: self._current(cid) for cid in ids}
                cap = min(MAX_RESPONSE_CHARS, MAX_READ_CHARS - self.read_chars)
                result = {'kind': 'rag_chunks', 'chunks': [], 'already_loaded': [], 'not_loaded': [],
                          'notice': 'Read text remains in this conversation. linked_images are unread image candidates; '
                                    'read their candidate_id to inspect pixels. Relation adjacent_reference means near '
                                    'the chunk boundary; content_list_sequence means extraction order. Select only '
                                    'evidence actually used with kb_select_evidence. Caption-only images do not '
                                    'support visual claims.'}
                image_status = {}
                labels = {}
                # Reserve status and identity-label space before accepting text.
                # Delivery may shorten placeholders but cannot overrun the cap.
                for cid in ids:
                    item = self.candidates[cid]
                    if item['snapshot']['kind'] != 'image':
                        continue
                    snap = item['snapshot']
                    status = {'candidate_id': cid, 'status': 'caption_only', 'reason': 'source_unavailable'}
                    if cid in self.image_inputs:
                        status = {'candidate_id': cid, 'status': 'already_delivered'}
                    elif not include_images:
                        status['reason'] = 'not_requested'
                    elif not self.image_reader:
                        status['reason'] = 'vision_disabled'
                    image_status[cid] = status
                    labels[cid] = encode({'image_candidate_id': cid, 'source': snap['source'], 'page': snap['page']})
                if image_status:
                    result['image_inputs'] = list(image_status.values())
                label_reserve = sum(len(labels[cid]) for cid in labels
                                    if cid not in self.image_inputs and include_images and self.image_reader)
                text_cap = cap - label_reserve
                accepted = []
                for cid in ids:
                    if cid in self.loaded:
                        result['already_loaded'].append(cid)
                        continue
                    snap = self.candidates[cid]['snapshot']
                    if snap['kind'] == 'image':
                        from .publication import image_source_digest
                        image = self.candidates[cid]['row'].get('image', '')
                        checksum = image_source_digest(current_docs[cid], image)
                        if not checksum:
                            result['not_loaded'].append(cid)
                            image_status[cid].update(status='not_loaded', reason='source_unavailable')
                            continue
                        snap.update(image=image, image_digest=checksum,
                                    visual_confirmed=bool(self.candidates[cid]['row'].get('visual_confirmed')))
                    chunk = {'candidate_id': cid, 'source': snap['source'], 'kb_slug': snap['kb_slug'],
                             'page': snap['page'], 'text': snap['text'], 'type': snap['kind']}
                    trial = {**result, 'chunks': result['chunks'] + [chunk]}
                    if len(encode(trial)) > text_cap - 100:
                        result['not_loaded'].append(cid)
                        if cid in image_status:
                            image_status[cid].update(status='not_loaded', reason='response_budget')
                        continue
                    result['chunks'].append(chunk)
                    accepted.append(cid)
                used = accepted + result['already_loaded']
                if len(encode(result)) > text_cap:
                    return encode({'error': 'The conversation read budget is exhausted. Select already-read evidence.'})
                # Optional directories fit around accepted body text, never replace it.
                result = self._linked_image_hints(result, used, current_docs, cap=text_cap)
                if used:
                    result = self.relationship_scope.attach(result, [current_docs[cid].kb for cid in used],
                        cap=text_cap, establish=True)
                image_parts = []
                delivered = []
                for cid in used:
                    if cid not in image_status or cid in self.image_inputs:
                        continue
                    status = image_status[cid]
                    if not include_images or not self.image_reader:
                        continue
                    if self.images_read + len(delivered) >= 4:
                        status['reason'] = 'image_limit'
                        continue
                    row = self.candidates[cid]['row']
                    try:
                        image = self.image_reader(row['doc_id'], row.get('image', ''))
                    except Exception:
                        image = None
                    if not image:
                        status['reason'] = 'image_unavailable'
                        continue
                    from .publication import image_source_digest
                    if image_source_digest(current_docs[cid], row.get('image', '')) != self.candidates[cid]['snapshot']['image_digest']:
                        status['reason'] = 'source_changed'
                        continue
                    status.clear()
                    status.update(candidate_id=cid, status='delivered')
                    image_parts.extend([{'type': 'text', 'text': labels[cid]}, image])
                    delivered.append(cid)
                text = encode(result)
                self.loaded.update(accepted)
                self.image_inputs.update(delivered)
                self.images_read += len(delivered)
                self.read_chars += len(text) + sum(len(part['text']) for part in image_parts if part.get('type') == 'text')
                if used:
                    self.state.update(pending=True, finalized=False)
                    self._clear()
                return [{'type': 'text', 'text': text}] + image_parts if image_parts else text
            except ValueError as exc:
                return encode({'error': str(exc)})

    def _clear(self):
        self.evidence[:] = [e for e in self.evidence if not e.get('rag_selected')]
        self.citations[:] = [c for c in self.citations if not c.get('rag_selected')]

    def show_value(self, candidate_id, *, title, value, unit='', quote=''):
        with self.lock:
            try:
                self._ids([candidate_id], read=True)
                selected = self.state.get('selected_ids', [])
                if candidate_id not in selected or not self.state.get('finalized'):
                    raise ValueError('Select the supporting passage with kb_select_evidence first.')
                for selected_id in selected:
                    self._current(selected_id)
                from .value_cards import grounded_value_card
                snapshot = self.candidates[candidate_id]['snapshot']
                card = grounded_value_card(snapshot, title=title, value=value, unit=unit, quote=quote)
                cards = snapshot.setdefault('value_cards', [])
                if card not in cards:
                    if sum(len(item['snapshot'].get('value_cards', [])) for item in self.candidates.values()) >= 8:
                        raise ValueError('At most eight source value cards can be displayed.')
                    cards.append(card)
                chosen = json.loads(self.select(list(selected)))
                if chosen.get('error'):
                    raise ValueError(chosen['error'])
                return encode({'presentation': 'value_card', 'chart': card['chart'],
                    'notice': 'The value card is displayed above the answer with its selected source. '
                              'For a simple exact-answer question, return only its assigned [N] citation. '
                              'Do not repeat the value or add insights, caveats, or other prose.'})
            except ValueError as exc:
                return encode({'error': str(exc)})

    def select(self, candidate_ids):
        with self.lock:
            try:
                ids = self._ids(candidate_ids,read=True)
                for cid in ids:self._current(cid)
                self._clear()
                by_doc = {}
                for cid in ids:
                    item = self.candidates[cid]; snap = item['snapshot']; row = item['row']
                    self.evidence.append(dict(snap))
                    entry = by_doc.setdefault(snap['doc_id'],{'doc_id':snap['doc_id'],'source':snap['source'],
                        'kb_slug':snap['kb_slug'],'document_digest':snap['document_digest'],
                        'highlights':[],'chunks':[],'images':[],'candidate_ids':[],
                        'selected_chunk_ids':[],'rag_selected':True})
                    entry['candidate_ids'].append(cid)
                    if snap.get('value_cards'):
                        entry.setdefault('value_cards', []).extend(snap['value_cards'])
                    if snap['chunk_id']:
                        entry['selected_chunk_ids'].append(snap['chunk_id'])
                    if snap['chunk_id'] and snap['page']:
                        entry['chunks'].append({'chunk_id':snap['chunk_id'],'page':snap['page'],
                                               'is_image':snap['kind']=='image'})
                    anchor = re.sub(r'<[^>]+>|^【[^】]*】\s*','',snap['text']).strip()[:40]
                    if anchor and anchor not in entry['highlights']:entry['highlights'].append(anchor)
                    if snap['kind']=='image':entry.setdefault('image_snapshots',[]).append(dict(snap))
                    if row.get('image'):
                        url = (f"/kb/evidence/{snap['chunk_id']}/page.png" if str(row['image']).startswith('page') and snap['chunk_id']
                               else f"/kb/doc/{snap['doc_id']}/img/{row['image']}")
                        entry['images'].append(url)
                self.citations.extend(by_doc.values())
                self.state.update(pending=False,finalized=True,selected_ids=ids)
                return encode({'selected_candidate_ids':ids,'selected_chunks':len(ids),'documents':len(by_doc),
                               'notice':'引用和核实依据已替换为本次选择。未选候选和已读但未采用的片段不会进入引用。'})
            except ValueError as exc:
                return encode({'error':str(exc)})
