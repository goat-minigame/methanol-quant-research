"""Long/flat SMA research, previous-close signals executed at next open."""
import math
from datetime import date
from statistics import fmean, pstdev
from .data import validate_bars


def backtest(rows, fast=10, slow=30, cost_bps=5, start=None, end=None, signals=None, strategy="ma_cross"):
    if not isinstance(fast, int) or not isinstance(slow, int) or not 1 <= fast < slow:
        raise ValueError('均线参数须满足 1 <= fast < slow')
    if not math.isfinite(cost_bps) or not 0 <= cost_bps < 10000:
        raise ValueError('单边成本基点须在 [0, 10000)')
    start = date.fromisoformat(start).isoformat() if start else None
    end = date.fromisoformat(end).isoformat() if end else None
    if start and end and start > end:
        raise ValueError('开始日期不能晚于结束日期')
    bars = [bar for bar in validate_bars(rows) if end is None or bar['date'] <= end]
    if len(bars) <= slow:
        raise ValueError(f'至少需要 {slow + 1} 个交易日才能执行策略')
    first_index = next((i for i, bar in enumerate(bars) if start is None or bar['date'] >= start), None)
    if first_index is None:
        raise ValueError('选定日期范围没有行情')
    closes = [bar['close'] for bar in bars]
    equity, benchmark, peak, previous = 1.0, 1.0, 1.0, 0
    curve, trades, returns = [], [], []
    active = None
    fee = cost_bps / 10000
    for i in range(first_index, len(bars)):
        bar = bars[i]
        # Only information from days before i is available at the open of i.
        if signals is None:
            position = int(i >= slow and fmean(closes[i-fast:i]) > fmean(closes[i-slow:i]))
        else:
            position = signals[bar['date']]
            if position not in (0,1): raise ValueError('策略仓位只支持0或1')
        opening = bar['open']
        ending = bars[i+1]['open'] if i+1 < len(bars) else bar['close']
        turnover = abs(position - previous)
        if previous and not position:
            active.update(exit_date=bar['date'], exit_price=opening, exit_reason='均线离场' if signals is None else '策略离场')
            active['return_after_cost'] = opening / active['entry_price'] * (1-fee)**2 - 1
            trades.append(active)
            active = None
        if position and not previous:
            active = {'entry_date': bar['date'], 'entry_price': opening}
        # Charge an entry/exit cost against equity, then the open-to-open return.
        factor = (1-fee)**turnover * (1 + position * (ending/opening - 1))
        if i == len(bars)-1 and position:
            factor *= 1-fee
            turnover += 1
            active.update(exit_date=bar['date'], exit_price=ending, exit_reason='样本结束平仓')
            active['return_after_cost'] = ending / active['entry_price'] * (1-fee)**2 - 1
            trades.append(active)
        daily_return = factor - 1
        returns.append(daily_return)
        equity *= factor
        benchmark *= ending/opening
        peak = max(peak, equity)
        curve.append({'date': bar['date'], 'position': position, 'interval_end_date': bars[i+1]['date'] if i+1 < len(bars) else bar['date'],
                      'return': daily_return, 'equity': equity, 'benchmark': benchmark,
                      'drawdown': equity/peak-1, 'turnover': turnover})
        previous = position
    vol = pstdev(returns)
    summary = {
        'strategy': strategy, 'bars': len(curve), 'first_date': curve[0]['date'], 'last_date': bars[-1]['date'],
        'fast': fast, 'slow': slow, 'one_way_cost_bps': cost_bps,
        'total_return': equity-1, 'annualized_return': equity**(252/len(curve))-1,
        'max_drawdown': min(row['drawdown'] for row in curve),
        'annualized_volatility': vol * math.sqrt(252),
        'sharpe_zero_rate': fmean(returns)/vol * math.sqrt(252) if vol else None,
        'win_rate': sum(t['return_after_cost']>0 for t in trades)/len(trades) if trades else None,
        'completed_trades': len(trades), 'benchmark_return': benchmark-1,
        'position_days': sum(row['position'] for row in curve),
        'prehistory_bars': first_index, 'requested_start': start, 'requested_end': end,
        'evaluation_policy': '评估起点重置现金；此前历史仅用于均线；终点当日收盘强制平仓。',
        'large_open_gaps': [{'date': bars[i]['date'], 'previous_date': bars[i-1]['date'],
                             'gap_return': bars[i]['open']/bars[i-1]['close']-1}
                            for i in range(max(1, first_index), len(bars))
                            if abs(bars[i]['open']/bars[i-1]['close']-1) >= .10],
        'model': '无杠杆价格指数；上一日收盘信号，下一日开盘执行；成本按单边基点计。',
    }
    return {'summary': summary, 'curve': curve, 'trades': trades}
