# PyPI 发布

PyPI 分发名为 `OperonDBS`，因为 `operon` 分发名已属于另一个无关项目。这不会改变
导入包名或命令名：用户安装 `OperonDBS`，在 Python 中导入 `operon`，并执行
`operon` 命令。

PEP 517 构建依赖仅包含 setuptools、Cython 与 setuptools-scm。

## GitHub Actions 发布链路

`.github/workflows/publish.yml` 在推送 `v*` tag 时运行。它先验证 release
tag 严格等于 setuptools-scm 在该提交上推导出的版本（`v<version>`），再构建：

- 一份源码分发包；
- CPython 3.10-3.15 的 manylinux x86-64 wheel；
- CPython 3.10-3.15 的 macOS Intel wheel；
- CPython 3.10-3.15 的 macOS Apple Silicon wheel。

`verify-release` job 会先于其他所有 job 运行，且它们都依赖它：它检出 release tag，
   **等待**该提交的 `test` workflow 得出 `success` 结论——分支与其 tag 一起推送时，`test` 与
   `publish` 两个工作流同一时刻启动，因此闸门采用轮询（上限 60 分钟）而不是与 test 运行竞速——
   并要求 `scripts/release-preflight.sh --ci --tag <tag>` 通过。它会先导入维护者公开的 GPG 公钥，
   因此 tag 的签名是被**真正验证**过，而不只是"存在签名"；若该公钥不可用，这一项报 `skip`
   而不是 `fail`。因此，tag 落在 CI 为红（或压根
   被取消、没跑完）的提交上时，什么都不会被构建，更不会被上传。

每个 wheel 都会先安装到隔离测试环境，导入编译后的 parser 并调用 CLI；源码分发包
通过 Twine 检查。最终发布 job 必须等待全部构建成功，并通过 PyPI Trusted
Publishing 认证，不保存长期 API token。

在 PyPI 中配置 trusted publisher 时，owner 填 `HYLi360`，repository 填 `Operon`，
workflow 填 `publish.yml`，environment 填 `pypi`。GitHub environment 名称必须完全
一致；可以为该 environment 增加人工审批规则，在最终上传前保留一道确认。

## 发布步骤

1. 主程序版本不再需要手工改动：`pyproject.toml` 声明 `dynamic = ["version"]`，版本由
   setuptools-scm 从附注、GPG 签名的 `vX.Y.Z` tag 推导。如有变化，同时更新代码中的
   `SCHEMA_VERSION` / `METADATA_SCHEMA_VERSION`。文档版本标记通过 `docs/conf_common.py` 的
   `myst_substitutions` 从这些唯一来源渲染，无需手工批量替换；
   `tests/unit/test_docs_versions.py` 会拒绝 Markdown 源文件中硬编码的当前版本。因此准备
   发布不再改变任何提交——曾经的版本 bump 提交，以及它曾引发的证据重做，一并消失。
2. 运行 `scripts/release-preflight.sh` —— 一条命令、一个退出码，就是发布闸门：
   全量 pytest 套件、两棵树语言的严格文档构建、setuptools-scm 为本次检出推导的版本与已安装
   元数据的一致性、缺陷登记库（凡是 `fixed_in` 为本版本的记录都必须 `verified`、带
   `fix_commit`，且该提交包含在当前树中），以及"你即将打 tag 的那个提交"的跨版本矩阵
   证据（`--run-matrix` 会把矩阵跑在闸门里；`--tag vX.Y.Z` 还会断言 create 出来的 tag
   是附注、已 GPG 签名、名为 `v<version>` 且指向 `HEAD`）。发布不再依赖谁记得住这些
   单个命令。
3. 在发布提交上打 tag：`git tag -s -m "Operon vX.Y.Z" vX.Y.Z`。打 tag 不改变任何提交，因此
   该提交已录得的绿色 `test` run 与矩阵证据，以及读取同一检出的 preflight，在打 tag 之后
   仍然有效。不要复用仍指向旧软件包元数据的 tag。
4. 把分支与 tag 一起推送：`git push origin master vX.Y.Z`。tag 推送会启动 publish
   工作流，其闸门随即等待该提交的 `test` 运行得出结论（实测单次 23-38 分钟）。若该运行
   为红或被取消，什么都不会被构建与上传：修复提交，或用 `gh run rerun <run-id>` 重跑测试，
   然后删除并重新推送 tag 以再次触发发布。
5. 可选：从该 tag 创建 GitHub Release 来承载发布说明。发布它不会触发任何工作流——
   发布链路已在 tag 推送时运行。
6. 在 PyPI 的 `OperonDBS` 项目页核对文件与元数据。

PyPI 同一版本的文件不可覆盖。如果错误内容已经发布，应该发布新版本：修复内容，在新发布提交上
打 `vX.Y.(Z+1)` tag 再发布——绝不替换或移动已发布的版本。
