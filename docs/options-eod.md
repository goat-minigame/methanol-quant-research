# MA 期权日终接入：Issue #2 首版（部分 P0）

依赖已合并的 Issue #1 / PR #4 的 `sources` 公共存证工具，但使用独立 SQLite、
独立 `methanol.options` 包和 CLI。未修改共享 CLI、公共 sources、CI 或原油模块。
这不是 Issue #2 全部完成：仅采集与观察，不产生买入建议、自动交易或通知。

## 运行

Python 3.10+ 标准库即可，不需要密钥或可选行情库：

```bash
python -m methanol.options ingest --date 2026-10-12
python -m methanol.options ingest --date 2026-10-13
python -m methanol.options status
python -m methanol.options report --output reports/options/observation.json
python -m methanol.options status --as-of 2026-10-13T09:00:00Z
python -m methanol.options backup private-acceptance/options-backup
python scripts/check_offline.py
```

全局 `--db`、`--raw-dir` 放在子命令之前；默认分别为
`data/options/options.sqlite3`、`data/options/raw`。`--date` 必填，不推断交易日。
网络失败或解析失败保存 attempt 并返回 1；不会生成合成行情。
`status/report` 的退出码 0 只说明读取无完整性错误，**不表示数据新鲜或可交易**。

恢复：将备份中的 `options.sqlite3` 和整个 `raw/` 一起复制到新的私人目录，
用 `python -m methanol.options --db <目录>/options.sqlite3 --raw-dir <目录>/raw status`
验证。证据文件按哈希名定位，不依赖旧机器绝对路径。
备份目标必须不存在，避免覆盖；备份使用 SQLite backup API。

## 来源与字段

官方公开 TXT，日期同时从 URL 和正文核验：

- `https://www.czce.com.cn/cn/DFSStaticFiles/Option/2026/20261012/OptionDataDaily.txt`
- `https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20261012/FutureDataDaily.txt`
- 2026-10-13 使用相同路径格式替换日期。

响应原始字节、SHA-256、请求地址、请求起止、UTC 响应完成时间和解析时间均保存。
期权逐行保存原始字段，字段级来源继承所在响应；报告附原始哈希和证据哈希。
公开访问不等于允许再分发，真实数据与数据库不提交到公开仓库。

当前目标固定为 MA2611 和 MA2701：按观察日期的唯一 24 个月交割窗口，
将 MA611 / MA701 解析为对应期货，并将例如 MA611P3600 映射为 MA2611P3600。
保留交易所原始代码；拒绝 MA0、无效月份、缺任一标的、缺 C/P、重复或未知 MA 代码。
期权和期货按同日、同标的关联，不能用主连或另一日期价格替代。

| 字段 | 口径与缺项处理 |
|---|---|
| close / settlement | 今收盘与今结算分别保存，绝不互相填充 |
| volume / open_interest | 原始成交量（手）与持仓量；缺失 null，零量仍为零量 |
| 无成交 / 零收盘 | close=null，原文数值留 raw_fields；不将零价当可成交价格 |
| latest / bid / ask / spread / executable_capacity | 全部 null；日表不证明实时盘口或成交容量 |
| observed_date | 官方正文交易日期；不是精确报价时间 |
| quote_timestamp / published_at | null；不把抓取时间当交易或发布时间 |
| expiry / multiplier / fees / price_unit | null，规则与报价单位未完成权威核验，不猜测 |
| count_unit | lot，由日表“成交量(手)”表头记录 |
| IV / Greeks / 历史分位 | 归一化字段未实现，全部 null；官方表内原始 DELTA/隐含波动率只保留原文，不替代模型或历史分位 |

规则核验尝试：交易所主页及甲醇品种页面
`https://www.czce.com.cn/cn/sspz/jc/H770304index_1.htm` 返回 HTTP 412。
停止该入口，未改网络设置、未绕反爬。尚不能证明到期规则、交易日历、乘数、费用和报价单位，
这些是待补验项目，不是已经完成的规则映射。到期、停牌状态也不能从缺行或零成交推出。

## 时间、修订与闸门

每个 `(kind, day, raw_hash, parser_version)` 保存一个版本；同响应复跑不重复入库，
但每次 attempt 保留。新哈希或新解析版本另存，不覆盖原版本。
`available_at=max(响应完成时间, 解析完成时间)`；as-of 仅看到该时刻已经可得的版本，
同日期选择最后入库的可见版本。时间戳必须带时区，统一按 UTC 比较。
返回以前见过的完全相同响应按幂等重复处理，不推断为交易所主动恢复旧版本。

`status/report` 会重读证据和原始响应并校验哈希、规范化结果、来源与可用时间。
不支持的旧解析器版本隔离并提示重新采集；不会拿新解释倒灌过去。
当前 SQLite 不是带数字签名的防恶意篡改账本，哈希不能独立证明交易所真实性。

新鲜度保守要求与 as-of 的上海自然日相同；不自行用周末/节假日推断仍然新鲜。
旧日数据保留作历史观察并标 stale。即便当天数据完整，盘口、规则和事件证据不齐，
仍固定输出 **“暂无合格信号”**，`candidates=[]`，逐行列缺项和对应期货关联。
没有可调阈值把不合格数据变成候选。

MTO 流拍、美伊事件验证、消息去重/冷却、实时行情、可成交容量、交易费、到期计算、
IV/Greeks 与历史分位均未实现。4→140 是用户未买入的历史事件，不是真实持仓。
本轮不回放或验证该倍数，不以周末或当前消息制造周五信号。

## 2026-10-13 实际验收（脱敏摘要）

最终解析器首次采集 UTC：

| 观察日 | 期权完成时间 | 期货完成时间 | 期权条数 / 期货条数 |
|---|---|---|---|
| 2026-10-12 | 09:23:29.269699 | 09:23:32.899569 | 188 / 2 |
| 2026-10-13 | 09:23:40.576964 | 09:23:41.464060 | 188 / 2 |

各日两条期货分别是 MA2611、MA2701。在线相同响应复跑新增快照为零。
当天/前日观察均不能证明流动性和事件条件，报告无候选。
原始文件指纹（期权 / 期货）：

- 10-12：`1354fe02063d26067920103c3277a7a5eab4c679d9ac26193dfc0a1b7149417a` /
  `fda6df13a2bee9682417a24de85ab322f28a7018a2f48835089a4686d3717386`
- 10-13：`a30082affc77f17cb31bad0210d114335dc658c080fff7f7c1a1d4d49a0c52e3` /
  `34aa6a602d3725497d44c3666253a7e3da7ff72979224247398f2156ad516262`

135 项离线测试通过（旧 103 + 新 32），含禁网络全量回归、真实格式的明确合成 fixture、
时间门禁、修订 as-of、幂等、规则缺项、缺价、陈旧、故障存证、证据/数据库篡改拒绝、
CLI 和移动目录后的备份恢复。fixture 均不来自真实行情。

字段缺项数量：10-12 的 188 条期权中 38 条无有效收盘；10-13 中 57 条无有效收盘。
各日 MA2611 链 106 条、MA2701 链 82 条。全部期权缺 bid/ask、可成交容量和已验证规则。
这类缺项不会用结算价、最新价或模拟盘口补足。
