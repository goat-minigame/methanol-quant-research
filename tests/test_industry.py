"""Synthetic only. Online acceptance is a separate evidence/database audit."""
import json
import tempfile
import sqlite3
import unittest
from pathlib import Path
from methanol import sources as b
from methanol import industry as i

class IndustryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.db=i.connect(self.root/'test.sqlite')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def evidence(self,raw=b'SYNTHETIC'):
        h,p=b.archive(self.root/'raw',raw,'.response')
        return dict(started_at='2026-10-13T01:00:00Z',ingested_at='2026-10-13T01:00:02Z',
                    response_completed_at='2026-10-13T01:00:02Z',method='GET',url='https://example.invalid/',
                    raw_hash=h,raw_path=p,http_status=200)
    def save(self,rows,processed='2026-10-13T01:00:03Z',raw=b'SYNTHETIC'):
        e=self.evidence(raw);a=b.persist(self.db,self.root/'raw','industry_mfa',None,e,[])
        return i.persist_events(self.db,'industry_mfa',a['attempt_id'],e,rows,processed)
    def test_report_coverage_period_and_publication_not_assumed(self):
        raw='''<p>2026-07-12 16:05:52 测试作者</p><p>市场要闻与重要数据</p>
        <p>外采甲醇MTO企业开工率42.1%；MTO企业开工率52.2%</p>
        <p>甲醇港口库存吨100，其中江苏30吨、浙江30吨、广东20吨、福建20吨</p><p>市场分析</p>'''.encode()
        rows=i.parse_report(raw)
        self.assertEqual(len(rows),7);self.assertEqual([r['value'] for r in rows[:2]],[42.1,52.2])
        self.assertTrue(all(r['observed_start'] is None and r['published_at'] is None for r in rows))
        e=self.evidence(raw);b.persist(self.db,self.root/'raw','industry_hibor',None,e,rows)
        self.assertEqual(b.available_rows(self.db,'2026-10-14T00:00:00Z'),[])
        with self.assertRaises(ValueError):i.parse_report(raw.replace(b'42.1',b'142.1'))
    def test_portwatch_estimate_not_methanol_and_null_capacity(self):
        a=dict(date='2026-10-01',portid='chokepoint6',portname='Strait of Hormuz',n_total=3,n_tanker=1,capacity=None)
        raw=json.dumps({'features':[{'attributes':a}],'exceededTransferLimit':True}).encode()
        rows=i.parse_portwatch(raw);self.assertEqual(len(rows),3);self.assertIsNone(rows[2]['value'])
        self.assertIn('not_methanol',str(rows[0]['detail']))
        a['portid']='wrong'
        with self.assertRaises(ValueError):i.parse_portwatch(json.dumps({'features':[{'attributes':a}]}).encode())
    def test_ukmto_late_report_does_not_promote_api_time(self):
        a=dict(incidentIssuer='UKMTO',place='Strait of Hormuz',incidentNumber=1,sitecoreId='synthetic',
           utcDateCreated='2026-10-12T10:00:00Z',utcDateOfIncident='2026-10-12T09:59:00Z',
           incidentTypeName='Attack',otherDetails='UKMTO received a time-late report; authorities investigating.')
        row=i.parse_ukmto(json.dumps([a]).encode())[0]
        self.assertIsNone(row['event_start']);self.assertIsNone(row['published_at']);self.assertIn('delayed_report',row['quality'])
        a['otherDetails']='Incident Date: 03 OCT 2026 Incident Time: TBC'
        row=i.parse_ukmto(json.dumps([a]).encode())[0];self.assertEqual(row['event_start'],'2026-10-03');self.assertEqual(row['event_precision'],'day')
    def test_mfa_statement_is_not_confirmed_agreement_or_today(self):
        raw='''<p>王毅会见伊朗外长阿拉格齐</p><p>2026-07-25 01:23</p>
        <p>2026年7月24日，王毅会见伊朗外长。</p><p>王毅表示，支持谈判。</p><p>阿拉格齐通报，谈判意愿不变。</p>'''.encode()
        rows=i.parse_mfa(raw);self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]['event_start'],'2026-07-24');self.assertEqual(rows[1]['stage'],'stated_willingness')
        self.assertIsNone(rows[1]['detail']['agreement_reached'])
    def test_auction_headline_not_result_and_buyer_not_seller(self):
        raw='''<h1>招标结果</h1><p>销售产品竞卖公告</p><p>产品名称：甲醇</p><p>供货总量：100吨</p>
        <p>起拍单价：2000元/吨</p><p>正式竞价启动时间：2026年9月22日11:00</p>'''.encode()
        a=i.parse_auction(raw)[0]['detail']['auction'];self.assertEqual(a['result_status'],'planned_notice_only')
        self.assertIsNone(a['clearing_price']);self.assertIsNone(a['actual_quantity']);self.assertIsNone(a['unsold_quantity'])
        buyer=i.parse_auction('<p>甲醇采购流标公告</p>'.encode())[0]
        self.assertEqual(buyer['event_type'],'buyer_procurement_failure');self.assertEqual(buyer['detail']['auction']['direction'],'buyer_procurement')
    def test_event_first_seen_processed_revision_and_idempotence(self):
        old=i.event('diplomacy','test','statement','announced',detail={'text':'SYNTHETIC old'})
        self.assertEqual(self.save([old]),1)
        for time in ['2026-10-12T23:59:59Z','2026-10-13T01:00:01Z','2026-10-13T01:00:02Z']:
            self.assertEqual(i.visible_events(self.db,time),[])
        self.assertEqual(len(i.visible_events(self.db,'2026-10-13T01:00:03Z')),1)
        self.assertEqual(self.save([old],raw=b'SYNTHETIC page chrome changed'),0)
        new=dict(old,stage='withdrawn',detail={'text':'SYNTHETIC correction'})
        self.assertEqual(self.save([new],'2026-10-13T02:00:00Z'),1)
        self.assertEqual(i.visible_events(self.db,'2026-10-13T01:59:59Z')[0]['stage'],'announced')
        self.assertEqual(i.visible_events(self.db,'2026-10-13T02:00:00Z')[0]['stage'],'withdrawn')
        self.assertEqual(self.db.execute('select count(*) from industry_event_evidence').fetchone()[0],3)
    def test_future_publication_and_failed_response_blocked(self):
        row=i.event('diplomacy','future','statement','announced');row['published_at']='2026-10-14T00:00:00Z'
        self.save([row]);self.assertEqual(i.visible_events(self.db,'2026-10-13T23:59:59Z'),[])
        e=dict(self.evidence(),http_status=403,error_category='http_403')
        a=b.persist(self.db,self.root/'raw','industry_mfa',None,e,[])
        with self.assertRaises(ValueError):i.persist_events(self.db,'industry_mfa',a['attempt_id'],e,[row],'2026-10-13T02:00:00Z')
    def test_event_reversion_is_a_new_visible_revision(self):
        a=i.event('diplomacy','reversion','statement','announced')
        b=dict(a,stage='withdrawn')
        self.save([a]);self.save([b],'2026-10-13T02:00:00Z')
        self.assertEqual(self.save([a],'2026-10-13T03:00:00Z'),1)
        self.assertEqual(i.visible_events(self.db,'2026-10-13T02:59:59Z')[0]['stage'],'withdrawn')
        self.assertEqual(i.visible_events(self.db,'2026-10-13T03:00:00Z')[0]['stage'],'announced')
        self.assertEqual(self.save([a],'2026-10-13T04:00:00Z'),0)
    def test_auction_nulls_persist_and_forecast_off(self):
        raw='<p>销售产品竞卖公告</p><p>产品名称：甲醇</p>'.encode();rows=i.parse_auction(raw)
        self.save(rows)
        a=self.db.execute('select * from auction_fact').fetchone();self.assertIsNone(a['planned_quantity']);self.assertIsNone(a['unsold_quantity'])
        report=i.status(self.root/'test.sqlite','2026-10-14T00:00:00Z')
        self.assertFalse(report['forecast_enabled']);self.assertEqual(report['auction_verified_results'],0)
    def test_backup_restore_preserves_event_evidence_and_cutoff(self):
        row=i.event('diplomacy','backup','statement','announced')
        self.save([row])
        restored=sqlite3.connect(self.root/'restore.sqlite');self.db.backup(restored)
        restored.row_factory=sqlite3.Row
        self.assertEqual(list(self.db.iterdump()),list(restored.iterdump()))
        self.assertEqual(i.visible_events(restored,'2026-10-13T01:00:02Z'),[])
        self.assertEqual(len(i.visible_events(restored,'2026-10-13T01:00:03Z')),1)
        self.assertEqual(restored.execute('pragma integrity_check').fetchone()[0],'ok')
        self.assertEqual(restored.execute('pragma foreign_key_check').fetchall(),[])
        restored.close()

if __name__=='__main__':unittest.main()
