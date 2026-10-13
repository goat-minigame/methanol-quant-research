import csv
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_tool import bars
from test_markets import FakeFrame
from methanol.data import FIELDS
from methanol.indicators import calculate, ema
from methanol.strategies import NAMES, positions, compare
from methanol.backtest import backtest
from methanol.fundamentals import fetch_fundamental, inspect_releases


def write_bars(path, rows):
    with path.open('w',newline='') as f:
        writer=csv.DictWriter(f,FIELDS);writer.writeheader();writer.writerows(rows)


class IndicatorTests(unittest.TestCase):
    def test_ema_seed_and_recursion(self):
        self.assertEqual(ema([1,2,3,4],3),[None,None,2,3])
        self.assertEqual(ema([None,1,2,3,4],3),[None,None,None,2,3])

    def test_warmup_and_flat_market(self):
        rows=calculate(bars([100]*80))
        self.assertIsNone(rows[12]['atr14'])
        self.assertIsNone(rows[13]['rsi14'])
        self.assertIsNone(rows[32]['macd_dea'])
        self.assertIsNotNone(rows[33]['macd_dea'])
        latest=rows[-1]
        self.assertEqual(latest['rsi14'],50)
        self.assertEqual(latest['atr14'],2)
        self.assertEqual(latest['cci20'],0)
        self.assertEqual(latest['boll_width20'],0)
        self.assertAlmostEqual(latest['k9'],50)
        self.assertEqual(len(latest)-2,29)

    def test_known_trend_and_indicator_ranges(self):
        latest=calculate(bars(list(range(100,180))))[-1]
        self.assertEqual(latest['sma20'],169.5)
        self.assertEqual(latest['rsi14'],100)
        self.assertEqual(latest['obv'],7900)
        self.assertAlmostEqual(latest['roc10'],179/169-1)
        self.assertTrue(-100<=latest['williams_r14']<=0)
        self.assertGreater(latest['cci20'],0)
        for value in latest.values():
            if isinstance(value,float):self.assertTrue(math.isfinite(value))

    def test_future_prices_do_not_change_past_indicators_or_signals(self):
        first=bars(list(range(100,200)))
        changed=bars(list(range(100,180))+list(range(1000,1020)))
        self.assertEqual(calculate(first)[:80],calculate(changed)[:80])
        a,b=positions(first),positions(changed)
        for name in NAMES:
            for i in range(81):self.assertEqual(a[name][first[i]['date']],b[name][first[i]['date']])

    def test_missing_volume_remains_missing(self):
        rows=bars([100]*80);rows[20]['volume']=None
        result=calculate(rows)
        self.assertIsNone(result[20]['obv'])
        self.assertIsNone(result[21]['volume_ratio20'])
        self.assertIsNone(result[-1]['obv'])


class StrategyTests(unittest.TestCase):
    def test_ma_matches_existing_strategy_and_buy_hold_cost(self):
        rows=bars(list(range(100,200)))
        signals=positions(rows)
        first=backtest(rows)
        second=backtest(rows,signals=signals['ma_cross'])
        self.assertAlmostEqual(first['summary']['total_return'],second['summary']['total_return'])
        buy=backtest(rows,signals=signals['buy_hold'],strategy='buy_hold',cost_bps=100)
        self.assertAlmostEqual(buy['summary']['total_return'],199/100*.99**2-1)
        self.assertEqual(buy['summary']['completed_trades'],1)

    def test_all_strategies_same_test_dates_and_report(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'ma.csv';write_bars(path,bars(list(range(100,260))))
            result=compare(path,Path(d)/'report','2024-04-01')
            ranges={(r['test']['first_date'],r['test']['last_date'],r['test']['bars']) for r in result['strategies'].values()}
            self.assertEqual(len(ranges),1)
            self.assertEqual(set(result['strategies']),set(NAMES))
            self.assertTrue((Path(d)/'report/ensemble/report.html').exists())
            self.assertTrue((Path(d)/'report/comparison.csv').exists())

    def test_invalid_positions_are_rejected(self):
        rows=bars([100]*40)
        with self.assertRaises(ValueError):backtest(rows,signals={r['date']:2 for r in rows})


class FundamentalTests(unittest.TestCase):
    def write(self,path,rows):
        with path.open('w',newline='') as f:
            writer=csv.DictWriter(f,['metric','observation_date','available_at','value','unit','source']);writer.writeheader();writer.writerows(rows)

    def test_release_filter_revisions_and_timezone(self):
        rows=[{'metric':'inventory','observation_date':'2024-01-01','available_at':time,'value':value,'unit':'万吨','source':'public citation'}
              for time,value in [('2024-01-02T09:00:00+08:00',10),('2024-01-03T09:00:00+08:00',20)]]
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'data.csv';self.write(path,rows)
            result=inspect_releases(path,'2024-01-02T02:00:00Z')
            self.assertEqual(result['latest_available']['inventory']['value'],10)
            self.assertEqual(result['excluded_future_releases'],1)
            later=inspect_releases(path,'2024-01-03T10:00:00+08:00')
            self.assertEqual(later['latest_available']['inventory']['value'],20)
            early=inspect_releases(path,'2024-01-01T10:00:00+08:00')
            self.assertEqual(early['latest_available'],{})

    def test_naive_timestamps_and_duplicates_rejected(self):
        row={'metric':'inventory','observation_date':'2024-01-01','available_at':'2024-01-02T09:00:00+08:00','value':10,'unit':'万吨','source':'public citation'}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'data.csv';self.write(path,[row,row])
            with self.assertRaises(ValueError):inspect_releases(path,'2024-01-03T10:00:00+08:00')
            self.write(path,[row])
            with self.assertRaises(ValueError):inspect_releases(path,'2024-01-03T10:00:00')

    def test_basis_and_warehouse_dispatch_only_ma(self):
        calls=[]
        def basis(**kwargs):calls.append(kwargs);return FakeFrame([{'symbol':'MA','spot_price':2500}])
        fake=SimpleNamespace(__version__='test',futures_spot_price=basis,futures_warehouse_receipt_czce=lambda **kw:{'MA':FakeFrame([{'warehouse':'test','receipts':10}])})
        with patch('methanol.fundamentals.akshare',return_value=fake):
            result=fetch_fundamental('basis','2024-01-02')
            self.assertEqual(calls,[{'date':'20240102','vars_list':['MA']}])
            self.assertEqual(result['availability_status'],'unverified_for_historical_training')
            self.assertEqual(fetch_fundamental('warehouse','2024-01-02')['row_count'],1)
        with self.assertRaises(ValueError):fetch_fundamental('basis')

    def test_macro_mapping_and_empty_data(self):
        fake=SimpleNamespace(__version__='test',macro_china_pmi_yearly=lambda:FakeFrame([{'日期':'2024-01-01','今值':50}]))
        with patch('methanol.fundamentals.akshare',return_value=fake):
            self.assertEqual(fetch_fundamental('pmi')['row_count'],1)
        fake.macro_china_pmi_yearly=lambda:FakeFrame([])
        with patch('methanol.fundamentals.akshare',return_value=fake),self.assertRaises(ValueError):fetch_fundamental('pmi')


if __name__=='__main__':unittest.main()
