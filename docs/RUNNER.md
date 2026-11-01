# Windows 自托管竞彩运行器

当前运行器**已经注册**：Windows，目录 **`D:\actions-runner`**，标签 **`football-local`**。
当前采用**非管理员、用户登录时触发的计划任务**运行 `run.cmd`，不是 Windows 服务；无需重新注册。
仓库变量 `FOOTBALL_FEEDS_RUNNER` 现在为空，由审查方在合并后设置为单个标签 **`football-local`**。
工作流 `runs-on` 直接使用该字符串，不拆逗号，也不用 JSON。

`apis` 和 `verify` 使用 GitHub 托管 Ubuntu，无需本机。只有 `sporttery` 需要能访问中国竞彩网且获准采集的运行器。
电脑须开机、联网；当前计划任务方案还需要用户登录。变量为空时整个 sporttery job 显示 **skipped**。

## 软件与首次注册

1. 安装 Git for Windows，让运行器进程的 PATH 能找到 `git` 和 Git Bash 的 `bash`。
   运行器压缩包自带 Node；工作流的 `actions/setup-python@v5` 安装 **Python 3.12**，
   不假设电脑已安装 Python。运行账号须能写运行器目录、Python 工具缓存，并访问 GitHub、PyPI 和体彩域名。
   sporttery 所有命令步骤明确使用 `shell: bash`。
2. 只有新电脑或已移除的运行器才需注册。在仓库 **Settings → Actions → Runners → New self-hosted runner**
   选择 Windows / x64，按页面给出的最新下载命令和校验说明下载、解压，再进入运行器目录。
3. 从同一页面取得短期注册令牌。非管理员方式在普通 PowerShell 中执行：

   ```powershell
   Set-Location D:\actions-runner
   $runnerRegistrationToken = Read-Host 'GitHub 页面中的临时注册令牌'
   .\config.cmd --unattended --url https://github.com/qw5563680/FOOTBALL --token "$runnerRegistrationToken" --labels football-local --name "$env:COMPUTERNAME-football"
   Remove-Variable runnerRegistrationToken
   ```

   注册令牌是 GitHub 的短期运行器令牌，不是两个数据 API 的 key。以下启动方式二选一，
   不要让同一运行器同时以服务和计划任务启动。

## 方式 A：非管理员登录计划任务（当前采用）

在 Windows **任务计划程序 → 创建任务**中检查已有任务，避免重复创建：

- 常规：使用当前 Windows 账号，选择“只在用户登录时运行”，不勾选“使用最高权限运行”。
- 触发器：“登录时”，选择该用户。可按本机情况设置短暂延迟，等待网络就绪。
- 操作：“启动程序”；程序填 `C:\Windows\System32\cmd.exe`，
  参数填 `/c D:\actions-runner\run.cmd`，**起始于**填 `D:\actions-runner`。
- 设置：允许手动运行；取消“运行超过指定时间则停止”，避免运行器被定时终止；
  “如果任务已在运行”选择“不启动新实例”。按电脑的供电策略检查条件是否会阻止启动。

保存后可右键任务 → 运行，在 GitHub Runners 页确认显示 **Idle**。登录时任务会重新启动监听；
注销、关机、睡眠或网络中断可能使运行器离线。当前机器使用这一路径，不应查找或操作 Windows Runner 服务。

## 方式 B：管理员 Windows 服务

新安装时使用管理员 PowerShell 执行注册命令并加 `--runasservice`：

```powershell
Set-Location D:\actions-runner
$runnerRegistrationToken = Read-Host 'GitHub 页面中的临时注册令牌'
.\config.cmd --unattended --url https://github.com/qw5563680/FOOTBALL --token "$runnerRegistrationToken" --labels football-local --name "$env:COMPUTERNAME-football" --runasservice
Remove-Variable runnerRegistrationToken
```

按运行器提示配置服务账号，在 `services.msc` 确认 GitHub Actions Runner 服务运行并自动启动。
服务账号须具备上述 PATH、目录和网络权限；服务方式不依赖交互登录。
若从现有计划任务迁移，先停用任务并结束监听，再按 GitHub 提示移除/重新配置运行器，避免并行实例。

## 合并后启用与检查

由审查方在 **Settings → Secrets and variables → Actions → Variables** 设置
`FOOTBALL_FEEDS_RUNNER=football-local`。本 PR 不修改仓库变量。
在 **Actions → Football cloud feeds → Run workflow** 选择代码分支，
`operation=sync`、`days=3`、`slot=auto`：apis 跳过，sporttery 安装 Python 后恢复、采集、导出、rebase 和写回。
未设置变量时 sporttery 也跳过。HTTP 567 表示站点访问策略拒绝，应先核查本机网络是否获准访问。

两个 API key 只放仓库 Secrets：`FOOTBALL_DATA_ORG_TOKEN` 与 `API_FOOTBALL_KEY`。
竞彩 job 不需要它们，不要写入运行器注册脚本、系统变量、仓库文件或日志。

## 暂停、停用和移除

1. 先删除或清空仓库变量 `FOOTBALL_FEEDS_RUNNER`。下一次 sporttery job 将跳过，hosted API 采集继续。
   已开始或已排队的 job 不会因变量变化自动停止；在 Actions 等待完成或明确取消后，再停止监听。
2. **当前计划任务方式**：在任务计划程序中选择该任务 → 禁用，防止下次登录重启；
   在运行器控制台按 Ctrl+C 正常停止监听。无人值守时可结束该任务，并确认其 `Runner.Listener` 子进程已退出。
   永久停用可在确认后删除该计划任务。
3. **服务方式**：管理员 PowerShell 用 `Get-Service 'actions.runner.*'` 查找准确服务名，
   再执行 `Stop-Service -Name '<准确的服务名>'`；长期停用可执行
   `Set-Service -Name '<准确的服务名>' -StartupType Disabled`，也可通过 `services.msc` 操作。
4. 永久移除时在 GitHub Runners 页选中运行器，按 **Remove** 页获取短期移除令牌，在运行器目录执行：

   ```powershell
   $runnerRemovalToken = Read-Host 'GitHub 页面中的临时移除令牌'
   .\config.cmd remove --token "$runnerRemovalToken"
   Remove-Variable runnerRemovalToken
   ```

   确认计划任务/服务及 GitHub 记录均已移除，再按需要删除目录。恢复使用时先启动相应监听方式，
   确认 GitHub 显示 Idle，再由维护者恢复仓库变量。

两条任务通过工作流共用 concurrency 串行写入 `data/football-feeds`。写回 rebase 冲突时不强推；
原始响应和审计状态保留为 90 天 Actions artifact，先核查远端变更再重跑。
