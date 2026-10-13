"""Research-only market adapters for MIT a-stock-data-quant, with MA extension."""
import hashlib
import importlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import Request, urlopen
from .vendor.a_stock_data_quant import futures as upstream_futures, options as upstream_options

UPSTREAM = '0944c726f19aa041ecf2d93ac7357223d061823d'


def save_snapshot(result, output):
    result['observed_at_utc'] = datetime.now(timezone.utc).isoformat()
    result['upstream_commit'] = UPSTREAM
    result['usage'] = '行情研究快照；采集时间不等于行情时间，不保证实时，不能作为订单或历史时点特征。'
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write the complete JSON atomically; interrupted requests never overwrite old snapshots.
        temporary = path.with_name(path.name+'.tmp')
        temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False,default=str),encoding='utf-8')
        temporary.replace(path)
    return result


def futures_list():
    return {'contracts': upstream_futures.cmd_list(SimpleNamespace())}


def number(value):
    if value is None or value == '-' or value == '': return None
    try: result = float(value)
    except (TypeError, ValueError): raise ValueError('行情数值字段无法解析')
    if not math.isfinite(result): raise ValueError('行情包含非有限数值')
    return result


def futures_quotes(aliases='MA', output=None, timeout=15):
    quotes, errors, responses = [], [], []
    names = list(dict.fromkeys(alias.strip() for alias in aliases.split(',') if alias.strip()))
    if not names: raise ValueError('至少指定一个期货品种')
    for alias in names:
        entry = upstream_futures.CATALOG.get(alias)
        if entry is None:
            errors.append({'alias':alias,'error':'未知品种；使用 futures list 查看目录'})
            continue
        url = f"https://futsseapi.eastmoney.com/static/{entry['sc']}_{entry['cont']}_qt"
        try:
            with urlopen(Request(url,headers={'User-Agent':upstream_futures.UA}),timeout=timeout) as response:
                raw = response.read()
            payload = json.loads(raw.decode('utf-8'))
            qt = payload.get('qt') if isinstance(payload,dict) else None
            if not isinstance(qt,dict) or not number(qt.get('p')) or number(qt.get('p')) < 0:
                raise ValueError('无有效报价；接口或交易时段可能不可用')
            row = {'alias':alias,'exchange':entry['exchange'],'continuous_code':entry['cont'],
                   'name':qt.get('name'),'code':qt.get('dm'),'price':number(qt.get('p')),
                   'open':number(qt.get('o')),'high':number(qt.get('h')),'low':number(qt.get('l')),
                   'previous_settlement':number(qt.get('zjsj')),
                   'change':number(qt.get('zde')),'percent_change':number(qt.get('zdf')),
                   'volume':number(qt.get('vol')),'open_interest':number(qt.get('ccl')),
                   'source':'eastmoney','url':url,'provider_time':qt.get('time'),
                   'raw_sha256':hashlib.sha256(raw).hexdigest()}
            quotes.append(row)
            responses.append({'alias':alias,'raw_response':payload,'raw_text':raw.decode('utf-8')})
        except (URLError,OSError,ValueError,TypeError) as exc:
            errors.append({'alias':alias,'url':url,'error':str(exc)})
    return save_snapshot({'quotes':quotes,'errors':errors,'responses':responses,
                          'note':'主连快照不等于单合约历史；zjsj为昨结算，非上一收盘。'},output)


def akshare():
    try: return importlib.import_module('akshare')
    except ImportError as exc:
        raise RuntimeError('期权在线数据需要可选依赖：python -m venv .venv 后运行 .venv/bin/python -m pip install -r requirements-options.txt；用 .venv/bin/python 执行命令') from exc


def clean_records(frame):
    if frame is None or getattr(frame,'empty',False): raise ValueError('数据接口返回空数据，不视为验证成功')
    rows = frame.to_dict(orient='records')
    def clean(value):
        if isinstance(value,dict): return {str(k):clean(v) for k,v in value.items()}
        if isinstance(value,list): return [clean(v) for v in value]
        if hasattr(value,'item'): return clean(value.item())
        if isinstance(value,float) and not math.isfinite(value): return None
        if hasattr(value,'isoformat'): return value.isoformat()
        return value
    if not rows: raise ValueError('数据接口没有有效行')
    return clean(rows)


def option_underlyings():
    result = upstream_options.cmd_underlyings(SimpleNamespace())
    result['commodity_options'] = [{'code':'MA','underlying':'甲醇期货','exchange':'CZCE','source':'AKShare/Sina','extension':'本项目新增'}]
    result['note'] = '目录是适配能力说明，不能证明合约当前上市或接口在线可用；部分ETF上游明确不支持。'
    return result


