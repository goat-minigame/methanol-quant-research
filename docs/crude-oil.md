# Issue #3 原油事实管道首版（草稿）

基于 `de27636b2de32933dafd46ba7541045d3260adbb`，仅新增 `crude_oil/`、独立测试与本文档。复用 Issue #1 的 SQLite、哈希归档、版本及时间门禁；不修改 MA 数据源、默认采集目录或任何既有预测。无需新增运行依赖，Python 3.10+ 标准库即可运行。

## 命令与安全配置

```bash
# 显式联网采集；缺少认证或来源时退出1，成功来源仍正常保存
python -m crude_oil sources ingest --dates 2026-10-09 2026-10-12
# 独立采集官方无密钥数据
python -m crude_oil sources ingest --dates 2026-10-09 2026-10-12 \
  --sources ine_sc eia_public_stocks eia_public_flows eia_public_refinery
python -m crude_oil sources status
python -m crude_oil report
python -m crude_oil report --as-of 2026-10-13T09:18:00Z --output reports/crude/historical.json
python -m crude_oil backup --destination data/crude/backup-new
python -m crude_oil restore --source data/crude/backup-new --destination data/crude/restored-new
python -m crude_oil report --db data/crude/restored-new/crude.sqlite3
python scripts/check_offline.py
```

默认数据库 `data/crude/crude.sqlite3`，原文 `data/crude/raw/`，JSON事实报告 `reports/crude/report.json`，均已被仓库现有忽略规则排除。建议原油使用独立数据库。status/report 仅查看本地证据，不隐式联网或创建提醒。备份和恢复目标必须不存在，不覆盖旧库；移动备份后亦可恢复。`--timeout` 为正有限单请求秒数，`--retries` 为0–3；仅网络超时、429和部分5xx重试，每次尝试单独存档。曾发生失败但重试成功仍保留错误并返回1，方便审计。

EIA 最小配置：用户通过安全环境配置渠道注入 **`EIA_API_KEY`**。不在聊天、命令参数、仓库或 `.env` 文件中填写值。工具只读取这个指定环境变量，不搜索其他凭据，不生成密钥，不保存持久凭据。缺失时本地记录 `authentication_required`，不使用 demo key。

