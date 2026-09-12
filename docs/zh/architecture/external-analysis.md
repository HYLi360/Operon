# 外部分析执行模型

## 外部分析执行模型

外部 BLAST/HMMER/BUSCO 等程序不再需要手工拼接命令。`config/tools.yaml` 中的
recipe 声明输入类目、artifact 类型、启动方式、参数和结果解析器；`analyze` 命令自动：

1. 从 `files` manifest 中选出匹配 `entity_type + file_role + format` 的全部输入；
2. 按 `input_kind` 校验输入文件或目录仍存在且内容哈希与 manifest 一致；
3. 探测程序版本（`version_args + version_pattern`）并记录到 `analysis_jobs`；
4. 计算参考数据库身份（单文件 SHA-256 / 目录指纹 / 显式 checksum）；
5. 按 `analysis_name + file_id + 输入 SHA + 参数指纹 + 工具版本 + 数据库身份` 查找已完成缓存，命中则跳过；
   未命中时进入第二级续跑：同一 `(analysis, file_id)` 的旧 `completed` 作业若输入内容哈希一致且
   记录的输出 artifact 逐字节验证通过，则收养该输出（以当前指纹插入新 `completed` 行并在
   `changes` 审计表留痕，状态记为 `adopted`），否则才重算；
6. 未命中时以 `conda run`、容器前缀或直接路径启动程序；文件与目录输出都必须存在且非空，stdout/stderr 落盘；
7. 计算文件或目录内容哈希，解析 top hits 或 BUSCO JSON summary 写入
   `analysis_hits`/`analysis_results`，并同步同名指标到 `qc_results`。带坐标的 parser
   （`blast_tabular`、`hmmer_domtblout`、`rpsbproc_tabular`）还会把每条解析出的命中以结构化行写入
   `analysis_alignments`（query/subject ID、命中排名、query/subject 区间、e-value、
   bitscore、identity 百分比，未映射列进 `extra_json`），不受 `max_hits_per_query`
   截断；`report analysis --hits` 读取该表。

目录使用由相对路径、空目录、文件大小/内容和符号链接目标组成的确定性树哈希。
`database_mode: mutable_cache` 用于 BUSCO 等会逐步下载 lineage 的共享缓存，以显式
`database_version` 标识其逻辑版本；不可变参考库仍使用默认的 `reference` 内容身份。

recipe 也可以用 `commands` 命令链代替单命令（与 `arguments` 互斥）。渲染后的各步按顺序
通过同一个 executor 与顶层 tool 的 `run_method` 执行（一个 recipe 一个环境，是刻意的
能力限制），共享一条运行记录与一条 `analysis_jobs` 行。第一个命令是 recipe 的逻辑归属：
作业的 `tool_version` 与缓存身份中的版本成分都来自它——优先取第一个块自己声明的
`version_args`/`version_pattern`，否则回退到顶层 tool 的探测（recipe 加载时强制二者指向
同一程序）。调用顶层 `executable` 的 step 继承已经探测的 tool 版本；其他程序可在
command block 中声明自己的 `version_args` 与 `version_pattern`，并经同一个 launcher/executor
探测。未配置的附加程序明确记录为 `unknown`，系统不会猜测版本参数。每步有独立的
`logs/<run_id>.step<N>.stdout.log` / `.stderr.log`，每步的 `argv`、`executable`、
`tool_version`、`tool_version_raw`、`version_source`、`version_command` 与 `exit_code` 都记录在
`execution_details.steps` 中。第一个非零退出的步骤
中止整条链并使 job 失败，错误形如
`step N/M failed`。中间产物放在确定性的 `${work_dir}` 暂存目录（输出旁的
`<output_name>.work`），执行前删除重建、成功或失败后移除（`--keep-partial` 保留）；路径
必须确定，因为渲染后的命令参与参数指纹与缓存身份。期望输出校验、内容哈希与结果解析在
最后一步之后执行一次。SSH 后端配置 remote root 时，该暂存目录与项目根下其他路径一样映射
到远端 root 并在远端创建。典型例子是把 `rpsblast -outfmt 11`（ASN.1 归档）与 `rpsbproc`
耦合，后者的表格报告由 `rpsbproc_tabular` 解析：`DATA`/`SESSION`/`QUERY`/`DOMAINS` 块中的
domain 行成为全量 `analysis_alignments` 行（query 取 QUERY 的 definition line，subject 取
accession，坐标来自 `from`/`to` 列，hit type/PSSM ID/short name 留在 `extra_json`），EAV
hits 照常按 `max_hits_per_query` 截断。每个探测到的步骤版本都混入缓存指纹：升级任何一步的
程序（即使 recipe 文本与主工具版本未变）都会使精确缓存失效，验证输出收养照常适用。

