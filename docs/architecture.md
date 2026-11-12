# 当前架构

本仓库实现本地 Argo CD YAML 分析服务和 Zed 适配器。原生 Go 进程识别 `Application`、`AppProject`、`ApplicationSet`，提供 Schema 与静态领域诊断、补全、悬停、文档与工作区符号、定义、引用、受验证的重命名和快速修复。Rust 适配器负责选取、校验并启动原生进程。

本文描述当前检出的源码。[VERSION](../VERSION)与 [native-server-version](../editors/zed/native-server-version)仍为 `0.0.1-alpha.3`；该值本身不能证明已发布 alpha.3 包含当前 `main` 的索引实现。实际运行身份以 `argocd-ls version --json` 的版本、源码提交、构建模式、合同和目录摘要为准；安装包与源码构建的关系见[分发](distribution.md)。

## 模块职责

| 模块 | 当前职责与主要入口 |
| --- | --- |
| [cmd/argocd-ls](../cmd/argocd-ls/main.go) | 进程入口，调用 `cli.Run`，传递标准输出、错误输出和退出码。 |
| [internal/cli](../internal/cli/cli.go) | `check`、`explain-config`、`version`、`serve --stdio` 的参数与报告；[render.go](../internal/cli/render.go)显式调用 renderer，再复用检查报告；平台进程文件约束子进程的退出和清理。 |
| [internal/workspace/workspace.go](../internal/workspace/workspace.go) | 配置、scope、root 归属、规范路径/客户端 URI、稳定文件读取、Helm 分类，以及 CLI 的一次性采集。 |
| [internal/workspace/stream.go](../internal/workspace/stream.go) | 不持有资源文本的 `Catalog`；`Prepare` 读取配置，`Select`/`selectFile` 统一流式与一次性文件选择，`File` 分类，`LocalPaths` 只检查已知本地仓库依赖。 |
| [internal/workspace/pins.go](../internal/workspace/pins.go) | 根据配置的具体结构和已验证版本生成 pin 候选，不决定 LSP 交付时机。 |
| [internal/lsp/server.go](../internal/lsp/server.go) | framed stdio transport、能力协商、串行协议所有者、buffer 版本、取消、文档分析、诊断交付与修改请求。 |
| [internal/lsp/incremental.go](../internal/lsp/incremental.go) | 会话级持久索引；配置、文件与领域摘要，分批扫描、优先级选择、工作量预算、scope 完整性、事实提交与关联失效。 |
| [internal/lsp/incremental_query.go](../internal/lsp/incremental_query.go)、[pins.go](../internal/lsp/pins.go) | 组合当前请求文本与已提交摘要；构造并复核查询依赖；配置 pin 执行完整范围的修改前后验证。 |
| [internal/lsp/rename.go](../internal/lsp/rename.go)、[project_rename.go](../internal/lsp/project_rename.go)、[symbols.go](../internal/lsp/symbols.go) | 私有重命名证明当前文档；Project 重命名取得整个 scope 摘要与 generation，再重取受影响的打开 snapshot 并生成版本化 `documentChanges`。工作区符号从 Index 和紧凑摘要取得位置，以固定容量堆合并并复核返回文件 revision。 |
| [internal/assets](../internal/assets/catalog.go) | 嵌入运行时版本目录、Schema、默认 `auto` 目标和 readiness；[schema.go](../internal/assets/schema.go)使用本地数据编译，禁止运行时网络加载。 |
| [internal/buildinfo](../internal/buildinfo/buildinfo.go) | 原生产品、版本、源码、合同、目录和构建身份，供 CLI、打包器与适配器核对。 |
| [editors/zed](../editors/zed/README.md) | 独立 Rust/WASM 包；`src/lib.rs` 对接 Zed，`src/distribution.rs` 管理下载/缓存/激活，`src/identity.rs` 校验原生身份。 |
| [tools](../tools/) | `collect_catalog.py` 采集版本来源，`normalize_schemas.py` 生成与接纳目录，`vendor_c.py` 恢复/核对 native parser 来源，`native.py` 构建打包，`release.py` 验证发行身份和聚合平台产物。 |
| [tests](../tests/)、[.github/workflows](../.github/workflows/) | 真实 CLI/stdio 消费者、资源与工作量门禁、源码故障对照、归档和原生/适配器隔离验证；可执行入口见[开发说明](../DEVELOPMENT.md)。 |

