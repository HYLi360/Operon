# NCBI adapter 恢复与迁移

本流程用于将旧项目迁移到 database schema 2.9、metadata schema 1.4，并修复旧 NCBI adapter 的已知异常。

本文用于把长期运行的旧 `operon` 数据库迁移到 database schema 2.9、metadata schema 1.4，
并修复旧 NCBI Datasets adapter 可能留下的 annotation 重复、GCA/GCF canonical 漂移和
QC 状态降级。整个过程遵守以下边界：

- 不初始化项目，不删除旧 annotation、file、workflow、QC、analysis、release 或 raw 文件；
- schema 迁移只加列、表和索引；业务修复以新 repair workflow、逻辑 supersession 和
  `changes` 补偿记录表达；
- `backup create` 以只读方式打开源数据库，不会为了备份而先迁移；
- `ncbi-reconcile` 的计划阶段使用只读连接，只读取 SQLite 中的 metadata、SHA-256、
  QC/analysis/release 引用，不迁移 schema，也不打开 raw 生物学内容；
- `ncbi-datasets --plan-only` 使用只读连接，会检查 manifest 中记录的本地路径是否存在，
  但不会迁移 schema、读取文件内容、下载数据或写 workflow。

以下命令假设从代码仓库的 `.venv` 执行。把示例路径换成实际路径；备份目录必须位于项目
目录之外，而且执行前不能已经存在。

## 1. 停止写入并设置路径

先停止所有 `ncbi-datasets`、`ingest`、QC、analysis、release 和其他可能写 SQLite/项目目录
的进程。只读查询可以继续，但迁移窗口内最好也保持无人操作。

```bash
OPERON_CODE=/path/to/Operon
OPERON_PROJECT=/path/to/operon-project
OPERON_BACKUP=/path/to/backups/operon-pre-2.9
OPERON_STAGE=/path/to/staging/operon-2.9-rehearsal
OPERON_ACTOR=database-maintainer

cd "$OPERON_CODE"
```

不要把这些变量指向项目父目录、磁盘根目录或 home 根目录。后续命令不删除这些路径；如果
目标已存在，应另选一个新目录。

## 2. 验证准备使用的程序

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m operon --version
```

完整测试必须通过。若生产环境运行 cx_Freeze 构建，则还应重新构建并先在 staging 使用同一
二进制完成下文演练。

## 3. 记录迁移前只读基线

`query` 只读打开 SQLite，不触发 schema 迁移。至少保存以下结果；它们是迁移后的数量守恒
验收基线。

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" query \
  "PRAGMA integrity_check"
.venv/bin/python -m operon --project "$OPERON_PROJECT" query \
  "PRAGMA foreign_key_check"
.venv/bin/python -m operon --project "$OPERON_PROJECT" query \
  "SELECT 'annotations' AS item, COUNT(*) AS n FROM annotations UNION ALL SELECT 'files', COUNT(*) FROM files UNION ALL SELECT 'qc_results', COUNT(*) FROM qc_results UNION ALL SELECT 'analysis_jobs', COUNT(*) FROM analysis_jobs UNION ALL SELECT 'releases', COUNT(*) FROM releases UNION ALL SELECT 'workflow_runs', COUNT(*) FROM workflow_runs"
.venv/bin/python -m operon --project "$OPERON_PROJECT" query \
  "SELECT entity_type, state, COUNT(*) AS n FROM entity_state GROUP BY entity_type, state ORDER BY entity_type, state"
```

第一条应返回 `ok`，第二条应无数据行。若不是这样，先停止迁移并调查原有损坏；不要把
完整性问题与 adapter 修复混在一次操作里。

## 4. 创建并校验迁移前备份

`results` 备份包含一致的 SQLite、配置、日志、QC、analysis、reports、taxonomy 和 releases，
足以保护昂贵的 BUSCO/QC 结果，但不复制 raw。若需要整库灾难恢复且空间允许，改用
`--scope full`。

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" backup create \
  --output "$OPERON_BACKUP" --scope results
.venv/bin/python -m operon backup verify --input "$OPERON_BACKUP"
```

校验结果必须是 `"ok": true`。备份目录应保持只读、不直接作为生产项目继续运行。

## 5. 在备份副本上演练控制面迁移

复制已校验备份作为 staging。复制后 staging 中的 `backup-manifest.json` 不再用于证明后续
改动后的内容；原始备份目录仍保持不变。

```bash
cp -a "$OPERON_BACKUP" "$OPERON_STAGE"
.venv/bin/python -m operon --project "$OPERON_STAGE" migrate \
  > "$OPERON_STAGE/migrate-result.json"
