# 转换问题修复计划：CU-CTPA、JHU CTPE、MIMIC-IV

- **版本 2**（2026-09-13）：在版本 1 基础上纳入第二轮证据，以及研究团队（Xinye）当天拍板的 18 项决定。
- **依据**：同日对三个数据集现有构建的只读审计。审计期间没有修改仓库、配置和产出，也没有重建。

阅读说明：
- 文中数字是事件数或记录数，另有说明的除外。
- 不含任何患者标识或病历文本。
- 问题编号 `P-*`、任务编号 `T*`、决定编号 `D-R*` 在全文中互相引用；第 6 节的追踪矩阵保证每个问题都有对应的任务、检查和验收。

---

## 0. 总览

### 0.1 审计对象

| 数据集 | 配置哈希（与当前 yaml 一致） | 规范层 / OMOP 与 MEDS 构建日期 | 校验（通过/跳过/失败） | 事件数 |
|---|---|---|---|---:|
| CU-CTPA | `e68d27e3` | 2026-09-12 / 2026-09-12 | 36 / 4 / 0 | 13,701,522 |
| JHU CTPE | `0f305d69` | 2026-09-11 / 2026-09-12 | 39 / 1 / 0 | 31,611,295 |
| MIMIC-IV | `c35b70d8` | 2026-09-12 / 2026-09-12 | 35 / 5 / 0 | 304,811,180 |

### 0.2 问题与决定数量

| 类别 | 数量 | 编号 |
|---|---:|---|
| 转换器层（三个数据集共有） | 13 | `P-C1`–`P-C13` |
| CU-CTPA 独有 | 12 | `P-CU1`–`P-CU12` |
| JHU CTPE 独有 | 12 | `P-J1`–`P-J12` |
| MIMIC-IV 独有 | 21 | `P-M1`–`P-M21` |
| 确认不修（写明理由，持续监控） | 6 | `W1`–`W7`（`W3` 已转为 `P-M21`） |
| 决定 | 18 | `D-R1`–`D-R18`，全部已于 2026-09-13 拍板 |
| 仍需数据方回答的问题 | 3 | 见 3.3 |

### 0.3 路线

| 阶段 | 内容 | 是否改产出 | 粗估 |
|---|---|---|---|
| 0 | 把审计固化成工具和检查，让问题先被检测到 | 否 | 1–2 天 |
| 1 | 修转换器核心（含 `visit_detail` 发布器） | 否（只在 fixture 上测） | 5–7 天 |
| 2 | 各数据集的 yaml、准备脚本、映射；JHU 随访与 ICU 转科；CU 影像清单 | 否（只在 fixture 上测） | 5–7 天 |
| 3 | 按顺序重建三个数据集，对比并验收 | 是 | 1–2 天 |
| 4 | MIMIC ICU 模块全部纳入（D-R13） | 是 | 2–4 周 |
| 5 | 决定记录、README、故障目录、论文 | 否 | 1 天 |

阶段 0 不依赖任何数据，可以立即开始。

---

## 1. 原则与约束

1. **先检测，后修复。** 每类问题先有一个检查，这个检查必须在现有三份产出上失败；修复后同一个检查通过，才算修好。
2. **判断不写进代码。**
   - 单位改写、时间回退、合并规则、纳入范围都写进 `datasets/*.yaml` 或 `mappings/`；
   - 在 `docs/DECISIONS.md` 记一节，注明是数据方的回答还是研究团队的判断；
   - 不确定的进 quarantine 或 review。
3. **核心代码不认识任何数据集。** `tests/test_no_hardcoded_dataset_strings.py` 禁止 `src/` 出现数据集的列名、文件名、标签，也禁止写死概念 id。数据集特有规则只能放在 `datasets/*.yaml`、`mappings/`、`tools/prepare_*.py`；换算表和合理范围表放在参考 CSV 里。
4. **`mappings/` 只能由 `ehr2trace compile` 写入。** 修改映射或映射备注都走 propose → review → compile。
5. **配置字段一次加齐。** 配置哈希一变就要全量重新 ingest，所以阶段 1 的配置模型改动集中在 `T1.11` 一次完成。
6. **构建安全。** 构建期间不改 `datasets/*.yaml` 和 `src/ehr2trace/config.py`；重建和计时前，先检查有没有残留的 `multiprocessing.spawn` 进程。
7. **原值永远保留。** 规范化只新增列，不覆盖原始值、原始单位和原始时间。
8. **每个新检查都要有对应故障。** 在 `src/ehr2trace/faults.py` 和 `docs/FAULT_CATALOGUE.md` 各加一个故障，来历就是本次审计。
9. **身份信息最小化。**
   - 准备脚本只投影需要的列。CU 影像元数据 CSV 里的姓名、出生日期、原始患者号不得复制；
   - 读取格式不明的原始文件时只输出统计量或遮掩后的形状，不打印首行。JHU ICU 病程记录文件没有表头，首行就是病历正文。
10. **日期层面的比较在数据集声明的时区里做。** 规范层时间是 UTC；直接按 UTC 取日期，会把傍晚的事件算到第二天。

---

## 2. 问题清单

### 2.1 转换器层（三个数据集共有）

| ID | 问题 | 证据 | 位置 |
|---|---|---|---|
| P-C1 | **不同的记录被当成重复合并。** 事件身份只含数据集、source、病人、事件类型、代码体系、源代码、事件时间、就诊号、解析值、区分字段，不含剂量、剂量单位、途径、状态、结束时间 | CU 药物 1,118,171 行；JHU 医嘱 1,322,496 行（分区内）；MIMIC 处方 1,327,147 行、药房 1,866,580 行 | `src/ehr2trace/canonical/normalize.py:349` |
| P-C2 | **合并时字段冲突被静默处理。** `merge_events` 假设同 id 即同内容，保留先遇到的非空值 | 就诊结束值：CU ICU 176 组、MIMIC transfers 25 组、edstays 4 组。诊断状态：JHU 45,348 组。化验可用时间：MIMIC 328,120 组、JHU 14,709 组 | `src/ehr2trace/canonical/dedup.py:18` |
| P-C3 | **OMOP 测量的单位概念全为 0**，代码里写死 | CU 982,669 行、JHU 15,911,891 行、MIMIC 161,725,013 行 | `src/ehr2trace/omop.py:794`、`:849` |
| P-C4 | **单位不规范、不换算、不查合理性** | JHU 钠有 5 种写法；MIMIC D-dimer 混用两种单位；CU 体温华氏标成摄氏 | 无 |
| P-C5 | **源数据本来没有单位的数值，无法声明单位** | JHU 心电图数值 4,114,733 条只有 26 条有单位，肺功能数值 107,550 条都没有单位；MIMIC OMR 通用名没有单位 | 配置模型 |
| P-C6 | **OMOP 剂量单位只从剂量文本里解析** | CU `dose_unit_source_value` 3,088,590 行全空；MIMIC 处方文本解析出单位的只有 13/16,654,600 和 9/1,735,600，`drug_exposure` 有 18,567,232 行为空 | `src/ehr2trace/omop.py` 的 `dose_map` 与第 709 行 |
| P-C7 | **死亡冲突按时间戳判断**（`count(DISTINCT event_time)`）：只有日期的死亡和有时刻的死亡必然被判为冲突 | MIMIC 11,402 人没有 DEATH 行。按纽约本地日期，其中 11,401 人两个来源是同一天，真正跨日只有 1 人（相差 4 天）。版本 1 写的"9,538 人同日、1,864 人跨日"是按 UTC 取日期造成的，已更正 | `src/ehr2trace/omop.py:918` |
| P-C8 | **数值解析把数字后面的任意文字当成单位** | JHU 心电图有 26 条诊断文本被拆成"数字 + 单位" | `src/ehr2trace/canonical/values.py:35` |
| P-C9 | **`codes.parquet` 的描述取字母序最小的源名** | CU 的 SpO2 概念（127,715 条，其中 127,700 条是 SpO2）描述成 `Post SpO2`；体温概念（127,703 条，其中 90 条是 Core）描述成 `Core (Body) Temperature` | `src/ehr2trace/meds.py:390` |
| P-C10 | **没配置的原始表和原始列不报警** | CU 的 T1a、T7 三列；JHU `All_kinds/`；MIMIC ICU 模块等 | 无 |
| P-C11 | **校验盲区**：(a) 被合并行算作"变成了事件"；(b) 零产出的 source 仍报 present；(c) 任意比例的隔离都算"有原因"；(d) 死亡冲突只写 quality_issue | 三个数据集 0 失败 | `src/ehr2trace/validate.py` |
| P-C12 | **没有 `visit_detail` 发布器**，转科、换服务、ICU 住院只能写成 `visit_occurrence` | MIMIC transfers 2,413,554 行、services 593,071 行 | `src/ehr2trace/omop.py` |
| P-C13 | **规范层和 MEDS 没有速率、操作类型、出院去向字段** | MIMIC eMAR 输注速率 2,104,436 行、POE 操作类型、出院去向 396,210 行都无处可放 | 配置模型、`src/ehr2trace/schema.py` |

### 2.2 CU-CTPA

| ID | 问题 | 证据 |
|---|---|---|
| P-CU1 | **药物医嘱合并**（P-C1） | 准备后 4,206,761 行，发布事件 3,088,590 条。被合并的 781,331 组里：剂量不同 726,710、状态不同 214,177、结束日期不同 379,701、频次不同 669,635；单组最多 22 条。全字段真正相同的只有 11,263 行 |
| P-CU2 | **体温华氏度标成摄氏度** | `Temp` 67,660 条（67,604 条在 90–110，13 条在 30–45）；`Core (Body) Temperature` 90 条（全部在 90–110）。源单位列写着 degree Celsius，和 `Temp (in Celsius)` 59,953 条一起映射到概念 3020891。`mappings/measurement.csv:73` 备注写着 unit agrees |
| P-CU3 | **剂量没有单位** | yaml 只映射了 `ordered_dose`；`dose_unit`（mg 1,420,563、Units 1,158,072、Units/kg/hr 418,259 …）只留在 source 层；MEDS 里纯数字剂量 1,897,802 条 |
| P-CU4 | **笔记重复** | 491,192 组笔记同人、同日、同类型、全文相同，只有 `arb_encounter_id` 不同，多出约 936,403 条事件。中位长度 8,482 字符；365,425 组跨文件，123,980 组在同一文件内 |
| P-CU5 | **T1a 没进产出** | 168,046 个 accession（其中 40,091 次后续扫描没有日期）。影像目录只有约 7,000 个 accession 有影像（2016–2017 年，`conversion_summary.csv` 共 7,486 行）；`links.csv` 和 `copy_summary.csv`（110,567 个序列）已经有原始 Study Date |
| P-CU6 | **ICU 住院天数冲突被静默合并**（P-C2） | 176 组，177 行。开始时间只到日期，住院天数带小数，两者相差 0.25–9.9 天，更像是同一天两次进出 ICU |
| P-CU7 | **CPT 合并** | 97,183 组合并了 97,205 行。核实是同一次服务的两条账单：一条院方收费（HB CHG …），一条专业收费（PR …、MEDICINE、EM …）。例如 93306 心超"院方 + 心血管专业收费"就有 55,120 组。`procedure_name` 没有映射 |
| P-CU8 | **T7 三列没用上** | 非空行数：`number_of_doses` 244,181、`dispensed_quantity` 834,932、`quantity_unit` 1,430,902 |
| P-CU9 | **下拉选项值被当成测量值** | `Respirations`（Custom List）14 条，值为 3 或 4，被映射成呼吸频率 |
| P-CU10 | **不合理的数值没有处理** | `Temp (in Celsius)` 最低 −15.6；`Temp` 最低 7.8、最高 134.6；`Resp` 最高 196。零值：BP diastolic 15、BP systolic 7、Pulse 21、Resp 51、SpO2 11 |
| P-CU11 | **准备步骤和文档与数据不符** | `prepare_manifest.json` 原始输入 sha256 为 null；准备阶段丢了 129 行没记录（T1a 3、T5 6、T6 70、T7 50）。yaml 的 `batch_relationship` 写"disjoint pulls"，实际 1,427,559 组相同笔记跨文件出现；yaml flowsheets 注释说 Post BP 行没有 Blood Pressure 类型，实际有；`measurement.csv:73` 备注与数值不符 |
| P-CU12 | **笔记的就诊号基本对不上其他表**（源数据特性，转换器修不了，只能记录并影响合并规则） | 笔记里 163,612 个"病人 + 就诊号"组合，只有 4,293 个（2.6%）出现在诊断、药物、生命体征或操作表里。相比之下，药物与诊断的就诊号能对上 65.6%，生命体征与诊断 66.3% |