### 领域分析内部

`internal/analysis` 接收不可变输入，不执行文件系统或 LSP I/O。公共仓库不含这些 Argo CD 规则。

| 文件 | 职责 |
| --- | --- |
| [schema.go](../internal/analysis/schema.go) | `Analyzer` 识别资源、准备 Schema、组织文档分析与预算；未识别为 Argo CD 的 YAML 不进入 Argo CD 领域检查。 |
| [projection.go](../internal/analysis/projection.go) | 调用公共 `yamlvalue` 投影 alias/merge 有效值和来源；本仓决定 Argo CD GVK 归属、累计预算及 ApplicationSet override 来源，防止把派生值作为可直接编辑的文本。 |
| [appset.go](../internal/analysis/appset.go)、[appset_budget.go](../internal/analysis/appset_budget.go) | ApplicationSet 有效模板、generator override、受支持的静态 `templatePatch` 与构造前的展开预算。动态表达式保留未知状态。 |
| [domain.go](../internal/analysis/domain.go)、[local_paths.go](../internal/analysis/local_paths.go) | AppProject 绑定/策略、source 与 destination 检查、多 source 引用及有效仓库路径提取。路径存在性由 workspace 提供。 |
| [summary.go](../internal/analysis/summary.go) | 从分析结果生成脱离 YAML 文本和语法树的领域摘要、语义摘要、导航 Facts、依赖 key 与位置。保留 AppSet 的有效变体和精确性。 |
| [queries.go](../internal/analysis/queries.go)、[schema_queries.go](../internal/analysis/schema_queries.go)、[candidates.go](../internal/analysis/candidates.go) | 文档符号、定义/引用、字段和枚举补全、悬停、Project 候选；Schema 路径与候选统一调用公共 `schemaquery`，本仓选择自动版本分支与产品展示。 |
| [reference_recovery.go](../internal/analysis/reference_recovery.go) | 不完整 YAML 的有限只读恢复；验证整个相关结构及标量范围，拒绝重复键等歧义，不生成猜测性编辑。 |
| [fields.go](../internal/analysis/fields.go)、[fixes.go](../internal/analysis/fixes.go) | 字段名/枚举纠正和 Project 替换候选；按当前 snapshot 生成编辑，并验证修改前后的 Schema 与领域结果。 |
| [rename.go](../internal/analysis/rename.go)、[project_rename.go](../internal/analysis/project_rename.go) | Application 私有 source ref 与同控制平面 namespace 的 AppProject 绑定重命名。声明、引用、来源、完整性和新名字是否捕获旧引用均由本仓证明，公共 `edit` 只负责精确应用计划。 |

## 公共依赖及状态归属

公共依赖为 `github.com/lazysnail1024/k8s-crd-toolkit`，当前源码 pin 为 `v0.0.0-20261112033740-db4adba0093e`，完整提交为 `db4adba0093ee2a709356aa1c2517e6d62644745`。[go.mod](../go.mod)和 [go.sum](../go.sum)固定官方 Go module 来源及校验值，[toolkit.lock.json](../contracts/toolkit.lock.json)与 [toolkit-snapshot.json](../contracts/toolkit-snapshot.json)固定 design-5 合同及运行时文件摘要。构建使用已提交的 [vendor](../vendor/github.com/lazysnail1024/k8s-crd-toolkit/)，无需发布 tag、相邻 checkout 或本地 `replace`。

