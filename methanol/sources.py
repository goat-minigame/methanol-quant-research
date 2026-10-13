"""Auditable acquisition only. No forecasts, trading, proxy changes or synthetic fallback."""
import hashlib
import json
import math
import re
import socket
import sqlite3
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from unittest.mock import patch

from .data import SOURCE as SINA_URL, parse_response

SOURCES = {
    'czce': ('official', 'czce', 'https://www.czce.com.cn/cn/DFSStaticFiles/Future/{year}/{day}/FutureDataDaily.txt'),
    'akshare_czce': ('official_via_adapter', 'czce', 'http://www.czce.com.cn/cn/DFSStaticFiles/Future/{year}/{day}/FutureDataDaily.txt'),
    'sina': ('third_party_unverified', 'sina_lineage_unverified', SINA_URL + '?' + urlencode({'symbol': 'MA2701'})),
    'quhe': ('republication', 'huatai_report', 'https://www.quheqihuo.com/news/202610124495511.html'),
    'cctd': ('public_candidate', 'cctd', 'https://www.cctd.com.cn/index.php?m=content&c=index&a=lists&catid=520'),
    'cctd_scpi': ('public_composite', 'cctd_scpi', 'https://www.cctd.com.cn/Echarts/data/SCPI.php'),
    'mysteel': ('public_candidate', 'mysteel', 'https://www.mysteel.com/'),
    'huaxiao_quotes': ('public_candidate', 'huaxiaowang', 'https://www.huaxiaowang.com/index/quote/lists'),
    'huaxiao_freight': ('public_candidate', 'huaxiaowang', 'https://www.huaxiaowang.com/index/quote/farelines'),
    '100ppi': ('public_candidate', '100ppi', 'https://www.100ppi.com/'),
    'jin10': ('public_candidate', 'jin10', 'https://datacenter-api.jin10.com/'),
    'port_inventory': ('unavailable', 'unknown', None),
}

DEFAULT_SOURCES = tuple(SOURCES)

SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
 id TEXT PRIMARY KEY, confidence TEXT NOT NULL, lineage TEXT NOT NULL, url TEXT,
 reuse_status TEXT NOT NULL DEFAULT 'unverified_public_access_only');
