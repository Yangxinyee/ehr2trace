# 面向患者世界模型与 agent 强化学习的数据准备：定位与优化审计

审计日期：2026-09-06。范围：当前代码、CTPE/MIMIC 全量 canonical 汇总、每套数据一个 MEDS 分片的 schema，以及已保存的完整 validation 指标。全量扫描涉及 31,752,664 和 296,595,466 条 canonical events；未导出患者标识、病历文本或个体轨迹。审计脚本与结果为 `tools/audit_world_model_readiness.py` 和 `results/world_model_readiness.json`。以下提出优化方案，本轮没有重建临床数据、修改转换规则或训练模型。

## 1. 系统定位与贡献

**ehr2trace 是面向患者世界模型和 agent 学习的、可审计的纵向患者事件数据基础层。**

主要使用场景按“患者世界模型作为环境，为临床决策 agent 提供训练/评估基础”解释；若训练的是模拟患者对话与行为的 agent，还需第 6 节的额外数据边界。患者世界模型、模拟患者角色和决策策略是不同组件，不能仅凭 MEDS 导出宣称三者已经实现。

贡献层级：

1. **患者事件数据基础设施**：统一来源、身份、时间、临床事实和来源链，支持 OMOP 检查与 MEDS 消费。
2. **可执行的转换正确性契约**：把时间、身份、来源、划分和审批假设变成可检查的条件。
3. **规模与训练准备度证据**：实际转换、故障检测、重建、时间敏感性实验，加上本轮针对世界模型/RL 的缺口审计。