def expiry(value):
    if not re.fullmatch(r'(?:\d{4}|\d{6})',value) or not 1 <= int(value[-2:]) <= 12:
        raise ValueError('ETF/指数期权到期月须为有效YYYYMM或YYMM')
    return value[-4:]


def options_query(action, code, month=None, output=None):
    # Validate requests before importing optional packages or contacting a provider.
    supported = set(upstream_options.ETF_UNDERLYINGS)|set(upstream_options.CFFEX_UNDERLYINGS)|{'MA'}
    if code not in supported: raise ValueError('未知期权标的；使用 options underlyings 查看目录')
    if action not in ('expiries','chain','values'): raise ValueError('未知期权查询动作')
    if action == 'values' and code not in upstream_options.ETF_UNDERLYINGS:
        raise ValueError('values仅为ETF的上游价值分析原表，不是PCR')
    if action == 'chain':
        if code == 'MA':
            if not month or not re.fullmatch(r'[Mm][Aa]\d{3,4}',month):
                raise ValueError('甲醇期权--expiry请填写 expiries MA 返回的原始合约代码，例如MA701；不猜测合约年份')
        else: month = expiry(month or '')
    if code in upstream_options.ETF_UNDERLYINGS and action == 'chain' and upstream_options.ETF_UNDERLYINGS[code][1] is None:
        raise ValueError('该ETF的上游链查询明确不支持')
    ak = akshare()
    if code == 'MA':
        if action == 'expiries':
            rows = clean_records(ak.option_commodity_contract_sina(symbol='甲醇期权'))
            result = {'code':code,'contracts':rows,'source':'akshare/sina','function':'option_commodity_contract_sina'}
        else:
            rows = clean_records(ak.option_commodity_contract_table_sina(symbol='甲醇期权',contract=month))
            normalized, pcr = normalize_ma_chain(rows)
            result = {'code':code,'contract':month,'rows':rows,'normalized_chain':normalized,'open_interest_pcr':pcr,'source':'akshare/sina','function':'option_commodity_contract_table_sina',
                      'note':'PCR仅按完整可用持仓量求和；无成交量PCR。最新价与买卖报价可能陈旧，合约到期与行权规则需核验。'}
    else:
        args = SimpleNamespace(code=code,expiry=month)
        if action == 'expiries': result = upstream_options.cmd_expiries(args)
        elif action == 'chain': result = upstream_options.cmd_chain(args)
        else: result = upstream_options.cmd_pcr(args)
        if result.get('error'): raise RuntimeError(result['error'])
        if not result.get('rows') and not result.get('expiries'):
            raise ValueError('数据接口返回空数据，不视为验证成功')
        if action == 'values': result['note'] = '上游仅提供价值分析原表；未计算成交量/持仓量PCR，不作为甲醇方向因子。'
    result['akshare_version'] = getattr(ak,'__version__','unknown')
    return save_snapshot(result,output)


def normalize_ma_chain(rows):
    normalized, seen = [], set()
    for row in rows:
        strike = number(row.get('行权价'))
        if strike is None or strike <= 0: raise ValueError('甲醇期权链缺少有效行权价')
        for prefix, side, code_key in [('看涨合约-', 'call', '看涨期权合约'), ('看跌合约-', 'put', '看跌期权合约')]:
            contract = row.get(prefix+code_key)
            if not contract: continue
            if not re.fullmatch(r'MA\d{3,4}[CP]\d+(?:\.\d+)?',str(contract),re.I):
                raise ValueError('甲醇期权合约编码无法识别，拒绝将其他标的当作MA')
            if str(contract) in seen: raise ValueError('期权链包含重复合约')
            seen.add(str(contract))
            item = {'contract':str(contract),'side':side,'strike':strike,
                    'bid':number(row.get(prefix+'买价')),'ask':number(row.get(prefix+'卖价')),
                    'last':number(row.get(prefix+'最新价')),'open_interest':number(row.get(prefix+'持仓量'))}
            if any(v is not None and v < 0 for k,v in item.items() if k in ('bid','ask','last','open_interest')):
                raise ValueError('期权报价或持仓量不能为负')
            normalized.append(item)
    if not normalized: raise ValueError('没有可识别的甲醇期权合约')
    call = [r['open_interest'] for r in normalized if r['side']=='call']
    put = [r['open_interest'] for r in normalized if r['side']=='put']
    pcr = sum(put)/sum(call) if call and put and all(v is not None for v in call+put) and sum(call)>0 else None
    return normalized, pcr
