# Feature 索引命令

`operon features` 查询 `index/features/` 下的扁平 GFF3/GTF feature 缓存（EXT-8；见
[派生索引缓存](../architecture/files-and-storage.md#派生索引缓存)）。每份 GFF3/GTF 注释归档对应
一个 `{file_id}.{sha256}.v2` 目录，内含 `data.parquet` 与 `meta.json` 身份标记。该索引是未登记、
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
- `--attribute KEY=VALUE` 按逐字属性块匹配（GFF3 百分号编码 / GTF 带引号原文）；`--attribute
  KEY~=PATTERN` 以正则匹配值。可重复的 `--attribute` 自成一个 OR 组（命中任一 token 即保留），
  上述其余可重复列过滤与该组是 AND 组合。对 `ID`/`Parent`（GFF3）与 `gene_id`/`transcript_id`
  （两种方言）会短路到展开后的解码列，正则锚点作用于解码值。
- 区域过滤用 tabix 重叠语义：feature 的闭区间 `[start, end]` 与 `[--start, --end]` 重叠即命中；
  两端也可只给一个。
- `--limit N` 上限打印行数（默认 1000；`0` 为不限）。`--format tsv`（默认）打出紧凑列
  `seqid source type start end strand phase id parent gene_id transcript_id line_number`；
  `--format json` 输出完整行——含 `score` 与逐字的 `attributes`——并按文件分组附带索引统计。
- `--rebuild` 先丢弃缓存索引并从归档重建。
- 扁平列：GFF 的九个 feature 列、规范化的 `id`/`parent` 与 `gene_id`/`transcript_id`、
  逐字的 `attributes` 文本、可溯源的源 `line_number`。v2 起 `id`/`parent` 在两种方言间同构：
  GFF3 读 `ID`/`Parent`（百分号解码）；GTF 把属性蕴含的结构身份物化——gene 行以 `gene_id`
  作为自身 id，transcript 行以 `transcript_id` 为 id、`parent = gene_id`，CDS/exon 等子行
  无自身 id、parent 指向所属转录本。此前的 v1 缓存会透明重建。

## features bed

```bash
operon features bed (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                    [--type T] [--seqid S] [--source S] [--strand STRAND]
                    [--start N] [--end N] [--attribute KEY=VALUE|KEY~=PATTERN]
                    [--longest-transcript] [--measure {CDS,mRNA}]
                    [--name-attr ATTR] [--limit N] [--output PATH] [--rebuild]
```

- 每个选中的 feature 输出一行 BED6（`chrom`、`chromStart`、`chromEnd`、`name`、`score`、
  `strand`）；`chromStart` 是 `start - 1`（BED 零基左闭）。`score .` 记为 `0`；缺 strand 记为
  `.`。列过滤组合与 `features query` 相同（`--limit` 默认 `0` = 不限——BED 语义是出全量）。
- `--longest-transcript` 改为每个 gene 输出一行：gene 提供 `chrom`、`chromStart`、`chromEnd`、
  `score` 与 `strand`，名字取它最长的转录本，没有转录本的 gene 不输出。排序测度默认 CDS 长度和
  （`--measure CDS`），也可用转录本 span（`--measure mRNA`）；并列时取 id 较小者。
  `--name-attr protein_id` 在命中属性时用该属性值命名（否则回落到转录本 id）。
- BED 按坐标工具格式输出：不做表格公式转义（与 `features query --format tsv` 不同，
  ODR-83 契约只约束 TSV 输出）。
- `--output PATH` 写成磁盘上的普通文件（不归档、不进 manifest）；缺省写 stdout。选择与跳过语义
  同 `features query`；显式 `--file-id` 零行退出码 1。

## features parents / features children

```bash
operon features parents  (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                         --id X [--id Y …] [--until gene] [--max-depth 100]
                         [--format {tsv,json}] [--rebuild]
operon features children (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                         --id X [--id Y …] [--recursive] [--type T]
                         [--format {tsv,json}] [--rebuild]
```

- 两个动词回答"这个 id 是谁"的反问。`parents` 沿层级向上走到 `--until TYPE`（默认 `gene`）或
  无父链根；`children` 沿层级向下（直属子代，`--recursive` 取所有层；`--type T` 限制可达行）。
- 层级边由方言决定。GFF3 走 `parent` 链；GTF 没有 `Parent` 边，转录本所属基因通过
  `gene_id`/`transcript_id` 列里物化的属性解析——gene 的子代是携带该 `gene_id` 的转录本行，
  `--recursive` 再累上引用这些转录本的所有行。
- 输出列与渲染与 `features query` 完全一致（同一组 TSV 列、同一 JSON 文档形状）。`--id` 找不到
  时行集为空，显式 `--file-id` 因此退出码 1。

## features bins

```bash
operon features bins (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                     [--bin-size N] [--type T] [--format {tsv,json}]
                     [--rebuild]
```

- 按 `(seqid, bin)` 计数 feature——首个 bin 覆盖 1-based 坐标 `1..bin_size`，feature 落到包含其
  1-based `start` 的 bin。默认 `--type gene`（染色体绘图的 gene density；可重复，同 query）。
- TSV 列：`seqid`、`bin_start`、`bin_end`、`count`、`feature_start_min`、`feature_end_max`
  （bin 内 feature 实际占用的坐标范围）与 `features_per_bp`（count / bin_size）。
- 选择与跳过语义同 `features query`。

## features stats

```bash
operon features stats (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                      [--format {tsv,json}] [--rebuild]
```

- 打印 manifest 身份与按 `type`、`source`、`seqid` 的三张统计表（数量、min start、max end）。
- `--format json` 把身份与三张表合成一份文档。选择与跳过语义同 `features query`。
