"""Bounded official adapters; shared storage without changing MA source definitions."""
import csv
import io
import json
import math
import os
import re
import time
from datetime import date, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from methanol import sources as base

INE = 'https://www.ine.com.cn/data/tradedata/future/dailydata/kx{day}.dat'
RULES = 'https://www.ine.com.cn/products/sc/standard/'
WPSR = 'https://ir.eia.gov/wpsr/table4.csv'
# EIA v2 supports legacy series identifiers through the documented seriesid route.
SERIES = {
    'crude_inventory': ('WCESTUS1', 'Thousand Barrels'),
    'cushing_inventory': ('W_EPC0_SAX_YCUOK_MBBL', 'Thousand Barrels'),
    'crude_production': ('WCRFPUS2', 'Thousand Barrels per Day'),
    'refinery_utilization': ('WPULEUS3', 'Percent'),
    'gasoline_inventory': ('WGTSTUS1', 'Thousand Barrels'),
    'distillate_inventory': ('WDISTUS1', 'Thousand Barrels'),
    'gasoline_product_supplied': ('WGFUPUS2', 'Thousand Barrels per Day'),
    'distillate_product_supplied': ('WDIUPUS2', 'Thousand Barrels per Day'),
}
CATALOG = {
    'ine_sc': ('official_exchange', 'INE', INE, 'dated_futures', 'Asia/Shanghai', 3),
    'eia_public_stocks': ('official_statistics', 'EIA', WPSR, 'weekly_stocks', 'America/New_York', 14),
    'eia_public_flows': ('official_statistics', 'EIA', 'https://ir.eia.gov/wpsr/table1.csv', 'weekly_supply_demand_proxy', 'America/New_York', 14),
    'eia_public_refinery': ('official_statistics', 'EIA', 'https://ir.eia.gov/wpsr/table2.csv', 'weekly_refinery_utilization', 'America/New_York', 14),
    'brent_futures': ('unverified', 'ICE', 'https://www.ice.com/products/219/Brent-Crude-Futures', 'futures_unavailable', 'Europe/London', 3),
    'wti_futures': ('unverified', 'CME', 'https://www.cmegroup.com/markets/energy/crude-oil/light-sweet-crude.quotes.html', 'futures_unavailable', 'America/Chicago', 3),
}
for metric, (code, _) in SERIES.items():
    CATALOG['eia_' + metric] = ('official_statistics', 'EIA',
        'https://api.eia.gov/v2/seriesid/PET.' + code + '.W/', 'weekly_statistics', 'America/New_York', 14)


def connect(path):
    db = base.connect(path)
    with db:
        for key, (confidence, lineage, url, *_rest) in CATALOG.items():
            db.execute('INSERT OR IGNORE INTO source(id,confidence,lineage,url) VALUES(?,?,?,?)', (key, confidence, lineage, url))
    return db


def numeric(value):
    if value is None or value in ('', '.', '--', 'NA', 'N/A'):
        return None
    if isinstance(value, bool):
        raise ValueError('boolean is not a numeric observation')
    result = float(str(value).replace(',', ''))
    if not math.isfinite(result) or result < 0:
        raise ValueError('nonfinite or negative observation')
    return result