### 2.3 JHU CTPE

| ID | 问题 | 证据 |
|---|---|---|
| P-J1 | **医嘱合并**（P-C1） | 分区内合并 1,322,496 行，涉及的 1,168,526 组每组剂量或状态都不同。另有 3,585,115 个跨分区组是 29/29b 之间的真重复，合并是对的 |
| P-J2 | **问题列表状态或名称冲突**（P-C2） | 分区内合并 79,102 行（74,269 组）。状态组合：空与 Active 27,177 组、空与 Resolved 6,454、Active 与 Resolved 5,430，涉及 Deleted 的约 5,974 组 |
| P-J3 | **化验可用时间冲突**（P-C2） | 14,709 组：9,384 组相差不到 1 小时，5,117 组 1–24 小时，128 组 1–7 天，80 组超过 7 天 |
| P-J4 | **`CCDA_data/All_kinds/` 里属于 CTPE 队列的表都没读** | 文件名与分区的对应已用 MRN 核实：`29_pulmonary_embolism` 对应 29_has，`29_no_pulmonary_embolism` 对应 29_no，`29b_has_pulmonary_embolism` 对应 29b_has，`29b_pulmonary_embolism` 对应 29b_no。<br>各表：<br>· 随访：每组一个工作表，每人一行<br>· ICU 转科工作簿：每组 28.9–42.2 万行，覆盖 99.6%–99.8% 的病人<br>· 手术病理：每组 131–159 MB，29_has 那组是 xlsx、102 万行<br>· 心导管：覆盖约 11% 的病人，各组比例一致<br>· 血脂：每组 10.9–19.1 万行，覆盖约 70%<br>· ICU 病程记录：4 个文件约 33.6 GB，没有表头，内容还包括急诊、入院、操作前评估等笔记<br>· 手术记录：每组 30–77 行 |
| P-J5 | **就诊类型没映射** | 8,582/25,573（34%）：Emergency 4,093、Hospital Outpatient Surgery 1,926、Observation 1,512、Outpatient 320 等 |
| P-J6 | **化验参考范围没映射** | 源里有参考范围上下限两列，yaml 没映射 |
| P-J7 | **没有单位的数值测量**（P-C5） | 心电图数值 4,114,733 条，肺功能数值 107,550 条 |
| P-J8 | **心电图诊断文本被误解析**（P-C8） | 26 条 |
| P-J9 | **单位写法不统一**（P-C3、P-C4） | 钠：mmol/L 1,087,299、mEq/L 185,995、meq/L 78,640、MEQ/L 1,139、mMOL/L 649（量纲相同）；WBC 有 6 种写法 |
| P-J10 | **分区内给药记录合并**（P-C1，规模小） | 4,807 行，4,796 组 |
| P-J11 | **已删除的问题列表条目被当作诊断发布** | 状态为 `Deleted` 的 30,069 行（其余：空 5,531,824、Active 571,102、Resolved 162,915） |
| P-J12 | **交付文件标错** | 手术记录目录的 `29_pulmonary_embolism.xlsx`（按命名应是 29_has）与 `29_no_pulmonary_embolism.xlsx` 行数相同，MRN 100% 属于 29_no，是副本。29_has 的手术记录在这次交付中缺失 |

### 2.4 MIMIC-IV

| ID | 问题 | 证据 |
|---|---|---|
| P-M1 | **处方和药房记录合并**（P-C1） | 处方：按 NDC 合并 1,064,318 行（pharmacy_id 不同的 803,922 组，剂量不同 370,486 组），按药名合并 262,829 行。药房：合并 1,866,580 行，1,526,725 组全部 pharmacy_id 不同。eMAR 有 `emar_seq`，不受影响 |
| P-M2 | **ED 生命体征零产出** | `datasets/mimiciv.yaml:297` 把宽表声明成 `component_measurements`，又没映射值列，1,564,610 行全部以"component has no name"被隔离。体温非空 999,642 条，其中 3,983 条在摄氏范围 |
| P-M3 | **分诊生命体征被隔离** | 2,849,786 个值因为"没有时间"被隔离（`mimiciv.yaml:341`–`343`）。每行都有 `stay_intime`，与 edstays 的到院时间一致（425,087/425,087）；ED 第一条生命体征中位在到院后 3 分钟，61% 在 30 分钟内 |
| P-M4 | **ICU 模块没读** | caregiver、chartevents（压缩后 3.5 GB）、d_items、datetimeevents、icustays、ingredientevents（0.31 GB）、inputevents（0.40 GB）、outputevents、procedureevents |
| P-M5 | **院内死亡缺失**（P-C7） | 11,402 人有两条死亡事件（`patients.dod` 在本地零点，`admissions.deathtime` 有具体时刻）。按纽约本地日期 11,401 人同一天，1 人相差 4 天。OMOP DEATH 26,899 行，实际死亡 38,301 人；MEDS_DEATH 49,703 条 |
| P-M6 | **药敏结果丢失** | `ab_name`、`dilution_text` 没映射，1,344,018 行被合并进菌种事件（203,209 组，其中抗生素不同的 141,124 组）；原始药敏行 1,410,258 |
| P-M7 | **假就诊** | transfers 表 `eventtype = discharge` 的 546,024 行（科室 UNKNOWN、无结束时间）被发布成就诊；其中 91.3% 与入院表出院时间相差不到 1 小时 |
| P-M8 | **就诊类型** | edstays 的就诊类型取自出院去向（`mimiciv.yaml:112`：HOME 241,628、ADMITTED 158,010 等）；transfers 科室和 services 没映射。`visit_occurrence` 里概念为 0 的 3,431,629/3,977,657（86%） |
| P-M9 | **D-dimer 量纲混用**（P-C4） | itemid 50915：两种单位在各年代中位数都约 500（ng/mL 与 ng/mL FEU），更像换了标签；2017–2019 年几乎只有 FEU。itemid 51196：2008–2010 年 ng/mL 3,180 条（中位 539）、FEU 3,465 条（中位 846），2011 年后几乎只有 FEU。版本 1 写的"中位约 3,700"来自规范层解析值，已改用源数值 `valuenum` |
| P-M10 | **OMOP 处方剂量单位为空**（P-C6） | 见 P-C6 |
| P-M11 | **eMAR 输注速率没发布**（P-C13） | 2,104,436 行有速率：肝素 units/hr 346,875、呋塞米 mg/hr 78,565、胰岛素 26,774、硝酸甘油 mcg/kg/min 22,200 等；另有 1,263,928 行 mL/hr 速率没有药名（见 P-M21） |
| P-M12 | **POE 操作类型没用上**（P-C13） | `transaction_type`，涉及 52,212,109 条服务医嘱 |
| P-M13 | **入院表的出院去向没用上**（P-C13） | `discharge_location` 非空 396,210 行 |
| P-M14 | **ED 发药合并，且没有就诊号** | 9,183 组 `med_rn` 不同被合并（另有 337,258 组只是 GSN 不同，合并正确）；`ed_pyxis` 和 `ed_diagnosis` 都没映射 `stay_id` |
| P-M15 | **ED 事件挂不到 ED 就诊上** | edstays 的就诊号用 `hadm_id`，ED 生命体征用 `stay_id`，发药和诊断没有就诊号 |
| P-M16 | **化验可用时间冲突**（P-C2） | 328,120 组：270,415 组相差不到 1 小时，54,890 组 1–24 小时，2,582 组 1–7 天，233 组超过 7 天 |
| P-M17 | **OMR 通用名没有单位**（P-C5） | Weight 15,108、BMI 29,021、Height 2,365 |
| P-M18 | **其他没读的表**（P-C10） | hosp：drgcodes、hcpcsevents、poe_detail、provider、d_hcpcs；ED：medrecon；note：discharge_detail、radiology_detail |
| P-M19 | **就诊冲突**（P-C2） | transfers 25 组（开始时间精确到秒，结束时间相差 0–473 分钟）；edstays 4 组 |
| P-M20 | **转科和换服务写成了 `visit_occurrence`**（P-C12） | 见 P-C12 |
| P-M21 | **没有药名的给药和药房记录被隔离，其实可以找回**（原 W3） | eMAR 有 2,120,873 行没有药名，其中 1,463,157 行（69%）可通过 `pharmacy_id` 在处方表找到药名；药房有 1,137,574 行没有药名，其中 1,069,122 行（94%）可找回，这些记录的类型主要是大容量静脉输液 1,035,767 行、TPN 68,439 行 |

### 2.5 查过、没有问题的（不需要行动）

- **CU-CTPA**
  - 原始 CSV 自 2026-03 以来没改过；
  - T2、T3、T4 行数对得上，血压拆分精确到行；
  - 再入院合并符合设计。
- **JHU CTPE**
  - `CTPE/` 下 25 个文件都读了；
  - 29/29b 跨批去重正常；
  - has/no 两组在给药数据上没有结构差异；
  - `All_kinds/` 的心导管、血脂在四组间覆盖率一致；
  - 剂量文本自带单位。
- **MIMIC-IV**
  - 准备清单记录了全部输入的 sha256；
  - 2026-09-06 就绪审计的问题已修复；
  - 笔记没有重复。
- **三个数据集**：只有日期的死亡不会把当天更早的记录误标成"死后记录"。

### 2.6 确认不修（写明理由，持续监控）

| ID | 内容 | 理由 | 监控 |
|---|---|---|---|
| W1 | JHU 534,610 条医嘱没有下单日期，被隔离 | 源数据本来没有日期 | `QUARANTINE_SHARE_DECLARED` 中声明为预期 |
| W2 | CU BMI 124,367 个值没有时间，被隔离 | 沿用现有决定 | 同上 |
| W3 | ~~MIMIC 无药名记录被隔离~~ | **已转为 P-M21，按 D-R18 修复** | — |
| W4 | MIMIC 1,131,566 条事件结束早于开始，已标记保留 | 源时间戳如此 | 现有 `END_TIME_NEVER_PRECEDES_START` |
| W5 | 有时间的事件中，可用时间为假设值的比例：CU 99.8%、JHU 48.3%、MIMIC 29.3% | 源数据没有"何时可知"的记录；已逐条标记 | 现有 `MEDS_AVAILABILITY_PREVENTS_LEAKAGE` |
| W6 | MIMIC 放射报告 8,237 条与同一病人另一条全文相同（0.35%） | 规模可忽略 | `NOTE_TEXT_UNIQUE` 只报告 |
| W7 | MIMIC `hospital_expire_flag` 没用上 | 与 `deathtime` 重复 | `RAW_COVERAGE_DECLARED` 中声明为忽略 |

---

## 3. 决定

### 3.1 决定结果（2026-09-13，研究团队 Xinye 拍板）

"与推荐一致"指与拍板时给出的推荐选项一致；其中 D-R3、D-R4、D-R6、D-R11、D-R16 的选项在拍板前已根据第二轮证据修订。

