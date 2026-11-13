from datetime import datetime, timezone

from django.test import SimpleTestCase, override_settings

from kb.report_chart_specs import build_charts, normalize_charts, render_charts_html


@override_settings(TIME_ZONE='Asia/Shanghai')
class ExtendedReportVisualTests(SimpleTestCase):
    headers = ['Name', 'Day', 'Value', 'Category', 'Start', 'End']

    def build(self, definition, rows):
        return build_charts(self.headers, rows, [definition])[0]

    def test_progress_uses_all_rows_and_preserves_zero_missing_and_over_target(self):
        spec = {'type': 'progress', 'title': 'Budget', 'target': 10, 'unit': 'CNY',
                'series': [{'field': 'Value', 'aggregate': 'sum'}]}
        chart = self.build(spec, [{'Value': 8}, {'Value': 5}])
        self.assertEqual(chart['value'], 13)
        self.assertEqual(chart['target'], 10)
        self.assertEqual(chart['unit'], 'CNY')
        self.assertEqual(chart['meta']['row_count'], 2)
        self.assertEqual(self.build(spec, [{'Value': 0}])['value'], 0)
        self.assertIsNone(self.build(spec, [{'Value': None}])['value'])
        for value in (-1, 'unparsed', True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.build(spec, [{'Value': value}])

    def test_sparkline_aggregates_equal_timestamps_and_retains_real_spacing_and_gaps(self):
        spec = {'type': 'sparkline', 'x': 'Day', 'series': [{'field': 'Value', 'aggregate': 'avg'}]}
        chart = self.build(spec, [{'Day': '2026-01-10', 'Value': None},
                                 {'Day': '2026-01-01', 'Value': 0},
                                 {'Day': '2026-01-02', 'Value': 2},
                                 {'Day': '2026-01-02', 'Value': 4}])
        self.assertEqual(chart['labels'], ['2026-01-01', '2026-01-02', '2026-01-10'])
        self.assertEqual(chart['series'][0]['values'], [0, 3, None])
        self.assertEqual(chart['x_values'][2] - chart['x_values'][1], 8 * (chart['x_values'][1] - chart['x_values'][0]))
        numeric = self.build(spec, [{'Day': '10', 'Value': 7}, {'Day': '1', 'Value': 0}])
        self.assertEqual(numeric['x_values'], [1, 10])
        self.assertEqual(numeric['series'][0]['values'], [0, 7])
        for rows in ([{'Day': 'unparsed', 'Value': 1}], [{'Day': None, 'Value': 1}],
                     [{'Day': 1, 'Value': 1}, {'Day': '2026-01-01', 'Value': 2}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.build(spec, rows)

    def test_heatmap_aggregates_cells_without_fabricating_zero_for_absent_combinations(self):
        spec = {'type': 'heatmap', 'x': 'Day', 'y': 'Category',
                'series': [{'field': 'Value', 'aggregate': 'sum'}]}
        chart = self.build(spec, [{'Day': 'Monday', 'Category': 'A', 'Value': 0},
                                 {'Day': 'Tuesday', 'Category': 'B', 'Value': 3},
                                 {'Day': 'Tuesday', 'Category': 'B', 'Value': 5}])
        self.assertEqual(chart['x_labels'], ['Monday', 'Tuesday'])
        self.assertEqual(chart['y_labels'], ['A', 'B'])
        self.assertEqual(chart['cells'], [{'x': 0, 'y': 0, 'value': 0}, {'x': 1, 'y': 1, 'value': 8}])
        self.assertEqual(chart['meta']['row_count'], 3)

    def test_heatmap_keeps_null_blank_and_literal_placeholder_categories_distinct(self):
        spec = {'type': 'heatmap', 'x': 'Day', 'y': 'Category', 'series': [{'aggregate': 'count'}]}
        rows = [{'Day': None, 'Category': None}, {'Day': '', 'Category': ''},
                {'Day': 'Missing (null)', 'Category': 'Blank (empty)'},
                {'Day': 1, 'Category': 'A'}, {'Day': '1', 'Category': 'A'},
                {'Day': None, 'Category': None}]
        chart = self.build(spec, rows)
        self.assertEqual(len(chart['x_labels']), 5)
        self.assertEqual(len(set(chart['x_labels'])), 5)
        self.assertEqual(len(set(chart['y_labels'])), len(chart['y_labels']))
        self.assertIn('Missing (null)', chart['x_labels'])
        self.assertIn('Blank (empty)', chart['y_labels'])
        coordinates = {(cell['x'], cell['y']) for cell in chart['cells']}
        self.assertEqual(len(coordinates), len(chart['cells']))
        self.assertEqual(sorted(cell['value'] for cell in chart['cells']), [1, 1, 1, 1, 2])
        self.assertEqual(sum(cell['value'] for cell in chart['cells']), len(rows))
        self.assertEqual(chart['meta']['row_count'], len(rows))

    def test_pareto_orders_actual_full_contributions_and_rejects_omitted_or_negative_categories(self):
        spec = {'type': 'pareto', 'x': 'Category', 'series': [{'field': 'Value', 'aggregate': 'sum'}]}
        chart = self.build(spec, [{'Category': 'A', 'Value': 2}, {'Category': 'B', 'Value': 3},
                                 {'Category': 'A', 'Value': 7}, {'Category': 'C', 'Value': 0}])
        self.assertEqual(chart['labels'], ['A', 'B', 'C'])
        self.assertEqual(chart['series'][0]['values'], [9, 3, 0])
        with self.assertRaises(ValueError):
            self.build({**spec, 'limit': 1}, [{'Category': 'A', 'Value': 2}, {'Category': 'B', 'Value': 3}])
        with self.assertRaises(ValueError):
            self.build(spec, [{'Category': 'A', 'Value': -2}])

    def test_timeline_uses_iso_business_dates_site_zone_and_actual_duration(self):
        spec = {'type': 'timeline', 'label': 'Name', 'start': 'Start', 'end': 'End', 'group': 'Category'}
        chart = self.build(spec, [{'Name': '<Pump & 1>', 'Category': 'Maintenance',
                                  'Start': '2026-01-02', 'End': '2026-01-04'},
                                 {'Name': 'Inspection', 'Start': '2026-01-01T00:00:00+08:00',
                                  'End': '2026-01-01T12:00:00+08:00'}])
        self.assertEqual(chart['items'][0]['label'], 'Inspection')
        item = chart['items'][1]
        self.assertEqual(item['start'], datetime(2026, 1, 1, 16, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(item['end'] - item['start'], 2 * 86400000)
        self.assertEqual(item['start_label'], '2026-01-02')
        self.assertEqual(item['group'], 'Maintenance')
        for row in ({'Name': 'bad', 'Start': '2026-01-02', 'End': '2026-01-01'},
                    {'Name': 'missing', 'Start': None, 'End': '2026-01-01'}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.build(spec, [row])

    def test_interactive_table_selects_report_columns_and_honestly_marks_loaded_subset(self):
        rows = [{'Name': '<script>literal</script>', 'Value': 0, 'Hidden': 'not-a-report-field'},
                {'Name': 'B', 'Value': False}, {'Name': 'C', 'Value': None}]
        spec = {'type': 'table', 'title': 'Detail <rows>', 'columns': ['Name', 'Value'], 'limit': 2}
        chart = self.build(spec, rows)
        self.assertEqual(chart['columns'], [{'key': 'Name', 'label': 'Name'}, {'key': 'Value', 'label': 'Value'}])
        self.assertEqual(chart['rows'], [{'Name': '<script>literal</script>', 'Value': 0}, {'Name': 'B', 'Value': False}])
        self.assertTrue(chart['truncated'])
        self.assertEqual(chart['total_rows'], 3)
        self.assertEqual(chart['meta']['omitted_row_count'], 1)
        markup = render_charts_html([chart])
        self.assertIn('Detail &lt;rows&gt;', markup)
        self.assertNotIn('not-a-report-field', repr(chart))
        long = self.build(spec, [{'Name': 'x' * 4000, 'Value': 1}])['rows'][0]['Name']
        self.assertEqual(long['original_chars'], 4000)
        self.assertTrue(long['truncated'])
        self.assertEqual(len(long['text']), 2000)
        identifier = 1234567890123456789012345
        self.assertEqual(self.build(spec, [{'Name': identifier, 'Value': 1}])['rows'][0]['Name'], str(identifier))

    def test_configurations_round_trip_and_cannot_reference_hidden_columns_or_literal_data(self):
        valid = [
            {'type': 'progress', 'target': 20, 'series': [{'aggregate': 'count'}]},
            {'type': 'sparkline', 'x': 'Day', 'series': [{'field': 'Value'}]},
            {'type': 'heatmap', 'x': 'Day', 'y': 'Category', 'series': [{'aggregate': 'count'}]},
            {'type': 'pareto', 'x': 'Category', 'series': [{'aggregate': 'count'}]},
            {'type': 'timeline', 'label': 'Name', 'start': 'Start', 'end': 'End'},
            {'type': 'table', 'columns': ['Name', 'Value']},
        ]
        normalized = normalize_charts(valid, self.headers)
        self.assertEqual(normalize_charts(normalized, self.headers), normalized)
        cases = [dict(valid[0], target=0), dict(valid[0], target=True), dict(valid[0], target=float('inf')),
                 dict(valid[1], x='Hidden'), dict(valid[2], y='Hidden'), dict(valid[3], sort='source'),
                 dict(valid[3], series=[{'field': 'Value', 'aggregate': 'avg'}]),
                 dict(valid[4], start='Hidden'), dict(valid[5], columns=['Name', 'Hidden']),
                 dict(valid[5], columns=['Name', 'Name']), dict(valid[5], columns=[])]
        for spec in valid:
            cases += [dict(spec, data=[1, 2]), dict(spec, script='alert(1)'), dict(spec, options={'onClick': 'evil'})]
        for spec in cases:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                normalize_charts([spec], self.headers)

    def test_visual_limits_reject_incomplete_charts_and_keep_empty_states_truthful(self):
        sparkline = {'type': 'sparkline', 'x': 'Day', 'series': [{'aggregate': 'count'}]}
        heatmap = {'type': 'heatmap', 'x': 'Day', 'y': 'Category', 'series': [{'aggregate': 'count'}]}
        timeline = {'type': 'timeline', 'label': 'Name', 'start': 'Start', 'end': 'End'}
        for spec, rows in ((sparkline, [{'Day': index} for index in range(501)]),
                           (heatmap, [{'Day': index, 'Category': 'A'} for index in range(33)]),
                           (timeline, [{'Name': str(index), 'Start': '2026-01-01', 'End': '2026-01-02'} for index in range(101)])):
            with self.subTest(kind=spec['type']), self.assertRaises(ValueError):
                self.build(spec, rows)
        self.assertEqual(self.build(sparkline, [])['series'][0]['values'], [])
        self.assertEqual(self.build(heatmap, [])['cells'], [])
        self.assertEqual(self.build(timeline, [])['items'], [])
        table = self.build({'type': 'table', 'columns': ['Name']}, [])
        self.assertEqual(table['rows'], [])
        self.assertEqual(table['total_rows'], 0)
        self.assertFalse(table['truncated'])

    def test_offline_bundle_contains_all_helpers_before_dispatch_and_metric_markup_stays_simple(self):
        from kb.report_chart_specs import chart_assets, chart_styles
        assets = chart_assets()
        self.assertLess(assets.index('EnGenieInteractiveTable'), assets.index('/* EnGenie Report Charts:'))
        self.assertLess(assets.index('EnGenieExtendedVisualizations'), assets.index('/* EnGenie Report Charts:'))
        self.assertIn('.eit-', chart_styles())
        self.assertNotIn('fetch(', assets)
        self.assertNotIn('innerHTML', assets)
        card = build_charts(['Value'], [{'Value': 7}], [{'type': 'metric', 'series': [{'field': 'Value'}]}])[0]
        self.assertNotIn('report-chart-note', render_charts_html([card]))
        self.assertIn('row_count', card['meta'])
