"""Causal daily indicators; documented warmup, no missing-price imputation."""
import csv
import math
from pathlib import Path
from statistics import fmean, pstdev
from .data import inspect_dataset, validate_bars
from .markets import save_snapshot


def ema(values, period):
    result, history, state = [], [], None
    for value in values:
        if value is None:
            result.append(None); continue
        history.append(value)
        if len(history) < period: result.append(None); continue
        state = fmean(history[-period:]) if state is None else state+2/(period+1)*(value-state)
        result.append(state)
    return result


def calculate(rows):
    bars = validate_bars(rows)
    closes = [r['close'] for r in bars]
    e12, e26 = ema(closes,12), ema(closes,26)
    dif = [a-b if a is not None and b is not None else None for a,b in zip(e12,e26)]
    dea = ema(dif,9)
    output, tr, gains, losses = [], [], [], []
    avg_gain=avg_loss=atr=None
    k=d=50.0
    obv=0.0
    for i, bar in enumerate(bars):
        close=bar['close']
        change=close-closes[i-1] if i else 0
        tr.append(max(bar['high']-bar['low'],abs(bar['high']-closes[i-1]),abs(bar['low']-closes[i-1])) if i else bar['high']-bar['low'])
        if i: gains.append(max(change,0));losses.append(max(-change,0))
        if i == 14: avg_gain=fmean(gains[:14]);avg_loss=fmean(losses[:14])
        elif i > 14: avg_gain=(avg_gain*13+gains[-1])/14;avg_loss=(avg_loss*13+losses[-1])/14
        rsi=None if avg_gain is None else (50 if avg_gain+avg_loss==0 else (100 if avg_loss==0 else 100-100/(1+avg_gain/avg_loss)))
        if i == 13: atr=fmean(tr[:14])
        elif i > 13: atr=(atr*13+tr[-1])/14
        if bar['volume'] is None: obv=None
        elif i and obv is not None: obv += (1 if change>0 else -1 if change<0 else 0)*bar['volume']
        row={'date':bar['date'],'close':close}
        for n in (5,10,20,30,60):row[f'sma{n}']=fmean(closes[i-n+1:i+1]) if i+1>=n else None
        row.update(ema12=e12[i],ema26=e26[i],macd_dif=dif[i],macd_dea=dea[i],
                   macd_hist=2*(dif[i]-dea[i]) if dea[i] is not None else None,rsi14=rsi,atr14=atr,obv=obv)
        if i>=8:
            recent=bars[i-8:i+1]; high=max(b['high'] for b in recent);low=min(b['low'] for b in recent)
            rsv=100*(close-low)/(high-low) if high>low else 50
            k=2*k/3+rsv/3;d=2*d/3+k/3
            row.update(k9=k,d9=d,j9=3*k-2*d)
        else: row.update(k9=None,d9=None,j9=None)
        if i>=19:
            sample=closes[i-19:i+1];mid=fmean(sample);std=pstdev(sample)
            typical=[(b['high']+b['low']+b['close'])/3 for b in bars[i-19:i+1]]
            mean=fmean(typical);deviation=fmean(abs(x-mean) for x in typical)
            volume=[b['volume'] for b in bars[i-19:i+1]]
            vma=fmean(volume) if all(v is not None for v in volume) else None
            row.update(boll_mid20=mid,boll_upper20=mid+2*std,boll_lower20=mid-2*std,
                       boll_width20=4*std/mid,cci20=(typical[-1]-mean)/(.015*deviation) if deviation else 0,
                       donchian_high20=max(b['high'] for b in bars[i-19:i+1]),
                       donchian_low20=min(b['low'] for b in bars[i-19:i+1]),
                       volume_ma20=vma,volume_ratio20=bar['volume']/vma if vma else None)
        else:
            for name in ('boll_mid20','boll_upper20','boll_lower20','boll_width20','cci20','donchian_high20','donchian_low20','volume_ma20','volume_ratio20'):row[name]=None
        row['roc10']=close/closes[i-10]-1 if i>=10 else None
        row['momentum10']=close-closes[i-10] if i>=10 else None
        if i>=13:
            high=max(b['high'] for b in bars[i-13:i+1]);low=min(b['low'] for b in bars[i-13:i+1])
            row['williams_r14']=-100*(high-close)/(high-low) if high>low else -50
        else:row['williams_r14']=None
        row['realized_volatility20']=pstdev([closes[j]/closes[j-1]-1 for j in range(i-19,i+1)])*math.sqrt(252) if i>=20 else None
        output.append(row)
    return output


def indicator_report(input_path, output='reports/indicators.json', csv_output='reports/indicators.csv'):
    bars,evidence=inspect_dataset(input_path)
    rows=calculate(bars)
    if csv_output:
        path=Path(csv_output);path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,list(rows[0]));writer.writeheader();writer.writerows(rows)
    return save_snapshot({'dataset':evidence,'latest':rows[-1],'indicator_count':len(rows[-1])-2,
                          'definitions':'收盘后计算；EMA以首个完整窗口均值初始化；MACD柱=2*(DIF-DEA)；RSI/ATR用Wilder平滑；BOLL用总体标准差；缺失窗口为null；指标不构成交易建议。'},output)