| ID | 问题 | 决定 | 关键依据 | 与推荐 | 影响任务 |
|---|---|---|---|---|---|
| D-R1 | CU `Temp`、`Core (Body) Temperature` 单位 | **按 °F 读并换算成 °C**，原值保留并标记单位被覆盖；之后请数据方确认 | 中位 98，99.9% 在 90–110 | 一致 | T1.6、T2.CU2 |
| D-R2 | CU 同文、只差就诊号的笔记 | **合并为一条；组内恰有一个就诊号能对上其他表时用它，否则就诊号置空并标记**；全部来源行留在溯源 | P-CU12：笔记就诊号只有 2.6% 能对上 | 一致 | T1.2、T2.CU3 |
| D-R3 | CU 同日同就诊同 CPT 的多条账单 | **合并为一次服务**，两条账单都记进溯源并标记 `BILLING_DUPLICATE`；不发布数量 2 | 核实为院方收费加专业收费 | 一致（选项已修订） | T1.2、T2.CU4 |
| D-R4 | 就诊结束值冲突 | **按时间精度区分**：开始只到日期的（CU ICU）视为不同住院，各发一条；开始精确到秒的（MIMIC transfers、edstays）视为冲突，保留开始、结束置空并标记，冲突值写 quarantine | CU 住院天数带小数，差 0.25–9.9 天 | 一致（选项已修订） | T1.1、T1.2、T2.CU5、T2.M12 |
| D-R5 | 化验可用时间冲突 | **取最早的可用时间并标记** `AVAILABILITY_MERGED` | MIMIC 82% 相差不到 1 小时 | 一致 | T1.3、T2.J4、T2.M12 |
| D-R6 | JHU 问题列表状态 | **去掉 Deleted 行；其余合并时非空状态优先；Active 与 Resolved 同时出现则标记冲突** | Deleted 30,069 行；冲突以"空 vs Active"为主 | 一致（选项已修订） | T1.2、T2.J4 |
| D-R7 | MIMIC 分诊时间 | **用 ED 到院时间作回退并标记** `TIME_FALLBACK`；主诉作为文本事件 | 首条生命体征中位在到院后 3 分钟 | 一致 | T2.M3 |
| D-R8 | MIMIC transfers 的 `discharge` 行 | **过滤掉** | 出院时间已在入院表 | 一致 | T2.M5 |
| D-R9 | ED 就诊类型 | **固定为急诊**；出院去向写入 `discharged_to` | 出院去向不是就诊类型 | 一致 | T2.M6 |
| D-R10 | D-dimer 单位 | **按"检验代码 + 单位"拆成不同代码，不换算** | FEU 与 DDU 无精确系数；50915 看起来只是标签变更 | 一致 | T1.6、T2.M7 |
| D-R11 | MIMIC 死亡发布 | **按数据集时区比较日期；同一天的合并为一条，取 admissions 的精确时刻；OMOP 与 MEDS 每人一条；唯一 1 例跨日冲突不发布到 OMOP，MEDS 保留两条并标记，进复核** | 11,401/11,402 同日 | 一致（选项已修订） | T1.8 |
| D-R12 | JHU `All_kinds/` 纳入范围 | **纳入随访和 ICU 转科**；手术病理、心导管、血脂、ICU 病程记录、手术记录写入 `out_of_scope` 并注明理由 | 随访可作删失终点；ADT 覆盖约 99.7% 病人 | 一致 | T2.J5、T2.J7 |
| D-R13 | MIMIC ICU 模块 | **全部 ICU 模块纳入**：icustays、chartevents、inputevents、ingredientevents、outputevents、procedureevents、datetimeevents；d_items 用于查表；caregiver 声明为不纳入（照护者标识，不是临床事实） | chartevents 压缩后 3.5 GB | **不同**（推荐为最小集） | T4.1–T4.4 |
| D-R14 | `visit_detail` 发布器 | **新增**：转科、换服务、ICU 住院、JHU ICU 转科改走 `visit_detail` | 就诊表 86% 概念为 0 | 一致 | T1.13、T2.M6、T2.J7、T4.1 |
| D-R15 | eMAR 输注速率 | **规范层和 MEDS 新增 `rate`、`rate_unit`；OMOP 在 `drug_exposure.sig` 里写入速率文本** | OMOP 无速率列 | **不同**（推荐为只进 MEDS） | T1.4、T1.11、T2.M8 |
| D-R16 | CU T1a | **全部 accession 进影像检查清单（不进事件流）；有影像的约 7,500 个从 `links.csv` 取原始 Study Date 并记录影像路径** | 元数据 CSV 已含检查日期 | 一致（选项已修订） | T2.CU7 |
| D-R17 | 不合理数值 | **原值保留，标记 `IMPLAUSIBLE`，不进规范化列**；合理范围表放参考 CSV 并写依据 | 不猜、不删 | 一致 | T1.6、T2.CU6、T2.M2 |
| D-R18 | MIMIC 无药名的给药与药房记录 | **准备脚本按 `pharmacy_id` 查处方表找回药名，标记 `NAME_FROM_LINKED_ORDER`；找不回的继续隔离** | 可找回 146 万 + 107 万行 | 一致 | T2.M13 |

### 3.2 两项与推荐不同的决定带来的额外工作

- **D-R13（全部 ICU 模块）**
  - chartevents 是 MIMIC 最大的表，重建时间、内存和磁盘都会明显增加；
  - 需要按 itemid 逐项做单位与合理范围声明；
  - 分批纳入（T4.1–T4.4），每批单独验收。
- **D-R15（OMOP 写 sig）**
  - `sig` 目前用来存放解析不了的剂量文本（`src/ehr2trace/omop.py` 第 700 行附近，截断到 250 字符）；
  - 需要定义合并格式，例如"<剂量文本>; rate <值> <单位>"，并保证不超长；
  - 在 `docs/DECISIONS.md` 写明：`sig` 在 OMOP 中本是给药说明，这里是有意借用。

### 3.3 仍需数据方回答的问题

1. **CU**：`Temp`、`Core (Body) Temperature` 的单位列写着摄氏，但数值是华氏，请确认（D-R1 已先按华氏执行）。
2. **JHU**：手术记录的 29_has 文件被 29_no 的副本替代，请补发（P-J12）。
3. **MIMIC**：1 例住院死亡时刻与 `dod` 相差 4 天（D-R11 复核队列）。这是公开数据，可自行复核，不需要问数据方。

---

## 4. 阶段与任务

### 阶段 0：让问题先被检测到（不改产出）

**T0.1 把审计固化成工具**
- 新增 `tools/audit_conversion.py`，对任意数据集输出 `results/<dataset>/conversion_audit.json`，内容包括：
  - 各 source 的事件、合并、隔离数；
  - 合并组内各字段的差异计数（JHU 另分"分区内"和"跨分区"两种口径）；
  - 单位与就诊概念覆盖率、同一代码的量纲分布、温度类数值范围；
  - 剂量单位空值率、死亡人数与 DEATH 行数（按数据集时区比较日期）；
  - 同文笔记重复数、就诊号可关联率、没用上的原始表和列。
- 只输出统计量，读取原始文件时不打印行内容。

**T0.2 新增检查**（`src/ehr2trace/validate.py` 的 `@check`，代码里不出现数据集字符串）

| 检查 | 规则 | 覆盖问题 | 现有产出预计 |
|---|---|---|---|
| `DUPLICATES_AGREE` | 被合并的源行必须在所有映射字段上与保留事件一致；已声明合并规则的字段除外 | P-C1、P-C2、P-C11a | 三个数据集都失败 |
| `SOURCE_YIELDS_EVENTS` | 有解析行的 source 必须产出事件，除非声明为预期为空 | P-C11b、P-M2 | MIMIC 失败 |
| `QUARANTINE_SHARE_DECLARED` | 某 source 某原因的隔离比例超过阈值（默认 5%）时，必须在 yaml 声明；T1.11 之前只报告 | P-C11c、P-M3、P-M21、W1、W2 | 报告 |
| `DEATH_PUBLISHED` | 有死亡事件的人都有 DEATH 行（本地日期真冲突的除外）；MEDS 每人一条（冲突者除外） | P-C7、P-C11d、P-M5 | MIMIC 失败 |
| `UNIT_CONCEPT_COVERAGE` | 有源单位或声明单位的数值测量里，单位概念非 0 的比例达到阈值 | P-C3、P-C5、P-J7、P-J9、P-M17 | 三个都失败 |
| `UNIT_KNOWN` | 解析出的单位必须在单位表里 | P-C8、P-J8 | JHU 失败 |
| `UNIT_VALUE_PLAUSIBLE` | 按代码加单位对照合理范围表 | P-C4、P-CU2、P-CU9、P-CU10、P-M2 | CU 失败 |
| `UNIT_HOMOGENEOUS_PER_CODE` | 同一代码不能混用无法精确换算的量纲 | P-C4、P-M9 | MIMIC 失败 |
| `DOSE_UNIT_CARRIED` | 源有剂量单位的，MEDS 和 OMOP 都不能为空 | P-C6、P-CU3、P-M10 | CU、MIMIC 失败 |
| `VISIT_CONCEPT_COVERAGE` | 就诊概念非 0 的比例达到阈值；零时长的 UNKNOWN 就诊为 0 | P-J5、P-M7、P-M8 | JHU、MIMIC 失败 |
| `NOTE_TEXT_UNIQUE` | 同人、同日、同类型、同全文的笔记不超过一条 | P-CU4、W6 | CU 失败 |
| `ENCOUNTER_RESOLVES` | 事件就诊号能对上同一病人的就诊事件；可在 yaml 声明预期的可关联率（CU 笔记） | P-CU12、P-M14、P-M15 | MIMIC 失败；CU 笔记报告 |
| `CODE_DESCRIPTION_IS_REPRESENTATIVE` | `codes.parquet` 的描述是概念名或出现最多的源名 | P-C9 | 至少 CU 失败 |
| `RAW_COVERAGE_DECLARED` | 每个原始表、每个原始列：已映射、已保留，或已声明忽略并写明理由；T1.11 之前只报告 | P-C10、P-CU5、P-CU8、P-J4、P-J6、P-J12、P-M4、P-M11–13、P-M18 | 报告 |
| `EXCLUDED_STATUS_NOT_PUBLISHED` | yaml 声明为排除的状态值（例如问题列表的 Deleted）不得出现在发布的事件里 | P-J11 | JHU 失败 |

**T0.3 故障注入**
- 在 `src/ehr2trace/faults.py` 新增 9 个故障，都在 `tests/fixtures/ctpe_shape/` 上运行：
  1. 剂量不同的医嘱被合并；
  2. 零产出 source；
  3. 单位与数值矛盾；
  4. 丢掉剂量单位；
  5. 日期型死亡与时刻型死亡并存（时刻落在 UTC 次日）；
  6. 同文笔记挂在多个就诊号上；
  7. 未声明的原始列；
  8. 数字后面跟诊断文字；
  9. 已删除的问题列表条目被发布。
- `docs/FAULT_CATALOGUE.md` 记录来历；`tests/integration/test_fault_detection.py` 断言全部被检出。

**T0.4 修复前基线**
- 在三份现有产出上跑 `ehr2trace validate` 和 T0.1，存到 `results/<dataset>/remediation_baseline_2026-09-13.json`；
- 逐项对照第 2 节，漏报就补检查。

**阶段 0 验收**：每个 `P-*` 至少被一个检查报失败或报告；9 个新故障全部被检出。

### 阶段 1：修转换器核心（在 fixture 上开发和测试）

**T1.1 事件身份**（P-C1、P-J1、P-J10、P-M1，D-R4）
- `drug_order`、`drug_admin`、`drug_dispense` 的身份加入剂量（规范化为"数值 + 单位"后再算哈希）、剂量单位、途径、状态、结束时间。
- `condition` 的状态**不纳入**身份（D-R6 用合并规则处理）。
- `visit` 通过配置项 `identity_extra_fields` 决定是否纳入结束时间：CU ICU 住院（开始只到日期）纳入；MIMIC 不纳入（D-R4）。
- 有记录号的 source 用现有 `sequence_number` 字段声明区分字段（例如 MIMIC `pharmacy_id`、ED 发药 `med_rn`）。

**T1.2 合并规则**（P-C2，D-R2、D-R3、D-R4、D-R6）
- `merge_events` 遇到非空字段不一致时，按 yaml 声明的规则处理：

| 规则 | 行为 | 用途 |
|---|---|---|
| `earliest` / `latest` | 取最早 / 最晚 | 可用时间（T1.3） |
| `null_and_flag` | 置空，标记 `VALUE_CONFLICT`，冲突值写 quarantine | MIMIC 就诊结束时间（D-R4） |
| `priority` | 按声明顺序取值，指定组合出现时标记冲突 | JHU 问题列表状态：Resolved/Active 优先于空，二者同时出现标记（D-R6） |
| `prefer_linked` | 优先取在其他表出现过的值（准备脚本生成的关联标记列），否则置空并标记 | CU 笔记就诊号（D-R2） |
| `keep_all_flag` | 保留一条事件，所有取值留在溯源，打指定标记 | CU CPT 的院方与专业收费（`BILLING_DUPLICATE`，D-R3） |

- 没有声明规则的冲突，写 `MERGE_CONFLICT` 质量问题，并让 `DUPLICATES_AGREE` 失败。