| 公共模块 | 本仓调用位置与边界 |
| --- | --- |
| `source` | workspace/LSP 接收文本，analysis 使用不可变 `Snapshot`、revision、byte span 和 UTF-16 坐标转换；文件归属与客户端 URI 解释仍由本仓决定。 |
| `yamlview` | `analysis/schema.go` 和投影/查询读取容错 YAML 视图；`Document.ContentSpan` 依据完整 CST 保守识别可省略的尾注释，原始 Span、Raw 与 UTF-16 位置保持不变。公共层不判断 Argo CD kind、AppProject key 或 target 版本。 |
| `yamlvalue` | `analysis/projection.go` 复用有界有效值、alias/merge、来源与可选身份字段提取。`Project` 先核对完整原文，再通过 `ContentSpan` 减少严格解码器重复扫描尾注释的工作；含 TAB 的文档保留完整范围，非 ASCII 注释和无法证明的内容保留在解码输入中。本仓提供并解释身份字段；CLI 按整个请求累计投影预算，LSP 按文件任务累计，打开 buffer 的总字节配额独立检查。 |
| `schemaquery` | `schema_queries.go`、`queries.go`、`fields.go` 和 `fixes.go` 共用编译后的 Schema 图，处理静态引用、组合、字段/数组路径及有限 enum/const 候选。未知结构、取消或预算不足返回明确错误；版本目录、未知字段诊断、来源与修复安全仍由本仓决定。 |
| `edit` | `analysis/fixes.go` 将精确位置组织为 snapshot 绑定的修改计划；是否允许该领域修改、是否可向客户端交付由本仓验证。 |
| `workspace.Documents` | `server.go` 的 Open/Change/Close 和 `incremental.go` 的 BeginRead/PublishRead；统一接受 revision、打开文本及读票据。`fitsBuffer` 读取 `OpenBytes`，不另存一份累计文本计数。 |
| `workspace.WorkQueue` | `incremental.go` 的 Put/Pop/Complete/Remove；拥有排队、运行、重复通知与待补入工作身份。每次 Pop 都通过 `defer Complete` 释放票据。本仓 `workRead` 只区分重新读文件与仅重算领域，不再复制 queue 的等待状态。 |
| `workspace.Scanner` / `Membership` | 增量索引驱动 scanner，记录 pass 和 observation。文件记录保存 observation，公共 Membership 不再复制文件表；观察与 sweep 在同一所有者锁下执行。只有成功完成的当前 pass 可以证明文件已消失。 |
| `index.Index` | 保存唯一一份持久导航 Facts、反向依赖、提交票据与 scope 状态。本仓提交后清空 `summary.Facts`，领域摘要只保留领域需要的值。`SymbolsWithPrefix` 在有界截断前按 opaque key 前缀和名字过滤；Project namespace 编码与截断后的产品行为留在本仓，不在公共锁内调用领域回调。 |

`Index.Reuse` 的调用点在 `incremental.go`：本仓先确认字节 hash、target/配置解释和客户端 URI 均未变化，再给新的有效票据复用已保留事实。公共 Index 不推断“内容未变”，也不替本仓判断解释是否相同。内容/位置变化走重新分析和 Commit；只有语义相同并不意味着旧位置仍然正确。

[toolkit-design-5.md](../contracts/toolkit-design-5.md)是构建核对的合同快照，其 SHA-256 由 [toolkit.lock.json](../contracts/toolkit.lock.json)记录；锁文件、[toolkit-snapshot.json](../contracts/toolkit-snapshot.json)、[vendor-toolkit-sources.json](../contracts/vendor-toolkit-sources.json)及 [vendor-c-sources.json](../contracts/vendor-c-sources.json)连接模块提交、合同字节、运行源码与 native parser 来源。合同和来源清单属于可执行构建约束，不能随说明文字改写。

公共层提供有界的数据结构和有效性规则；worker、timer、watcher 注册、配置、Argo 领域语义、LSP 消息和发布策略由本仓持有。Argo CD 与 `zed-argo-workflows` 各自编译引用公共源码，两者没有直接模块依赖；各个原生进程分别实例化 Documents、Index 和 WorkQueue，不共享运行时索引或缓存，也不因此自动提供跨插件跳转。

## 从启动到可用结果

### CLI 与 stdio

