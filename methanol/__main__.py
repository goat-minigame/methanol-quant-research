import argparse
import json
import sys
from .data import fetch_data, read_csv
from .backtest import backtest
from .report import write_report


def main():
    parser = argparse.ArgumentParser(description='甲醇公开行情下载与量化学习工具（Python 标准库，无额外依赖）')
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
    args = parser.parse_args()
    try:
        if args.command == 'fetch':
            result = fetch_data(args.output, args.symbol, args.timeout)
        elif args.command == 'check':
            bars = read_csv(args.input)
            result = {'rows': len(bars), 'first_date': bars[0]['date'], 'last_date': bars[-1]['date']}
        else:
            result = write_report(backtest(read_csv(args.input), args.fast, args.slow, args.cost_bps), args.output, args.input)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(f'错误: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
