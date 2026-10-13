"""Seven fixed long/flat strategies with previous-close execution timing."""
import csv
import html
import json
from datetime import date,timedelta
from pathlib import Path
from .indicators import calculate
from .backtest import backtest
from .data import inspect_dataset
from .report import write_report

NAMES=('buy_hold','ma_cross','macd','rsi','boll','kdj','ensemble')


def positions(rows):
    data=calculate(rows)
    states={'rsi':0,'boll':0}
    output={name:{} for name in NAMES}
    for i,bar in enumerate(data):
        # Decisions are available after the PREVIOUS bar close.
        previous=data[i-1] if i else None
        signals={name:0 for name in NAMES}
        signals['buy_hold']=1
        if previous:
            p=previous
            signals['ma_cross']=int(p['sma30'] is not None and p['sma10']>p['sma30'])
            signals['macd']=int(p['macd_dea'] is not None and p['macd_dif']>p['macd_dea'])
            signals['kdj']=int(p['k9'] is not None and p['k9']>p['d9'])
            if p['rsi14'] is not None:
                if p['rsi14']<30:states['rsi']=1
                elif p['rsi14']>70:states['rsi']=0
            if p['boll_mid20'] is not None:
                if p['close']<p['boll_lower20']:states['boll']=1
                elif p['close']>=p['boll_mid20']:states['boll']=0
            signals.update(states)
            signals['ensemble']=int(sum(signals[n] for n in ('ma_cross','macd','rsi','boll','kdj'))>=3)
        for name in NAMES:output[name][bar['date']]=signals[name]
    return output


def compare(input_path, output='reports/strategies', split_date='2022-01-01', cost_bps=5):
    split=date.fromisoformat(split_date)
    bars,evidence=inspect_dataset(input_path)
    before=[b for b in bars if b['date']<split.isoformat()]
    if len(before)<60:raise ValueError('训练分界前至少需要60根日线，保证统一指标暖机')
    signals=positions(bars)
    results={};summaries={}
    for name in NAMES:
        training=backtest(bars,cost_bps=cost_bps,end=(split-timedelta(days=1)).isoformat(),signals=signals[name],strategy=name)
        testing=backtest(bars,cost_bps=cost_bps,start=split.isoformat(),signals=signals[name],strategy=name)
        results[name]={'train':training,'test':testing}
        summaries[name]={'train':training['summary'],'test':testing['summary']}
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    for name,result in results.items():
        write_report(result['test'],output/name,input_path)
    summary={'dataset':evidence,'split_date':split.isoformat(),'cost_bps':cost_bps,'strategies':summaries,
             'sparse_test_strategies':[name for name,r in summaries.items() if r['test']['completed_trades']<20],
             'plan':'7个预设做多/空仓策略；同日期与同成本，无参数搜索和自动选优。所有信号只用上一日收盘信息。',
             'state_policy':'RSI/BOLL信号状态由既往历史延续，测试账户重置现金；训练不使用测试收益。',
             'limits':'历史已被查看，多策略比较存在多重试验风险；不能挑测试期最优者宣称预测能力。连续主力不代表真实期货账户。'}
    (output/'comparison.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    with (output/'comparison.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,['strategy','period','first_date','last_date','total_return','max_drawdown','completed_trades','win_rate']);writer.writeheader()
        for name,parts in summaries.items():
            for period,part in parts.items():writer.writerow({'strategy':name,'period':period,**{k:part[k] for k in writer.fieldnames if k not in ('strategy','period')}})
    table=''.join(f"<tr><td><a href='{name}/report.html'>{name}</a></td><td>{r['train']['total_return']:.2%}</td><td>{r['test']['total_return']:.2%}</td><td>{r['test']['max_drawdown']:.2%}</td><td>{r['test']['completed_trades']}</td></tr>" for name,r in summaries.items())
    doc=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>甲醇多策略比较</title><style>body{{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 20px}}td,th{{padding:12px;border-bottom:1px solid #ddd}}table{{width:100%;border-collapse:collapse}}p{{line-height:1.7}}</style><h1>甲醇多策略比较</h1><p>{html.escape(summary['plan'])} 分界：{split.isoformat()}；单边成本：{cost_bps}基点。</p><table><tr><th>策略</th><th>训练收益</th><th>测试收益</th><th>测试回撤</th><th>完整交易</th></tr>{table}</table><p>{html.escape(summary['limits'])}</p><p>测试期不足20次完整交易的策略：{html.escape(', '.join(summary['sparse_test_strategies']))}；交易数少，结果证据有限。点击策略查看详细报告。RSI/BOLL是有状态规则，测试期会使用训练期已知的历史状态，但账户不继承训练期盈亏。</p></html>'''
    (output/'report.html').write_text(doc,encoding='utf-8')
    return summary
