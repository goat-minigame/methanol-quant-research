"""Chronological evaluation with fixed parameters and explicit reuse disclosure."""
import html
import json
from datetime import date, timedelta
from pathlib import Path
from .backtest import backtest
from .data import inspect_dataset
from .report import write_report


def research(input_path, output, split_date='2022-01-01', fast=10, slow=30, cost_bps=5, start=None, end=None):
    split = date.fromisoformat(split_date)
    if start and date.fromisoformat(start) >= split:
        raise ValueError('训练期开始日期必须早于测试分界日期')
    if end and date.fromisoformat(end) < split:
        raise ValueError('结束日期必须覆盖测试期')
    rows, evidence = inspect_dataset(input_path)
    train = backtest(rows, fast, slow, cost_bps, start, (split-timedelta(days=1)).isoformat())
    test = backtest(rows, fast, slow, cost_bps, split.isoformat(), end)
    if test['summary']['prehistory_bars'] < slow:
        raise ValueError('测试期之前的历史不足以计算慢均线')
    sensitivity = []
    for fee in sorted({0, cost_bps, 10, 20}):
        result = backtest(rows, fast, slow, fee, split.isoformat(), end)
        sensitivity.append({'one_way_cost_bps': fee, **{key: result['summary'][key] for key in
                              ['total_return', 'max_drawdown', 'completed_trades']}})
    plan = {'split_date': split.isoformat(), 'fast': fast, 'slow': slow, 'cost_bps': cost_bps,
            'start': start, 'end': end, 'selection': '固定参数；没有搜索最优参数或根据测试期自动选参',
            'boundary': '训练期在分界前最后交易日收盘结束；测试期重置现金，历史仅用于均线。',
            'disclosure': '历史时间划分评估；整段历史此前已被查看，测试段不是从未看过的独立留出集。禁止据此宣称独立样本外成功。'}
    warnings = ['未建模期货保证金、换月、涨跌停成交和逐日结算；连续主力跳空原因尚未核验。',
                '成本情景仅用于压力比较，不能看完结果后挑选低成本作为真实交易假设。']
    if test['summary']['bars'] < 252 or test['summary']['completed_trades'] < 20:
        warnings.append('测试期不足一年或完整交易少于20次，证据有限；阈值仅为提醒，不是统计显著性标准。')
    summary = {'plan': plan, 'dataset': evidence, 'train': train['summary'], 'test': test['summary'],
               'test_cost_sensitivity': sensitivity, 'warnings': warnings}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    write_report(train, output/'train', input_path)
    write_report(test, output/'test', input_path)
    (output/'research.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    def line(label, item):
        return f"<tr><td>{label}</td><td>{item['total_return']:.2%}</td><td>{item['max_drawdown']:.2%}</td><td>{item['completed_trades']}</td></tr>"
    comparisons = line('训练期', train['summary'])+line('测试期', test['summary'])
    costs = ''.join(line(f"单边 {item['one_way_cost_bps']} 基点", item) for item in sensitivity)
    table_head = '<tr><th>区间 / 情景</th><th>累计收益</th><th>最大回撤</th><th>完整交易数</th></tr>'
    doc = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>甲醇研究可信度评估</title>
<style>body{{font:16px system-ui;max-width:1000px;margin:40px auto;padding:0 20px;color:#172334}}td,th{{padding:12px;border-bottom:1px solid #ddd;text-align:left}}table{{border-collapse:collapse;width:100%}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7fb;padding:20px}}p{{line-height:1.7}}</style>
<h1>甲醇研究可信度评估</h1><p>固定均线 {fast}/{slow}，单边成本 {cost_bps} 基点，测试分界 {split.isoformat()}。{html.escape(plan['disclosure'])}</p>
<h2>按时间划分的表现</h2><table>{table_head}{comparisons}</table><p><a href="train/report.html">训练期详细报告</a> · <a href="test/report.html">测试期详细报告</a></p>
<h2>测试期成本压力比较</h2><table>{table_head}{costs}</table>
<h2>研究限制</h2><ul>{''.join('<li>'+html.escape(w)+'</li>' for w in warnings)}</ul>
<h2>研究计划与数据检查</h2><pre>{html.escape(json.dumps({'plan':plan,'dataset':evidence},ensure_ascii=False,indent=2))}</pre></html>'''
    (output/'report.html').write_text(doc, encoding='utf-8')
    return summary