```

`migrate-result.json` 必须满足：

- `schema_version` 为 `2.9`；
- `integrity_check` 为 `ok`；
- `foreign_key_violations` 为 `0`；
- migration 账本包含 `2.6-recovery-and-ncbi-identities`、
  `2.7-entity-lifecycle-retirement`、`2.8-execution-environments` 与
  `2.9-lineage-recipes-resources`
  （原库已处于更近版本时只新增缺失项）。

然后生成业务修复计划：

```bash
.venv/bin/python -m operon --project "$OPERON_STAGE" ncbi-reconcile \
  > "$OPERON_STAGE/ncbi-reconcile-plan.json"
```

逐项审阅 `warnings`、`annotation_supersessions`、`assembly_updates`、`file_role_updates`、
`file_path_repairs`、`accession_primary_updates` 和 `state_restorations`。任何
`alternate_role_conflict` 都是阻断项；不要应用，也不要手改 SQL 绕过。

`file_role_updates` 与 `file_path_repairs` 还会伴随物理文件移动：角色改名（如
`assembly_report` → `assembly_report_genbank`）后，归档文件会被移到新角色的 canonical
路径，并在同一事务中更新 `files.relative_path`、写入 `changes` 审计。若移动前发现目标
路径已被不同字节占用，整个计划直接失败，不会移动任何文件；本地缺失（如 REMOTE_ONLY）
的行只做记录不搬动，其 file_id 会列在结果的 `skipped_path_moves` 中。这一步是后续
paired 下载能恢复的前提：只有腾空 plain canonical 路径，canonical 侧的 ingest 才不会
撞上旧字节。

计划合理后在 staging 应用：

```bash
.venv/bin/python -m operon --project "$OPERON_STAGE" ncbi-reconcile \
  --apply --actor "$OPERON_ACTOR" \
  > "$OPERON_STAGE/ncbi-reconcile-apply.json"
```

## 6. 验收 staging

```bash
.venv/bin/python -m operon --project "$OPERON_STAGE" migrate
.venv/bin/python -m operon --project "$OPERON_STAGE" query \
  "SELECT COUNT(*) AS supersessions FROM entity_supersessions"
.venv/bin/python -m operon --project "$OPERON_STAGE" query \
  "SELECT status, COUNT(*) AS n FROM workflow_runs WHERE step='ncbi_datasets_reconcile' GROUP BY status"
.venv/bin/python -m operon --project "$OPERON_STAGE" query \
  "SELECT COUNT(*) AS repair_changes FROM changes WHERE workflow_run_id IN (SELECT run_id FROM workflow_runs WHERE step='ncbi_datasets_reconcile')"
.venv/bin/python -m operon --project "$OPERON_STAGE" query \
  "SELECT COUNT(*) AS annotations FROM annotations UNION ALL SELECT COUNT(*) FROM files UNION ALL SELECT COUNT(*) FROM qc_results UNION ALL SELECT COUNT(*) FROM analysis_jobs UNION ALL SELECT COUNT(*) FROM releases"
.venv/bin/python -m operon --project "$OPERON_STAGE" ncbi-reconcile \
  > "$OPERON_STAGE/ncbi-reconcile-postcheck.json"
```

验收条件：

- integrity 仍为 `ok`，外键违规仍为 0；
- 最新 repair workflow 为 `completed`；
- annotation/file/QC/analysis/release 总数与迁移前基线完全相同；
- `entity_supersessions` 只增加逻辑映射，旧 `ANN_` 与 `FIL_` 行仍存在；
- `changes` 中每个修复字段都关联 repair workflow；
- 原有 BUSCO/analysis 输出目录仍存在，相关 `analysis_jobs` 与 `analysis_results` 数量不减少。
- postcheck 的 summary 全部为 0；已经 supersede 的行不会再次进入修复计划。

results/control staging 不含 raw，因此不要在这里用 `--plan-only` 判断生产环境还缺哪些下载：
路径存在性检查会把未复制的 raw 文件正确视为缺失。若需要在 staging 验证下载规划，必须使用
full 备份或文件系统快照。

## 7. 在生产项目执行正式迁移

确认 staging 验收通过后，再次确认所有写进程已停止。若演练与正式迁移相隔较久，应使用新
目录再做一次 `results` 备份并校验，以覆盖这段时间的新结果。

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" migrate \
  > operon-production-migrate.json
.venv/bin/python -m operon --project "$OPERON_PROJECT" ncbi-reconcile \
  > operon-production-reconcile-plan.json
```

