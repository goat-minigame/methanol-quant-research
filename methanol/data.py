"""Fetch public Sina futures daily bars, with strict validation and provenance."""
import csv
import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

FIELDS = ['date', 'open', 'high', 'low', 'close', 'volume', 'hold', 'settle']
SOURCE = 'https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_MA0=/InnerFuturesNewService.getDailyKLine'


def validate_bars(rows):
    if not rows:
        raise ValueError('行情为空，不能回测')
    clean, dates = [], set()
    for row in rows:
        stamp = date.fromisoformat(str(row['date'])).isoformat()
        if stamp in dates:
            raise ValueError(f'重复交易日: {stamp}')
        dates.add(stamp)
        bar = {'date': stamp}
        for key in FIELDS[1:]:
            value = row.get(key)
            if value is None or value == '':
                if key in ('open', 'high', 'low', 'close'):
                    raise ValueError(f'{stamp}: 缺少 {key}')
                bar[key] = None
                continue
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f'{stamp}: 非法 {key}')
            if key in ('open', 'high', 'low', 'close') and value == 0:
                raise ValueError(f'{stamp}: {key} 必须大于零')
            bar[key] = value
        if not (bar['low'] <= min(bar['open'], bar['close']) <= max(bar['open'], bar['close']) <= bar['high']):
            raise ValueError(f'{stamp}: OHLC 价格关系不成立')
        clean.append(bar)
    return sorted(clean, key=lambda row: row['date'])


def parse_response(raw):
    text = raw.decode('utf-8-sig').strip()
    # Sina prepends a JavaScript block comment; treat it as inert text.
    text = re.sub(r'\A(?:\s*/\*.*?\*/\s*)+', '', text, flags=re.S)
    if text.startswith('['):
        payload = text
    else:
        match = re.fullmatch(r'[^\r\n]*?=\s*\((.*)\)\s*;?\s*', text, re.S)
        if not match:
            raise ValueError('行情响应不是预期的 JSON / JSONP，可能接口已变更')
        payload = match.group(1)
    rows = json.loads(payload)
    if not isinstance(rows, list):
        raise ValueError('行情响应必须是列表')
    mapped = []
    aliases = dict(zip(FIELDS, ['d', 'o', 'h', 'l', 'c', 'v', 'p', 's']))
    for row in rows:
        if isinstance(row, dict):
            mapped.append({key: row.get(key, row.get(alias)) for key, alias in aliases.items()})
        elif isinstance(row, list) and len(row) == len(FIELDS):
            mapped.append(dict(zip(FIELDS, row)))
        else:
            raise ValueError('无法识别行情字段结构')
    return validate_bars(mapped)


def read_csv(path):
    return inspect_dataset(path)[0]


def fetch_data(output, symbol='MA0', timeout=30):
    if not re.fullmatch(r'MA(?:0|[0-9]{3,4})', symbol):
        raise ValueError('品种仅支持 MA0 或 MA 加三/四位合约月份编码')
    url = SOURCE + '?' + urlencode({'symbol': symbol})
    req = Request(url, headers={'User-Agent': 'methanol-quant-research/0.1'})
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError('公开行情下载失败。检查 stock2.finance.sina.com.cn 的网络许可与接口状态；未生成替代数据。原因: ' + str(exc)) from exc
    bars = parse_response(raw)
    output = Path(output)
    # Validate before writing; retain original response and source metadata for audit.
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(bars)
    output.with_suffix('.raw.txt').write_bytes(raw)
    metadata = {
        'source': '新浪财经公开期货日线接口（第三方行情）', 'url': url,
        'symbol': symbol, 'fetched_at_utc': datetime.now(timezone.utc).isoformat(),
        'first_date': bars[0]['date'], 'last_date': bars[-1]['date'], 'rows': len(bars),
        'raw_sha256': hashlib.sha256(raw).hexdigest(),
        'csv_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
        'notes': 'MA0 为主力连续序列；换月拼接、复权口径与交易所结算价需另行核验。',
    }
    output.with_suffix('.metadata.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    return metadata


def inspect_dataset(path):
    """Verify local integrity, not the truth or independence of the market source."""
    path = Path(path)
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    provenance = path.with_suffix('.metadata.json')
    evidence = {'csv_sha256': digest, 'status': 'unverified', 'metadata': None}
    if provenance.exists():
        metadata = json.loads(provenance.read_text(encoding='utf-8'))
        if not isinstance(metadata, dict) or metadata.get('csv_sha256') != digest:
            raise ValueError('CSV 与来源记录哈希不一致；请核验或重新下载，不可使用旧来源记录')
        raw_path = path.with_suffix('.raw.txt')
        if not raw_path.exists():
            raise ValueError('来源记录存在但原始响应缺失，无法完成完整性校验')
        raw = raw_path.read_bytes()
        if metadata.get('raw_sha256') != hashlib.sha256(raw).hexdigest():
            raise ValueError('原始行情响应哈希不一致')
        evidence.update(status='verified_local_hashes', metadata=metadata)
    # Parse the exact bytes whose hash was computed.
    import io
    rows = validate_bars(list(csv.DictReader(io.StringIO(content.decode('utf-8-sig')))))
    if evidence['metadata'] is not None:
        meta = evidence['metadata']
        if (meta.get('rows'), meta.get('first_date'), meta.get('last_date')) != (len(rows), rows[0]['date'], rows[-1]['date']):
            raise ValueError('来源记录中的行数或日期范围与 CSV 不一致')
    evidence['quality'] = quality_check(rows)
    return rows, evidence


def quality_check(rows):
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    last = date.fromisoformat(rows[-1]['date'])
    if last > today:
        raise ValueError('行情包含未来日期，请核验数据')
    gaps = [{'date': b['date'], 'previous_date': a['date'], 'gap_return': b['open']/a['close']-1}
            for a, b in zip(rows, rows[1:]) if abs(b['open']/a['close']-1) >= .10]
    return {'rows': len(rows), 'first_date': rows[0]['date'], 'last_date': rows[-1]['date'],
            'age_calendar_days': (today-last).days, 'large_open_gaps': gaps,
            'zero_volume_days': sum(b['volume'] == 0 for b in rows),
            'missing_volume_days': sum(b['volume'] is None for b in rows),
            'calendar_note': '距最后行情的自然日数不是缺失交易日数；未接入交易所日历。',
            'verification_note': '哈希仅校验本地文件一致性，不证明行情正确；大跳空原因尚未核验。'}
