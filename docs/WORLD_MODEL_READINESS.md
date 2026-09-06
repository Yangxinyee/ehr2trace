# 面向患者世界模型与 agent 强化学习的数据准备：定位与优化审计

审计日期：2026-09-06。范围：当前代码、CTPE/MIMIC 全量 canonical 汇总、每套数据一个 MEDS 分片的 schema，以及已保存的完整 validation 指标。全量扫描涉及 31,752,664 和 296,595,466 条 canonical events；未导出患者标识、病历文本或个体轨迹。审计脚本与结果为 `tools/audit_world_model_readiness.py` 和 `results/world_model_readiness.json`。以下提出优化方案，本轮没有重建临床数据、修改转换规则或训练模型。

## 1. 系统定位与贡献

**ehr2cdm 是面向患者世界模型和 agent 学习的、可审计的纵向患者事件数据基础层。**

主要使用场景按“患者世界模型作为环境，为临床决策 agent 提供训练/评估基础”解释；若训练的是模拟患者对话与行为的 agent，还需第 6 节的额外数据边界。患者世界模型、模拟患者角色和决策策略是不同组件，不能仅凭 MEDS 导出宣称三者已经实现。

贡献层级：

1. **患者事件数据基础设施**：统一来源、身份、时间、临床事实和来源链，支持 OMOP 检查与 MEDS 消费。
2. **可执行的转换正确性契约**：把时间、身份、来源、划分和审批假设变成可检查的条件。
3. **规模与训练准备度证据**：实际转换、故障检测、重建、时间敏感性实验，加上本轮针对世界模型/RL 的缺口审计。

[ETHOS](https://doi.org/10.1038/s41746-024-01235-0)展示了基于患者时间线的生成式预测；[EHRWorld v1](https://arxiv.org/abs/2602.03569v1)研究患者状态与动作条件下的纵向模拟。它们说明为什么需要可用的历史和动作表示，但不能替代对本系统的数据与训练效果验证。本论文暂不声称已经提供经过验证的世界模型、反事实模拟器或强化学习策略。

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

标题改为 **ehr2cdm: Auditable EHR Data Preparation for Patient World Models and Agent Learning**。摘要、引言、贡献、相关工作、系统边界和讨论围绕上游数据准备展开。增加拟议的 transition 接口和实测 readiness 缺口；draw.io 架构图将未实现的任务层与学习系统用虚线明确区分。保留原始转换/验证/历史实验数值与局限。未将规模数字改称 RL transition 数或完整可用 episode 数，也未声称新世界模型/RL 实验已经完成。
