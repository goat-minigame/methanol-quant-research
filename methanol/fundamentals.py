"""MA macro/basis entry points and explicit availability-time local data audit."""
import csv
import hashlib
import math
from datetime import date,datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from .markets import akshare,clean_records,save_snapshot

CATALOG={
    'pmi':{'function':'macro_china_pmi_yearly','source':'Jin10 via AKShare','meaning':'制造业景气需求背景'},
    'ppi':{'function':'macro_china_ppi_yearly','source':'Jin10 via AKShare','meaning':'工业品价格环境'},
    'industrial':{'function':'macro_china_industrial_production_yoy','source':'Jin10 via AKShare','meaning':'工业需求背景'},
    'basis':{'function':'futures_spot_price','source':'100ppi via AKShare','meaning':'甲醇现货与具体合约基差；报价地域、品级和时间需核验'},
    'warehouse':{'function':'futures_warehouse_receipt_czce','source':'CZCE via AKShare','meaning':'甲醇交易所仓单；不等于港口或社会库存'},
}


def catalog():
    return {'sources':CATALOG,'additional_metrics':['港口库存','开工率','进口量','煤炭成本','MTO开工率'],
            'note':'additional_metrics为待补充数据，不是已经接入的数据源；在线宏观可能修订且没有历史版本，默认只展示不进入回测。'}


def fetch_fundamental(kind, day=None, output=None):
    if kind not in CATALOG:raise ValueError('未知基本面来源')
    spec=CATALOG[kind]
    if kind in ('basis','warehouse'):
        if day is None:raise ValueError('现货/仓单查询须明确--date YYYY-MM-DD，不默认猜测交易日')
        day=date.fromisoformat(day).strftime('%Y%m%d')
    ak=akshare()
    fn=getattr(ak,spec['function'],None)
    if fn is None:raise RuntimeError('已安装AKShare缺少所需数据函数')
    if kind=='basis':frame=fn(date=day,vars_list=['MA'])
    elif kind=='warehouse':
        response=fn(date=day)
        if not isinstance(response,dict):raise ValueError('仓单接口结构变化')
        key=next((k for k in response if str(k).strip() in ('MA','甲醇')),None)
        if key is None:raise ValueError('仓单结果不含甲醇，不能以其他品种替代')
        frame=response[key]
    else:frame=fn()
    rows=clean_records(frame)
    return save_snapshot({'kind':kind,'source':spec['source'],'function':spec['function'],'requested_date':day,
                          'rows':rows,'row_count':len(rows),'akshare_version':getattr(ak,'__version__','unknown'),
                          'availability_status':'unverified_for_historical_training',
                          'note':'公开数据展示快照；原表日期不自动认定为可用时间。宏观修订、公布时点、现货单位和期货合约须核验后才能用于历史模型。'},output)


def timestamp(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:raise ValueError('available_at/as-of必须含时区，例如+08:00')
    return result


def inspect_releases(input_path, as_of, output=None):
    cutoff=timestamp(as_of)
    path=Path(input_path);raw=path.read_bytes()
    import io
    source=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    if not source:raise ValueError('基本面CSV为空')
    required={'metric','observation_date','available_at','value','unit','source'}
    rows=[];seen=set()
    for row in source:
        if not required.issubset(row) or any(not row[k].strip() for k in required):raise ValueError('基本面CSV缺少必填字段或出处')
        day=date.fromisoformat(row['observation_date'])
        published=timestamp(row['available_at'])
        if published.astimezone(ZoneInfo('Asia/Shanghai')).date()<day:raise ValueError('已实现基本面数据不能早于观测日期发布')
        value=float(row['value'])
        if not math.isfinite(value):raise ValueError('基本面数值必须有限')
        key=(row['metric'],day,published)
        if key in seen:raise ValueError('基本面CSV存在重复观测/发布记录')
        seen.add(key)
        rows.append((row,day,published,value))
    available=[r for r in rows if r[2]<=cutoff]
    latest={}
    for row,day,published,value in sorted(available,key=lambda r:(r[1],r[2])):
        latest[row['metric']]={'observation_date':day.isoformat(),'available_at':published.isoformat(),
                               'value':value,'unit':row['unit'],'source':row['source'],
                               'age_observation_days':(cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date()-day).days}
    return save_snapshot({'input_file':str(path),'csv_sha256':hashlib.sha256(raw).hexdigest(),
                          'as_of':cutoff.isoformat(),'total_records':len(rows),'available_records':len(available),
                          'excluded_future_releases':len(rows)-len(available),'latest_available':latest,
                          'note':'按明确发布时刻筛选；同一观测日修订仅在其发布时间后可见。手工时间戳与数据出处仍需外部核验，结果没有自动作为交易因子。'},output)
