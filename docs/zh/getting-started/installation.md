# 安装

## 环境准备

安装前确认系统具有 Python 3.10 或更高版本及可用的 C 编译工具链。

需要：

- Python 3.10 或更高版本
- Python 自带的 `venv` 与 `pip`
- 可用的 C 编译工具链；`operon` 默认构建并使用 Cython 内置 QC 扩展
- 可选：BUSCO、QUAST、FastQC、fastp 等外部工具（不在本指南中安装）

## 平台支持

Linux 是独立应用发布、本地 Slurm 及完整外部生物信息学工具生态的主要平台。
macOS 支持从源码安装后的本地执行，也支持作为 SSH/SFTP 客户端向 Linux 主机提交
远端 Slurm 作业。本地资源采样在可用时读取 procfs，在 macOS 上则使用系统 `ps`
命令。

`tools.yaml` 配置的外部命令仍须在控制端平台上自行安装。SSH 直连执行要求远端主机
提供 util-linux `setsid`，因此该计算端模式仍以 Linux 为目标；这不影响 Mac 作为
控制端使用。目前不发布签名或公证过的 macOS 独立应用包。

## 从 PyPI 安装

PyPI 分发名为 `OperonDBS`，导入包名与命令名仍为 `operon`：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install OperonDBS

# 按需安装 SSH/SFTP 与终端界面功能
python -m pip install 'OperonDBS[remote,tui]'
```

发布的 wheel 已包含编译后的 QC parser。PyPI 安装不使用 cx_Freeze；cx_Freeze 只用于
另一条独立应用目录发布链路。

## 从仓库安装

在仓库根目录安装 `operon`：

```bash
# 在仓库根目录
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

# 需要 SSH/SFTP 远程存储或计算时
python -m pip install -e '.[remote]'
```

验证安装：

```bash
operon --version
# 输出：operon 0.6.2

operon --help
```

如需构建独立 cx_Freeze 应用，请安装 `build` extra 并使用统一发布入口：

```bash
python -m pip install -e '.[build]'
python tools/build.py
```

目录结构与校验规则见[应用程序发布](../contributor/application-release.md)。