`check` 经 `workspace.Load` 一次性采集选定 root/scope/file，再完成 Schema 与领域分析并写出统一报告。CLI 保留五秒协作式任务预算和累计输入/投影预算，报告 `analysisComplete` 与 `notEvaluated`。`render-check` 只有在显式调用时执行 renderer；编辑器打开文件不会触发 Helm 渲染或集群连接。

`serve --stdio` 经 `lsp.Run` 启动。串行协议所有者接收通知、接受 buffer 版本、管理取消，并校验 worker 返回结果；后台工作只收到不可变输入。当前 capabilities 包括增量文本同步、hover、definition、references、documentSymbol、workspace symbol、completion 和 workspace folders。只有客户端支持版本化 `documentChanges` 才宣告可 resolve 的 code action 与 prepare/rename。当前未宣告格式化或 semantic tokens；普通 YAML 服务可以并行提供自己的能力。具体协商在 [server.go](../internal/lsp/server.go)。

前台最多同时保留四个轻读 worker 和一个重操作 worker。代码动作、resolve、prepare/rename、references 和 workspace symbol 使用重操作额度；hover、completion、documentSymbol 与 definition 使用轻读额度。额度耗尽立即返回可重试错误，不创建无界等待任务；取消请求只撤销交付资格，实际 worker 退出才归还额度，避免取消突发绕过上限。单次请求仍有五秒协作式期限，读取和修改的依赖证明分别校验。

### 当前文件、关联文件和后台发现

打开或编辑文件先触发独立文档分析。它只读取有界 root 配置和必要的 Chart 元数据，不枚举或读取未打开的 YAML，因此当前文档的 Schema 诊断、字段补全、悬停、符号和局部纠错无需等待全工作区完成。新 buffer 版本替换旧文档任务，不重置后台发现游标。

文档路径与后台路径都按单文件计费，同一 YAML 流中的多个文档共用该文件的投影预算。现有最新文档结果最多保留一个文件的完整值图与检查结果；其他文件保留 snapshot 和有界发布诊断，同字节、revision、target 与 root 配置未变时复用诊断。前台查询需要未保留的值图时只重建目标文件及所属 root 的打开配置，仍先检查所有打开文本的 128 MiB 总配额。

后台会话以持久文件记录推进发现：scanner 每批最多 256 项工作，advance 最多处理 32 个文件任务。当前文档所在目录被优先探索，工作队列使用公共层 `Current:Related:Background = 8:4:1` 的加权轮转；已知依赖变化把关联消费者提升到 Related。排队满时保留有界待处理身份，不通过丢事件宣告完成。

每个后台文件任务独立获得五秒协作式预算，整个工作区不共享一个五秒截止时间。大工作区可以持续后台推进，同时发布当前文件结果；操作系统阻塞读取本身不受硬实时取消保证。证据入口是 [mixed_parent_index_e2e.py](../tests/mixed_parent_index_e2e.py)和 [persistent_workspace_e2e.py](../tests/persistent_workspace_e2e.py)。

### 归属与成员

编辑器声明的 workspace roots 是边界。打开包含多个 Git 仓库的父目录时，嵌套 `.git` 只属于扫描排除，不会自动把仓库切成互不可见的分析 root；父目录的 scope、repository mapping 和跨仓库 Project 引用仍按配置生效。显式多 root 的归属由 `Owner` 和规范路径决定。

`selectFile` 是 CLI 与流式索引共同的 scope/exclusion/owner 策略。被排除但显式打开的文件可以获得隔离的文档诊断，其 `isolated:` scope 不向正常 scope 提供 Project 候选或完整性证明。未识别为本产品资源的 YAML 不产生 Argo CD 声明/引用。精确 glob 仍在文件选择时执行；只有无歧义的 `**/目录名/**` 才转换成整个目录的剪枝，避免误剪被包含的后代。

scanner 不递归符号链接目录。显式文件别名以规范路径参与归属、以原客户端 URI 返回诊断与编辑；别名目标变化需要重新采集。以上行为由 [scope_glob_e2e.py](../tests/scope_glob_e2e.py)、[repository_exclusion_e2e.py](../tests/repository_exclusion_e2e.py)和 [workspace_regressions_e2e.py](../tests/workspace_regressions_e2e.py)保护。

