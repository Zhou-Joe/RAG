from __future__ import annotations

"""数据源执行：有期限的互斥锁、版本暂存、整批发布及成功后的游标提交。"""

import logging
import secrets
import re
import threading
import time
import uuid
from datetime import timedelta

from django.core.files.base import ContentFile
from django.db import close_old_connections,transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import access
from .feed_cron import CronSchedule
from .feed_protocol import canonical,validate_batch
from .models import Document,FeedRecord,FeedRun,KnowledgeBase,KnowledgeFeed,KbTracker,TrackerRow
from .publication import digest

MAX_RUN_SECONDS=60*60
# Keep the fencing lease longer than the complete execution budget.
LEASE_SECONDS=MAX_RUN_SECONDS+5*60


class FeedBusy(ValueError):pass


def authorize(feed):
    user=feed.created_by
    if not user or not user.is_active or not access.can_manage_kb(user,feed.kb):raise ValueError('数据源创建者已无管理权限，更新已停止。')
    if (feed.header_env and not feed.credential_id) and not access.is_global_admin(user):raise ValueError('环境变量认证必须由仍有效的全局管理员管理。')


def rotate_token(feed):
    token='fosfeed_'+secrets.token_urlsafe(32)
    feed.token_digest=digest(token);feed.save(update_fields=['token_digest','updated_at'])
    return token


def claim(feed_id,*,trigger='manual',payload=None,request_key='',force=False):
    """单条条件更新实现跨进程互斥；重复 batch_id 返回已有运行。"""
    if payload is not None:validate_batch(payload)
    signature=digest(canonical(payload)) if payload is not None else ''
    now=timezone.now();token=uuid.uuid4().hex
    with transaction.atomic():
        feed=KnowledgeFeed.objects.select_related('kb','created_by').get(pk=feed_id)
        authorize(feed)
        if not feed.enabled and not force:raise ValueError('数据源已暂停。')
        old=None
        if request_key:
            old=feed.runs.filter(request_key=request_key).first()
            if old:
                if old.payload_digest!=signature:raise ValueError('同一个 batch_id 不能用于不同内容。')
                if old.status!='failed':return old,False
        if trigger=='cron' and (feed.next_run_at is None or feed.next_run_at>now):raise FeedBusy('本次计划尚未到期。')
        locked=KnowledgeFeed.objects.filter(pk=feed.pk).filter(Q(lease_token='')|Q(lease_until__lte=now)).update(
            lease_token=token,lease_until=now+timedelta(seconds=LEASE_SECONDS),last_run_at=now,
            **({'next_run_at':CronSchedule(feed.cron,feed.timezone).next_after(now)} if feed.mode=='pull' else {}))
        if not locked:raise FeedBusy('数据源已有运行中的任务。')
        if old:
            # A failed delivery may retry the same idempotency key. Keep the old
            # run in history and transfer the unique key to its new attempt.
            old.request_key='';old.save(update_fields=['request_key'])
        feed.runs.filter(status='running').update(status='failed',finished_at=now,error='执行进程失去租约；已有发布版本保留，游标未由该任务推进。',payload={})
        run=FeedRun.objects.create(feed=feed,trigger=trigger,lease_token=token,payload=payload or {},
                                   payload_digest=signature,request_key=request_key)
    return run,True


def _check(run,started):
    if time.monotonic()-started>=MAX_RUN_SECONDS:raise ValueError('更新达到60分钟上限，整批未发布；原有版本与游标保留。')
    feed=KnowledgeFeed.objects.select_related('kb','created_by').get(pk=run.feed_id)
    authorize(feed)
    if feed.lease_token!=run.lease_token or not feed.lease_until or feed.lease_until<=timezone.now():raise ValueError('本次执行锁已失效，禁止继续发布。')
    return feed


