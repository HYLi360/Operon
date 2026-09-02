# 元数据导入与字段扩展

## 批量导入元数据

小规模数据可用 `add`；成百上千条记录使用受控 CSV/XLSX 表格导入。先生成模板：

```bash
operon import table --table organisms --template organisms.xlsx
operon import table --table samples --template samples.csv
```

填写后先预览并确认：

```bash
operon import table --table organisms --file organisms.xlsx

# 自动化环境必须显式确认碰撞策略
operon import table --table organisms --file organisms.csv \
  --on-conflict update --yes
```

导入行为：

- 只允许人工管理的 entity/accession 表；`files`、QC、decision 等系统表不可覆盖。
- 先完成 schema、受控词汇和外键校验，再显示每行 `insert/update/unchanged`。
- `--on-conflict error` 拒绝修改已有行，`skip` 跳过，`update` 更新并记录逐字段审计。
- 不提供删除或“完整快照替换”语义；任一写入失败时该表的整个事务回滚。
- XLSX 的第一张 `data` 工作表用于导入；模板的第二张 `schema` 工作表仅供查看。

CSV 示例：

```text
assembly_id,sample_id,assembly_accession,assembly_version,assembly_level,assembly_method
ASM_000001,SMP_000001,GCA_000000001,1,chromosome,SPAdes v4.0.0
```

所有可用列请用 `operon schema --dump` 查看。

若要连同文件一起导入一个完整数据集，使用：

```bash
operon import dataset
```

向导界面暂时全部使用英文，已有 organism 使用 scientific name 自动补全。source 章节
要求明确选择 INSDC 或非 INSDC，并记录 database/repository 与 provider，同时询问记录 URL、
引用文献和 License。非 INSDC 数据必须提供 citation/DOI 与 License 名称或 SPDX identifier；
INSDC 来源可将这两项留空。taxonomy ID、sequencing、genome FASTA 或部分 annotation 文件
仍可跳过，汇总审阅会保留醒目的 warning。选择 `Edit ...` 修改某一章节后会直接回到汇总
审阅，而不会接着运行原向导的后续章节。最终确认前不会修改 SQLite 或归档文件。

成功导入后，规范化来源写入 `data_sources`，并通过 `source_links` 关联本次选择/创建的
entity 与归档 file；相同来源内容按身份复用。`report metadata` 和 release 都会包含这两张表。

## 扩展元数据字段

1. 打开 `config/schemas.yaml`。
2. 在对应表 `fields` 下添加字段，例如给 organism 加 `provenance_note`：

```yaml
tables:
  organisms:
    fields:
      provenance_note:
        type: string
        description: 项目自定义来源备注
```

3. 运行 `operon add ... --field provenance_note=...` 或 `operon import table ...`；系统会自动在 SQLite 表上增加该列（`ensure_metadata_columns`）。
4. 使用 `operon report metadata` 检查派生导出。

注意：CSV/XLSX 中的未知列会被拒绝；必须先改 schema，再导入数据。
