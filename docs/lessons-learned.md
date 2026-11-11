# 经验教训

本文按问题整理已发生的用户故障、审计发现和测试反例。每项说明当前约束与可执行保护，不保留实验流水账；完整实现见[当前架构](architecture.md)。通过一个旧提交的测试不代表当前提交或实际安装包已通过相同验证。

## 当前文件响应曾被全工作区采集阻塞

**来源：用户场景与真实进程复现。** 打开包含六个 Git 仓库的父目录时，整工作区采集受同一个五秒任务期限约束，尚未完成采集就失败，当前文件也被拖住。仅缩小 scope、跳过外部 YAML 或增加超时不能满足“当前文件及关联目录及时可用”的需求。[问题与修复入口：PR #9](https://github.com/lazysnail1024/zed-argocd/pull/9)、[持久索引：PR #12](https://github.com/lazysnail1024/zed-argocd/pull/12)。

当前文档分析与后台发现分开，scanner 游标跨 batch 延续，当前目录和关联任务优先，五秒预算落在后台单文件任务。关闭输入分析后释放文本，只保留领域摘要与索引事实。CLI 的一次性预算保持独立；本次索引不能被描述成取消了所有命令的五秒限制。

保护：[mixed_parent_index_e2e.py](../tests/mixed_parent_index_e2e.py)、[persistent_workspace_e2e.py](../tests/persistent_workspace_e2e.py)、[parent_workspace_faults.py](../tests/parent_workspace_faults.py)。验收既观察首次诊断，又观察后续 Ready、关联能力和实际工作量；首次返回快不足以证明索引最终完整。

## 共享代码后仍曾保留重复状态

**来源：实现审查与有正对照的源码消融。** 接入公共队列和 Index 后，消费者仍维护等待/优先级/队列填充状态，并在领域摘要中重复保留导航 Facts；维护两份状态削弱了提取公共模块的收益。[整理入口：PR #13](https://github.com/lazysnail1024/zed-argocd/pull/13)。

当前 WorkQueue 唯一管理 waiting/running/backlog，Documents 管理接受文本与 OpenBytes，Index 唯一持有持久 Facts；提交后清空 `summary.Facts`。本仓保留 `workRead`，因为“重新取输入还是只重算领域”是消费者策略。删除抽象的依据是剩余职责和行为对照，不是净删行数。

保护：[TestIncrementalFactsOwnershipAndReuse](../internal/lsp/incremental_test.go)及 [shared_lifecycle_mutations.py](../tests/shared_lifecycle_mutations.py)。把有效 Reuse 换成空 Facts 提交会丢掉导航事实和依赖关系；因此“去重状态”不能退化成丢数据。未变化的字节、配置解释和位置需要明确证明，不能只依赖相同语义摘要。

## 文件归属与删除证明不能靠“没看到”

**来源：审计反例和可编译故障变体。** 将被排除但显式打开的 AppProject 放入正常 scope，会泄漏 definition 候选并清除本应存在的未解析诊断；仅“当前文件可以分析”不能授权它成为其他文件的依赖。扫描枚举结束与最终 sweep 之间出现的新文件，也不能因为不在早先枚举结果中就被删除。

当前 CLI 和 Catalog 共用 `selectFile`，排除文件使用隔离 scope；无关 YAML 不生成 Argo CD 事实。Membership 的 pass/observation 与 watcher 接受使用同一锁下的事实，只有成功且完整的当前 pass 能证明成员消失。取消、扫描错误和预算不足都不提供负面证明。

保护：[audit_repair_e2e.py](../tests/audit_repair_e2e.py)、[ablate_audit_repair.py](../tests/ablate_audit_repair.py)的 `excluded-isolation`、[TestWatchCreationBetweenEnumerationAndSweep](../internal/lsp/incremental_test.go)、[shared_lifecycle_mutations.py](../tests/shared_lifecycle_mutations.py)。变异锚点随策略移到 `stream.go` 后也必须更新，不能让失效的注入位置造成虚假的绿灯。

## 取消与交付之间曾丢更新或返回旧结果

**来源：真实 stdio 审计复现。** 扫描开始时就消耗重扫标记，随后被 buffer 编辑取消，会吞掉磁盘更新；scope 刷新后仍用旧输入制作查询证明，会允许已经变化的引用回答交付。后台 batch 又可能在结果提交后被无关通知替代，使已计算诊断未到客户端。

当前有效票据与事件 fence 区分请求、接受和提交；未完成需求不能因取消消失。查询证明来自实际产生回答的 snapshot 和依赖集合，串行交付者再核对。仍有效的领域结果重新排队发布，避免重读已接受字节；跨 root 解析失败仅撤销失败 root 的权威。同路径 root remove/add 曾只登记后台重建，最终 roots 列表未变让 debounce 期间的旧证明仍有效；现在接纳 root 事件时立即推进配置 fence，等待后台之前就撤销旧证明。[project_rename_proof_test.go](../internal/lsp/project_rename_proof_test.go)同时检查新成员在提交前、提交后和 root 重建前后的证明，不依赖 sleep。

保护：[lsp_regressions_e2e.py](../tests/lsp_regressions_e2e.py)、[query_delivery_e2e.py](../tests/query_delivery_e2e.py)、[publication_delivery_e2e.py](../tests/publication_delivery_e2e.py)。测试通过实际文件打开或协议屏障安排事件顺序，断言 stale 拒绝、健康 root 可用和恢复进度；超时或进程异常不能冒充成功检出。

## 有界前台必须把取消后的 worker 也算在内

**来源：前台实现审查与可编译消融。** 仅从请求 ID 表删除已取消请求，不能证明后台执行已经结束；若此时归还准入额度，取消突发仍能创建无界 worker。删除实际退出时的额度归还又会使后续请求永久收到 Busy。

当前四个轻读额度与一个重操作额度独立，prepare/rename、引用和工作区符号使用重操作额度。worker 自己退出时归还预约，取消只撤销协议交付资格；请求 ID 再利用还须匹配原 context。真实 stdio 测试发出 128 个 hover，再取消一批请求并验证恢复，不使用调度延迟注入。把读额度扩大到 128、或删除退出归还，分别使指定准入上界和恢复断言失败。

保护：[navigation_editing_guards_e2e.py](../tests/navigation_editing_guards_e2e.py)、[navigation_editing_ablation.py](../tests/navigation_editing_ablation.py)。这些反例证明保护的必要性，不是所有机器上的时延承诺。

## 重命名既要覆盖旧引用，也不能捕获新名字的旧用法

**来源：独立 stdio 负例。** source ref 的新名字只检查已声明名称时，已有未解析 `$captured/...` 会在重命名后意外获得绑定。前后错误数减少并不能发现这种含义变化。另一个真实反例是同一 YAML 流中的既有 sibling 领域错误：修改前只检查选中文档、修改后检查整个流，会将未变化的错误误判为新增，错误拒绝合法私有重命名。

当前私有重命名要求唯一声明，完整覆盖 Helm `valueFiles` 与 `fileParameters[].path`，新名字不能与任何既有声明或引用冲突。修改前后对称分析整个 YAML 流；只有选中 Application 的私有绑定被改写。别名、共享或无法精确映射的转义来源拒绝修改。AppProject 跨文件重命名另要求完整 closed scope、唯一 namespace 绑定与每个受影响文件的打开版本，并保留整个 scope generation 防止新文件或未解析引用在交付前改变证明。

保护：[navigation_editing_guards_e2e.py](../tests/navigation_editing_guards_e2e.py)覆盖 CRLF、多文档、不在 BMP 内的字符、未变 sibling 错误和意外捕获；[project_rename_e2e.py](../tests/project_rename_e2e.py)覆盖跨文件及 AppSet 模板边界。[navigation_editing_ablation.py](../tests/navigation_editing_ablation.py)和 [ablate_project_rename.py](../tests/ablate_project_rename.py)删除引用采集或唯一声明检查，要求编译成功后命中指定失败断言。

## 有界索引之后的映射与合并也必须有界

**来源：工作区符号实现审查。** 索引已经限制了返回数，消费者却对每个匹配再次扫描该文件所有文档，密集多文档输入仍会产生平方级查找。达到结果上限时直接从 root/scope 循环返回，也会跳过最终排序，让截断结果随枚举顺序改变。

当前通过 Index 分配的 `document:N` 直接定位摘要文档；所有分区统一进入容量 20,000 的最大堆，再做一次稳定排序，返回证明只保留最终文件。AppProject 两种 lookup key 在查询前缀处去重。修复候选同样先按 namespace 前缀过滤再限额，`Truncated` 传播为不完整，不能拿被截断集合证明唯一性或引用全集。

保护：[navigation_editing_guards_e2e.py](../tests/navigation_editing_guards_e2e.py)检查两根逆序输入、多文档精确位置、Project 去重、全局排序和 buffer 更新后的名称撤回；公共 Index 合同保护过滤在截断之前发生。小型功能用例不代替密集资源的内存和工作量测试。

## Schema 验证和编辑候选曾使用两套不同路径解释

**来源：默认自动版本模式的真实 stdio 复现。** Schema 已给出 `SCHEMA_CONSTRAINT`，枚举快速修复却为空：原候选遍历只沿原始 `properties/items`，没有经过自动版本目录的 `$defs` 和组合节点。精确版本有四个动作不代表默认版本也可用。

当前补全和快速修复复用公共 `schemaquery.Values`，字段查询和未知字段检查读取同一份编译后图。产品仍负责版本分支选择与安全修改；公共层保留组合约束、取消和预算，遇到不支持的结构明确失败。抽取时还发现编译后的 Schema 没有保留 `x-kubernetes-preserve-unknown-fields` 注解；本仓仅按编译节点的已解析 Location 读取该注解，避免重新维护另一套 `$ref` 解释器。

保护：[shared_schemaquery_e2e.py](../tests/shared_schemaquery_e2e.py)在默认 auto 与精确版本都验证四候选、resolve、只修改目标标量、诊断消失和旧版本拒绝；[shared_schema_test.go](../internal/analysis/shared_schema_test.go)保护 preserve-unknown-fields 与转义引用；[ablate_shared_schemaquery.py](../tests/ablate_shared_schemaquery.py)移除公共枚举查询后必须仍能编译，并让两个真实 LSP 场景的指定候选断言失败。

## 单文件 workspace 诊断曾被测试误当成 root Ready

**来源：macOS ARM CI 的测试前置条件竞态。** config URI 消费者在 Application 收到 workspace 诊断后立即请求配置 pin；当 root 最后的领域任务尚未完成时，localhost URI 分支得到空候选。该次失败发生在 resolve 之前，其他 URI/身份用例正常，不能据此称为生产环境的 macOS URI 损坏。

pin 要求整个相关 root 完整；`x-argocd-analysis-scope=workspace` 只是发布阶段。修复是在请求前使用已有 trace 等待 `Ready`、`complete: true` 和空队列，未放松 pin 安全条件，也未延长超时。

保护：[TestConfigPinWaitsForRootReady](../internal/lsp/incremental_test.go)使用真实文件、scanner 和 batch，无 sleep 或假时钟；规范与 localhost URI 都先验证未 Ready 时无 pin，再验证 Ready 后原 URI、target 和精确插入文本。[config_uri_e2e.py](../tests/config_uri_e2e.py)保留原 URI/编辑断言，[native.yml](../.github/workflows/native.yml)显式运行 LSP/workspace race 测试。

## 有效值、原始文本与未知状态曾混用

**来源：语义和交互审计复现。** 下列输入曾暴露不同边界错误，当前分别保留针对性保护。

| 已发生问题 | 当前约束 | 回归入口 |
| --- | --- | --- |
| ApplicationSet `templatePatch` 的 `spec: null` 或 scalar/array 导致向非 map 写入，CLI panic 或 LSP 退出 | 明确处理 merge 删除和非法类型，先构造有效合并目标再恢复受保护 project，保留输入错误 | [semantic_regressions_e2e.py](../tests/semantic_regressions_e2e.py)、[appset_overrides_e2e.py](../tests/appset_overrides_e2e.py) |
| repository path 只取原始 YAML，漏掉 patch/override 后的有效 path；未采样被当作不存在 | 路径提取与领域分析共用有效 AppSet 变体，存在、不存在、未检查分别表示 | [workspace_regressions_e2e.py](../tests/workspace_regressions_e2e.py)、[repository_exclusion_e2e.py](../tests/repository_exclusion_e2e.py) |
| 普通 YAML 未保存地改成 Helm 模板后仍按旧分类分析 | buffer、采集和查询共用分类，当前文本改变立即双向重分类，不隐式运行 Helm | [workspace_regressions_e2e.py](../tests/workspace_regressions_e2e.py)、[host_capabilities_e2e.py](../tests/host_capabilities_e2e.py) |
| 截断到光标前恢复不完整 YAML，隐藏后方重复键或多行标量，产生错误确定跳转 | 恢复前验证相关全文结构和标量完整范围；未知只用于有边界的只读结果，不能成为编辑依据 | [semantic_regressions_e2e.py](../tests/semantic_regressions_e2e.py)、[interaction_remediation_e2e.py](../tests/interaction_remediation_e2e.py) |
| URI 直接取 Path 导致 symlink/Windows drive 成员查找失败，热编辑退回采集 | 规范文件身份与客户端 URI 分开，别名改变重新确认身份 | [workspace_regressions_e2e.py](../tests/workspace_regressions_e2e.py)、[incremental_performance_e2e.py](../tests/incremental_performance_e2e.py) |

可靠的结果要同时保留“值是什么”“值从哪里来”“这个结论是否已证明”。只要丢掉任一层，Schema 通过也不足以保证导航或编辑正确。

## 输入大小小不代表展开和保留成本小

**来源：预算边界测试与原生 CI。** YAML alias、重复 AppSet override 或大 patch 可以在源文本限制内构造大量有效值；只限制最后摘要不能限制中间构造。摘要还计入完整绝对路径：参考负载在短工作路径可通过原 64 MiB 限制，在较长 CI artifact 路径进入 Limited。

当前在构造变体前计算节点和展开字符串成本，失败不留下部分权威。摘要逻辑预算为 96 MiB，指定大工作区消费者仍保持 512 MiB RSS 门禁并使用长路径；没有通过缩短测试路径或删除归属断言来制造通过。逻辑预算与 RSS 分别验证。

保护：[appset_budget_test.go](../internal/analysis/appset_budget_test.go)、[appset_derived_budget_test.go](../internal/analysis/appset_derived_budget_test.go)、[summary_test.go](../internal/analysis/summary_test.go)、[persistent_workspace_e2e.py](../tests/persistent_workspace_e2e.py)。

## 公共解码优化必须保留原始输入的证明

**来源：公共提供方与 Workflow 的 128 MiB 输入边界复现。** 小资源后的大段 ASCII 注释反复进入严格 YAML 投影，消耗了原有五秒分析期限。把文档范围改成最后一个值节点的位置会破坏块标量、位置和严格语法校验；CST 与成熟解码器对 Unicode 换行和 TAB 的解释也存在已复现的差异，不能直接删除看似无效的尾文本。

当前公共 `yamlview.Document.ContentSpan` 只省略由完整 CST 和字符检查证明安全的尾部 ASCII 注释，`yamlvalue.Project` 仍先核对完整原文并使用原有严格解码器。含任意 TAB 的文档保留完整范围，非 ASCII 注释和控制字符也保留给解码器处理。CD 通过既有 `analysis/projection.go` 调用得到该优化，继续使用原始 Snapshot 计算输入预算和位置，保留每次分析 100,000 个投影节点的合计上限；插件不增加另一套裁剪逻辑或投影缓存。

保护：公共合同固定在 [toolkit-design-4.md](../contracts/toolkit-design-4.md)，提供方的注释、Unicode/TAB 和完整原文匹配消费者保护裁剪边界；CD 的 [alias_projection_cli_e2e.py](../tests/alias_projection_cli_e2e.py)、[projection_repair_e2e.py](../tests/projection_repair_e2e.py)、[alias_projection_lsp_e2e.py](../tests/alias_projection_lsp_e2e.py)、[alias_provenance_edges_e2e.py](../tests/alias_provenance_edges_e2e.py)及 [projection_aggregate_e2e.py](../tests/projection_aggregate_e2e.py)分别验证有效值、严格拒绝、精确来源、CRLF/UTF-16 和总预算恢复。资源上限另由 [projection_retention_probe.py](../tests/projection_retention_probe.py)在真实 512 MiB cgroup 中验证，不能以本地语义通过替代。

## 版本覆盖曾漏掉独立 ApplicationSet 安装来源

**来源：版本目录与兼容性复核。** 仅收集 Argo CD 主安装树中的 CRD，漏掉 2.3.x 固定引用的 standalone ApplicationSet addon，导致有效旧模板中的 `ksonnet` 字段被误报。[修复入口：PR #7](https://github.com/lazysnail1024/zed-argocd/pull/7)。

当前目录同时记录 bundled CRD 和固定 addon 引用，并保留不可变 commit/hash；没有确切 CRD 来源的目标不借用新版本。自动模式使用完整版本 Schema 分支，补全/悬停也必须按整分支筛选，否则“验证拒绝不兼容组合，补全却建议该组合”仍是不一致。共享 `$defs` 和按 kind 准备避免启动时展开全部版本。

保护：[multi_version_e2e.py](../tests/multi_version_e2e.py)、[multi_version_test.go](../internal/analysis/multi_version_test.go)、[ablate_multi_version.py](../tests/ablate_multi_version.py)。删除历史分支、对象边界或查询分支筛选的反例是受控实验，不应描述成每项都曾在用户现场发生。

## 测试数量、CI 绿灯和发布身份曾被混为一谈

**来源：测试执行审查、运行失败与隔离实验。** 新增 runner 曾只写进开发文档而未被实际 native CI 调用；深层 AppSet 矩阵因相同 schema/规则重复执行超过 480 秒。Windows 打包验证也曾因重复 build 耗尽 420 秒命令预算，随后去掉重复构建；对应修复见 [PR #7](https://github.com/lazysnail1024/zed-argocd/pull/7)。

当前必需步骤直接进入 [native_ci.py](../tests/native_ci.py)，缺步骤、编译失败、超时、缺报告和 runner 异常都失败。深入 AppSet 场景按确切 Schema hash 与完整 localRules 分组，独立全版本兼容矩阵继续覆盖每个 tag；不能用分组代替版本目录覆盖。性能验证看真实 `diskReads`、`parsedFiles` 和 fallback，不把 `fullScan:false` 或单次耗时当充分证据。

源码删除也有保留反例：Rust 身份校验改成单次 typed JSON 解析，会放宽未知深层字段的输入限制，因此保留原校验；native pin 与 Cargo 版本重新耦合、删除 WASM 身份检查或依赖许可证都被对应消费者拒绝。保护为 [ablate_identity.py](../editors/zed/tests/ablate_identity.py)、[ablate_distribution.py](../editors/zed/tests/ablate_distribution.py)、[native_package_e2e.py](../tests/native_package_e2e.py)和 [native_ci_process_e2e.py](../tests/native_ci_process_e2e.py)。

当前安装入口仍指向已发布的固定版本，源码合并不会自动更新它。发布证明要对应具体 commit、产物摘要和实际平台；原生 CLI/stdio、Rust 安装器、WASM 打包和真实 Zed GUI 各有验收边界。缺少 GUI 执行记录时不标记通过。