def _tracker_values(item,run,feed):
    values={'标题':item['title']}
    values.update(item.get('fields',{}))
    if not item.get('fields'):values['内容']=item['content']
    values['更新时间']=item.get('updated_at','')
    values['来源链接']=item.get('source_url','')
    from zoneinfo import ZoneInfo
    values.setdefault('采集时间',run.started_at.astimezone(ZoneInfo(feed.timezone)).isoformat())
    values.setdefault('来源ID',item['id'])
    return values


def _tracker_labels(feed,payload):
    """列 = 标题 + 采集字段（或推送方自带 fields）+ 更新时间/来源链接。"""
    labels=['标题']
    for field in (feed.adapter_spec or {}).get('body_fields',[]):
        if field.get('label') and field['label'] not in labels:labels.append(field['label'])
    for item in (payload or {}).get('items',[]):
        if not item.get('fields') and '内容' not in labels:labels.append('内容')
        for key in item.get('fields',{}):
            if key not in labels:labels.append(key)
    labels+=[label for label in ('更新时间','来源链接','采集时间','来源ID') if label not in labels]
    return [{'label':label} for label in labels]


def _ensure_tracker(feed,labels):
    """定期抓取的知识库默认启用追踪表；已有自定义配置时只补列，不覆盖启用状态。"""
    try:tracker=feed.kb.tracker
    except KbTracker.DoesNotExist:return KbTracker.objects.create(kb=feed.kb,enabled=True,fields=labels,schema_version=1)
    current=[field.get('label') for field in tracker.fields]
    wanted=[field['label'] for field in labels]
    if not set(wanted).issubset(current):
        tracker.fields=list(tracker.fields)+ \
            [field for field in labels if field['label'] not in current]
        tracker.schema_version+=1
        tracker.save(update_fields=['fields','schema_version','updated_at'])
    return tracker


def _markdown(item):
    body=item['content']
    if item.get('format')=='text':
        fence='`'*(max([len(part) for part in re.findall(r'`+',body)]+[2])+1)
        body=fence+'\n'+body+'\n'+fence
    return '# '+item['title'].strip()+'\n\n'+body


def _document_markdown(item,feed):
    """Keep generated collection dates out of the versioned source text."""
    body=item['content']
    for field in (feed.adapter_spec or {}).get('body_fields',[]):
        if field.get('source') not in ('today','now'):continue
        label=field.get('label','')
        value=(item.get('fields') or {}).get(label)
        if not label or value is None:continue
        section=r'(?:\A|\n\n)## '+re.escape(label)+r'\n\n'+re.escape(str(value))+r'(?=\n\n## |\Z)'
        body=re.sub(section,'',body,count=1).strip()
    return _markdown({**item,'content':body})


def _document_digest(item,feed):
    """Deduplicate source text, including in append mode."""
    return digest(_document_markdown(item,feed))


def _document_name(item, doc_id):
    title=re.sub(r'[\\/:*?"<>|\x00-\x1f]+',' ',item['title']).strip(' .') or 'record'
    # FileField defaults to max_length=100. Leave room for "documents/" and
    # a storage collision suffix while keeping Windows path segments short.
    return f'{title[:40]}-{doc_id.hex}.md'


def _queue_document(doc_id):
    try:
        doc=Document.objects.get(pk=doc_id)
        if doc.status==Document.Status.COMPLETED:
            # Recover a worker interrupted between marking the document
            # complete and publishing its pending FeedRecord version.
            if finalize_feed_document(doc):
                from .tracker import run_extraction_async
                run_extraction_async(doc.id)
            return
        if doc.status!=Document.Status.PENDING:return
        from .pipeline import process_document_async
        process_document_async(str(doc_id))
    except Exception:
        # The persisted pending status is the durable queue. A later queue
        # startup can recover this document even if the wake-up fails here.
        logging.getLogger(__name__).exception('Could not wake document ingestion for %s',doc_id)


def _retire_document(doc, removed):
    if doc is None:return
    Document.objects.filter(pk=doc.pk).update(status=Document.Status.SUPERSEDED,
                                                 stage_detail='已由更新版本替代')
    removed.append(doc)


