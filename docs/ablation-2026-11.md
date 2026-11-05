# Zed Argo CD 消融实验与精简记录

日期：2026-11-05（Asia/Singapore）。基线为
[`7722ae595eb4d02864b0eb517b2225918a81cd92`](https://github.com/lazysnail1024/zed-argocd/commit/7722ae595eb4d02864b0eb517b2225918a81cd92)，
该提交的[原生 CI](https://github.com/lazysnail1024/zed-argocd/actions/runs/41992473846)已成功。
可检查的[实验数据摘要](ablation-evidence-2026-11.json)保留二进制身份、120 个延迟样本、被拒绝变体的补丁与差异输入，以及故障变体的编译和断言结果。

## 方法与接受标准

先构建不可变基线二进制，再从同一提交创建独立工作树，分别删除候选机制。
每个变体使用同一批真实 CLI / framed stdio LSP 消费者与基线对照；成功的组再组合验证。
测试调用生产二进制，包含实际 Helm 渲染、版本化编辑、自然发生的重扫和文件系统失败。

接受删除的条件是调用关系更简单、可观察的分析契约保持一致，而且增量工作量没有退化。
JSON 字段类型也属于契约，比较时区分 `true` 与 `1`。
对必要的条件则编译故障变体，要求具体行为断言失败且其他正对照仍然通过；编译错误、超时和测试异常不能算检出。

唯一有意改变的用户输出是 `render-check --format human`：它现在复用普通 `check` 的计数、配置错误和诊断格式。
JSON 报告、severity、目标版本、资源数量、完成状态及退出码保持原有含义。

## 采纳的删除

| 组 | 原来的成本 | 实际修改 | 主要验收 |
| --- | --- | --- | --- |
| 初始化协议依赖 | `glsp` 只用于构造一次 initialize 响应，项目没有使用其 transport / handler | 在已有 JSON transport 中直接构造实际支持的 capabilities；移除模块、sum、vendor 和对应致谢 | 四种客户端能力组合的初始化 JSON 完全一致；每版 10 个能力/行为记录及 25 个 LSP 生命周期记录 |
| LSP / workspace 转发层 | 无调用入口、固定参数包装、伪 reply/reject 回调、已有文件集合的临时副本 | 删除 `LoadWithConfig`、`TargetForFile`、`uriPath`、`analyze(...fullScan)`；修复请求直接返回结果；直接遍历 `Input.Files` | 九个现有消费者，基线与变体共 18 次执行、每版 178 个结果记录；另有重复 root / symlink 对照 |
| LSP 比较与修复循环 | 把单个文件包装成 slice，再建立 map；同一请求范围反复转换 UTF-16 offset | URI 有序成员直接逐项比较，单文件直接比较；复用外层已验证的范围 | 配置 URI、Helm 状态切换、失败 root 隔离、版本化 pin、Quick Fix 和增量工作量 |
| 分析入口与重复状态 | 无调用的 `Analyze` / `FixesInRange`、与选择状态重复的 `deferred` 参数、重复资源计数 | 保留实际调用的有状态 Analyzer；由 `selected == nil` 表示候选阶段；资源数取文档集合长度；删除不可达 Required 分支 | schema、projection、目标版本、LSP 和 1/20/100/300 候选修复测试 |
| CLI 报告链路 | render-check 拼装 check 参数、编码 JSON、解码 JSON，再编码；诊断使用 `[]any` | 两个命令共享具体的 `checkReport` 和 `writeReport`，诊断为 `[]analysis.Diagnostic`；移除 renderer hash 的惰性开关 | 真实 Helm 6 个场景、获取 renderer 的 5 个场景、输出失败 34 个场景；实际 render/check 报告逐项对照 |
| 分发工具的跨产品分支 | 此仓库的 native/release 已固定 Argo CD，但候选工具仍含 Workflows 分支和不存在的 scripts 路径后备 | 固定实际支持的 `developer-package` 变体与 `tools/candidate.py` 路径 | 五个平台的 adapter metadata 与原实现一致；现有完整候选/篡改/缺平台消费者 |

`sameScopeFiles` 的有序比较依据是：`scopeResult` 唯一构造点保存 URI 排序后的文件；整 scope 复用不改顺序；查询副本只替换相同 URI 的 snapshot，且不会提升为诊断缓存。
比较仍包括 URI、canonical path、target、output、revision 和完整文本。
跨 root 去重的 `seen`、失败 root 回滚、scope completeness 及版本化编辑条件均保留。

### 依赖和二进制的独立对照

只移除初始化依赖时，Linux amd64 二进制由 **12,220,304** 字节变为 **12,160,952** 字节，减少 **59,352** 字节；实际链接的第三方 Go 模块从 9 个变为 8 个。
删除的 vendored 目录与模块清单共 8,248 行，其中绝大多数是第三方协议类型定义，不能记作本项目原创逻辑的精简量。
各独立组组合后的原生二进制大小可能随编译身份变化；这里仅报告同一工具链下该单项实验的值。

## 拒绝或保留的候选

### 身份验证的第二次 JSON 解析：实验后保留

候选把 `validate_release` 的两次解析合并成一次 typed `Identity` 解析。
403 个 JSON 输入分别通过 local 与 managed 入口，形成 **806 组**新旧真实进程对照：798 组 exit/stdout/stderr 完全一致，8 组不同。

未知字段嵌套深度为 127、128、129、256 时，typed deserializer 可以跳过未知值，而原来的第二次 `serde_json::Value` 解析执行递归深度限制。
单次解析变体因此接受了原实现拒绝的 4 个 managed release 输入；另外 4 个 managed development 输入的错误原因不同。
这项删除会放宽已有输入边界。最终保留原 `src/identity.rs`，没有为让删减通过而增加 visitor 或补充一套深度检查。

| 输入源码 | SHA-256 |
| --- | --- |
| 原身份验证器，最终保留 | `1bb605cb48e75ff1113db873f4768bb3622da679d67f73c4acd70a0f75a27900` |
| 被否决的单次解析变体 | `4b7ce125ade295229c59305aff74fe3bf1f9d93343b55dc20438987fd4897420` |

### 缓存、并发与输入边界

以下条件继续保留，并由现有故障注入消费者验证：

组合后的 Go 代码已执行全部八个既有故障变体：八个原实现控制通过，八个变体均成功编译并被指定行为断言检出。

| 保留项 | 删除后的具体风险 / 对照 |
| --- | --- |
| 尚未完成的 root 扫描标记 | 扫描开始就清除标记，随后被编辑取消，会丢失此次文件成员更新；`R-02-consume-scan-on-start` |
| 基于本次实际输入的 read proof | 用旧 scope 制作 proof，会错误接受已经变化的依赖；`R-03-proof-from-old-scope` |
| root 级解析失败隔离 | 一个 root 的坏输入会中断其他 root 的诊断和跳转；`R-04-global-parser-failure` |
| Helm buffer 重新分类、URI canonical identity | 未保存模板和 symlink 的成员判断可能使用旧事实；`R-06`、`R-07` |
| 不完整 YAML 的引用恢复限制 | 重复键和不可靠前缀可能产生错误的精确跳转；`R-08` |
| 早期 oversized scalar 检查 | 在昂贵 YAML 解码前拒绝超大值；简单移动检查还会改变失败时的 ownership 归属，因此未删除 |
| Rust `Host`、staging、不可变 generation、SHA 与身份校验 | 它们分别服务真实 Zed 操作、部分下载隔离、并发路径稳定和执行前验证；23 项生产安装器测试继续执行 |

另编译了“始终向不支持版本化编辑的客户端宣告 codeAction”变体：只有两个对应的 initialize 断言失败，其他八个控制通过。
因此新增能力验收确实能识别错误宣告，并非只观察进程成功退出。

## 性能验收

LSP 独立变体与基线各测试 1,000 和 3,000 资源，每个工作负载 30 次编辑。
两版全部热编辑保持 `diskReads=0`、`parsedFiles=1`、`domainDocuments=2`、`acquisitionFallbacks=0`。
自然的 30 秒 reconciliation 也实际触发并通过验收。

| 资源数 | 基线 p50 / p95 | LSP 删减版 p50 / p95 |
| --- | --- | --- |
| 1,000 | 128.902 / 133.076 ms | 130.249 / 141.117 ms |
| 3,000 | 146.204 / 192.234 ms | 144.135 / 175.978 ms |

这些是共享机器上的描述性样本，包含原有 120 ms debounce；未改变 debounce、测试阈值或工作量断言。
结果支持“没有退化为全量工作”的结论，不构成统计意义上的延迟提升声明。

## 修复测试本身

1. `ablate_identity.py` 原先匹配已不存在的单行源码，并写死 `cargo +stable`。现在读取仓库固定工具链，检查变异锚点恰好一次，先要求原实现通过，再要求变体仅错误接受指定输入。编译失败、缺失结果或旁支失败均判定实验失败。Linux amd64 CI 会执行它并保留结果。
2. `output_failure_e2e.py` 的原 fixture 实际包含三个有效资源，却断言两个；原二进制与候选在相同四项资源数断言失败。现在只在该 fixture 中排除 renderer 输入 `chart/**`，使普通 check 明确分析 app 与 project；显式 Helm 渲染照常读取 chart。修复后原版与最终候选各 34 项通过。
3. initialize 契约按规范化 JSON 比较，以免 Python 把 `true` 和 `1` 当作相等；版本字段必须为非空字符串。
4. 原生进程消费者增加成功 render/check 的报告对照，覆盖普通 warning、严格 warning 和 schema error。五平台 CI 使用明确标注的受控 renderer fixture；本地还单独执行实际 Helm v3.19.0。

## 复现与验收边界

使用仓库固定 Go、Rust 工具链和 C 编译器。以下输出目录均需是新目录：

```sh
python3 tools/native.py build --platform linux/amd64 --output artifacts/ablation-build
python3 tests/host_capabilities_e2e.py artifacts/ablation-build/argocd-ls artifacts/ablation-host
python3 tests/lsp_e2e.py artifacts/ablation-build/argocd-ls artifacts/ablation-lsp
python3 tests/quickfix_scaling_e2e.py artifacts/ablation-build/argocd-ls artifacts/ablation-fixes
python3 tests/incremental_performance_e2e.py artifacts/ablation-build/argocd-ls artifacts/ablation-perf --sizes 1000,3000 --repeats 30 --require-incremental
python3 tests/audit_regression_mutations.py artifacts/ablation-build/argocd-ls artifacts/ablation-mutations
cargo test --locked
python3 tests/ablate_identity.py --output artifacts/ablation-identity
```

真实 Helm 的报告和输出失败验收分别使用：

```sh
python3 tests/readiness_render_e2e.py artifacts/ablation-build/argocd-ls /absolute/path/to/helm artifacts/ablation-render
python3 tests/output_failure_e2e.py artifacts/ablation-build/argocd-ls /absolute/path/to/helm artifacts/ablation-output
```

最后一项使用 Linux `/dev/full` 和进程暂停机制。其他平台由原生 CI 的可移植消费者验证。
完整构建、打包、CLI/LSP、工作量和故障注入入口为 `python3 tests/native_ci.py --platform <native-platform> --output artifacts/ablation-native`。
五平台及候选聚合是否通过，应查看本 PR 当前提交的 Checks，不能用基线或旧提交的成功代替。
这些验证覆盖原生服务、适配器构建和分发产物；没有新增真实 Zed GUI 安装记录。

### 独立变体的二进制身份

| 变体 | SHA-256 |
| --- | --- |
| 原始基线 | `9648a98ed3d2bfc0c843306b032966dadff9a1d38f1690d140020bd41a41902e` |
| 仅移除 glsp | `2822ebf84570cf095b3ab2a918cd65d382a653f700f6699b0d64cad1468d072b` |
| 仅 LSP / workspace 精简 | `6b9f7c9516ce869802d4d433111eb8690d65b9827f79b9358c98582f431cc382` |
| 分析 / typed CLI 报告初版，228 个成对记录 | `8a7cfca36d2e978bf360ef510f765abf311c190c4bbc7c7e97780198c012b648` |
| 分析 / CLI 最终版，补验 renderer 与输出失败 | `e18f392c25dbdc76c172b1ac8b026e94884c02acfe64afe53e4ed2a235da92d1` |

独立变体是实验输入，不是发布包；最终 PR 的原生构建身份及完整证据由当前 CI 产物记录。
