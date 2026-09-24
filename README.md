# 终界网络 EnderNet

《终界网络》是一款《我的世界·中国版》PE 端的存储物流自动化向模组。

## 将 dev 合并到 master

使用 `git merge-dev` 合并，`plans/` 保留为合并前 `master` 上的完整内容，忽略来自 `dev` 的新增、修改和删除。首次安装或更新命令时，在包含脚本的分支上，从仓库根目录运行 PowerShell（需要 Python）：

```powershell
@'
import pathlib, subprocess
script = pathlib.Path('scripts/merge-dev.sh').read_text()
subprocess.run(['git', 'config', '--local', 'alias.merge-dev', '!' + script], check=True)
'@ | python -
```

安装后的命令保存在本地 Git 配置中，切换分支后仍可使用；新克隆的仓库需要重新安装。先提交所有改动，或用 `git stash push -u` 收起改动（包括未跟踪文件），确保工作区和暂存区干净，再运行：

```powershell
git switch master
git merge-dev
```

命令会创建合并提交；其他路径如有冲突，解决后运行 `git commit`，或用 `git merge --abort` 取消。普通 `git merge dev`、IDE 合并和远端 PR 合并不会应用此规则。合并后 `dev` 的提交仍存在于历史中，仅 `master` 的文件树排除其 `plans/` 改动。