[ETHOS](https://doi.org/10.1038/s41746-024-01235-0)展示了基于患者时间线的生成式预测；[EHRWorld v1](https://arxiv.org/abs/2602.03569v1)研究患者状态与动作条件下的纵向模拟。它们说明为什么需要可用的历史和动作表示，但不能替代对本系统的数据与训练效果验证。本仓库暂不声称已经提供经过验证的世界模型、反事实模拟器或强化学习策略。

## 2. 已核实的关键缺口

| 优先级 | 证据 | 对训练的影响 | 建议 |
|---|---|---|---|
| P0 | CTPE/MIMIC 分别有 **22,821 / 364,627** 条无事件时间、无可用时间的 `VITAL_STATUS`；其中字面值为 deceased 的记录分别为 **4,877 / 38,301** | 将最终生命状态作为初始患者信息可能泄漏未来结局；不能把“无时间戳”统一解释为“从一开始就知道” | 建立稳定基线属性 allowlist，生命状态按知晓时间/事件表达，未来结局仅进入目标或受控 reward 计算 |
| P0 | `as_of_view()` 允许 null availability；MEDS 保留整行 `end_time`，没有字段级可见性 | 一条就诊事件即使已开始，其后来确认的出院时间仍可能被提前暴露；事后补录、修订也不能只靠一列 available_time 处理 | 区分发生时间、记录时间、有效区间、计划值和已观察值；按字段/版本构建 as-of 视图，禁止未来字段进入 observation |
| P0 | 旧 MIMIC canonical POE 有 **52,212,109** 条 drug_order，其中只有 **23,295,778** 条 `Medications`；其余 **28,916,331** 条包括检验、影像、会诊等 | 动作空间被误标；仅采用当前 medication-only 过滤还会丢失“请求检查”的决策 | 引入检查请求、影像请求、会诊、药物医嘱等独立动作类型，保留请求与结果的联系，再重建 |
| P0 | **1,180,121** 条 ED Pyxis 记录被归为 drug_admin，全部无 encounter；官方定义为发药记录 | “发药”不等于患者实际接受治疗，也不保证每行是一项独立发药 | 单列 dispense；通过 source/encounter 键关联，只有有支持证据时才构造 administration；处理一项发药对应多个 GSN 行 |
| P0 | MIMIC **40,687,720** 条 eMAR canonical events 的 dose、route 都为空；**40,247,235** 条有 status，但 MEDS schema 不含 dose/route/status。CTPE 的 **2,416,841** 条 administration 有 dose 文本，**2,393,865** 条有 route，也没有随 MEDS 导出 | 不能可靠区分剂量、途径、执行/取消/未给等动作；原始数据存在的信息也被目标投影丢弃 | 补齐 eMAR detail 的关联；导出原始与规范化的 dose/unit/route/rate/status、动作类型及 order-to-administration 链 |
| P0 | 完整 MIMIC validation 为 **29 pass / 5 skip / 2 fail**：596,115 条未标记倒置区间，以及 AGE/GENDER 审批记录不一致 | 不能宣称当前数据满足已有契约 | 保留原时间和问题证据，重建并复验；追溯合法审批来源，不能自动补造接受决定 |
| P1 | 21 个 MIMIC configured sources 未包括 ICU `chartevents`、`inputevents`、`outputevents`、`procedureevents`、`icustays` 和 hosp `emar_detail` | 不支持完整的 ICU 生理变化与输注动作重建；“事件很多”不等于目标任务的信息齐全 | 按首个任务补齐所需模块，而非无目标扩大摄取；先确认原始文件可用性，再写 manifest/适用性报告 |
| P1 | MIMIC **73,355,880 / 157,839,647** 条 labevents 没有 encounter；ED stay 与 hospital admission 使用不同标识来源 | 不能仅依赖 encounter_id 直接组成可靠 episode；无 encounter 不代表错误或无临床意义 | 建立 patient–hospital admission–ED/ICU stay 层级与键的 namespace；无法确定归属时保留不确定性，避免按最近时间强行匹配 |
| P1 | 已保存 MEDS 检查记录：CTPE **15,351,255 / 31,638,792（48.5%）**、MIMIC **87,250,944 / 295,136,958（29.6%）** timed events 带 assumed-availability | 这些比例使用 timed MEDS events 作分母；通过当前 ordering 检查并不证明当时已经知道该事实 | 按来源与事件类型报告可见性等级，提供 strict-observed 和 assumption-permitted 两种配置并做敏感性分析 |
| P1 | 当前 MIMIC OMOP drug/measurement 非零概念覆盖率均为 0%；canonical 单位为 source unit，数值/范围/比较符的 MEDS 表达较窄 | 跨数据集状态/动作词汇和数值尺度不一致；原始代码仍可用于单数据集基线，0% 不是“所有事件不可用” | 针对目标任务统一术语和 UCUM 单位、范围/比较符与缺失类型，保留 raw + normalized 双表示和映射版本 |
| P1 | 未实现任务级 transition、reward、terminal/censoring 和可复用的 trajectory builder | 事件流不能直接作为 offline-RL transition 数据集 | 增加独立任务层，不把任务奖励写回通用 clinical event 表 |
| P2 | 每患者一个 MEDS 文件，MIMIC 共 364,673 个分片；规范化和验证含重复全表关联 | 大量小文件和重复扫描影响训练吞吐与迭代速度 | 另建训练专用 shard/index，保持患者完整与原始 event_id，可缓存同一快照的共享统计；不要通过跨患者打散来换取速度 |

Pyxis 的发药定义及一项记录可能对应多个 GSN 见[官方 Pyxis 文档](https://mimic.mit.edu/docs/iv/modules/ed/pyxis.html)。eMAR 的具体给药量、单位及未完整给药字段来自 [emar_detail](https://mimic.mit.edu/docs/iv/modules/hosp/emar_detail.html)，关联具有一对多结构，不能直接 join 后重复计算动作。[inputevents](https://mimic.mit.edu/docs/iv/modules/icu/inputevents.html)提供输注量、速率、区间及 order/linkorder 信息；[chartevents](https://mimic.mit.edu/docs/iv/modules/icu/chartevents.html)提供 ICU 观测。这里只据文档说明应补哪些来源，不声称已读入这些原始表。

另以两条完全合成的 MEDS 记录执行 `tools/probe_world_model_visibility.py`：当前 `as_of_view()` 会返回无时间的生命状态及 cutoff 之后的 visit end；只改变事后生命状态值就会改变早先 observation。结果保存在 `results/world_model_visibility_probe.json`。这是对当前 helper 的可重复行为验证，不是现实患者泄漏比例或模型性能估计。

说明：本轮新发现没有被追加成现有 36 项 validator 的“失败项”，也没有重跑 validator；它们属于独立 readiness audit。POE 当前 YAML 已改成只保留 medication，但保存的 canonical 仍包含旧分类，这正是代码状态与输出状态要分别审计的原因。各数字都是事件/记录数，不能改称独立患者数或实际治疗次数。

## 2a. 2026-09 转换修复对上述缺口的处理（2026-09-14 补记）

2026-09-13 对三个已构建数据集做了只读审计，随后制定了修复计划（`docs/CONVERSION_REMEDIATION_PLAN.md`，18 项决定见 `docs/DECISIONS.md`）。修复改动了上表几项缺口涉及的代码与配置，下表逐项标注：

- 任务编号与决定编号取自计划第 4 节与第 3.1 节，提交取自计划 9.1 节，均已合入 main。
- 三个数据集的正式重建与校验尚未完成。"已改动"只表示代码与配置已经改变，不表示已在正式构建的数据上验证关闭。
- 上表数字仍是 2026-09-06 的审计值，未改。

| 上表缺口 | 本次修复的改动 | 任务与决定 | 提交 | 状态 |
|---|---|---|---|---|
| P0 无时间的 `VITAL_STATUS` | 无 | — | — | 不在本次修复计划内 |
| P0 字段级可见性（`as_of_view()`、`end_time`） | 只有一项相关：同一结果的多个可用时间冲突时取最早，并标记 `AVAILABILITY_MERGED`。字段级可见性没有实现 | T1.3，D-R5 | `377904e` | 未关闭 |
| P0 POE 动作类型 | POE 的 `transaction_type` 映射为规范层与 MEDS 的 `action` 列。检查、影像、会诊等独立动作类型不在计划内 | T1.11、T2.M8 | `81ae532`、`1f48629` | 部分改动，待正式构建验证 |
| P0 ED Pyxis | ED 各表统一用 `stay_id` 作就诊号；`ed_pyxis` 以 `med_rn` 区分不同的发药；就诊号按声明的可关联率由 `ENCOUNTER_RESOLVES` 检查。发药与给药的区分不在计划内 | T2.M9 | `1f48629`、`4063026` | 部分改动，待正式构建验证 |
| P0 给药的剂量、途径、状态与速率 | 药物事件的身份纳入剂量（数值加单位）、途径、状态与结束时间，剂量不同的医嘱不再合并。规范层与 MEDS 新增 `rate_source`、`rate`、`rate_unit`；eMAR 的输注速率映射到 `rate`；OMOP 的 `drug_exposure.sig` 写入速率文本。OMOP 剂量单位缺失时回退到事件自身的单位。没有药名的 eMAR 与药房行，按 `pharmacy_id` 从处方取药名，并标记 `NAME_FROM_LINKED_ORDER` | T1.1、T1.4、T1.11、T2.M8、T2.M13，D-R15、D-R18 | `81ae532`、`377904e`、`4820be6`、`1f48629` | 已改动，待正式构建验证 |
| P0 MIMIC 校验 2 项失败 | 无。审计时（计划 0.1 节）MIMIC 校验已是 35 通过 / 5 跳过 / 0 失败 | — | — | 本次修复之前已不存在 |
| P1 ICU 模块未读 | MIMIC-IV 配置扩到 33 个 source：`icustays` 作为 `visit_detail`；`inputevents`、`ingredientevents` 作为带速率的给药；`chartevents`、`outputevents` 作为测量；`procedureevents` 作为操作；`datetimeevents` 作为 observation；`d_items` 用于查表；`caregiver` 声明为不纳入 | T4.1–T4.4，D-R13 | `1f48629` | 已改动，待 MIMIC-IV 正式构建验证 |
| P1 就诊层级与键 | 新增 `visit_detail` 事件与 OMOP `VISIT_DETAIL` 表。转科、换服务与 ICU 住院（MIMIC-IV、JHU ADT、CU）挂在所属就诊下；找不到所属就诊的保留在规范层与 MEDS，不发布到 OMOP，并记 `VISIT_DETAIL_UNPARENTED`。transfers 的 discharge 行不再发布为就诊，ED 就诊类型固定为急诊。各来源的就诊号按声明的可关联率由 `ENCOUNTER_RESOLVES` 检查。检验没有就诊号是源数据的性质，未改 | T1.13、T2.CU5、T2.J7、T2.M5、T2.M6、T2.M9，D-R8、D-R9、D-R14 | `4820be6`、`e8b2a70`、`3042dde`、`f2e810a`、`1f48629`、`4063026` | 已改动，待正式构建验证。CU 的 ICU 住院在 OMOP 中是否改走 `visit_occurrence`，待 Xinye 确认（计划 9.3） |
| P1 可用时间为假设值的比例 | 无。只有分诊测量以到院时间作回退时，标记 `TIME_FALLBACK` | T2.M3，D-R7 | `1f48629` | 未关闭（计划 2.6 节列为确认不修，W5） |
| P1 单位与数值的表达 | OMOP 的 `unit_concept_id` 按单位表与词表中的 UCUM 概念查得。规范层与 MEDS 新增规范化数值与单位，只做精确换算。按源代码声明或覆盖单位，并标记 `UNIT_DECLARED` 或 `UNIT_OVERRIDDEN`。超出合理范围的标记 `IMPLAUSIBLE`，不进规范化列。量纲无法精确换算的按单位拆码。JHU 化验参考范围的上下限先在配置中映射，但转换器从不读取这两个角色：修复前，JHU 的 OMOP 测量 15,911,634 条没有一条带参考范围（计划 9.3）。规范层 schema 3 起新增 `range_low`、`range_high` 读取这两个角色；OMOP 测量优先取行内的参考范围，源数据没有报告时才取配置的范围。比较符与缺失类型未改；药物与测量的概念覆盖率不在计划内 | T1.5–T1.7、T2.J2、T2.J3、T2.J6、T2.M7、T2.M10，D-R1、D-R10、D-R17 | `377904e`、`4820be6`、`f1569b6`、`c60c4d1`、`d734ae2`、`f2e810a`、`2e472bd`、`eccaeed`、`1f48629` | 已改动，待正式构建验证 |
| P1 任务层 | 无 | — | — | 不在本次修复计划内 |
| P2 MEDS 分片 | 无 | — | — | 不在本次修复计划内 |

审计还发现了上表之外、与轨迹数据直接相关的问题。修复的改动如下，状态同样是"已改动，待正式构建验证"：

| 问题 | 改动 | 任务与决定 | 提交 |
|---|---|---|---|
| MIMIC-IV 有 11,402 人的两条死亡记录被当作冲突，没有 DEATH 行 | 按数据集时区比较日期。同一天的合并为一条，取有时刻的记录，并标记 `DEATH_TIME_MERGED`；跨日的两条都保留，标记 `DEATH_DATE_CONFLICT`，不发布到 DEATH | T1.8，D-R11 | `377904e`、`4820be6` |
| 剂量、状态不同的医嘱被合并为一个事件（CU 药物 1,118,171 行） | 见上表"给药的剂量、途径、状态与速率"一行。没有声明规则的字段冲突标记 `MERGE_CONFLICT`，并使 `DUPLICATES_AGREE` 失败 | T1.1、T1.2 | `377904e` |
| MIMIC-IV 的 ED 生命体征 1,564,610 行全部被隔离；分诊值因为没有时间被隔离 | 准备脚本把宽表转为每个值一行，清单记 `rows_added_by_split`。分诊以到院时间作回退，并标记 `TIME_FALLBACK` | T2.M2、T2.M3，D-R7、D-R17 | `1f48629` |
| JHU 已删除的问题列表条目被发布为诊断 | `excluded_status` 去掉 `Deleted`，由 `EXCLUDED_STATUS_NOT_PUBLISHED` 检查 | T2.J4，D-R6 | `f2e810a`、`4063026` |
| JHU 随访未读，缺少删失终点 | 读入最近一次随访，作为 observation 事件 | T2.J7，D-R12 | `f2e810a` |
| CU 同一篇笔记挂在多个就诊号上 | 合并为一条，按 `prefer_linked` 规则决定保留哪个就诊号 | T1.2、T2.CU3，D-R2 | `377904e`、`3042dde` |

检查现在共 55 项，其中 15 项是审计之后为上述问题新增的；故障目录新增 9 个故障，每个都复现审计实际发现的产出状态（`4063026`，见 `docs/FAULT_CATALOGUE.md`）。第 2 节"说明"中的 36 项，是 2026-09-06 时的检查数。

## 3. 建议新增的任务层

建立独立的、版本化的 `task_spec -> episodes -> observations/actions -> transitions` 数据管线。继续保留通用 canonical 与 OMOP/MEDS，让不同研究任务共享事实层。

| 对象 | 必需语义 |
|---|---|
| episode | patient / encounter namespace，起点、结束条件、可观察窗口、纳排规则、删失原因、切分与版本 |
| observation/history | 截止决策时真实可用的内容；稳定基线 allowlist；动态值的观测掩码、测量时间、距上次观测间隔、信息缺失类型 |
| action | 请求、处方、发药、实际执行、暂停/撤销等区别；药物剂量/速率/单位/途径；关联 order_id；执行状态及其知晓时间；动作缺失不等于 no-op |
| transition | `(o_t, a_t, delta_t, o_next, reward, terminated, truncated)`，所有组成部分的来源 event_ids；输入与未来监督严格分开 |
| reward/outcome | 任务定义、计算版本、结局可观察性、延迟、删失/失访；奖励读取未来结局时只能进入 reward/target，不得进入当时的 observation |
| split/preprocessing | 患者级独立，轨迹不跨 split；归一化、分箱、词表、检索索引及缺失处理只在训练集合拟合 |
| manifest | 来源快照与 artifact hashes、转换/任务配置、词表、代码、split、reward、feature 版本 |

这是部分可观测的序列决策数据。`o_t` 是观测而不是完整真实患者状态；世界模型可以学习 `p(o_next, delta_t | H_t, a_t)`。从观察性数据学到的条件分布不自动等于 `do(a_t)` 下的干预效果。临床混杂、动作支持范围和离线策略评估限制需要单独处理，见 [Gottesman et al., 2019](https://doi.org/10.1038/s41591-018-0310-5)。高预测准确率、较高离线估计奖励和临床策略改善是不同的证据层级。

## 4. 契约应补哪些测试

优先增加有语义依据、能实际检出错误的测试，而不是为每列写一个不为空断言：

- **未来信息隔离**：改变 cutoff 之后的死亡/出院/记录修订，cutoff 之前的 observation 必须不变；final vital status 不得出现在未知时点的 baseline。
- **动作真实性**：dispense 不得无证据转成 administered；取消/未给记录不得进入“实际给药”动作；补录时间和实际执行时间保持分离。
- **剂量可追溯与守恒**：eMAR detail 一对多链接和输注 rate 变更不能因 join 或重复源记录放大剂量；无法解析时保留 raw 和 mask，不能变成 0。
- **episode 与边界**：patient/encounter 归属一致，任务终止与删失分开；禁止训练窗口跨患者、跨 split 或覆盖不可见的未来。
- **奖励隔离**：奖励计算可以依赖未来 target，但 observation 编译路径不能读取 reward/outcome 字段。
- **同刻事件与缺失**：hash 排序只保证可复现，不证明临床先后；明确时间分辨率、同刻事件组、先后未知和未观测标记。
- **数值与映射**：同一概念不同单位换算后语义一致；单位/范围/比较符不得静默丢弃；映射覆盖变化和动作词汇漂移需要按 task 监测。

MIMIC 的时间外验证不能简单排序不同患者的去标识化绝对年份；需使用其去标识化时间机制允许的 cohort-era 信息或独立外部数据，并保留无法精确排序的限制，参见[patients 文档](https://mimic.mit.edu/docs/iv/modules/hosp/patients.html)。

## 5. 最小可交付路径与论文下一步实验

**第一阶段：可信数据视图。** 先解决未来信息和 action 分类，补齐必要给药字段，处理已有两项 validation 失败。验收依据是新增语义故障被检出、当前快照复验记录齐全，以及原始事实/来源没有丢失。准备一套稳定 baseline allowlist 和 action ontology。

**第二阶段：一个任务的端到端轨迹。** 先在住院期间选择有实际给药和后续观测支持的任务，固定时间粒度或事件驱动步长、episode、终止/删失与结局。优先交付 logged action-conditioned forecasting 轨迹和清晰基线。若目标是 ICU 液体/升压药决策，应先补 ICU 模块；当前 hosp/ED/notes 的规模不能代替这些来源。

**第三阶段：世界模型与 agent 证据。** 使用完全相同的 patient split 比较：原始宽松时间策略、严格可见性、加入完整动作信息。保存单步预测与多步 rollout、时距误差、连续值误差/校准、事件概率与不确定性、物理/临床约束违例。患者级 bootstrap，按来源/缺失程度/episode 长度分层；避免只报告一种 AUROC。

**第四阶段：RL。** 在任务定义和数据支持范围确定后，先建立记录策略/行为模仿基线，再评估离线策略或模拟环境中的训练。记录 action support、策略分歧、有效样本规模、估计方差及删失。不能用同一世界模型给自身训练出的策略打分，再把这种自洽性当作治疗效果证据。

对目前 arXiv 版最有价值的新增实验，是“同一患者切分下，严格时间与动作表示能否改善可解释的下一步/多步预测与语义错误率”。这比增加一个与主线关系较弱的通用 LLM 概念匹配实验更能支撑新定位。本轮论文明确把这些列为待验证工作，没有补造结果。

## 6. 如果目标包括对话型患者 agent

还需一个与轨迹层分离的交互层：患者已知信息、医生可见信息、模拟器隐藏状态、询问/检查动作、工具响应、对话回合、成本及评价规则。参考 [AgentClinic](https://arxiv.org/abs/2405.07960) 的交互式任务定位，但当前 EHR notes 不是逐轮患者对话，也不能直接充当真实交互轨迹。

出院总结可用于构造后验目标或受控隐藏信息；不能把其中全部诊断和最终结局复制到 admission-time profile，再将任务称为前瞻性问诊。由 LLM 生成的对话、偏好或依从性标签必须单独标记为合成/推断数据，并保留句子或事件级证据；不能标成观察到的患者行为。若用于强化学习，应把奖励、病例答案和隐藏结局与 agent 输入隔离。

## 7. 本轮论文修改边界

标题改为 **ehr2trace: Auditable EHR Data Preparation for Patient World Models and Agent Learning**。摘要、引言、贡献、相关工作、系统边界和讨论围绕上游数据准备展开。增加拟议的 transition 接口和实测 readiness 缺口；draw.io 架构图将未实现的任务层与学习系统用虚线明确区分。保留原始转换/验证/历史实验数值与局限。未将规模数字改称 RL transition 数或完整可用 episode 数，也未声称新世界模型/RL 实验已经完成。
