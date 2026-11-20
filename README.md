# 受限混配方案与实际领料质量核账

纯 Python 3.11 标准库实现（SQLite 持久化 + `fractions.Fraction` 精确有理数 +
自研精确分支定界整数搜索），无第三方依赖。所有数据均为合成，不对应任何真实
食品或药品标准。

## 目录结构

```
blend/
  model.py      不可变数据模型、精确有理数、统一异常
  catalog.py    独立原料质量目录（SQLite，原子修订，质量边界产品，
                受控成分双实验室证据化发布与防绕开关卡）
  blinding.py   盲样比对服务（轮次盲码、开放/更正窗口、冻结关闭、
                逐人中位数资格、版本化重评）
  planner.py    整数混配规划器（精确分支定界）+ 独立全枚举核对器
  warehouse.py  履约仓（预览/确认事务锁、领料/退回/取消状态机、台账）
tests/          46 个 unittest 用例（含 300 例随机实例与全枚举一致性核对）
demo.py         九个端到端场景演示
```

## 运行

```bash
python3 -m unittest discover -s tests -v   # 全量测试
python3 demo.py                             # 端到端演示
```

## 模块入口

### 1. 质量目录 `blend.catalog.QualityCatalog`

- `replace_lots(lots, note)` —— 一次整体修订（区间与可用量同事务生效），
  失败整体回滚、修订号不动；历史修订永久保留，可按 rev 读取。
- `product(usage, rev=None)` —— 独立计算任意用量组合的每成分质量和上下界
  与分数上下界，产出 `UsageProduct`（不感知订单、不处理优先级）。
- `get_lots / get_lot / components / revisions` —— 修订与 lot 查询。
- 区间语义：同批每克的同一未知成分分数在 `[lo,hi]` 内任取，成分间不声明
  相关性；质量和界为各 lot 贡献区间的线性求和（独立区间算术）。

#### 受控成分与外部实验室证据链

部分合成成分改由外部实验室报告支撑，目录方在开通该来源前先核对实验室
当轮比对能力，再决定两份报告能否共同支持本批发布：

- `enable_controlled_component(component)` —— 成分受控后，`replace_lots`
  手工入口既不能改写其区间、也不能把它丢出词表（防绕关）；不触碰受控
  成分的手工维护与全部历史修订/旧产品继续可读。
- `register_lab_report(...)` —— 登记外部实验室闭区间报告（端点仍是
  `[0,1]` 原料质量分数语义），登记本身不改变目录。
- `publish_external_component(lot_id, component, governance, report_ids,
  pt_version, ...)` —— 单事务证据化发布，任一条件不满足整次修订失败、
  修订号不动：
  1. 两份报告指向相同实物样本、相同 lot 与成分；
  2. 两位签发实验室在指定**当前关闭版本**均获资格（旧版本结论、开放期
     试报不采信），且分属发布当时登记的不同运营机构；
  3. 两闭区间有交集，采用精确 `Fraction` 交集。
- 发布时把报告、机构归属与盲样资格整体冻结为 `PublicationEvidence`
  （`get_publication / get_publication_by_rev`）；后续机构归并或比对重评
  不改写已发布产品，新发布按新事实重判。

### 1b. 盲样比对服务 `blend.blinding.BlindingService`

- `register_organization / register_lab / merge_organizations` —— 机构
  与具名实验室档案；归并只改当前归属，不动历史证据快照。
- `create_round(round_id, component, tolerance)` 与
  `issue_blind_codes(round_id, lab_ids)` —— 按轮次+成分向不同实验室发放
  同一合成试样的独立盲码（`secrets` 随机、全局唯一）。
- 开放期只凭本实验室盲码 `submit_reading`（任意精确有理数，能力试验值
  不受 `[0,1]` 约束）与查 `receipts`；重复提交即更正，旧行标记 superseded
  且不删除；盲码视图只回本实验室回执，不泄漏其他读数或盲码映射。
- `close_round` 冻结各实验室最后有效读数并逐人评定：排除被评者，用其他
  **至少三家**实验室的中位数作参照（偶数家取中间两数的精确平均），
  绝对差不大于轮次容差才 `QUALIFIED`；不足三家或无有效读数标
  `UNRATABLE`。结果具名，含盲码、来源读数编号、参照值与差值。
- `reopen_round` 后正式更正并再次 `close_round`：旧关闭版本永久保留，
  新版本经 `supersedes_version` 接续；开放中的试报不构成资格
  （`is_qualified` 只认已关闭版本）。全部状态落 SQLite，重开持久化。

### 2. 规划器 `blend.planner.Planner`

```python
Planner(order, lots).solve()          # 无解抛 InfeasibleError
Planner(order, lots).solve_or_none()  # 无解返回 None
brute_force_best(order, lots)         # 独立第二条实现：笛卡尔积全枚举
```

