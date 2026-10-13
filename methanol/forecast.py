"""Fixed-design, purged walk-forward direction prediction. No model search."""
import csv
import html
import json
import math
import random
from datetime import date
from pathlib import Path
from statistics import fmean, pstdev
from .data import inspect_dataset, validate_bars


def features(rows, i):
    # Decision is after close i-1; open i and any later values are unavailable.
    closes = [r['close'] for r in rows[i-21:i]]
    changes = [b/a-1 for a, b in zip(closes, closes[1:])]
    volume = [r['volume'] for r in rows[i-20:i]]
    volume_change = volume[-1]/fmean(volume)-1 if all(v is not None for v in volume) and fmean(volume) else 0
    return [closes[-1]/closes[-2]-1, closes[-1]/closes[-6]-1,
            closes[-1]/closes[0]-1, pstdev(changes), volume_change]


def fit_model(samples, iterations=200):
    xs, ys = zip(*samples)
    means = [fmean(x[k] for x in xs) for k in range(5)]
    scales = [pstdev(x[k] for x in xs) or 1 for k in range(5)]
    normalized = [[1]+[max(-10, min(10, (x[k]-means[k])/scales[k])) for k in range(5)] for x in xs]
    weights = [0.0]*6
    for _ in range(iterations):
        grad = [0.0]*6
        for x, y in zip(normalized, ys):
            z = max(-30, min(30, sum(a*b for a, b in zip(weights, x))))
            residual = 1/(1+math.exp(-z))-y
            for k in range(6): grad[k] += residual*x[k]
        weights = [w-.1*(grad[k]/len(xs)+(.1*w if k else 0)) for k, w in enumerate(weights)]
    return means, scales, weights


def probability(model, x):
    means, scales, weights = model
    values = [1]+[max(-10, min(10, (x[k]-means[k])/scales[k])) for k in range(5)]
    z = max(-30, min(30, sum(a*b for a, b in zip(weights, values))))
    return 1/(1+math.exp(-z))


def walk_forward(rows, start='2022-01-01', window=504, refit=63):
    if window < 60 or refit < 1:
        raise ValueError('训练窗口至少60个标签，重训间隔至少1个交易日')
    start = date.fromisoformat(start).isoformat()
    rows = validate_bars(rows)
    first = next((i for i, row in enumerate(rows) if row['date'] >= start), len(rows))
    if first < 21+window+1:
        raise ValueError('评估开始前的历史不足：需要21日特征、训练窗口及1日标签隔离')
    if first >= len(rows)-1:
        raise ValueError('评估日期范围没有可验证的下一开盘标签')
    xs = {i: features(rows, i) for i in range(21, len(rows)-1)}
    labels = {i: int(rows[i+1]['open'] > rows[i]['open']) for i in xs}
    predictions, fits = [], []
    for i in range(first, len(rows)-1):
        if (i-first) % refit == 0:
            # Last training j=i-2: its target ends at open i-1, already observed.
            indices = range(i-1-window, i-1)
            model = fit_model([(xs[j], labels[j]) for j in indices])
            majority = int(sum(labels[j] for j in indices)*2 >= window)
            fits.append({'prediction_start': rows[i]['date'], 'training_start': rows[i-1-window]['date'],
                         'last_training_sample': rows[i-2]['date'], 'last_label_observed': rows[i-1]['date'],
                         'samples': window})
        p = probability(model, xs[i])
        prior_closes = [r['close'] for r in rows[i-30:i]]
        sma = int(fmean(prior_closes[-10:]) > fmean(prior_closes))
        predictions.append({'date': rows[i]['date'], 'decision_date': rows[i-1]['date'],
                            'target_end_date': rows[i+1]['date'], 'actual_up': labels[i],
                            'probability_up': p, 'model': int(p >= .5), 'sma': sma,
                            'always_up': 1, 'training_majority': majority,
                            'market_return': rows[i+1]['open']/rows[i]['open']-1})
    return predictions, fits


def metrics(predictions, name, cost_bps=5):
    if not predictions or not math.isfinite(cost_bps) or not 0 <= cost_bps < 10000:
        raise ValueError('需要有效预测且单边成本位于[0,10000)')
    tp = sum(r[name] == 1 and r['actual_up'] == 1 for r in predictions)
    tn = sum(r[name] == 0 and r['actual_up'] == 0 for r in predictions)
    fp = sum(r[name] == 1 and r['actual_up'] == 0 for r in predictions)
    fn = len(predictions)-tp-tn-fp
    up_recall, down_recall = (tp/(tp+fn) if tp+fn else None), (tn/(tn+fp) if tn+fp else None)
    equity, peak, drawdown, previous, turnover = 1.0, 1.0, 0.0, 0, 0
    fee = cost_bps/10000
    for i, row in enumerate(predictions):
        position = row[name]
        change = abs(position-previous)
        equity *= (1-fee)**change * (1+position*row['market_return'])
        turnover += change
        if i == len(predictions)-1:
            equity *= (1-fee)**position
            turnover += position
        peak = max(peak, equity)
        drawdown = min(drawdown, equity/peak-1)
        previous = position
    return {'samples': len(predictions), 'accuracy': (tp+tn)/len(predictions),
            'balanced_accuracy': (up_recall+down_recall)/2 if up_recall is not None and down_recall is not None else None,
            'up_precision': tp/(tp+fp) if tp+fp else None,
            'confusion': {'tp': tp, 'tn': tn, 'fp': fp, 'fn': fn},
            'total_return': equity-1, 'max_drawdown': drawdown, 'one_way_turnover': turnover,
            'brier_score': fmean((r['probability_up']-r['actual_up'])**2 for r in predictions) if name == 'model' else None}


