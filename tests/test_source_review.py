"""Synthetic regression cases for the Issue #1 review; no network or market data."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from methanol import industry, sources


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = industry.connect(self.root / 'synthetic.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def row(self, value=100):
        return sources.record('close', value, 'CNY/t', '2026-10-12', '2026-10-12', 'MA2701')

    def evidence(self, hour=1):
        raw_hash, raw_path = sources.archive(self.root / 'raw', b'SYNTHETIC repeated body', '.response')
        return dict(started_at='2026-10-13T00:00:00Z', ingested_at='2026-10-13T00:00:02Z',
                    response_completed_at='2026-10-13T00:00:02Z',
                    reprocessed_at=f'2026-10-13T{hour:02d}:00:00Z', method='GET',
                    raw_hash=raw_hash, raw_path=raw_path, http_status=200)

    def save(self, row, hour=1):
        return sources.persist(self.db, self.root / 'raw', 'czce', '2026-10-12', self.evidence(hour), [row])

    def test_numeric_reversion_and_backup_keep_three_versions(self):
        for hour, value in enumerate((100, 101, 100), 1):
            self.assertEqual(self.save(self.row(value), hour)['inserted_count'], 1)
        self.assertEqual(self.save(self.row(), 4)['inserted_count'], 0)
        restored = sqlite3.connect(':memory:')
        restored.row_factory = sqlite3.Row
        self.db.backup(restored)
        try:
            self.assertEqual(list(self.db.iterdump()), list(restored.iterdump()))
            self.assertEqual(restored.execute('pragma integrity_check').fetchone()[0], 'ok')
            self.assertEqual(restored.execute('pragma foreign_key_check').fetchall(), [])
            for hour, value in enumerate((100, 101, 100), 1):
                self.assertEqual(sources.available_rows(restored, f'2026-10-13T{hour:02d}:00:00Z')[0]['value'], value)
        finally:
            restored.close()

    def test_null_or_quarantined_revision_suppresses_old_value(self):
        self.save(self.row())
        self.save(self.row(None), 2)
        self.assertEqual(len(sources.available_rows(self.db, '2026-10-13T01:59:59Z')), 1)
        self.assertEqual(sources.available_rows(self.db, '2026-10-13T02:00:00Z'), [])
        self.save(dict(self.row(102), quality='unverified'), 3)
        self.assertEqual(sources.available_rows(self.db, '2026-10-13T03:00:00Z'), [])

    def test_status_does_not_leak_reprocessing_before_available(self):
        self.save(self.row(), 2)
        report = sources.status(self.root / 'synthetic.sqlite', '2026-10-13T01:00:00Z')
        czce = next(r for r in report['sources'] if r['id'] == 'czce')
        self.assertEqual(czce['observation_count'], 0)
        self.assertIsNone(czce['latest_observed_end'])
        self.assertEqual(report['counts_all_time']['observation'], 1)  # explicitly all-time audit count

    def test_latest_selection_uses_availability_not_insert_order(self):
        self.save(self.row(103), 3)
        self.save(self.row(102), 2)
        cutoff = '2026-10-13T04:00:00Z'
        self.assertEqual(sources.latest_observations(self.db, cutoff)[0]['value'], 103)
        # Same shared selector drives industry status and cross-check.
        row = sources.record('port_inventory', 10, 't')
        sources.persist(self.db, self.root / 'raw', 'industry_hibor', None, self.evidence(3), [row])
        sources.persist(self.db, self.root / 'raw', 'industry_hibor', None, self.evidence(2), [dict(row, value=20)])
        report = industry.status(self.root / 'synthetic.sqlite', cutoff)
        self.assertEqual(report['observations'][0]['value'], 10)

    def test_failed_response_cannot_insert_verified_rows(self):
        with self.assertRaises(ValueError):
            sources.persist(self.db, self.root / 'raw', 'czce', None,
                            dict(self.evidence(), http_status=403), [dict(self.row(), quality='verified')])
        self.assertEqual(self.db.execute('select count(*) from observation').fetchone()[0], 0)

    def test_invalid_timeouts_rejected_before_requests_or_database_creation(self):
        for timeout in (0, -1, float('nan'), float('inf')):
            with self.subTest(timeout=timeout), patch('methanol.sources.urlopen') as request:
                for call in (
                    lambda: sources.acquire('https://example.invalid', self.root, timeout),
                    lambda: sources.acquire_akshare('2026-10-12', self.root, timeout),
                    lambda: sources.ingest(self.root / 'unused.sqlite', self.root, ['2026-10-12'], timeout=timeout),
                    lambda: industry.ingest(self.root / 'unused.sqlite', self.root, '2026-10-12', timeout=timeout),
                ):
                    with self.assertRaises(ValueError):
                        call()
                request.assert_not_called()
        self.assertFalse((self.root / 'unused.sqlite').exists())

    def test_requests_timeout_classes_are_not_generic_adapter_errors(self):
        for name in ('ConnectTimeout', 'ReadTimeout'):
            self.assertEqual(sources.error_category(type(name, (Exception,), {})('synthetic')), 'timeout')

    def test_industry_registration_does_not_expand_default_sources(self):
        with patch('methanol.sources.acquire', return_value=(b'SYNTHETIC invalid layout', self.evidence())), \
                patch('methanol.sources.acquire_akshare', return_value=(b'SYNTHETIC invalid layout', self.evidence())):
            result = sources.ingest(self.root / 'synthetic.sqlite', self.root / 'raw', ['2026-10-12'])
        self.assertTrue(result['attempts'])
        self.assertFalse(any(r['source'].startswith('industry_') for r in result['attempts']))

    def test_event_revision_order_compares_instants_not_timezone_strings(self):
        old = industry.event('diplomacy', 'synthetic', 'statement', 'announced')
        evidence = self.evidence()
        attempt = sources.persist(self.db, self.root / 'raw', 'industry_mfa', None, evidence, [])
        industry.persist_events(self.db, 'industry_mfa', attempt['attempt_id'], evidence, [old], '2026-10-13T01:00:00Z')
        industry.persist_events(self.db, 'industry_mfa', attempt['attempt_id'], evidence,
                                [dict(old, stage='withdrawn')], '2026-10-13T02:00:00Z')
        # Equivalent offset encoding can exist in an imported/older database.
        self.db.execute("UPDATE industry_event SET available_at='2026-10-13T09:00:00+08:00' WHERE stage='announced'")
        self.db.commit()
        self.assertEqual(industry.visible_events(self.db, '2026-10-13T03:00:00Z')[0]['stage'], 'withdrawn')

    def test_http_200_wrong_content_is_retained_but_not_ingested(self):
        with patch('methanol.sources.acquire', return_value=(b'<html>SYNTHETIC login</html>', self.evidence())):
            result = sources.ingest(self.root / 'synthetic.sqlite', self.root / 'raw', ['2026-10-12'], ['czce'])
        self.assertEqual(result['inserted_count'], 0)
        self.assertEqual(result['errors'][0]['error_category'], 'parse_error')
        self.assertEqual(self.db.execute('select http_status from ingest_attempt').fetchone()[0], 200)


if __name__ == '__main__':
    unittest.main()