**T1.3 可用时间的默认合并规则**（P-J3、P-M16，D-R5）
取最早的 `available_time`，标记 `AVAILABILITY_MERGED`。

**T1.4 OMOP 剂量单位与速率**（P-C6、P-CU3、P-M10、P-M11，D-R15）
- `dose_unit_source_value` = coalesce（剂量文本解析出的单位，事件单位）；
- 有速率时，`sig` 写成"<原有剂量文本>; rate <值> <单位>"，总长不超过现有 250 字符截断。

**T1.5 单位映射**（P-C3、P-J9）
- 新增 `mappings/unit.csv`（源单位字符串 → UCUM 概念），走 propose → review → compile；
- `src/ehr2trace/omop.py:794`、`:849` 改为查表。

**T1.6 单位规范化与合理性**（P-C4，D-R1、D-R10、D-R17）
- 规范层和 MEDS 新增规范化数值、规范化单位两列，原值不动；
- 只做精确换算（°F→°C 等），换算表放参考 CSV；
- 支持 yaml 的 `unit_override`，按源代码覆盖单位并标记 `UNIT_OVERRIDDEN`；
- 超出合理范围的标记 `IMPLAUSIBLE`，不进规范化列；
- 同一代码混用无法换算量纲的，支持按 yaml 拆码（代码加单位后缀）。

**T1.7 声明单位**（P-C5、P-J7、P-M17）
`point_event` 和 `component_measurements` 支持按源代码声明单位，标记 `UNIT_DECLARED`。

**T1.8 死亡发布**（P-C7、P-M5，D-R11）
- `_publish_death` 先把时间换算到数据集声明的时区，再按日期判断冲突；
- 同一天的合并为一条，取最精确的时刻（有时刻的优先于只有日期的）；
- OMOP 与 MEDS 每人一条；
- 跨日冲突不发布到 OMOP，MEDS 保留两条并标记 `DEATH_DATE_CONFLICT`，写入复核队列。

**T1.9 数值解析**（P-C8、P-J8）
`RE_NUMBER_UNIT` 的尾部必须在单位表里，否则整段按文本处理，标记 `NON_NUMERIC_RESULT`。

**T1.10 codes 描述**（P-C9）
取概念名；没有概念时取出现最多的源名。

**T1.11 配置模型一次加齐**（P-C10、P-C13，以及 T1.1、T1.2、T1.6、T1.7 的载体）

| 字段 | 用途 |
|---|---|
| `ignored_columns`、`out_of_scope` | 必须写理由 |
| `identity_extra_fields` | T1.1 |
| `merge_rules`（`earliest`、`latest`、`null_and_flag`、`priority`、`prefer_linked`、`keep_all_flag`） | T1.2 |
| `declared_units`、`unit_override`、`split_code_by_unit` | T1.6、T1.7 |
| `expected_quarantine`、`expected_empty`、`expected_encounter_link_rate` | 新检查 |
| `excluded_status` | JHU Deleted |
| `rate`、`rate_unit`、`action`（操作类型）、`discharged_to` | P-C13 |

同步更新 `src/ehr2trace/schema.py` 和 `tests/unit/test_config.py`。MEDS 扩展列有变化，所以要在 `dataset.json` 里提升版本并列出新增列。

**T1.12 测试**
- 每项先写失败用例；
- 在 `tests/fixtures/ctpe_shape/` 和 `tests/fixtures/generic_ehr/` 里加入陷阱：
  - 同刻不同剂量的医嘱；
  - 宽表生命体征；
  - 日期型与时刻型死亡（时刻落在 UTC 次日）；
  - 华氏标成摄氏；
  - 同文笔记挂在多个就诊号上；
  - 同一服务的院方与专业收费；
  - 同日两次 ICU 住院；
  - 已删除的问题列表条目；
  - 没有单位的组件；
  - 数字后面跟文字；
  - 未声明的原始列；
  - 可用时间冲突。
- 保持 `--workers 1` 与 `--workers 4` 输出一致，无硬编码检查通过。

**T1.13 `visit_detail` 发布器**（P-C12、P-M20，D-R14）
- 新增 `visit_detail` 事件去向：转科、换服务、ICU 住院挂在所属住院下；没有所属住院的，挂在同一病人时间上覆盖的就诊下，找不到则标记；
- `VISIT_CONCEPT_COVERAGE` 按 `visit_occurrence` 与 `visit_detail` 分别统计。

**阶段 1 验收**
- 单元测试和 fixture 集成测试全部通过；
- 新检查在 fixture 上：修复前的构建失败，修复后的构建通过；
- 故障注入全部被检出。

### 阶段 2：各数据集的 yaml、准备脚本与映射

#### CU-CTPA

| 任务 | 内容 | 问题 | 决定 |
|---|---|---|---|
| T2.CU1 | 药物 source 声明 `unit: [dose_unit]`；`number_of_doses`、`dispensed_quantity`、`quantity_unit` 映射或写进 `ignored_columns` 并写理由 | P-CU3、P-CU8 | — |
| T2.CU2 | `Temp`、`Core (Body) Temperature` 写 `unit_override: [degF]` 并记录数值证据；`mappings/measurement.csv:73` 的备注走 review → compile 修正 | P-CU2、P-CU11 | D-R1 |
| T2.CU3 | 笔记设 `encounter_in_identity: false`；`prepare_cu.py` 用查表生成"就诊号在其他表出现过"的标记列；`merge_rules: {encounter_id: prefer_linked}`；声明 `expected_encounter_link_rate` | P-CU4、P-CU12 | D-R2 |
| T2.CU4 | CPT 声明 `merge_rules: {procedure_name: keep_all_flag, procedure_category: keep_all_flag}`，标记 `BILLING_DUPLICATE` | P-CU7 | D-R3 |
| T2.CU5 | ICU 住院声明 `identity_extra_fields: [length_of_stay]`（开始只到日期，视为不同住院） | P-CU6 | D-R4 |
| T2.CU6 | `Respirations` 按名称加入 `row_filter`；生命体征写入合理范围表 | P-CU9、P-CU10 | D-R17 |
| T2.CU7 | 影像检查清单：`prepare_cu.py` 读 T1a，并从影像目录的 `links.csv` 只投影 accession、原始 Study Date、序列描述、影像路径（不复制姓名、出生日期、原始患者号）；清单写入 etl_audit 表和 MEDS 元数据，不进事件流 | P-CU5 | D-R16 |
| T2.CU8 | `tools/prepare_cu.py` 记录原始输入 sha256 和每一步丢弃的行数；修正 yaml 里 `batch_relationship` 和 flowsheets 两处注释 | P-CU11 | — |

#### JHU CTPE

| 任务 | 内容 | 问题 | 决定 |
|---|---|---|---|
| T2.J1 | `mappings/visit.csv` 补 Emergency、Hospital Outpatient Surgery、Observation、Outpatient 等（review → compile） | P-J5 | — |
| T2.J2 | 化验 source 映射 `value_low`、`value_high` | P-J6 | — |
| T2.J3 | 心电图、肺功能组件 `declared_units`（bpm、ms、L、% 等），依据写进注释 | P-J7 | — |
| T2.J4 | 问题列表：`excluded_status: [Deleted]`；`merge_rules: {status: priority}`（Resolved 与 Active 优先于空，二者同时出现标记冲突）。化验可用时间用 `earliest` | P-J2、P-J3、P-J11 | D-R5、D-R6 |
| T2.J5 | `All_kinds/` 中手术病理、心导管、血脂、ICU 病程记录、手术记录写入 `out_of_scope` 并写理由；手术记录注明 29_has 文件标错、已向数据方追问 | P-J4、P-J12 | D-R12 |
| T2.J6 | `mappings/unit.csv` 覆盖 mmol/L、mEq/L、K/cu mm 等写法 | P-J9 | — |
| T2.J7 | 新增两个 source：<br>· **随访**：`followup.xlsx` 四个 CTPE 工作表，按分区读，作为最近随访的 observation 事件，并纳入观察期终点规则；<br>· **ICU 转科**：四个 ICU 工作簿，作为 `visit_detail`，带 ICU 标记和就诊号。<br>纳入前在四组间比对列、格式、覆盖率（分组格式指纹），防止格式差异泄漏 has/no 标签 | P-J4 | D-R12、D-R14 |

#### MIMIC-IV

| 任务 | 内容 | 问题 | 决定 |
|---|---|---|---|
| T2.M1 | 处方、药房 source 用 `sequence_number` 声明 `pharmacy_id` | P-M1 | — |
| T2.M2 | `tools/prepare_mimiciv.py` 把 ED 生命体征宽表转成长表（每个生命体征一行，带单位），清单用 `rows_added_by_split` 记录新增行数；yaml 改为 `point_event` | P-M2 | D-R17 |
| T2.M3 | 分诊同样宽转长，事件时间和可用时间都取 `stay_intime`，标记 `TIME_FALLBACK`；`chiefcomplaint` 作为文本事件 | P-M3 | D-R7 |
| T2.M4 | 准备脚本补 `micro_specimen_id`、`isolate_num`、`ab_itemid`、`dilution_comparison`、`dilution_value`；同一文件拆成"菌种"和"药敏"两个 source | P-M6 | — |
| T2.M5 | transfers 用 `row_filter` 去掉 `eventtype = discharge` | P-M7 | D-R8 |
| T2.M6 | ED 就诊类型用准备脚本投影的常量"急诊"，出院去向写入 `discharged_to`；transfers、services 改走 `visit_detail`，`mappings/visit.csv` 补科室和服务映射 | P-M8、P-M20 | D-R9、D-R14 |
| T2.M7 | D-dimer 两个 itemid 声明 `split_code_by_unit` | P-M9 | D-R10 |
| T2.M8 | eMAR 的 `infusion_rate`/`infusion_rate_unit` 映射到 `rate`/`rate_unit`；POE 的 `transaction_type` 映射到 `action`；入院表的 `discharge_location` 映射到 `discharged_to` | P-M11、P-M12、P-M13 | D-R15 |
| T2.M9 | ED 相关表统一用 `stay_id` 作就诊号，edstays 同时记录 `hadm_id`；`ed_pyxis` 声明 `med_rn` 为区分字段并映射就诊号 | P-M14、P-M15 | — |
| T2.M10 | 准备脚本给 OMR 通用名补单位（Weight→lb、Height→in、BMI→kg/m2），依据写进注释 | P-M17 | — |
| T2.M11 | drgcodes、hcpcsevents、poe_detail、provider、d_hcpcs、medrecon、两张 note detail 表写进 `out_of_scope` 并写理由；`hospital_expire_flag` 写进 `ignored_columns` | P-M18、W7 | — |
| T2.M12 | 化验可用时间用 `earliest`；transfers、edstays 的结束时间用 `null_and_flag` | P-M16、P-M19 | D-R4、D-R5 |
| T2.M13 | 准备脚本对没有药名的 eMAR 和药房行，按 `pharmacy_id` 查处方表的 `drug` 作为药名，并新增标记列（yaml 映射为 `NAME_FROM_LINKED_ORDER`）；清单记录找回和仍缺失的行数 | P-M21 | D-R18 |

**阶段 2 验收**
- `ehr2trace inspect` 对三个数据集没有新的 blocker；
- 准备脚本在 `--sample` 小样本上跑通，清单行数前后对得上；
- 18 项决定都已记入 `docs/DECISIONS.md`。

### 阶段 3：重建、对比与验收

**T3.1 开工前**
- 用 `ps` 确认没有残留进程、没有构建在跑；
- 记录新配置哈希；
- 保留旧的 `omop.duckdb` 副本用于对比（合计约 61 GB，磁盘剩余约 3.0 T）。

**T3.2 按成本从低到高重建**
- 顺序：CU（约 21 分钟）→ JHU → MIMIC（数小时，不含 ICU 模块）；
- 每个数据集完整跑一遍 prepare → inspect → ingest → identity → canonical → omop → meds → validate；
- 构建期间不改任何配置。

**T3.3 对比**：每个数据集跑 T0.1，生成 `results/<dataset>/remediation_compare.md`。

**T3.4 验收**：按第 5 节逐项核对；不达标的回到对应任务，并在对比表里写明原因。

