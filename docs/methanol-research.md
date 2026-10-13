# 甲醇期货研究指南

## 当前能力与入口

```bash
python -m methanol indicators
python -m methanol compare-strategies
python -m methanol fundamentals catalog
python -m methanol futures quote MA --output data/snapshots/ma-quote.json
.venv/bin/python -m methanol options expiries MA
# 本次实际返回 ma2701；未来需要重新查询上市合约
.venv/bin/python -m methanol options chain MA --expiry ma2701 --output data/snapshots/ma2701-chain.json
.venv/bin/python -m methanol fundamentals fetch warehouse --date 2026-10-12 --output data/fundamentals/warehouse-20261012.json
```

指标 JSON/CSV 在 `reports/indicators.*`；7策略比较在 `reports/strategies/report.html`，含同一数据指纹、同一训练/测试日期、同一成本的比较及逐策略交易记录。

## 技术指标定义

共29个输出字段（部分属于同一指标族，不代表29种独立因子）：SMA5/10/20/30/60、EMA12/26、MACD的DIF/DEA/柱、RSI14、ATR14、OBV、K/D/J9、布林中/上/下轨及带宽、CCI20、唐奇安高/低20、成交量均线与比值20、ROC10、Momentum10、Williams %R14、20日年化已实现波动率。

- 只使用截至本日的已知行情；日内交易信号应使用上一日输出。
- EMA用首个完整窗口均值初始化；MACD参数12/26/9，柱为2*(DIF-DEA)。
- RSI与ATR采用Wilder平滑；ATR含隔夜真实波幅，平盘RSI定义为50。
- 布林带20日、2倍总体标准差；CCI20用0.015倍平均绝对偏差。
- KDJ用9日RSV，K/D从50初始化、按1/3平滑；%R14范围[-100,0]。
- 成交量缺失不伪造；缺失后的累计OBV不能恢复为完整序列，保持null。
- 已实现波动率用简单日收益总体标准差乘sqrt(252)，与`volatility`命令的样本标准差口径不同，字段名称和定义分别保留。
- 初始窗口不足时输出null。EMA初始化、平滑、总体/样本标准差会造成不同软件指标差异；不可将差异直接当成策略优势。

## 7个固定策略

全部为做多/空仓价格指数研究，上日收盘信号、次日开盘执行；结束时收盘平仓，进出单边成本分别扣除。

| 策略 | 固定规则 |
|---|---|
| buy_hold | 全期持有基准，含进出成本 |
| ma_cross | SMA10 > SMA30时持有 |
| macd | DIF > DEA时持有 |
| rsi | RSI14 <30进入，>70离开，否则保持既往状态 |
| boll | 收盘低于布林下轨进入，到达中轨离开，否则保持既往状态 |
| kdj | K9 > D9时持有 |
| ensemble | 上述5个技术策略至少3个持有时持有 |

训练截至2021-12-31收盘，测试为2022-01-04至2026-10-12。状态型RSI/BOLL规则在测试开始前可读取既往状态，但测试账户重置现金，不能继承训练收益。初始指标暖机保持空仓；持有基准不需要指标暖机。

### 当前真实历史比较（单边5基点）

| 策略 | 训练收益 | 测试收益 | 测试最大回撤 | 测试完整交易 |
|---|---:|---:|---:|---:|
| buy_hold | 40.42% | 28.26% | 40.05% | 1 |
| ma_cross | 28.63% | -15.86% | 37.48% | 24 |
| macd | 48.95% | -3.99% | 32.80% | 49 |
| rsi | -17.87% | 49.77% | 32.83% | 6 |
| boll | -28.36% | -5.03% | 27.10% | 16 |
| kdj | 43.32% | -3.42% | 36.05% | 99 |
| ensemble | 44.76% | 17.24% | 25.31% | 65 |

不能看完测试收益再挑选参数/策略并声称预测能力。RSI只有6次完整交易且训练亏损，不能凭测试收益高作推荐；持有基准只有1笔交易不代表无效，但不能据此估计交易胜率。全部为已查看历史的回顾性比较，连续主力换月、保证金、涨跌停和成交约束仍未建模，不是期货账户收益或实盘建议。

## 宏观和产业链入口

```bash
.venv/bin/python -m methanol fundamentals fetch pmi --output data/fundamentals/pmi.json
.venv/bin/python -m methanol fundamentals fetch ppi --output data/fundamentals/ppi.json
.venv/bin/python -m methanol fundamentals fetch industrial --output data/fundamentals/industrial.json
.venv/bin/python -m methanol fundamentals fetch basis --date 2026-10-12 --output data/fundamentals/basis-20261012.json
```

PMI/PPI/工业增加值采用AKShare/Jin10公共接口；甲醇现货/基差用AKShare/生意社，仓单用AKShare/郑商所。基差表保留原有具体合约和报价口径，不拿主力连续价格随意替换。仓单与港口库存、社会库存是不同数据。

港口库存、甲醇开工率、进口量、煤炭成本和MTO开工率当前仍为待补充来源，不能把目录中的研究项目说成已下载数据。

### 当时可用数据审计

自行收集的产业链数据应保留观测日、精确公布时刻、单位和出处。CSV格式：

```csv
metric,observation_date,available_at,value,unit,source
```

`available_at`须为带时区的ISO时间，如`2026-10-13T10:00:00+08:00`。这里没有附加虚构数值。用实际数据文件执行：

```bash
python -m methanol fundamentals inspect --input data/industry-releases.csv --as-of 2026-10-13T09:00:00+08:00 --output reports/available-industry.json
```

只能看见该时刻以前公布的记录；修订数据也按修订发布时间筛选，不回填到过去。输出每项最新可见记录、观测年龄及排除的未来公布记录数量。手工公布时刻仍需要外部核验。本版不把这些快照自动拼入模型，因为尚未建立完整的历史版本与发布时间序列。

## 验证与限制（2026-10-13）

- 57项测试通过，覆盖旧能力与新增指标公式、暖机、未来数据修改、策略边界、同日期比较、公布时间和修订隔离。
- 2,857根MA0真实日线上的指标和7策略报告已实际生成。
- 甲醇东方财富主连报价、Sina期权上市合约列表已实际获取；`ma2701`链返回82个期权合约。部分持仓量缺失，严格PCR为null，没有用缺失值补零。
- 郑商所2026-10-12甲醇仓单表实际获取129行；保留原始表，没有将仓库小计/合计重复加总。
- PMI与现货基差查询仍被新域名代理403阻塞；PPI/工业增加值适配函数存在，但未完成在线验证，不宣称可用。
- 新增 `datacenter-api.jin10.com`、`cdn.jin10.com`、`www.100ppi.com` 已保存为环境网络草稿，需在环境设置保存并发布后再试；原有网络许可保留。
- 行情采集时间不等于交易时点；新取得报价没有可靠提供方时间戳，不能把它与上一交易日收盘作“同时间跨源一致性”验证。
