import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_tool import bars
from methanol.forecast import features, fit_model, probability, walk_forward, metrics, paired_interval, forecast
from methanol.data import fetch_data
from methanol.__main__ import main


class ForecastTests(unittest.TestCase):
    def test_features_use_only_previous_close(self):
        rows = bars(list(range(100, 260)))
        first = features(rows, 100)
        rows[100]['close'] = 999999
        rows[100]['volume'] = 999999
        self.assertEqual(first, features(rows, 100))
        self.assertAlmostEqual(first[0], 199/198-1)

    def test_future_changes_do_not_change_prediction_including_boundary(self):
        rows = bars(list(range(100, 260)))
        original, _ = walk_forward(rows, '2024-04-01', 60, 10)
        changed = bars(list(range(100, 210))+list(range(1000, 1050)))
        new, _ = walk_forward(changed, '2024-04-01', 60, 10)
        original_early = [(r['date'], r['probability_up']) for r in original if r['date'] <= rows[110]['date']]
        new_early = [(r['date'], r['probability_up']) for r in new if r['date'] <= rows[110]['date']]
        self.assertEqual(original_early, new_early)

    def test_fit_audit_and_final_label(self):
        rows = bars(list(range(100, 260)))
        predictions, fits = walk_forward(rows, '2024-04-01', 60, 10)
        first = fits[0]
        self.assertLess(first['last_training_sample'], first['last_label_observed'])
        self.assertLess(first['last_label_observed'], first['prediction_start'])
        self.assertEqual(first['samples'], 60)
        self.assertEqual(predictions[-1]['target_end_date'], rows[-1]['date'])
        self.assertEqual(predictions[-1]['date'], rows[-2]['date'])

    def test_invalid_window_history_and_start(self):
        rows = bars(list(range(100, 260)))
        for start, window, refit in [('2024-04-01', 59, 10), ('2024-04-01', 60, 0), ('2024-01-30', 60, 10), ('2030-01-01', 60, 10)]:
            with self.subTest(start=start, window=window, refit=refit), self.assertRaises(ValueError):
                walk_forward(rows, start, window, refit)

    def test_probability_is_finite_for_large_feature_values(self):
        model = fit_model([([i, 0, 0, 0, 0], int(i>0)) for i in range(-10, 11)])
        self.assertTrue(0 < probability(model, [1e300]*5) < 1)
        self.assertLess(probability(model, [-10,0,0,0,0]), probability(model, [10,0,0,0,0]))

    def test_accuracy_confusion_and_costs(self):
        rows = [{'model': pred, 'actual_up': label, 'market_return': ret, 'probability_up': .6}
                for pred,label,ret in [(1,1,.1),(1,0,-.1),(0,0,-.1),(0,1,.1)]]
        result = metrics(rows, 'model', 100)
        self.assertEqual(result['accuracy'], .5)
        self.assertEqual(result['balanced_accuracy'], .5)
        self.assertEqual(result['confusion'], {'tp':1,'tn':1,'fp':1,'fn':1})
        self.assertAlmostEqual(result['total_return'], .99**2*1.1*.9-1)
        self.assertEqual(result['one_way_turnover'], 2)
        with self.assertRaises(ValueError): metrics(rows, 'model', float('nan'))

    def test_terminal_liquidation_and_single_class(self):
        result = metrics([{'model':1,'actual_up':1,'market_return':.1,'probability_up':.9}], 'model', 100)
        self.assertIsNone(result['balanced_accuracy'])
        self.assertEqual(result['one_way_turnover'], 2)
        self.assertAlmostEqual(result['total_return'], .99**2*1.1-1)

    def test_paired_resampling_reproducible(self):
        rows = [{'model':1,'sma':0,'actual_up':1} for _ in range(60)]
        first = paired_interval(rows, 'sma', 50, 10)
        self.assertEqual(first, paired_interval(rows, 'sma', 50, 10))
        self.assertEqual(first['approximate_95pct_block_interval'], [1,1])

    def test_forecast_cli_single_class_and_provenance(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'ma.csv'
            with patch('methanol.data.urlopen', return_value=io.BytesIO(json.dumps(bars(list(range(100, 260)))).encode())):
                fetch_data(path)
            output = Path(d)/'forecast'
            with patch('sys.argv', ['methanol','forecast','--input',str(path),'--output',str(output),'--start','2024-04-01','--window','60','--refit','10']), patch('builtins.print'):
                self.assertEqual(main(),0)
            summary = json.loads((output/'forecast.json').read_text())
            self.assertEqual(summary['dataset']['status'], 'verified_local_hashes')
            self.assertTrue((output/'predictions.csv').exists())
            self.assertIn('不是从未看过', summary['plan']['disclosure'])
            self.assertTrue((output/'report.html').exists())


if __name__ == '__main__':
    unittest.main()
