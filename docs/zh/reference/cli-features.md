# Feature 索引命令

`operon features` 查询 `index/features/` 下的扁平 GFF3/GTF feature 缓存（EXT-8；见
[派生索引缓存](../architecture/files-and-storage.md#派生索引缓存)）。每份 GFF3/GTF 注释归档对应
一个 `{file_id}.{sha256}.v1` 目录，内含 `data.parquet` 与 `meta.json` 身份标记。该索引是未登记、
可重建的派生数据：不属于 manifest、release 与备份；过期、损坏或缺提交标记的缓存都会在下一次使用时
从归档字节透明重建。构建按有界批次流式进行且行级严格——任一不是恰好 9 列合法数据的源行都会中止构建
并报出源行号。

## features query

```bash
operon features query (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                      [--start N] [--end N] [--limit N] [--format {tsv,json}]
                      [--rebuild]
```

- 二选一的查询范围。`--file-id` 只回答该 manifest 行；显式选中的 remote-only 或非注释文件会让命令
  失败（进程退出码 1，stderr 说明）。`--entity-type`/`--entity-id` 遍历实体下的注释文件，跳过不可用
  行并在 stderr 提示（与 `operon qc` 一致）。
- 可重复的列过滤：`--type`、`--seqid`、`--source`、`--strand`、`--gene-id`、`--transcript-id`、
  `--parent-id`。
- 区域过滤用 tabix 重叠语义：feature 的闭区间 `[start, end]` 与 `[--start, --end]` 重叠即命中；
  两端也可只给一个。
- `--limit N` 上限打印行数（默认 1000；`0` 为不限）。`--format tsv`（默认）打出紧凑列
  `seqid source type start end strand phase id parent gene_id transcript_id line_number`；
  `--format json` 输出完整行——含 `score` 与逐字的 `attributes`——并按文件分组附带索引统计。
- `--rebuild` 先丢弃缓存索引并从归档重建。
- 扁平列：GFF 的九个 feature 列，加上规范化的 `id`/`parent`（GFF3 的 `ID`/`Parent`，值做百分号解码）、
  两种方言共有的 `gene_id`/`transcript_id` 字面键、逐字的 `attributes` 文本，以及可溯源的源
  `line_number`。

## features stats

```bash
operon features stats (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                      [--format {tsv,json}] [--rebuild]
```

- 打印 manifest 身份与按 `type`、`source`、`seqid` 的三张统计表（数量、min start、max end）。
- `--format json` 把身份与三张表合成一份文档。选择与跳过语义同 `features query`。
