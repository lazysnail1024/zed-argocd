# Zed Argo CD 性能与易用性审计修复方案

本次处理基线为 `33c7dbf376f724479c7c056a64f40b53037ca978`。R-01～R-09 沿用 2026-11-05 复审编号，CD-01～CD-12 对应上一轮审计。修复与测试一起提交；本文中的验收条件是持续门禁要求，具体平台是否通过应以当前提交的 CI 报告为准。

## 修复与回归测试映射

| 复审问题 | 上轮关联问题 | 修复范围 | 必需回归入口 |
| --- | --- | --- | --- |
| R-01 patch 导致进程退出 | CD-06 ApplicationSet | 静态 patch 的类型、空值与 project 保护 | `semantic_regressions_e2e.py` |
| R-02 取消后丢失磁盘更新 | CD-01、CD-07 | 按 root 保留未应用的采集需求 | `lsp_regressions_e2e.py` |
| R-03 交付失效引用 | CD-02 | 使用实际查询快照建立依赖证明 | `lsp_regressions_e2e.py`、`query_delivery_e2e.py` |
| R-04 parser 错误影响其他 root | CD-05 | 失败 root 的结果与预算回滚 | `lsp_regressions_e2e.py` |
| R-05 有效 path 缺失误报 | CD-04、CD-06 | 共用有效 ApplicationSet 投影与明确的存在性事实 | `workspace_regressions_e2e.py`、`repository_exclusion_e2e.py` |
| R-06 未保存的 Helm 转换 | CD-08 | 采集、buffer 更新和查询采用相同分类规则 | `workspace_regressions_e2e.py`、`host_capabilities_e2e.py` |
| R-07 URI 导致热编辑全量读盘 | CD-01 | 规范文件身份与客户端 URI 分离 | `workspace_regressions_e2e.py`、`incremental_performance_e2e.py` |
| R-08 不完整 YAML 歧义恢复 | CD-09 | 恢复前验证整个当前文档的相关结构 | `semantic_regressions_e2e.py`、`interaction_remediation_e2e.py` |
| R-09 回归测试未进入 CI | CD-11 | 必需步骤清单、失败传播、真实故障变异 | `native_ci.py`、`native_ci_process_e2e.py`、`audit_regression_mutations.py` |

## R-01：templatePatch 删除或替换整个 spec

### 问题描述

静态 patch 合并后直接向 `effectiveSpec["project"]` 写入保护字段。`spec: null` 会删除 map，数字、字符串或数组也不能转为 map；这些输入曾使 CLI panic，并能终止整个 LSP 进程。

### 复现方法

在有效 ApplicationSet 上设置 `templatePatch: |`，分别写入 `spec: null`、`spec:`、`spec: 42`、`spec: text`、`spec: []`。通过 CLI 检查，并在同一 LSP 会话中从正常内容编辑到这些输入，再编辑回正常内容。

### 修改方式

在恢复受保护的 project 前区分删除后的空对象和非法类型。null 按合并删除语义处理；不合法的 spec 类型给出结构化错误。交叉复核发现的“null/scalar/array 基模板加 generator override”同样先在克隆对象建立有效的合并目标，保留原始 schema 错误。保留静态删除字段的语义，禁止通过顶层 panic recovery 或静默忽略输入掩盖问题。

### 验收标准

- 所有输入均返回合法协议结果，LSP 会话和其他文件查询继续工作。
- 无效输入有错误说明，恢复正常内容后诊断恢复。
- `destination.name: null` 能消除实际冲突；不删除 name 时真实冲突仍被发现。
- 既有 generator/template 覆盖与所有已锁定 target 的用例继续通过。

## R-02：重扫描被取消后未应用的更新丢失

### 问题描述

旧调度在启动扫描时清除标记。此后 `didChange` 取消扫描，替代任务会使用旧的磁盘快照；Project 更名、创建、删除或配置变化可能等待另一次事件才生效。

### 复现方法

启动带多个应用的工作区，改变磁盘文件并发送 watched-file 事件。在操作系统确认扫描已打开文件后发送 buffer 编辑，取消这一轮扫描；重复取消并观察下一轮诊断及引用结果。

### 修改方式

以 root 为单位记录尚未完成的重新采集需求。只有当前代次的结果被接受后才消耗对应需求；取消、过期结果不能消耗它。没有变化的 root 复用已采集输入。查询也必须考虑尚未应用的磁盘变化。

### 验收标准

- 单次和连续取消后，下一次完成的结果反映磁盘变化，无需补发事件、保存或重启。
- Project 改名、文件创建/删除和配置切换不会被吞掉。
- 已知过期输入不能产生旧 Project 的确定跳转或候选。
- 未变化 root 不重复全量采集；成功后的普通注释编辑保持增量工作计数。

