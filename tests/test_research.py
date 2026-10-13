import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_tool import bars
from methanol.data import fetch_data, inspect_dataset
from methanol.backtest import backtest
from methanol.research import research
from methanol.report import write_report


class IntegrityTests(unittest.TestCase):
    def fixture(self, directory):
        path = Path(directory)/'ma.csv'
        with patch('methanol.data.urlopen', return_value=io.BytesIO(json.dumps(bars(list(range(100, 180)))).encode())):
            fetch_data(path)
        return path

    def test_csv_tampering_rejects_report_before_output(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.fixture(d)
            rows, _ = inspect_dataset(path)
            path.write_bytes(path.read_bytes()+b'\n')
            with self.assertRaisesRegex(ValueError, '哈希不一致'):
                write_report(backtest(rows), Path(d)/'output', path)
            self.assertFalse((Path(d)/'output').exists())

    def test_raw_tampering_and_missing_raw(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.fixture(d)
            raw = path.with_suffix('.raw.txt')
            raw.write_text('changed')
            with self.assertRaisesRegex(ValueError, '原始行情响应哈希'):
                inspect_dataset(path)
            raw.unlink()
            with self.assertRaisesRegex(ValueError, '原始响应缺失'):
                inspect_dataset(path)

    def test_malformed_or_incorrect_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.fixture(d)
            meta = path.with_suffix('.metadata.json')
            data = json.loads(meta.read_text()); data['rows'] += 1
            meta.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, '行数或日期范围'):
                inspect_dataset(path)
            meta.write_text('{invalid}')
            with self.assertRaises(ValueError): inspect_dataset(path)

    def test_future_dated_market_data_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'future.csv'
            rows = bars([100, 101])
            for i, row in enumerate(rows): row['date'] = f'2099-01-0{i+1}'
            with patch('methanol.data.urlopen', return_value=io.BytesIO(json.dumps(rows).encode())):
                fetch_data(path)
            with self.assertRaisesRegex(ValueError, '未来日期'):
                inspect_dataset(path)

    def test_missing_provenance_is_explicitly_unverified(self):
        with tempfile.TemporaryDirectory() as d:
            path = self.fixture(d)
            path.with_suffix('.metadata.json').unlink()
            _, evidence = inspect_dataset(path)
            self.assertEqual(evidence['status'], 'unverified')
            self.assertIn('age_calendar_days', evidence['quality'])


class PeriodTests(unittest.TestCase):
    def test_prehistory_warms_signal_but_not_account_returns(self):
        result = backtest(bars(list(range(10, 50))), 2, 5, 0, '2024-01-11', '2024-01-15')
        self.assertEqual(result['summary']['bars'], 5)
        self.assertEqual(result['summary']['prehistory_bars'], 10)
        self.assertEqual(result['summary']['requested_start'], '2024-01-11')
        self.assertEqual(result['summary']['requested_end'], '2024-01-15')
        self.assertEqual(result['curve'][0]['position'], 1)
        self.assertEqual(result['trades'][0]['entry_date'], '2024-01-11')
        self.assertAlmostEqual(result['summary']['total_return'], 24/20-1)
        self.assertEqual(result['trades'][0]['exit_date'], '2024-01-15')

    def test_end_does_not_use_next_period_open(self):
        original = bars(list(range(10, 50)))
        changed = original[:15]+bars(list(range(1000, 1025)))
        # Keep the date chronology, change only prices strictly after the endpoint.
        for i in range(15, 40): changed[i]['date'] = original[i]['date']
        first = backtest(original, 2, 5, 0, end='2024-01-15')
        second = backtest(changed, 2, 5, 0, end='2024-01-15')
        self.assertEqual(first, second)

    def test_invalid_and_empty_periods(self):
        for start, end in [('2024-01-15', '2024-01-10'), ('2030-01-01', None), (None, '2023-12-31')]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                backtest(bars(list(range(10, 50))), 2, 5, 0, start, end)

    def test_fees_change_returns_not_signals(self):
        rows = bars(list(range(10, 50)))
        free = backtest(rows, 2, 5, 0)
        costly = backtest(rows, 2, 5, 20)
        self.assertEqual([r['position'] for r in free['curve']], [r['position'] for r in costly['curve']])
        self.assertLess(costly['summary']['total_return'], free['summary']['total_return'])

    def test_research_disjoint_periods_outputs_and_disclosure(self):
        with tempfile.TemporaryDirectory() as d:
            path = IntegrityTests().fixture(d)
            output = Path(d)/'research'
            result = research(path, output, '2024-02-10', 2, 5, 5)
            self.assertLess(result['train']['last_date'], result['test']['first_date'])
            self.assertEqual(result['test']['prehistory_bars'], 40)
            self.assertEqual(result['dataset']['status'], 'verified_local_hashes')
            self.assertIn('不是从未看过', result['plan']['disclosure'])
            self.assertEqual(len(result['test_cost_sensitivity']), 4)
            self.assertTrue((output/'test/report.html').exists())
            self.assertTrue((output/'train/trades.csv').exists())
            self.assertTrue((output/'research.json').exists())

    def test_research_cli_and_invalid_split(self):
        from methanol.__main__ import main
        with tempfile.TemporaryDirectory() as d:
            path = IntegrityTests().fixture(d)
            output = Path(d)/'cli'
            with patch('sys.argv', ['methanol', 'research', '--input', str(path), '--output', str(output), '--split-date', '2024-02-10']), patch('builtins.print'):
                self.assertEqual(main(), 0)
            self.assertTrue((output/'report.html').exists())
            with patch('sys.argv', ['methanol', 'research', '--input', str(path), '--output', d+'/invalid', '--split-date', '2020-01-01']), patch('builtins.print'):
                self.assertEqual(main(), 1)
            self.assertFalse((Path(d)/'invalid').exists())

    def test_test_prices_cannot_change_training_result(self):
        with tempfile.TemporaryDirectory() as d:
            path = IntegrityTests().fixture(d)
            first = research(path, Path(d)/'first', '2024-02-10', 2, 5)
            values = list(range(100, 140))+list(range(1000, 1040))
            with patch('methanol.data.urlopen', return_value=io.BytesIO(json.dumps(bars(values)).encode())):
                fetch_data(path)
            second = research(path, Path(d)/'second', '2024-02-10', 2, 5)
            self.assertEqual(first['train'], second['train'])
            self.assertNotEqual(first['test']['total_return'], second['test']['total_return'])


if __name__ == '__main__':
    unittest.main()
