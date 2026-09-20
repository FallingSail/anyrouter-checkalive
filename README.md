# Anyrouter Codex Keepalive

基于 LeafCreeper/Anyrouter-Checkalive 的本地改造，使用真正的 Codex CLI，
不是用 curl 模拟客户端。保留多 token、GitHub Actions、单次检查、定时轮询和 QQ 邮件。

## 必须先确认

中转站必须支持 Codex 使用的 Responses API；仅支持 Claude Messages 或 Chat
Completions 不够。BASE_URL 和 MODEL 必须由你按站点文档填写，没有默认生产端点。
通常 BASE_URL 包含 /v1，但不要添加 /responses；以站点 Codex 配置说明为准。
这只能验证请求可用性，无法证明请求会提高账号队列优先级。
每次探测都会产生真实 API 用量，Codex 自带提示上下文，不能保证成本极低。
请遵守站点条款与限流要求。

## GitHub Actions

1. 将当前修改后的代码提交到你自己的仓库（这里只修改了本地文件）。
2. Settings → Secrets and variables → Actions → Secrets：
   - ANYROUTER_TOKENS：每行一个中转站 token。
   - QQ_EMAIL、QQ_SMTP_AUTH_CODE：可选；后者是 SMTP 授权码。
3. Variables：
   - BASE_URL：你信任的中转站 Responses API 基地址。
   - MODEL：该站支持的 Codex 模型标识。
   - CODEX_VERSION：可选，建议验证后固定 CLI 版本；未设置时安装 latest。
4. 先手动运行 Codex Keepalive Once，确认正常再启用定时任务。

Keepalive 每天北京时间 02:00 启动，运行约六小时，间隔约 50 分钟，
不是全天覆盖。Recovery Monitor 每 30 分钟检查，全部成功且单次耗时
均小于 30 秒时退出。Actions 定时触发可能延迟，不保证精确时刻。
三个工作流共享并发组，避免同时消耗同一批账号；GitHub 只保留有限的待运行任务。

## 本地使用

批量脚本需要 Linux/WSL、Bash 4+、Python 3.11+、Node.js 和 Codex CLI。
Windows 建议在 WSL 内安装并运行所有依赖，不混用 Windows Python 和 WSL Codex。

Git Bash 中 Python 常叫 `python`，而不是 `python3`。探针启动脚本会依次检测
`python3`、`python`，要求 Python 3.11+；也可以显式设置 `export PYTHON_BIN=python`。
这只解决解释器选择，Windows 原生 Codex 启动和沙箱仍需实际联调；WSL 是推荐运行环境。
README 中直接调用 `python3` 的命令，在 Git Bash 中可改为 `python`。

```bash
npm install -g @openai/codex
cp .env.example .env
# 编辑 .env，填写自己的端点、模型和 token。
# 只 source 自己可信的文件；脚本不会自动执行 .env。
set -a
source .env
set +a
bash scripts/run-all.sh --once
bash scripts/run-all.sh
bash scripts/monitor-recovery.sh
```

多账号在 .env 的 ANYROUTER_TOKENS 引号内逐行填写。
单账号也可以设置 KEEPALIVE_TOKEN、BASE_URL、MODEL 后直接运行：
`python3 scripts/codex_probe.py`。避免把密钥写进命令行参数或提交到 Git。

可配置 PROBE_TIMEOUT_SEC（1–600，默认 120）、MAX_DURATION_SEC（默认 21500）、
SLEEP_BETWEEN_TOKENS（默认 30，带抖动）、SLEEP_BETWEEN_ROUNDS（默认 3000）、
POLL_INTERVAL（默认 1800，仅恢复监控）。

## 从第二个项目借鉴的部分

借鉴 anyrouter-keepr 的设计思路，没有复制其 Rust 实现：
- 区分成功、临时故障、超时、配置错误和未知失败。
- 配置错误账号在本次运行中暂停，避免反复发送无效认证请求。
- 日志脱敏、截断；账号只显示序号，不展示 token 片段。
- 临时独立 CODEX_HOME、临时工作目录、只读沙箱、禁用 shell 和网页搜索，
  不覆盖日常 Codex 配置；使用 --ephemeral，不保留探测会话。

不移植 Tauri/React 界面、SQLite 历史或本地代理，保持 Actions 版轻量。
也不照搬其 60–120 秒高频循环及“429 就是排队失败”的推断：
429 可能是真实限流，503 可能是上游故障，不能据此断定排队机制。
本版不立即重试，临时故障等待下一轮；应根据站点要求进一步拉长间隔。
成功必须同时满足零退出码、turn.completed 和非空最终回复。
每次探测从 `scripts/prompts.txt` 随机选取一个简短编程问题；允许重复抽到同一题。
可自行编辑提示词池：UTF-8 编码，每行一个完整问题，忽略空行和以 `#` 开头的注释。
问题应独立可回答，不依赖本地文件、联网或执行命令。文件缺失或没有有效题目时，
回退到 Python `==` 与 `is` 的区别。统一要求最多三句话或八行代码，不使用工具；
这是提示词约束，不是严格的 token 上限。更换问题不保证提高优先级或绕过站点限制。

## 测试与限制

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
bats tests/
```

离线测试不调用真实模型，不能替代中转站联调。
`tests/` 中的测试都使用模拟数据/进程，不需要真实 API key。只想检查真实账号，
加载 `.env` 后运行 `bash scripts/run-all.sh --once` 即可，不必先运行 `tests/`。
批量任务只要发生失败便返回非零；恢复监控仅达到恢复条件返回零。
邮件发送失败不会中断探测。临时目录清理由正常退出和异常处理负责；
宿主强制终止仍可能遗留临时文件。
Windows 文件占用也可能使临时目录清理失败：脚本会有限重试，仍失败时输出
WARNING 和遗留路径，不覆盖真实探测状态。目录内可能有未脱敏的原始日志，
请勿上传；文件占用解除后，只删除警告中标明的本次临时目录。
不要按进程名批量结束所有 Python/Codex 进程，以免影响其他程序。

## 来源与许可

- https://github.com/LeafCreeper/Anyrouter-Checkalive
- https://github.com/919101797/anyrouter-keepr

检查到的第一个仓库没有 LICENSE；公开可见不等于授予再分发许可，
公开发布这个衍生版本前请向原作者确认授权。第二个仓库采用 MIT 许可，
若以后复制其代码，需保留对应版权及许可文本。

官方参考：
- https://developers.openai.com/codex/noninteractive/
- https://developers.openai.com/codex/config-reference/
