# 用户配置

`operon` 把用户级设置放在项目之外的一个可选 YAML 文件里。它不会替代项目配置：
`project.yaml` 依旧掌握存储布局、数据库路径、资源与执行后端，用户文件只掌握
少数属于*你*而不是属于某个项目的设置。

```{note}
文件是可选的。没有该文件时，内置默认值生效，用户身份仍按原有方式回落到环境变量。
```

## 位置

用户配置遵循 XDG Base Directory 规范：

```text
$XDG_CONFIG_HOME/operon/config.yml      # 通常是 ~/.config/operon/config.yml
```

- `XDG_CONFIG_HOME` 未设置或为空 → `~/.config/operon/config.yml`。
- 不提供 `OPERON_CONFIG_HOME`：路径完全由 XDG 决定。
- `operon` 绝不在家目录创建 `~/.operon`，也不存在按项目的用户文件。
- 文件以 `0600` 权限写入，其目录为 `0700`。
- `operon config` 与项目无关：它不会打开 `operon.sqlite`，在 `operon init`
  之前、在任意项目目录之外都能使用。

## 优先级

命令行参数高于环境变量，环境变量高于文件，文件高于内置默认值。

| 设置 | 参数 | 环境变量 | 用户文件 | 兜底 |
| --- | --- | --- | --- | --- |
| 审计 actor | `--actor` | `OPERON_ACTOR`、`USER`、`LOGNAME`、`USERNAME` | `identity.actor` | 本机账户（`getpass`），再退化为无 actor |
| NCBI 联系地址 | `--email` | `NCBI_EMAIL` | `ncbi.email` | 未设置 |
| NCBI API 密钥 | `--api-key` | `NCBI_API_KEY` | 绝不写入文件 | 已存储的机密（见下文） |
| 终端图形 | — | `OPERON_SPLASH` | `ui.splash` | `auto`（自动探测） |

有两点需要明确：

- 本机账户位于环境变量与文件之间：正常登录时 actor 仍是 `$USER`，
  `identity.actor` 只在容器与 cron 等既无登录变量、又无密码条目的场景补空缺。
- 需要 actor 的操作（`retire --apply`、`restore --apply`，以及 TUI 的生命周期与
  curate 对话框）在拿不到身份时会明确报错，而不会写入无名的审计记录。

## 配置键

| 键 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `schema_version` | 整数 | `1` | 文档版本；由 `operon config init` 写入 |
| `identity.actor` | 字符串 | `""` | 兜底审计 actor |
| `ncbi.email` | 字符串 | `""` | NCBI API 请求使用的联系地址 |
| `ui.splash` | 枚举 | `auto` | `auto`、`text`、`blocks` 或 `kitty` |

无法识别的键会保留在文件中但被本版本忽略，`operon config check` 会报告它们。
看起来像机密材料的键（`…api_key`、`token`、`secret`、`password`、`credential`）
会被 `operon config set` 拒绝：凭据属于机密后端。

## 命令

```bash
operon config path
operon config show [--effective] [--json]
operon config get KEY
operon config set KEY VALUE
operon config unset KEY
operon config check
operon config init [--force]
operon config secret list
operon config secret set NAME
operon config secret get NAME
operon config secret clear NAME
```

- `path`：打印解析后的文件路径。
- `show`：打印已存储的文档（文件不存在时为默认值），并且绝不会创建该文件。
  加 `--effective` 时打印每个设置最终生效的值及其来源；`--json` 切换输出格式。
- `get`：打印单个值并退出 `0`；该键没有值时什么也不打印并退出 `1`，
  与 `git config` 一致。
- `set` / `unset`：写入前先校验键与值，然后以仅本人可读的权限原子写入。
  `unset` 恢复内置默认值。
- `check`：校验 YAML，对同组/其他用户可读的文件以及无法识别的键给出警告，
  对机密材料报错，并始终打印最终生效的设置——查看哪一层生效最快的方式。
- `init`：写入默认文档；不加 `--force` 时拒绝覆盖。

## 机密

凭据绝不写入用户文件，也不必出现在环境变量中。`operon` 按顺序使用第一个
可用的系统后端：

1. `secret-tool` —— Secret Service（GNOME Keyring、KWallet；`libsecret-tools`）。
2. `systemd-creds --user` —— 加密凭据存放在
   `$XDG_CONFIG_HOME/operon/secrets/`。
3. `/usr/bin/security` —— macOS 钥匙串。

三者都通过 `subprocess` 调用，不需要新增运行时依赖。若都不可用，`operon`
会明确报错，并提示改用 `--api-key` 或设置 `NCBI_API_KEY`。密钥按
`--api-key` > `NCBI_API_KEY` > 已存储值 的顺序解析。

```bash
# 不让值出现在 argv 或 shell 历史中
printf '%s' "$NCBI_TOKEN" | operon config secret set ncbi.api_key
operon config secret list
operon config secret clear ncbi.api_key
```

`secret set` 从标准输入读取值；在终端下则使用隐藏输入提示，绝不接受命令行参数
形式的值。`secret list` 只显示后端可用性与各机密是否已设置，永不显示值。
`secret get` 为脚本打印值，并在标准输出为终端时给出警告。

```{note}
`systemd-creds --user` 会把凭据绑定到用户、机器 ID 与内核 boot ID，因此在共享
家目录的 HPC 环境中可能无法在另一节点解密：远程执行时请优先使用环境变量
`NCBI_API_KEY` 或命令行 `--api-key`。
```

## 示例

```bash
# 容器与 cron 的兜底身份
operon config set identity.actor alice

# NCBI 联系地址，随后把 API 密钥写入机密后端
operon config set ncbi.email you@example.org
printf '%s' "$NCBI_TOKEN" | operon config secret set ncbi.api_key

# 为该用户跳过图形探测，无需环境变量
operon config set ui.splash text

# 当前生效的值及其原因
operon config show --effective
```

## 环境变量不会废弃

所有环境变量继续可用，并且继续优先于文件。`operon` 直接读取的变量是被审计的：
在 `tests/unit/test_env_audit.py` 中，任何未加注记的 `os.environ` 读取都会让该
测试失败，因此新增变量必须先经过评审与记录才能发布。用于 `environment_id` 的
环境捕获（`PROBE_ENV_VARS`）以及 `requests`/`paramiko` 读取的 HTTP/SSH 代理变量
不受影响。
