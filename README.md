# 处理授权终止后的权利回转基础平台

本项目是一套可离线运行的 Python 服务端平台，供创新药企业的研发管理、转化医学、商务拓展和基金运营团队管理候选药实验记录、研发证据、协作中心资源、尽调交接通道、交易风险告警与跟进任务。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/portfolio_ops/：研发中心、尽调通道、研究资源、交接计划和商业情景；
- src/discovery_lab/：研究协议、实验记录、异常排除、分析任务租约和候选结论；
- src/licensing_ops/：管线信息、交易风险告警、跟进工单和资源分配；其中 termination*.py 构成**交易终止与权利回转流程**（原协议/修订登记、终止通知与争议期、回转资产清单、数据/样本/权利交付、再许可与共同开发部分暂停、最终关闭、迟到材料留痕、恢复自研/重新授权放行）；
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
PYTHONPATH=src python3 -m licensing_ops.termination_acceptance
~~~

四条命令会在临时 SQLite 数据库中完成研发中心与交接通道登记、研究资源分配、候选药证据分析、交易风险处置，以及交易终止后权利回转的完整时间线（通知去重、争议解决、范围定稿、部分暂停、交付确认、关闭、迟到材料不重开、放行），不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m portfolio_ops.api --database portfolio.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m discovery_lab.api --database discovery.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m licensing_ops.api --database licensing.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m licensing_ops.termination_api --database termination.sqlite3 --host 127.0.0.1 --port 8083
~~~

终止回转服务（端口 8083）预置账号：法务 `legal` / `legal-pass-2026`、研发 `rd` / `rd-pass-2026`、商务 `bd` / `bd-pass-2026`、审计 `auditor` / `auditor-2026`。

终止回转流程的关键接口：`POST /agreements` 与 `.../amendments` 登记原协议及历次修订；`POST /termination_cases` 开启终止案（同通知编号或同协议未关闭案件幂等去重）；`POST /cases/{id}/disputes`、`.../resolve` 处理争议；`POST /cases/{id}/scope_items` 与 `.../finalize_scope` 建立并定稿回转范围（每项必须引用原协议 seq=0 或已登记修订序号）；`.../items/.../start_handover`、`complete_handover` 登记数据/样本/权利交付凭证；`.../suspend_part` 与 `.../resolve_collaboration` 处理共同开发/第三方承诺的**部分暂停**与最终出路；`.../resolve_sublicense` 处置再许可；`POST /cases/{id}/close` 最终关闭；`POST /cases/{id}/late_materials` 对关闭后到达的材料只登记留痕；`POST /cases/{id}/clearance` 在全部前置动作完成后放行恢复自研或重新授权；`GET /cases/{id}/timeline` 与 `.../rights_position` 分别返回不可覆盖的 append-only 时间线与当前可支配权利/遗留义务/未完成交接。

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。