## 输入、事实、查询、编辑与交付

| 边界 | 当前规则 |
| --- | --- |
| 文本接受 | didOpen/didChange 经 Documents 接受 snapshot；磁盘事件不能替换打开 buffer。didClose 释放覆盖并请求新读，不恢复早先缓存的磁盘文本。 |
| 事实提交 | watched YAML 到达即撤回旧导航事实，早于 debounce/读盘；worker 必须同时通过 event fence、Documents 读票据和 Index 提交票据，晚到结果不能复活旧位置或删除目标。 |
| 成员收敛 | 每 30 秒触发滚动 reconciliation，读取内容而非只信 mtime/size。活动 pass 中的新请求延续到下一轮；失败/部分扫描不证明缺失。目录事件缺少逐文件通知时由 reconciliation 收敛。 |
| 关联失效 | Index 的反向依赖定位已知消费者；非 YAML 事件涉及 repository path 或 Chart 归属时只重查对应事实和消费者。领域判断与路径采样复用同一份有效 ApplicationSet 投影。 |
| 只读查询 | 只采集当前请求文档，其他文件从脱离文本的摘要/Index 读取；查询不会推进后台 reconciliation 基线或发布诊断。回答携带实际使用的配置、buffer/revision、候选 key、引用 use set 与相关磁盘状态证明，交付前再次验证。 |
| 修改 | 局部字段/枚举纠正与私有 source ref 重命名要求同一接受 revision 和配置，在前后整个 YAML 流及领域检查通过后提供。Project 替换和资源重命名依赖完整 scope；配置 pin 还要求整个相关 root 完成，并重新执行有界的完整修改前后验证。普通编辑热路径不调用该全量验证。 |
| 诊断交付 | scope 不完整时暂缓依赖整个 scope 的 Project 诊断，保留当前文件的 YAML/Schema 诊断。已向客户端交付同 revision/配置的健康 workspace 结果后，较晚 Schema-only 结果不得清空更丰富的诊断。后台失败可恢复当前 buffer 的有效文档诊断；过期 batch 中仍有效的领域结果会重新排队交付，不重读已接受字节。 |

关键实现位于 [incremental.go](../internal/lsp/incremental.go)、[incremental_query.go](../internal/lsp/incremental_query.go)、[server.go](../internal/lsp/server.go)与 [pins.go](../internal/lsp/pins.go)。只读结果失效可以拒绝为 `ContentModified` 或以新输入重算；修改候选不能在版本变化后静默沿用。

已知的定义和补全候选在发现未结束时仍可使用，但保留重复候选和精确性约束。部分索引为空不等于资源不存在。缺失目标、唯一绑定、完整引用集合和跨资源编辑需要完整 scope；无法读取、解析失败、超预算或未知归属都不能被当作“没有资源”。一个 root 的失败不应清空健康 root 的诊断和查询。

### 重命名与工作区符号

私有 source ref 重命名覆盖同一 Application 的 `spec.sources[].ref`、Helm `valueFiles` 和 `fileParameters[].path` 中的 `$ref/...`。同文件其他 YAML 文档不参与该私有绑定；前后安全检查仍对称包含整个 YAML 流，既有 sibling 错误不会被误判为新增错误。重复声明、alias/共享锚点、无法精确映射的转义标量以及新名字已被任何声明或引用使用时拒绝修改。生成的编辑使用接受 snapshot 的字节范围转换为 UTF-16，并附打开版本。

AppProject 资源重命名要求 `referencePolicy: closed`、明确 `controlPlaneNamespace`、完整目标 scope 和唯一同 namespace 声明。受支持的 Application 与 ApplicationSet 有效模板 project 引用一起修改；动态或无法独立映射回源标量的绑定无法证明引用全集。每个受影响文件必须已打开且接受当前版本，先应用全部计划再做整体语义验证。scope 摘要用于确定闭包，修改文件重新取得完整 snapshot；这不允许跨 scope、跨 namespace 或修改未知/关闭版本的文件。

