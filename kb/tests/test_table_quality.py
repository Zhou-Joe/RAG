from django.test import SimpleTestCase, override_settings
from unittest.mock import patch
from kb.pipeline import _html_table_to_text, _md_for_embedding, _embeddings


class TableQualityTests(SimpleTestCase):
    def test_quoted_missing_reordered_attributes_and_headers(self):
        value = '<TABLE class="report"><tr><th>Part</th><th>Interval</th></tr><tr><td class="x" colspan="1" rowspan="1">Valve<br>assembly</td><td>Daily &amp; before use</td></tr></TABLE>'
        result = _md_for_embedding(value)
        self.assertIn('Part | Interval', result)
        self.assertIn('Valve assembly | Daily & before use', result)

    def test_carried_columns_do_not_truncate_last_cell(self):
        value = '<table><tr><td rowspan="2">A</td><td>B</td></tr><tr><td>C</td><td>D</td></tr></table>'
        result = _html_table_to_text(value)
        self.assertIn('A | C | D', result)

    def test_invalid_span_is_not_silently_discarded(self):
        for span in ('bad', '-1', '100000'):
            with self.assertRaises(ValueError):
                _html_table_to_text(f'<table><tr><td colspan="{span}">Required fact</td></tr></table>')

    def test_omitted_row_and_cell_end_tags_follow_html_rules(self):
        # MinerU output seen on FL-8B electrical tables: last row lacks </tr>.
        last_row = _html_table_to_text('<table><tr><td>44</td><td>B2</td></tr><tr><td>45</td><td>JB2</td></table>')
        self.assertEqual(last_row, '44 | B2\n45 | JB2')
        cells = _html_table_to_text('<table><tr><td>A<td>B<tr><td>C<td>D</table>')
        self.assertEqual(cells, 'A | B\nC | D')

    def test_truncated_or_nested_table_is_still_rejected(self):
        from kb.table_structure import parse_table
        with self.assertRaisesMessage(ValueError, '表格 HTML 不完整'):
            parse_table('<table><tr><td>Required fact</td>')
        with self.assertRaises(ValueError):
            _html_table_to_text('<table><tr><td><table><tr><td>x</td></tr></table></td></tr></table>')

    @override_settings(EMBEDDING_BATCH_SIZE=16)
    def test_local_endpoint_uses_explicit_safe_batch(self):
        with patch('kb.pipeline.embedding_settings', return_value={'model':'BAAI/bge-m3','base_url':'http://127.0.0.1:8766/v1','api_key':'local','dimensions':1024}):
            self.assertEqual(_embeddings().chunk_size,16)


class QueryPlanTests(SimpleTestCase):
    def test_identifiers_are_not_rewritten(self):
        from kb.query_plan import parse_plan
        self.assertIsNone(parse_plan('{"standalone":"AB-124 interval","action":"new","output":"text","subquestions":[]}', 'AB-123 interval'))

    def test_followup_and_table_request_are_explicit(self):
        from kb.query_plan import parse_plan
        p = parse_plan('{"standalone":"Valve inspection intervals","action":"format","output":"table","subquestions":[]}', '以上整理成表格')
        self.assertEqual(p.action,'format')
        self.assertEqual(p.output,'table')

class GlobalEvaluationTests(SimpleTestCase):
    def test_hit_rank_is_global_not_per_library(self):
        from types import SimpleNamespace
        from kb.eval import run_eval
        q = SimpleNamespace(question='check', expected_source='B.pdf', expected_keyword='daily')
        results = [{'kb_slug':'a','source':'A.pdf','text':'other','score':1},
                   {'kb_slug':'b','source':'B.pdf','text':'daily','score':.9}]
        with patch('kb.eval._doc_libs', return_value=[SimpleNamespace(slug='a',name='A'),SimpleNamespace(slug='b',name='B')]), patch('kb.eval.EvalQuestion.objects.all', return_value=[q]), patch('kb.eval.search_folder', return_value=results):
            report=run_eval()
        self.assertEqual(report['questions'][0]['rank_hit'],2)
        self.assertEqual(report['summary']['hit1'],0)
        self.assertEqual(report['summary']['hit3'],1)
