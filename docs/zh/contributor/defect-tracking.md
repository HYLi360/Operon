# 缺陷跟踪

Operon 目前的缺陷跟踪不使用外部 issue 跟踪器，而是用机器可读的注册表记录确认的缺陷：仓库根目录的 `defects.yml`。
注册表是缺陷报告与处置的唯一事实源；[已修复问题](../reference/resolved-issues.md)是面向用户的摘要页，K 系列历史编号与 ODR 系列编号并存。

> K 系列编号只归档于[已修复问题](../reference/resolved-issues.md)。注册表不会补充，也不会追加 K 系列编号。

## 注册表

每个确认的缺陷在 `defects:` 下占一条记录，编号为顺序分配的 `ODR-XXXX`。记录从 `defects.yml` 及所有 `defects/*.yml`
分片（按文件名排序）加载，因此注册表过大时可以拆分，而工具无需改动。

记录字段：

| 字段 | 内容 |
|---|---|
| `id` | `ODR-XXXX`，顺序、唯一（有校验） |
| `title` | 一行摘要 |
| `reported` | 缺陷确认的 ISO 日期 |
| `introduced_in` | 引入缺陷的版本或提交；未确定时为 `null` |
| `affected` | 受影响版本与触发前提 |
| `severity` | `low` / `medium` / `high` / `critical` |
| `component` | 主要模块或子系统 |
| `status` | `open` → `confirmed` → `fixed` → `verified`；`wontfix` / `duplicate` 为终态 |
| `reproduction` | 观察到的行为与复现方式 |
| `disposition` | 处置措施（或不处置的理由） |
| `fix_commit` | 修复落账后的 40 位提交哈希；待提交时为 `null`（仅校验格式） |
| `fixed_in` | 修复随发布交付的版本；交付前为 `null` |
| `regression_tests` | `路径::测试` 列表；`fixed`/`verified` 状态必填 |

使用 `scripts/defects.sh` 追加与查询：

```bash
scripts/defects.sh list [--status open] [--component tools]
scripts/defects.sh show ODR-0001
scripts/defects.sh add --title "..." --severity medium --component tools \
    --reproduction "..."
```

`add` 会分配下一个编号并追加一条 `status: open` 记录。

## 规则

1. **先建档。** 审计或调查确认缺陷后，先把记录追加到 `defects.yml`，再落修复。
2. **一个提交对应一个缺陷。** 修复与其回归测试在同一提交中；提交时回填 `fix_commit`。
3. **测试闭环必须完整。** 每个缺陷回归测试带 `@pytest.mark.bug("ODR-XXXX")`（已在 `pyproject.toml` 注册），
   且每个 `fixed`/`verified` 记录至少列出一个这样的测试。
   `tests/unit/test_defect_registry.py` 校验注册表模式与 marker 闭环的双向一致性——缺记录、悬空 marker、或 `fixed`
   记录缺回归测试都会让测试套件失败。`python -m pytest -m bug` 可只跑缺陷回归。

`verified` 额外要求 `fix_commit` 与 `fixed_in`，在修复随发布交付时设置。
