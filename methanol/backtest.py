"""Long/flat SMA research, previous-close signals executed at next open."""
import math
from statistics import fmean, pstdev
from .data import validate_bars


def backtest(rows, fast=10, slow=30, cost_bps=5):
    if not isinstance(fast, int) or not isinstance(slow, int) or not 1 <= fast < slow:
        raise ValueError('均线参数须满足 1 <= fast < slow')
    if not math.isfinite(cost_bps) or not 0 <= cost_bps < 10000:
        raise ValueError('单边成本基点须在 [0, 10000)')
    bars = validate_bars(rows)
    if len(bars) <= slow:
        raise ValueError(f'至少需要 {slow + 1} 个交易日才能执行策略')
    closes = [bar['close'] for bar in bars]
    equity, benchmark, peak, previous = 1.0, 1.0, 1.0, 0
    curve, trades, returns = [], [], []
    active = None
    fee = cost_bps / 10000
    for i, bar in enumerate(bars):
        # Only information from days before i is available at the open of i.
        position = int(i >= slow and fmean(closes[i-fast:i]) > fmean(closes[i-slow:i]))
        start = bar['open']
        end = bars[i+1]['open'] if i+1 < len(bars) else bar['close']
        turnover = abs(position - previous)
        if previous and not position:
            active.update(exit_date=bar['date'], exit_price=start, exit_reason='均线离场')
            active['return_after_cost'] = start / active['entry_price'] * (1-fee)**2 - 1
            trades.append(active)
            active = None
        if position and not previous:
            active = {'entry_date': bar['date'], 'entry_price': start}
        # Charge an entry/exit cost against equity, then the open-to-open return.
        factor = (1-fee)**turnover * (1 + position * (end/start - 1))
        if i == len(bars)-1 and position:
            factor *= 1-fee
            turnover += 1
            active.update(exit_date=bar['date'], exit_price=end, exit_reason='样本结束平仓')
            active['return_after_cost'] = end / active['entry_price'] * (1-fee)**2 - 1
            trades.append(active)
        daily_return = factor - 1
        returns.append(daily_return)
        equity *= factor
        benchmark *= end/start
        peak = max(peak, equity)
        curve.append({'date': bar['date'], 'position': position, 'interval_end_date': bars[i+1]['date'] if i+1 < len(bars) else bar['date'],
                      'return': daily_return, 'equity': equity, 'benchmark': benchmark,
                      'drawdown': equity/peak-1, 'turnover': turnover})
        previous = position
    vol = pstdev(returns)
    summary = {
        'bars': len(bars), 'first_date': bars[0]['date'], 'last_date': bars[-1]['date'],
        'fast': fast, 'slow': slow, 'one_way_cost_bps': cost_bps,
        'total_return': equity-1, 'annualized_return': equity**(252/len(bars))-1,
        'max_drawdown': min(row['drawdown'] for row in curve),
        'annualized_volatility': vol * math.sqrt(252),
        'sharpe_zero_rate': fmean(returns)/vol * math.sqrt(252) if vol else None,
        'completed_trades': len(trades), 'benchmark_return': benchmark-1,
        'position_days': sum(row['position'] for row in curve),
        'large_open_gaps': [{'date': bars[i]['date'], 'previous_date': bars[i-1]['date'],
                             'gap_return': bars[i]['open']/bars[i-1]['close']-1}
                            for i in range(1, len(bars))
                            if abs(bars[i]['open']/bars[i-1]['close']-1) >= .10],
        'model': '无杠杆价格指数；上一日收盘信号，下一日开盘执行；成本按单边基点计。',
    }
    return {'summary': summary, 'curve': curve, 'trades': trades}
