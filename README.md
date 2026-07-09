# Award Travel Copilot

定位：**把信用卡点数真的花成一张好票**。不做通用个人金融 app，不做 credit 追踪主战场（MaxRewards/CardPointers 已占），不和 seats.aero 拼搜索——只做它们中间没人做深的执行层：这张票对**我**能不能订、从哪转、转多少、风险是什么。

两个入口：

- `award_watch.py`：盯票守护进程（数据来自 seats.aero），新放位邮件提醒，提醒里带针对你余额的转点建议
- `points_os.py`：个人档案 CLI——`status` 看点数余额和各卡 credit 使用/过期状态；`advise` 把当前监控到的所有放位按"你真正可执行"过滤排序；`use`/`clear` 手动记录 credit 使用（辅助功能，不连银行）

个人数据都在本地：`profile.json`（点数、卡片、偏好，gitignored）+ `credits_catalog.json`（权益目录模板，自行核对修改）。

## 盯票（award_watch）

盯美国主要枢纽 <-> 日本主要机场（东京 NRT/HND、大阪 KIX 等）的商务舱里程票放位，发现新放位后写入 SQLite、更新本地 HTML 仪表盘，并发邮件提醒。

## 数据源：seats.aero

里程放位数据来自 [seats.aero](https://seats.aero) 的 Partner API（`https://seats.aero/partnerapi/search`）。它按航司常旅客计划持续查询官方奖励日历并缓存结果，比自己写爬虫抓 ANA/JAL/United 官网稳定得多（官网反爬虫较强，且自建爬虫可能违反航司使用条款）。

使用前需要：

1. 在 [seats.aero](https://seats.aero/register) 注册账号，订阅支持 Partner API 的套餐，拿到 API key（Pro API 的额度和价格见 seats.aero 官网当前定价，可能随时间调整，请以官网为准）。
2. 把 key 设置为环境变量：

```bash
export SEATS_AERO_API_KEY="your_key"
```

## 快速开始

还没有 API key？可以先用演示模式，用生成的模拟放位数据把整条链路（查询 → 去重 → 入库 → 仪表盘 → 邮件）跑通：

```bash
python3 award_watch.py --demo --once
```

演示模式写入独立的 `award_watch_demo.sqlite3` 和 `dashboard_demo.html`，不会污染真实数据；邮件主题带 `[DEMO]` 前缀。多跑几次可以观察"新放位提醒"和"放位消失"的行为。

有 key 之后正常运行：

```bash
python3 award_watch.py --once
```

运行一次后会生成：

- `award_watch.sqlite3`：放位记录和去重状态
- `dashboard.html`：当前仍然可订的放位列表

持续监控：

```bash
python3 award_watch.py
```

默认轮询间隔 1800 秒（30 分钟），可以在 `config.example.json` 里改 `poll_interval_seconds`。里程放位不像股价那样分秒必争，没必要调得比这更频繁，也能省 API 配额。

如果已经把 Gmail App Password 存进 Keychain（跟 `market-monitor-agent` 用的是同一个 Gmail 账号/同一份 Keychain 记录），可以直接双击：

```text
run_award_watch.command
send_test_email.command
```

如果还没配置过，先去 `market-monitor-agent/setup_gmail_password.command` 走一遍（两个工具复用同一个 SMTP 配置，不用重复设置）。

## 监控范围配置

复制配置文件后按需修改：

```bash
cp config.example.json config.local.json
python3 award_watch.py --config config.local.json
```

`seats_aero` 下可以调整：

- `origins` / `destinations`：机场三字码列表，默认是美国主要枢纽 <-> 日本主要机场
- `carriers`：只看这些承运人放出的位置，默认 `NH`（全日空）、`JL`（日航）、`UA`（联合）、`AA`（美联航）
- `cabin`：`economy` / `premium` / `business` / `first`，默认 `business`
- `search_window_days`：往后查多少天，默认 60。查询窗口越大，消耗的 API 额度越多，建议按实际计划出行的日期范围调整，而不是一直查一整年
- `min_remaining_seats`：低于这个余位数不提醒。注意：American 等计划不公布余位数量（API 返回 0 表示"未知"），这类放位不会被过滤，显示为"未知"

顶层的 `trip` 用来描述一次具体出行（旅行需求）：填了 `start_date` / `end_date`（`YYYY-MM-DD`）就只查这个区间，留空则用滚动的 `search_window_days` 窗口。

## 个人档案（profile.json）和转点建议

把你各体系的点数余额告诉程序，提醒邮件和仪表盘就会对每条放位直接回答：**你的点数够不够、应该从哪家转、要转多少、多久到账**。

```bash
cp profile.example.json profile.json
# 编辑 profile.json 填入真实余额、持有卡片和偏好
```

- `points`：银行可转点体系余额（`amex_mr`、`chase_ur`、`citi_typ`、`capital_one`、`bilt`）
- `airline_miles`：航司里程账户余额，key 用 seats.aero 的 program 名（如 `united`、`alaska`、`aeroplan`）。规划时会优先用直接余额，不足部分再算转点
- `cards`：持有的卡（对应 `credits_catalog.json` 的 key），用于 credit 追踪和税费支付建议
- `preferences.risk_tolerance`：`low` 时 `advise` 会对非即时到账的转点方案标警告
- `preferences.reserve_points`：留底不动用的点数，`advise` 计算时先扣除

转点关系表在 `transfer_partners.json`（银行 → 航司、比例、到账时间），整理于 2026-07。**转点规则会变且转点不可逆**：执行前务必到银行官网核实，先确认库存、再转点、立即出票。`profile.json` 已加入 `.gitignore`，不会被提交。

## points_os 常用命令

```bash
python3 points_os.py status            # 点数余额 + 各卡 credit 状态 + 过期预警
python3 points_os.py status --email    # 同时发邮件（复用 SMTP 配置）
python3 points_os.py advise --top 10   # 当前放位中对你真正可执行的方案，按里程价排序
python3 points_os.py use amex_platinum uber    # 记录本周期 Uber Cash 已用
python3 points_os.py clear amex_platinum uber  # 撤销记录
```

`advise` 读取 `award_watch.sqlite3` 里当前有效的全部放位，用你的余额（扣除留底）逐条判断可执行性，只展示够分的方案；若有未使用的航空杂费类 credit（如 Amex 的 Airline Fee Credit），会提示用它覆盖税费。

建议示例：

- `✔ 从 Amex Membership Rewards 转 75,000（即时到账）`
- `✘ 点数不足：已有 United 余额 30,000，最佳来源 Chase UR 只能凑 50,000，还差 8,000`
- `✘ 美国主流信用卡点数无法转入（Velocity 只接美国以外的部分体系）`

## 提醒逻辑

只有"新出现"的放位才会触发邮件：同一条航线+日期+项目组合，如果上一轮已经提醒过、这一轮仍然有位，不会重复发邮件；如果放位消失后又重新出现，会当作新放位再次提醒。`dashboard.html` 始终展示当前仍然有效的全部放位，不只是新增的。

## 注意事项

- seats.aero 的数据是缓存的（不是每一秒都实时），具体新鲜度取决于订阅套餐；下单前一定要去航司官网或 seats.aero 上核实一遍库存和价格再操作。
- 这个工具只做监控和提醒，不会自动订位。
