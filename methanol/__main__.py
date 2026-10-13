import argparse
import json
import sys
from .data import fetch_data, inspect_dataset
from .backtest import backtest
from .report import write_report
from .research import research
from .forecast import forecast
from .markets import futures_list, futures_quotes, option_underlyings, options_query
from .derivatives import volatility, black76
from .indicators import indicator_report
from .strategies import compare
from .fundamentals import catalog, fetch_fundamental, inspect_releases


def main():
    parser = argparse.ArgumentParser(description='甲醇公开行情下载与量化学习工具（核心为标准库，在线期权/基本面可选AKShare）')
    commands = parser.add_subparsers(dest='command', required=True)
    fetch = commands.add_parser('fetch', help='下载新浪公开甲醇期货日线，保留来源记录')
    fetch.add_argument('--symbol', default='MA0')
    fetch.add_argument('--output', default='data/ma_daily.csv')
    fetch.add_argument('--timeout', type=float, default=30)
    check = commands.add_parser('check', help='校验已有 CSV 的价格、日期和字段')
    check.add_argument('--input', default='data/ma_daily.csv')
    run = commands.add_parser('backtest', help='运行上一日信号、下一日开盘执行的均线策略')
    run.add_argument('--input', default='data/ma_daily.csv')
    run.add_argument('--output', default='reports/ma')
    run.add_argument('--fast', type=int, default=10)
    run.add_argument('--slow', type=int, default=30)
    run.add_argument('--cost-bps', type=float, default=5)
    run.add_argument('--start', help='评估开始日期 YYYY-MM-DD，之前历史仅用于均线')
    run.add_argument('--end', help='评估结束日期 YYYY-MM-DD')
    study = commands.add_parser('research', help='固定参数的训练/测试时间划分与成本压力评估')
    study.add_argument('--input', default='data/ma_daily.csv')
    study.add_argument('--output', default='reports/research')
    study.add_argument('--split-date', default='2022-01-01')
    study.add_argument('--fast', type=int, default=10)
    study.add_argument('--slow', type=int, default=30)
    study.add_argument('--cost-bps', type=float, default=5)
    study.add_argument('--start')
    study.add_argument('--end')
    predict = commands.add_parser('forecast', help='带标签隔离的滚动方向预测与基准比较')
    predict.add_argument('--input', default='data/ma_daily.csv')
    predict.add_argument('--output', default='reports/forecast')
    predict.add_argument('--start', default='2022-01-01')
    predict.add_argument('--window', type=int, default=504)
    predict.add_argument('--refit', type=int, default=63)
    predict.add_argument('--cost-bps', type=float, default=5)
    futures = commands.add_parser('futures', help='整合上游18品种目录与东方财富期货报价')
    future_cmd = futures.add_subparsers(dest='action', required=True)
    future_cmd.add_parser('list')
    quote = future_cmd.add_parser('quote')
    quote.add_argument('aliases', nargs='?', default='MA')
    quote.add_argument('--output')
    options = commands.add_parser('options', help='ETF/指数期权与新增甲醇期权适配')
    option_cmd = options.add_subparsers(dest='action', required=True)
    option_cmd.add_parser('underlyings')
    for action in ('expiries', 'chain', 'values'):
        option = option_cmd.add_parser(action)
        option.add_argument('code')
        option.add_argument('--output')
        if action == 'chain': option.add_argument('--expiry', required=True)
    pricing = option_cmd.add_parser('price', help='离线Black-76欧式期货期权近似，不是真实报价')
    pricing.add_argument('--futures', type=float, required=True)
    pricing.add_argument('--strike', type=float, required=True)
    pricing.add_argument('--days', type=float, required=True)
    pricing.add_argument('--rate', type=float, default=.02)
    pricing.add_argument('--sigma', type=float, required=True)
    pricing.add_argument('--kind', choices=['call','put'], default='call')
    vol = commands.add_parser('volatility', help='整合历史波动率与滚动百分位研究')
    vol.add_argument('--input', default='data/ma_daily.csv')
    vol.add_argument('--window', type=int, default=20)
    vol.add_argument('--lookback', type=int, default=120)
    vol.add_argument('--output')
    indicators = commands.add_parser('indicators', help='甲醇日线技术指标与暖机状态')
    indicators.add_argument('--input',default='data/ma_daily.csv')
    indicators.add_argument('--output',default='reports/indicators.json')
    indicators.add_argument('--csv-output',default='reports/indicators.csv')
    comparisons = commands.add_parser('compare-strategies', help='固定7策略的同日期/同成本训练测试比较')
    comparisons.add_argument('--input',default='data/ma_daily.csv')
    comparisons.add_argument('--output',default='reports/strategies')
    comparisons.add_argument('--split-date',default='2022-01-01')
    comparisons.add_argument('--cost-bps',type=float,default=5)
    basics = commands.add_parser('fundamentals', help='甲醇宏观/现货/仓单入口与发布时间审计')
    basic_cmd = basics.add_subparsers(dest='action',required=True)
    basic_cmd.add_parser('catalog')
    fundamental_fetch=basic_cmd.add_parser('fetch')
    fundamental_fetch.add_argument('kind',choices=['pmi','ppi','industrial','basis','warehouse'])
    fundamental_fetch.add_argument('--date')
    fundamental_fetch.add_argument('--output')
    releases=basic_cmd.add_parser('inspect')
    releases.add_argument('--input',required=True)
    releases.add_argument('--as-of',required=True)
    releases.add_argument('--output')
    args = parser.parse_args()
    try:
        if args.command == 'indicators':
            result=indicator_report(args.input,args.output,args.csv_output)
        elif args.command == 'compare-strategies':
            result=compare(args.input,args.output,args.split_date,args.cost_bps)
        elif args.command == 'fundamentals':
            if args.action == 'catalog':result=catalog()
            elif args.action == 'fetch':result=fetch_fundamental(args.kind,args.date,args.output)
            else:result=inspect_releases(args.input,args.as_of,args.output)
        elif args.command == 'futures':
            result = futures_list() if args.action == 'list' else futures_quotes(args.aliases, args.output)
        elif args.command == 'options':
            if args.action == 'underlyings': result = option_underlyings()
            elif args.action == 'price': result = black76(args.futures, args.strike, args.days, args.rate, args.sigma, args.kind)
            else: result = options_query(args.action,args.code,getattr(args,'expiry',None),args.output)
        elif args.command == 'volatility':
            result = volatility(args.input,args.window,args.lookback,args.output)
        elif args.command == 'fetch':
            result = fetch_data(args.output, args.symbol, args.timeout)
        elif args.command == 'check':
            _, result = inspect_dataset(args.input)
        elif args.command == 'forecast':
            result = forecast(args.input, args.output, args.start, args.window, args.refit, args.cost_bps)
        elif args.command == 'research':
            result = research(args.input, args.output, args.split_date, args.fast, args.slow, args.cost_bps, args.start, args.end)
        else:
            bars, _ = inspect_dataset(args.input)
            result = write_report(backtest(bars, args.fast, args.slow, args.cost_bps, args.start, args.end), args.output, args.input)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('errors') else 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f'错误: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