def paired_interval(predictions, baseline, repetitions=500, block=20):
    """Moving-block paired bootstrap; descriptive, not independent holdout evidence."""
    differences = [int(r['model'] == r['actual_up'])-int(r[baseline] == r['actual_up']) for r in predictions]
    n = len(differences)
    block = min(block, n)
    rng = random.Random(42)
    estimates = []
    for _ in range(repetitions):
        sample = []
        while len(sample) < n:
            begin = rng.randrange(n-block+1)
            sample.extend(differences[begin:begin+block])
        estimates.append(fmean(sample[:n]))
    estimates.sort()
    return {'accuracy_difference': fmean(differences), 'approximate_95pct_block_interval':
            [estimates[int(.025*repetitions)], estimates[min(repetitions-1,int(.975*repetitions))]],
            'block': block, 'repetitions': repetitions, 'seed': 42,
            'note': '描述性移动块重采样，不能修复历史重复查看或多次模型试验；不构成独立预测能力证明。'}


def forecast(input_path, output, start='2022-01-01', window=504, refit=63, cost_bps=5):
    rows, evidence = inspect_dataset(input_path)
    predictions, fits = walk_forward(rows, start, window, refit)
    names = ['model', 'sma', 'always_up', 'training_majority']
    comparison = {name: metrics(predictions, name, cost_bps) for name in names}
    years = {year: {name: metrics([r for r in predictions if r['date'][:4] == year], name, cost_bps) for name in names}
             for year in sorted({r['date'][:4] for r in predictions})}
    summary = {'plan': {'target': '当日开盘到下一交易日开盘上涨；平盘归入非上涨',
                       'decision': '上一交易日收盘后；不使用当日开盘或未来数据作为特征',
                       'features': ['1日收益', '5日收益', '20日收益', '20日波动', '上一日成交量/20日均量-1'],
                       'model': 'L2逻辑回归，训练集标准化；固定学习率0.1、L2系数0.1、200轮迭代、阈值0.5',
                       'window': window, 'refit': refit, 'requested_start': start, 'cost_bps': cost_bps,
                       'purge': '预测i只训练到样本i-2，最后标签截止i-1开盘；缺失成交量特征设为0。',
                       'disclosure': '已有历史上的滚动研究；未进行参数搜索，不是从未看过的独立样本。'},
               'dataset': evidence, 'first_date': predictions[0]['date'], 'last_date': predictions[-1]['date'],
               'label_last_date': predictions[-1]['target_end_date'], 'comparison': comparison,
               'yearly': years, 'training_fits': fits,
               'paired_accuracy_comparison': {name: paired_interval(predictions, name) for name in names[1:]},
               'limitations': ['方向准确率不等于交易胜率或盈利；非上涨预测在收益模拟中为空仓。',
                              '涨跌幅大小、手续费和换月会影响盈利；无杠杆价格指数仍非真实期货账户。',
                              '年度收益模拟在每年重置空仓并平仓，因此不能直接连乘还原全段收益。',
                              '固定模型没有证明比基准更好；概率未经独立校准。']}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output/'predictions.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(predictions[0])); writer.writeheader(); writer.writerows(predictions)
    (output/'forecast.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    labels = {'model': '滚动逻辑回归', 'sma': '10/30均线方向', 'always_up': '始终预测上涨', 'training_majority': '过去训练集多数方向'}
    table = ''.join(f"<tr><td>{labels[name]}</td><td>{m['accuracy']:.2%}</td><td>{format(m['balanced_accuracy'], '.2%') if m['balanced_accuracy'] is not None else '不适用'}</td><td>{m['total_return']:.2%}</td><td>{m['max_drawdown']:.2%}</td></tr>" for name, m in comparison.items())
    doc = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>甲醇滚动方向预测评估</title>
<style>body{{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 20px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:12px;text-align:left;border-bottom:1px solid #ddd}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fb;padding:20px}}p{{line-height:1.7}}</style>
<h1>甲醇滚动方向预测评估</h1><p>{summary['first_date']} 至 {summary['last_date']}，共 {len(predictions)} 次下一开盘方向预测。平盘归入非上涨；所有方法使用相同日期。单边成本 {cost_bps} 基点。</p>
<table><tr><th>方法</th><th>方向准确率</th><th>平衡准确率</th><th>价格指数收益</th><th>最大回撤</th></tr>{table}</table>
<p>方向准确率不是赚钱概率。模型输出上涨时持有、否则空仓，价格指数按开盘至下一开盘计算，最终在最后标签日开盘平仓。整段历史已经看过，不能宣称独立样本外成功。</p>
<h2>相对基准的准确率差与不确定性</h2><pre>{html.escape(json.dumps(summary['paired_accuracy_comparison'],ensure_ascii=False,indent=2))}</pre>
<h2>逐年结果</h2><pre>{html.escape(json.dumps(years,ensure_ascii=False,indent=2))}</pre>
<h2>可复现计划与限制</h2><pre>{html.escape(json.dumps({'plan':summary['plan'],'limitations':summary['limitations'],'dataset':evidence},ensure_ascii=False,indent=2))}</pre></html>'''
    (output/'report.html').write_text(doc, encoding='utf-8')
    return summary