def _publish_document(feed, record, item, fingerprint, removed, created_files, queued):
    """Stage one real Markdown Document while retaining the current version."""
    record=FeedRecord.objects.select_for_update().select_related('kb','document','pending_document').get(pk=record.pk)
    was_created=record.document_id is None and record.pending_document_id is None
    if record.kb_id is None:
        record.kb=KnowledgeBase.objects.create(
            name=item['title'][:100],slug='feed-'+uuid.uuid4().hex,
            parent=feed.kb,is_folder=False,category=feed.kb.category,
            department=feed.kb.department,visibility=feed.kb.visibility,
            created_by=feed.created_by)
    elif record.kb.name!=item['title'][:100]:
        record.kb.name=item['title'][:100]
        record.kb.save(update_fields=['name','updated_at'])

    if record.document_id and record.content_digest==fingerprint and not record.deleted:
        if record.pending_document_id:
            _retire_document(record.pending_document,removed)
            record.pending_document=None;record.pending_digest=''
        return record,False,was_created

    if (record.pending_document_id and record.pending_digest==fingerprint and not record.deleted
            and record.pending_document.status!=Document.Status.SUPERSEDED):
        if record.pending_document.status==Document.Status.FAILED:
            Document.objects.filter(pk=record.pending_document_id).update(
                status=Document.Status.PENDING,error_msg='',stage_detail='等待重试')
            queued.append(record.pending_document_id)
        elif record.pending_document.status==Document.Status.COMPLETED:
            queued.append(record.pending_document_id)
        return record,False,was_created

    if record.pending_document_id:
        _retire_document(record.pending_document,removed)

    doc=Document(id=uuid.uuid4(),kb=record.kb,file_type='md',status=Document.Status.PENDING,
                 description=item.get('source_url','')[:2000])
    doc.original_name=_document_name(item,doc.id)
    doc.file.save(doc.original_name,ContentFile(_document_markdown(item,feed).encode('utf-8')),save=False)
    created_files.append((doc.file.storage,doc.file.name))
    doc.save()
    record.pending_document=doc;record.pending_digest=fingerprint
    queued.append(doc.id)
    return record,True,was_created


def _delete_document_record(record,removed):
    record=FeedRecord.objects.select_for_update().select_related('document','pending_document').get(pk=record.pk)
    if record.document_id:_retire_document(record.document,removed)
    if record.pending_document_id:_retire_document(record.pending_document,removed)
    TrackerRow.objects.filter(document_id__in=[pk for pk in (record.document_id,record.pending_document_id) if pk]).delete()
    record.document=None;record.pending_document=None
    record.content_digest='';record.pending_digest='';record.deleted=True
    record.save(update_fields=['document','pending_document','content_digest','pending_digest','deleted','updated_at'])
    if record.kb_id:
        KnowledgeBase.objects.filter(pk=record.kb_id).update(doc_count=0,chunk_count=0)


def _is_managed_feed_document(doc):
    # A user can still upload an ordinary document into a feed's child KB.
    return doc.original_name.endswith(f'-{doc.id.hex}.md')


def is_nonembedded_feed_document(doc):
    """Only cron-generated document-mode files bypass vector indexing.

    Ordinary uploads into the same child library still use the normal OCR and
    embedding pipeline. The FeedRecord pointer ties this exact file version to
    its source, instead of trusting the child library or filename alone.
    """
    if not _is_managed_feed_document(doc):return False
    record=(FeedRecord.objects.select_related('feed').filter(kb_id=doc.kb_id).first())
    return bool(record and record.feed.adapter_spec.get('ingest_mode')=='documents'
                and doc.id in (record.document_id,record.pending_document_id))


def is_current_feed_document(doc):
    """Queued historical versions must never be processed on a later retry."""
    if not _is_managed_feed_document(doc):return True
    record=FeedRecord.objects.filter(kb_id=doc.kb_id).only('document_id','pending_document_id','deleted').first()
    return bool(record and not record.deleted and doc.id in (record.document_id,record.pending_document_id))


