import csv
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from methanol.data import FIELDS, fetch_data, parse_response, read_csv, validate_bars
from methanol.backtest import backtest
from methanol.report import write_report
from methanol.__main__ import main


def bars(prices, opens=None):
    opens = prices if opens is None else opens
    return [{'date': (date(2024, 1, 1)+timedelta(days=i)).isoformat(),
             'open': opening, 'high': max(opening, close)+1, 'low': min(opening, close)-1,
             'close': close, 'volume': 100, 'hold': 50, 'settle': None}
            for i, (opening, close) in enumerate(zip(opens, prices))]


class DataTests(unittest.TestCase):
    def test_jsonp_and_sort(self):
        rows = bars([10, 11])
        raw = ('/*<script>ignored</script>*/\nvar _MA0=('+json.dumps(rows[::-1])+');').encode()
        self.assertEqual(parse_response(raw), rows)

    def test_short_keys(self):
        self.assertEqual(parse_response(b'[{"d":"2024-01-01","o":"10","h":"12","l":"9","c":"11","v":"100","p":"50","s":"11"}]')[0]['close'], 11)

    def test_duplicate_and_bad_prices(self):
        a = bars([10])[0]
        with self.assertRaises(ValueError): validate_bars([a, a])
        for key, value in [('close', float('nan')), ('open', 0), ('volume', -1), ('high', 1)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_bars([dict(a, **{key: value})])

    def test_no_html_or_empty_data(self):
        for raw in [b'<html>blocked</html>', b'[]', b'{"error":"fail"}']:
            with self.subTest(raw=raw), self.assertRaises(ValueError): parse_response(raw)

    def test_fetch_keeps_source_and_hash(self):
        import io, hashlib
        raw = json.dumps(bars([10, 11])).encode()
        with tempfile.TemporaryDirectory() as d, patch('methanol.data.urlopen', return_value=io.BytesIO(raw)):
            path = Path(d)/'ma.csv'
            meta = fetch_data(path)
            self.assertEqual(meta['raw_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(meta['csv_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(read_csv(path), bars([10, 11]))
            self.assertEqual(path.with_suffix('.raw.txt').read_bytes(), raw)

    def test_network_failure_creates_no_fake_data(self):
        from urllib.error import URLError
        with tempfile.TemporaryDirectory() as d, patch('methanol.data.urlopen', side_effect=URLError('blocked')):
            path = Path(d)/'ma.csv'
            with self.assertRaises(RuntimeError): fetch_data(path)
            self.assertFalse(path.exists())


class StrategyTests(unittest.TestCase):
    def test_previous_close_next_open_and_fee(self):
        result = backtest(bars([10, 20, 30, 40]), fast=1, slow=2, cost_bps=100)
        self.assertEqual([r['position'] for r in result['curve']], [0, 0, 1, 1])
        # Buy at day 3 open=30, sell at day 4 close=40, two 1% costs.
        self.assertAlmostEqual(result['summary']['total_return'], 40/30*.99**2-1)
        self.assertEqual(result['trades'][0]['entry_price'], 30)
        self.assertEqual(result['trades'][0]['exit_reason'], '样本结束平仓')
        self.assertEqual(sum(r['turnover'] for r in result['curve']), 2)

    def test_open_execution_includes_overnight(self):
        result = backtest(bars([10, 20, 30, 40], opens=[10, 11, 25, 35]), 1, 2, 0)
        self.assertAlmostEqual(result['summary']['total_return'], 40/25-1)
        self.assertAlmostEqual(result['summary']['benchmark_return'], 40/10-1)

    def test_no_future_leak(self):
        first = backtest(bars([10, 20, 30, 40, 50, 60]), 1, 2)
        second = backtest(bars([10, 20, 30, 4, 5, 6]), 1, 2)
        self.assertEqual([r['position'] for r in first['curve'][:4]], [r['position'] for r in second['curve'][:4]])

    def test_exit_and_drawdown(self):
        result = backtest(bars([10, 20, 30, 5, 4, 3]), 1, 2, 0)
        self.assertEqual(result['trades'][0]['exit_price'], 4)
        self.assertEqual(result['trades'][0]['exit_reason'], '均线离场')
        self.assertEqual(result['summary']['large_open_gaps'][0]['date'], '2024-01-02')
        self.assertAlmostEqual(result['summary']['max_drawdown'], 4/30-1)

    def test_flat_and_invalid_parameters(self):
        result = backtest(bars([10]*5), 1, 2)
        self.assertEqual(result['summary']['completed_trades'], 0)
        self.assertIsNone(result['summary']['sharpe_zero_rate'])
        for fast, slow, fee in [(2, 2, 0), (0, 2, 0), (1, 9, 0), (1, 2, -1), (1, 2, float('nan'))]:
            with self.subTest(fast=fast, slow=slow, fee=fee), self.assertRaises(ValueError):
                backtest(bars([10]*5), fast, slow, fee)

    def test_report_outputs_and_source_escaping(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'synthetic.csv'
            import io
            raw = json.dumps(bars([10, 20, 30, 40])).encode()
            with patch('methanol.data.urlopen', return_value=io.BytesIO(raw)):
                metadata = fetch_data(path)
            metadata['source'] = '<script>bad()</script>'
            path.with_suffix('.metadata.json').write_text(json.dumps(metadata))
            output = Path(d)/'report'
            write_report(backtest(bars([10, 20, 30, 40]), 1, 2), output, path)
            self.assertEqual({p.name for p in output.iterdir()}, {'summary.json', 'curve.csv', 'trades.csv', 'report.html'})
            doc = (output/'report.html').read_text()
            self.assertIn('&lt;script&gt;', doc)
            self.assertNotIn('<script>', doc)
            self.assertEqual(json.loads((output/'summary.json').read_text())['completed_trades'], 1)

    def test_cli_end_to_end_synthetic(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'synthetic.csv'
            with path.open('w', newline='') as f:
                writer = csv.DictWriter(f, FIELDS)
                writer.writeheader(); writer.writerows(bars(list(range(10, 50))))
            with patch('sys.argv', ['methanol', 'backtest', '--input', str(path), '--output', d+'/output']), patch('builtins.print'):
                self.assertEqual(main(), 0)
            self.assertTrue((Path(d)/'output/report.html').exists())


if __name__ == '__main__':
    unittest.main()