[EIA官方API说明](https://www.eia.gov/opendata/documentation.php)要求将 key 放在请求URL，并可能在响应中回显。适配器不持久化带key的URL、响应头或异常全文；响应删除 `request` 回显并替换密钥后保存。`original_raw_hash` 是原始响应字节SHA-256，`raw_hash` 是实际保留的脱敏响应SHA-256，`raw_redacted=true` 明示两者不同。不会声称脱敏文件就是未修改原文。认证代码 `API_KEY_*` 与代理拒绝分别分类。EIA v2 适配器尚未获得真实认证载荷验证，返回记录继续隔离；未来拿到key后需显式运行并审查实际schema，不能仅凭合成测试解除质量限制。

## 来源、口径与门禁

- [INE日交易快讯](https://www.ine.com.cn/data/tradedata/future/dailydata/kx20261012.dat)：核验JSON的 `report_date`、成功代码及 `sc_f`；仅接受有效YYMM逐合约，排除小计和其他品种。分别保存收盘、结算、成交量、持仓。价格标为CNY/桶，量为手，时区Asia/Shanghai；未做乘数、保证金、换月收益或主连拼接。零量保留；空价/零价保留null并标缺失，非法数值或重复合约拒绝整次解析。
- [SC规则页](https://www.ine.com.cn/products/sc/standard/)本轮HTTP200实际是WAF人机验证，不能算规则核验成功。明确保留 `current_contract_rules_unverified`；当前货币/单位映射仍需规则原文最终复核，全部SC记录不获研究资格。不绕WAF，不用HTTP Last-Modified或JSON update_date推定公布时刻。
- [EIA官方周报目录](https://www.eia.gov/petroleum/supply/weekly/)直接链接 [table4库存CSV](https://ir.eia.gov/wpsr/table4.csv)、[table1供需CSV](https://ir.eia.gov/wpsr/table1.csv)、[table2炼厂CSV](https://ir.eia.gov/wpsr/table2.csv)。只取明确的最近两周列，拒绝重复/缺失类别；不会把同一表的四周均值、同比列当当周数据。库存为周末时点，单位百万桶；产量和汽油/馏分油products supplied为周平均千桶/日；炼厂利用率为百分比。products supplied只是需求代理，不是直接终端消费。对应官方psw01/02/04工作簿在本轮也已读取核对单位；CSV库存与工作簿千桶值相差1000倍，不混用数值单位。
- EIA API目录预设8个周频seriesid：商业库存、Cushing库存、产量、炼厂利用率、汽油/馏分油库存、汽油/馏分油products supplied。严格核验周频、series、单位和日期。公开CSV与API均为EIA同源，不能算独立交叉核验。**公开文件成功不满足Issue要求的EIA v2真实认证请求验收。**
- [CME公开报价页](https://www.cmegroup.com/markets/energy/crude-oil/light-sweet-crude.quotes.html)及[ICE Brent产品页](https://www.ice.com/products/219/Brent-Crude-Futures)已实测HTTP200；没有证明合法可复用的免费期货日线API。两种期货继续 `unavailable`，不解析网页报价，不将EIA现货当期货。此结论仅指本轮尚未验证来源，不是声称全网不存在免费来源。
- 未新增未经验证的事件；报告events为空且明确缺项。既有 `methanol.industry` 官方事件结构不变。没有把外交声明当协议或履行，也没有把通航警报当完整运输量。

统计日期、统计周、公布时刻、首次看到及修订相互独立。公布时间未知为null；`first_seen` 使用完整响应完成时间，`available_at` 不早于响应完成、已知公布及重解析时间。现在抓取历史周报不回填过去可知时间。A→B→A保留三版；重复值关联新证据，不新增同版。先选as-of可见最新修订再展示；新版null不回退旧值。报告生成前核验SQLite完整性、外键、原文和尝试证据哈希。不完整/损坏证据直接失败。

所有记录保留质量标记；`parsed_facts`只表示可解析事实，**不等于研究可用、准确、实时或可预测**。研究资格计数保守固定0，EIA认证验收状态固定pending；SC规则和EIA认证schema需后续明确验证。报告按来源当地自然日显示数据年龄，SC阈值3天、周频阈值14天仅作展示预警，不保证应发布数据齐全。未接入交易所/美国假日日历：没有记录≠确认休市；日历间隔明确未分类，不填补数据。

## 2026-10-13 UTC 本环境真实验收（非合成）

首次重新请求INE于09:18:01完成，返回JSON总计74条，包含SC2611。生产适配器后续入库时间起点为09:23:42；不会用早期探索请求替代入库记录的实际first_seen。最终采集09:29:21–09:29:25期间完成：

| 来源 | 实际观测期 | 新增事实版本 | 结果 |
|---|---|---:|---|
| INE SC | 2026-10-09、10-12 | 160 | 每日20个逐合约×4字段，规则待核验 |
| EIA table4 | 2026-09-25、10-02 | 8 | 4类库存×2期 |
| EIA table1 | 同上周结束日 | 6 | 产量及两类products supplied×2期 |
| EIA table2 | 同上周结束日 | 2 | 炼厂利用率×2期 |
| EIA v2 | 无 | 0 | 未配置用户key，authentication_required |
| Brent/WTI期货 | 无 | 0 | unavailable |

最终176版本、23次正式采集尝试，预测及验证记录均0。重复采集五个请求新增0；完整备份29文件并恢复，恢复前后事实和表计数一致；`09:18:00Z`历史截止事实0。私有数据、源站原文、完整日志和备份不随公开代码发布。公开仅保留此脱敏摘要及原文哈希。

原文SHA-256：

| 响应 | SHA-256 |
|---|---|
| INE 2026-10-12 | `ab694960048145acdb21bbc594faee9a0bd41b84936b7ba306f3374a5d3c6aab` |
| EIA table4 | `d57324e8310efc9814fc3c373bdc271a2b195c36ee9bf4b052fad93b640fd439` |
| EIA table1 | `fd0c1e8d04a39e9b8bb227166087ce33b1c79f2e5e2119d1938b909503a5deec` |
| EIA table2 | `d14564c2ef9fc28fe518b0f069e5444ba631cb96d9e2a2fbe7b0411aad441d75` |
| EIA API_KEY_MISSING | `b71fb384d31b5e7ccc66d279785c9e6cd51fa51f07820a3363c849034b9608bd` |

无key的 `/v2/` 及 `/v2/petroleum/pri/spt/data/?frequency=daily&data%5B0%5D=value&length=1` 首轮09:17:58–59、复验09:27:10均403 API_KEY_MISSING，明确是EIA认证。此现货端点仅用于错误诊断，没有价格入库。早期猜测的周报旧路径404；改用官网实际CSV链接后成功；wpsr.csv/table1.pdf/table4.pdf为源站403，未绕过。

离线验证：原103项测试保留，加38项纯合成测试，共 **141项通过**。覆盖跨时区、发布时间/修订门禁、A→B→A、null修订、去重、逐合约换月隔离、缺口、WAF/异常输入、单位、周平均列、认证脱敏、失败重试、原文篡改、备份移动与恢复、CLI及MA目录不变。`check_offline.py`阻断socket；在线证据不作为CI fixture。另通过compileall、tabnanny和git diff检查。CI以草稿PR具体提交的远程检查为准。

本版有真实管道和事实报告，但**Issue #3未完全验收**：EIA真实认证请求、SC当前规则原文、Brent/WTI合法期货日线、历史发布修订档案、事件覆盖及独立核验均仍有缺项。未开发价格预测、跨品种信号、交易、部署或新提醒。