### 阶段 4：MIMIC ICU 模块全部纳入（D-R13）

分四批纳入，每批按阶段 3 的流程重建、对比、验收。

| 任务 | 内容 | 要点 |
|---|---|---|
| T4.1 | `icustays` → `visit_detail`（挂在所属住院下） | 依赖 T1.13 |
| T4.2 | `inputevents`、`ingredientevents` → 给药事件，带 `rate`/`rate_unit`、剂量与持续时间 | 与 eMAR 的肝素等速率口径对照 |
| T4.3 | `chartevents` → 测量事件（d_items 查表得名称，`valueuom` 作单位）；`outputevents` → 测量事件；`procedureevents` → 操作事件（带结束时间）；`datetimeevents` → 以日期时间为值的 observation 事件 | 最大的一批：<br>· 先在 `--sample` 上验证内存与时间；<br>· 逐 itemid 生成单位与合理范围声明，走审核；<br>· 按 itemid 统计零产出和隔离比例 |
| T4.4 | `caregiver` 写入 `out_of_scope`（照护者标识，不是临床事实） | — |

**阶段 4 验收**
- 各 ICU 表"读入 = 事件 + 声明过滤 + 隔离"；
- 零产出为 0；
- 合理性和单位覆盖率达标；
- 重建时间和峰值内存记录在对比表中。

### 阶段 5：文档、论文与记录

| 任务 | 内容 |
|---|---|
| T5.1 | `docs/DECISIONS.md`：18 项决定各一节，注明由研究团队（Xinye）于 2026-09-13 决定；D-R13、D-R15 注明与推荐不同及理由 |
| T5.2 | `README.md` 更新数字表；`docs/FAULT_CATALOGUE.md` 更新故障数和结果 |
| T5.3 | 论文 `main.tex` 更新数字和校验描述，同步更新 `main_zh_tldr.tex` |
| T5.4 | `docs/WORLD_MODEL_READINESS.md` 标注已解决的缺口（ICU 模块、给药速率、就诊层级等） |
| T5.5 | 在本文档末尾追加"执行记录"：每个 `P-*` 的最终状态（已修 / 不修 / 推迟）及对应提交 |

---

## 5. 验收标准

### 5.1 三个数据集共同

| 指标 | 修复前 | 目标 |
|---|---|---|
| 阶段 0 的新检查 | 多项失败 | 全部通过（已声明的预期项除外） |
| 有源单位或声明单位的数值测量中，单位概念非 0 的比例 | 0% | ≥ 95% |
| 未声明的原始表和原始列 | 未统计 | 0 |
| 现有检查 | 全部通过 | 不退化 |
| `--workers 1` 与 `--workers 4` 输出哈希 | 一致 | 一致 |
| 新增故障检出率 | — | 9/9 |

### 5.2 CU-CTPA

| 指标 | 修复前 | 目标 |
|---|---:|---|
| 药物事件数 | 3,088,590 | 约 4,195,000（全字段相同的 11,263 行仍合并） |
| 体温规范化数值超出合理范围 | 67,750 条标错单位 | 0（原值保留，标记单位被覆盖） |
| OMOP 剂量单位为空 | 3,088,590 | 仅限源里 `dose_unit` 为空或为 `*Unspecified` 的行 |
| 笔记事件数 | 2,664,292 | 约 1,728,000；同人同日同类型同全文的重复为 0 |
| ICU 住院事件 | 39,148 | 39,325（同日不同天数的 177 行各自成为住院） |
| CPT 事件 | 718,261 | 718,261，其中 97,183 条带 `BILLING_DUPLICATE` |
| 被当成测量的下拉选项行 | 14 | 0 |
| 影像检查清单 | 0 | 168,046 个 accession，其中约 7,500 个带原始 Study Date |

### 5.3 JHU CTPE

| 指标 | 修复前 | 目标 |
|---|---:|---|
| 医嘱事件数 | 8,479,129 | 约 9,800,000 |
| 跨分区合并组数 | 3,585,115 | 不减少 |
| 发布的诊断中来自 Deleted 行的 | 有（源 30,069 行） | 0 |
| 未处理的诊断状态冲突 | 45,348 组 | 0（Active 与 Resolved 并存的已标记） |
| 就诊概念为 0 的比例 | 34% | < 5% |
| 心电图、肺功能数值有声明单位 | 0 | 100% |
| 被误拆成"数字 + 单位"的文本 | 26 | 0 |
| 随访事件 | 0 | 覆盖 22,980 名已发布病人中有随访记录的全部病人（源 30,968 行，跨分区合并后每人一条） |
| ICU 转科 `visit_detail` | 0 | 规范层与 MEDS：源 1,523,224 行跨分区去重后 946,025 条全部发布；四组格式指纹一致。**OMOP 只能发布挂得上就诊的那部分**（实测约 19%）：CDM 5.4 的 `VISIT_DETAIL.visit_occurrence_id` 是 NOT NULL，挂不上的不是可以"标记后发布"的行，而是 OMOP 放不下的行，只能记 `VISIT_DETAIL_UNPARENTED` 并留在规范层。这一条修正了 D-R14 原本"找不到则标记"的读法（2026-09-14 实测） |

### 5.4 MIMIC-IV

| 指标 | 修复前 | 目标 |
|---|---:|---|
| OMOP DEATH 行数 | 26,899 | 38,300 |
| MEDS 死亡事件数 | 49,703 | 38,302（38,300 人各一条，1 例冲突保留两条并标记） |
| ED 生命体征事件 | 0 | 等于准备清单记录的非空生命体征值数 |
| 分诊事件 | 0（2,849,786 个值被隔离） | 全部发布并带 `TIME_FALLBACK` |
| 药敏事件 | 0 | 约 1,410,000 |
| UNKNOWN 零时长就诊 | 546,196 | 0 |
| ED 就诊有就诊概念 | 0% | 100% |
| `visit_occurrence` 就诊概念为 0 | 86% | < 5%（转科、换服务改走 `visit_detail` 后） |
| OMOP 处方剂量单位为空 | 约 1,840 万 | 仅限源里 `dose_unit_rx` 为空的行 |
| 被合并的处方/药房组中 pharmacy_id 不同的 | 803,922 + 161,080 + 1,526,725 | 0 |
| 带速率的给药事件 | 0 | 源 2,104,436 行全部带 `rate`；OMOP `sig` 含速率文本且无超长 |
| 因无药名隔离的 eMAR / 药房行 | 2,120,873 / 1,137,574 | 约 657,716 / 约 68,452 |
| ED 事件就诊号能对上 ED 就诊 | 未统计 | 100%（声明了关联的 source） |
| ICU 模块（阶段 4） | 未读 | 见阶段 4 验收 |

---

## 6. 追踪矩阵

| 问题 | 任务 | 检查 | 决定 | 验收 |
|---|---|---|---|---|
| P-C1 | T1.1、T2.M1 | `DUPLICATES_AGREE` | — | 5.1–5.4 |
| P-C2 | T1.2、T1.3、T2.CU3–5、T2.J4、T2.M12 | `DUPLICATES_AGREE` | D-R2–D-R6 | 5.2–5.4 |
| P-C3 | T1.5、T2.J6 | `UNIT_CONCEPT_COVERAGE` | — | 5.1 |
| P-C4 | T1.6、T2.CU2、T2.M7 | `UNIT_VALUE_PLAUSIBLE`、`UNIT_HOMOGENEOUS_PER_CODE` | D-R1、D-R10、D-R17 | 5.1、5.2 |
| P-C5 | T1.7、T2.J3、T2.M10 | `UNIT_CONCEPT_COVERAGE` | — | 5.1、5.3 |
| P-C6 | T1.4、T2.CU1 | `DOSE_UNIT_CARRIED` | — | 5.2、5.4 |
| P-C7 | T1.8 | `DEATH_PUBLISHED` | D-R11 | 5.4 |
| P-C8 | T1.9 | `UNIT_KNOWN` | — | 5.3 |
| P-C9 | T1.10 | `CODE_DESCRIPTION_IS_REPRESENTATIVE` | — | 5.1 |
| P-C10 | T0.2、T1.11、T2.CU1、T2.J5、T2.M11、T4.4 | `RAW_COVERAGE_DECLARED` | D-R12、D-R13 | 5.1 |
| P-C11 | T0.2、T0.3、T0.4 | 全部新检查 + 故障注入 | — | 5.1 |
| P-C12 | T1.13 | `VISIT_CONCEPT_COVERAGE` | D-R14 | 5.4 |
| P-C13 | T1.4、T1.11、T2.M8 | `RAW_COVERAGE_DECLARED` | D-R15 | 5.4 |
| P-CU1 | T1.1 | `DUPLICATES_AGREE` | — | 5.2 |
| P-CU2 | T1.6、T2.CU2 | `UNIT_VALUE_PLAUSIBLE` | D-R1 | 5.2 |
| P-CU3 | T1.4、T2.CU1 | `DOSE_UNIT_CARRIED` | — | 5.2 |
| P-CU4 | T1.2、T1.11、T2.CU3 | `NOTE_TEXT_UNIQUE` | D-R2 | 5.2 |
| P-CU5 | T2.CU7 | `RAW_COVERAGE_DECLARED` | D-R16 | 5.2 |
| P-CU6 | T1.1、T2.CU5 | `DUPLICATES_AGREE` | D-R4 | 5.2 |
| P-CU7 | T1.2、T2.CU4 | `DUPLICATES_AGREE` | D-R3 | 5.2 |
| P-CU8 | T2.CU1 | `RAW_COVERAGE_DECLARED` | — | 5.1 |
| P-CU9 | T2.CU6 | `UNIT_VALUE_PLAUSIBLE` | — | 5.2 |
| P-CU10 | T1.6、T2.CU6 | `UNIT_VALUE_PLAUSIBLE` | D-R17 | 5.2 |
| P-CU11 | T2.CU2、T2.CU8 | `INPUT_MANIFEST_COMPLETE`（扩展到准备步骤） | — | 5.1 |
| P-CU12 | T2.CU3 | `ENCOUNTER_RESOLVES`（声明预期可关联率） | D-R2 | 5.2 |
| P-J1 | T1.1 | `DUPLICATES_AGREE` | — | 5.3 |
| P-J2 | T1.2、T2.J4 | `DUPLICATES_AGREE` | D-R6 | 5.3 |
| P-J3 | T1.3、T2.J4 | `DUPLICATES_AGREE` | D-R5 | 5.1 |
| P-J4 | T2.J5、T2.J7 | `RAW_COVERAGE_DECLARED` | D-R12、D-R14 | 5.3 |
| P-J5 | T2.J1 | `VISIT_CONCEPT_COVERAGE` | — | 5.3 |
| P-J6 | T2.J2 | `RAW_COVERAGE_DECLARED` | — | 5.1 |
| P-J7 | T1.7、T2.J3 | `UNIT_CONCEPT_COVERAGE` | — | 5.3 |
| P-J8 | T1.9 | `UNIT_KNOWN` | — | 5.3 |
| P-J9 | T1.5、T2.J6 | `UNIT_CONCEPT_COVERAGE` | — | 5.1 |
| P-J10 | T1.1 | `DUPLICATES_AGREE` | — | 5.3 |
| P-J11 | T1.11、T2.J4 | `EXCLUDED_STATUS_NOT_PUBLISHED` | D-R6 | 5.3 |
| P-J12 | T2.J5 | `RAW_COVERAGE_DECLARED`（声明为不纳入并注明原因） | D-R12 | 3.3 |
| P-M1 | T1.1、T2.M1 | `DUPLICATES_AGREE` | — | 5.4 |
| P-M2 | T2.M2 | `SOURCE_YIELDS_EVENTS`、`UNIT_VALUE_PLAUSIBLE` | D-R17 | 5.4 |
| P-M3 | T2.M3 | `QUARANTINE_SHARE_DECLARED` | D-R7 | 5.4 |
| P-M4 | T4.1–T4.4 | `RAW_COVERAGE_DECLARED` | D-R13 | 阶段 4 验收 |
| P-M5 | T1.8 | `DEATH_PUBLISHED` | D-R11 | 5.4 |
| P-M6 | T2.M4 | `DUPLICATES_AGREE` | — | 5.4 |
| P-M7 | T2.M5 | `VISIT_CONCEPT_COVERAGE` | D-R8 | 5.4 |
| P-M8 | T2.M6 | `VISIT_CONCEPT_COVERAGE` | D-R9、D-R14 | 5.4 |
| P-M9 | T1.6、T2.M7 | `UNIT_HOMOGENEOUS_PER_CODE` | D-R10 | 5.1 |
| P-M10 | T1.4 | `DOSE_UNIT_CARRIED` | — | 5.4 |
| P-M11 | T1.4、T1.11、T2.M8 | `RAW_COVERAGE_DECLARED` | D-R15 | 5.4 |
| P-M12 | T1.11、T2.M8 | `RAW_COVERAGE_DECLARED` | — | 5.1 |
| P-M13 | T1.11、T2.M8 | `RAW_COVERAGE_DECLARED` | — | 5.1 |
| P-M14 | T1.1、T2.M9 | `DUPLICATES_AGREE`、`ENCOUNTER_RESOLVES` | — | 5.4 |
| P-M15 | T2.M9 | `ENCOUNTER_RESOLVES` | — | 5.4 |
| P-M16 | T1.3、T2.M12 | `DUPLICATES_AGREE` | D-R5 | 5.1 |
| P-M17 | T1.7、T2.M10 | `UNIT_CONCEPT_COVERAGE` | — | 5.1 |
| P-M18 | T2.M11 | `RAW_COVERAGE_DECLARED` | — | 5.1 |
| P-M19 | T1.2、T2.M12 | `DUPLICATES_AGREE` | D-R4 | 5.1 |
| P-M20 | T1.13、T2.M6 | `VISIT_CONCEPT_COVERAGE` | D-R14 | 5.4 |
| P-M21 | T2.M13 | `QUARANTINE_SHARE_DECLARED` | D-R18 | 5.4 |
| W1、W2 | T0.2、T1.11 | `QUARANTINE_SHARE_DECLARED`（声明为预期） | — | 5.1 |
| W4 | — | `END_TIME_NEVER_PRECEDES_START`（现有） | — | 现有 |
| W5 | — | `MEDS_AVAILABILITY_PREVENTS_LEAKAGE`（现有） | — | 现有 |
| W6 | T0.2 | `NOTE_TEXT_UNIQUE`（只报告） | — | 5.1 |
| W7 | T2.M11 | `RAW_COVERAGE_DECLARED`（声明为忽略） | — | 5.1 |

