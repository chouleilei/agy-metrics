# agy-metrics

面向 **Google Antigravity CLI (`agy`)** 的轻量、纯本地终端状态栏与历史统计插件（Linux / macOS，Python 3 零第三方依赖，不依赖 `jq`）。

* **不修改官方客户端 / `app.asar`**
* **不读取/不转发 OAuth Token，不搭建代理或保活定时任务**
* **100% 基于 `agy` 官方原生机制**：`plugin hooks` + `statusLine` + 只读解析本地 `transcript.jsonl` / `conversation_summaries.db`

---

## 核心功能

### 1. 终端底部实时状态栏 (`statusLine`)

```text
Ctx 44.0% │ 会话(24轮105步) In:14.24M Out:136.0k │ 缓存 87.8% │ 速度 47.6(8.5s)/平均96.6 tok/s │ 周余 71.8% │ ~$4.56
```

* **真实上下文占比 (`Ctx`)**：支持按实际窗口上限（默认 **512K / `524,288` tokens**）自动折算官方上报比例；低于 512K 的模型保持原窗口计算；恢复历史会话（`agy -c`）首屏自动回退读取日志水位。
* **会话计数与 Token**：实时统计 **`(X轮Y步)`**（用户提问轮次 + 大模型调用步数）、累计 `In`（`新Input + Cache读`）与 `Out`。
* **缓存命中率**：计算 `Cache读 / (新Input + Cache读)` 的会话累计命中率与当轮命中率。
* **端到端生成速度**：通过生命周期 Hook 与状态栏协同记录上轮耗时与 `tok/s`，以及剔除对话空闲等待后的**会话平均有效生成速率**。
* **Gemini 周额度剩余 (`周余`)**：后台单实例异步调用官方 `agy --print /usage --output-format json`（默认 180 秒本地缓存、不阻塞状态栏渲染、不消耗 LLM Token）。
* **等效 API 费用**：优先读取官方下发 `cost`，否则按步骤模型单价实时估算（支持人民币换算与自定义单价表）。

### 2. 历史与工作区统计命令行 (`agy-stats`)

* 汇总 **今日 / 近 7 天 / 近 30 天 / 全部历史** 的会话数、轮/步数、新Input、Cache读、总Input、Output、缓存命中率与等效费用。
* **工作区维度分析**：关联 `conversation_summaries.db` 展示会话标题与所属工作区目录，支持按当前目录或指定工作区过滤。
* **按模型汇总（含平均 TPS）**：统计各模型消耗占比、缓存命中率、等效费用及累计计时 **平均 TPS**。
* **单次会话 Step 级明细**：逐 step 展示时间、模型、Token、缓存命中率与会话总计。

---

## 快速安装

```bash
git clone https://github.com/chouleilei/agy-metrics.git
cd agy-metrics
./install.sh
```

重启 `agy` 终端会话即可在底部看到状态栏（会话内可用 `/statusline` 随时开关）。

### 卸载

```bash
./uninstall.sh
```

---

## `agy-stats` 常用命令

```bash
agy-stats                      # 全部历史总览 + 工作区分布 + 模型分布(含平均TPS) + 最近会话
agy-stats -w                   # 只统计当前工作区目录的历史会话
agy-stats -w /path/to/repo     # 只统计指定工作区
agy-stats -w /path/to/dir -r   # 递归包含子目录工作区
agy-stats --days 7             # 附加最近 7 天的每日明细
agy-stats --current            # 查看最近一次活跃会话的 Step 级明细
agy-stats --session <ID前缀>   # 查看指定会话明细
agy-stats --json               # 输出结构化 JSON
```

---

## 环境变量与自定义配置

| 变量 / 文件 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `AGY_MAX_CTX_TOKENS` | `524288` (512K) | 上下文百分比折算上限 |
| `AGY_QUOTA_TTL_SEC` | `180` | `周余` 配额本地缓存有效时间（秒），设大可减少 `/usage` 刷新频率 |
| `AGY_NO_QUOTA_REFRESH` | `0` | 设为 `1` 可完全禁止后台刷新配额（纯离线模式） |
| `AGY_USD_CNY` | 空 | 设置汇率（如 `7.2`）后状态栏将显示 `~¥` 折算金额 |
| `AGY_DATA_DIR` | 自动探测 | 显式指定 `antigravity-cli` 数据目录 |
| `~/.gemini/antigravity-cli/pricing.json` | 无 | 自定义/覆盖模型单价（USD / 1M tokens）：`{"model-prefix": {"input": 1.5, "cache_read": 0.075, "output": 7.5}}` |

---

## 项目结构

```text
agy-metrics/
├── plugin.json                # agy 原生插件清单
├── hooks.json                 # SessionStart / PreInvocation / PostInvocation / Stop Hooks
├── install.sh                 # 一键安装脚本
├── uninstall.sh               # 一键卸载脚本
├── scripts/
│   ├── agy_metrics_lib.py     # 增量日志解析、单价表、数据库元数据读取
│   ├── record_timing.py       # Hook 耗时与分模型速度记录
│   ├── statusline.py          # 终端 statusLine 渲染脚本
│   ├── refresh_quota.py       # 非阻塞 Gemini 周额度缓存刷新
│   └── agy-stats              # 终端历史/工作区统计 CLI
└── tests/
    └── test_metrics.py        # 单元测试与回归测试
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```