def parse_sc(raw, day):
    day = date.fromisoformat(day).isoformat()
    doc = json.loads(raw)
    if doc.get('report_date') != day.replace('-', '') or doc.get('o_code') != '0000':
        raise ValueError('missing/mismatched INE trading date or response code')
    rows, seen = [], set()
    for item in doc['o_curinstrument']:
        if item.get('PRODUCTID') != 'sc_f':
            continue
        month = str(item['DELIVERYMONTH']).strip()
        if month == '小计':
            continue
        if not re.fullmatch(r'\d{4}', month) or not 1 <= int(month[2:]) <= 12:
            raise ValueError('not a dated SC contract')
        delivery = (2000 + int(month[:2])) * 12 + int(month[2:])
        trade = date.fromisoformat(day)
        if not 0 <= delivery - (trade.year * 12 + trade.month) <= 36:
            raise ValueError('SC delivery outside bounded listing window')
        contract = 'SC' + month
        if contract in seen:
            raise ValueError('duplicate SC contract')
        seen.add(contract)
        for metric, field in [('close', 'CLOSEPRICE'), ('settle', 'SETTLEMENTPRICE'),
                              ('volume', 'VOLUME'), ('open_interest', 'OPENINTEREST')]:
            value = numeric(item[field])
            if value is not None and metric in ('volume', 'open_interest') and not value.is_integer():
                raise ValueError('fractional lot count')
            if value == 0 and metric in ('close', 'settle'):
                value = None  # zero/blank is not silently turned into a tradable price
            rows.append(base.record(metric, value, 'CNY/barrel' if metric in ('close', 'settle') else 'lot',
                day, day, contract, detail={'instrument_type': 'dated_futures', 'currency': 'CNY',
                'timezone': 'Asia/Shanghai', 'raw_contract': month, 'source_update_text': doc.get('update_date'),
                'update_is_not_verified_publication': True, 'contract_rules_url': RULES,
                'rules_status': 'current_official_rule_page_waf_unverified',
                'zero_volume': numeric(item['VOLUME']) == 0, 'parser_version': 'ine-sc-v1'},
                flags=['current_contract_rules_unverified']))
    if not rows:
        raise ValueError('SC absent; no proof of holiday')
    return rows


def parse_public_stocks(raw):
    table = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
    if not table or table[0][0] != 'STUB_1' or len(table[0]) != 8 or table[0][3] != 'Difference':
        raise ValueError('WPSR table4 schema changed')
    days = [datetime.strptime(table[0][i], '%m/%d/%y').date() for i in (1, 2)]
    if days[0] - days[1] != timedelta(days=7) or any(d.weekday() != 4 for d in days):
        raise ValueError('WPSR weekly observation dates inconsistent')
    labels = {'Commercial (Excluding SPR)': 'crude_inventory', 'Cushing': 'cushing_inventory',
              'Total Motor Gasoline': 'gasoline_inventory', 'Distillate Fuel Oil': 'distillate_inventory'}
    rows, seen = [], set()
    for item in table[1:]:
        if not item or item[0] not in labels:
            continue
        if item[0] in seen or len(item) != 8:
            raise ValueError('duplicate/invalid stock row')
        seen.add(item[0])
        for index, day in enumerate(days, 1):
            rows.append(base.record(labels[item[0]], numeric(item[index]), 'million_barrels',
                day.isoformat(), day.isoformat(), region='Cushing, Oklahoma' if item[0] == 'Cushing' else 'US',
                detail={'observation_kind': 'week_ending_stock_snapshot', 'timezone': 'America/New_York',
                'source_label': item[0], 'unit_reference': 'https://ir.eia.gov/wpsr/psw04.xls',
                'not_eia_v2': True, 'independent_of_eia_v2': False, 'parser_version': 'wpsr-table4-v1'}))
    if seen != set(labels):
        raise ValueError('missing required stock categories')
    return rows


def parse_eia(raw, metric):
    doc = json.loads(raw)
    response = doc['response']
    if doc.get('error') or response.get('frequency') != 'weekly' or not response.get('data'):
        raise ValueError('EIA weekly data missing or wrong frequency')
    code, unit = SERIES[metric]
    rows, seen = [], set()
    for item in response['data']:
        day = date.fromisoformat(item['period'])
        if day.weekday() != 4 or day in seen or item.get('series') != code or item.get('units') != unit:
            raise ValueError('EIA series/date/unit mismatch or duplicate')
        seen.add(day)
        value = numeric(item['value'])
        if metric == 'refinery_utilization' and value is not None and value > 100:
            raise ValueError('invalid utilization percentage')
        stock = metric.endswith('inventory')
        start = day if stock else day - timedelta(days=6)
        rows.append(base.record(metric, value, unit, start.isoformat(), day.isoformat(),
            region='Cushing, Oklahoma' if metric == 'cushing_inventory' else 'US',
            detail={'series_id': 'PET.' + code + '.W', 'timezone': 'America/New_York',
            'observation_kind': 'week_ending_stock_snapshot' if stock else 'weekly_average',
            'product_supplied_is_demand_proxy': 'product_supplied' in metric,
            'parser_version': 'eia-seriesid-v1', 'authenticated_transport': True},
            flags=['live_authenticated_schema_unverified']))
    return rows


