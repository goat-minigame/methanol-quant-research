"""Synthetic fixtures only. Network is mocked; live acceptance is documented separately."""
import copy
import io
import json
import math
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from crude_oil import sources as s, report as r
from crude_oil.__main__ import main
from methanol import sources as base


def sc_doc(day='20261012', month='2611', value=600):
    return {'report_date': day, 'o_code': '0000', 'o_curinstrument': [
        {'PRODUCTID': 'sc_f', 'DELIVERYMONTH': month, 'CLOSEPRICE': value,
         'SETTLEMENTPRICE': 599, 'VOLUME': 100, 'OPENINTEREST': 200}]}


def csv_raw():
    return ('"STUB_1","10/2/26","9/25/26","Difference","10/3/25","Percent Change","10/4/24","Percent Change"\n' +
            '\n'.join('"'+label+'",400,399,1,350,1,350,1' for label in
            ['Commercial (Excluding SPR)', 'Cushing', 'Total Motor Gasoline', 'Distillate Fuel Oil'])).encode()


def eia_doc(metric='crude_inventory', value='400000'):
    code, unit = s.SERIES[metric]
    return {'response': {'frequency': 'weekly', 'data': [dict(period='2026-10-02', series=code, units=unit, value=value)]}}


class Parsers(unittest.TestCase):
    def test_sc_contract_unit_and_unknown_publication(self):
        rows = s.parse_sc(base.canonical(sc_doc()), '2026-10-12')
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]['contract'], 'SC2611')
        self.assertEqual(rows[0]['unit'], 'CNY/barrel')
        self.assertIsNone(rows[0]['published_at'])
        self.assertIn('current_contract_rules_unverified', rows[0]['quality'])

    def test_sc_wrong_date_and_waf(self):
        for raw in [b'<html>WAF</html>', base.canonical(sc_doc('20261009')), b'{}']:
            with self.assertRaises(ValueError):s.parse_sc(raw, '2026-10-12')

    def test_sc_not_continuous_or_expired_or_invalid_month(self):
        for month in ['0', '小计', '2613', '2511', '3012']:
            with self.assertRaises(ValueError):s.parse_sc(base.canonical(sc_doc(month=month)), '2026-10-12')

    def test_sc_duplicate_contract(self):
        doc = sc_doc(); doc['o_curinstrument'] *= 2
        with self.assertRaises(ValueError):s.parse_sc(base.canonical(doc), '2026-10-12')

    def test_sc_roll_keeps_both_contracts(self):
        doc = sc_doc(); other = copy.deepcopy(doc['o_curinstrument'][0]);other['DELIVERYMONTH'] = '2612'
        doc['o_curinstrument'].append(other)
        self.assertEqual({x['contract'] for x in s.parse_sc(base.canonical(doc), '2026-10-12')}, {'SC2611','SC2612'})

    def test_sc_fractional_volume(self):
        doc=sc_doc();doc['o_curinstrument'][0]['VOLUME']=1.5
        with self.assertRaises(ValueError):s.parse_sc(base.canonical(doc),'2026-10-12')

    def test_sc_null_zero_and_zero_volume(self):
        doc=sc_doc(value=0);doc['o_curinstrument'][0]['VOLUME']=0
        row=s.parse_sc(base.canonical(doc),'2026-10-12')[0]
        self.assertIsNone(row['value']);self.assertTrue(row['detail']['zero_volume'])

    def test_numeric_reject_distortion(self):
        for value in [True, float('inf'), 'NaN', '-1', 'abc']:
            with self.assertRaises(ValueError):s.numeric(value)
        self.assertIsNone(s.numeric('--'))

    def test_public_week_not_publication(self):
        rows=s.parse_public_stocks(csv_raw())
        self.assertEqual(len(rows),8)
        self.assertEqual(rows[0]['observed_end'],'2026-10-02')
        self.assertIsNone(rows[0]['published_at'])
        self.assertTrue(rows[0]['detail']['not_eia_v2'])

    def test_public_duplicate_missing_changed_week(self):
        for raw in [csv_raw()+b'\n"Cushing",1,1,1,1,1,1,1',csv_raw().replace(b'Cushing',b'Other'),csv_raw().replace(b'9/25/26',b'9/24/26')]:
            with self.assertRaises(ValueError):s.parse_public_stocks(raw)

    def test_eia_period_and_proxy_definition(self):
        row=s.parse_eia(base.canonical(eia_doc('gasoline_product_supplied')), 'gasoline_product_supplied')[0]
        self.assertEqual(row['observed_start'],'2026-09-26')
        self.assertEqual(row['observed_end'],'2026-10-02')
        self.assertTrue(row['detail']['product_supplied_is_demand_proxy'])
        self.assertIsNone(row['published_at'])

    def test_eia_wrong_unit_series_duplicate_frequency(self):
        original=eia_doc()
        variants=[]
        for field,value in [('units','Dollars per Barrel'),('series','SPOT'),('period','2026-10-01')]:
            doc=copy.deepcopy(original);doc['response']['data'][0][field]=value;variants.append(doc)
        doc=copy.deepcopy(original);doc['response']['data']*=2;variants.append(doc)
        doc=copy.deepcopy(original);doc['response']['frequency']='daily';variants.append(doc)
        for doc in variants:
            with self.assertRaises(ValueError):s.parse_eia(base.canonical(doc),'crude_inventory')

    def test_eia_rate_bounds(self):
        with self.assertRaises(ValueError):s.parse_eia(base.canonical(eia_doc('refinery_utilization','101')),'refinery_utilization')


