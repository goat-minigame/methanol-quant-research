"""All payloads here are deliberately SYNTHETIC, never acceptance market evidence."""
import json
import io
import types
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from methanol.sources import (SOURCES, acquire, acquire_akshare, archive, available_rows, connect, digest,
    error_category, ingest, normalize_contract, parse_coal, parse_czce, parse_sina,
    parse_weekly, parse_cctd_prices, parse_scpi, parse_huaxiao, persist, record, response_error_category, status)


SYNTHETIC = '''合约行情日期：2026-10-12
品种月份|昨结算|今开盘|最高价|最低价|今收盘|今结算|涨跌1|涨跌2|成交量(手)|空盘量|增减量|成交额
MA0|9|9|9|9|9|9|0|0|1|1|0|1
MA701|1990|2000|2030|1990|2010|2005|20|15|1,234|8,765|10|20
小计||||||||||||
'''.encode()


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dbpath = self.root / 'test.sqlite'
        self.db = connect(self.dbpath)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def evidence(self, raw=b'SYNTHETIC test only', ingested='2026-10-13T06:00:00+00:00'):
        h, p = archive(self.root/'raw', raw, '.response')
        return dict(started_at=ingested, ingested_at=ingested, method='GET', url='https://example.invalid/',
                    request_hash=digest(b'SYNTHETIC request'), raw_hash=h, raw_path=p, http_status=200)

    def save(self, rows, raw=b'SYNTHETIC test only', ingested='2026-10-13T06:00:00+00:00'):
        return persist(self.db, self.root/'raw', 'czce', '2026-10-12', self.evidence(raw, ingested), rows)

    def test_contract_normalization_and_decade(self):
        for d in ('2026-10-09', '2026-10-12'):
            self.assertEqual(normalize_contract('MA701', d), 'MA2701')
            self.assertEqual(normalize_contract('ma2701', d), 'MA2701')
        self.assertEqual(normalize_contract('MA001', '2029-12-01'), 'MA3001')
        for symbol in ('MA0', 'MA713', 'MA601', 'MA1701', 'MA3701', 'MA701C3000'):
            with self.assertRaises(ValueError):
                normalize_contract(symbol, '2026-10-12')

    def test_czce_parse_and_no_reference_hardcode(self):
        rows = parse_czce(SYNTHETIC, '2026-10-12')
        self.assertEqual([r['value'] for r in rows], [2010, 2005, 1234, 8765])
        self.assertTrue(all(r['contract']=='MA2701' for r in rows))
        self.assertTrue(all(r['published_at'] is None for r in rows))
        self.assertEqual(parse_czce(SYNTHETIC.decode().encode('gb18030'), '2026-10-12'), rows)

    def test_czce_reject_wrong_date_duplicate_and_schema(self):
        payload = SYNTHETIC + SYNTHETIC.splitlines()[3] + b'\n'
        for raw, day in [(SYNTHETIC, '2026-10-09'), (payload,'2026-10-12'),
                         (SYNTHETIC.replace('今收盘'.encode(), b'NEW_FIELD'), '2026-10-12'),
                         (b'<html>error</html>', '2026-10-12')]:
            with self.assertRaises(ValueError):
                parse_czce(raw, day)

    def test_missing_is_null_zero_volume_preserved(self):
        rows = parse_czce(SYNTHETIC.replace(b'2010|2005', b'2010|--').replace(b'1,234', b'0'), '2026-10-12')
        self.assertIsNone(rows[1]['value'])
        self.assertIn('missing_value', rows[1]['quality'])
        self.assertEqual(rows[2]['value'], 0)
        for value in (b'NaN', b'Infinity', b'-1', b'0'):
            with self.assertRaises(ValueError):
                parse_czce(SYNTHETIC.replace(b'2010',value), '2026-10-12')

    def test_weekly_unknown_period_and_missing_fields(self):
        raw = '<h1>模拟周报 2026-10-12</h1><p>外采甲醇MTO开工率：51.23%</p><p>周频到港量：4.56万吨</p>'.encode()
        rows = parse_weekly(raw)
        mto = next(r for r in rows if r['metric']=='mto_operating_rate')
        self.assertEqual(mto['value'],51.23)
        self.assertIsNone(mto['observed_start'])
        self.assertIsNone(mto['published_at'])
        self.assertEqual(len([r for r in rows if r['value'] is None]),3)
        self.save(rows)
        self.assertEqual(available_rows(self.db, '2027-01-01T00:00:00Z'), [])
        with self.assertRaises(ValueError):
            parse_weekly(raw+raw)
        with self.assertRaises(ValueError):
            parse_weekly(b'<html>login</html>')

    def test_coal_only_producing_regions_and_unknown_specification(self):
        raw = '''<p>2026-10-08 山西 动力煤 5500大卡 含税 坑口价：501元/吨</p>
        <p>2026-10-09 山西 坑口价：502元/吨</p>
        <p>2026-10-09 秦皇岛 山西 平仓价：601元/吨</p>'''.encode()
        rows = parse_coal(raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['region'], '山西')
        self.assertEqual(rows[0]['detail']['tax_basis'], '含税')
        self.assertIn('incomplete_coal_specification', rows[1]['quality'])
        self.assertIsNone(rows[1]['published_at'])
        with self.assertRaises(ValueError):
            parse_coal('<p>秦皇岛平仓价：601元/吨</p>'.encode())

    def test_idempotent_versions_and_immutable_raw(self):
        rows = parse_czce(SYNTHETIC, '2026-10-12')
        self.assertEqual(self.save(rows)['inserted_count'],4)
        self.assertEqual(self.save(rows)['inserted_count'],0)
        revised = parse_czce(SYNTHETIC.replace(b'2010',b'2011'), '2026-10-12')
        self.assertEqual(self.save(revised, b'SYNTHETIC revision')['inserted_count'],4)
        self.assertEqual(self.db.execute('SELECT count(*) FROM ingest_attempt').fetchone()[0],3)
        evidence = self.evidence()
        Path(evidence['raw_path']).write_bytes(b'corruption')
        with self.assertRaises(ValueError):
            self.evidence()

    def test_changing_page_chrome_keeps_observations_and_all_evidence(self):
        rows = parse_czce(SYNTHETIC, '2026-10-12')
        self.assertEqual(self.save(rows)['inserted_count'], 4)
        self.assertEqual(self.save(rows, b'SYNTHETIC new navigation')['inserted_count'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM observation_evidence').fetchone()[0], 8)

    def test_report_original_section_not_ai_intro_or_sidebar(self):
        raw = '''<p>中国甲醇开工率99%</p><p>发布时间：2026-10-12 09:12</p>
        <p>华泰期货10月12日甲醇期货研报原文：</p>
        <p>中国甲醇开工率52.1%；外采甲醇MTO企业开工率41.2%；华东MTO企业周度采购量123吨</p>
        <p>市场分析</p><p>中国甲醇开工率98%</p>'''.encode()
        rows = {r['metric']: r for r in parse_weekly(raw)}
        self.assertEqual(rows['methanol_operating_rate']['value'], 52.1)
        self.assertEqual(rows['mto_operating_rate']['value'], 41.2)
        self.assertEqual(rows['east_china_mto_purchases']['value'], 123)
        self.assertIsNone(rows['mto_operating_rate']['published_at'])
        self.assertIsNone(rows['mto_operating_rate']['observed_end'])
        self.assertEqual(rows['mto_operating_rate']['detail']['publication_text'], '发布时间：2026-10-12 09:12')

    def test_public_coal_tiles_do_not_mix_port_prices(self):
        raw = ''.join(f'<div>{region}5500 坑口价</div><div>本期值</div><div>{value}</div>'
                      '<div>较上期</div><div>1</div><div>日期：2026-09-30 单位：元/吨</div>'
                      for region, value in [('山西', 500), ('陕西', 501), ('蒙西', 502)])
        rows = parse_cctd_prices((raw+'<div>更多数据</div>').encode())
        self.assertEqual([r['value'] for r in rows], [500, 501, 502])
        self.assertTrue(all(r['detail']['tax_basis'] is None for r in rows))

    def test_scpi_composite_is_not_pithead_and_duplicate_dates_rejected(self):
        raw = b'[{"name":"2026-09-18","age":"500.0"},{"name":"2026-10-09","age":"501"}]'
        rows = parse_scpi(raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['metric'], 'regional_coal_composite_price')
        self.assertIn('not_pithead', rows[0]['detail']['price_basis'])
        with self.assertRaises(ValueError):
            parse_scpi(raw.replace(b'2026-10-09', b'2026-09-18'))

    def test_spot_weekend_carry_null_and_explicit_year_range(self):
        raw = '''<input id="date_s" value="2026-10-09"><input id="date_e" value="2026-10-13">
        <table class="quotetable"><tr><th>区域名称</th><th>10月09日</th><th>10月10日</th>
        <th>10月13日</th><th>单位</th></tr><tr><td>测试产区</td><td>2000</td>
        <td>2000</td><td>-----</td><td>元/吨</td></tr></table>'''.encode()
        rows = parse_huaxiao(raw)
        self.assertEqual(rows[0]['observed_end'], '2026-10-09')
        self.assertIn('possible_carried_forward', rows[1]['quality'])
        self.assertIsNone(rows[2]['value'])
        self.assertIsNone(rows[0]['published_at'])
        with self.assertRaises(ValueError):
            parse_huaxiao(raw.replace(b'2026-10-09', b'2025-10-09'))

    def test_cutoff_publication_ingestion_revision_timezone(self):
        row = record('close', 2010, 'CNY/t', '2026-10-12','2026-10-12','MA2701', published='2026-10-13T13:00:00+08:00')
        self.save([row])
        self.assertEqual(available_rows(self.db,'2026-10-13T05:59:59Z'),[])
        self.assertEqual(len(available_rows(self.db,'2026-10-13T14:00:00+08:00')),1)
        revision = dict(row,value=2011,published_at='2026-10-14T01:00:00Z')
        self.save([revision],b'SYNTHETIC revision','2026-10-14T02:00:00Z')
        self.assertEqual(available_rows(self.db,'2026-10-13T07:00:00Z')[0]['value'],2010)
        self.assertEqual(available_rows(self.db,'2026-10-14T03:00:00Z')[0]['value'],2011)
        with self.assertRaises(ValueError):
            available_rows(self.db,'2026-10-13T06:00:00')

    def test_first_seen_completion_boundary_and_no_historical_backfill(self):
        rows = parse_czce(SYNTHETIC, '2026-10-12')
        evidence = self.evidence()
        evidence['started_at'] = '2026-10-13T05:59:00Z'
        evidence['response_completed_at'] = evidence['ingested_at']
        persist(self.db, self.root/'raw', 'czce', '2026-10-12', evidence, rows)
        for cutoff in ('2026-10-12T23:59:59Z', '2026-10-13T05:59:30Z', '2026-10-13T05:59:59.999999Z'):
            self.assertEqual(available_rows(self.db, cutoff), [])
        visible = available_rows(self.db, '2026-10-13T06:00:00Z')
        self.assertEqual(len(visible), 4)
        self.assertTrue(all(r['published_at'] is None and r['availability_basis']=='first_seen' for r in visible))

    def test_reprocessing_not_visible_before_processing_and_old_version_remains(self):
        row = record('close', 2010, 'CNY/t', '2026-10-12', '2026-10-12', 'MA2701')
        self.save([row])
        revised = dict(row, value=2011)
        evidence = self.evidence(raw=b'SYNTHETIC revision')
        evidence.update(reprocessed_at='2026-10-13T07:00:00Z', reprocessed_from_attempt=1)
        persist(self.db, self.root/'raw', 'czce', '2026-10-12', evidence, [revised])
        self.assertEqual(available_rows(self.db,'2026-10-13T06:59:59Z')[0]['value'],2010)
        visible = available_rows(self.db,'2026-10-13T07:00:00Z')
        self.assertEqual([r['value'] for r in visible],[2011])
        self.assertEqual(visible[0]['availability_basis'],'first_seen_and_reprocessed')

    def test_legacy_migration_preserves_rows_and_reads_hashed_reprocessing_evidence(self):
        self.save(parse_czce(SYNTHETIC, '2026-10-12'))
        before = [tuple(r) for r in self.db.execute('SELECT * FROM observation')]
        self.db.execute('DROP TABLE observation_availability')
        self.db.commit()
        migrated = connect(self.dbpath)
        self.assertEqual([tuple(r) for r in migrated.execute('SELECT * FROM observation')],before)
        self.assertEqual(len(available_rows(migrated,'2026-10-13T06:00:00Z')),4)
        self.assertTrue(all(r[0]=='legacy_acquire_completion' for r in migrated.execute('select time_quality from observation_availability')))
        migrated.close()

    def test_unknown_end_time_reprocessing_and_statistical_period_stay_blocked(self):
        row = record('close', 2010, 'CNY/t', '2026-10-12', '2026-10-12', 'MA2701')
        for evidence in [dict(self.evidence(), started_at='2026-10-13T07:00:00Z'),
                         dict(self.evidence(), method='unknown'),
                         dict(self.evidence(), reprocessed_from_attempt=1)]:
            # Different payload metadata only: use a different observation value per version.
            row = dict(row,value=row['value']+1)
            persist(self.db,self.root/'raw','czce','2026-10-12',evidence,[row])
        persist(self.db,self.root/'raw','quhe',None,self.evidence(),
                [record('mto_operating_rate',50,'%')])
        self.assertEqual(available_rows(self.db,'2026-10-20T00:00:00Z'),[])

    def test_future_observation_and_publication_unknown_excluded(self):
        for row in [record('x',1,'%', '2026-10-14','2026-10-14',published='2026-10-13T01:00:00Z'),
                    record('x',1,'%', '2026-10-12','2026-10-12'),
                    record('x',None,'%', '2026-10-12','2026-10-12',published='2026-10-13T01:00:00Z')]:
            self.save([row])
        self.assertEqual(available_rows(self.db,'2026-10-20T01:00:00Z'),[])

    def test_error_classes(self):
        self.assertEqual(error_category(URLError('Tunnel connection failed: 403 Forbidden')), 'proxy_connect_403')
        self.assertEqual(error_category(HTTPError('x',403,'Forbidden',{},None)), 'http_403')
        self.assertEqual(error_category(HTTPError('x',404,'Not Found',{},None)), 'http_404')
        self.assertEqual(error_category(URLError(socket.gaierror(-2,'no name'))), 'dns_error')
        self.assertEqual(error_category(TimeoutError()), 'timeout')
        self.assertEqual(error_category(ValueError('bad')), 'parse_error')
        self.assertEqual(response_error_category(403, {'server':'envoy'}, b'Domain forbidden'), 'proxy_domain_forbidden')

    def test_failure_preserved_no_response_fabricated(self):
        with patch('methanol.sources.urlopen',side_effect=URLError('Tunnel connection failed: 403 Forbidden')):
            result = ingest(self.dbpath,self.root/'raw',['2026-10-09','2026-10-12'],['czce'])
        self.assertEqual(result['inserted_count'],0)
        attempts=list(self.db.execute('SELECT * FROM ingest_attempt'))
        self.assertEqual(len(attempts),2)
        self.assertTrue(all(r['raw_hash'] is None for r in attempts))
        for r in attempts:
            self.assertEqual(digest(Path(r['evidence_path']).read_bytes()),r['evidence_hash'])
        report=status(self.dbpath)
        self.assertFalse(report['real_forecast_enabled'])
        self.assertFalse(report['minimum_forecast_conditions_met'])
        self.assertFalse(report['cross_check']['independent_verification'])
        self.assertEqual(report['counts_all_time']['forecast'],0)

    def test_sina_contract_lineage_not_assumed(self):
        raw=json.dumps([dict(d='2026-10-12',o=2000,h=2030,l=1990,c=2010,v=10,p=20)]).encode()
        rows=parse_sina(raw,['2026-10-12'])
        self.assertIsNone(rows[1]['value'])
        self.assertIn('response_contract_identity_unverified',rows[0]['quality'])
        with self.assertRaises(ValueError):
            parse_sina(raw,['2026-10-09'])
        with self.assertRaises(ValueError):
            parse_sina(raw,['2026-10-12'],'MA0')

    def test_successful_transport_parse_failure_is_logged(self):
        with patch('methanol.sources.acquire',return_value=(b'changed format',self.evidence(b'changed format'))):
            result=ingest(self.dbpath,self.root/'raw',['2026-10-12'],['czce'])
        self.assertEqual(result['attempts'][0]['error_category'],'parse_error')
        self.assertEqual(result['inserted_count'],0)

    def test_http_denial_response_bytes_are_retained(self):
        exc=HTTPError('http://example.invalid',403,'Forbidden',{'server':'envoy'},io.BytesIO(b'Domain forbidden'))
        with patch('methanol.sources.urlopen',side_effect=exc):
            raw,evidence=acquire('http://example.invalid',self.root/'raw',1)
        self.assertEqual(raw,b'Domain forbidden')
        self.assertEqual(evidence['error_category'],'proxy_domain_forbidden')
        self.assertEqual(Path(evidence['raw_path']).read_bytes(),raw)

    def test_akshare_empty_without_request_not_claimed_nontrading(self):
        fake_ak=types.SimpleNamespace(__version__='synthetic',get_czce_daily=lambda day: [])
        class Session:
            def send(self,*args,**kwargs):
                raise AssertionError('not called')
        fake_requests=types.SimpleNamespace(sessions=types.SimpleNamespace(Session=Session))
        with patch.dict('sys.modules',akshare=fake_ak,requests=fake_requests):
            raw,evidence=acquire_akshare('2026-10-12',self.root/'raw',1)
        self.assertIsNone(raw)
        self.assertEqual(evidence['error_category'],'adapter_empty_without_request')

    def test_akshare_preserves_policy_denial_and_actual_url(self):
        class Session:
            def send(self,request,**kwargs):
                return types.SimpleNamespace(status_code=403,headers={'server':'envoy'},
                                             content=b'Domain forbidden',reason='Forbidden')
        fake_requests=types.SimpleNamespace(sessions=types.SimpleNamespace(Session=Session))
        request=types.SimpleNamespace(url='http://www.czce.com.cn/synthetic-test',method='GET',headers={})
        def fetch(day):
            Session().send(request)
        fake_ak=types.SimpleNamespace(__version__='synthetic',get_czce_daily=fetch)
        with patch.dict('sys.modules',akshare=fake_ak,requests=fake_requests):
            raw,evidence=acquire_akshare('2026-10-12',self.root/'raw',1)
        self.assertEqual(evidence['error_category'],'proxy_domain_forbidden')
        self.assertEqual(evidence['url'],request.url)
        self.assertEqual(raw,b'Domain forbidden')
        self.assertEqual(len(evidence['requests']),1)

    def test_status_cutoff_and_port_inventory_unavailable(self):
        ingest(self.dbpath,self.root/'raw',['2026-10-12'],['port_inventory'])
        report=status(self.dbpath,'2000-01-01T00:00:00Z')
        self.assertTrue(all(s['latest_attempt'] is None for s in report['sources']))
        report=status(self.dbpath)
        port=next(s for s in report['sources'] if s['id']=='port_inventory')
        self.assertEqual(port['availability'],'unavailable')
        self.assertEqual(port['observation_count'],0)


if __name__ == '__main__':
    unittest.main()