---

## 7. 风险与对策

| 风险 | 后果 | 对策 |
|---|---|---|
| 身份加入剂量后，跨批写法差异把真重复拆开 | JHU 事件数虚增 | 剂量先规范化再算哈希；验收要求跨分区合并组数不减少 |
| 单位换算与单位覆盖本身是判断 | 换错会污染规范化数值 | 只做精确换算；覆盖必须带数值证据并记入决定；原值始终保留 |
| 配置模型改动触发全量重建 | 三个数据集都要重新 ingest | 字段集中在 T1.11 一次加完；重建集中在阶段 3 |
| MEDS 新增扩展列（规范化数值、速率、操作类型等） | 下游读取和 in.json 构建受影响 | `dataset.json` 版本提升并列出新增列 |
| 全部纳入 ICU 模块（D-R13） | chartevents 体量大，重建时间、内存、磁盘显著增加；数千个 itemid 的单位与范围需要声明 | 分四批纳入；先用 `--sample` 测资源；按 itemid 自动生成声明草稿再审核；计时前检查残留进程 |
| `sig` 被借用存放速率（D-R15） | 与现有"未解析剂量文本"共用字段，可能超长或难以解析 | 固定拼接格式和长度上限；在 DECISIONS 写明借用；速率的结构化数值以 MEDS 列为准 |
| 按 `pharmacy_id` 找回药名（D-R18） | 关联的处方与实际给药可能不完全一致 | 打 `NAME_FROM_LINKED_ORDER` 标记；清单记录找回比例；抽样对比 eMAR 与处方的药名一致率 |
| 影像元数据含身份信息 | 泄露风险 | 准备脚本只投影 accession、原始 Study Date、序列描述、路径 |
| JHU 新纳入表的分组格式差异 | 泄漏 has/no 标签 | T2.J7 先做分组格式指纹比对 |
| 阈值（隔离比例、合理范围、可关联率）定得不当 | 误报，或被调松以求通过 | 阈值写在 yaml 或参考 CSV 并附依据，改动走决定记录 |
| 构建期间误改配置或有残留进程 | 构建作废或 OOM | 遵守第 1 节第 6 条；T3.1 开工检查 |

---

## 8. 顺序与工作量

```text
阶段 0（1–2 天）
  → 阶段 1（5–7 天，含 visit_detail）
  → 阶段 2（5–7 天，含 JHU 随访与 ICU 转科、CU 影像清单、MIMIC 药名找回）
  → 阶段 3（1–2 天）
  → 阶段 4（2–4 周，ICU 模块分四批）
  → 阶段 5（1 天）
```

阶段 0–3 约 3–4 周，阶段 4 另计。阶段 0 和阶段 1 可以并行推进；阶段 2 中 T2.J7、T2.M6 依赖 T1.13。

---

## 附录 A：审计方法与局限

- **数据来源**：各数据集 `ehr2cdm_work/<dataset>/` 下的 `omop/omop.duckdb`（`evt`、`lnk`、OMOP 表、`etl_audit`）、`canonical/quarantine.parquet`、`manifest/inputs.json`、`runs/validation.json`、source 层 parquet、准备脚本输出 parquet，以及 JHU `All_kinds/` 和 CU 影像目录的元数据文件。全部只读访问。
- **合并分析**：按事件身份用到的字段对源行分组，统计组内其他字段的不同取值数。JHU 另外只在同一分区内分组，以区分"不同记录被合并"和"29/29b 真重复"。
- **分组对应**：`All_kinds/` 文件与 CTPE 分区的对应，用"文件中 MRN 在各分区出现的比例"判断；ICU 病程记录只扫描前 40 万行。
- **时间比较**：规范层 `evt.event_time` 是不带时区的 UTC。日期层面的比较先换算到数据集声明的时区（MIMIC 为 America/New_York）；在 DuckDB 中需先 `SET TimeZone='UTC'`。
- **局限**：
  - 分组用的是原始字符串，而转换器按解析后的值计算身份，个别组计数可能略有出入；
  - MIMIC 化验的分组包含被 `row_filter` 丢弃的项目；
  - D-dimer 中位数使用源数值 `valuenum`。
- **可复现性**：本次审计的查询没有保存在仓库里，T0.1 负责固化成工具。

---

## 9. 执行记录（2026-09-14 起）

按 T5.5 要求，逐项记录每个问题的最终状态和证据。每一行的"证据"都来自实际构建，不是预期值。

### 9.1 已合入 main 的改动

| 提交 | 内容 | 对应任务 |
|---|---|---|
| `81ae532` | 配置模型一次加齐：合并规则、身份扩展、单位声明与覆盖、按单位拆码、排除状态、标记列、声明字段、阈值、per-source root；`observation` 与 `visit_detail` 两种事件；16 个质量标记；规范化值/单位、速率、操作、出院去向列。CODE 0.7.0，HASH_RULE 2，SCHEMA 2 | T1.11 |
| `94fcca1` | `docs/DECISIONS.md` 增加 18 项决定各一节 | T5.1 |
| `fe0d3d4` | `tools/remediation_rebuild.py`：按阶段计时、查残留进程、构建前后比对配置哈希 | T3.1 |
| `4820be6` | OMOP/MEDS 发布层：`visit_detail` 表、单位概念查表、剂量单位回退、`sig` 写速率、按数据集时区判断死亡、MEDS 扩展列与 codes 描述 | T1.4、T1.5、T1.8、T1.10、T1.13 |
| `377904e` | 规范层：身份纳入剂量、合并规则、单位规范化与合理范围、数值解析、死亡按本地日期合并 | T1.1–T1.3、T1.6–T1.9 |
| `f1569b6` | 参考表两条规则的调和：两张表对同一写法**一致**时允许重复（不一致才报错）；`ucum` 为空表示"这不是单位"，必须写理由 | T1.6 |
| `f1183bd` | 已声明的非单位不再报 UNIT_UNKNOWN；CU CPT 结束日期加 `null_and_flag` | 见 9.2 |
| `3042dde` | 合并 CU-CTPA 分支：准备脚本逐步记账、`encounter_linked`、影像清单、yaml 声明、单位与合理范围表 | T2.CU1–T2.CU8 |
| `f2e810a` | 合并 JHU CTPE 分支：随访与 ICU 转科准备脚本（先做四组格式指纹）、就诊类型与 ICU 病区映射、单位表、问题列表排除 Deleted、化验参考范围 | T2.J1–T2.J7 |
| `c60c4d1` | 10 个单位写法冲突按 OMOP UCUM 词表有无概念调和 | T1.6、T2.J6 |
| `e8b2a70` | 修正 5.3：挂不上就诊的 `visit_detail` 在 OMOP 中不可发布（`visit_occurrence_id` NOT NULL） | T1.13 |
| `4342a60` | 9.3 合并时判断记录 | T5.5 |
| `53ed6b9` | CU 影像文件与拷贝脚本声明为"已编目、不读取" | T2.CU7 |
| `0540613` | 9.4：CU 与 JHU 最终构建前半段（准备、导入、身份）的实测记录 | T3.2、T5.5 |
| `1f48629` | 合并 MIMIC-IV 分支：33 个 source（含 ICU 模块）、准备脚本 v3（ED 宽表转长表、微生物拆分、药名回填、未读清单）、病区与服务映射 | T2.M1–T2.M13、T4.1–T4.4 |
| `d1c6d70` | 复配医嘱 MAIN/BASE 两行的药名差异用 `keep_all_flag`，新增标记 `NAME_VARIANTS_MERGED`；MIMIC `execution.bucket_count` 改为 256 | T1.2、T2.M1 |
| `d734ae2` | 公共单位表 6 个国际单位与 eGFR 代码改为词表写法 | T1.5、T1.6 |
| `78386bb` | `Emergency Department Observation` 映射改为 9201 | T2.M6 |
| `e5d8fa9` | 9.3 记录 MIMIC 合并带来的四项判断 | T5.5 |
| `4063026` | 合并校验分支：15 项新检查、故障注入、审计工具 | T0.1–T0.3 |
| `2fb3241` | 新旧对比工具 `tools/remediation_compare.py` | T3.3 |
| `3eb3de5` | CU：笔记作者与科室按 `keep_all_flag` 合并，`source_file` 与再入院标记改为忽略列；声明 `UNTIMED_VITAL_STATUS` 与就诊概念阈值 | T3.2（回到 T1.2、T2.CU3） |
| `57b8ddc` | JHU：问题列表与检验名称、医嘱剂量写法按 `keep_all_flag`，参考上下限与遮蔽时长按 `null_and_flag`；七个来源声明就诊号可关联率 | T3.2（回到 T1.2、T0.2） |
| `e03e91f` | `DUPLICATES_AGREE`：可用时间规则只在转换器把早于采集的结果时间挪到采集时间之后仍不一致时才要求留痕 | T0.2 |
| `68a4379` | `UNIT_VALUE_PLAUSIBLE`：转换器标为不合理而扣下的值计入越界数 | T0.2 |
| `2e472bd` | 参考范围进入规范层 `range_low`/`range_high`（schema 3）并由 OMOP 优先采用；按角色键的合并规则经一对多映射落到字段（`duration_masked` → `value_text`） | T3.2（回到 T1.2、T2.J2） |
| `eccaeed` | JHU：参考范围规则改键为 `range_low`/`range_high`；声明 outcome 的 `VALUE_CONFLICT` 隔离与随访工作簿 28a 表不纳入 | T3.2（回到 T2.J2、T0.2） |
| `9dc98a5` | 9.5：CU 正式构建后半段实测；9.3：OMOP 日期口径与 fixture 合入时机 | T5.5 |
| `53d9e18` | 合并 fixture 陷阱：12 个陷阱、`zoned_site` 数据集 | T1.12 |
| `c4b5695` | `CODE_VERSION` 回到 0.7.0，规范层任务哈希折入 `CANONICAL_SCHEMA_VERSION` | T3.2 |
| `4f87ea7` | 9.1、9.3、9.6：参考范围、合并字段与版本哈希三处修复的记录 | T5.5 |
| `8c9ca5b` | `DUPLICATES_AGREE`：时钟换日当天的原始可用时间按转换器自己的函数换算（zoneinfo，fold=0） | T0.2 |
| `041e0ad` | `CODE_DESCRIPTION_IS_REPRESENTATIVE`：按 MEDS 发布器的方式，在每个代码自己的行里给名称排序，并列按字母 | T0.2 |
| `409f68d` | `DUPLICATES_AGREE`：键为 `range_low`/`range_high` 的规则比对 `value_low`/`value_high` 角色的原始单元格 | T0.2 |
| `4308116` | `EHR_DUCKDB_MEMORY_GB`：运维上限，约束分析连接与 OMOP 发布连接的内存 | T3.2 |
| `8ef91a6` | 9.1、9.3、9.6：内存上限、三处检查修正与被内核杀掉的阶段 | T5.5 |
| `3056c48` | 每项检查的结果只取决于构建本身：`QUARANTINE_IS_EXPLAINED` 的原因与计数取自同一次分组，计数相同的行按代码或状态排序 | T0.2 |
| `f82a02f` | 校验只把检查在内存中真正读取的列载入内存；校验自己的三个 DuckDB 连接也遵守运维上限 | T0.2、T3.2 |