工作区符号直接读取 Index 和脱离文本的摘要，不重新读关闭文件。索引 `document:N` 身份直接定位对应摘要文档；AppProject 的 any-namespace lookup key 只产生一个显示项。前台通过公共 `SymbolsWithPrefixContext` 查询，扫描每个文件和每个符号均计入工作量，零命中同样计费。一次 workspace symbol 请求跨全部 root/scope 和两个前缀共用 1,200,000 次访问额度，覆盖现有 100,000 文件/500,000 符号上限；取消、预算耗尽或扫描分区变化不会把部分候选当成成功结果。

root 顺序、catalog 和单文件摘要仅在短锁内借用；公共索引遍历、符号位置映射和 heap 合并不持有 workspace 所有者锁。等待锁本身可取消。所有分区仍通过容量 20,000 的最大堆合并，再按 URI、源码位置和名字稳定排序。同一 URI 的不同 revision 不能混入一次结果；交付前继续复核返回位置的文件 revision 和 root 配置身份。访问计数仅属于当次查询，不进入交付证明；trace 的 `complete` 表明发现或结果容量边界，符号列表本身不证明完整引用集合。

补全与修复的可选 AppProject 全集查询也在所有者锁外执行，单次上限 600,000 次访问。后台提交使这次候选扫描未完成时，丢弃该扫描的候选并返回明确 incomplete 的当前文档/已知关联结果；Schema 字段补全不等待全工作区稳定。跨资源安全修改仍必须有完整候选与输入证明，不能从这一降级结果取得完整性。

## 内存与工作量边界

关闭文件只保留脱离文本的领域值、候选身份、依赖、byte span 与 UTF-16 range，不保留原 YAML、CST 或完整投影图。AppSet 有效变体保留领域检查需要的值，未知仍是未知。语义摘要与位置分开：注释改动可能只更新位置而不重验反向依赖。

| 资源 | 当前上限 |
| --- | --- |
| 单份 YAML 文本 | 5 MiB |
| 保留的打开文本 | 128 MiB |
| LSP 打开文档 / URI | 2,048 个 / 单 URI 4,096 字节 |
| 会话候选文件 | 100,000 |
| WorkQueue 就绪容量 / outstanding 容量 | 512 / 100,001；额外 1 个只容纳删除后尚未归还的在途 worker 票据，不扩大文件容量 |
| 通知入口不同文件事件 | 512；溢出合并为 reconciliation 请求 |
| 前台请求 | 4 个轻读 + 1 个重操作 worker；每个请求五秒协作式期限 |
| Project 修复候选 / 工作区符号结果 | 按配置 namespace 过滤后的候选 1,000 / 全局符号 20,000；截断不能充当完整修改证明 |
| 投影与 ApplicationSet 展开 | CLI 整次请求、LSP 每个文件任务分别有 100,000 逻辑节点及 64 MiB 展开字符串预算；同一文件内所有 YAML 文档继续累计，在构造大量有效变体前计费 |
| 文件摘要、元数据和诊断 | 96 MiB 逻辑计费 |
| 公共 Index Facts | 96 MiB 逻辑计费、500,000 symbols、1,000,000 references/dependencies |
| scanner 目录 | 每个声明 root 100,000 |

数值来源：[workspace.go](../internal/workspace/workspace.go)、[incremental.go](../internal/lsp/incremental.go)、[server.go](../internal/lsp/server.go)、[projection.go](../internal/analysis/projection.go)和 [appset_budget.go](../internal/analysis/appset_budget.go)。逻辑计费不等于进程 RSS 上限，parser、Schema、查询和 Go runtime 另有占用。后台逐文件消费允许累计关闭输入超过 128 MiB；CLI 的一次性累计预算仍独立存在。AppSet 超限报告 `INPUT_BUDGET_EXCEEDED` 并留下不完整状态，不能靠最终摘要会丢弃大字段绕过构造预算。

## 完整性与验证信号

诊断的 `x-argocd-analysis-scope` 值 `document` / `workspace` 表示发布阶段。`workspace` 也可以对应终态受限结果，单个文件收到它不证明 root 已 Ready，空诊断列表也不证明无错误或完成发现。