def acquire_eia(url, raw_dir, timeout):
    """Read ONLY EIA_API_KEY. Never persist its value, request echo or exception URL."""
    base.validate_timeout(timeout)
    key = os.environ.get('EIA_API_KEY')
    now = base.utcnow()
    evidence = dict(started_at=now, url=url, method='GET')
    evidence['request_hash'] = base.digest(base.canonical({'url': url, 'method': 'GET'}))
    if not key:
        evidence.update(ingested_at=now, error_category='authentication_required', error='EIA_API_KEY is not configured')
        return None, evidence
    request_url = url + '?' + urlencode({'api_key': key, 'length': 8, 'sort[0][column]': 'period', 'sort[0][direction]': 'desc'})
    raw = None
    try:
        with urlopen(Request(request_url, headers={'User-Agent': 'crude-oil-audit/1.0'}), timeout=timeout) as response:
            raw = response.read()
            evidence['http_status'] = response.status
    except HTTPError as exc:
        raw = exc.read()
        evidence.update(http_status=exc.code, error_category=base.response_error_category(exc.code, exc.headers, raw), error='EIA HTTP request failed')
    except Exception as exc:
        evidence.update(error_category=base.error_category(exc), error='EIA transport failed; exception text suppressed')
    evidence['ingested_at'] = base.utcnow()
    if raw is not None:
        # The original byte hash is retained, but EIA echoes api_key in its JSON.
        evidence['original_raw_hash'] = base.digest(raw)
        try:
            error = json.loads(raw).get('error')
            if isinstance(error, dict) and error.get('code') in ('API_KEY_MISSING', 'API_KEY_INVALID', 'API_KEY_DISABLED', 'API_KEY_UNVERIFIED'):
                evidence.update(error_category='authentication_required', error='EIA API key authentication required')
        except (ValueError, AttributeError):
            pass
        try:
            doc = json.loads(raw)
            doc.pop('request', None)
            safe = base.canonical(doc)
        except (ValueError, AttributeError):
            safe = b'{"error":"non_json_response_body_withheld"}'
        safe = safe.replace(key.encode(), b'[REDACTED]').replace(urlencode({'api_key': key}).split('=', 1)[1].encode(), b'[REDACTED]')
        evidence['raw_hash'], evidence['raw_path'] = base.archive(raw_dir, safe, '.response')
        evidence['raw_redacted'] = True
        raw = safe
        if not evidence.get('error_category'):
            evidence['response_completed_at'] = evidence['ingested_at']
    return raw, evidence


