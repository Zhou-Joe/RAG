from django.test import SimpleTestCase

from kb.report_chart_specs import build_charts, chart_assets, chart_styles, normalize_charts, render_charts_html


class ReportChartSpecTests(SimpleTestCase):
    def spec(self, **changes):
        return {'type': 'line', 'title': 'Wait by day', 'x': 'Day',
                'series': [{'field': 'Wait', 'aggregate': 'avg'}], **changes}

    def test_default_dashboard_has_four_views_and_uses_all_matching_rows(self):
        rows = [{'Day': '2026-01-01', 'Wait': 0}, {'Day': '2026-01-01', 'Wait': 4},
                {'Day': '2026-01-02', 'Wait': -3}, {'Day': '2026-01-03', 'Wait': None}]
        charts = build_charts(['Day', 'Wait'], rows)
        self.assertEqual([chart['type'] for chart in charts], ['line', 'bar', 'pie', 'combo'])
        self.assertEqual(charts[0]['series'][0]['values'], [2, -3, None])
        self.assertEqual(charts[1]['series'][0]['values'], [4, -3, None])
        self.assertEqual(charts[2]['series'][0]['values'], [2, 1, 1])
        self.assertEqual(charts[3]['series'][0]['values'], [2, 1, 1])
        self.assertEqual(charts[3]['series'][1]['axis'], 'right')
        self.assertEqual(charts[0]['meta']['row_count'], 4)

    def test_metric_card_uses_one_full_result_aggregate_and_preserves_missing_zero(self):
        definition = {'type': 'metric', 'title': '<Total Wait>', 'x': None,
                      'series': [{'field': 'Wait', 'aggregate': 'avg'}]}
        rows = [{'Day': 'A', 'Wait': 0}, {'Day': 'A', 'Wait': 6}, {'Day': 'B', 'Wait': 12}]
        chart = build_charts(['Day', 'Wait'], rows, [definition])[0]
        self.assertEqual(chart['type'], 'metric')
        self.assertEqual(chart['value'], 6)
        self.assertEqual(chart['quality'], {'records': 3, 'valid': 3, 'missing': 0})
        self.assertEqual(chart['meta']['omitted_groups'], 0)
        self.assertNotIn('series', chart)
        self.assertIn('report-chart--metric', render_charts_html([chart]))
        self.assertIn('&lt;Total Wait&gt;', render_charts_html([chart]))
        self.assertIn('.erc-metric-value', chart_styles())
        self.assertEqual(build_charts(['Day', 'Wait'], [{'Wait': 0}], [definition])[0]['value'], 0)
        self.assertIsNone(build_charts(['Day', 'Wait'], [{'Wait': None}], [definition])[0]['value'])
        count = {**definition, 'series': [{'aggregate': 'count'}]}
        self.assertEqual(build_charts(['Day', 'Wait'], [], [count])[0]['value'], 0)

    def test_metric_card_config_rejects_grouping_multiple_series_and_literal_values(self):
        valid = {'type': 'metric', 'x': None, 'series': [{'aggregate': 'count'}]}
        normalized = normalize_charts([valid], ['Day', 'Wait'])
        self.assertEqual(normalize_charts(normalized, ['Day', 'Wait']), normalized)
        for changes in ({'x': 'Day'}, {'series': [{'aggregate': 'count'}] * 2}, {'value': 9000}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_charts([{**valid, **changes}], ['Day', 'Wait'])
        chart = build_charts(['Day', 'Wait'], [{'Day': 'Today', 'Wait': 7}])[0]
        self.assertEqual(chart['type'], 'metric')
        self.assertEqual(chart['title'], 'Wait')
        self.assertEqual(chart['value'], 7)

    def test_custom_definition_round_trips_without_literal_data_or_code(self):
        spec = self.spec(type='combo', series=[{'aggregate': 'count', 'type': 'bar'},
            {'aggregate': 'sum', 'field': 'Wait', 'type': 'line', 'axis': 'right', 'label': 'Waiting'}],
            options={'smooth': True, 'legend': False, 'y_label': 'Records', 'right_y_label': 'Minutes'})
        normalized = normalize_charts([spec], ['Day', 'Wait'])
        self.assertEqual(normalize_charts(normalized, ['Day', 'Wait']), normalized)
        for change in ({'data': [1, 2]}, {'script': 'alert(1)'}, {'options': {'callback': 'evil'}},
                       {'x': 'unknown'}, {'limit': True}, {'type': 'iframe'}, {'series': []},
                       {'series': [{'field': 'unknown', 'aggregate': 'sum'}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_charts([self.spec(**change)], ['Day', 'Wait'])

    def test_grouping_is_complete_before_display_limit_and_omissions_are_explicit(self):
        rows = [{'Day': 'A', 'Wait': 1}, {'Day': 'B', 'Wait': 4}, {'Day': 'A', 'Wait': 9},
                {'Day': 'C', 'Wait': 8}, {'Day': 'D', 'Wait': 2}]
        chart = build_charts(['Day', 'Wait'], rows, [self.spec(limit=2, sort='value_desc')])[0]
        self.assertEqual(chart['labels'], ['C', 'A'])
        self.assertEqual(chart['series'][0]['values'], [8, 5])
        self.assertEqual(chart['meta']['row_count'], 5)
        self.assertEqual(chart['meta']['omitted_groups'], 2)
        self.assertEqual(chart['meta']['omitted_row_count'], 2)
        self.assertIn('not plotted', render_charts_html([chart]))

    def test_pie_other_slice_preserves_whole_distribution(self):
        rows = [{'Day': label, 'Wait': index} for index, label in enumerate('ABCDE')]
        chart = build_charts(['Day', 'Wait'], rows, [self.spec(type='pie', limit=2,
            series=[{'aggregate': 'count'}])])[0]
        self.assertEqual(chart['labels'], ['A', 'B', 'Other (3)'])
        self.assertEqual(chart['series'][0]['values'], [1, 1, 3])
        self.assertTrue(chart['meta']['other_included'])
        self.assertIn('combined into Other', render_charts_html([chart]))

    def test_negative_pie_values_are_rejected_even_when_outside_display_limit(self):
        spec = self.spec(type='pie', limit=1, sort='value_desc', series=[{'field': 'Wait', 'aggregate': 'sum'}])
        with self.assertRaises(ValueError):
            build_charts(['Day', 'Wait'], [{'Day': 'A', 'Wait': 10}, {'Day': 'B', 'Wait': -2}], [spec])
        with self.assertRaises(ValueError):
            normalize_charts([self.spec(type='pie')], ['Day', 'Wait'])

    def test_explicit_numeric_conversion_never_treats_bad_or_boolean_cells_as_zero(self):
        for value in ('unknown', True, float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_charts(['Day', 'Wait'], [{'Day': 'A', 'Wait': value}], [self.spec()])
        chart = build_charts(['Day', 'Wait'], [{'Day': 'A', 'Wait': None}, {'Day': 'B', 'Wait': 0}], [self.spec()])[0]
        self.assertEqual(chart['series'][0]['values'], [None, 0])
        self.assertEqual(chart['series'][0]['quality'][0], {'records': 1, 'valid': 0, 'missing': 1})

    def test_row_ordinal_charts_preserve_source_order_and_empty_disables_charts(self):
        rows = [{'Wait': 0}, {'Wait': None}, {'Wait': 8}]
        chart = build_charts(['Wait'], rows, [{'type': 'line', 'x': None,
            'series': [{'field': 'Wait', 'aggregate': 'sum'}]}])[0]
        self.assertEqual(chart['labels'], ['1', '2', '3'])
        self.assertEqual(chart['series'][0]['values'], [0, None, 8])
        self.assertEqual(build_charts(['Wait'], rows, []), [])
        self.assertEqual(build_charts(['Wait'], []), [])

    def test_safe_markup_preserves_source_names_and_assets_are_offline(self):
        spec = self.spec(title='<script>alert(1)</script>', options={'y_label': '<img src=x>'})
        charts = build_charts(['Day', 'Wait'], [{'Day': '</script><img src=x>', 'Wait': 2}], [spec])
        html = render_charts_html(charts)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertIn('data-engenie-chart="0"', html)
        self.assertLess(len(chart_assets().encode()), 30000)
        self.assertNotIn('fetch(', chart_assets())
        self.assertNotIn('innerHTML', chart_assets())
        self.assertIn('.erc-tooltip', chart_styles())

    def test_bounded_schema_rejects_unknown_fields_and_unsafe_style_values(self):
        cases = [self.spec(options={'horizontal': True}), self.spec(options={'stacked': 'yes'}),
                 self.spec(series=[{'field': 'Wait', 'aggregate': 'avg', 'axis': 'right'}]),
                 self.spec(type='combo', series=[{'aggregate': 'count', 'type': 'bar'}, {'aggregate': 'count', 'type': 'bar'}])]
        for spec in cases:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                normalize_charts([spec], ['Day', 'Wait'])
        with self.assertRaises(ValueError):normalize_charts([self.spec()] * 9, ['Day', 'Wait'])
        with self.assertRaises(ValueError):normalize_charts([self.spec()], ['Day', 'Wait', {}])

    def test_scatter_uses_real_numeric_positions_and_rejects_text_x(self):
        spec = self.spec(type='scatter', x='X', series=[{'field': 'Wait', 'aggregate': 'sum'}])
        chart = build_charts(['X', 'Wait'], [{'X': 0, 'Wait': 1}, {'X': 1000, 'Wait': 2}, {'X': 10, 'Wait': 0}], [spec])[0]
        self.assertEqual(chart['x_values'], [0, 1000, 10])
        self.assertEqual(chart['series'][0]['values'], [1, 2, 0])
        with self.assertRaises(ValueError):
            build_charts(['X', 'Wait'], [{'X': 'category', 'Wait': 1}], [spec])
        for options in ({'type': []}, {'sort': []}, {'series': [{'aggregate': []}]},
                        {'series': [{'aggregate': 'count', 'type': []}]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                normalize_charts([self.spec(**options)], ['Day', 'Wait'])

