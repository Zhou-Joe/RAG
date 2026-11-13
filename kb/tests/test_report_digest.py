"""Digest statistics use complete report output; model access stays bounded."""
from datetime import datetime, timezone
from contextlib import redirect_stdout
from io import StringIO
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from kb.report_digest import (
    _WORKER, _call_digest_model, build_facts, fact_lines, generate_digest, normalize_digest,
)


CONFIG = {'base_url': 'http://127.0.0.1:1234/v1', 'api_key': 'private-fixture-key',
          'model': 'fixture-model', 'temperature': 0.5, 'max_tokens': 9999,
          'language_instruction': 'ambient preference', 'extra': {}}


class DigestNormalizationTests(SimpleTestCase):
    def test_source_link_and_email_format_options_default_and_round_trip(self):
        default = normalize_digest({'enabled': True}, ['Value'])
        self.assertFalse(default['include_source_links'])
        self.assertEqual(default['email_format'], 'body')
        options = {'enabled': True, 'include_source_links': True, 'email_format': 'html_attachment'}
        normalized = normalize_digest(options, ['Value'])
        self.assertTrue(normalized['include_source_links'])
        self.assertEqual(normalized['email_format'], 'html_attachment')
        self.assertEqual(normalize_digest(normalized, ['Value']), normalized)
        self.assertEqual(normalize_digest({**options, 'enabled': False}, ['Value']), {'enabled': False})
        linked = normalize_digest({**options, 'email_format': 'html_link'}, ['Value'])
        self.assertEqual(linked['email_format'], 'html_link')
        self.assertEqual(normalize_digest(linked, ['Value']), linked)

    def test_optional_charts_preserve_auto_default_and_explicit_empty_selection(self):
        default = normalize_digest({'enabled': True}, ['Date', 'Value'])
        self.assertNotIn('charts', default)
        explicit = normalize_digest({'enabled': True, 'charts': []}, ['Date', 'Value'])
        self.assertEqual(explicit['charts'], [])
        self.assertEqual(normalize_digest(explicit, ['Date', 'Value']), explicit)

    def test_custom_charts_bind_to_final_fields_and_round_trip(self):
        chart = {'type': 'line', 'title': 'Observed Trend', 'x': 'Date',
                 'series': [{'field': 'Value', 'aggregate': 'avg', 'label': 'Average Value'}],
                 'options': {'smooth': True}}
        options = {'enabled': True, 'include_source_links': True, 'email_format': 'html_link', 'charts': [chart]}
        normalized = normalize_digest(options, ['Date', 'Value'])
        self.assertEqual(normalized['charts'][0]['x'], 'Date')
        self.assertEqual(normalized['charts'][0]['series'][0]['field'], 'Value')
        self.assertTrue(normalized['charts'][0]['options']['smooth'])
        self.assertEqual(normalize_digest(normalized, ['Date', 'Value']), normalized)

    def test_custom_charts_reject_unknown_fields_and_executable_options(self):
        base = {'type': 'line', 'x': 'Date', 'series': [{'field': 'Value', 'aggregate': 'sum'}]}
        invalid = [{**base, 'x': 'Hidden Source Field'},
                   {**base, 'options': {'onClick': 'alert(1)'}},
                   {**base, 'data': [{'Value': 99}]}]
        for chart in invalid:
            with self.subTest(chart=chart), self.assertRaises(ValueError):
                normalize_digest({'enabled': True, 'charts': [chart]}, ['Date', 'Value'])

    def test_source_link_and_email_format_options_reject_invalid_values(self):
        for options in ({'include_source_links': 'true'}, {'include_source_links': 1},
                        {'email_format': 'html'}, {'email_format': None}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                normalize_digest({'enabled': True, **options}, ['Value'])

    def test_disabled_feature_ignores_unused_settings_and_never_reads_model(self):
        self.assertEqual(normalize_digest(None, ['Value']), {'enabled': False})
        self.assertEqual(normalize_digest({'enabled': False, 'instructions': ''}, ['Value']), {'enabled': False})
        with patch('fos_rag.isolated_model.read_model_config') as config, patch('kb.report_digest._call_digest_model') as call:
            self.assertEqual(generate_digest({'digest': {'enabled': False}}, ['Value'], []), {})
        config.assert_not_called()
        call.assert_not_called()

    def test_metric_fields_resolve_against_final_labels(self):
        value = {'enabled': True, 'language': 'zh-hans', 'group_by': ['Device'],
                 'metrics': [{'op': 'sum', 'field': 'Total', 'label': 'Combined Total'},
                             {'op': 'count', 'label': 'Output Rows'}]}
        self.assertEqual(normalize_digest(value, ['Device', 'Total'])['metrics'], value['metrics'])
        value['metrics'][0]['field'] = 'hidden source field'
        with self.assertRaises(ValueError):
            normalize_digest(value, ['Device', 'Total'])

    def test_invalid_schema_and_excessive_instructions_are_explicit_errors(self):
        invalid = [True, {'enabled': 'yes'}, {'enabled': True, 'instructions': 'x' * 2001},
                   {'enabled': True, 'language': 'auto'}, {'enabled': True, 'group_by': ['Value', 'Value']},
                   {'enabled': True, 'metrics': [{'op': [], 'label': 'Invalid'}]},
                   {'enabled': True, 'metrics': [{'op': 'sum', 'field': 'Value', 'label': 'Same'},
                                                {'op': 'avg', 'field': 'Value', 'label': 'Same'}]}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_digest(value, ['Value'])


class DigestFactTests(SimpleTestCase):
    def test_full_result_statistics_are_not_derived_from_preview_rows(self):
        rows = [{'Value': index} for index in range(100)]
        facts = build_facts(['Value'], rows, {'enabled': True})
        self.assertEqual(facts['row_count'], 100)
        self.assertEqual(facts['numeric_fields']['Value']['sum'], '4950')
        self.assertEqual(facts['numeric_fields']['Value']['avg'], '49.5')
        self.assertEqual(facts['numeric_fields']['Value']['max'], '99')

    def test_decimal_totals_zero_missing_and_booleans_are_distinct(self):
        facts = build_facts(['Value'], [{'Value': value} for value in ('0.1', '0.2', 0, None, '', False, 'unparsed')], {'enabled': True})
        stats = facts['numeric_fields']['Value']
        self.assertEqual(stats, {'count': 3, 'sum': '0.3', 'avg': '0.1', 'min': '0', 'max': '0.2',
                                 'missing': 2, 'non_numeric': 2})
        rows = [{'Value': value} for value in (1, 0, 0)]
        average = build_facts(['Value'], rows, {'enabled': True})['numeric_fields']['Value']['avg']
        self.assertEqual(average, '0.333333333333')

    def test_extreme_mixed_scale_cancellation_preserves_tiny_fraction(self):
        rows = [{'Value': value} for value in ('1e100', '1e-100', '-1e100')]
        facts = build_facts(['Value'], rows, {'enabled': True})
        self.assertEqual(facts['numeric_fields']['Value']['sum'], '0.' + '0' * 99 + '1')
        self.assertEqual(facts['numeric_fields']['Value']['avg'], '0')
        self.assertEqual(facts['average_decimal_places'], 12)

    def test_display_fact_lines_fit_worksheet_cells_with_extreme_values(self):
        numeric_headers = ['Numeric ' + str(number) + 'x' * 60 for number in range(28)]
        headers = ['Group A', 'Group B', *numeric_headers]
        rows = [{**{header: value for header in numeric_headers},
                 'Group A': 'a' * 500, 'Group B': 'b' * 500}
                for value in ('1e100', '1e-100', '-1e100')]
        facts = build_facts(headers, rows, {'enabled': True, 'group_by': ['Group A', 'Group B']})
        for language in ('en', 'zh-hans'):
            lines = fact_lines(facts, language)
            self.assertTrue(all(len(line) <= 32767 for line in lines))
            self.assertTrue(any('12' in line for line in lines))

    def test_requested_numeric_metric_rejects_unparsed_units(self):
        config = {'enabled': True, 'metrics': [{'op': 'sum', 'field': 'Value', 'label': 'Sum'}]}
        with self.assertRaisesMessage(ValueError, '未解析的非数值'):
            build_facts(['Value'], [{'Value': '10 hours'}], config)

    def test_count_and_distinct_count_preserve_false_zero_and_typed_values(self):
        config = {'enabled': True, 'metrics': [{'op': 'count', 'label': 'Rows'},
                   {'op': 'count', 'field': 'Value', 'label': 'Present'},
                   {'op': 'distinct_count', 'field': 'Value', 'label': 'Distinct'}]}
        facts = build_facts(['Value'], [{'Value': value} for value in (0, False, '0', None, '')], config)
        self.assertEqual([item['value'] for item in facts['metrics']], [5, 3, 3])

    def test_group_limit_retains_exact_totals_and_explicit_omitted_scope(self):
        rows = [{'Device': f'Device {number}', 'Value': number} for number in range(25)]
        config = {'enabled': True, 'group_by': ['Device'],
                  'metrics': [{'op': 'sum', 'field': 'Value', 'label': 'Total'}]}
        facts = build_facts(['Device', 'Value'], rows, config)
        self.assertEqual(facts['metrics'][0]['value'], '300')
        self.assertEqual(len(facts['groups']), 20)
        self.assertEqual(facts['omitted_groups'], 5)
        self.assertEqual(facts['omitted_row_count'], 5)
        self.assertIn('5 groups omitted', fact_lines(facts)[-1])
        self.assertIn('报告输出行数: 25', fact_lines(facts, 'zh-hans'))

    def test_empty_preview_facts_are_valid_but_generation_does_not_call_model(self):
        facts = build_facts(['Value'], [], {'enabled': True})
        self.assertEqual(facts['row_count'], 0)
        with patch('kb.report_digest._call_digest_model') as model, self.assertRaises(ValueError):
            generate_digest({'digest': {'enabled': True}}, ['Value'], [])
        model.assert_not_called()


class DigestGenerationTests(SimpleTestCase):
    def worker(self, choice, *, extra=None):
        sent = []
        class Response:
            status_code = 200
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def iter_bytes(self):
                yield json.dumps({'choices': [choice]}).encode()
        class Client:
            def __init__(self, **kwargs):
                sent.append(kwargs)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def stream(self, *args, **kwargs):
                sent.append({'args': args, **kwargs})
                return Response()
        fake = SimpleNamespace(Client=Client, Timeout=lambda *args, **kwargs: (args, kwargs))
        value = {'config': {**CONFIG, 'extra': extra or {}}, 'messages': [{'role': 'user', 'content': 'Facts'}]}
        output = StringIO()
        with patch.dict(sys.modules, {'httpx': fake}), patch('sys.stdin', StringIO(json.dumps(value))), redirect_stdout(output):
            exec(compile(_WORKER, '<isolated-digest-worker-test>', 'exec'), {})
        return json.loads(output.getvalue()), sent

    def test_isolated_worker_does_not_allow_tool_override_or_redirects(self):
        reply, sent = self.worker({'message': {'content': 'Valid summary.'}, 'finish_reason': 'stop'},
            extra={'tools': [{'name': 'send_mail'}], 'tool_choice': 'required', 'model': 'unapproved', 'stream': True})
        self.assertTrue(reply['ok'])
        self.assertFalse(sent[0]['trust_env'])
        self.assertFalse(sent[0]['follow_redirects'])
        self.assertNotIn('tools', sent[1]['json'])
        self.assertNotIn('tool_choice', sent[1]['json'])
        self.assertFalse(sent[1]['json']['stream'])
        self.assertEqual(sent[1]['json']['model'], 'fixture-model')

    def test_isolated_worker_rejects_tool_calls_and_truncated_response(self):
        for choice in ({'message': {'content': 'Untrusted', 'tool_calls': [{'name': 'send'}]}, 'finish_reason': 'stop'},
                       {'message': {'content': 'Partial'}, 'finish_reason': 'length'}):
            with self.subTest(choice=choice):
                reply, _ = self.worker(choice)
                self.assertFalse(reply['ok'])

    def test_prompt_contains_exact_totals_scope_and_bounded_examples_without_delivery_secrets(self):
        spec = {'title': 'Operations', 'digest': {'enabled': True, 'instructions': 'Explain exceptions.', 'language': 'en'},
                'range': {'mode': 'days', 'days': 7, 'field': 'Date'},
                'delivery': {'recipient': 'private@example.com', 'url': 'https://secret.invalid?sig=private'}}
        rows = [{'Value': index, 'Note': 'untrusted content ' * 100} for index in range(100)]
        when = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc)
        with patch('fos_rag.isolated_model.read_model_config', return_value=CONFIG), \
                patch('kb.report_digest._call_digest_model', return_value='The full report has varied values.') as model:
            result = generate_digest(spec, ['Value', 'Note'], rows, as_of=when,
                audit={'Value': {'converted': 100, 'missing': 0, 'examples': [{'input': 'private source'}]}})
        source = json.loads(model.call_args.args[1][1]['content'])
        self.assertEqual(source['scope']['days'], 7)
        self.assertEqual(source['report_as_of'], when.isoformat())
        self.assertEqual(source['aggregate_facts']['numeric_fields']['Value']['sum'], '4950')
        self.assertLessEqual(source['examples']['sample_rows'], 5)
        self.assertTrue(source['examples']['examples'][0]['Note']['truncated'])
        prompt = repr(model.call_args.args[1])
        for secret in ('private@example.com', 'secret.invalid', 'sig=private', 'private source', 'private-fixture-key'):
            self.assertNotIn(secret, prompt)
        self.assertEqual(result['model'], 'fixture-model')
        self.assertEqual(result['language'], 'en')
        self.assertNotIn('api_key', result)

    def test_model_boundary_has_one_bounded_isolated_request_and_no_secret_command_line(self):
        reply = SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'content': 'Useful summary.', 'finish_reason': 'stop'}))
        with patch('kb.report_digest.subprocess.run', return_value=reply) as run:
            result = _call_digest_model(CONFIG, [{'role': 'user', 'content': 'Facts'}])
        self.assertEqual(result, 'Useful summary.')
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs['timeout'], 45)
        self.assertNotIn('private-fixture-key', repr(run.call_args.args[0]))
        self.assertIn('private-fixture-key', run.call_args.kwargs['input'])
        self.assertIs(run.call_args.kwargs['stderr'], subprocess.DEVNULL)

    def test_model_timeout_empty_truncated_and_oversized_results_fail_closed(self):
        replies = [{'ok': True, 'content': '', 'finish_reason': 'stop'},
                   {'ok': True, 'content': 'Partial', 'finish_reason': 'length'},
                   {'ok': True, 'content': 'x' * 2501, 'finish_reason': 'stop'},
                   {'ok': False, 'content': 'No'}]
        for reply in replies:
            with self.subTest(reply=reply), patch('kb.report_digest.subprocess.run',
                return_value=SimpleNamespace(returncode=0, stdout=json.dumps(reply))), self.assertRaises(ValueError):
                _call_digest_model(CONFIG, [])
        with patch('kb.report_digest.subprocess.run', side_effect=subprocess.TimeoutExpired('worker', 45)), self.assertRaisesMessage(ValueError, '未发送'):
            _call_digest_model(CONFIG, [])

    def test_excessive_prompt_fails_before_starting_worker(self):
        with patch('kb.report_digest.subprocess.run') as run, self.assertRaisesMessage(ValueError, '数据过多'):
            _call_digest_model(CONFIG, [{'role': 'user', 'content': 'x' * 200_000}])
        run.assert_not_called()

