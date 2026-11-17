# 数据结构
本小型项目首次启动使用 SQLAlchemy metadata 建立 users/nodes/scenarios/drills/audits 五张业务表，并新增 configurations/configuration_members 两表保存成员组态代际。自然键 users.email、nodes.name、scenarios.name 唯一，configurations.generation 唯一。

## 组态表
- `configurations`：每次换组一行。`status` 为 preparing/active/superseded；`generation` 只在生效时分配（准备中为 NULL），因此同次重试不会消耗代际；`preparing_slot` 唯一且仅准备中行为 1，数据库层保证同时最多一个准备中变更；`request_id` 唯一，同一次发起可安全重试；`target_log_position`/`target_primary_id` 冻结发起时原主日志与身份，作为追平门槛。
- `configuration_members`：`(config_id, node_id)` 唯一，保存准备时的成员名及生效时日志位置；追平依据是节点当前 `log_position` 与 `target_log_position` 的比较，不是巡检时的手工声明。

`nodes.vote_enabled` 是生效组态的投影，由组态生效事务统一写入；节点巡检接口不得直接翻转它。旧库首次升级时 `scripts/seed_demo.py::bootstrap_configuration` 把既有投票节点收敛为第 1 代生效组态，此后所有变更走 prepare/activate 流程。

drills.scenario_id 为外键，快照在入队时固定，且同时冻结 `nodes`、`scenario` 与完整 `configuration`（生效组、准备中组、冻结时间）。上线结构演进须补充版本化迁移，本项目不支持对既有不同结构数据库自动升级。
