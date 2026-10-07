# 公开 GitHub 更新

本流程只发布 `scripts/public-release-scope.json` 允许的源码和文档，目标固定为 `Osir1603644274/ecommerce-shopping-agent`。它不提交、重置或清理 `F:\agent` 的工作树。输出只写到 `.runtime/public-release/`。

## 首次初始化

先确认 Python 3.12、GitHub CLI、Windows `curl.exe` 可用，并以拥有目标仓库写权限的账号执行 `gh auth login`。**当前电脑已完成初始化，日常使用直接运行下一节的 `plan`。** 以下命令只用于重新建立基线：

```powershell
cd F:\agent
& .\.venv\Scripts\python.exe scripts\public_release.py init `
  --snapshot .runtime\github-update-20260922-unified-routing\stage `
  --sha 47ecf5551de44d1531e5f92198e1612af614ec72 `
  --evidence-plan .runtime\github-update-20260922-unified-routing\publish-plan.json `
  --receipt .runtime\github-update-20260922-unified-routing\remote-verification-result.json
```

`init` 会复核快照清单和 GitHub `main` 提交。若快照或提交已不同，应先取得并核实当前远端快照，不能修改 SHA 以跳过检查。

## 每次更新

```powershell
cd F:\agent
& .\.venv\Scripts\python.exe scripts\public_release.py plan
```

查看输出的 `plan.json`，重点看 `changed`、`removed` 和源文件清单。默认有删除就停止；确有意删除时重新执行 `plan --allow-delete`，审查后再发布：

```powershell
& .\.venv\Scripts\python.exe scripts\public_release.py publish --plan .runtime\public-release\runs\<本次目录>\plan.json --message "feat: describe change"
```

发布会重查 `main`、快照哈希，上传变更 blob，创建基于当前提交的树与新提交，以非强制方式更新 `main`，再校验远端 blob 和 GitHub Actions。若网络在更新引用后中断，使用同一计划执行 `verify --plan <plan.json>`；不要盲目重复 `publish`。

## 维护

- 新增公开模块或文档时，修改 `scripts/public-release-scope.json`，并先检查 `plan.json` 的文件清单。运行产物和原始数据不应加入允许范围。
- 检索算法、客服和交易代码变化时，按影响面更新 `tests` 列表；这些测试在公开快照中运行。
- `main` 被其他人更新时，脚本会停止。先取得新的公开快照、逐文件哈希计划和远端校验回执，再执行 `init --replace --snapshot ... --sha ... --evidence-plan ... --receipt ...`；不能仅把本地状态中的 SHA 改成远端值。
- 每次成功验证后，新快照成为下次的基线。历史运行目录保留，清理前确认当前状态引用的目录及所需证据。
- 本地 `.env` 中的凭据值会在候选快照中替换成演示占位符；任何剩余密钥命中都会阻止发布。不要把凭据写入允许列表或提交日志。
