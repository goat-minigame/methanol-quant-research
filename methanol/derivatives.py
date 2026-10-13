"""Offline volatility and European futures-option research approximations."""
import math
from statistics import stdev
from .data import inspect_dataset
from .markets import save_snapshot


def volatility(input_path, window=20, lookback=120, output=None):
    if window < 2 or lookback < 2: raise ValueError('波动率和排名窗口至少为2')
    rows, evidence = inspect_dataset(input_path)
    returns = [rows[i]['close']/rows[i-1]['close']-1 for i in range(1,len(rows))]
    values, history = [], []
    for i, row in enumerate(rows):
        hv = stdev(returns[i-window:i])*math.sqrt(252) if i >= window else None
        values.append(hv)
        prior = values[-lookback:]
        percentile = None
        if len(prior) == lookback and all(v is not None for v in prior):
            # Match pandas rolling.rank(pct=True): average rank for ties.
            rank = sum(v < hv for v in prior)+(sum(v == hv for v in prior)+1)/2
            percentile = rank/lookback*100
        history.append({'date':row['date'],'annualized_historical_volatility':hv,'percentile':percentile})
    if history[-1]['percentile'] is None: raise ValueError('数据不足以完成波动率排名窗口')
    return save_snapshot({'dataset':evidence,'window':window,'lookback':lookback,
                          'latest':history[-1],'history':history,
                          'note':'改编自上游compute_hv与compute_hv_percentile；简单收益样本标准差，年化252日。仅为收盘后风险描述，未导入其低波做多/高波做空规则。连续主力跳空仍影响HV。'},output)


def black76(futures, strike, days, rate, sigma, kind='call'):
    if kind not in ('call','put'): raise ValueError('期权类型须为call或put')
    if any(not math.isfinite(v) for v in [futures,strike,days,rate,sigma]) or min(futures,strike,days,sigma) <= 0:
        raise ValueError('期货价、行权价、剩余自然日和波动率须为有限正数，利率须为有限值')
    t = days/365
    discount = math.exp(-rate*t)
    std = sigma*math.sqrt(t)
    d1 = (math.log(futures/strike)+std*std/2)/std
    d2 = d1-std
    cdf = lambda x: .5*(1+math.erf(x/math.sqrt(2)))
    pdf = math.exp(-d1*d1/2)/math.sqrt(2*math.pi)
    sign = 1 if kind == 'call' else -1
    price = discount*sign*(futures*cdf(sign*d1)-strike*cdf(sign*d2))
    return {'price':price,'delta_futures':discount*(cdf(d1) if kind == 'call' else cdf(d1)-1),
            'gamma_futures':discount*pdf/(futures*std),
            'vega_per_1pct':discount*futures*pdf*math.sqrt(t)*.01,
            'inputs':{'futures':futures,'strike':strike,'days':days,'rate':rate,'sigma':sigma,'kind':kind},
            'model':'Black-76欧式期货期权近似，按单位标的报价；不是甲醇期权实际公允价。',
            'limits':'未建模美式提前行权、波动率微笑、合约乘数与成交成本；MA期权行权规则需按交易所核验。'}