def finalize_feed_document(doc):
    """Publish a staged version only after document processing succeeds.

    Returns False for a stale version so pipeline skips tracksheet extraction.
    """
    if not _is_managed_feed_document(doc):return True
    removed=[]
    with transaction.atomic():
        record=(FeedRecord.objects.select_for_update().select_related('document')
                .filter(kb_id=doc.kb_id).first())
        if record is None or record.deleted or doc.id not in (record.document_id,record.pending_document_id):
            _retire_document(doc,removed)
            active=False
        elif record.pending_document_id==doc.id:
            old=record.document if record.document_id!=doc.id else None
            record.document=doc;record.content_digest=record.pending_digest
            record.pending_document=None;record.pending_digest=''
            record.save(update_fields=['document','content_digest','pending_document','pending_digest','updated_at'])
            if old:
                _retire_document(old,removed)
                if record.feed.write_mode=='upsert':TrackerRow.objects.filter(document_id=old.id).delete()
            active=True
        else:
            active=True
        kb=doc.kb
        kb.doc_count=kb.documents.filter(status=Document.Status.COMPLETED).count()
        kb.chunk_count=sum(kb.documents.filter(status=Document.Status.COMPLETED).values_list('chunk_count',flat=True))
        kb.save(update_fields=['doc_count','chunk_count','updated_at'])
        for old in removed:
            transaction.on_commit(lambda old=old:_delete_vectors(old))
    return active


def _delete_vectors(doc):
    if not doc.chunk_count and not doc.kb.embedding_model:
        # Newly fetched text-only files never entered either search index.
        return
    try:
        from .retriever import delete_doc_vectors
        delete_doc_vectors(doc.kb.slug,doc.original_name)
    except Exception:
        logging.getLogger(__name__).exception('Could not remove superseded vectors for %s',doc.pk)


def _publish_row(feed,tracker,record,item,run):
    """结构化记录直接写追踪行，既不创建文档，也不落盘MD文件。"""
    legacy=TrackerRow.objects.filter(tracker=tracker,document_id=record.document_id).first() if record.document_id else None
    if legacy and not legacy.feed_id and feed.write_mode=='upsert':
        legacy.feed=feed;legacy.external_id=record.external_id;legacy.document=None
        legacy.save(update_fields=['feed','external_id','document','updated_at'])
    row,created=TrackerRow.objects.update_or_create(tracker=tracker,feed=feed,external_id=record.external_id,
        feed_run=run if feed.write_mode=='append' else None,defaults={'values':_tracker_values(item,run,feed),
        'proposed_values':{},'status':TrackerRow.Status.DONE,'error':'','schema_version':tracker.schema_version})
    if record.document_id:
        Document.objects.filter(pk=record.document_id).update(status=Document.Status.SUPERSEDED)
        record.document=None
    return created


def _delete_row(record,*,preserve_history=False):
    removed=[]
    if not preserve_history:TrackerRow.objects.filter(feed=record.feed,external_id=record.external_id,feed_run__isnull=True).delete()
    if record.document_id and not preserve_history:
        removed.append(record.document)
        TrackerRow.objects.filter(tracker__kb=record.feed.kb,document_id=record.document_id).delete()
        Document.objects.filter(pk=record.document_id).update(status=Document.Status.SUPERSEDED)
    record.document=None;record.deleted=True
    record.save(update_fields=['document','deleted','updated_at'])
    return removed


