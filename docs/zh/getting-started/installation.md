# 安装

## 环境准备

安装前确认系统具有 Python 3.10 或更高版本及可用的 C 编译工具链。

需要：

- Python 3.10 或更高版本
- Python 自带的 `venv` 与 `pip`
- 可用的 C 编译工具链；`operon` 默认构建并使用 Cython 内置 QC 扩展
- 可选：BUSCO、QUAST、FastQC、fastp 等外部工具（不在本指南中安装）

安装 `operon`：

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
# 输出：operon 0.6.1

operon --help
```
