# qq-channel-monitor

监控 QQ 频道(pd.qq.com)的公开帖子,发现命中关键词的新帖后自动发邮件提醒。
默认示例场景:监控大学二手市场的「出显示器」帖子,也适合盯任何频道的任何关键词。

> **无需登录 QQ 账号** —— 频道页面对游客可见,本项目不接触任何 QQ 凭据。

## 特性

- 🕷️ **真实浏览器内核抓取**:自动通过腾讯 EdgeOne 的 JS 反爬挑战(纯 HTTP 请求拿不到页面内容)
- 🧠 **分层关键词判定**:不只是"包含关键词"——能区分"出显示器"和"求购显示器",能识别"显示器支架"这类配件词,误报率低(详见[检测规则](#-检测规则))
- 🔁 **按帖子 ID 去重**:同一帖子只提醒一次;首轮运行只建立基线,不会用历史帖刷屏
- 🌙 **静默时段**:凌晨命中的帖子不丢,时段结束后汇总成一封邮件
- 🩺 **存活自检**:某频道连续多轮抓取失败时发告警邮件,恢复后发恢复通知
- 🎲 **随机抖动**:检测间隔随机浮动,访问节奏不像机器人
- 🛠️ **零框架单文件**:一个 Python 文件 + systemd,适合丢在任意一台小服务器上

## 工作原理

```
QQ 频道页面(可配置多个)
      │  每 interval_sec ± jitter_sec 秒一轮
      ▼
无头 Chromium 渲染页面(自动解 EdgeOne JS 挑战,cookie 持久化复用)
      │  提取每张帖子卡片: 帖子ID + 可见文本(作者/时间/正文/图片标题)
      ▼
按帖子 ID 去重(state.json, 只处理新帖)
      │
      ▼
四层规则判定是否命中
      │ 命中
      ▼
静默时段? ──是──> 存入待发队列,时段结束后汇总发
      │否
      ▼
SMTP 邮件(含作者/内容/帖子直达链接)
```

抓取失败不影响其它频道;整轮异常只记日志、下一轮自动继续。

## 环境要求

- Python 3.8+(仅用标准库 + playwright)
- 能访问 `pd.qq.com` 的机器(国内服务器/本机均可)

## 快速开始

### 1. 安装

```bash
git clone https://github.com/<你的用户名>/qq-channel-monitor.git
cd qq-channel-monitor
pip3 install -r requirements.txt

# 安装 Chromium 及其系统依赖(Ubuntu/Debian)
python3 -m playwright install-deps chromium
python3 -m playwright install chromium
# 国内服务器下载慢可加镜像:
# PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright python3 -m playwright install chromium
```

### 2. 配置

```bash
cp config.example.json config.json
vi config.json
```

必填的只有 `smtp` 三项(以 QQ 邮箱为例:网页版邮箱 → 设置 → 账号 → 开启 SMTP 服务 → 生成**授权码**,授权码不是邮箱登录密码)。其余字段都有默认值,不填会用内置默认。

### 3. 试跑一轮

```bash
python3 monitor.py --once          # 跑一轮(首轮只建基线)
python3 monitor.py --status        # 查看已跟踪帖子数、待发队列
python3 monitor.py --check "出一台显示器"    # 测试任意文本会不会命中
python3 monitor.py --test-email    # 验证邮箱配置
```

首轮运行会记录当前所有帖子作为基线,**不发邮件**;之后只提醒新出现的命中帖。

### 4. 常驻运行(systemd)

```bash
sudo cp qq-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now qq-monitor
```

服务崩溃自动重启、开机自启。若部署路径/用户不同,修改 unit 文件里的 `WorkingDirectory` 和 `ExecStart`。

## 配置说明

| 字段 | 说明 | 默认 |
|---|---|---|
| `urls` | 要监控的频道页,`name` 只用于展示 | 两个示例频道 |
| `interval_sec` | 检测间隔(秒) | `300` |
| `jitter_sec` | 间隔随机浮动 ±N 秒,`0` 关闭 | `60` |
| `keywords` | 关键词列表,帖子文本含任一即进入判定 | `["显示器"]` |
| `sell_words` | 出售词(全局兜底判定用) | 见文件 |
| `buy_words` | 硬排除词,帖子出现任一即不报 | 求购/回收/出租/维修… |
| `sell_patterns` | 交易信号(正则),兜底判定用 | 可刀/包邮/99新… |
| `quiet` | 静默时段 `[start, end)`,小时数,支持跨零点 | `0-8` 点 |
| `fail_alert_after` | 连续 N 轮抓取失败发告警邮件,`0` 关闭 | `3` |
| `smtp` | 发件邮箱 / SMTP 授权码 / 收件邮箱 | 空 |

修改配置后执行 `sudo systemctl restart qq-monitor` 生效。

## 🔍 检测规则

命中需要**同时满足**:包含关键词 + 是"出售"而不是"求购"。判定分四层:

1. **硬排除**:帖子含 `求购 / 回收 / 出租 / 预算 / 维修` 等词 → 不报
2. **配件排除**:关键词只出现在"显示器支架 / 显示器膜"等配件词里 → 不报
3. **邻接判定**(核心):看关键词紧邻的前后文字——
   - 前面是`出 / 卖 / 转让 / 闲置 / 急出` → 出售 ✅
   - 前面是`收 / 求 / 找 / 换 / 想要 / 有没有人 / 谁有` → 求购 ❌
   - 后面紧跟`出了 / 出售 / 可刀 / 包邮 / 价格+出` → 出售 ✅
4. **交易信号兜底**:没有邻接线索时,帖子里须有`可刀 / 包邮 / 自提 / 99新 / 成新`等交易信号才报——防止"今天**演出**,教室显示器坏了"这种误报

规则内置了 30+ 条测试用例(覆盖求购、配件、出租、倒装句、价格后置等场景),可用 `--check` 验证:

```bash
python3 monitor.py --check "出一台27寸显示器 300可刀"   # 命中
python3 monitor.py --check "求购一台显示器"             # 不命中
```

## 运维

```bash
systemctl status qq-monitor        # 服务状态
tail -f ~/qq-monitor/monitor.log   # 实时日志
python3 monitor.py --status        # 已跟踪帖子数 / 待发队列 / 各频道健康度
sudo systemctl restart qq-monitor  # 改配置后重启
```

## 常见问题

**会被腾讯反爬吗?**
抓取行为与真人浏览几乎一致(频率克制、真实内核、cookie 复用),项目作者长期运行未遇拦截。即使策略收紧,失败模式也是"暂时抓不到 → 记日志 → 连续失败会收到告警邮件",自动重试恢复。请勿调高频率滥用。

**需要登录 QQ 吗?**
不需要。频道首页对游客可见;也正因如此,只能看到每个频道首页列表里的帖子(约一屏多),更早的帖子不会触发提醒。

**能监控其它频道/其它关键词吗?**
能。把 `urls` 换成任意频道页链接、`keywords` 换成任意词即可(如 `["键盘", "显卡"]`)。注意适配一下 `sell_words`/`buy_words`,让"出/收"的语义符合你的品类。

**支持微信/钉钉/飞书提醒吗?**
当前只内置了 SMTP 邮件。`send_email()` 只有一处调用,换成任意推送渠道的 Webhook 都很容易。

## 免责声明

本项目仅用于个人学习与生活便利,请遵守目标平台的服务条款,保持低频访问,勿用于商业用途或对目标站点造成压力。使用本项目产生的任何风险由使用者自行承担。

## License

[MIT](LICENSE)