外部命令的实际执行由 `execution.py` 的后端抽象接管，`run_external_command` 通过
`get_executor(project, backend)` 选择后端：

- `local`（默认）：原有本地子进程行为，完全不变；
- `slurm`：本地 Slurm 集群。在 `logs/` 下生成 `<run_id>.sbatch` 批处理脚本
  （`--cpus-per-task` 取线程数，可选 `--time`/`--partition`/`--mem`、
  `extra_sbatch` 与 `setup_commands`），用 `sbatch --parsable` 提交并按
  `poll_interval` 轮询 `squeue`，作业消失后读取脚本写入的 `<run_id>.exitcode`
  退出码文件（失败时回退 `sacct`）；前提是项目目录位于与计算节点共享的
  文件系统上；
- `ssh`：通过核心运行时依赖 paramiko（惰性导入）在 SSH 远程主机
  （HPC 头节点/云虚拟机）上执行；`execution.ssh.scheduler: slurm` 时改为在远端
  走 sbatch/squeue。支持 `remote_root` 路径映射（空表示共享文件系统）；输入
  文件经 SFTP 上传（内容一致跳过，严格 SHA-256/目录树哈希；不同内容拒绝覆盖）；
  若配置 `storage_remote`，REMOTE_ONLY 输入在远端原位消费。运行前清除精确计算出的
  远端旧输出，expected outputs 经临时文件拉回并与远端内容再次比对；已有本地输出
  只有内容完全相同时才接受。`storage_remote` 与显式 `remote_root` 必须指向同一 root；
  一个分析批次以一个惰性 SSH client 完成版本探测、远端输入验证、数据库预检和所有
  命令，结束时统一关闭。直连命令以 util-linux `setsid --wait` 在独立进程组中运行
  （保证退出码可靠回传），超时时根据受限 PID 文件向远端进程组发送 TERM/KILL，
  无法发出终止请求时在错误与 provenance 中明确提示进程可能仍在运行。远端 Slurm
  严格按 `poll_interval` 轮询，并对作业消失后短暂不可见的 exitcode 文件重试。

三个后端共用同一份 provenance 与正确性契约：退出码、起止时间、日志照常写入
`workflow_runs` 与 `logs/workflow.jsonl`；SQLite 额外保存 executor、scheduler job ID
与资源/脚本详情，成功判定与输入/输出 SHA-256 校验不变；
工具版本探测在非 `local` 后端时也经同一后端执行。单个 recipe 可用 `slurm:`
mapping 覆盖 `execution.slurm` 的同名字段（如给 BUSCO 单独调内存/时间）。

执行环境捕获（`environment.py`、`environment_capture.py`）将规范化 JSON 存入 `execution_environments`，通过 `environment_id` 寻址。工作流和分析任务引用该文档；命令链的每一步也记录各自的环境 ID。扩展 JSON 文档使用 `capture_schema: 1`，无需数据库迁移。文档在捕获时、入库前完成脱敏：hostname 替换为 `sha256:` 加其 SHA-256 的前 16 位 hex；`PATH`、`CONDA_PREFIX`、`VIRTUAL_ENV` 及 conda prefix 等类路径值的 `$HOME` 前缀替换为 `~`。远端探测传输原文（探针文件以 umask 077 写入），由控制端 Python 进程在入库前脱敏；瞬态的 `home` 探针键仅用于替换，随后被丢弃，既不进入入库文档也不进入任何指纹。`environment_id` 与各子指纹由脱敏后的文档计算，因此同配置在不同主机上的捕获去重到同一条记录。引入脱敏之前写入的文档按内容寻址、不可变，不做迁移；新捕获产生新的环境 ID。

