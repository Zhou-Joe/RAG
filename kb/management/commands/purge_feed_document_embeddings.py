"""Remove old search indexes from cron-generated document-mode files.

The current ingestion path stores these files as readable Markdown and uses
tracksheet AI extraction, without embedding them. This command cleans versions
indexed before that behavior changed. It is safe to rerun.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Sum

from kb import keyword_index
from kb.feeds import _is_managed_feed_document
from kb.models import ChunkProvenance, Document, FeedRecord, KnowledgeBase, KnowledgeFeed
from kb.retriever import delete_doc_vectors


class Command(BaseCommand):
    help = "Remove vector and keyword entries for cron-generated document-mode feed files."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count matching files without changing them.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        files = libraries = cleaned = 0
        failures = []
        feeds = KnowledgeFeed.objects.filter(adapter_spec__ingest_mode="documents")
        for feed in feeds.iterator():
            records = (FeedRecord.objects.filter(feed=feed, kb__isnull=False)
                       .select_related("kb"))
            for record in records.iterator():
                kb = record.kb
                generated = [doc for doc in Document.objects.filter(kb=kb).iterator()
                             if _is_managed_feed_document(doc)]
                if not generated:
                    continue
                libraries += 1
                files += len(generated)
                if dry_run:
                    continue
                for doc in generated:
                    # Delete by source name, never by entire child collection:
                    # a user may also have uploaded an ordinary file there.
                    try:
                        if (doc.chunk_count or kb.embedding_model
                                or (Path(settings.CHROMA_ROOT) / kb.slug).exists()):
                            if delete_doc_vectors(kb.slug, doc.original_name) is False:
                                raise RuntimeError("Chroma vector deletion failed")
                        else:
                            # Fresh text-only files have no Chroma collection.
                            if keyword_index.delete_doc(kb.slug, doc.original_name) is False:
                                raise RuntimeError("keyword index deletion failed")
                    except Exception as exc:
                        failures.append(f"{kb.slug}/{doc.original_name}: {exc}")
                        # Preserve the old count so a rerun still signals that
                        # this source needs cleanup.
                        continue
                    ChunkProvenance.objects.filter(document=doc).delete()
                    updates = {"chunk_count": 0}
                    if doc.status == Document.Status.COMPLETED:
                        updates["stage_detail"] = "已保存采集原文 · 未向量化"
                    Document.objects.filter(pk=doc.pk).update(**updates)
                    cleaned += 1
                remaining = (Document.objects.filter(kb=kb, status=Document.Status.COMPLETED)
                             .aggregate(total=Sum("chunk_count"))["total"] or 0)
                fields = {"chunk_count": remaining}
                if not Document.objects.filter(kb=kb, chunk_count__gt=0).exists():
                    fields.update(embedding_model="", embedding_dimensions=None)
                KnowledgeBase.objects.filter(pk=kb.pk).update(**fields)
        if dry_run:
            self.stdout.write(f"Would clean {files} generated documents across {libraries} child libraries.")
        else:
            self.stdout.write(f"Cleaned {cleaned} of {files} generated documents across {libraries} child libraries.")
        if failures:
            raise CommandError(f"Failed to clean {len(failures)} generated documents: "
                               + "; ".join(failures[:3]))
