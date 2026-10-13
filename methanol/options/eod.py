"""Versioned CZCE daily chains with conservative first-seen availability."""
import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from ..sources import (acquire, archive, canonical, decode, digest,
                       normalize_contract, number, timestamp, utcnow)

PARSER_VERSION = 'ma-eod-2'
TARGETS = ('MA2611', 'MA2701')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS attempt (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, day TEXT NOT NULL,
 completed TEXT NOT NULL, evidence_hash TEXT NOT NULL, evidence_file TEXT NOT NULL,
 error TEXT, inserted INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS snapshot (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, day TEXT NOT NULL,
 raw_hash TEXT NOT NULL, parser_version TEXT NOT NULL,
 first_seen TEXT NOT NULL, available_at TEXT NOT NULL,
 payload TEXT NOT NULL, attempt_id INTEGER NOT NULL REFERENCES attempt(id),
 UNIQUE(kind,day,raw_hash,parser_version));
'''


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    return db


def source_url(kind, day):
    d = date.fromisoformat(day)
    if kind not in ('Option', 'Future'):
        raise ValueError('unknown source kind')
    return f'https://www.czce.com.cn/cn/DFSStaticFiles/{kind}/{d.year}/{d:%Y%m%d}/{kind}DataDaily.txt'


def parse(raw, day, kind):
    """Strict daily schema; absent trades never become executable zero prices."""
    source_url(kind, day)
    text = decode(raw)
    title = '期权' if kind == 'Option' else '期货'
    if not re.search(title + r'每日行情表\(' + re.escape(day) + r'\)', text[:200]):
        raise ValueError('missing/mismatched official daily title/date')
    lines = [line for line in text.splitlines() if '|' in line]
    if not lines:
        raise ValueError('missing daily table')
    headers = [c.strip() for c in lines[0].split('|')]
    required = {'合约代码', '今收盘', '今结算', '成交量(手)', '持仓量'}
    if not required.issubset(headers) or len(headers) != len(set(headers)):
        raise ValueError('daily schema changed')
    result, seen = [], set()
    pattern = r'(MA\d{3,4})([CP])(\d+(?:\.\d+)?)' if kind == 'Option' else r'(MA\d{3,4})'
    for line in lines[1:]:
        cells = [c.strip() for c in line.split('|')]
        code = cells[0]
        match = re.fullmatch(pattern, code)
        if not match:
            if code.startswith('MA') and not ('计' in code):
                raise ValueError('unrecognized MA code')
            continue
        underlying = normalize_contract(match[1], day)
        if underlying not in TARGETS:
            continue
        if len(cells) != len(headers) or code in seen:
            raise ValueError('changed columns or duplicate contract')
        seen.add(code)
        row = dict(zip(headers, cells))
        vol, oi = number(row['成交量(手)']), number(row['持仓量'])
        if any(v is not None and not v.is_integer() for v in (vol, oi)):
            raise ValueError('fractional lot count')
        close, settle = number(row['今收盘']), number(row['今结算'])
        flags = []
        if vol == 0:
            close = None
            flags.append('no_trades_close_unavailable')
        elif close == 0:
            close = None
            flags.append('zero_close_unusable')
        if settle == 0:
            settle = None
            flags.append('zero_settlement_unusable')
        normalized = underlying + (match[2] + match[3] if kind == 'Option' else '')
        result.append(dict(contract=normalized, raw_contract=code, underlying=underlying,
                           option_type=match[2] if kind == 'Option' else None,
                           strike=number(match[3]) if kind == 'Option' else None,
                           close=close, settlement=settle, volume=vol, open_interest=oi,
                           latest=None, bid=None, ask=None, spread=None, executable_capacity=None,
                           expiry=None, multiplier=None, fees=None, rules_verified=False,
                           quote_timestamp=None, published_at=None, observed_date=day,
                           granularity='daily', price_unit=None, price_unit_status='rule_unverified', count_unit='lot',
                           iv=None, delta=None, theta=None, iv_percentile=None,
                           flags=flags, raw_fields=row))
    for target in TARGETS:
        subset = [r for r in result if r['underlying'] == target]
        if not subset or (kind == 'Option' and {r['option_type'] for r in subset} != {'C', 'P'}):
            raise ValueError('requested underlying/chain absent: ' + target)
        if kind == 'Future' and len(subset) != 1:
            raise ValueError('duplicate normalized future')
    if len({r['contract'] for r in result}) != len(result):
        raise ValueError('duplicate normalized contract')
    return result


def ingest(db_path, root, day, timeout=20):
    date.fromisoformat(day)
    results = []
    with connect(db_path) as db:
        for kind in ('Option', 'Future'):
            raw, evidence = acquire(source_url(kind, day), root, timeout)
            evidence['parser_version'] = PARSER_VERSION
            rows, error, inserted = [], evidence.get('error'), 0
            if not evidence.get('error_category'):
                try:
                    if evidence.get('http_status') != 200 or evidence.get('method') != 'GET':
                        raise ValueError('not a successful GET response')
                    if evidence.get('final_url', evidence['url']) != source_url(kind, day):
                        raise ValueError('unexpected source redirect')
                    rows = parse(raw, day, kind)
                    first = timestamp(evidence['response_completed_at'])
                    if first < timestamp(evidence['started_at']):
                        raise ValueError('invalid response timing')
                    if date.fromisoformat(day) > first.astimezone(ZoneInfo('Asia/Shanghai')).date():
                        raise ValueError('future observation date')
                except (ValueError, TypeError, KeyError) as exc:
                    error = str(exc)
            if evidence.get('error_category') and not error:
                error = evidence['error_category']
            evidence['parse_error'] = error
            processed = utcnow()
            evidence['processed_at'] = processed
            eh, ep = archive(root, canonical(evidence), '.attempt.json')
            attempt = db.execute('INSERT INTO attempt(kind,day,completed,evidence_hash,evidence_file,error,inserted) VALUES(?,?,?,?,?,?,0)',
                                 (kind, day, processed, eh, Path(ep).name, error)).lastrowid
            if not error:
                available = max(timestamp(processed), first).isoformat()
                inserted = db.execute('''INSERT OR IGNORE INTO snapshot
                    (kind,day,raw_hash,parser_version,first_seen,available_at,payload,attempt_id)
                    VALUES(?,?,?,?,?,?,?,?)''', (kind, day, evidence['raw_hash'], PARSER_VERSION,
                    first.isoformat(), available, canonical(rows).decode(), attempt)).rowcount
                db.execute('UPDATE attempt SET inserted=? WHERE id=?', (inserted, attempt))
            results.append(dict(kind=kind, day=day, rows=len(rows) if not error else 0,
                                inserted_snapshots=inserted, error=error, evidence_hash=eh,
                                first_seen=evidence.get('response_completed_at'), raw_hash=evidence.get('raw_hash')))
    return results


def status(db_path, root, as_of=None):
    cutoff = timestamp(as_of or utcnow())
    today = cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date()
    observations, errors, selected = [], [], {}
    with connect(db_path) as db:
        # Compare timestamps as instants, never lexicographic user offsets.
        for s in db.execute('SELECT * FROM snapshot ORDER BY id'):
            if timestamp(s['available_at']) <= cutoff and date.fromisoformat(s['day']) <= today:
                selected[(s['kind'], s['day'])] = dict(s)
        attempts = [dict(a) for a in db.execute('SELECT * FROM attempt') if timestamp(a['completed']) <= cutoff]
        for key, s in selected.items():
            a = db.execute('SELECT * FROM attempt WHERE id=?', (s['attempt_id'],)).fetchone()
            try:
                eraw = (Path(root) / a['evidence_file']).read_bytes()
                if digest(eraw) != a['evidence_hash']:
                    raise ValueError('evidence hash mismatch')
                evidence = json.loads(eraw)
                raw = (Path(root) / (s['raw_hash'] + '.response')).read_bytes()
                if digest(raw) != s['raw_hash'] or evidence['raw_hash'] != s['raw_hash']:
                    raise ValueError('raw hash mismatch')
                if s['parser_version'] != PARSER_VERSION:
                    raise ValueError('unsupported archived parser version; reacquire required')
                rows = parse(raw, s['day'], s['kind'])
                if canonical(rows).decode() != s['payload']:
                    raise ValueError('payload differs from evidence')
                if (evidence.get('http_status') != 200 or evidence.get('method') != 'GET'
                        or evidence.get('final_url', evidence['url']) != source_url(s['kind'], s['day'])
                        or evidence['url'] != source_url(s['kind'], s['day'])):
                    raise ValueError('source mismatch')
                if (timestamp(evidence['response_completed_at']) != timestamp(s['first_seen'])
                        or timestamp(s['available_at']) < max(timestamp(evidence['processed_at']), timestamp(s['first_seen']))):
                    raise ValueError('availability mismatch')
                for row in rows:
                    row.update(source_url=evidence['url'], raw_hash=s['raw_hash'],
                               evidence_hash=a['evidence_hash'], first_seen=s['first_seen'],
                               available_at=s['available_at'], source='CZCE',
                               freshness='same_calendar_day' if s['day'] == today.isoformat() else 'stale',
                               age_calendar_days=(today-date.fromisoformat(s['day'])).days)
                    row['observation_only'] = True
                    row['blockers'] = ['missing_bid_ask', 'unverified_rules_and_price_unit', 'events_unverified']
                    if row['freshness'] == 'stale':
                        row['blockers'].append('stale_daily_observation')
                    if row['close'] is None:
                        row['blockers'].append('missing_close')
                    observations.append(row)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(dict(kind=s['kind'], day=s['day'], error=str(exc)))
    futures = {(r['underlying'], r['observed_date']): r for r in observations if r['option_type'] is None}
    for row in observations:
        if row['option_type']:
            future = futures.get((row['underlying'], row['observed_date']))
            row['underlying_close'] = future['close'] if future else None
            row['underlying_raw_hash'] = future['raw_hash'] if future else None
            if not future or future['close'] is None:
                row['blockers'].append('missing_underlying_close')
    summary = {}
    for row in observations:
        key = row['observed_date'] + '/' + row['underlying']
        counts = summary.setdefault(key, {'options': 0, 'futures': 0, 'missing_close': 0, 'stale': 0})
        counts['options' if row['option_type'] else 'futures'] += 1
        counts['missing_close'] += int(row['close'] is None)
        counts['stale'] += int(row['freshness'] == 'stale')
    return dict(summary=summary, as_of=cutoff.isoformat(), mode='daily_observation_only', message='暂无合格信号',
                candidates=[], observations=observations, integrity_errors=errors,
                attempts=attempts, blockers=['bid_ask_and_executable_liquidity_unavailable',
                    'expiry_multiplier_fees_unverified', 'events_not_integrated',
                    'iv_greeks_history_not_implemented'],
                freshness_policy='Same Shanghai calendar date only; no trading-calendar inference. Stale rows are historical observations only.',
                historical_replay='first_seen_and_processed_at_only; no backdated Friday signal')


def backup(db_path, root, destination):
    """Portable private backup: immutable evidence plus SQLite online backup."""
    import shutil
    dest = Path(destination)
    dest.mkdir(parents=True, exist_ok=False)
    with connect(db_path) as src:
        with sqlite3.connect(dest / 'options.sqlite3') as target:
            src.backup(target)
    shutil.copytree(root, dest / 'raw')
    report = status(dest / 'options.sqlite3', dest / 'raw')
    if report['integrity_errors']:
        raise ValueError('backup evidence verification failed')
    return str(dest)
