"""Exercise the browser's exact capture code against real Matplotlib artists.

Run using a Python environment with Matplotlib installed:
    python scripts/tests/chat_matplotlib_capture.test.py
"""
import io
import json
from pathlib import Path
import subprocess
import unittest

import matplotlib
matplotlib.use('Agg')


class ChatMatplotlibCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        cls.scripts = json.loads(subprocess.check_output([
            'node', '-e',
            "const v=require('./static/js/chat_visualizations.js');process.stdout.write(JSON.stringify([v.pythonCaptureSetup,v.pythonCaptureRead]));",
        ], cwd=root, text=True, encoding='utf-8'))

    def capture(self, code, context=None):
        context = {} if context is None else context
        exec(self.scripts[0], context)
        exec(code, context)
        setup, expression = self.scripts[1].strip().rsplit('\n', 1)
        exec(setup, context)
        return json.loads(eval(expression, context)), context

    def test_line_keeps_actual_zero_missing_values_and_safe_source_labels(self):
        charts, _ = self.capture("""
import numpy as np, io
fig, ax = _eg_plt.subplots()
ax.plot(['<b>A</b>', 'B', 'C'], [0, np.nan, 2], label='Source <script>')
ax.set_title('Actual values')
fig.savefig(io.BytesIO(), format='png')
_eg_plt.close(fig)
""")
        self.assertEqual(len(charts), 1)
        self.assertEqual(charts[0]['type'], 'line')
        self.assertEqual(charts[0]['labels'], ['<b>A</b>', 'B', 'C'])
        self.assertEqual(charts[0]['series'][0]['values'], [0, None, 2])
        self.assertEqual(charts[0]['series'][0]['label'], 'Source <script>')

    def test_plain_and_grouped_bars_preserve_category_alignment(self):
        charts, _ = self.capture("""
import numpy as np
fig, ax = _eg_plt.subplots()
x = np.arange(2)
ax.bar(x-.15, [10, 20], .3, label='First')
ax.bar(x+.15, [4, 8], .3, label='Second')
ax.set_xticks(x, ['North', 'South'])
""")
        self.assertEqual(charts[0]['type'], 'bar')
        self.assertEqual(charts[0]['labels'], ['North', 'South'])
        self.assertEqual([item['values'] for item in charts[0]['series']], [[10, 20], [4, 8]])

    def test_dual_axis_combo_preserves_values_and_axes(self):
        charts, _ = self.capture("""
fig, ax = _eg_plt.subplots()
ax.bar(['A', 'B'], [100, 200], label='Volume')
ax.set_ylabel('Volume')
right = ax.twinx()
right.plot(['A', 'B'], [.25, .5], label='Rate')
right.set_ylabel('Rate')
""")
        self.assertEqual(charts[0]['type'], 'combo')
        self.assertEqual(charts[0]['labels'], ['A', 'B'])
        self.assertEqual([item['type'] for item in charts[0]['series']], ['bar', 'line'])
        self.assertEqual([item['axis'] for item in charts[0]['series']], ['left', 'right'])
        self.assertEqual([item['values'] for item in charts[0]['series']], [[100, 200], [.25, .5]])

    def test_pie_and_doughnut_keep_original_values_instead_of_normalized_angles(self):
        for kind, kwargs in [('pie', ''), ('doughnut', ", wedgeprops={'width': .4}")]:
            charts, _ = self.capture("""
fig, ax = _eg_plt.subplots()
ax.pie([20, 10], labels=['First', 'Second']%s)
""" % kwargs)
            self.assertEqual(charts[0]['type'], kind)
            self.assertEqual(charts[0]['series'][0]['values'], [20, 10])
            self.assertEqual(charts[0]['labels'], ['First', 'Second'])

    def test_scatter_keeps_numeric_spacing_and_coordinates(self):
        charts, _ = self.capture("""
fig, ax = _eg_plt.subplots()
ax.scatter([10, 0, 100], [3, 1, 4], label='Samples')
""")
        self.assertEqual(charts[0]['type'], 'scatter')
        self.assertEqual(charts[0]['x_values'], [0, 10, 100])
        self.assertEqual(charts[0]['series'][0]['values'], [1, 3, 4])

    def test_new_run_resets_previous_figures_and_does_not_stack_wrappers(self):
        first, context = self.capture("_eg_plt.plot([0, 1], [20, 30])")
        second, _ = self.capture("_eg_plt.plot([0, 1], [3, 4])", context)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]['series'][0]['values'], [3, 4])

    def test_unsupported_or_oversized_plots_keep_png_fallback(self):
        for code in ["ax.imshow([[1, 2], [3, 4]])", "ax.plot(range(501), range(501))",
                     "ax.plot([1, 2], [3, 4]); ax.set_yscale('log')",
                     "ax.bar([0, 1], [2, 4], bottom=[1, 2])",
                     "ax.bar([0, 1], [2, 4]); ax.bar([0, 1], [3, 5], bottom=[2, 4])",
                     "ax.plot([0, 1], [1, 2]); ax.imshow([[1, 2], [3, 4]])",
                     "ax.fill_between([0, 1, 2], [1, 2, 3])",
                     "ax.plot([0, 1], [1, 2]); ax.axhline(1.5)",
                     "ax.step([0, 1, 2], [1, 2, 3])",
                     "ax.bar([0, 10, 100], [1, 2, 3])",
                     "ax.plot([0, 10, 100], [1, 2, 3]); ax.bar([0, 10, 100], [3, 4, 5])"]:
            charts, context = self.capture("""
import io
fig, ax = _eg_plt.subplots()
%s
png = io.BytesIO()
fig.savefig(png, format='png')
""" % code)
            self.assertEqual(charts, [])
            self.assertGreater(len(context['png'].getvalue()), 100)

    def test_fractional_numeric_x_labels_are_not_snapped_to_nearest_tick(self):
        charts, _ = self.capture("_eg_plt.plot([.9, 1.9, 2.9], [1, 2, 3])")
        self.assertEqual(charts[0]['labels'], ['0.9', '1.9', '2.9'])
        self.assertEqual(charts[0]['x_values'], [.9, 1.9, 2.9])

    def test_irregular_numeric_line_preserves_actual_horizontal_spacing(self):
        charts, _ = self.capture("_eg_plt.plot([0, 10, 100], [1, 2, 3])")
        self.assertEqual(charts[0]['x_values'], [0, 10, 100])
        self.assertEqual(charts[0]['series'][0]['values'], [1, 2, 3])

    def test_clearing_a_previously_saved_figure_does_not_reuse_its_chart(self):
        charts, _ = self.capture("""
import io
fig, ax = _eg_plt.subplots()
ax.plot([0, 1], [1, 2])
fig.savefig(io.BytesIO(), format='png')
fig.clear()
""")
        self.assertEqual(charts, [])

    def test_metric_helper_captures_actual_scalars_alongside_figures(self):
        charts, _ = self.capture("""
engenie_metric('Total <b>', 0, unit='items', subtitle='Source <script>')
engenie_metric('Average', None)
_eg_plt.plot([0, 1], [10, 20])
""")
        self.assertEqual([item['type'] for item in charts], ['metric', 'metric', 'line'])
        self.assertEqual(charts[0]['value'], 0)
        self.assertEqual(charts[0]['title'], 'Total <b>')
        self.assertEqual(charts[0]['unit'], 'items')
        self.assertEqual(charts[0]['subtitle'], 'Source <script>')
        self.assertIsNone(charts[1]['value'])

    def test_metric_helper_resets_between_runs_and_rejects_invalid_values(self):
        first, context = self.capture("engenie_metric('First', 3)")
        second, _ = self.capture("engenie_metric('Second', -12.5, unit='%')", context)
        self.assertEqual(first[0]['value'], 3)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]['title'], 'Second')
        for expression in ["float('nan')", "float('inf')", "True", "'not a number'", "[1, 2]"]:
            with self.assertRaises(ValueError):
                self.capture("engenie_metric('Invalid', %s)" % expression)
        with self.assertRaises(ValueError):
            self.capture("[engenie_metric('Too many', n) for n in range(9)]")


if __name__ == '__main__':
    unittest.main()