生产计划应与刚完成的 staging 计划一致；至少其 summary 计数和 warnings 必须一致。确认后：

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" ncbi-reconcile \
  --apply --actor "$OPERON_ACTOR" \
  > operon-production-reconcile-apply.json
```

重复第 6 节的验收查询，并与第 3 节基线比较。此时 metadata schema 会提升到 1.4，项目自定义
字段会保留。

## 8. 先预览，再恢复被叫停的 NCBI 下载

在实际生产项目上只计算 532 个 accession 的缺失集合：

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" ncbi-datasets \
  --accession-file "$OPERON_PROJECT/accession.txt" \
  --include genome --include sequence-report \
  --plan-only > operon-ncbi-download-plan.json
```

重点检查：

- `download_plan` 中只能出现 `genome`、`sequence-report`，不能出现 gff3/protein/cds；
- 已经存在且状态/路径有效的角色不能再次列入；
- paired GCA/GCF 可以分属不同下载组，但不会再争用同一 assembly 文件角色；
- `skipped_existing` 是已经完整满足此次请求的 accession。

计划正确后执行真实下载。若需要保留“从旧失败运行站起来”的链条，传入被叫停运行的
workflow ID：

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" ncbi-datasets \
  --accession-file "$OPERON_PROJECT/accession.txt" \
  --include genome --include sequence-report \
  --resume-run WF_PREVIOUS_INTERRUPTED \
  > operon-ncbi-resume-result.json
```

新运行会在 `workflow_runs.resumes_run_id` 指向旧运行；旧运行不会被覆盖。每个 accession 的
状态和 attempt 保存在 `adapter_run_items`。再次中断时，使用最新失败/中断运行的 ID 重跑同一
请求；已成功的角色由 manifest 逐项跳过。

恢复阶段的 ingest 对两类历史残留会自愈，而不是报 checksum 冲突：

- 角色已改名但文件仍留在旧 canonical 路径（早期 reconcile 或手工 SQL 改名所致）：占用文件
  的字节与 manifest 行一致时，会被搬到该行自己角色的 canonical 路径并更新
  `files.relative_path`，新内容随即正常归档；
- 无任何 manifest 行认领的孤儿文件（停机前的中断运行留下）：会被隔离为同目录下的
  `<文件名>.orphan-<sha前12位>`，字节保留、写入 `changes` 审计，绝不静默覆盖。

若占用字节与认领行的 checksum 也不一致，仍会抛出 `ConflictError`——这意味着归档内容本身
不可信，必须人工核对，不要用删除文件的方式绕过。

监控查询：

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" query \
  "SELECT run_id, resumes_run_id, status, started_at, finished_at, error FROM workflow_runs WHERE step='ncbi_datasets_import' ORDER BY started_at DESC LIMIT 10"
.venv/bin/python -m operon --project "$OPERON_PROJECT" query \
  "SELECT status, COUNT(*) AS n FROM adapter_run_items WHERE run_id='WF_CURRENT' GROUP BY status ORDER BY status"
```

## 9. 回退与恢复原则

schema 2.6/2.7 迁移都是纯加法，但 metadata schema 1.4 和补偿式修复仍应作为一个整体回退：

1. 立即停止所有写进程；
2. 不在原数据库上执行逆向 `DELETE`/`UPDATE`；当前版本没有自动反向修复命令；
3. 优先把迁移前备份恢复到一个新目录并运行 `backup verify`、只读基线查询和应用层检查；
4. 确认恢复副本无误后，才在维护窗口内切换项目路径或用经过审核的文件系统快照恢复；
5. 若只是在下载阶段中断，不回退数据库：保留失败 workflow，使用 `--resume-run` 新建下一次
   尝试，这正是预期的历史模型。

不要只替换 `operon.sqlite` 而保留迁移后的 config/logs，也不要只恢复 config：数据库、配置、
日志和结果索引必须来自同一备份时间点。full 备份或外部 raw 快照的恢复同样必须以校验和
验证结束。