## R-03：scope 变化后查询证明与实际输入不一致

### 问题描述

查询刷新了配置和 scope，但用于交付时校验的依赖集合来自刷新前的结果。新纳入 scope 的 buffer 即使已经修改，旧请求仍可能返回失效引用。

### 复现方法

将两个 scope 合并，发送 references 查询后立即修改新纳入文件的 project。发送一个后续协议请求作为消息已处理的屏障，确认编辑先于引用响应被处理。再覆盖反方向拆分与配置切换。

### 修改方式

从实际产生回答的不可变查询快照建立证明，并捕获其配置、成员、buffer 版本与磁盘 generation。交付前只验证这些真实依赖；代码修改操作继续使用严格的版本检查。

### 验收标准

- 陈旧回答被 `ContentModified` 拒绝或用当前输入重算。
- 独立 scope 的编辑不导致无关只读请求失败。
- Quick Fix 旧版本拒绝、取消请求单次终结及恢复查询仍成立。

## R-04：解析失败跨 root 传播

### 问题描述

超出 YAML 深度或文档数限制仍走全局错误分支，清除了健康 root 的真实诊断，并阻止其 definition、completion 和 references。

### 复现方法

root A 放入有效 Project 和具有 `SCHEMA_TYPE` 错误的 Application；root B 放入深度 257 的 YAML 或 2,002 个文档。分别通过磁盘文件和未保存 buffer 触发，并交换 root 顺序。

### 修改方式

每个 root 建立结果检查点。解析失败时丢弃该 root 的部分输入、scope、诊断和预算占用；保留其他 root 的完整结果。失败原因发布到具体输入，错误 root 不再提供修改候选。取消仍中断整个被取消的任务。

### 验收标准

- A 的真实错误不会被清空，三类只读查询保持有效。
- B 显示明确的未完成原因，输入限制继续生效。
- 修复 B 后自动恢复；root 顺序与 open/disk 两种输入方式均受保护。

## R-05：ApplicationSet 的有效仓库路径未被采样

### 问题描述

原路径索引只读取原始 YAML，忽略 templatePatch 和 generator 继承后的 repoURL、revision、path；领域检查又把未采样的 map 零值解释为路径不存在。

### 复现方法

让 template 指向 `original`，静态 patch 指向真实存在的 `patched`。与等价 Application 对照，并删除、恢复 `patched`；再覆盖修改 repoURL/revision 和 generator 覆盖部分 source 字段并继承其余字段的情况。

### 修改方式

路径采样复用领域分析的有效 ApplicationSet 变体，不另写一套合并语义。缓存按不可变 snapshot 复用引用提取；每次采集比较实际存在性。区分已检查且不存在、已检查且存在、未检查三种情况；动态字段保持未知。

### 验收标准

- 存在的有效路径无误报，真实缺失和目录恢复能更新诊断。
- 修改 URL/revision/path 后与等价 Application 一致，文件系统单独变化也能失效缓存。
- 动态未知和未采样路径不会被断言为不存在。
- 50,001 文件排除测试和带 repository mapping 的增量性能门禁通过。

## R-06：Helm 模板必须保存后才重新分类

### 问题描述

buffer 热更新只替换文本，没有重算是否为未渲染 Helm 模板。普通 YAML 编辑成含模板表达式的文本时出现 YAML projection 错误，保存后的全扫描才纠正。

### 复现方法

在包含 Chart.yaml 的 templates 目录打开纯 YAML，编辑为 `project: {{ .Values.project }}`，不保存，随后编辑回纯 YAML。与直接打开相同模板文本对照。

### 修改方式

提取共享分类规则，保留已跳过文件的成员与目标元数据；更新 buffer 时仅重分类发生变化的输入，并在查询路径采用同一规则。保持旧 snapshot 不变。

### 验收标准

- 无需保存即可双向转换；模板收到 `HELM_UNRENDERED` 提示，不产生普通 YAML projection 误报。
- 同一文本直接打开和编辑到达时一致。
- 转换不重读未变化 YAML，不自动执行 Helm。

## R-07：客户端 URI 与文件身份不一致

### 问题描述

增量路径从 URI 直接取 Path，未采用与服务器相同的 symlink/Windows drive 规范化，导致成员查找失败并回退全扫描。`fullScan:false` 本身不能证明零读盘。

### 复现方法

分别通过规范路径、symlink URI 和 Windows `file:///C:/...` 打开文件并做注释编辑，检查磁盘读取、解析和采集 fallback 次数；继续验证 definition 和实际 textEdit 的 URI。

