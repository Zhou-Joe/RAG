from __future__ import annotations

"""验证按文档先过滤再排序，防止同库其它资料挤占聚焦抽取名额。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase
from langchain_core.documents import Document

from kb import keyword_index


class KeywordSourceScopeTests(SimpleTestCase):
    def test_source_filter_is_applied_before_ranking_and_limit(self):
        with TemporaryDirectory() as temporary, patch.object(keyword_index,'_SCHEMA_READY',False), \
                patch.object(keyword_index,'_db_path',return_value=Path(temporary)/'keyword.sqlite3'):
            keyword_index.add_chunks('scoped',[Document(page_content='验收要求。',metadata={'source':'target.pdf'})],ids=['target'],strict=True)
            keyword_index.add_chunks('scoped',[Document(page_content='验收要求。'*30,metadata={'source':'other.pdf'}) for i in range(25)],
                                     ids=[f'other-{i}' for i in range(25)],strict=True)
            self.assertEqual(keyword_index.search('scoped','验收',limit=1)[0]['source'],'other.pdf')
            result=keyword_index.search('scoped','验收',limit=1,source='target.pdf')
            self.assertEqual([r['chunk_id'] for r in result],['target'])
            self.assertEqual(keyword_index.search('scoped','验收',source='missing.pdf'),[])
            self.assertEqual([r['chunk_id'] for r in keyword_index.search(
                'scoped', '验收', limit=1, sources=['target.pdf'])], ['target'])
            self.assertEqual(keyword_index.search('scoped','验收',sources=[]),[])
