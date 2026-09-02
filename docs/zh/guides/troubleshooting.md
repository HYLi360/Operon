# 故障排查

## Checksum 与格式问题

```bash
operon verify          # 找 MISSING / CHECKSUM_FAILED
operon status          # 看实体级状态
operon query "SELECT file_id, entity_type, entity_id, file_role, status, relative_path FROM files WHERE status != 'CHECKSUM_VERIFIED'"
```

典型处理：

| 状态 | 建议 |
|---|---|
| `REMOTE_ONLY` | 预期状态；用 `operon locations` 看缓存位置、用 `operon verify` 实时复核，需本地字节时执行 `pull` |
| `REMOTE_UNVERIFIED`（仅 verify 输出） | 远端暂时不可达，未确认副本是否仍在；检查 SSH/网络后重试 `verify` |
| `MISSING` | 恢复文件到 `relative_path`，或从源头重新归档为新实体版本 |
| `CHECKSUM_FAILED` | 不要继续 QC；确认文件是否被误改，从原始来源恢复 |
| `QC_FAILED` | 查看 `operon report qc` 中 `parseable=0` 的文件，以及 `logs/workflow.jsonl` 中的错误 |
| 格式解析失败 | 用外部工具（如 `seqkit stats`、GFF3 validator）检查；修复后作为新版本归档，不要覆盖 raw |