### 修改方式

共用 URI 到规范文件路径的转换。采集时保存规范身份供成员查找，保留 snapshot 的原始客户端 URI用于协议输出。别名目标变化时重新采集，不能为了缓存命中继承旧目标身份。

### 验收标准

- 常规、可用 symlink 和实际 Windows drive 热编辑均零无关 YAML 读盘。
- 断言真实 `diskReads`、`parsedFiles`、`acquisitionFallbacks`，不只检查请求扫描标志。
- 诊断、definition 与 textEdit 使用正确客户端 URI；symlink 改目标正确失效。

## R-08：截断前缀隐藏重复键

### 问题描述

不完整 YAML 的恢复截断到查询行，然后把前缀中的唯一 project 当成全文唯一绑定。后方重复 project/spec/身份字段可能因此被忽略。

### 复现方法

在同一 spec 写两个 project；先确认完整输入不提供确定跳转，再在无关后缀制造未闭合结构。覆盖重复 spec、身份字段、merge/alias、带引号的键和多文档位置。还需覆盖 `project: team` 后的多行标量延续：真实值为 `team extra` 时，破坏无关后缀不能让它变成 `team`。

### 修改方式

恢复只读候选之前，验证整个当前文档中的相关结构和所选标量的完整范围，无法排除歧义时拒绝恢复。保留单个可靠 project 加无关破损后缀的导航与补全，不将恢复出来的猜测输入用于修改计划。

### 验收标准

- 破坏后缀不能把已有歧义变成确定跳转。
- 可靠区域恢复、未完成字段补全、UTF-16 与多文档坐标继续正确。
- 不完整输入不产生猜测性 textEdit/code action。

## R-09：将修复变成持续门禁

### 问题描述

上轮新增 runner 只出现在开发说明中；原 native CI 的 15 个步骤没有执行它们，CI 通过并不能保护这次修复。

### 复现方法

检查实际工作流调用及 `native_ci.py` 的命令清单。对修复 guard 做可编译的定点变异，检查测试是否拒绝；模拟一个必需 consumer 非零退出和一个必需步骤遗漏。

### 修改方式

在现有五平台 native 工作流调用的入口中直接运行旧修复与新边界测试，预先记录必需步骤 ID。仅当全部必需步骤完成且成功时判定通过。固定 Linux amd64 执行 1,000/3,000 资源、每组 30 次编辑的普通及 repository 工作负载，并运行源码故障变异。

### 验收标准

- 任何断言失败、未执行、编译失败或测试异常均导致 CI 非零，保留日志与 JSON 报告。
- 变异必须成功编译；正常版本通过，移除保护后的版本必须被指定回归测试拒绝。
- 性能门禁检查实际工作量，保留二进制摘要、原始样本、p50/p95；保持原有防抖配置。
- native、bundle/WASM 与真实编辑器验收分开记录。

## 持续验收矩阵

| 层次 | Linux amd64 | Linux arm64 | macOS amd64/arm64 | Windows amd64 |
| --- | --- | --- | --- | --- |
| native 构建、打包、既有协议用例 | 必需 | 必需 | 必需 | 必需 |
| 新语义、工作区、scope/解析隔离边界 | 必需 | 必需 | 必需 | 必需 |
| 扫描开始后取消的 inotify 同步用例 | 必需 | 必需 | 明确不支持 | 明确不支持 |
| 实际 Windows drive URI | 不适用 | 不适用 | 不适用 | 必需 |
| symlink URI | 必需（主机允许时） | 必需（主机允许时） | 必需（主机允许时） | 主机无权限时记录原因 |
| 1,000/3,000 × 30 双工作负载与源码变异 | 固定 runner 必需 | 不重复 | 不重复 | 不重复 |
| bundle 与实际 WASM 构建 | 必需 | 必需 | 必需 | 必需 |
| 真实 Zed 安装与 GUI 操作 | 待人工记录 | 待人工记录 | 待人工记录 | 待人工记录 |

旧问题 CD-03 的 300 候选 Quick Fix、CD-04 的 50,001 文件排除、CD-10 的两类 source ref、CD-12 的配置引导继续由原有真实消费者保护；不因新边界修复而删除这些控制。Quick Fix 还断言每次 resolve 只有一个 safe plan、两次文档分析及两次 scope 领域分析。

真实 Zed 验收仍需记录编辑器版本、OS、PR commit 和 bundle 摘要，并实际完成：安装 development extension；编辑、保存和外部修改后看到正确诊断；进行补全/定义/引用/Quick Fix；切换纯 YAML 与 Helm 模板；验证多 root 隔离。没有执行记录前不得把 GUI 项标记为通过。