### 9.2 CU-CTPA：抽样重建已验证（2026-09-14）

1/200 患者抽样（639 人，74,955 源行），全流程 52 秒，**36 通过 / 4 跳过 / 0 失败**。

| 决定 | 证据（抽样构建的实测值） |
|---|---|
| D-R1 华氏温度 | `Temp` 原值 94.3–102.9，规范化为 34.6–39.4 `Cel`；`unit_source` 仍是源写的 `degree Celsius`；328 条 `UNIT_OVERRIDDEN` |
| D-R2 笔记就诊号 | 笔记事件 7,747 条，**恰好等于**（病人, 日期, 类型, 全文）的分组数；1,904 条 `ENCOUNTER_UNLINKED`，125 条 `ENCOUNTER_FROM_LINKED_ROW`。按此比例全量约 186 万条（5.2 的目标是约 172.8 万） |
| D-R3 院方与专业收费 | 428 条 `BILLING_DUPLICATE` |
| D-R4 ICU 住院天数入身份 | 152 条 `visit_detail` 事件 |
| D-R14 `visit_detail` | 74 条挂到所属就诊，78 条无法挂靠，记为 `VISIT_DETAIL_UNPARENTED`（CU 的 ICU 表没有就诊号，只能按时间包含匹配） |
| D-R17 不合理数值 | 1 条 `IMPLAUSIBLE`（`Temp (in Celsius)` 的 2.3） |
| P-CU1 药物合并 | 19,170 源行 → 19,170 事件（100%，每行一个事件，全字段相同的重复在此抽样中不出现）；修复前是 73% |
| P-C3 单位概念 | measurement 4,811/4,859 带非零 `unit_concept_id`（修复前全为 0） |
| P-C6 剂量单位 | `drug_exposure` 15,725/19,170 带 `dose_unit_source_value`（修复前全为空） |

抽样构建本身发现并修复的两件事：`*Unspecified` 被误报为未知单位（1,769 条），以及 CPT 的院方与专业账单对结束日期不一致（20 组，已改为 `null_and_flag`，冲突值进 quarantine）。

### 9.3 合并时由协调者做出的判断

以下判断不在 18 项决定之内，是合并各分支时出现、按仓库既有约定处理的。每一项都可以推翻，推翻时改对应映射行或参考表行即可。

| 日期 | 问题 | 判断 | 依据 |
|---|---|---|---|
| 2026-09-14 | 单位表：CU 与公共表 25 个写法重复 | 允许两张表对同一写法**一致**；只有不一致才报错 | 数据集表带着本数据集的行数作为依据，删掉会丢证据 |
| 2026-09-14 | 单位表：`*Unspecified` 等 3 个写法不是单位 | `ucum` 留空表示"已看过，不是单位"，必须写理由；不再报 UNIT_UNKNOWN | 否则真正没人看过的写法会被淹没（CU 抽样中占 1,769 条） |
| 2026-09-14 | 单位表：JHU 与公共表 10 个写法不一致 | 5 个是同一量的两种写法（每立方毫米 = 每微升），统一走公共表的换算；5 个以 OMOP UCUM 词表**有概念**的写法为准：`mEq/L`→`10*-3.eq/L`、`pH`→`pH`、`application`→`[App]`、`Act`→`{actuat}`；`Bar` 保持注释 `{bar}`，不映射到同名的压力单位 | 没有概念的单位规范化后什么也得不到；药用的"块"不是压力 |
| 2026-09-14 | CU CPT 院方与专业账单的结束日期不一致（抽样 20 组） | `end_time` 用 `null_and_flag`，冲突值进 quarantine；PCS 同表同规则 | 与 D-R4 对 MIMIC transfers 的处理一致 |
| 2026-09-14 | `mappings/visit.csv`：字符串 `Observation` 被 JHU 映射为 9201 Inpatient Visit，被 MIMIC（transfers.careunit）映射为 581385 Observation Room | 9201，两个数据集共用 | `mappings/` 是所有数据集共用的命名空间，一个字符串只能有一个概念；文件里 2026-09-09 已有"所有 observation 入院类型都是 9201"的约定，JHU 那一行明确权衡过 581385 并因此放弃。MIMIC 工作根的 `review/decisions.csv` 同步改为 9201，避免今后重新 compile 时悄悄改回 |
| 2026-09-14 | `Emergency` 两边都映射到 9203，只有备注不同 | 保留 main 的行 | 概念相同，无分歧 |
| 2026-09-14 | CU 全量准备列出 15,081 个未读文件，其中 14,972 个 NIfTI、48 个 DICOM 侧文件和 4 个拷贝脚本没有任何 `out_of_scope` 覆盖（1/200 抽样只列本批病人的影像，所以抽样时看不到） | 声明为 `imaging_payload` 与 `imaging_copy_scripts` | D-R16：影像已由 `imaging_series.parquet` 按序列编目并记录去标识路径，像素不是表，也不进事件流；声明不进配置哈希，不触发重建 |
| 2026-09-14 | MIMIC 抽样：28 个处方合并事件的药名不同，全是同一 NDC、同一 pharmacy_id 下复配医嘱的 MAIN 与 BASE 两行描述同一袋液体（`Sodium Chloride 0.9%` 的 `Bag` 与 `Floor Stock Bag`） | 两个处方 source 的 `source_name` 用 `keep_all_flag`，新增标记 `NAME_VARIANTS_MERGED` | 合并为一个事件是对的，差别只在容器措辞；`MERGE_CONFLICT` 表示没有声明规则，规则已声明时不该再用它 |
| 2026-09-14 | `Emergency Department Observation`（MIMIC transfers.careunit，全量 101,347 行）原映射 581385 Observation Room | 改为 9201 | 与共享字符串 `Observation` 及所有 observation 入院类型的 9201 约定一致，一个临床含义只用一个概念；先改 MIMIC 工作根的 review 日志，再编译到临时目录并只取这一行 |
| 2026-09-14 | 公共单位表 6 个 UCUM 代码在词表里没有概念（`u[iU]/mL`、`m[iU]/mL`、`m[iU]/L`、`m[iU]`、`k[iU]/L`、`mL/min/{1.73_m2}`，共 22 行；MIMIC 抽样中 4,754 条测量因此单位概念为 0） | 改为词表的写法（`10*-6.[iU]/mL`、`mL/min/(173.10*-2.m2)` 等） | 与 `c60c4d1` 同一原则：解析不到概念的单位规范化后一无所得 |
| 2026-09-14 | MIMIC 纳入 ICU 模块后，按抽样外推规范层在 64 桶、4 进程下需约 390 GB，超过本机 251 GB | `execution.bucket_count` 改为 256 | 运行参数，不进内容地址，只把同样的工作分得更细 |
| 2026-09-14 | CU 笔记全量规范层：同一笔记的拷贝之间作者名不同 792 个、作者科室不同 54 个、`source_file` 不同 967,563 个，都没有规则 | `note_author`、`AuthorService` 用 `keep_all_flag` 标 `NAME_VARIANTS_MERGED`；`source_file` 从 `keep_columns` 移到 `ignored_columns` | 一条笔记一个事件（D-R2）。792 个中 149 个只差大小写或标点、359 个是一方把另一方写全，作者仍在事件上，所有写法留在溯源。文件名是同一笔记跨文件交付的必然差异，准备后的行仍带着它 |
| 2026-09-14 | CU 再入院：186,349 个就诊中 32,932 个由多行合并，5 个标记列不一致（1 年 24,375、6 个月 19,916、90 天 14,684、30 天 7,714、7 天 2,376） | 5 个标记列不再保留，列入 `ignored_columns` | 标记按"住院 × CTPA 扫描"计算，扫描列在导出时被丢弃（`owner_answers.readmission_index_event`）。没有一组标记属于这次就诊本身，保留第一组等于发布一个说不出对应哪次扫描的标签 |
| 2026-09-14 | 5.2 的 CU 药物事件目标"约 4,195,000（全字段相同的 11,263 行仍合并）"复现不出 | 以实测为准：4,206,761 行得到 4,206,761 个事件，不合并任何医嘱 | 准备后的表在 17 个交付列上没有两行完全相同，去掉首尾空白后也没有；只看旧构建带的 14 列也只有 2,789 行重复。11,263 的口径查不到，而"每列都相同才是同一医嘱"的规则已按计划实现（`order_record_key`） |
| 2026-09-14 | CU 的 `visit_occurrence` 只有未标类型的再入院（186,349 行，概念全为 0）；`UNTIMED_VITAL_STATUS` 占 78.0%，没有声明 | `validation.visit_concept_coverage_min: 0` 并写明理由；`demographics` 声明 `UNTIMED_VITAL_STATUS` 上限 0.8 | 再入院不说是哪类就诊，概念 0 是 readmissions 上已记录的决定；ICU 住院走 `visit_detail`，仍按默认阈值。活着的病人没有状态成立的日期，与 JHU（77.4%，上限 0.8）同理。两项都不进配置哈希 |
| 2026-09-14 | JHU 全量规范层：`problem_list` 54,516 个合并条目同一 ICD-10 代码两个名称；`labs` 65 个两个名称，参考下限 2 个、上限 3 个不同；`outcome` 8,032 个就诊两个遮蔽时长；`all_rx` 1,064 个医嘱同一剂量两种写法（数值相同，375 个跨两批） | 名称与剂量写法用 `keep_all_flag` 标 `NAME_VARIANTS_MERGED`；参考上下限与遮蔽时长用 `null_and_flag` | 名称和写法不同不是事实不同：身份本来就按代码、按剂量的数值与单位认定同一事件。同一结果的两个参考范围、同一就诊的两个遮蔽时长互相矛盾，与 ADT 住院同一字段的规则一致（D-R4） |
| 2026-09-14 | JHU 各来源就诊号能对上已交付就诊或 ADT 住院的比例：医嘱 63.7%、给药 80.4%、检验 69.6%、超声 71.0%、心电 88.0%、肺功能报告 12.2%、肺功能数值 17.6%，都低于默认的 95% | 每个来源声明 `expected_encounter_link_rate`，下限略低于实测 | 对不上的就诊号规范格式后没有一个能对上，也没有一个指向别的病人的就诊：交付里没有这些就诊（Outcome 表只有建库围绕的住院），不是解析问题。不进配置哈希 |
| 2026-09-14 | CU 的 ICU 住院按 D-R14 走 `visit_detail`，但 CU 没有就诊号，只能按时间包含挂到就诊；1/200 抽样的 152 个里 78 个挂不上 | 按 5.3 的修正执行：挂不上的在 OMOP 中不发布，记 `VISIT_DETAIL_UNPARENTED`；规范层与 MEDS 保留全部 39,325 个。**待 Xinye 确认** | CDM 5.4 的 `visit_detail.visit_occurrence_id` 不可为空。若要 OMOP 保留全部 ICU 住院，只能让 CU 的 ICU 住院改走 `visit_occurrence`，相当于对 CU 推翻 D-R14，并需要重建 CU |
| 2026-09-14 | OMOP 的日期列按 UTC 日历取日（`CAST(event_time AS DATE)`），日期时间列也是 UTC；fixture 陷阱发现本地晚间的事件在 OMOP 里落到第二天 | 暂保持现状，**待 Xinye 决定** | 实测：JHU 8.9% 的事件日期挪了一天（测量 15.0%、给药 14.8%、就诊开始 17.2%、死亡 4,710 人中 315 人），CU 35.6% 的测量日期挪了一天；只有日期的事件不受影响。改为本地日期只动 OMOP 发布并重跑 OMOP 阶段；MIMIC 若在其 OMOP 阶段开始前决定则无需重跑 |
| 2026-09-14 | fixture 陷阱 4 在 `reference/plausible_ranges/generic_ehr.csv` 加了一行，而参考表摘要覆盖整个 `reference/` 目录并进入每个数据集的规范层地址 | 在三个数据集的规范层按 schema 3 重建之前合入 | 参考范围与合并规则两处修复本来就要求重建全部规范层，先合入则三个 v07 构建共用同一参考表摘要；这一行只作用于 generic_ehr，不改变真实数据集的内容，也没有检查比对摘要。代码版本仍是 0.7.0（见 `c4b5695`），区分修复前后的构建要看运行报告里的规范层 schema 版本或提交号 |
| 2026-09-14 | JHU 化验与 MIMIC labevents 把参考范围映射到 `value_low`/`value_high` 角色，转换器却从不读取这两个角色：JHU 的 OMOP 15,911,634 条测量没有一条带参考范围。规范层的 `value_low`/`value_high` 表示"结果本身写成范围"，把参考范围填进去会让约一半化验结果显示成范围 | 新增规范层列 `range_low`/`range_high`（schema 3）读取这两个角色；OMOP `range_low`/`range_high` 优先取行内参考范围，没有时取配置的范围；JHU 的两条规则改键为 `range_low`/`range_high` | P-J6 的问题是参考范围没有进入输出，只映射不读取等于没修。MIMIC labevents 495,663 组同身份多行里参考范围全部一致，MIMIC 配置不用改 |
| 2026-09-14 | JHU ICU 转科与 outcome 的 `duration_masked` 规则只作用在行负载上：该角色与 `text` 共用 `value_text`，合并按字段找规则时找不到，事件保留幸存行的时长，并记下 18,858 个 `MERGE_CONFLICT` | 角色到字段改为一对多映射，规则落到 `value_text` | D-R4 要求冲突的时长置空并隔离，原实现只隔离不置空 |
| 2026-09-14 | 为了让规范层按新代码重算，先把 `CODE_VERSION` 升到 0.7.1；但它也进入 ingest 与 staging 的地址，规范层随即拒绝三个数据集全部 ingest 输出，包括已跑两小时的 MIMIC ingest | `CODE_VERSION` 回到 0.7.0，规范层任务哈希同时折入已因 schema 3 升级的 `CANONICAL_SCHEMA_VERSION` | 只重算列形状变了的那一层，上游输出不作废：CU 从规范层起重建，JHU 因配置变化从 ingest 起，MIMIC 沿用 0.7.0 的 ingest |
| 2026-09-14 | 另一个 compile 会话发现：`mappings/` 中 12 行（11 个 PFT 后缀行与 ETHNICITY 4271761，均为 2026-09-11）在任何工作根里都没有对应决定；MIMIC compile 在 `SOURCE/Observation` 与 `SOURCE/Emergency` 上因同日冲突退出 1 | 本次构建不经 compile，直接读 `mappings/`，不受影响；记录在案，**待 Xinye 决定** | `Observation` 的现行取值 9201 是上面的协调者判断，可以推翻 |
| 2026-09-14 | 三个正式构建同机并行时内存耗尽：MIMIC 规范层 12 个进程占 153–177 GB，内核杀掉了 CU 的 MEDS 阶段和 JHU 的 OMOP 阶段。每个 DuckDB 分析连接默认占物理内存的一半，OMOP 发布连接沿用 DuckDB 的 80% 默认值 | 新增运维上限 `EHR_DUCKDB_MEMORY_GB`；CU 与 JHU 余下的阶段逐个重跑，每步先等可用内存、在 30 GB 上限下运行，并把这些可重跑的进程设为内核优先回收的对象；校验分支的旧构建基线暂停到 MIMIC 规范层完成 | 杀掉一个可重跑的阶段只损失几分钟，杀掉 MIMIC 规范层要损失数小时；上限不改变任何输出或地址 |
| 2026-09-14 | 校验分支的 MIMIC 修复前基线：旧构建上的校验 18:15 被内核在 201 GB 时杀掉，没有写出结果；运行脚本随后把早先一次运行留在构建里的 `validation.json` 当作 a0d1569 的结果写进基线文件 | 基线文件中 MIMIC 的校验部分改标为"未在 a0d1569 上取得"，旧结果另存并注明来源；脚本改为只拷贝本次运行写出的文件。MIMIC 旧构建的完整审计与校验等机器空闲后重跑 | 在那之前，MIMIC 的新旧对比没有修复前的检查结果；基线文件里的审计部分是真实的 a0d1569 输出，CU 与 JHU 的基线不受影响 |
| 2026-09-15 | JHU 药物医嘱事件修复后为 9,308,574，未达到第 5.3 节"约 9,800,000"的目标（低 5.0%） | 目标算重了，构建是对的：按实测，这一项应为 9,308,574。构建不改，第 5.3 节的目标值保留原样，以本行为准 | 目标是修复前的 8,479,129 加上分区内被合并的约 130 万行，把每一行都当成一个新事件。实测时，旧构建与 v07 按 `source_row_id` 对应，14,418,724 行全部对上。旧构建分区内多出的 1,299,779 行里，809,248 行所在的医嘱在 29 与 29b 两个导出中各有一份；分区内拆开后，两份仍作为真重复跨分区合并，只多出 339,556 个事件。其余 490,531 行多出 489,889 个事件。修复后分区内仍在合并的只剩 1,620 行，全部是同一剂量的不同写法（`keep_all_flag` + `NAME_VARIANTS_MERGED`） |
| 2026-09-15 | MIMIC 校验第二次被内核杀掉：DuckDB 上限 40 GB，197 分钟时仍涨到 250 GB，日志为空。v07 构建有 799,153,396 个事件，是旧构建 304,811,180 的 2.6 倍，因为 ICU 与 ED 源这次才转换。校验把 12 个投影列整张读进内存，约 140 GB | `8353c97`：`Layers.events` 改为按列投影的惰性 parquet 扫描，每项检查只取自己读的行和列，在流式引擎上执行。每项检查的起止时间与峰值内存写入 `runs/validation.progress`。MIMIC 校验 13:25 PDT 重跑 | 列大小取自 parquet 元数据，`event_id` 一列未压缩就有 54.3 GB。检查逻辑不变，全部测试通过；CU 与 JHU 的已有校验结果没有重跑 |