local、Slurm 和 SSH 后端在执行计算命令前探测。Slurm 在计算节点执行 `setup_commands` 后探测；直接 SSH 在计算命令 shell 的工作目录内探测。识别到 `conda`、`mamba` 或 `micromamba run` 时，通过相同启动器及目标名称/prefix 执行探测。读取 `conda-meta/*.json` 不要求目标环境安装 Python 或包管理器。直接运行的程序继承执行器环境。已知的不透明 shell/容器启动器及不支持的包管理器选项标记为 `unsupported_launcher`，并设置 `capture_scope: executor_only`，不会把外层 Conda 环境宣称为工具环境；不解析任意自定义包装程序内部行为。

Conda 快照保存包名、版本、build、subdir、依赖、安装包 URL 和可用的 SHA-256/MD5。完整清单包含 `@EXPLICIT` 重建规范，优先使用 SHA-256，缺少时使用 MD5。包指纹排除安装 prefix 和主机名。URL 中的用户凭据、`/t/` 令牌和查询参数会被移除，因此私有 channel 可能需要在重建时另外提供凭据。通过 `INSTALLER` 元数据检测到的 pip 发行包名单独记录；Conda 规范不恢复 pip 包、editable 安装、手动修改的文件或自定义激活脚本。包清单描述原始安装包，并不验证已安装文件内容。空清单、损坏记录、中断捕获或缺少可解析安装包身份的清单不能导出为完整重建规范。

独立的 `system_fingerprint` 和 `hardware_fingerprint` 分别覆盖 OS/内核/架构、发行版及 glibc，以及 CPU 型号/特性、总内存和可获取的 NVIDIA GPU 型号/驱动/计算能力。主机名、PATH、prefix、CPU 亲和性和运行设置不进入这两个指纹；选定的 locale、时区、线程/设备环境变量及 CPU 亲和性作为上下文保留。探测主要面向 Linux；CPU/GPU 信息缺失时明确标记不可用（GPU 不存在与探测失败尚不区分），不收集动态指标、GPU UUID 或设备序列号。硬件指纹描述已观测字段，字段不可用时不能据此保证硬件等价。

探测采用尽力而为策略，总时限 30 秒（NVIDIA 查询为 5 秒），依赖 POSIX shell、`base64` 和常见系统工具。宿主提供 GNU `timeout` 时用它限时；否则（macOS 没有 `timeout`）由 POSIX 看门狗施加同样的 30 秒上限，因此缺少该工具的宿主仍会完成探测，而不是标记 `unavailable`。`/proc` 等仅 Linux 可用的数据源只会让对应字段保持 unavailable。`capture_status` 区分 complete、partial、failed、unavailable 和 unsupported_launcher。旧文档仍可读取，可能没有这些字段。保留旧的控制端探测作为后备，其 Python 版本不能解释为目标解释器版本。

环境指纹同时驱动 recipe 级的复用策略：可选字段 `environment_policy` 取值为 `ignore`、`warn`（默认）或 `strict`，其他取值在配置校验时报错。该字段仅在显式设置时进入参数指纹——与 `hmmer_mode` 和比对列映射键同一机制——因此设置或变更它只会让完成缓存精确失效一次。以非 `ignore` 策略命中完成缓存时，`operon` 会比对"环境相关性指纹"：由文档中的 `system_fingerprint`、`hardware_fingerprint` 与 `conda.package_fingerprint` 复合而成。hostname、路径、CPU 亲和性和运行时线程变量刻意排除在外——线程数已经参与参数指纹，而亲和性是瞬时调度状态。指纹一致时照常复用缓存；不一致时，`warn` 复用缓存结果、同时在 run details 中记录警告并打印，`strict` 则把命中视为未命中并重算。没有作业前探针的后端（`slurm` 与远端 Slurm）无法在复用前比对，因此 `strict` 降级为 `warn`，并在 run details 中记录该降级。任一侧环境文档缺少子指纹（数据库 schema {{ db_schema }} 之前捕获的记录）时，比对记为 `unavailable`：`warn` 照常复用，`strict` 同样降级为 `warn`，旧的 provenance 永远不会引发误重算。

