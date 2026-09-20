# 一次启动，持续定时请求

## 首次配置

将修改提交到 GitHub 默认分支。使用存储库级配置，不需要 Environment。
Settings → Secrets and variables → Actions → Secrets：

- `SCHEDULER_TOKEN`：你创建的 fine-grained GitHub PAT，仅授权当前仓库，
  Repository permissions → Variables → Read and write。无需 Actions 写权限。
  若组织要求审批，先完成审批。令牌过期或撤销后调度会失败。
- `ANYROUTER_TOKENS`：中转站 API key，每行一个；这些 key 共用同一个站点和模型。
- `QQ_EMAIL`、`QQ_SMTP_AUTH_CODE`：可选；不配置就不发邮件。

Variables：`BASE_URL`、`MODEL`，以及可选的 `CODEX_VERSION`。
可选 `PROBE_TIMEOUT_SEC`（默认120秒）、`SLEEP_BETWEEN_TOKENS`（默认30秒）。
脚本自动创建存储库 Variable `CODEX_SCHEDULE_STATE` 保存调度状态，不要预先填写，
也不要存入 API key；此变量只含启停标记、间隔和下次到期时间。

## 每天使用

只选择一个入口，在默认分支点击 Run workflow：

1. **Codex Start Now**：填写 interval（默认15分钟），启动后立即进行一次探测，
   随后持续调度。立即是指工作流获得运行器并安装依赖之后，并非零延迟。
2. **Codex Start At Beijing Time**：填写 start_time（默认04:00，北京时间）、
   interval。先保存等待状态，到点后首次探测，之后跨天持续运行，不会每天重新等待04:00。
   如果指定时间已经过去（或恰好到达），安排到明天。

重新运行任一启动入口会替换之前的计划，不会创建第二份计划。
修改间隔同样重新启动即可；Start Now 会额外立即探测一次，Start At 会重新安排首次时间。

**Codex Stop Schedule**：点击一次停止后续模型请求。它与正在执行的探测共用并发组，
可能需要等待当前运行完成。需要立即中断时先 Cancel 当前探测，再执行停止入口。
后台每5分钟的检查仍会出现，但显示 stopped，不会安装 Codex 或调用模型。
若连这些空检查也不要，Disable `Codex Schedule Worker`；下次使用时先重新启用它。

## 间隔、状态与历史

后台 `Codex Schedule Worker` 每5分钟检查一次仓库状态，未到期就退出。
interval 允许5–1440分钟，建议用5的倍数，例如10、15、30。
GitHub cron可能延迟或漏过，不能保证精确的15分钟，更不是持续保持上游排队连接。
到期判断时先把下次时间设置为“当前时间＋间隔”，然后安装依赖并请求。
不会补发错过的历史轮次；探测耗时较长时也不会并发追赶。
安装失败、任务取消或模型失败也不会回滚下次时间，避免立即重试造成重复消耗。

每轮调用 `run-all.sh --once`，每个key各自从提示词池随机抽题。
临时拥挤或认证失败会使本轮标红，但不会关闭长期计划；下一到期轮仍会重新检查。
持续认证失败请主动停止并修正配置。
每轮探测预算20分钟，整个工作流30分钟；大量账号应拆分，避免部分账号未获检查。

实际探测结果在 worker 的运行历史及 Summary 中查看；立即启动的首次结果在
Start Now 对应运行中查看。绿色的 waiting/stopped 仅表示调度检查正常，不表示模型成功。
配置QQ邮箱后，每轮实际探测都会发送汇总（成功或失败均发送）；空检查不发邮件。
GitHub历史有保留期限，云端运行计入适用的额度；全天检查约288次，
15分钟探测理论上约96轮。令牌有效期、额度和GitHub的定时任务停用政策都可能中断调度。

## 并发与旧任务

所有探测及调度操作共享 `codex-keepalive` 并发组，避免状态读改写重叠。
GitHub只保留有限的待运行任务，连续快速点击多个入口可能替换待运行任务；
请等待入口执行完毕，并在 Summary 或仓库 Variable 中确认状态。
停止入口也可能受队列替换影响；紧急停止可禁用 worker 并取消正在运行/等待的任务，
之后确认停止状态已保存，再恢复 worker。

原 Keepalive 的每日自动触发已移除，三个旧入口仍可手动使用。
不要在新计划运行时启动旧的长时间 Keepalive/Recovery Monitor，否则会阻塞新调度。
启动状态保存在GitHub，不影响你本地测试。
当前方案仅支持每仓库一个站点/模型；多个站点请使用独立仓库或另行扩展隔离状态和并发组。

## 停止权限或网络故障

调度器读取或写入状态失败时直接报错，不会发送模型请求。
查看 Check or update schedule 日志；401/403优先检查PAT有效期、仓库授权和Variables权限。
不要上传令牌、.env或原始未脱敏日志。
