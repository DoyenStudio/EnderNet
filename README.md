# 终界网络 EnderNet

《终界网络》是一款《我的世界·中国版》PE 端的存储物流自动化向模组。

## 前置模组与流体联动

开发环境的 `.mcdev.json` 已包含 `D:\addon\transferlib`；发布时请一并安装该项目的行为包与资源包（radium 1.1.0 或兼容的 1.x 版本）。EnderNet 在业务调用时通过 `GetComponent(levelId, "radium", "transfer")` 获取公共服务，不导入其他模组的内部脚本，也不声明强制 manifest 依赖。缺失、尚未初始化或版本不兼容时，流体通路拒绝搬运，后续会重新获取组件。

主动流体存货口、取货口以及样板接口的流体投料通过公共端口操作注册该能力的机器，例如当前 SkyBlueTech。查询使用目标机器的接触面，遵守其输入、输出槽与过滤规则。EnderNet 只提交整数 mB 的事务；机器保有的小数余量不会被截断转入网络。被动流体接口的本地储罐和流体 UI 缓冲仍待实现，本次联动不改变这些未定规则。离线测试不能替代游戏中的机器加载、侧面配置和存档验证。

能源接入口六面均通过 transferlib（radium）的 `radium:energy` 接收 FE。每 2 FE 可供网络消耗 1 EN。各口独立保存余额，剩余的 1 FE 可与下次输入合并。创造能量源继续独立提供 EN。

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