### 9.4 正式构建前半段：准备、ingest、identity（2026-09-14）

正式构建写入新的工作根 `/media/extradrive/Xinye/CCDA_data/ehr2cdm_work_v07`。旧构建原样留在 `ehr2cdm_work/` 作对比基线（已构建的工作根不能移动：清单和阶段摘要记录的是绝对路径）。三个旧构建都用空的 subject 盐（各抽 200 个 subject id 全部可复算），新构建同样为空，所以新旧可按病人一一对比。

规范层、OMOP、MEDS 三个阶段要等校验分支和 MIMIC 分支合入后再跑：它们分别改变检查、共享单位表（其摘要进入规范层地址）和映射，先跑只会作废。

**CU-CTPA**

| 项 | 实测 |
|---|---|
| 准备（全量，127,955 人） | 330 秒；49 个原始输入全部记录 sha256（修复前为 null） |
| 被丢弃并命名的行（P-CU11 中的 129 行） | `person_not_in_T1`：CT 检查 3、药物 50、再入院 6、笔记 70，与审计一致 |
| 逐步对账 | 12 步中 11 步按清单自己的 dropped / added / collapsed 精确闭合：血压拆分 +223,259；T5 交叉连接去重 ICU 319,465、再入院 233,267；影像序列跨批重复 4,053。`flowsheets_undated` 的 rows_read 沿用整张 T2 的计数，而有日期的行记在 `flowsheets` 那一步，仅是显示方式，没有行缺失 |
| 笔记就诊号可关联 | 85,984 行关联、4,006,268 行不关联（D-R2、P-CU12） |
| 影像清单（D-R16） | 168,046 个 accession（5.2 目标 168,046），其中 7,254 个有影像元数据、7,254 个带原始 Study Date（5.2 预计约 7,500）；序列 102,107 条 |
| ingest | 295.6 秒；16,682,059 源行，逐 source 与旧构建完全相同；0 行隔离 |
| identity | 127,955 人，与旧构建相同 |

**JHU CTPE**

| 项 | 实测 |
|---|---|
| 准备 | 复用 JHU agent 用最终脚本做的全量输出：随访 30,968 行、ICU 转科 1,523,224 行，与 5.3 一致；四组格式指纹一致，未使用越过差异写入的开关 |
| ingest | 241.1 秒；75,113,066 源行 = 旧的 73,558,874 + 新增两张表的 1,554,192；旧有 10 个 source 逐一与旧构建相同；0 行隔离 |
| identity | 22,982 人，四个分区人数与旧构建逐一相同：新增两张表没有带进队列以外的病人 |

### 9.5 正式构建后半段：规范层、OMOP、MEDS、校验（2026-09-14）

CU 与 JHU 的配置修正（`3eb3de5`、`57b8ddc`）提交后从 ingest 起整体重建，校验在 `e03e91f` 上运行。三个数据集同机并行构建，另有修复前基线的校验在跑，所以下面的耗时不是基准数字。

**CU-CTPA**

| 项 | 实测 |
|---|---|
| 构建 | ingest 380.9 秒、规范层 384.8 秒、OMOP 231.7 秒、MEDS 195.4 秒、校验 283.6 秒；配置哈希 `e4377227` 构建前后一致 |
| 校验 | 55 项：50 通过、0 失败、5 跳过（队列标签、分区成员、仅给药成为 drug_exposure、出生年可复现、排除状态，CU 都不适用）；修复前 39 通过、11 失败；没有检查由通过变为失败 |
| 合并 | 1,190,671 个事件合并了多行；690,369 处不一致按声明规则处理并留下标记；0 个 MERGE_CONFLICT |
| 5.2 目标 | 药物事件 4,206,761（目标约 4,195,000，见 9.3）；笔记 1,724,808（目标约 1,728,000），同日同文重复 0；ICU 住院 39,325；CPT 718,261，其中 97,182 个由院方与专业账单合并且规则已施加；下拉选项行 0；影像清单 168,046 个 accession，7,254 个带原始 Study Date |
| 单位与剂量 | 957,305 个带单位的测量全部有单位概念；`Temp` 67,660 个与 `Core (Body) Temperature` 90 个按华氏转换并全部标 `UNIT_OVERRIDDEN`，越界值 276 个全部标记；OMOP 剂量单位为空 634,968 行，恰等于源里剂量单位为空的行数 |
| 死亡 | 28,149 人，OMOP 与 MEDS 各 28,149 行 |
| ICU 住院的 OMOP 挂靠 | 18,804 个挂到就诊并发布（概念覆盖 100%）；20,521 个挂不上，不发布，记 `VISIT_DETAIL_UNPARENTED`，规范层与 MEDS 保留（见 9.3 待决） |

### 9.6 尚未完成

CU 的 MEDS 与校验、JHU 的 OMOP、MEDS 与校验：内存耗尽后在上限下逐个重跑，并更新 9.5 中 CU 的数字、补上 JHU 的；MIMIC-IV 的规范层（进行中）、OMOP、MEDS，非哈希声明，以及校验；校验分支在最终提交上重跑旧构建基线；新旧对比文档（T3.3）与验收（T3.4）；README、故障目录与论文数字（T5.2–T5.4，文档分支 `remediation/docs`、论文分支 `remediation-2026-09`）；每个 `P-*` 的最终状态（T5.5）。待 Xinye 决定：OMOP 日期按 UTC 还是本地日历；CU 挂不上就诊的 ICU 住院在 OMOP 中如何发布；`mappings/` 中无决定支撑的 12 行。