- 决策变量本身就是整数克数（0 或步长倍数，截断到库存/M），**不做连续比例
  再舍入**；
- 稳健质量语义：所有 Fraction 端点统一放大成整数，稳健合格落地为整系数
  线性不等式 `Σu·lo ≥ L·M` 且 `Σu·hi ≤ H·M`，对目录允许的全部取值恒成立；
- 三级字典序目标：成本最小 → 正用量 lot 数最少 → 排序 lot_id 用量向量
  字典序最小（元组比较天然保证）；
- 剪枝：子集容量/步长 gcd/质量包络预筛 + DFS 后缀容量、gcd、质量可达包络、
  线性成本下界剪枝；规模上限（12 批、M≤300、步长 1..20、4 成分）下秒级完成。
- `evaluate_usage(lots, usage)` 不依赖目录即可算质量产品，仓库可消费外部产品。

### 3. 履约仓 `blend.warehouse.Warehouse`

| 入口 | 语义 |
| --- | --- |
| `register_lots` | 登记物料档案与初始库存 |
| `plan_order / preview_order` | 规划并保存预览，绑定目录 rev，**不占料** |
| `preview_external` | 消费外部 `PlanResult/UsageProduct`，仓库不重新解释质量 |
| `confirm_preview` | 单 `BEGIN IMMEDIATE` 事务：版本核对 → 库存核对 → 占料落账 |
| `issue` | 实际领料（PENDING → 已领），写 ISSUE 台账 |
| `return_material` | 已领份额登记退回入库，ISSUE/RETURN 双留痕 |
| `cancel_order` | 只释放尚未领出的份额；已领部分不复活 |
| `get_order / explain_order / inventory` | 查询与解释入口 |

- **并发争料**：确认在写锁事务内逐 lot 复核 `initial - allocated - issued
  + returned`，两个独立连接不可能双花同一份库存，失败方收到
  `ConflictError`（或锁等待超时）。
- **质量换代**：确认时若目录 rev 与预览绑定值不一致，事务整体回滚并抛
  `StalePreviewError`，预览标记 `REJECTED_STALE`，绝不把旧方案套新质量；
  已确认订单永久保存确认时的质量边界快照与来源 rev，旧确认结果保持来源。
- **状态机**：`PENDING → PARTIAL_ISSUED → ISSUED`；取消置 `RELEASED`；
  退回置 `PARTIAL_RETURNED/RETURNED`。文件库重开后全部状态完整保持。

## 演示场景（`demo.py`）

1. 目录独立质量产品与精确分数边界；
2. 中点可行而稳健不行（便宜宽区间料被规划器拒绝）；
3. 固定费改变最优方案；
4. 包装步长约束与步长 gcd 不可达的 INFEASIBLE；
5. 预览不占料 → 确认 → 部分领料 → 取消只放未领 → 退回留痕；
6. 两个独立线程/连接抢最后 10g 库存，恰好一个成功、审计无双花；
7. 质量换代：旧预览拒绝、按新质量重新规划、旧确认保持 rev1 来源；
8. 12 批/M=300/4 成分最大规模性能，以及小库存分支定界 vs 全枚举最优性核对；
9. 盲样比对：冻结盲码、中位数资格与正式更正重评的资格变化、双报告精确
   交集发布、同机构拒绝、原目录产品复读与文件库重开持久化。

## 关键设计取舍

- **精确算术优先**：端点用 `Fraction`，搜索前按所有分母 LCM 放大为整数，
  全链路无浮点；压线可行/不可行判定可复现。
- **规划与目录解耦**：规划器只吃 `Lot` 列表，`evaluate_usage` 可对任意外部
  lot 集出产品；仓库通过 `preview_external` 消费外部质量产品。
- **SQLite 即锁即核**：不另建锁服务，`BEGIN IMMEDIATE` 拿库级写锁后在事务内
  读最新库存行复核，配合 `busy_timeout` 串行化确认。
- **台账不可变**：领料与退回各立 ledger 行，库存用
  `initial - allocated - issued + returned` 守恒口径推导，取消不抹历史。
- **证据即快照**：外部受控发布在同一事务里把报告、机构归属与盲样资格
  冻结进目录库，发布后比对服务侧的机构归并/重评只影响新发布；旧版本
  结论与开放试报永远不能回写成已发布产品的资格。
- **能力值与质量分数分域**：盲样读数是自定的无量纲能力试验值（任意
  有理数，不做 `[0,1]` 校验）；实验室报告与目录区间仍严格遵循
  `[0,1]` 的原料质量分数语义，两条算术链互不污染。
- **剪枝只剪不可行支**：后缀质量包络是可达集的外包络，可能保守但绝不会
  剪掉可行解；正确性另由 `brute_force_best` 的 300 例随机对拍兜底。
