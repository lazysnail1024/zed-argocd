# 版本目录与自动兼容

默认 target 为 `auto`，不要求用户创建配置文件。显式 exact target 使用该版本采集到的 CRD 和本仓静态规则。配置入口见[配置说明](configuration.md)。

## 当前离线目录

以下数据来自已提交的 [compat/catalog.lock.json](../compat/catalog.lock.json)，采集截止为 **2026-11-05T23:59:59Z**；它描述该快照，不表示今天上游的最新发布状态。

| 项目 | 当前内容 |
| --- | --- |
| 版本窗口 | Argo CD 2.0.0–3.6.1，major 2 与 3 |
| 数字稳定 tag | 370 |
| Application / AppProject | 各 370 个 target |
| ApplicationSet | 330 个 target：312 个 bundled CRD，18 个固定 addon 引用 |
| 不同源 Schema | 59 |

采集器用官方 Release 列表确定截止时的最新稳定版本与相邻两个 major，再用 tag 列表补全该窗口内的数字稳定 tag。预发布后缀不进入窗口；没有匹配 Release 记录的数字 tag 仍可进入目录，不据此推测缺失 Release 的原因。annotated tag 解析到不可变 commit，来源响应、tag object、CRD 和规范化 Schema 都记录 SHA-256。

ApplicationSet 覆盖包括 Argo CD 2.3.x 固定引用的独立 addon：2.3.0 指向 ApplicationSet v0.4.0，其余采集到的 2.3.x 指向 v0.4.1。安装引用、commit 和 CRD digest 留在来源记录中。没有 bundled CRD 或固定独立安装引用的 exact target 不借用新版本 Schema；自动模式汇集已证明的来源，并不提供一个独立的 standalone ApplicationSet 产品版本目录。

### 文件职责

| 路径 | 用途 |
| --- | --- |
| [compat/catalog.lock.json](../compat/catalog.lock.json) | 版本、tag/commit、CRD 与规范化资产的完整来源和 readiness |
| [compat/upstream](../compat/upstream/) | 采集的上游响应与 tag object |
| [compat/local-rules-sources.json](../compat/local-rules-sources.json) | 已实现本地规则对应的 controller 源码证据；领域矩阵据此选取版本 |
| [schemas](../schemas/) | 以内容身份存储的采集/规范化输入 |
| [internal/assets/catalog.json](../internal/assets/catalog.json) | 编译进运行时的精简目录 |
| [internal/assets/schemas](../internal/assets/schemas/) | 运行时 exact 和自动兼容 Schema 资产 |
| [tools/collect_catalog.py](../tools/collect_catalog.py)、[tools/normalize_schemas.py](../tools/normalize_schemas.py) | 来源采集、Schema 生成、报告接纳与 readiness 生成 |

这些是版本支持的构建和验证数据，不是可随文档清理删除的历史附件。

## 自动模式的判断

Argo CD 各产品版本共享 `argoproj.io/v1alpha1`，文件不能据此识别实际 controller 版本。自动模式要求文档满足至少一个已覆盖版本的**完整 Schema**，通过根 `anyOf` 组织；不会将不同版本字段合成宽松的并集。

例如旧 `ksonnet` 与新的 `syncPolicy.automated.enabled` 分别被支持，但一个从未同时包含两者的版本组合报告 `SCHEMA_VERSION_COMBINATION`。已知字段类型错误仍为 `SCHEMA_TYPE`，必需值缺失仍为 `SCHEMA_REQUIRED`。

自动 Schema 对结构化对象设置额外字段边界，防止旧分支因不认识新字段而放过新字段中的错误。上游声明的 preserve-unknown 对象和开放 map 保留扩展行为；所有分支都不认识的字段报告 `SCHEMA_UNKNOWN_FIELD` warning。exact target 的原始 Schema 不因此被改写。

