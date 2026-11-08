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
        self.initial_candidates = len(self.candidates)
        for cid, item in self.candidates.items():
            snap = item['snapshot']; row = item['row']
            identity = (snap['doc_id'], snap['document_digest'], snap['chunk_id'] or snap['chunk_digest'],
                        snap['chunk_digest'], row.get('image', ''))
            self.identities[identity] = cid

    def snapshot(self):
        return {'candidates': self.candidates, 'loaded': sorted(self.loaded), 'presented': sorted(self.presented)}

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
                identity = (str(doc.pk),checksums[doc.pk],row.get('chunk_id') or digest(text),digest(text),row.get('image',''))
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
        if cid in self.loaded and snapshot.get('kind')=='image':
            from .publication import image_source_digest
            if not snapshot.get('image_digest') or image_source_digest(doc,snapshot.get('image'))!=snapshot['image_digest']:
                raise ValueError('图片文件已变化或不可读，请重新检索后选择。')
        return doc

    def read(self, candidate_ids, *, include_images=True):
        with self.lock:
            try:
                ids = self._ids(candidate_ids)
                if not ids:
                    raise ValueError('请选择要读取的candidate_id。')
                current_docs = {cid: self._current(cid) for cid in ids}
                result = {'kind':'rag_chunks','chunks':[],'already_loaded':[],'not_loaded':[],
                          'notice':'完整正文保留在会话中，可用于后续追问。新增读取后用kb_select_evidence选择实际采用的已读candidate_id。'}
                accepted = []
                for cid in ids:
                    if cid in self.loaded:
                        result['already_loaded'].append(cid)
                        continue
                    snap = self.candidates[cid]['snapshot']
                    if snap['kind']=='image':
                        from .publication import image_source_digest
                        image=self.candidates[cid]['row'].get('image','')
                        checksum=image_source_digest(current_docs[cid],image)
                        if not checksum:
                            result['not_loaded'].append(cid);continue
                        snap.update(image=image,image_digest=checksum,visual_confirmed=bool(self.candidates[cid]['row'].get('visual_confirmed')))
                    chunk = {'candidate_id':cid,'source':snap['source'],'kb_slug':snap['kb_slug'],
                             'page':snap['page'],'text':snap['text'],'type':snap['kind']}
                    trial = {**result,'chunks':result['chunks']+[chunk]}
                    if len(encode(trial))>min(MAX_RESPONSE_CHARS,MAX_READ_CHARS-self.read_chars)-400:
                        result['not_loaded'].append(cid)
                        continue
                    result['chunks'].append(chunk); accepted.append(cid)
                used = accepted + result['already_loaded']
                if used:
                    result = self.relationship_scope.attach(result, [current_docs[cid].kb for cid in used],
                        cap=min(MAX_RESPONSE_CHARS, MAX_READ_CHARS-self.read_chars), establish=True)
                text = encode(result)
                if len(text)>MAX_READ_CHARS-self.read_chars:
                    return encode({'error':'本轮正文读取预算已用完。请根据已读内容选择依据；不要把未读预览当作事实。'})
                self.loaded.update(accepted)
                self.read_chars += len(text)
                if accepted or result['already_loaded']:
                    self.state.update(pending=True,finalized=False)
                    self._clear()
                # 图片只有被明确选中读取后才进入上下文；不额外暗附图片候选。
                parts = [{'type':'text','text':text}]
                for cid in accepted:
                    row = self.candidates[cid]['row']
                    if include_images and row.get('type')=='image' and self.image_reader and self.images_read<4:
                        image = self.image_reader(row['doc_id'],row.get('image',''))
                        if image:
                            parts.append(image); self.images_read += 1
                return parts if len(parts)>1 else text
            except ValueError as exc:
                return encode({'error':str(exc)})

    def _clear(self):
        self.evidence[:] = [e for e in self.evidence if not e.get('rag_selected')]
        self.citations[:] = [c for c in self.citations if not c.get('rag_selected')]

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