class Storage(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.dbpath=self.root/'data.sqlite3';self.rawdir=self.root/'raw'
        self.db=s.connect(self.dbpath);self.addCleanup(self.db.close)

    def save(self, value=600, when='2026-10-13T00:00:00+00:00', day='2026-10-12', published=None, extra=None):
        raw=base.canonical(sc_doc(day.replace('-',''),value=value))
        h,p=base.archive(self.rawdir,raw,'.response')
        ev=dict(started_at=when,ingested_at=when,response_completed_at=when,method='GET',http_status=200,raw_hash=h,raw_path=p,url='https://synthetic.invalid')
        ev.update(extra or {})
        rows=s.parse_sc(raw,day)
        for row in rows:row['published_at']=published
        return base.persist(self.db,self.rawdir,'ine_sc',day,ev,rows)

    def test_idempotent_and_revision_a_b_a(self):
        self.assertEqual(self.save()['inserted_count'],4)
        self.assertEqual(self.save(when='2026-10-13T01:00:00+00:00')['inserted_count'],0)
        self.assertEqual(self.save(601,'2026-10-13T02:00:00+00:00')['inserted_count'],1)
        self.assertEqual(self.save(600,'2026-10-13T03:00:00+00:00')['inserted_count'],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM observation').fetchone()[0],6)

    def test_response_completion_cross_timezone(self):
        self.save()
        self.assertEqual(len(r.status(self.dbpath,'2026-10-13T07:59:59+08:00')['facts']),0)
        self.assertEqual(len(r.status(self.dbpath,'2026-10-12T20:00:00-04:00')['facts']),4)

    def test_known_future_publication_blocks(self):
        self.save(published='2026-10-14T00:00:00Z')
        self.assertFalse(r.status(self.dbpath,'2026-10-13T12:00:00Z')['facts'])

    def test_reprocessing_blocks_old_asof(self):
        self.save(extra={'reprocessed_at':'2026-10-14T00:00:00Z'})
        self.assertFalse(r.status(self.dbpath,'2026-10-13T12:00:00Z')['facts'])

    def test_latest_null_does_not_resurrect(self):
        self.save();self.save(None,'2026-10-13T01:00:00Z')
        facts=r.status(self.dbpath,'2026-10-13T02:00:00Z')['facts']
        self.assertIsNone(next(x for x in facts if x['metric']=='close')['value'])

    def test_revision_asof(self):
        self.save();self.save(610,'2026-10-14T00:00:00Z')
        facts=r.status(self.dbpath,'2026-10-13T12:00:00Z')['facts']
        self.assertEqual(next(x for x in facts if x['metric']=='close')['value'],600)

    def test_calendar_gap_not_invented_holiday(self):
        self.save(day='2026-10-09');self.save()
        gap=r.status(self.dbpath)['calendar_gaps'][0]
        self.assertEqual(gap['calendar_days'],2)
        self.assertIn('unclassified',gap['classification'])

    def test_staleness_local_day(self):
        self.save()
        report=r.status(self.dbpath,'2026-10-15T17:00:00Z')
        sc=next(x for x in report['sources'] if x['source']=='ine_sc')
        self.assertEqual(sc['age_calendar_days'],4);self.assertEqual(sc['staleness'],'stale')

    def test_tamper_blocks_report(self):
        self.save();next(self.rawdir.glob('*.response')).write_bytes(b'tampered')
        with self.assertRaises(ValueError):r.status(self.dbpath)

    def test_backup_restore_relocation(self):
        self.save();r.backup(self.dbpath,self.root/'backup')
        shutil.move(self.root/'backup',self.root/'moved')
        r.restore(self.root/'moved',self.root/'restored')
        self.assertEqual(len(r.status(self.root/'restored/crude.sqlite3')['facts']),4)
        with self.assertRaises(ValueError):r.restore(self.root/'moved',self.root/'restored')

    def test_backup_tamper(self):
        self.save();r.backup(self.dbpath,self.root/'backup')
        next((self.root/'backup/raw').glob('*.response')).write_bytes(b'bad')
        with self.assertRaises(ValueError):r.restore(self.root/'backup',self.root/'restored')

    def test_status_does_not_mutate_ma_catalog(self):
        before=dict(base.SOURCES);r.status(self.dbpath);self.assertEqual(base.SOURCES,before)

    def test_cli_local_report(self):
        self.save()
        with patch('sys.stdout',new_callable=io.StringIO) as stdout:
            self.assertEqual(main(['report','--db',str(self.dbpath),'--output',str(self.root/'report.json')]),0)
        self.assertFalse(json.loads(stdout.getvalue())['forecast_enabled'])


class Transport(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_missing_key_no_network(self):
        with patch.dict(os.environ,{},clear=True),patch.object(s,'urlopen') as network:
            raw,ev=s.acquire_eia('https://api.eia.gov/v2/',self.root,1)
        network.assert_not_called();self.assertIsNone(raw)
        self.assertEqual(ev['error_category'],'authentication_required')

    def test_eia_redacts_echo_and_key(self):
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self):return base.canonical(dict(eia_doc(),request={'params':{'api_key':'synthetic-secret'}},extra='synthetic-secret'))
        with patch.dict(os.environ,{'EIA_API_KEY':'synthetic-secret'}),patch.object(s,'urlopen',return_value=Response()):
            raw,ev=s.acquire_eia('https://api.eia.gov/v2/',self.root,1)
        self.assertNotIn(b'synthetic-secret',raw)
        self.assertNotIn('synthetic-secret',json.dumps(ev))
        self.assertNotEqual(ev['original_raw_hash'],ev['raw_hash'])
        self.assertIn('response_completed_at',ev)

    def test_eia_exception_suppressed(self):
        with patch.dict(os.environ,{'EIA_API_KEY':'synthetic-secret'}),patch.object(s,'urlopen',side_effect=URLError('synthetic-secret')):
            _,ev=s.acquire_eia('https://api.eia.gov/v2/',self.root,1)
        self.assertNotIn('synthetic-secret',json.dumps(ev))

    def test_eia_auth_failure_body_redaction(self):
        exc=HTTPError('https://invalid/?api_key=synthetic-secret',403,'forbidden',{},io.BytesIO(b'{"error":{"code":"API_KEY_INVALID","message":"synthetic-secret"}}'))
        with patch.dict(os.environ,{'EIA_API_KEY':'synthetic-secret'}),patch.object(s,'urlopen',side_effect=exc):
            raw,ev=s.acquire_eia('https://api.eia.gov/v2/',self.root,1)
        self.assertEqual(ev['error_category'],'authentication_required')
        self.assertNotIn(b'synthetic-secret',raw)

    def test_bad_timeout_and_retries(self):
        for timeout in [0,-1,math.inf,math.nan]:
            with self.assertRaises(ValueError):s.ingest(self.root/'db',self.root,[],['wti_futures'],timeout)
        with self.assertRaises(ValueError):s.ingest(self.root/'db',self.root,[],['wti_futures'],retries=4)

    def test_retry_retains_failure_and_success(self):
        now=base.utcnow();raw=base.canonical(sc_doc())
        h,p=base.archive(self.root,raw,'.response')
        failed=dict(started_at=now,ingested_at=now,error_category='timeout',error='synthetic timeout')
        success=dict(started_at=now,ingested_at=now,response_completed_at=now,method='GET',http_status=200,raw_hash=h,raw_path=p)
        with patch.object(base,'acquire',side_effect=[(None,failed),(raw,success)]),patch.object(s.time,'sleep'):
            result=s.ingest(self.root/'db',self.root,['2026-10-12'],['ine_sc'])
        self.assertEqual(len(result['attempts']),2);self.assertEqual(result['inserted_count'],4)

    def test_http200_waf_is_parse_failure(self):
        now=base.utcnow();raw=b'<html>WAF</html>';h,p=base.archive(self.root,raw,'.response')
        ev=dict(started_at=now,ingested_at=now,http_status=200,method='GET',raw_hash=h,raw_path=p)
        with patch.object(base,'acquire',return_value=(raw,ev)):
            result=s.ingest(self.root/'db',self.root,['2026-10-12'],['ine_sc'])
        self.assertEqual(result['inserted_count'],0);self.assertEqual(result['errors'][0]['error_category'],'parse_error')

    def test_unavailable_futures_no_spot_substitution(self):
        with patch.object(base,'acquire') as network:
            result=s.ingest(self.root/'db',self.root,[],['wti_futures','brent_futures'])
        network.assert_not_called();self.assertEqual(result['inserted_count'],0)
        self.assertTrue(all(a['error_category']=='unavailable' for a in result['attempts']))


class PublicFlows(unittest.TestCase):
    def fixture(self, refinery=False):
        header = 'STUB_1,STUB_2,10/2/26,9/25/26,Difference,10/3/25,' + ('Percent Change,10/4/24,Percent Change,10/2/26,10/3/25,Percent Change' if refinery else 'Difference,10/2/26,10/3/25,Percent Change,10/2/26,10/3/25,Percent Change')
        labels = [('Refiner Inputs and Utilization','Percent Utilization')] if refinery else [
            ('Crude Oil Supply','(1) Domestic Production'),('Products Supplied','(31) Finished Motor Gasoline'),('Products Supplied','(33) Distillate Fuel Oil')]
        length = 12 if refinery else 13
        return (header+'\n'+'\n'.join(','.join([a,b,'90','89']+['1']*(length-4)) for a,b in labels)).encode()

    def test_weekly_not_four_week_average(self):
        rows=s.parse_public_flows(self.fixture())
        self.assertEqual(len(rows),6)
        self.assertEqual(rows[0]['value'],90)
        self.assertEqual(rows[0]['observed_start'],'2026-09-26')
        self.assertIsNone(rows[0]['published_at'])

    def test_refinery_percent(self):
        rows=s.parse_public_flows(self.fixture(True),True)
        self.assertEqual(len(rows),2);self.assertEqual(rows[0]['unit'],'Percent')
        with self.assertRaises(ValueError):s.parse_public_flows(self.fixture(True).replace(b',90,',b',101,'),True)

    def test_flow_missing_and_duplicate(self):
        raw=self.fixture()
        for bad in [raw.replace(b'Finished Motor Gasoline',b'Unknown'),raw+b'\n'+raw.splitlines()[-1]]:
            with self.assertRaises(ValueError):s.parse_public_flows(bad)

    def test_flow_week_gap(self):
        with self.assertRaises(ValueError):s.parse_public_flows(self.fixture().replace(b'9/25/26',b'9/18/26'))