开启 `ARGOCD_LS_TRACE=1` 后，`argocd-document-analysis` 与 `argocd-analysis` 记录工作量，不记录源文件内容。持久记录包括 `state`（`Discovering`、`Indexing`、`Ready`、`Limited`）、`complete`、`knownFiles`、`queueDepth`、`discoveryWork`、`summaryBytes`、`diskReads` 和 `parsedFiles`。完整成员验收同时等待 `Ready`、`complete: true`、空队列；有错误或未完成扫描时不能仅凭队列空置为 Ready。

| 要验证的性质 | 当前入口 |
| --- | --- |
| Facts 唯一所有者、Reuse、枚举与 sweep 之间新增文件、pin 等待 Ready | [internal/lsp/incremental_test.go](../internal/lsp/incremental_test.go)、[shared_lifecycle_mutations.py](../tests/shared_lifecycle_mutations.py) |
| 当前文件及时响应、混合 YAML/资源归属、发现中可 resolve 局部编辑 | [mixed_parent_index_e2e.py](../tests/mixed_parent_index_e2e.py) |
| 不变保存零读/零解析、单 watch 局部工作、漏通知、超队列突发和大输入释放 | [persistent_workspace_e2e.py](../tests/persistent_workspace_e2e.py) |
| 查询与诊断交付期间的新事件、root 隔离 | [query_delivery_e2e.py](../tests/query_delivery_e2e.py)、[publication_delivery_e2e.py](../tests/publication_delivery_e2e.py)、[lsp_regressions_e2e.py](../tests/lsp_regressions_e2e.py) |
| 正常热编辑未退化为全量采集 | [incremental_performance_e2e.py](../tests/incremental_performance_e2e.py)，检查实际 reads/parses/domain work/fallback，不只计耗时 |
| source ref 完整修改、UTF-16/多文档隔离、捕获旧引用拒绝、符号排序与版本新鲜度 | [navigation_editing_e2e.py](../tests/navigation_editing_e2e.py)、[navigation_editing_guards_e2e.py](../tests/navigation_editing_guards_e2e.py) |
| Project 跨文件与 AppSet 精确模板修改、namespace/scope 隔离、动态或歧义拒绝 | [project_rename_e2e.py](../tests/project_rename_e2e.py)、[ablate_project_rename.py](../tests/ablate_project_rename.py) |
| 新成员事件、提交后的 scope generation、同路径 root 重建都撤销旧修改证明 | [project_rename_proof_test.go](../internal/lsp/project_rename_proof_test.go)、[navigation_editing_ablation.py](../tests/navigation_editing_ablation.py)；真实索引状态转换，不使用计时延迟 |
| 前台准入有界、取消后额度恢复及保护确实必要 | [navigation_editing_guards_e2e.py](../tests/navigation_editing_guards_e2e.py)、[navigation_editing_ablation.py](../tests/navigation_editing_ablation.py)；变体必须编译成功并使指定协议断言失败 |
| 默认 auto 与精确版本复用同一 enum 候选并可 resolve | [shared_schemaquery_e2e.py](../tests/shared_schemaquery_e2e.py)、[ablate_shared_schemaquery.py](../tests/ablate_shared_schemaquery.py)、[shared_schema_test.go](../internal/analysis/shared_schema_test.go) |

Linux 原生 CI 的参考负载是 50,000 个填充 YAML 加 100,000 个非 YAML 文件，混合 ConfigMap/Application/Workflow，并约束内核峰值 RSS 不超过 512 MiB。它验证指定工作负载的发现和释放，不能推导任意数量的密集 Argo CD 摘要均可装入内存。执行配置在 [tests/native_ci.py](../tests/native_ci.py)。

## CI 分片与重放证据

