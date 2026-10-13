import hashlib
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_tool import bars
from methanol.markets import futures_list, futures_quotes, options_query, option_underlyings, normalize_ma_chain, expiry
from methanol.derivatives import black76, volatility
from methanol.data import FIELDS
from methanol.__main__ import main


class FakeFrame:
    empty = False
    def __init__(self, rows): self.rows=rows
    def to_dict(self, orient): return self.rows


def chain():
    return [{'行权价':2500,'看涨合约-看涨期权合约':'MA701C2500','看涨合约-买价':100,'看涨合约-卖价':110,
             '看涨合约-最新价':105,'看涨合约-持仓量':100,'看跌合约-看跌期权合约':'MA701P2500',
             '看跌合约-买价':120,'看跌合约-卖价':130,'看跌合约-最新价':125,'看跌合约-持仓量':200}]


class MarketTests(unittest.TestCase):
    def test_upstream_files_match_pinned_manifest(self):
        root=Path('methanol/vendor/a_stock_data_quant')
        manifest=json.loads((root/'UPSTREAM.json').read_text())
        for name,record in manifest['files'].items():
            self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),record['sha256'])
        self.assertEqual(len(futures_list()['contracts']),18)
        self.assertEqual(option_underlyings()['commodity_options'][0]['code'],'MA')

    def test_https_quote_settlement_null_and_snapshot(self):
        raw=json.dumps({'qt':{'p':'2500','zjsj':'2480','o':'-','name':'甲醇','dm':'MAM','vol':100}}).encode()
        with tempfile.TemporaryDirectory() as d, patch('methanol.markets.urlopen',return_value=io.BytesIO(raw)) as request:
            path=Path(d)/'quote.json'
            result=futures_quotes('MA',path)
            self.assertTrue(request.call_args.args[0].full_url.startswith('https://'))
            self.assertEqual(result['quotes'][0]['previous_settlement'],2480)
            self.assertIsNone(result['quotes'][0]['open'])
            self.assertNotIn('prev_close',result['quotes'][0])
            self.assertEqual(hashlib.sha256(result['responses'][0]['raw_text'].encode()).hexdigest(),result['quotes'][0]['raw_sha256'])
            self.assertTrue(path.exists())

    def test_partial_failure_has_nonzero_cli_status(self):
        from urllib.error import URLError
        with patch('sys.argv',['methanol','futures','quote','MA']), patch('methanol.markets.urlopen',side_effect=URLError('blocked')), patch('builtins.print'):
            self.assertEqual(main(),1)
        self.assertTrue(futures_quotes('unknown')['errors'])

    def test_bad_expiry_fails_before_dependency_or_request(self):
        for value in ['202613','2700','invalid']:
            with self.assertRaises(ValueError): expiry(value)
        self.assertEqual(expiry('202701'),'2701')
        with patch('methanol.markets.akshare') as ak:
            with self.assertRaises(ValueError): options_query('chain','MA','202701')
            with self.assertRaises(ValueError): options_query('chain','159915','202701')
            ak.assert_not_called()

    def test_ma_chain_dispatch_and_correct_pcr(self):
        ak=SimpleNamespace(__version__='test',option_commodity_contract_table_sina=lambda **kwargs:FakeFrame(chain()))
        with patch('methanol.markets.akshare',return_value=ak):
            result=options_query('chain','MA','MA701')
        self.assertEqual(result['open_interest_pcr'],2)
        self.assertEqual(len(result['normalized_chain']),2)
        self.assertEqual(result['akshare_version'],'test')

    def test_ma_missing_interest_does_not_invent_pcr(self):
        rows=chain(); rows[0]['看跌合约-持仓量']=None
        _,pcr=normalize_ma_chain(rows)
        self.assertIsNone(pcr)
        rows[0]['看跌合约-持仓量']=-1
        with self.assertRaises(ValueError):normalize_ma_chain(rows)
        with self.assertRaises(ValueError):normalize_ma_chain(chain()*2)

    def test_empty_and_provider_errors_are_not_success(self):
        ak=SimpleNamespace(__version__='test',option_commodity_contract_sina=lambda **kw:FakeFrame([]))
        with patch('methanol.markets.akshare',return_value=ak), self.assertRaises(ValueError):
            options_query('expiries','MA')
        with patch('methanol.markets.akshare',return_value=ak),patch('methanol.markets.upstream_options.cmd_expiries',return_value={'error':'failed'}),self.assertRaises(RuntimeError):
            options_query('expiries','510050')

    def test_unsupported_pcr_is_not_called_pcr(self):
        with self.assertRaises(ValueError):options_query('values','MA')


class DerivativeTests(unittest.TestCase):
    def test_black76_parity_and_greek_finite_differences(self):
        inputs=[2500,2400,30,.02,.3]
        call=black76(*inputs); put=black76(*inputs,kind='put')
        self.assertAlmostEqual(call['price']-put['price'],math.exp(-.02*30/365)*100)
        bump=.01
        high=black76(2500+bump,*inputs[1:])['price']
        low=black76(2500-bump,*inputs[1:])['price']
        self.assertAlmostEqual((high-low)/(2*bump),call['delta_futures'],places=7)
        self.assertAlmostEqual((high-2*call['price']+low)/(bump*bump),call['gamma_futures'],places=6)
        self.assertLess(put['delta_futures'],0)

    def test_black76_invalid_inputs(self):
        for f,k,t,r,v in [(0,2500,30,.02,.3),(2500,2500,0,.02,.3),(2500,2500,30,.02,0),(2500,2500,30,float('nan'),.3)]:
            with self.assertRaises(ValueError):black76(f,k,t,r,v)

    def test_volatility_past_only_and_upstream_formula(self):
        import csv
        from statistics import stdev
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'ma.csv'
            rows=bars([100,101,103,105,110,108,111,112,115,114])
            def write():
                with path.open('w',newline='') as f:
                    writer=csv.DictWriter(f,FIELDS);writer.writeheader();writer.writerows(rows)
            write();first=volatility(path,2,3)
            expected=stdev([103/101-1,105/103-1])*math.sqrt(252)
            self.assertAlmostEqual(first['history'][3]['annualized_historical_volatility'],expected)
            rows[-1].update(open=999,close=999,high=1000,low=998)
            write();second=volatility(path,2,3)
            self.assertEqual(first['history'][:-1],second['history'][:-1])


if __name__=='__main__':unittest.main()