Recipe 版本与快照（schema 2.9）：<!-- version-pin -->recipe 新增可选 `version:` 字段（正整数，缺省 1，
非法值在配置校验时报错）。`analyze` 处理每个候选文件时把当前 recipe 连同其引用的
tool spec 原文记录到 `recipe_snapshots` 表：快照文档为
`{"recipe": <recipe 原文 mapping>, "tool": <引用的 tool spec 原文>}`，经规范化 JSON
的 SHA-256 内容寻址，以 `UNIQUE(recipe_name, recipe_version, recipe_sha256)` 去重——
因此工具定义变更同样产生新快照，缓存命中也会记录当前配置的快照。
`analysis_jobs.recipe_snapshot_id` 回指产生该作业的精确配置；续跑收养的作业继承原
作业的快照 id（它由那份配置产生，而非当前配置）。查看用 `operon recipes list /
history / show`，QC profile 侧对应的 `qc_profiles` 快照用 `operon profiles
history / show`；在 CLI 中恢复均为 print-only，由人工把输出写回配置 YAML，CLI
不做原地改写。带审计的替代途径是 TUI 的 Config 界面：其结构化编辑器把每次变更
保存为下一个版本并记录新快照，也可以把任何已记录的快照载入编辑器（保存即创建
下一个版本）；TUI 保存 recipe 时会规范化 `tools.yaml` 格式并丢弃手写注释。

运行资源使用记录（schema 2.9）：<!-- version-pin -->`workflow_runs` 新增 `duration_seconds`（墙钟秒数，
此前只进 JSONL）、`avg_rss_mb`（平均 RSS）与 `cpu_seconds`（核时）三列，既有
`max_rss_mb` 列现在真正填充。采集按后端实现：

- `local`：存在 procfs 时由采样线程轮询 `/proc/<pid>/status` 的 VmRSS，否则
  使用 POSIX `ps` 的 RSS 字段（包括 macOS）得到峰值与平均；核时取
  `getrusage(RUSAGE_CHILDREN)` 的运行前后差值；
- `slurm`：作业结束后以扩展字段查询 `sacct`（`MaxRSS`/`AveRSS`/`Elapsed`/
  `TotalCPU`）；远端 Slurm（`ssh` + `scheduler: slurm`）走同一路径；
- `ssh` 直连：远端 POSIX 采样循环汇总隔离进程组的 RSS，把统计写入 stats 文件并
  读回（直连模式 `cpu_seconds` 留空）。

任何后端采集不到对应指标时该列留 NULL，不报错也不影响任务本体；资源数据只用于
审计与容量评估，不参与成功判定。

`run-external` 的 provenance 与 `analyze` 对齐：`--tool NAME` 命中 `config/tools.yaml`
中已配置工具时自动探测版本并记录 `tool_version` 与 `tool_version_raw`（探测失败降级为
warning，不阻断运行）；`--input PATH`（可重复）声明输入文件，逐文件 SHA-256 的组合
哈希写入 `input_sha256`，完整清单进入 `execution_details`；`--threads` 记录向执行
后端申请的线程数。

优雅停机（`shutdown.py`）：`analyze` 批次运行期间安装 SIGINT/SIGTERM 处理器，信号被
转换为 `ShutdownRequested`（`KeyboardInterrupt` 子类）在主线程抛出，沿常规异常路径
完成清理——`local` 后端子进程以 `start_new_session` 独立进程组启动，中断/超时时对整个
进程组（含孙进程）先 TERM 后 KILL；`slurm` 与远端 Slurm 后端在中断时 `scancel` 当前
作业；`ssh` 直连后端复用超时路径的远端进程组 TERM/KILL。当前 `analysis_jobs` 行被置为
`interrupted`（不参与完成缓存命中），半成品输出默认删除（`--keep-partial` 保留），批次
随即以退出码 130 终止；清理期间第二次信号立即强制退出。被 SIGKILL 杀死的进程留下的
`RUNNING` 行会在下一次 `analyze` 启动时清扫为 `interrupted`，保证续跑语义始终成立。

日常使用见 [How-to 操作手册](../guides/index.md)；字段、占位符、artifact、数据库身份、缓存和
parser 的完整契约见 [Recipe 配置参考](../reference/recipe-overview.md)。