CREATE TABLE IF NOT EXISTS ingest_attempt (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL REFERENCES source(id), requested_date TEXT,
 started_at TEXT NOT NULL, ingested_at TEXT NOT NULL, url TEXT, request_hash TEXT,
 raw_hash TEXT, raw_path TEXT, evidence_hash TEXT NOT NULL, evidence_path TEXT NOT NULL,
 status TEXT NOT NULL, error_category TEXT, error TEXT, http_status INTEGER,
 inserted_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS observation (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL REFERENCES source(id),
 metric TEXT NOT NULL, value REAL, unit TEXT NOT NULL, region TEXT NOT NULL DEFAULT '',
 contract TEXT NOT NULL DEFAULT '', observed_start TEXT, observed_end TEXT,
 published_at TEXT, ingested_at TEXT NOT NULL, raw_hash TEXT NOT NULL,
 quality TEXT NOT NULL, detail TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
 attempt_id INTEGER NOT NULL REFERENCES ingest_attempt(id));
CREATE TABLE IF NOT EXISTS forecast (
 id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, as_of TEXT NOT NULL,
 value REAL, status TEXT NOT NULL CHECK(status='disabled'), reason TEXT NOT NULL,
 CHECK(value IS NULL));
CREATE TABLE IF NOT EXISTS forecast_verification (
 id INTEGER PRIMARY KEY, forecast_id INTEGER REFERENCES forecast(id),
 verified_at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS observation_time ON observation(metric, observed_end, published_at);
CREATE TABLE IF NOT EXISTS observation_evidence (
 observation_id INTEGER NOT NULL REFERENCES observation(id),
 attempt_id INTEGER NOT NULL REFERENCES ingest_attempt(id),
 PRIMARY KEY(observation_id, attempt_id));
CREATE TABLE IF NOT EXISTS observation_availability (
 observation_id INTEGER PRIMARY KEY REFERENCES observation(id),
 first_seen TEXT, processed_at TEXT, available_at TEXT,
 availability_basis TEXT NOT NULL, time_quality TEXT NOT NULL);
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return stamp.astimezone(timezone.utc)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def archive(root, raw, suffix):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / (digest(raw) + suffix)
    try:
        with path.open('xb') as stream:
            stream.write(raw)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise ValueError('raw archive hash collision or corruption')
    return digest(raw), str(path)


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    with db:
        for name, (confidence, lineage, url) in SOURCES.items():
            db.execute('''INSERT INTO source(id,confidence,lineage,url) VALUES(?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET url=excluded.url''',
                       (name, confidence, lineage, url))
        db.execute('INSERT OR IGNORE INTO observation_evidence SELECT id,attempt_id FROM observation')
        for row in db.execute('''SELECT o.*,a.evidence_path,a.evidence_hash
                FROM observation o JOIN ingest_attempt a ON a.id=o.attempt_id
                LEFT JOIN observation_availability v ON v.observation_id=o.id
                WHERE v.observation_id IS NULL''').fetchall():
            evidence = None
            paths = [Path(row['evidence_path']), Path(path).resolve().parent/'raw'/Path(row['evidence_path']).name]
            for candidate in paths:
                try:
                    raw = candidate.read_bytes()
                    if digest(raw) == row['evidence_hash']:
                        evidence = json.loads(raw)
                        if evidence.get('raw_hash') != row['raw_hash']:
                            evidence = None
                        break
                except (OSError, ValueError):
                    pass
            save_availability(db, row['id'], row['published_at'], evidence)
    return db


def save_availability(db, observation_id, published_at, evidence):
    """Add derived timing; never alter original evidence or invent publication times.

    Legacy acquire/acquire_akshare recorded ingested_at after response.read / send.
    A generic fetched_at/start timestamp is deliberately not an accepted substitute.
    """
    first_seen = processed = available = None
    basis, quality = 'unknown', 'unverified_response_completion'
    try:
        if (not evidence or evidence.get('http_status') != 200 or evidence.get('error_category')
                or not evidence.get('raw_hash') or evidence.get('method') not in ('GET', 'POST')):
            raise ValueError('no successful captured response')
        completed = evidence.get('response_completed_at') or evidence['ingested_at']
        end, start = timestamp(completed), timestamp(evidence['started_at'])
        if end < start:
            raise ValueError('response completes before request starts')
        first_seen = end.isoformat()
        processed = evidence.get('reprocessed_at')
        if 'reprocessed_from_attempt' in evidence and not processed:
            raise ValueError('reprocessing time missing')
        floor = max(end, timestamp(processed)) if processed else end
        if published_at:
            floor = max(floor, timestamp(published_at))
        available = floor.isoformat()
        basis = 'published_and_first_seen' if published_at else 'first_seen'
        if processed:
            processed = timestamp(processed).isoformat()
            basis += '_and_reprocessed'
        quality = ('explicit_response_completion' if evidence.get('response_completed_at')
                   else 'legacy_acquire_completion')
    except (KeyError, ValueError, TypeError):
        available, basis = None, 'unknown'
    db.execute('INSERT OR IGNORE INTO observation_availability VALUES(?,?,?,?,?,?)',
               (observation_id, first_seen, processed, available, basis, quality))


def normalize_contract(symbol, trading_date):
    """Resolve CZCE YMM against delivery month in [trade month, trade month+24]."""
    day = date.fromisoformat(trading_date)
    match = re.fullmatch(r'MA(\d{3}|\d{4})', str(symbol).strip().upper())
    if not match:
        raise ValueError('not an MA dated contract (MA0 excluded)')
    code = match[1]
    month = int(code[-2:])
    if not 1 <= month <= 12:
        raise ValueError('invalid delivery month')
    years = [y for y in range(day.year, day.year + 3)
             if str(y).endswith(code[:-2]) and 0 <= (y-day.year)*12+month-day.month <= 24]
    if len(years) != 1:
        raise ValueError('contract outside unambiguous 24-month validation window')
    return f'MA{years[0] % 100:02d}{month:02d}'


def number(value):
    text = str(value).strip().replace(',', '')
    if value is None or text in ('', '-', '--', '—', 'N/A', 'null'):
        return None
    result = float(text)
    if not math.isfinite(result) or result < 0:
        raise ValueError('invalid nonnegative finite value')
    return result


def decode(raw):
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError('unknown response encoding')


def record(metric, value, unit, start=None, end=None, contract='', region='', published=None, detail=None, flags=()):
    flags = set(flags)
    if value is None:
        flags.add('missing_value')
    if not start or not end:
        flags.add('unknown_observation_period')
    if not published:
        flags.add('unknown_publication_time')
    return dict(metric=metric, value=value, unit=unit, region=region, contract=contract,
                observed_start=start, observed_end=end, published_at=published,
                quality=','.join(sorted(flags)) if flags else 'verified', detail=detail or {})


def parse_czce(raw, trading_date, contract='MA2701'):
    text = decode(raw)
    target = normalize_contract(contract, trading_date)
    # The date must occur in the raw document, not merely in the request path.
    dates = re.findall(r'(20\d{2})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})', text[:1500])
    if trading_date not in [date(int(y), int(m), int(d)).isoformat() for y, m, d in dates]:
        raise ValueError('missing/mismatched response trading date')
    lines = [line.strip() for line in text.splitlines() if '|' in line]
    columns = None
    found = []
    aliases = {'close': ('收盘价', '今收盘'), 'settle': ('结算价', '今结算'),
               'volume': ('成交量', '成交量(手)', '成交量（手）'),
               'open_interest': ('持仓量', '空盘量')}
    for line in lines:
        cells = [c.strip().replace(' ', '') for c in line.split('|')]
        if cells[0] in ('品种月份', '品种代码', '合约代码'):
            columns = cells
            continue
        if columns is None or not re.fullmatch(r'MA\d{3,4}', cells[0]):
            continue
        if normalize_contract(cells[0], trading_date) != target:
            continue
        if len(cells) != len(columns):
            raise ValueError('CZCE column count changed')
        row = dict(zip(columns, cells))
        for metric, keys in aliases.items():
            key = next((k for k in keys if k in row), None)
            if key is None:
                raise ValueError('CZCE required header missing: ' + metric)
            value = number(row[key])
            if value is not None and ((metric in ('close', 'settle') and value <= 0)
                                      or (metric in ('volume', 'open_interest') and not value.is_integer())):
                raise ValueError('invalid price/count')
            found.append(record(metric, value, 'CNY/t' if metric in ('close', 'settle') else 'lot',
                                trading_date, trading_date, target,
                                detail={'raw_symbol': cells[0], 'excerpt': line}))
    if len(found) != 4:
        raise ValueError('target contract absent or duplicated; not proof of non-trading day')
    return found


def parse_sina(raw, days, contract='MA2701'):
    bars = parse_response(raw)
    rows = []
    for day in days:
        target = normalize_contract(contract, day)
        matches = [bar for bar in bars if bar['date'] == day]
        if len(matches) != 1:
            raise ValueError('requested date absent from Sina: ' + day)
        for metric, key in [('close', 'close'), ('settle', 'settle'), ('volume', 'volume'), ('open_interest', 'hold')]:
            rows.append(record(metric, matches[0][key], 'CNY/t' if metric in ('close', 'settle') else 'lot',
                               day, day, target, detail={'requested_symbol': contract},
                               flags=['response_contract_identity_unverified', 'source_lineage_unverified']))
    return rows


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'div', 'br', 'tr', 'li', 'h1', 'h2'):
            self.parts.append('\n')
        if tag in ('td', 'th'):
            self.parts.append(' | ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden-1)
        if tag in ('p', 'div', 'tr', 'li'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def page_lines(raw):
    page = PageText()
    page.feed(decode(raw))
    return [' '.join(line.split()) for line in ''.join(page.parts).splitlines() if line.strip()]


def parse_weekly(raw):
    """Candidate text extraction, always quarantined until real-layout validation.

    No article date is promoted to observation date; precise publication is not inferred.
    """
    lines = page_lines(raw)
    specs = {
        'methanol_operating_rate': (r'中国甲醇开工率', '%'),
        'coal_methanol_operating_rate': (r'煤制甲醇开工率', '%'),
        'mto_operating_rate': (r'外采甲醇MTO(?:企业)?开工率', '%'),
        'weekly_arrivals': (r'(?:周频到港量|周度到港量)', '万吨'),
        'east_china_mto_purchases': (r'华东MTO(?:企业)?周(?:度)?采购量', '吨'),
    }
    publication = next((line for line in lines if re.fullmatch(r'发布时间：20\d{2}-\d{2}-\d{2} \d{2}:\d{2}', line)), None)
    # Exclude the explicitly AI-generated introduction, sidebar and related articles.
    start = next((i for i, line in enumerate(lines) if '甲醇期货研报原文：' in line), None)
    actual_layout = start is not None
    if actual_layout:
        stop = next((i for i in range(start+1, len(lines)) if lines[i] == '市场分析'), len(lines))
        lines = lines[start+1:stop]
    records = []
    for metric, (label, unit) in specs.items():
        matches = [(m, line) for line in lines for m in re.finditer(
            label + r'\s*[:：为约]?\s*([\d,]+(?:\.\d+)?)\s*' + re.escape(unit), line, re.I)]
        if len(matches) > 1:
            raise ValueError('ambiguous repeated weekly metric: ' + metric)
        if not matches:
            records.append(record(metric, None, unit, detail={'reason': 'field_not_found'}, flags=['parser_unverified']))
            continue
        match, line = matches[0]
        value = number(match[1])
        if unit == '%' and value > 100:
            raise ValueError('invalid operating rate')
        # Keep original time wording for manual review. No guessed week ranges.
        records.append(record(metric, value, unit, detail={'excerpt': line, 'publication_text': publication,
            'publication_timezone': None, 'period_status': 'not_explicit_in_original_report',
            'parser_version': 'quhe-original-v2'},
            flags=['publication_timezone_unverified'] if actual_layout else ['parser_unverified']))
    if not any(row['value'] is not None for row in records):
        raise ValueError('no weekly metric recognized; possible response structure change')
    return records


def parse_coal(raw):
    """Strict candidate for explicitly dated producing-region pithead price lines.

    Home pages may expose no series. Port prices cannot satisfy this parser.
    Missing heat/tax/grade is retained as unknown, never filled from Issue text.
    """
    rows = []
    for line in page_lines(raw):
        region = re.search(r'(山西|陕西|蒙西)', line)
        day = re.search(r'(20\d{2}-\d{2}-\d{2})', line)
        price = re.search(r'坑口(?:价|价格)\s*[:：]?\s*([\d,]+(?:\.\d+)?)\s*元/吨', line)
        if not region or not day or not price or '秦皇岛' in line:
            continue
        observed = date.fromisoformat(day[1]).isoformat()
        heat = re.search(r'(\d{4})\s*(?:大卡|kcal/kg)', line, re.I)
        coal = re.search(r'(动力煤|无烟煤|烟煤|褐煤)', line)
        tax = next((x for x in ('不含税', '含税') if x in line), None)
        detail = dict(excerpt=line, price_basis='pithead', calorific_value=heat[1] if heat else None,
                      coal_type=coal[1] if coal else None, tax_basis=tax)
        flags = ['parser_unverified', 'observation_date_role_unverified']
        if not heat or not coal or not tax:
            flags.append('incomplete_coal_specification')
        rows.append(record('producing_region_coal_price', number(price[1]), 'CNY/t', observed, observed,
                           region=region[1], detail=detail, flags=flags))
    if not rows:
        raise ValueError('no dated producing-region pithead series; source/article discovery still required')
    return rows


def parse_cctd_prices(raw):
    """Public producing-region tiles; date is a labelled series date, not publication."""
    lines = page_lines(raw)
    rows = []
    for i, line in enumerate(lines):
        m = re.fullmatch(r'(山西|陕西|蒙西)(\d{4}) 坑口价', line)
        if not m:
            continue
        block = lines[i:i+7]
        if len(block) != 7 or block[1] != '本期值' or block[3] != '较上期':
            raise ValueError('CCTD price tile layout changed')
        # The date follows the change value (six lines total on current layout).
        day = next((re.fullmatch(r'日期：?(20\d{2}-\d{2}-\d{2}) 单位：元/吨', x) for x in block
                    if x.startswith('日期')), None)
        if not day:
            raise ValueError('CCTD tile date/unit missing')
        observed = date.fromisoformat(day[1]).isoformat()
        rows.append(record('producing_region_coal_price', number(block[2]), 'CNY/t',
            observed, observed, region=m[1], detail={'excerpt': ' / '.join(block[:6]),
            'coal_type': '动力煤', 'calorific_value': m[2], 'tax_basis': None,
            'price_basis': 'pithead', 'date_role': 'source_labelled_series_date'},
            flags=['tax_basis_unknown', 'observation_date_role_unverified']))
    if len(rows) != 3:
        raise ValueError('CCTD three public producing-region tiles not found')
    return rows


def parse_scpi(raw):
    """The public SCPI chart is a Shaanxi composite, never a pithead cost proxy."""
    payload = json.loads(decode(raw))
    if not isinstance(payload, list) or not payload:
        raise ValueError('SCPI chart empty or changed')
    rows, seen = [], set()
    for item in payload:
        day = date.fromisoformat(item['name']).isoformat()
        if day in seen:
            raise ValueError('duplicate SCPI series date')
        seen.add(day)
        rows.append(record('regional_coal_composite_price', number(item['age']), 'CNY/t',
            day, day, region='陕西', detail={'series': 'SCPI', 'excerpt': item,
            'price_basis': 'regional_composite_not_pithead', 'coal_type': None,
            'calorific_value': None, 'tax_basis': None,
            'date_role': 'chart_label_not_verified_observation_period',
            'definition_url': SOURCES['cctd'][2]},
            flags=['incomplete_coal_specification', 'observation_date_role_unverified']))
    return rows


class QuoteTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.dates = [], None, None, {}
        self.active = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('id') in ('date_s', 'date_e'):
            self.dates[attrs['id']] = date.fromisoformat(attrs['value'])
        if tag == 'table' and 'quotetable' in attrs.get('class', '').split():
            self.active = True
        if self.active and tag == 'tr':
            self.row = []
        if self.active and tag in ('td', 'th'):
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if self.active and tag in ('td', 'th') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell = None
        if self.active and tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        if tag == 'table':
            self.active = False


def parse_huaxiao(raw, freight=False):
    table = QuoteTable()
    table.feed(decode(raw))
    if not table.rows or set(table.dates) != {'date_s', 'date_e'}:
        raise ValueError('Huaxiao public table/date range missing')
    head, *body = table.rows
    offset = 2 if freight else 1
    expected = ['装货地', '卸货地'] if freight else ['区域名称']
    if head[:offset] != expected or '单位' not in head:
        raise ValueError('Huaxiao table headers changed')
    unit_index = head.index('单位')
    days = []
    for label in head[offset:unit_index]:
        m = re.fullmatch(r'(\d{2})月(\d{2})日', label)
        if not m:
            raise ValueError('Huaxiao date label changed')
        candidates = [date(y, int(m[1]), int(m[2])) for y in
                      range(table.dates['date_s'].year, table.dates['date_e'].year+1)]
        candidates = [d for d in candidates if table.dates['date_s'] <= d <= table.dates['date_e']]
        if len(candidates) != 1:
            raise ValueError('ambiguous Huaxiao year')
        days.append(candidates[0])
    if len(set(days)) != len(days) or days != sorted(days):
        raise ValueError('Huaxiao duplicate/unordered dates')
    rows = []
    for cells in body:
        if len(cells) != len(head) or cells[unit_index] != '元/吨':
            raise ValueError('Huaxiao row shape/unit changed')
        previous = None
        for i, day in enumerate(days, offset):
            value = None if cells[i] == '-----' else number(cells[i])
            flags = ['quote_update_time_unverified', 'specification_unverified']
            if day.weekday() >= 5 or (value is not None and value == previous):
                flags.append('possible_carried_forward')
            rows.append(record('methanol_freight' if freight else 'methanol_spot_quote', value,
                'CNY/t', day.isoformat(), day.isoformat(), region=' → '.join(cells[:offset]),
                detail={'excerpt': cells[:offset]+[head[i], cells[i]],
                        'date_role': 'display_date_not_confirmed_new_quote',
                        'tax_basis': None, 'grade': None}, flags=flags))
            previous = value
    if not rows:
        raise ValueError('Huaxiao empty public table')
    return rows


def error_category(exc):
    message = str(exc).lower()
    if 'tunnel connection failed: 403' in message or 'connect tunnel failed' in message:
        return 'proxy_connect_403'
    if isinstance(exc, HTTPError):
        return 'http_' + str(exc.code)
    if 'timeout' in type(exc).__name__.lower():
        return 'timeout'
    if 'proxyerror' in type(exc).__name__.lower():
        return 'proxy_error'
    reason = getattr(exc, 'reason', exc)
    if isinstance(reason, socket.gaierror) or 'name or service not known' in message or 'nameresolutionerror' in message:
        return 'dns_error'
    if isinstance(reason, (TimeoutError, socket.timeout)) or 'timed out' in message:
        return 'timeout'
    if 'ssl' in type(reason).__name__.lower() or 'certificate verify failed' in message:
        return 'tls_error'
    if isinstance(exc, (ValueError, KeyError, IndexError, UnicodeError)):
        return 'parse_error'
    if isinstance(exc, ImportError):
        return 'dependency_missing'
    return 'network_error' if isinstance(exc, (URLError, OSError)) else 'adapter_error'


def response_error_category(code, headers, body):
    # An explicit environment denial body is evidence of proxy policy, not origin refusal.
    if code == 403 and body.strip() == b'Domain forbidden' and headers.get('server', '').lower() == 'envoy':
        return 'proxy_domain_forbidden'
    return 'http_' + str(code)


def validate_timeout(timeout):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be positive and finite')


def acquire(url, raw_dir, timeout, method='GET'):
    validate_timeout(timeout)
    evidence = {'started_at': utcnow(), 'url': url, 'method': method,
                'headers': {'User-Agent': 'methanol-source-audit/1.0'}}
    evidence['request_hash'] = digest(canonical({k: evidence[k] for k in ('url', 'method', 'headers')}))
    raw = None
    try:
        with urlopen(Request(url, headers=evidence['headers'], method=method, data=b'' if method == 'POST' else None), timeout=timeout) as response:
            raw = response.read()
            evidence.update(http_status=response.status, response_headers=dict(response.headers), final_url=response.url)
    except HTTPError as exc:
        raw = exc.read()
        evidence.update(http_status=exc.code, response_headers=dict(exc.headers),
                        error_category=response_error_category(exc.code, exc.headers, raw), error=str(exc))
    except Exception as exc:
        evidence.update(error_category=error_category(exc), error=str(exc))
    evidence['ingested_at'] = utcnow()
    if raw is not None and not evidence.get('error_category'):
        evidence['response_completed_at'] = evidence['ingested_at']
    if raw is not None:
        evidence['raw_hash'], evidence['raw_path'] = archive(raw_dir, raw, '.response')
    return raw, evidence


def acquire_akshare(day, raw_dir, timeout):
    """Observe AKShare's real requests without changing URL/proxy/redirect policy.

    Run serially: temporary requests interception is confined to this synchronous CLI.
    Use archived raw bytes for strict parsing, never AKShare's zero-filled frame.
    """
    validate_timeout(timeout)
    fallback = SOURCES['akshare_czce'][2].format(year=day[:4], day=day.replace('-', ''))
    evidence = {'started_at': utcnow(), 'url': fallback, 'method': 'GET',
                'request_hash': digest(canonical({'function': 'get_czce_daily', 'date': day}))}
    raw = None
    captures = []
    try:
        import akshare
        import requests
        evidence['akshare_version'] = akshare.__version__
        original = requests.sessions.Session.send

        def send(session, request, **kwargs):
            item = {'url': request.url, 'method': request.method,
                    'request_hash': digest(canonical({'url': request.url, 'method': request.method,
                                                     'headers': dict(request.headers)}))}
            kwargs['timeout'] = timeout
            try:
                response = original(session, request, **kwargs)
                item.update(http_status=response.status_code, response_headers=dict(response.headers))
                body = response.content
                item['raw_hash'], item['raw_path'] = archive(raw_dir, body, '.response')
                captures.append(item)
                # Check before AKShare can silently turn HTTP errors into an empty frame.
                if response.status_code >= 400:
                    raise HTTPError(request.url, response.status_code, response.reason, response.headers, None)
                return response
            except Exception as exc:
                item.update(error_category=(response_error_category(exc.code, item.get('response_headers', {}), body)
                                           if isinstance(exc, HTTPError) else error_category(exc)), error=str(exc))
                if item not in captures:
                    captures.append(item)
                raise

        with patch.object(requests.sessions.Session, 'send', send):
            frame = akshare.get_czce_daily(day.replace('-', ''))
        evidence['adapter_row_count'] = len(frame)
        if not captures:
            evidence.update(error_category='adapter_empty_without_request',
                            error='AKShare returned without HTTP; calendar/coverage not independently verified')
    except Exception as exc:
        evidence.update(error_category=error_category(exc), error=str(exc))
    if captures:
        evidence.update(captures[-1])
        evidence['requests'] = captures
        if evidence.get('raw_path'):
            raw = Path(evidence['raw_path']).read_bytes()
    evidence['ingested_at'] = utcnow()
    if raw is not None and not evidence.get('error_category'):
        evidence['response_completed_at'] = evidence['ingested_at']
    return raw, evidence


def persist(db, raw_dir, source, day, evidence, rows):
    # Every attempt retained; byte-identical observation versions are idempotent.
    evidence = dict(evidence)
    if rows and (evidence.get('error_category') or evidence.get('http_status') != 200):
        raise ValueError('cannot persist observations from failed response')
    evidence['source'] = source
    evidence['requested_date'] = day
    evidence['parsed_row_count'] = len(rows)
    evidence_hash, evidence_path = archive(raw_dir, canonical(evidence), '.attempt.json')
    status = ('unavailable' if evidence.get('error_category') in ('unavailable', 'source_discovery_required')
              else 'failed' if evidence.get('error_category') else 'quarantined' if rows else 'unavailable')
    if rows and all(row['quality'] == 'verified' for row in rows):
        status = 'success'
    with db:
        cursor = db.execute('''INSERT INTO ingest_attempt(source,requested_date,started_at,ingested_at,url,
            request_hash,raw_hash,raw_path,evidence_hash,evidence_path,status,error_category,error,http_status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (source, day, evidence['started_at'], evidence['ingested_at'], evidence.get('url'),
             evidence.get('request_hash'), evidence.get('raw_hash'), evidence.get('raw_path'),
             evidence_hash, evidence_path, status, evidence.get('error_category'), evidence.get('error'), evidence.get('http_status')))
        attempt_id = cursor.lastrowid
        inserted = 0
        for row in rows:
            # Compare only the latest revision of this series, not any historical value.
            # A -> B -> A must create a third version even when raw bytes repeat.
            values = [row[k] for k in ('metric', 'value', 'unit', 'region', 'contract', 'observed_start', 'observed_end', 'published_at')]
            candidates = db.execute("""SELECT * FROM observation WHERE source=? AND metric=?
                AND unit=? AND region=? AND contract=? AND observed_start IS ? AND observed_end IS ?
                ORDER BY id DESC""", [source, *[row[k] for k in
                    ('metric', 'unit', 'region', 'contract', 'observed_start', 'observed_end')]])
            previous = next((r for r in candidates if series_key(dict(r)) == series_key(dict(row, source=source))), None)
            same = previous and all(previous[k] == (canonical(v).decode() if k == 'detail' else v)
                                    for k, v in row.items())
            if same:
                db.execute('INSERT OR IGNORE INTO observation_evidence VALUES(?,?)', (previous['id'], attempt_id))
                continue
            fingerprint = digest(canonical({'source': source, 'raw_hash': evidence['raw_hash'], **row,
                                             'predecessor_id': previous['id'] if previous else None}))
            result = db.execute('''INSERT OR IGNORE INTO observation(source,metric,value,unit,region,contract,
                observed_start,observed_end,published_at,ingested_at,raw_hash,quality,detail,fingerprint,attempt_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                [source, *values, evidence['ingested_at'], evidence['raw_hash'], row['quality'],
                 canonical(row['detail']).decode(), fingerprint, attempt_id])
            inserted += result.rowcount
            if result.rowcount:
                db.execute('INSERT INTO observation_evidence VALUES(?,?)', (result.lastrowid, attempt_id))
                save_availability(db, result.lastrowid, row['published_at'], evidence)
        db.execute('UPDATE ingest_attempt SET inserted_count=? WHERE id=?', (inserted, attempt_id))
    return dict(source=source, requested_date=day, status=status, inserted_count=inserted,
                error_category=evidence.get('error_category'), error=evidence.get('error'), attempt_id=attempt_id)


def ingest(db_path, raw_dir, days, names=None, timeout=20):
    validate_timeout(timeout)
    days = sorted(set(date.fromisoformat(d).isoformat() for d in days))
    if not days:
        raise ValueError('at least one explicit trading date required')
    names = names or list(DEFAULT_SOURCES)
    if any(name not in SOURCES for name in names):
        raise ValueError('unknown source')
    results = []
    db = connect(db_path)
    try:
        for name in names:
            for day in days if name in ('czce', 'akshare_czce') else [None]:
                url = SOURCES[name][2]
                rows = []
                if not url:
                    now = utcnow()
                    evidence = dict(started_at=now, ingested_at=now, error_category='unavailable',
                                    error='No verified legal public port inventory series; warehouse receipts are not a substitute')
                    raw = None
                elif name == 'akshare_czce':
                    raw, evidence = acquire_akshare(day, raw_dir, timeout)
                else:
                    if day:
                        url = url.format(year=day[:4], day=day.replace('-', ''))
                    raw, evidence = (acquire(url, raw_dir, timeout, method='POST') if name == 'cctd_scpi'
                                     else acquire(url, raw_dir, timeout))
                if not evidence.get('error_category'):
                    try:
                        if name in ('czce', 'akshare_czce'):
                            rows = parse_czce(raw, day)
                        elif name == 'sina':
                            rows = parse_sina(raw, days)
                        elif name == 'quhe':
                            rows = parse_weekly(raw)
                        elif name == 'cctd':
                            rows = parse_cctd_prices(raw)
                        elif name == 'cctd_scpi':
                            rows = parse_scpi(raw)
                        elif name == 'mysteel':
                            rows = parse_coal(raw)
                        elif name in ('huaxiao_quotes', 'huaxiao_freight'):
                            rows = parse_huaxiao(raw, freight=name == 'huaxiao_freight')
                        else:
                            evidence.update(error_category='source_discovery_required',
                                            error='HTTP access alone does not verify series, reuse terms, fields or update times')
                    except Exception as exc:
                        evidence.update(error_category=error_category(exc), error=str(exc))
                evidence['requested_dates'] = [day] if day else days
                results.append(persist(db, raw_dir, name, day, evidence, rows))
    finally:
        db.close()
    return {'attempts': results, 'inserted_count': sum(r['inserted_count'] for r in results),
            'errors': [r for r in results if r['status'] in ('failed', 'unavailable')], 'real_forecast_enabled': False}


def series_key(row):
    key = tuple(row[k] for k in ('source', 'metric', 'region', 'contract', 'unit', 'observed_start', 'observed_end'))
    detail = json.loads(row['detail']) if isinstance(row['detail'], str) else row['detail']
    return key + tuple(detail.get(k) for k in ('coal_type', 'calorific_value', 'tax_basis', 'price_basis'))


def visible_observations(db, as_of):
    cutoff = timestamp(as_of)
    rows = [dict(r) for r in db.execute("""SELECT o.*,v.first_seen,v.processed_at,
        v.available_at,v.availability_basis FROM observation o
        JOIN observation_availability v ON v.observation_id=o.id""")
        if r['available_at'] and timestamp(r['available_at']) <= cutoff]
    return sorted(rows, key=lambda r: (timestamp(r['available_at']), r['id']))


def latest_observations(db, as_of):
    latest = {}
    for row in visible_observations(db, as_of):
        latest[series_key(row)] = row
    return list(latest.values())


def available_rows(db, as_of):
    cutoff = timestamp(as_of)
    rows = []
    # Choose the visible revision BEFORE quality filtering: a missing/quarantined
    # correction must not resurrect an obsolete good value.
    for row in latest_observations(db, as_of):
        official_first_seen = (row['source'] in ('czce', 'akshare_czce')
                               and row['quality'] == 'unknown_publication_time'
                               and row['metric'] in ('close', 'settle', 'volume', 'open_interest')
                               and row['contract'] == 'MA2701')
        if row['value'] is None or (row['quality'] != 'verified' and not official_first_seen):
            continue
        reference = row['published_at'] or row['first_seen']
        if (reference and row['observed_start'] and row['observed_end']
                and date.fromisoformat(row['observed_start']) <= date.fromisoformat(row['observed_end'])
                <= timestamp(reference).astimezone(ZoneInfo('Asia/Shanghai')).date()
                <= cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date()):
            rows.append(row)
    return rows


def cross_check(db, as_of):
    # Two CZCE transports are one source; third-party provenance is not proven independent.
    rows = [r for r in latest_observations(db, as_of) if r['contract'] == 'MA2701'
            and r['metric'] in ('close', 'settle', 'volume', 'open_interest')]
    grouped = {}
    for row in rows:
        grouped.setdefault((row['observed_end'], row['metric']), {})[row['source']] = dict(row)
    comparisons = []
    for (day, metric), group in sorted(grouped.items()):
        official = group.get('czce') or group.get('akshare_czce')
        third = group.get('sina')
        if official and third:
            a, b = official['value'], third['value']
            comparisons.append(dict(date=day, metric=metric, official=a, sina=b,
                                    difference=b-a if a is not None and b is not None else None,
                                    independent=False, status='lineage_and_contract_identity_unverified'))
    return {'independent_verification': False, 'comparisons': comparisons,
            'note': 'AKShare/CZCE share origin; Sina is only a candidate second source, independence not established.'}


def status(db_path, as_of=None):
    as_of = as_of or utcnow()
    cutoff = timestamp(as_of)
    db = connect(db_path)
    try:
        sources = []
        visible = visible_observations(db, as_of)
        for source in db.execute('SELECT * FROM source ORDER BY id'):
            item = dict(source)
            attempts = [dict(r) for r in db.execute('SELECT * FROM ingest_attempt WHERE source=? ORDER BY id', (item['id'],))
                        if timestamp(r['ingested_at']) <= cutoff]
            item['latest_attempt'] = attempts[-1] if attempts else None
            observations = [r for r in visible if r['source'] == item['id']]
            item['observation_count'] = len(observations)
            item['latest_observed_end'] = max((r['observed_end'] for r in observations if r['observed_end']), default=None)
            item['latest_published_at'] = max((r['published_at'] for r in observations if r['published_at']), default=None)
            item['age_observation_days'] = ((cutoff.date()-date.fromisoformat(item['latest_observed_end'])).days
                                            if item['latest_observed_end'] else None)
            item['availability'] = ('restricted' if attempts and attempts[-1]['error_category'] in ('proxy_connect_403', 'proxy_domain_forbidden', 'authentication_required', 'http_403')
                                    else attempts[-1]['status'] if attempts else 'unavailable')
            item['transport_ok'] = bool(attempts and attempts[-1]['http_status'] == 200)
            item['parsed_nonnull_versions'] = sum(r['value'] is not None for r in observations)
            item['staleness'] = ('unknown' if item['age_observation_days'] is None else
                                 'stale' if item['age_observation_days'] > (7 if item['id'] in ('quhe','cctd','cctd_scpi','mysteel') else 3)
                                 else 'within_display_age_limit')
            sources.append(item)
        counts = {table: db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                  for table in ('observation', 'ingest_attempt', 'forecast', 'forecast_verification')}
        eligible = available_rows(db, as_of)
        event_counts = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='industry_event'").fetchone():
            for row in db.execute('SELECT category,available_at FROM industry_event'):
                if timestamp(row['available_at']) <= cutoff:
                    event_counts[row['category']] = event_counts.get(row['category'], 0) + 1
        return {'as_of': as_of, 'sources': sources, 'counts_all_time': counts,
                'industry_event_versions_visible': event_counts,
                'eligible_observations': len(eligible), 'cross_check': cross_check(db, as_of),
                'research_eligible_observations': len(eligible),
                'availability_policy': 'known publication AND local first_seen; official publication-unknown rows use response completion; revisions wait for reprocessed_at',
                'minimum_forecast_conditions_met': False, 'real_forecast_enabled': False,
                'gaps': ['Independently validated dated MA2701 history with publication times',
                         'Long producing-region coal series with coal/heat/tax specifications',
                         'MTO, inventory and arrivals history with per-field observation and publication times',
                         'Frozen target/horizon, sufficient aligned training and calibration samples, prospective validation'],
                'note': 'Two price dates and one weekly report cannot train or calibrate a price model. No numeric forecast.'}
    finally:
        db.close()
