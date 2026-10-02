# 处理授权终止后的权利回转基础平台

本项目是一套可离线运行的 Python 服务端平台，供创新药企业的研发管理、转化医学、商务拓展和基金运营团队管理候选药实验记录、研发证据、协作中心资源、尽调交接通道、交易风险告警与跟进任务。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

`reversion_ops` 子系统专门处理对外授权在首付款之后因战略调整或临床结果变化而终止的场景：从终止通知、争议期、资产清单、数据/样本交付、再许可影响到最终关闭，形成一条只追加、不可覆盖的哈希链时间线，并在查询中清楚展示当前可支配权利、遗留义务与未完成交接。

## 目录

- src/portfolio_ops/：研发中心、尽调通道、研究资源、交接计划和商业情景；
- src/discovery_lab/：研究协议、实验记录、异常排除、分析任务租约和候选结论；
- src/licensing_ops/：管线信息、交易风险告警、跟进工单和资源分配；
- src/reversion_ops/：交易终止通知、争议期、权利回转资产清单、数据/样本交付、再许可处置、前置动作门控、最终关闭与恢复自研/重新授权放行；
- fixtures/：离线验收使用的研究协议与结构化实验记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.acceptance --workspace .
PYTHONPATH=src python3 -m discovery_lab.acceptance --workspace .
PYTHONPATH=src python3 -m licensing_ops.acceptance
PYTHONPATH=src python3 -m reversion_ops.acceptance --workspace .
~~~

四条命令会在临时 SQLite 数据库中完成研发中心与交接通道登记、研究资源分配、候选药证据分析、交易风险处置，以及一次完整的交易终止与权利回转，不访问外部网络。

## 交易终止与权利回转流程（reversion_ops）

终止案例状态沿下列主线单向推进，每个动作都追加一条哈希链事件，事件只增不改：

1. **终止通知**：登记原协议（revision_seq=0）及历次修订（amendment），交易采纳到当前修订后发出终止通知。同一 `notice_key` 重复通知返回 `duplicate=true`，**不会产生第二次回转**；同键不同内容报冲突。
2. **争议期**：通知后进入争议窗口。窗口内提出争议则案例挂起，只把本次终止波及（affected）的再许可/共同开发/第三方承诺置为暂停，**不受影响的合作事项继续执行**；争议解决后回到通知状态，窗口届满且无未决争议才能进入收尾。
3. **资产清单**：逐项登记地区权利、数据、样本、物料、知识产权与后续义务，每项**必须引用原协议或某次修订的具体条款**（协议号 + 修订序号 + 条款），受影响项须确认后才能冻结回转范围。
4. **数据/样本交付**：按资产项提交带 SHA-256 清单的交付批次，由接收方接收或拒收；拒收可补齐重交。争议期间受影响交付暂停。
5. **再许可影响**：受影响的再许可可终止或随附存续义务；不受影响的共同开发/承诺禁止被处置。
6. **前置动作与最终关闭**：关闭前校验所有受影响资产到达终局、无在途交接、受影响再许可已处置、close 类前置动作全部满足；不满足时案例落为 `preclose_blocked` 并保留受阻时间线。**关闭决定一旦形成不可重开**；部分终止只回转受影响地区（交易保持 active），全部终止回转所有地区（交易 closed）。
7. **迟到材料**：关闭后才送达的材料只能登记归档（`reopened=false`），**不能重开已关闭决定**，也不能再补办资产或交付。
8. **恢复自研 / 重新授权**：必须在终止关闭、且 close 与对应类型（resume_dev/relicense）前置动作全部满足后才放行；权利未回转的地区不放行。

查询接口 `GET /deals/{id}/rights` 返回当前可支配权利（已回转地区）、遗留义务（托管/豁免资产、存续承诺）与未完成交接（在途/被拒交付、未结前置动作）；`GET /terminations/{id}` 返回含协议依据、资产、交付、前置动作和完整时间线的总览；`GET /timeline/verify` 校验哈希链完整性。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.api --database portfolio.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m discovery_lab.api --database discovery.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m licensing_ops.api --database licensing.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m reversion_ops.api --database reversion.sqlite3 --host 127.0.0.1 --port 8083
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。