补全和悬停根据已有字段名筛选完整版本分支，排除正在编辑的字段；缺少 required 字段或普通值错误不阻塞修复建议。已有名称无法匹配任何完整分支时，查询回退到覆盖声明帮助修复，验证仍独立报告不兼容。查询与校验共用编译后的 Schema，不维护第二套约束解释器。实现见 [schema_queries.go](../internal/analysis/schema_queries.go)。

生成数据用本地 `$defs` 共享重复结构，运行时按实际 kind 准备 Schema，遍历去重；不在启动时编译 370 套独立 Schema，也不在打开文件时请求网络。身份响应提供 `defaultTargetVersion: "auto"` 和 `supportedVersions: {min, max, majors, releaseCount}`；auto 目标没有 pin action，显式 scope 目标仍支持经验证的 pin。

## 覆盖声明的边界

覆盖指采集 CRD、编辑器 Schema 查询和本仓已实现的静态检查。动态 ApplicationSet 表达式、远程渲染、controller 执行、Kubernetes admission 和完整运行时策略不是这些结果的证明范围；不确定项通过诊断及报告 `notEvaluated` 保留。

`semanticsVerified` 是本地验证合同的 readiness 字段，不表示每个版本的 controller 行为均已复现。针对 controller 源码的领域矩阵与覆盖整个目录的 CRD/查询矩阵分别运行。当前文档索引的完整性也与版本目录 readiness 分开，见[当前架构](architecture.md)。

## 更新与接纳

使用项目规定的 Go/C 工具链以及 Python/PyYAML，从仓库根目录执行。先将 `catalog_cutoff` 设为本次采集明确采用的 UTC 截止时间；重新采集当前快照可用 `2026-11-05T23:59:59Z`。

```sh
catalog_cutoff=2026-11-05T23:59:59Z
python3 tools/collect_catalog.py --cutoff "$catalog_cutoff"
python3 tools/normalize_schemas.py
go test -mod=vendor ./internal/assets ./internal/analysis
go build -mod=vendor -o build/argocd-ls ./cmd/argocd-ls
python3 tests/multi_version_e2e.py build/argocd-ls artifacts/catalog-bootstrap --bootstrap
python3 tools/normalize_schemas.py --accept-report artifacts/catalog-bootstrap/report.json
go build -mod=vendor -o build/argocd-ls ./cmd/argocd-ls
python3 tests/multi_version_e2e.py build/argocd-ls artifacts/catalog-final
```

每次验证使用新的输出目录。采集主动清除 readiness，只有覆盖全部 exact tag、digest 匹配且通过的 bootstrap 报告可以接纳；之后重新构建，再验证完整公开报告和零配置 LSP。改变 Schema 会撤销旧验证标记。

| 验证入口 | 覆盖方式 |
| --- | --- |
| [multi_version_e2e.py](../tests/multi_version_e2e.py) | 默认执行每个 exact tag、inventory、auto 和 LSP；Linux CI 使用完整目录 |
| 同一入口的 `--representatives` | 其他原生平台按不同 Schema bundle 选代表，同时保留 inventory、auto 和 LSP 检查 |
| [multi_version_test.go](../internal/analysis/multi_version_test.go) | 已声明字段的自动查询覆盖 |
| [appset_overrides_e2e.py](../tests/appset_overrides_e2e.py)的 `--all-targets` | 对具备所测字段的 target 按确切 AppSet Schema hash 与完整 localRules 分组，深入场景每组一次，auto 独立执行；报告记录每组所有覆盖版本 |
| [ablate_multi_version.py](../tests/ablate_multi_version.py) | 编译受控删除变体，对照历史分支、对象边界、共享定义和查询分支的行为 |

`multi_version_e2e.py` 没有 `--all-targets` 选项。AppSet 深层场景的分组不替代全 tag 兼容矩阵；具体 CI 命令以 [native_ci.py](../tests/native_ci.py)和 [native.yml](../.github/workflows/native.yml)为准。已发生的漏采来源和重复执行问题见[经验教训](lessons-learned.md)。
