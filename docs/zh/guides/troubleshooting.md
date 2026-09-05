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
| `QC_FAILED` | 查看 `operon report qc` 中 `parseable=0` 的文件，再用 `operon workflow list --step qc --status failed` 与 `operon workflow show WF_ID` 查看错误和执行详情 |
| 格式解析失败 | 用外部工具（如 `seqkit stats`、GFF3 validator）检查；修复后作为新版本归档，不要覆盖 raw |


## 延伸阅读

不属于单一工作流缺陷的隐式语义、边界情形与已知问题——例如缺失指标的处理方式、多文件
实体的 QC 状态语义、退出码约定等——见[隐式行为、边界情形与已知问题](../reference/behaviors-and-limitations.md)。
