# 文档

这里保留当前实现、日常操作和已经发生的问题。实现说明对应当前检出的源码；安装说明中的已发布版本与源码进度分别核对，不能据此认定已发布二进制包含 `main` 的全部改动。

| 需要了解的内容 | 入口 |
| --- | --- |
| 安装、升级、开始使用 | [项目 README](../README.md) |
| 模块职责、文件索引、公共依赖与隔离边界 | [当前架构](architecture.md) |
| 已发生问题、原因和持续保护 | [经验教训](lessons-learned.md) |
| 本地开发、依赖更新、验证入口 | [开发说明](../DEVELOPMENT.md) |
| 自动版本、显式配置与引用策略 | [配置](configuration.md) |
| 版本目录、Schema 生成和更新验证 | [多版本支持](multi-version-support.md) |
| 原生服务和 Zed 适配器的安装、发布、回滚 | [分发](distribution.md) |

[原生发布说明](native-release-notes.md)和 [Zed 发布说明](../editors/zed/release-notes.md)是发布工作流使用的当前输入。`contracts/`、`compat/`、`schemas/`、`internal/assets/` 中的合同、锁、来源清单和许可证承担构建或验证职责。Go 依赖由 `go.mod`/`go.sum` 固定，原始许可证从验证后的模块源码随产物打包，不提交 `vendor/`。
