"""Write portable CSV/JSON and a self-contained HTML report."""
import csv
import html
import json
from pathlib import Path
from .data import inspect_dataset


def chart(series):
    values = [value for _, data, _ in series for value in data]
    lo, hi = min(values), max(values)
    span = hi-lo or 1
    paths = []
    for label, data, color in series:
        points = ' '.join(f'{45+710*i/max(len(data)-1,1):.2f},{260-220*(v-lo)/span:.2f}' for i, v in enumerate(data))
        paths.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points}"/><title>{html.escape(label)}</title>')
    return f'<svg viewBox="0 0 800 300" role="img" aria-label="策略净值蓝色，基准净值灰色"><text x="5" y="40">{hi:.2f}</text><text x="5" y="260">{lo:.2f}</text>{"".join(paths)}<text x="45" y="290">样本开始</text><text x="680" y="290">样本结束</text></svg>'


def write_report(result, output, input_path):
    _, evidence = inspect_dataset(input_path)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    summary = dict(result['summary'], input_file=str(input_path), dataset=evidence)
    summary_path = output/'summary.json'
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    for name in ('curve', 'trades'):
        rows = result[name]
        fields = list(rows[0]) if rows else ['entry_date', 'entry_price', 'exit_date', 'exit_price', 'exit_reason', 'return_after_cost']
        with (output/f'{name}.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    provenance = Path(input_path).with_suffix('.metadata.json')
    source_text = json.dumps(evidence, ensure_ascii=False, indent=2) if provenance.exists() else '本地 CSV：来源未经工具验证。请自行核验；测试夹具属于合成数据。'
    parameter_text = f"均线 {summary['fast']}/{summary['slow']}" if summary.get('strategy','ma_cross') == 'ma_cross' else '固定策略规则见甲醇研究指南'
    metrics = [('累计收益', f"{summary['total_return']:.2%}"), ('最大回撤', f"{summary['max_drawdown']:.2%}"),
               ('基准收益', f"{summary['benchmark_return']:.2%}"), ('已完成交易', str(summary['completed_trades']))]
    cards = ''.join(f'<div class="card">{label}<strong>{value}</strong></div>' for label, value in metrics)
    plot = chart([('策略', [r['equity'] for r in result['curve']], '#1565c0'), ('基准', [r['benchmark'] for r in result['curve']], '#888')])
    doc = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>甲醇量化学习报告</title>
<style>body{{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 20px;background:#f5f7fb;color:#172334}}.cards{{display:flex;flex-wrap:wrap;gap:12px}}.card{{background:white;padding:20px;border-radius:10px;flex:1;min-width:150px}}strong{{display:block;font-size:26px;margin-top:10px}}svg{{width:100%;background:white;margin:20px 0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:20px}}p{{line-height:1.7}}</style>
<h1>甲醇量化学习报告</h1><p>策略：{html.escape(summary.get('strategy','ma_cross'))}。{summary['first_date']} 至 {summary['last_date']} · {summary['bars']} 根日线 · {parameter_text} · 单边成本 {summary['one_way_cost_bps']} 基点</p>
<div class="cards">{cards}</div>{plot}<p>蓝色：策略净值；灰色：同期无杠杆持有价格指数。策略暖机期保持现金；最后一个交易日收盘平仓。</p>
<h2>数据检查提醒</h2><p>发现 {len(summary["large_open_gaps"])} 处开盘价相对上一收盘价跳变达到 10%。详情见完整指标 large_open_gaps；可能涉及真实波动、换月或数据问题，本工具未自动剔除或修正。</p><h2>如何理解结果</h2><p>评估前历史仅用于计算均线，评估起点账户重置为空仓；评估终点收盘强制平仓。数据哈希仅证明本地文件一致，不能代替独立来源核验。</p><p>上一日收盘形成信号，下一日开盘执行。每行净值对应当日开盘至下一日开盘（最后一行为当日收盘），回撤只在这些观测点计算，不代表盘中最大风险。年化采用 252 个交易日，短样本年化容易失真。</p>
<p>这是价格序列研究练习。MA0 主力连续可能有换月跳空；没有模拟实际换月、涨跌停无法成交、保证金、杠杆、逐日结算、合约乘数及真实每手手续费。结果不是可执行的期货账户收益，也不是投资建议。</p>
<h2>数据来源</h2><pre>{html.escape(source_text)}</pre><h2>完整指标</h2><pre>{html.escape(json.dumps(summary,ensure_ascii=False,indent=2))}</pre></html>'''
    (output/'report.html').write_text(doc, encoding='utf-8')
    return summary