[native.yml](../.github/workflows/native.yml) 在五个原生平台各运行三个独立 lane：`consumers` 保留真实 CLI/stdio、语义、资源、race、消融和发行 staging 验收；`provenance` 运行真实来源与打包安全反例；`compiler-environment` 运行 normal、flags、search、link、persisted-go 五个编译环境控制。安全 lane 使用已有安全消费者产生的真实 build，不再额外构建一个未被使用的主程序。所有 lane 从空构建缓存开始，五个 compiler case 仍各用独立空 GOCACHE；本地省略 `--suite` 时默认 `all`，完整串行验收仍可使用。

[ci_gate.py](../tools/ci_gate.py) 只接收本次 workflow run/attempt 的十五份报告，并从当前源码重新计算每个平台的必需步骤。它同时要求 Actions 的所有 lane 成功，核对真实 source commit/tree、完整源码 inventory、Go 原生 host/target、编译器与工具摘要、每一步退出及清理状态、五份发布记录对应的实际消费者 binary 摘要。synthetic 编译控制的 commit 与正式来源不同，但源文件 inventory 必须一致。报告检查用于拒绝缺失、错配和错误摘要；信任根仍是确切 checkout、同一次 workflow 的 job 结果和受控的发布权限，不把任意 JSON 当作密码学证明。现有 `release.py collect` 继续验证五平台完整发布包、原始许可证与校验和。

[ci_evidence.py](../tools/ci_evidence.py) 仅打包本仓 `artifacts` 下明确选择的子目录。它保留成功与失败输出、合成源码与 `.git` 历史、成员权限及链接/特殊节点元数据，只排除顶层 Go/module 缓存与五个编译环境 case 的可重建缓存。流式 tar/gzip 归档采用压缩级别 1，上传已有压缩归档时关闭第二次压缩；路径、类型、权限、文件内容和归档 SHA-256 均由 manifest 与流式读回校验，不依赖上传动作的摘要 warning。实现参考 [GitHub 官方 upload-artifact 的压缩与权限说明](https://github.com/actions/upload-artifact#permission-loss)。

归档不跟随 symlink，也不打开 FIFO、socket 或设备节点；验证命令不解压。部分安全反例含绝对链接，因此下载证据后不能不加检查地使用可信模式整包解压；应先运行 `verify`、检查 manifest，再按对应测试命令重新生成夹具。归档与汇总器的正反例分别由 [ci_evidence_e2e.py](../tests/ci_evidence_e2e.py) 和 [ci_shards_e2e.py](../tests/ci_shards_e2e.py) 在原生消费者 lane 执行；合成 gate 报告只验证拒绝逻辑，不冒充真实平台验收。

## 原生进程、编辑器与发行隔离

原生构建只依赖 Go/C、vendor、版本数据与 native 工具；删除编辑器目录后仍可独立构建和打包。Rust 适配器目录可独立构建 WASM，不编译 Go，不包含 Argo CD 分析或原生 executable。`serverPath`、Zed worktree PATH、固定 native release pin 依序决定启动对象；managed 下载先校验 SHA-256，再校验产品、`adapterContractVersion=1`、分析资产 readiness 和确切 release 身份，成功后激活完整 cache generation。

适配器协议版本与公共源码合同 `toolkit-design-5` 是两个边界；前者由 [identity.rs](../editors/zed/src/identity.rs)核对，后者由原生构建来源链核对。身份中的 `analysisReady` 表示内置分析资产可用，不表示某个已打开工作区的索引到达 Ready。

原生版本来自根 `VERSION`，适配器版本来自自身 manifest/Cargo，native pin 来自 `native-server-version`，三者分别有用途。`native.yml` / `release.yml` 验证原生平台产物；`zed.yml` / `zed-release.yml` 验证 WASM、适配器包和确切 native pin；发布输入分别是 [native-release-notes.md](native-release-notes.md)与 [editors/zed/release-notes.md](../editors/zed/release-notes.md)。

隔离回归在 [native_isolation.py](../tests/native_isolation.py)、[ablate_distribution.py](../editors/zed/tests/ablate_distribution.py)和包验证消费者中执行。CLI/stdio、安装器、WASM 和归档验证各自证明相应边界，不能替代真实 Zed GUI 操作记录。已发生的边界失误见[经验教训](lessons-learned.md)。