def ingest(db_path, raw_dir, days, names=None, timeout=20, retries=1):
    base.validate_timeout(timeout)
    if not isinstance(retries, int) or isinstance(retries, bool) or not 0 <= retries <= 3:
        raise ValueError('retries must be an integer between zero and three')
    days = sorted(set(date.fromisoformat(x).isoformat() for x in days))
    names = list(CATALOG) if names is None else list(names)
    if not names or any(n not in CATALOG for n in names) or ('ine_sc' in names and not days):
        raise ValueError('known sources and explicit SC dates required')
    db = connect(db_path)
    attempts = []
    try:
        for name in names:
            for day in days if name == 'ine_sc' else [None]:
                url = CATALOG[name][2].format(day=day.replace('-', '') if day else '')
                for retry in range(retries + 1):
                    rows = []
                    if name in ('brent_futures', 'wti_futures'):
                        now = base.utcnow()
                        raw, ev = None, dict(started_at=now, ingested_at=now, url=url,
                            error_category='unavailable', error='No verified licensed free futures daily API; public quote page is not an API')
                    elif name.startswith('eia_') and not name.startswith('eia_public_'):
                        raw, ev = acquire_eia(url, raw_dir, timeout)
                    else:
                        raw, ev = base.acquire(url, raw_dir, timeout)
                    if not ev.get('error_category'):
                        try:
                            if name == 'ine_sc':
                                rows = parse_sc(raw, day)
                            elif name == 'eia_public_stocks':
                                rows = parse_public_stocks(raw)
                            elif name in ('eia_public_flows', 'eia_public_refinery'):
                                rows = parse_public_flows(raw, refinery=name == 'eia_public_refinery')
                            else:
                                rows = parse_eia(raw, name[4:])
                            local_day = base.timestamp(ev['ingested_at']).astimezone(ZoneInfo(CATALOG[name][4])).date()
                            if any(date.fromisoformat(r['observed_end']) > local_day for r in rows):
                                raise ValueError('future observation date')
                        except (ValueError, KeyError, TypeError, IndexError, AttributeError, UnicodeError):
                            rows = []
                            ev.update(error_category='parse_error', error='Schema/date/value validation failed; inspect private response')
                    ev['retry_index'] = retry
                    attempts.append(base.persist(db, raw_dir, name, day, ev, rows))
                    if ev.get('error_category') not in ('timeout', 'network_error', 'http_429', 'http_500', 'http_502', 'http_503', 'http_504'):
                        break
                    if retry < retries:
                        time.sleep(0.25 * (2 ** retry))
    finally:
        db.close()
    return {'attempts': attempts, 'inserted_count': sum(a['inserted_count'] for a in attempts),
            'errors': [a for a in attempts if a['error_category']], 'forecast_enabled': False}


def parse_public_flows(raw, refinery=False):
    """Select weekly columns, never similarly dated four-week-average columns."""
    table = list(csv.reader(io.StringIO(raw.decode('cp1252'))))
    headers = [i for i, row in enumerate(table) if row[:2] == ['STUB_1', 'STUB_2']]
    if len(headers) != 1:
        raise ValueError('weekly flow header absent or duplicated')
    header = table[headers[0]]
    expected_len = 12 if refinery else 13
    if len(header) != expected_len or header[4] != 'Difference':
        raise ValueError('weekly flow columns changed')
    days = [datetime.strptime(header[i], '%m/%d/%y').date() for i in (2, 3)]
    if days[0]-days[1] != timedelta(days=7) or any(d.weekday() != 4 for d in days):
        raise ValueError('weekly flow dates inconsistent')
    specs = {('Refiner Inputs and Utilization', 'Percent Utilization'): 'refinery_utilization'} if refinery else {
        ('Crude Oil Supply', 'Domestic Production'): 'crude_production',
        ('Products Supplied', 'Finished Motor Gasoline'): 'gasoline_product_supplied',
        ('Products Supplied', 'Distillate Fuel Oil'): 'distillate_product_supplied'}
    rows, seen = [], set()
    for item in table[headers[0]+1:]:
        if len(item) < 2:
            continue
        key = (item[0].strip(), re.sub(r'^\(\d+\)\s*', '', item[1]).strip())
        if key not in specs:
            continue
        if key in seen or len(item) != expected_len:
            raise ValueError('duplicated/malformed flow row')
        seen.add(key)
        for index, day in enumerate(days, 2):
            value = numeric(item[index])
            if refinery and value is not None and value > 100:
                raise ValueError('invalid utilization')
            rows.append(base.record(specs[key], value, 'Percent' if refinery else 'Thousand Barrels per Day',
                (day-timedelta(days=6)).isoformat(), day.isoformat(), region='US',
                detail={'observation_kind': 'weekly_average', 'timezone': 'America/New_York',
                'product_supplied_is_demand_proxy': not refinery and 'Products' in key[0],
                'not_eia_v2': True, 'independent_of_eia_v2': False, 'source_label': list(key),
                'unit_reference': 'https://ir.eia.gov/wpsr/psw02.xls' if refinery else 'https://ir.eia.gov/wpsr/psw01.xls',
                'parser_version': 'wpsr-flow-v1'}))
    if seen != set(specs):
        raise ValueError('required flow category absent')
    return rows
