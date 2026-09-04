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
   `analysis_hits`/`analysis_results`，并同步同名指标到 `qc_results`。

目录使用由相对路径、空目录、文件大小/内容和符号链接目标组成的确定性树哈希。
`database_mode: mutable_cache` 用于 BUSCO 等会逐步下载 lineage 的共享缓存，以显式
`database_version` 标识其逻辑版本；不可变参考库仍使用默认的 `reference` 内容身份。

外部命令的实际执行由 `execution.py` 的后端抽象接管，`run_external_command` 通过
`get_executor(project, backend)` 选择后端：

- `local`（默认）：原有本地子进程行为，完全不变；
- `slurm`：本地 Slurm 集群。在 `logs/` 下生成 `<run_id>.sbatch` 批处理脚本
  （`--cpus-per-task` 取线程数，可选 `--time`/`--partition`/`--mem`、
  `extra_sbatch` 与 `setup_commands`），用 `sbatch --parsable` 提交并按
  `poll_interval` 轮询 `squeue`，作业消失后读取脚本写入的 `<run_id>.exitcode`
  退出码文件（失败时回退 `sacct`）；前提是项目目录位于与计算节点共享的
  文件系统上；
- `ssh`：通过 paramiko（可选依赖 `OperonDBS[remote]`，惰性导入）在 SSH 远程主机
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

执行环境捕获（schema 2.8，`environment.py`）：三个后端都会在每次运行时探测执行环境，
规范化 JSON 文档写入 `execution_environments` 表；其主键 `environment_id` 是该规范化
JSON 的 SHA-256（内容寻址，同内容自动去重），`workflow_runs` 与 `analysis_jobs` 通过
各自的 `environment_id` 列引用。

- `local`：直接收集 hostname、OS/kernel/架构、Python 与 `operon` 版本，
  以及 `PATH`、`CONDA_PREFIX`、`CONDA_DEFAULT_ENV`、`VIRTUAL_ENV`、
  `SINGULARITY_NAME`、`APPTAINER_NAME`、`container` 等环境变量和 docker 探测结果；
- `slurm`：在 sbatch 脚本中嵌入探针，作业内把结果写入 `<run_id>.env` 后读回——
  探到的是计算节点环境，而非提交节点；
- `ssh`：直连模式通过 paramiko 在远端执行探针；远端 Slurm 模式嵌入并读回同一份
  作业内探针，因此记录计算节点而不是 SSH 登录节点。

探针失败只把该次运行的 `environment_id` 留为 NULL，不报错也不影响运行；2.8 之前的
历史行同样为 NULL。

Recipe 版本与快照（schema 2.9）：recipe 新增可选 `version:` 字段（正整数，缺省 1，
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

运行资源使用记录（schema 2.9）：`workflow_runs` 新增 `duration_seconds`（墙钟秒数，
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
