# a-stock-data-quant 整合说明

上游：https://github.com/jangviktor-web/a-stock-data-quant

固定提交：`0944c726f19aa041ecf2d93ac7357223d061823d`。上游 MIT 许可证、原文件与 SHA-256 清单保存在 `methanol/vendor/a_stock_data_quant/`。原文件保持不变；适配逻辑位于 `methanol/markets.py` 和 `methanol/derivatives.py`。

## 整合范围

| 上游内容 | 本项目接入 | 修改与边界 |
|---|---|---|
| `bin/cn/futures.py` | 18品种目录、期货报价 | 引用原目录，改用HTTPS；`zjsj`按昨结算命名；空报价/部分失败返回非零退出码；保留原响应和采集时间 |
| `bin/cn/options.py` | ETF/股指到期月、期权链、价值分析原表 | 引用原分派函数；严格校验到期月份；空数据/上游错误明确失败；原`pcr`实际未计算PCR，改称`values` |
| `scripts/quant/volatility.py` | 年化HV、滚动HV百分位 | 将样本标准差及平均秩公式改编为标准库实现；全2,857根行情与原pandas实现一致；不引入低波做多/高波做空假设 |
| 商品期权扩展 | `options expiries MA`、`options chain MA` | 上游没有MA期权；本项目新增AKShare/Sina适配，统一call/put字段并计算完整持仓量PCR |
| 期权定价研究 | `options price` | 本项目新增Black-76欧式近似及Delta/Gamma/Vega；不是实际成交报价，不含提前行权 |

没有导入上游A股回测、股票专用策略、账户配置、密钥配置和消息推送。上游的ETF期权知识说明不能代替交易所实际合约乘数、行权规则和手续费。整合行情研究模块不等于接入交易账户，也不代表提高了甲醇方向预测准确率。

## 使用

核心研究、目录、离线波动率和理论定价无需额外依赖：

```bash
python -m methanol futures list
python -m methanol options underlyings
python -m methanol futures quote MA,TA --output data/snapshots/futures.json
python -m methanol volatility --output reports/volatility.json
python -m methanol options price --futures 2500 --strike 2500 --days 30 --rate .02 --sigma .3 --kind call
```

在线期权适配使用可选隔离环境：

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-options-lock.txt
.venv/bin/python -m pip check
.venv/bin/python -m methanol options expiries MA --output data/snapshots/ma-expiries.json
# 用上一步实际返回的合约代码替换 MA701，不猜测三位编码所对应的年份
.venv/bin/python -m methanol options chain MA --expiry MA701 --output data/snapshots/ma-chain.json
.venv/bin/python -m methanol options expiries 510050
.venv/bin/python -m methanol options chain IO --expiry 202701
.venv/bin/python -m methanol options values 510050
```

`requirements-options.txt` 固定AKShare主版本；`requirements-options-lock.txt` 固定本次Python 3.12/Linux安装验证的传递依赖版本，换平台需要另行验证。没有安装上游全部A股依赖（如mootdx）。在线AKShare请求可能耗时，必要时使用 `timeout 45 .venv/bin/python -m methanol options ...` 限制单次验证。

来源记录中的采集时间不是行情发生时间。期货报价中 `provider_time` 可能为空，不能据此判定实时性。甲醇期权链中的 `open_interest_pcr` 只按可用且完整的call/put持仓量计算；缺少任一侧、持仓量缺失或call总量为零时返回null，不以0填补。没有成交量PCR或隐含波动率预测。链中合约编码、行权价或数据结构不符合预期时明确报错。

新采集的快照未拼入历史训练集：没有可靠的历史发布时间与当时可用性信息，不能将今天的期权链当成过去的预测因子。Black-76输出按单位标的计算，不乘合约乘数；MA期权可能存在提前行权影响，不能把欧式近似当作精确估值。

## 验证状态（2026-10-13）

- 默认Python与可选`.venv`中各45项测试通过，覆盖原研究功能以及新增分派、许可证文件哈希、报价字段、HTTPS、部分失败、期权链/PCR、定价平价关系和Greeks有限差分。
- AKShare 1.19.1 已安装，所有调用函数存在；锁定依赖重复安装及`pip check`通过。
- 2,857根真实MA0历史上的离线HV与上游pandas公式一致。
- 东方财富期货报价、Sina甲醇期权到期合约查询实际请求均返回代理403；新增在线接口**未验证成功**，ETF/股指在线查询尚未验证。没有生成替代行情。
- 已在云环境草稿新增 `futsseapi.eastmoney.com`、`stock.finance.sina.com.cn`、`hq.sinajs.cn`、`query.sse.com.cn`、`www.sse.com.cn`、`www.szse.cn`、`push2.eastmoney.com`，保留原有域名。请在环境设置审核、保存并发布，再重试相应在线命令。草稿保存不等于当前实例访问权限已生效。


## 后续甲醇重点验证更新

2026-10-13后续重查已获取甲醇主连报价、期权合约列表及ma2701链（82个期权合约），原403记录属于早先验证状态。新增指标、多策略比较、仓单及宏观/基差入口，详见 [甲醇研究指南](methanol-research.md)。当前57项测试通过；新增宏观/现货域名仍被代理403阻塞，不能泛化为全部在线接口已经可用。
