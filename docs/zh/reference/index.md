# 命令与配置参考

## CLI 参考

- [项目与元数据命令](cli-project-metadata.md)
- [文件与 QC 命令](cli-files-qc.md)
- [外部分析命令](cli-analysis.md)
- [远程存储命令](cli-remote.md)
- [判定、发布与报告命令](cli-decisions-reports.md)
- [Workflow 历史命令](cli-workflow.md)
- [终端界面](cli-tui.md)
- [Taxonomy、生命周期与管理命令](cli-taxonomy-lifecycle-admin.md)

## 配置与数据模型

- [Recipe 配置模型](recipe-overview.md)
- [Recipe 字段参考](recipe-fields.md)
- [结果解析器与示例](recipe-parsers-examples.md)
- [数据模型](data-model.md)

```{toctree}
:hidden:

cli-project-metadata
cli-files-qc
cli-analysis
cli-remote
cli-decisions-reports
cli-workflow
cli-tui
cli-taxonomy-lifecycle-admin
recipe-overview
recipe-fields
recipe-parsers-examples
data-model
```

全部命令使用全局形式：

```text
operon [--project PATH] [--version] <子命令> [参数]
```