def execute(run_id):
    """Publish a complete batch, then commit the cursor and queue documents."""
    run=FeedRun.objects.select_related('feed__kb','feed__created_by').get(pk=run_id)
    if run.status!='running':return run
    started=time.monotonic();counts={'created':0,'updated':0,'deleted':0,'unchanged':0,'stale':0};removed=[]
    created_files=[];published=False
    try:
        feed=_check(run,started);etag=''
        document_mode=(feed.adapter_spec or {}).get('ingest_mode','tracker')=='documents'
        if run.trigger in ('push','import'):payload=validate_batch(run.payload)
        else:
            from .feed_http import fetch_batch
            feed._run_checkpoint=lambda:_check(run,started)
            payload,etag=fetch_batch(feed)
        actions=[]
        if payload is not None:
            warned=sum(bool(item.get('metadata',{}).get('collection_warning')) for item in payload['items'])
            if warned:counts['warnings']=warned
            for item in payload['items']:
                feed=_check(run,started)
                record=(FeedRecord.objects.filter(feed=feed,external_id=item['id']).first() if document_mode
                        else FeedRecord.objects.get_or_create(feed=feed,external_id=item['id'])[0])
                stamp=parse_datetime(item['updated_at']) if item.get('updated_at') else None
                fingerprint=(_document_digest(item,feed) if document_mode else
                    digest(canonical({key:item.get(key,{} if key in ('metadata','fields') else 'markdown' if key=='format' else '')
                        for key in ('title','content','format','source_url','metadata','fields')})))
                if feed.write_mode=='upsert' and record and stamp and record.upstream_updated_at and stamp<record.upstream_updated_at:
                    counts['stale']+=1;counts['unchanged']+=1;continue
                if not document_mode and feed.write_mode=='upsert' and not record.deleted and record.content_digest==fingerprint and TrackerRow.objects.filter(feed=feed,external_id=record.external_id,feed_run__isnull=True,status=TrackerRow.Status.DONE).exists():
                    counts['unchanged']+=1;actions.append((record,item,None,stamp));continue
                actions.append((record,item,fingerprint,stamp))
            _check(run,started)
        with transaction.atomic():
            current=_check(run,started)
            # Fencing update acquires a database write lock before publishing.
            if not KnowledgeFeed.objects.filter(pk=feed.pk,lease_token=run.lease_token,lease_until__gt=timezone.now()).update(updated_at=timezone.now()):raise ValueError('执行锁已失效。')
            tracker=None if document_mode else _ensure_tracker(feed,_tracker_labels(feed,payload))
            queued=[]
            for record,item,fingerprint,stamp in actions:
                if document_mode:
                    if record is None:record=FeedRecord.objects.create(feed=current,external_id=item['id'])
                    record,staged,new_record=_publish_document(
                        current,record,item,fingerprint,removed,created_files,queued)
                    counts['created' if new_record else 'updated']+=1 if staged else 0
                    if not staged:counts['unchanged']+=1
                    record.deleted=False
                elif fingerprint is not None:
                    counts['created' if _publish_row(current,tracker,record,item,run) else 'updated']+=1
                    record.content_digest=fingerprint;record.deleted=False
                record.title=item['title'];record.source_url=item.get('source_url','');record.metadata=item.get('metadata',{})
                if stamp:record.upstream_updated_at=stamp
                if document_mode:
                    record.save(update_fields=['kb','pending_document','pending_digest','deleted','title','source_url','metadata','upstream_updated_at','updated_at'])
                else:
                    record.save(update_fields=['document','content_digest','deleted','title','source_url','metadata','upstream_updated_at','updated_at'])
            for external_id in payload.get('deleted',[]) if payload is not None else []:
                record,new=FeedRecord.objects.get_or_create(feed=feed,external_id=external_id,defaults={'deleted':True})
                if not new and record.deleted:counts['unchanged']+=1;continue
                if not new:
                    if document_mode:_delete_document_record(record,removed)
                    else:removed.extend(_delete_row(record,preserve_history=current.write_mode=='append'))
                counts['deleted']+=1
            if document_mode:
                # A 304/empty incremental response may omit a document whose
                # earlier indexing failed or finished just before a crash.
                # Retry/finalize it without requiring the upstream to resend.
                stranded=(FeedRecord.objects.select_for_update().select_related('pending_document')
                          .filter(feed=current,deleted=False,pending_document__status__in=[
                              Document.Status.FAILED,Document.Status.COMPLETED]))
                for record in stranded:
                    pending=record.pending_document
                    if pending.status==Document.Status.FAILED:
                        Document.objects.filter(pk=pending.pk,status=Document.Status.FAILED).update(
                            status=Document.Status.PENDING,error_msg='',stage_detail='等待重试')
                    queued.append(pending.pk)
            fields={'lease_token':'','lease_until':None}
            if payload is not None and 'cursor' in payload:fields['cursor']=payload['cursor']
            if feed.mode=='pull':fields['etag']=etag
            KnowledgeFeed.objects.filter(pk=feed.pk,lease_token=run.lease_token).update(**fields)
            KnowledgeBase.objects.filter(pk=current.kb_id).update(
                doc_count=Document.objects.filter(kb=current.kb_id,status=Document.Status.COMPLETED).count())
            counts['write_mode']=current.write_mode
            run.status='completed';run.counts=counts;run.finished_at=timezone.now();run.payload={}
            run.save(update_fields=['status','counts','finished_at','payload'])
            if document_mode:
                for doc_id in dict.fromkeys(queued):transaction.on_commit(lambda doc_id=doc_id:_queue_document(doc_id))
                for old in removed:transaction.on_commit(lambda old=old:_delete_vectors(old))
        published=True
        # 删除的记录可能残留历史向量；清理尽力而为。
        if not document_mode:
            try:
                from .retriever import delete_doc_vectors
                for old in removed:
                    try:delete_doc_vectors(old.kb.slug,old.original_name)
                    except Exception:pass
            except Exception:pass
    except Exception as exc:
        if not published:
            for storage,name in created_files:
                try:storage.delete(name)
                except Exception:logging.getLogger(__name__).exception('Could not clean rolled-back document file %s',name)
        run.status='failed';run.error=(str(exc) if isinstance(exc,ValueError) else '更新失败（'+type(exc).__name__+'），原有版本与游标保留。')[:1000]
        run.finished_at=timezone.now();run.payload={};run.counts={}
        run.save(update_fields=['status','error','finished_at','payload','counts'])
        KnowledgeFeed.objects.filter(pk=run.feed_id,lease_token=run.lease_token).update(lease_token='',lease_until=None)
    if run.status=='completed':
        try:
            from .feed_reports import after_run
            after_run(run)
        except Exception:
            # Reporting failures must not undo a committed collection or replay it.
            import logging
            logging.getLogger(__name__).exception('Report delivery failed for completed feed run %s',run.pk)
    return run


