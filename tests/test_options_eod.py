"""All market values here are SYNTHETIC, not historical prices or exchange evidence."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from methanol.options import eod
from methanol.sources import archive, canonical

DAY = '2026-10-12'
FIRST = '2026-10-13T09:00:00+00:00'
PROCESSED = '2026-10-13T09:00:01+00:00'
LATER = '2026-10-13T10:00:00+00:00'


def fixture(kind='Option', close='12', volume='5'):
    title = '期权' if kind == 'Option' else '期货'
    codes = ['MA611C3000', 'MA611P3000', 'MA701C3000', 'MA701P3000'] if kind == 'Option' else ['MA611', 'MA701']
    return (f'郑州商品交易所{title}每日行情表({DAY})\n'
            + '合约代码|今收盘|今结算|成交量(手)|持仓量\n'
            + '\n'.join(f'{c}|{close}|13|{volume}|9' for c in codes)).encode()


class ParseTests(unittest.TestCase):
    def test_mapping(self):
        rows = eod.parse(fixture(), DAY, 'Option')
        self.assertEqual(rows[0]['contract'], 'MA2611C3000')
        self.assertEqual(rows[-1]['underlying'], 'MA2701')
        self.assertEqual(rows[-1]['option_type'], 'P')
        self.assertEqual(rows[-1]['strike'], 3000)

    def test_separate_settlement(self):
        row = eod.parse(fixture(), DAY, 'Option')[0]
        self.assertEqual((row['close'], row['settlement'], row['latest']), (12, 13, None))

    def test_no_trades(self):
        row = eod.parse(fixture(close='0', volume='0'), DAY, 'Option')[0]
        self.assertIsNone(row['close'])
        self.assertEqual(row['settlement'], 13)
        self.assertIn('no_trades_close_unavailable', row['flags'])

    def test_no_trade_nonzero_close(self):
        self.assertIsNone(eod.parse(fixture(volume='0'), DAY, 'Option')[0]['close'])

    def test_zero_close(self):
        self.assertIsNone(eod.parse(fixture(close='0'), DAY, 'Option')[0]['close'])

    def test_missing(self):
        row = eod.parse(fixture(close='--', volume='--'), DAY, 'Option')[0]
        self.assertIsNone(row['close'])
        self.assertIsNone(row['volume'])
        for field in ('expiry', 'multiplier', 'fees', 'bid', 'ask', 'spread', 'executable_capacity', 'iv_percentile'):
            self.assertIsNone(row[field])

    def test_nan_negative_fraction(self):
        for close, volume in [('nan', '5'), ('-1', '5'), ('12', '1.5')]:
            with self.subTest(close=close, volume=volume), self.assertRaises(ValueError):
                eod.parse(fixture(close=close, volume=volume), DAY, 'Option')

    def test_date_mismatch(self):
        with self.assertRaises(ValueError):
            eod.parse(fixture(), '2026-10-09', 'Option')

    def test_schema(self):
        with self.assertRaises(ValueError):
            eod.parse(fixture().replace('今收盘'.encode(), b'unknown'), DAY, 'Option')

    def test_duplicate(self):
        raw = fixture()
        with self.assertRaises(ValueError):
            eod.parse(raw + b'\n' + raw.splitlines()[-1], DAY, 'Option')

    def test_reject_continuous(self):
        with self.assertRaises(ValueError):
            eod.parse(fixture().replace(b'MA611C3000', b'MA0C3000'), DAY, 'Option')

    def test_absent_underlying(self):
        with self.assertRaises(ValueError):
            eod.parse(b'\n'.join(fixture().splitlines()[:-2]), DAY, 'Option')

    def test_wrong_kind(self):
        with self.assertRaises(ValueError):
            eod.parse(fixture(), DAY, 'Future')


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'options.sqlite3'
        self.raw = self.root / 'raw'
        self.close = '12'
        self.first = FIRST
        self.processed = PROCESSED

    def acquire(self, url, root, timeout):
        kind = 'Option' if '/Option/' in url else 'Future'
        raw = fixture(kind, close=self.close)
        h, path = archive(root, raw, '.response')
        return raw, dict(url=url, method='GET', http_status=200, started_at=FIRST,
                         response_completed_at=self.first, ingested_at=self.first,
                         raw_hash=h, raw_path=path)

    def ingest(self):
        with patch.object(eod, 'acquire', self.acquire), patch.object(eod, 'utcnow', return_value=self.processed):
            return eod.ingest(self.db, self.raw, DAY)

    def status(self, when=LATER):
        return eod.status(self.db, self.raw, when)

    def test_persist_idempotent(self):
        self.assertEqual(sum(r['inserted_snapshots'] for r in self.ingest()), 2)
        self.assertEqual(sum(r['inserted_snapshots'] for r in self.ingest()), 0)
        result = self.status()
        self.assertEqual(len(result['observations']), 6)
        self.assertEqual(len(result['attempts']), 4)
        self.assertFalse(result['integrity_errors'])

    def test_first_seen_not_historical(self):
        self.ingest()
        self.assertEqual(self.status('2026-10-12T16:00:00+00:00')['observations'], [])
        self.assertEqual(self.status(FIRST)['observations'], [])
        self.assertEqual(len(self.status(PROCESSED)['observations']), 6)

    def test_timezone_equivalence(self):
        self.ingest()
        self.assertEqual(self.status()['observations'], self.status('2026-10-13T18:00:00+08:00')['observations'])

    def test_naive_rejected(self):
        with self.assertRaises(ValueError):
            self.status('2026-10-13T18:00:00')

    def test_revision_as_of(self):
        self.ingest()
        self.close = '99'
        self.first = '2026-10-13T11:00:00+00:00'
        self.processed = '2026-10-13T11:00:01+00:00'
        self.ingest()
        self.assertEqual(self.status()['observations'][0]['close'], 12)
        self.assertEqual(self.status('2026-10-13T12:00:00+00:00')['observations'][0]['close'], 99)

    def test_repeated_old_response_does_not_replace_revision(self):
        self.ingest()
        self.close = '99'
        self.first = '2026-10-13T11:00:00+00:00'
        self.processed = '2026-10-13T11:00:01+00:00'
        self.ingest()
        self.close = '12'
        self.ingest()
        self.assertEqual(self.status('2026-10-13T12:00:00+00:00')['observations'][0]['close'], 99)

    def test_stale_is_no_signal(self):
        self.ingest()
        result = self.status()
        self.assertEqual(result['message'], '暂无合格信号')
        self.assertEqual(result['candidates'], [])
        self.assertTrue(all(r['freshness'] == 'stale' for r in result['observations']))

    def test_future_observation_rejected(self):
        self.first = '2026-10-11T01:00:00+00:00'
        self.assertTrue(all(r['error'] for r in self.ingest()))

    def test_network_failure_audited(self):
        with patch.object(eod, 'acquire', return_value=(None, {'error_category': 'timeout', 'error': 'timeout'})):
            result = eod.ingest(self.db, self.raw, DAY)
        self.assertTrue(all(r['error'] for r in result))
        self.assertEqual(len(list(self.raw.glob('*.attempt.json'))), 2)

    def test_raw_tamper_blocks(self):
        self.ingest()
        next(self.raw.glob('*.response')).write_bytes(b'corrupted')
        self.assertEqual(len(self.status()['integrity_errors']), 1)

    def test_evidence_tamper_blocks(self):
        self.ingest()
        next(self.raw.glob('*.attempt.json')).write_bytes(b'{}')
        self.assertEqual(len(self.status()['integrity_errors']), 1)

    def test_payload_tamper_blocks(self):
        self.ingest()
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE snapshot SET payload='[]'")
        self.assertEqual(len(self.status()['integrity_errors']), 2)

    def test_backup_restore(self):
        self.ingest()
        dest = self.root / 'backup'
        eod.backup(self.db, self.raw, dest)
        import shutil
        shutil.rmtree(self.raw)
        self.db.unlink()
        result = eod.status(dest / 'options.sqlite3', dest / 'raw', LATER)
        self.assertEqual(len(result['observations']), 6)
        self.assertFalse(result['integrity_errors'])
        with self.assertRaises(FileExistsError):
            eod.backup(dest / 'options.sqlite3', dest / 'raw', dest)

    def test_cli_report(self):
        self.ingest()
        from methanol.options.__main__ import main
        output = self.root / 'report.json'
        args = ['options', '--db', str(self.db), '--raw-dir', str(self.raw), 'report', '--as-of', LATER, '--output', str(output)]
        with patch('sys.argv', args):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.read_text())['candidates'], [])

    def test_same_day_still_no_candidates(self):
        self.first = '2026-10-12T09:00:00+00:00'
        self.processed = '2026-10-12T09:00:01+00:00'
        def fetch(url, root, timeout):
            raw, evidence = self.acquire(url, root, timeout)
            evidence['started_at'] = self.first
            return raw, evidence
        with patch.object(eod, 'acquire', fetch), patch.object(eod, 'utcnow', return_value=self.processed):
            eod.ingest(self.db, self.raw, DAY)
        result = self.status('2026-10-12T10:00:00+00:00')
        self.assertEqual(len(result['observations']), 6)
        self.assertTrue(all(r['freshness'] == 'same_calendar_day' for r in result['observations']))
        self.assertEqual(result['candidates'], [])

    def test_missing_underlying_not_substituted(self):
        self.ingest()
        with sqlite3.connect(self.db) as db:
            db.execute("DELETE FROM snapshot WHERE kind='Future'")
        row = self.status()['observations'][0]
        self.assertIsNone(row['underlying_close'])
        self.assertIn('missing_underlying_close', row['blockers'])

    def test_units_and_rules_fail_closed(self):
        self.ingest()
        row = self.status()['observations'][0]
        self.assertIsNone(row['price_unit'])
        self.assertFalse(row['rules_verified'])
        self.assertIn('unverified_rules_and_price_unit', row['blockers'])

    def test_unsupported_parser_quarantined(self):
        self.ingest()
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE snapshot SET parser_version='older'")
        self.assertEqual(len(self.status()['integrity_errors']), 2)
        self.assertEqual(self.status()['observations'], [])

    def test_source_redirect_rejected(self):
        def fetch(url, root, timeout):
            raw, evidence = self.acquire(url, root, timeout)
            evidence['final_url'] = 'https://example.invalid/data'
            return raw, evidence
        with patch.object(eod, 'acquire', fetch):
            result = eod.ingest(self.db, self.raw, DAY)
        self.assertTrue(all(r['error'] == 'unexpected source redirect' for r in result))
