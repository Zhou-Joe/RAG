"""Exercise card eligibility and exact source quantities without database I/O."""
import copy
import json
import sys
import types
import unittest
from unittest.mock import patch

from kb.value_cards import attach_value_card, grounded_value_card, grounded_card_current


class ValueCardRuleTests(unittest.TestCase):
    def attach(self, result, **params):
        defaults = dict(columns=['Temperature'], metrics=None, group_by=None, order_by=None,
                        limit=20, offset=0, title='Shanghai temperature', unit='°C', cap=10000)
        defaults.update(params)
        # The only dependency is the query module's JSON encoder. Use the same
        # encoding contract so these pure rules do not require Django startup.
        encoder = types.SimpleNamespace(encode=lambda data: json.dumps(
            data, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
        with patch.dict(sys.modules, {'kb.tracker_query': encoder}):
            return attach_value_card(result, **defaults)

    def rows(self, value=0, total=1):
        return {'kind': 'tracker_rows', 'total_matched': total, 'rows': [{'values': {'Temperature': value}}]}

    def snapshot(self, text='Vehicle A rated load is 2,500 kg.'):
        return dict(kind='text', doc_id='doc', chunk_id='load', document_digest='document-sha',
                    chunk_digest='chunk-sha', text=text)

    def test_unique_zero_gets_only_a_title_value_and_unit(self):
        result = self.attach(self.rows())
        self.assertEqual(result['chart'], dict(type='metric', title='Shanghai temperature', value=0, unit='°C'))
        self.assertEqual(result['presentation'], 'value_card')
        self.assertIn('return only', result['presentation_notice'])

    def test_latest_value_requires_an_explicit_sort(self):
        result = self.rows(total=5)
        self.assertNotIn('chart', self.attach(result, limit=1))
        self.assertEqual(self.attach(result, limit=1, order_by=[{'field': 'Date', 'direction': 'desc'}])['chart']['value'], 0)

    def test_samples_multiple_columns_and_later_pages_are_not_kpis(self):
        for params in (dict(columns=None), dict(columns=['Temperature', 'Location']), dict(offset=1)):
            self.assertNotIn('chart', self.attach(self.rows(), **params))
        self.assertNotIn('chart', self.attach(self.rows(total=10)))

    def test_null_boolean_nonfinite_and_truncated_cells_do_not_become_zero(self):
        for value in (None, False, True, 'NaN', 'Infinity', 'N/A', {'text': '10', 'truncated': True}):
            with self.subTest(value=value):
                self.assertNotIn('chart', self.attach(self.rows(value)))

    def test_numeric_strings_preserve_the_actual_value(self):
        self.assertEqual(self.attach(self.rows('-2.5'))['chart']['value'], -2.5)

    def test_only_one_ungrouped_metric_is_a_single_card(self):
        result = {'kind': 'tracker_summary', 'groups': [{'metrics': {'Count': 0}}]}
        metric = [{'op': 'count', 'as': 'Count'}]
        self.assertEqual(self.attach(result, metrics=metric, unit='')['chart']['value'], 0)
        self.assertNotIn('chart', self.attach(result, metrics=metric, group_by=[{'field': 'Location'}]))
        self.assertNotIn('chart', self.attach(result, metrics=metric * 2))

    def test_optional_card_never_exceeds_the_text_budget(self):
        self.assertNotIn('chart', self.attach(self.rows(), cap=100))

    def test_document_quantity_is_bound_to_the_exact_quote_and_source(self):
        snapshot = self.snapshot()
        card = grounded_value_card(snapshot, title='Rated load', value=2500, unit='kg', quote=snapshot['text'])
        self.assertTrue(grounded_card_current(card, snapshot))
        altered = copy.deepcopy(card); altered['chart']['value'] = 42
        self.assertFalse(grounded_card_current(altered, snapshot))
        newer = dict(snapshot, document_digest='new-document')
        self.assertFalse(grounded_card_current(card, newer))

    def test_quantity_cannot_be_converted_or_matched_to_another_units_value(self):
        snapshot = self.snapshot('Pressure: 20 psi (1.4 bar).')
        for value, unit, quote in ((20, 'bar', snapshot['text']), (140, 'kPa', snapshot['text']),
                                   (20, 'psi', 'Invented quote: 20 psi')):
            with self.subTest(value=value, unit=unit), self.assertRaises(ValueError):
                grounded_value_card(snapshot, title='Pressure', value=value, unit=unit, quote=quote)

    def test_chinese_quantity_needs_no_space_after_its_field_name(self):
        snapshot = self.snapshot('额定载荷2500kg。')
        card = grounded_value_card(snapshot, title='额定载荷', value=2500, unit='kg', quote=snapshot['text'])
        self.assertEqual(card['chart']['value'], 2500)

    def test_only_text_passages_can_support_this_numeric_tool(self):
        snapshot = dict(self.snapshot(), kind='image')
        with self.assertRaises(ValueError):
            grounded_value_card(snapshot, title='Load', value=2500, unit='kg', quote=snapshot['text'])