def start_async(run_id):
    def worker():
        close_old_connections()
        try:execute(run_id)
        finally:close_old_connections()
    threading.Thread(target=worker,name='knowledge-feed-'+str(run_id),daemon=True).start()


def run_due(now=None):
    now=now or timezone.now();results=[]
    for feed in KnowledgeFeed.objects.exclude(lease_token='').filter(lease_until__lte=now):
        with transaction.atomic():
            released=KnowledgeFeed.objects.filter(pk=feed.pk,lease_token=feed.lease_token,lease_until__lte=now).update(lease_token='',lease_until=None)
            if released:feed.runs.filter(lease_token=feed.lease_token,status='running').update(status='failed',finished_at=now,payload={},error='执行进程已停止或租约超时，原有版本与游标保留。')
    ids=list(KnowledgeFeed.objects.filter(enabled=True,mode='pull',next_run_at__lte=now).values_list('pk',flat=True)[:100])
    for feed_id in ids:
        try:run,fresh=claim(feed_id,trigger='cron')
        except FeedBusy:continue
        except KnowledgeFeed.DoesNotExist:continue
        except ValueError as exc:
            # A revoked owner or invalid configuration must be visible and must
            # not produce an unbounded silent retry on every scheduler tick.
            KnowledgeFeed.objects.filter(pk=feed_id).update(enabled=False)
            results.append(FeedRun.objects.create(feed_id=feed_id,trigger='cron',status='failed',lease_token='',
                                   error=str(exc)[:1000],finished_at=timezone.now()))
            continue
        if fresh:results.append(execute(run.pk))
    return results
