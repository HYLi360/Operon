# 备份、迁移与续跑

## 备份与迁移

推荐由 `backup` 命令创建 SQLite 一致快照，而不是在数据库运行期间直接复制文件。
`--output` 目录必须位于项目根之外且尚不存在，`backup create` 否则会拒绝执行：

```bash
# 配置、SQLite、审计与 workflow 日志
operon backup create --output /backups/my-project-control --scope control

# 另加 QC、analysis、reports、taxonomy、releases
operon backup create --output /backups/my-project-results --scope results

# 再加 raw、standardized 和本地占位符等全部项目管理数据
operon backup create --output /backups/my-project-full --scope full

operon backup verify --input /backups/my-project-full
```

注意范围边界：`results` 不包含 `raw/` 与 `standardized/`（通常最难重建的字节），
因此不能作为可恢复的 `full` 替代品；只有 `full` 能恢复数据文件。

`backup verify` 按精确快照校验：除检查 manifest 所列文件的大小与 SHA-256 外，也会拒绝
备份目录中任何未列入 manifest 的额外文件。不要把注释、临时文件或恢复记录直接放进备份
目录；需要附加说明时放在备份目录之外。

若使用 `REMOTE_ONLY`，本地备份还必须覆盖含 `file_locations` 的 SQLite；远端应独立
备份镜像 root（包括 `operon-manifest.json`）和实际对象。占位符本身不是恢复依据，
只有本地 `files` 身份与远端清单/字节同时保留，才能在新电脑上安全 hydrate。

`report metadata` 不是备份：它只导出便于浏览和交换的 metadata/manifest TSV，不包含
完整 QC、decision、changes、workflow、remote location 和数据库迁移状态。

更稳妥的做法是定期创建 release，并在 release 目录执行
`sha256sum -c checksums.sha256`（Linux）或
`shasum -a 256 -c checksums.sha256`（macOS）。

备份策略可按重建成本分级：

| 类型 | 示例 | 策略 |
|---|---|---|
| 不可替代 | 原始 FASTQ、外部原始下载、人工整理的元数据 | 多副本备份、checksum、不可变 |
| 重建昂贵 | assembly、注释、全基因组比对 | 保存并备份 |
| 易于重建 | 临时索引、中间排序文件、缓存 | 可清理，但保留生成规则 |

该原则成立的前提是工具环境与数据库版本都能重建，否则“可重建”只是理论上的。

旧版 v1 数据库**无需手工迁移**：当前程序打开数据库时会自动迁移 `qc_results` 与 `decisions` 到 v2 结构，旧 QC 数据以 `legacy:` 输入身份保留，旧 decision 可继续通过 `current_decisions` 读取。

## 失败任务续跑

所有核心步骤都幂等：

- 同一文件重复 `ingest`：相同 SHA-256 返回同一 `FIL_`，不重复复制。
- `standardize`：目标已存在且 checksum 相同则跳过。
- `qc`：同一 `input_identity + stage + metric + tool/version/parameter_set` upsert，不产生重复行。
- `evaluate`：追加新 decision，不覆盖历史。
- `release`：版本目录已存在时拒绝重复创建，不会悄悄覆盖。
- `taxonomy compile`：相同 profile/taxonomy/TSV 复用；身份相同而内容不同则拒绝覆盖。
- `report coverage`：输入成员、profile 和 reference-set 身份相同则校验并复用旧报告。
- `analyze`：Ctrl+C/SIGTERM 优雅停机后，当前作业记为 `interrupted`、半成品输出被清理
  （`--keep-partial` 可保留它们用于调试）；重跑时已完成文件走缓存，输入未变且输出验证
  一致的旧结果会被收养（`adopted`），只有真正未完成的文件才重新计算。

因此从中断处直接重跑相同命令即可。可通过 `status` 查看每个实体当前处于哪个状态。
