# Operon 开发指南（面向 AI 编码助手与贡献者）

本文件是本仓库**唯一**的项目要求汇总：所有贡献者（包括 AI 助手）在改动本仓库时**必须**遵守。
文中出现的"**必须**""**不得**""**应**""**建议**""**可以**"等要求级别关键词，按
[RFC 2119](https://www.rfc-editor.org/rfc/rfc2119.txt) 解释。

三件先读的事：

1. **语言**：本文件用中文书写；代码、注释、docstring 与提交信息**必须**用英文（见 §5.1）。
2. **冲突时的优先级**：对外承诺（契约、不变式、版本政策、兼容窗口）以 `docs/` 为准——索引在
   `docs/*/architecture/decisions.md`。本文件若与文档树冲突，**必须**立即修正本文件。
3. **落地要求**：任何改动都要有**实测**证据（测试数字、构建结果），不接受推测数字（见 §9）。

## 一、Operon 是什么

Operon 是一个 Python 编写、以文件为载体的数据库系统，面向大规模基因组数据：归档、元数据管理、
质量控制（QC）、基于规则的判定、确定性自动化、taxonomy 覆盖率审计与版本化数据集发布。每个受管
项目内，唯一可写的真相是单个 SQLite 文件（`operon.sqlite`）；大型序列文件**从不**进入数据库，
入库的只有它们的清单记录、QC 指标与 provenance。

五条核心不变式（编号与 `docs/*/architecture/decisions.md` 一致）：

1. **INV-1** 结构化元数据是唯一真相（阈值、判定、状态都以数据库与版本化配置为准）。
2. **INV-2** 原始数据不可变；派生数据可重建。
3. **INV-3** 文件身份是 `file_id + sha256 + size_bytes`，**永远不是**路径。
4. **INV-4** QC 工具只测量指标；判定只来自版本化的 YAML profile。
5. **INV-5** 每一步处理都是显式、幂等的状态机迁移，并留下机器可读的 provenance。

改动任何代码前，先确认不会破坏这五条；`docs/*/architecture/` 是它们的实现映射。

## 二、仓库结构

### 2.1 源码 `./operon/`

```
./operon/
├── adapters                  # 外部数据源适配器
│   ├── __init__.py
│   ├── ncbi_datasets.py       # 离线优先的 NCBI Datasets 适配器
│   ├── _ncbi_download.py      # requests/aiohttp 下载，Entrez 回退
│   ├── _ncbi_model.py         # 常量、数据模型与不访问数据库的纯函数
│   ├── _ncbi_plan.py          # 导入计划、include 复用匹配与持久化
│   ├── _ncbi_sources.py       # 来源发现、ZIP 安全
│   ├── _ncbi_storage.py       # 各层共用的无状态磁盘空间守卫
│   └── timetree.py            # 从最新 TimeTree 数据库取物种分歧时间
├── backup.py                 # 为当前 operon 数据库生成备份
├── classify.py               # 版本化 sequence_classification profile，标注 sequence_labels
├── cli.py                    # CLI 入口
├── config.py                 # 项目配置与目录布局
├── coverage.py               # 基于快照的 NCBI Taxonomy 覆盖率分母
├── database.py               # SQLite schema 与迁移
├── demo.py                   # 确定性合成演示项目
├── entity_view.py
├── environment_capture.py
├── environment.py
├── errors.py
├── execution.py              # 执行后端（local/Slurm/SSH）
├── export.py                 # 不可变 release 与选择性导出
├── fanout.py                 # 把已登记序列文件按数据派生拆分为 per-unit FASTA，
│                             # 落在 `analysis/derived/` 下，由配方 `file_role_prefix` 选择
├── files.py                  # 不可变清单归档与校验
├── import_wizard.py
├── __init__.py
├── lifecycle.py              # 可审计、可回退的实体退役
├── lineage.py                # 采纳外部工作流产物（adopt）
├── __main__.py
├── metadata_files.py
├── ncbi_reconcile.py         # 开发期适配器异常修复
├── pipeline.py               # ingest → standardize → QC → evaluate 四阶段执行器
├── profiles.py               # 版本化 QC profile 与判定引擎（与 rules.py 配对）
├── qc                        # 全部 QC 功能的归属地
│   ├── alignment.py           # 多序列比对 QC 的 Python 实现（仅作行为参照）
│   ├── _alignment.pyx         # 同一 API 的 Cython 实现（生产使用）
│   ├── imports.py
│   ├── __init__.py
│   ├── measure.py
│   ├── parsers.py             # 内置流式 QC 的 Python 实现（仅作行为参照）
│   └── _parsers.pyx           # 同一 API 的 Cython 实现（生产使用）
├── release.py                # 不可变 release
├── remotes.py                # SFTP 远端镜像（带校验和的 push/pull）与
│                             # `sftp://` / `remote://` URL 抓取
├── reports.py
├── rules.py                  # 版本化 QC profile 与判定引擎（与 profiles.py 配对）
├── schema.py                 # YAML 元数据 schema 与校验
├── secrets.py                # 安全存放令牌（如 NCBI API-Key）
├── sequence_tools.py         # 基于比对的域提取与序列选择
├── shutdown.py               # 优雅处理 SIGINT/SIGTERM
├── sql.py
├── table_import.py
├── taxonomy.py               # 冻结的 NCBI Taxonomy 快照
├── timetree.py               # 冻结的 TimeTree 证据与经人工复核的次级标定
├── tools                     # operon.tools
│   ├── _cache.py              # 可缓存/可采纳的任务查询、环境复用判定、
│   │                          # 陈旧 `RUNNING` 清扫
│   ├── _config.py             # 配方/工具模型、加载、校验、列举
│   ├── _defaults.py           # 默认 `config/tools.yaml`
│   ├── _execute.py            # 逐文件执行与收尾
│   ├── __init__.py
│   ├── _inputs.py             # 候选文件、数据库身份、运行参数、指纹
│   ├── _plan.py               # 逐文件计划
│   ├── _probe.py              # 启动命令、版本探测、命令 provenance、身份探测缓存
│   ├── _results.py            # 各软件的结果解析与 SQLite 回写
│   └── _run.py
├── tui                       # 基于 Textual 的终端界面（`operon tui`）
│   ├── actions.py             # Phase 2/3 写操作：每个函数自开短生命周期可写连接
│   ├── app.py
│   ├── app.tcss
│   ├── assets
│   │   ├── splash.png
│   │   └── splash.rgb.z
│   ├── data.py                # 只读访问层：只用短生命周期只读连接
│   ├── __init__.py
│   ├── parity.py              # CLI/TUI parity 注册表
│   ├── screens
│   │   ├── analyze.py
│   │   ├── backup.py
│   │   ├── classify.py
│   │   ├── common.py
│   │   ├── config_classification.py
│   │   ├── config_coverage.py
│   │   ├── config.py
│   │   ├── coverage.py
│   │   ├── decisions.py
│   │   ├── derived_ops.py
│   │   ├── entities.py
│   │   ├── environments.py
│   │   ├── files_ops.py
│   │   ├── files.py
│   │   ├── hits.py
│   │   ├── home.py
│   │   ├── import_wizard.py
│   │   ├── __init__.py
│   │   ├── labels.py
│   │   ├── ncbi_datasets.py
│   │   ├── publish.py
│   │   ├── remotes.py
│   │   ├── run_external.py
│   │   ├── runs.py
│   │   ├── table_import.py
│   │   └── taxonomy.py
│   ├── splash.py
│   └── splash_terminal.py
├── utils.py                  # 共享工具函数
└── workflow.py               # 状态机与运行日志
```

### 2.2 测试 `./tests/`

```
./tests/
├── compatibility
├── conftest.py       # 测试期通知生命周期，ODR-53
├── helpers.py        # 共享助手
├── __init__.py
├── integration
├── regression
├── tui_helpers.py    # TUI 共享助手
└── unit
```

### 2.3 文档 `./docs/`

```
./docs/
├── _build                 # 本地 Sphinx 构建产物
├── conf_common.py         # 两棵树共享的设置
├── locales
│   └── README.md
├── requirements.txt
└── <language>             # {en, zh}
    ├── .readthedocs.yaml  # RTD 项目信息（en 为 `operonproject`，其余为 `operonproject-<language>`）
    ├── architecture
    ├── conf.py
    ├── contributor
    ├── getting-started
    ├── guides
    ├── index.md
    ├── operations
    ├── overview.md
    └── reference
```

### 2.4 开发者脚本 `./scripts/`

```
./scripts/
├── defects.py            # \
├── defects.sh            # - 列出、查看或登记 defects.yml 中的缺陷
├── release-preflight.sh  # 发布前检查
├── run-test-matrix.sh    # \
└── setup-test-matrix.sh  # - 本地测试矩阵：必须先 setup 再 run
```

## 三、环境、测试与构建

始终在项目虚拟环境里工作（仓库根目录已有 `.venv/`；激活它，或显式调用 `.venv/bin/python`）。

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'   # 运行时 + pytest + Cython + Sphinx；
                                    # 同时编译 qc parsers 扩展

python -m pytest                    # 全量套件（含覆盖率门禁）
python -m pytest tests/unit         # 按类目：unit / integration /
                                    # regression / compatibility

python -m pytest --lf -q --no-cov   # 迭代：只跑上次失败，不测覆盖率
python -m pytest -n 4 --dist loadfile  # 并行（pytest-xdist）
scripts/setup-test-matrix.sh        # 每台机器一次：uv 管理的 3.10–3.15 环境
scripts/run-test-matrix.sh          # 在 3.10–3.15 上分批跑全量套件
                                    # （MATRIX_CONCURRENCY / MATRIX_JOBS 覆盖分批参数）
scripts/release-preflight.sh        # 发布闸门：版本、全量套件、两棵文档树、
                                    # 缺陷登记表、HEAD 的矩阵证据；
                                    # --run-matrix 在闸门内跑矩阵，
                                    # --tag vX.Y.Z 校验 tag 的名称/签名/指向

python setup.py build_ext --inplace # 只重建 Cython 扩展

sphinx-build -W --keep-going -b html docs/en docs/_build/en/html  # 严格构建，
sphinx-build -W --keep-going -b html docs/zh docs/_build/zh/html  # 每种语言一棵
```

任何改动后都要跑**相关类目**；在认为工作完成前**必须**跑全量套件。

CI（`.github/workflows/test.yml`）在 Python 3.10–3.15 上跑 pytest 与严格 Sphinx 构建。发布只发到
PyPI，且 `.github/workflows/publish.yml` 的 `verify-release` 作业必须看到被打 tag 提交的 `test`
运行为 `success` **且** `scripts/release-preflight.sh --ci --tag <tag>` 通过，才会构建与上传；
细节见 `docs/*/contributor/pypi-release.md`。

项目使用 `pytest-xdist` 并行测试：**不要**在测试之间共享状态，否则会出现随机失败。

参考耗时（本机 i7-13700HX 实测，仅供参考，不是承诺）：`python -m pytest` 全量约 3–4 分钟（覆盖率
开启），`--no-cov` 更快；本地矩阵约 3 分钟（3 个版本并行 × 4 workers）。预算与测量口径见
§8 与 `docs/*/operations/performance-budgets.md`。

## 四、源码约定（Conventions）

- **Python 3.10+**。以 `pyproject.toml` 为依赖的唯一权威：`[project.dependencies]` 是核心运行时依赖，
  `[project.optional-dependencies]` 是可单独安装的 extras（`test`、`docs`、`dev`）。运行时功能的
  extra 必须由其功能路径**惰性导入**（例如 Paramiko 只在远端/SSH 代码内导入）；测试/构建类 extra
  **不得**进入正常运行时路径。**不得**把某个 extra 提升为 core，也**不得**新增核心运行时依赖，
  除非用户明确授权该依赖；一个依赖获批**不**代表其他依赖也获批；仅仅"告知"用户不算授权，新的可选
  依赖仍须明确报备并放进最窄合适的 extra。
- **绝不静默覆盖已归档文件**：同一 entity + role、字节不同时**必须**抛 `ConflictError`；字节相同则**必须**幂等。
- **手动覆盖必留痕**：`curate`、强制 `set-state` 等一律记入 `changes` 审计表。
- **CLI 先行**：新能力先落在 CLI/核心，TUI 永不走在前头。任何 CLI 命令、flag 或 TUI 表面的改动，
  **必须**在同一提交里更新 parity 注册表 `operon/tui/parity.py`——否则
  `tests/unit/test_tui_cli_parity.py` 会失败。有意不在 TUI 提供的命令登记为 `cli-only` 并写明理由；
  已知缺口登记为 `planned` 并写明里程碑；CI 的 `pytest` 作业导出 `OPERON_PARITY_STRICT=1`，任何再次
  出现的 `planned` 条目都会让构建失败。
- **TUI 的读写边界**：读路径（`operon/tui/data.py`）只使用短生命周期的只读连接；写操作
  （`operon/tui/actions.py`）各自打开短生命周期的**可写**连接，调用与 CLI 相同的核心函数，并留下相同的
  `changes`/`workflow_runs` provenance；UI 绝不长期持有可写连接。
- **TUI 写路径契约**：表单/预览 → 显示等价 CLI 命令 → 显式 Confirm → 后台 worker → 通知并重载，
  或就地报错。写操作失败时**必须**回滚到原字节。
- **"建模键"式表单写入的语义是完整状态**：文档携带的键被写入，省略的键被删除（回落默认值）；
  未建模的键逐字保留，文档里给它们的值一律忽略。
- **可审计、可回滚的配置写入**：profile/recipe/tool 的保存 = 原子写 + 往返校验 + 失败时恢复原字节，
  并为每个受影响的 recipe 记录一条内容寻址快照（未变化的保存是 no-op）。
- **名字校验**：任何由表单写入的配置名/文件名**必须**先校验（拒绝路径穿越与非法字符），绝不把
  用户输入直接拼进路径。
- **阈值不写进代码**：QC 阈值只属于版本化 YAML profile（受管项目的 `config/profiles/` 下）。
- `docs/*/operations/database-compatibility.md` 列出了只为 pre-1.0 数据库而存在、并计划在 1.0 移除的
  迁移代码；改动 `operon/database.py` 的迁移或 NCBI 适配器 schema 升级路径前，先查该页。
- 代码用 `ruff` 格式化。

## 五、项目纪律（Project Discipline）

> 本节表述的是项目规格（specification），所有贡献者（包括 AI 助手）**必须**遵守。

### 5.1 语言

`README.md` 为英文；`README_ZH.md` 与 `AGENTS.md`（本文件）为中文。代码、注释、docstring 与提交
信息**必须**为英文。

### 5.2 Git 与 GPG

**不得**推送任何提交或 tag，除非用户**明确**要求。

若提交超时，假定用户不在场且需要 GPG 签名口令；可以在 `git` 后加 `-c commit.gpgsign=false` 作为
权宜之计（注意：重新签名会改变提交哈希），或**建议**用户使用密码库，以免每次都要输入 GPG 口令。

### 5.3 提交信息

- 提交标题**必须**符合 [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) 的相关规定。
- 3 行以内的微小改动**建议**只写标题、省略正文；20 行及以上的改动**必须**写正文。
- 所有提交**必须**经 GPG 签名。
- 回填 `defects.yml` 的 `fix_commit`/`fixed_in` 时，**应**使用以下格式：
  `docs(defects): backfill ODR-X's fix_commit/fixed_in`（单个 ODR），或
  `docs(defects): backfill fix_commits/fixed_ins for ODR-X, ODR-Y, ODR-Z`（多个 ODR）。

### 5.4 文档同步

当改动行为、CLI 表面、配置字段或存储布局时，**必须**在同一变更里更新**所有语言树**
（`docs/en/` 与 `docs/zh/`）：

- CLI 命令/flag → `docs/*/reference/cli-*.md`
- 任务级工作流 → `docs/*/guides/` 与 `docs/*/getting-started/`
- 架构、数据模型、状态机、正确性保证 → `docs/*/architecture/`
- `tools.yaml` 的配方/占位符/解析器 → `docs/*/reference/recipe-*.md`
- 迁移、性能诊断、兼容边界 → `docs/*/operations/`
- 面向贡献者的流程 → `docs/*/contributor/`；导航 → `docs/*/index.md`
- 架构、契约、版本政策、兼容窗口等**对外承诺** → `docs/*/architecture/decisions.md`（决策索引）。
  对外承诺**必须**落在 `docs/` 里；`others/` 只放内部推演与里程碑记录，不进版本控制，也**不得**
  作为对外依据。
- 性能预算 → `docs/*/operations/performance-budgets.md`
- 语言项目集合 → `docs/<language>/conf.py`、其 `docs/<language>/.readthedocs.yaml`，以及
  `tests/unit/test_docs_projects.py` 中的注册表

如改动还影响到 `AGENTS.md`、`README.md` 和（或）`README_ZH.md`，**应**将上述三个文件视为文档，
并在同一变更里同步。

### 5.5 版本号

当前版本号**不得**在任何文档里写死。`./docs` 内任何需要引用主程序版本、数据库 schema 版本或元数据
schema 版本的位置，**必须**使用替换占位符（`{{ operon_version }}`、`{{ db_schema }}`、
`{{ metadata_schema }}`）。需要记录与版本相关的特性或问题时，在该行加 `<!-- version-pin -->` 以豁免。

所有当前版本号都有单一、权威的来源：

- 主程序：`pyproject.toml`
- 数据库 schema：`operon/database.py` 的 `SCHEMA_VERSION`
- 元数据 schema：`operon/schema.py` 的 `METADATA_SCHEMA_VERSION`

因此直接改动它们来 bump 是安全的。需额外注意：若已在 `pyproject.toml` bump 主程序版本号，**必须**
先执行一次 `python -m pip install -e .`，然后再运行测试套件。

运行 `operon -v` 或 `operon --version` 可查看：

```
> operon -v
Operon the Database System
Main program version:    XXXXX
Database schema version: YYYYY
Metadata schema version: ZZZZZ
```

该输出**必须**写 stdout（保证 `operon -v | …` 这类管道可用）并以退出码 0 结束；`-v` 与 `--version`
等价。`tests/compatibility/test_python_support.py` 固定这条契约（三行标签与去尾空白后的逐行内容）。

### 5.6 静态检查与分支保护

本仓库的静态检查与分支保护由 Codacy 负责。本地复现方式见
`docs/*/contributor/development-testing.md` 的「本地 Codacy 分析」。

### 5.7 测试与覆盖率

测试细节见 §三。`master` 上的提交与分支合并均**必须**符合覆盖率门禁：

- 对于 `master` 上的提交：总覆盖率需达到 95% 以上（**硬**，由 `pyproject.toml` 管理）、分支覆盖率需
  达到 95% 以上（软），单次提交的覆盖率降低需低于 0.1%（软）；
- 对于从其他 worktree 合并来的提交（需经过 Pull Request）：总覆盖率需达到 95% 以上（**硬**）、
  分支覆盖率需达到 95% 以上（软）、单次提交的覆盖率降低需低于 0.1%（**硬**，由 Codacy 管理）、
  diff 部分的覆盖率需达到 85% 以上（**硬**，由 Codacy 管理，如适用），且代码质量需符合 Codacy 要求
  （不新增任何 medium 程度的 issue，不新增任何 minor 程度的 security issue）。

**门限随水位抬升（建议）**：当一次发布或一次大改动把实测总覆盖率抬升到门限之上时，**应**在同一次
变更里把 `fail_under` 抬到新水位下方约 1 个点，避免缓慢侵蚀悄无声息地通过；配套细则见
`docs/*/contributor/development-testing.md` 的「覆盖率门禁」。

#### Textual 的竞态缺陷

为避免新增 Textual（TUI）竞态缺陷，改动 TUI 代码时**必须**注意：

- 断言 UI 状态前必须**谓词等待**（等待被测的具体状态），不得"裸 `pilot.pause()` 之后直接读"，
  也不得直接读 `app._notifications`。
- 框架级 guard 的修理要走 `FittingSelect` 式的**最小覆盖**：在能覆盖全部调用路径的最小封装里修，
  而不是修在恰好先失败的那个调用点。
- 改动后，以随机序在本地执行**至少 3 次** `pytest tests/unit/test_tui*.py`，以充分暴露竞态问题。

背景、命名（ODR-0027 / ODR-0050 / ODR-0051）与「最小覆盖」的判定标准见
`docs/*/contributor/development-testing.md` 的「TUI 竞态缺陷」小节；那里的规则与本节等效，
任何一项变更都**必须**同时满足两处。

#### 使用沙盒的 AI 助手特别注意

受沙盒环境的特有约束，执行 TUI 相关测试代码时，可能因 Textual/asyncio cleanup block 而报告测试
失败。该问题常发生于 Codex/ChatGPT，但理论上任何使用沙盒环境的 AI 助手都可能触发。

对于任何 AI 助手：如果你确实在沙盒环境中、且需要运行完整测试套件，**必须**在尝试执行前向用户
警告该问题；如有必要，**建议**请求用户在沙盒外执行 TUI 测试。

### 5.8 代码与文档风格

- 代码使用 `ruff` 格式化（提交前对被改动的文件跑一次，且**不得**引入新的告警）。
- Markdown 文档**建议**采用 [markdownlint](https://github.com/davidanson/markdownlint) 格式化，
  但不作强制要求。

## 六、缺陷登记（Defect reports）

确认的缺陷记录在仓库根目录的机器可读登记表 `defects.yml`（以及可选的 `defects/*.yml` 分片；
`scripts/defects.sh` 负责追加与查询）。完整流程见 `docs/*/contributor/defect-tracking.md`，
其中**强制**规则有三条：

1. **先登记**。当一次审计或调查确认了缺陷，必须先把它的 `ODR-XXXX` 记录追加进 `defects.yml`，
   修复才能落地。
2. **一次提交修一个缺陷**。修复与其回归测试在同一提交里；提交时回填 `fix_commit`。
3. **闭环测试**。缺陷的回归测试带 `@pytest.mark.bug("ODR-XXXX")`，且每条 `fixed`/`verified` 记录
   至少列出一条这样的测试；`tests/unit/test_defect_registry.py` 校验登记表 schema 与这个闭环的
   两个方向。

时序相关的测试失败（尤其 TUI）是**要登记的缺陷**，不是可以重跑带过的 flake：先复现，再登记，再修。

## 七、决策与对外承诺

- `docs/*/architecture/decisions.md` 是决策索引：核心不变式（INV-\*）、扩展边界（EXT-\*）、
  契约与闸门（GATE-\*）逐条列出，并写明各自的"执法者"（测试、闸门或 CLI 表面）。
- **对外承诺必须落 `docs/`**：契约、不变式、版本政策、兼容窗口只有写进文档树才生效；计划草稿、
  里程碑记录与推演过程放在 `others/`（不进版本控制），**不得**作为对外依据。
- **开放问题也要在 `docs/` 里回答**：尚未裁定的问题在决策索引的「开放问题」中列出；在它被提升为
  决策之前，外部消费者**不得**依赖其中任何一种结果。
- 变更一项契约时，同一变更里**必须**完成三件事：①更新决策记录；②更新它的执法者（测试/闸门/CLI
  表面）；③更新两棵文档树（若 `README*` 或本文件有引用，一并同步）。

## 八、性能预算

- 性能预算的定义、夹具、测量方法与"数字如何入库"见 `docs/*/operations/performance-budgets.md`
  （对应决策索引里的 GATE-7）。
- 规则：**预算是上限而非平均值**，且**必须能被第三方复现**（数字旁边要有命令）。
- 若一次改动使某项测量劣于已记录的预算，该改动**必须**要么修回归、要么带着测量理由更新预算——
  默默接受更慢的路径不是选项。
- 加入第一个预算数字时，**必须**与产生它的测量脚本在同一变更里落地。

## 九、交付、汇报与检查清单

### 9.1 交付与汇报

- 交付 = **可运行/可验证的产物**，不是描述。声称完成之前，必须给出**实测**证据：相关测试的
  通过数字、覆盖率、构建结果（如 Sphinx 退出码）、必要时 CI run id 与逐腿结论。
- 汇报按**条目**组织：每项写清改了什么、对应的提交哈希、以及验证数字；**禁止**用推测或"应该没问题"
  代替实测。做不到或无法验证的，如实说明并给出替代路径或向用户提问。
- 被阻塞时先说明阻塞点，再给可选项；不要用看起来合理的假数据填补。

### 9.2 计划与里程碑记录

- 计划文件与里程碑记录放在 `others/`（该目录被 `.gitignore` 排除，不随仓库分发）：计划放
  `others/plans/`，里程碑记录写在对应的计划文件或该阶段的记录文件里。
- 落地后**必须**回填提交哈希与验证数字；**不得**把未落地项描述成已完成。

### 9.3 提交前检查清单

1. 读相关文档与既有实现（`docs/*/`、相邻模块、既有测试），不猜 API 形状。
2. 改动面尽可能小；不顺手重构、不改格式、不动无关文件。
3. 新增/修改行为**必须**伴随测试；缺陷修复**必须**带 `@pytest.mark.bug("ODR-XXXX")` 回归测试。
4. 跑相关测试类目；认为完成前跑全量 `python -m pytest`。
5. 覆盖率不下降；触及门禁阈值时按 §5.7 处理。
6. 若改动了 CLI 命令/flag 或 TUI 表面：同一提交更新 `operon/tui/parity.py`。
7. 双语文档已同步（§5.4）；涉及版本号的位置使用占位符（§5.5）。
8. 确认的缺陷已在 `defects.yml` 登记并回填 `fix_commit`（§六）。
9. `ruff` 无误、无新增告警；Markdown 尽量符合 markdownlint。
10. 提交信息符合 §5.3（Conventional Commits、必要的正文）；提交经 GPG 签名。
11. **不**推送、**不**打 tag，除非用户明确要求。

## 十、常见陷阱

- **Textual 的三类异步时序**（挂载、绘制、通知投递）会让"先读后断言"的测试随机变红——见 §5.7；
  正确做法是谓词等待。
- **覆盖率门禁是合并门**：只增加行而不覆盖其分支同样会拉低合并覆盖率；`# pragma: no cover` 不能
  替代测试。
- **文档里写死版本号**会被 `tests/unit/test_docs_versions.py` 抓住；请用替换占位符。
- **bump 主程序版本后**必须先 `python -m pip install -e .`，否则测试跑的是旧版本。
- **单解释器通过 ≠ 完成**：跨版本矩阵与 CI（Linux + macOS）才是放行标准。
- **归档不可覆盖**：同 entity + role 字节不同会抛 `ConflictError`，这是设计而不是缺陷，不要"修"它。
- **`defects.yml` 是三段式闭环**：登记记录、`fix_commit` 回填、带 marker 的回归测试，缺一不可。
- **`others/` 不进版本控制**：把对外承诺写在那里等于没写（见 §七）。
- **TUI 测试在沙盒里可能假红**：见 §5.7 的沙盒提醒。
