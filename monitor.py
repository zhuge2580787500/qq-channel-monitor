#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 频道(pd.qq.com)公开帖子监控 —— 无头浏览器抓取 + 关键词判定 + 邮件提醒

以"监控二手市场出显示器帖子"为默认示例, 频道 / 关键词 / 频率全部可在 config.json 配置。

功能:
  - 每隔 interval_sec ± jitter_sec 秒(随机抖动)用无头 Chromium 抓取频道页面
    (真实浏览器内核, 自动通过 EdgeOne 反爬挑战, 无需登录 QQ 账号)
  - 发现命中关键词的新帖子后发邮件提醒
  - 首次运行只建立基线(记录当前帖子), 不发邮件, 避免历史帖子刷屏
  - 静默时段(默认 0:00-8:00)命中的帖子进队列, 时段结束后汇总发送
  - 某频道连续 fail_alert_after 轮抓取失败时发告警邮件(不受静默时段限制), 恢复后发恢复通知
  - 邮箱未配置时: 命中帖子一直暂存, 配好 config.json 即会在下一轮发出

检测规则(分层, 见 is_match):
  1) 全局硬排除: 求购/收购/回收/出租/维修/预算等词出现即不报
  2) 配件排除: "显示器支架/挂架/膜"等, 卖的不是显示器本身
  3) 邻接判定: 关键词前紧跟 出/卖/转让/闲置 → 出售;
               关键词前紧跟 收/求/找/换/想要/有没有人 → 求购不报;
               关键词后紧跟 出了/出售/可刀/包邮/价格+出 → 出售
  4) 全局兜底: 无邻接关系时, 需有 可刀/包邮/自提/99新 等交易信号才报

用法:
  python3 monitor.py            # 常驻循环运行(systemd 使用)
  python3 monitor.py --once     # 只跑一轮, 用于测试
  python3 monitor.py --status   # 查看当前状态
  python3 monitor.py --test-email  # 发一封测试邮件
  python3 monitor.py --check "出显示器"   # 测试一段文本是否命中
"""
import fcntl
import json
import logging
import os
import random
import re
import smtplib
import sys
import time
import traceback
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from playwright.sync_api import sync_playwright

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
PROFILE_DIR = os.path.join(BASE, "browser-profile")
LOG_PATH = os.path.join(BASE, "monitor.log")
LOCK_PATH = os.path.join(BASE, "monitor.lock")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

POST_ID_RE = re.compile(r"/post/(B_[0-9A-Za-z]+)")
TIME_TOKEN_RE = re.compile(
    r"^(\d+秒前|\d+分钟前|\d+小时前|\d+天前|今天|昨天|刚刚|\d{1,2}-\d{1,2}|\d{4}-\d{1,2}-\d{1,2})$")

DEFAULT_CONFIG = {
    "urls": [
        {"name": "启翔湖畔", "url": "https://pd.qq.com/g/npu0lakeside?subc=1511405"},
        {"name": "湖畔集市", "url": "https://pd.qq.com/g/npu1lakeside"},
    ],
    "interval_sec": 300,
    "jitter_sec": 60,                        # 每轮间隔随机浮动 ±N 秒, 让访问节奏更像人
    "keywords": ["显示器"],
    # 全局兜底的出售词(邻接判定之外, 帖子里出现才作数)
    "sell_words": ["卖", "转让", "闲置", "出售", "急出", "亏出", "白给", "甩卖", "回血", "二手"],
    # 全局硬排除词: 出现任意一个直接不报(求购/回收/租赁/维修服务/预算党)
    "buy_words": ["求购", "求收", "收购", "回收", "出租", "租赁", "转租", "求租",
                  "租借", "预算", "维修", "修理", "换新"],
    # 全局兜底的交易信号(正则): 无邻接关系时, 有这些才算出售帖
    "sell_patterns": ["可刀", "小刀", "大刀", "包邮", "自提", "回血", "回点血",
                      "急售", "亏本", "白菜价", "清仓", "白送", "免费送", "捡漏",
                      "秒出", "有要的", "私我", "刀我",
                      "\\d{1,2}\\s*成新", "\\d{1,2}\\s*新"],
    "quiet": {"start": 0, "end": 8},          # [start, end) 区间内静默不发邮件
    "fail_alert_after": 3,                    # 同一频道连续 N 轮抓取失败则发告警邮件, 0 关闭
    "smtp": {
        "host": "smtp.qq.com",
        "port": 465,
        "user": "",                            # 发件邮箱, 如 xxx@qq.com
        "pass": "",                            # SMTP 授权码(不是邮箱登录密码)
        "to": "",                              # 收件邮箱
    },
}

# ------------------------------------------------------------- 匹配用的正则

# 关键词"前面"窗口的出售信号: 出/卖/转让/闲置/急出/回血... + 可选量词/尺寸
SELL_BEFORE_RE = re.compile(
    r"(急出|亏出|白菜出|便宜出|处理出|清仓|甩卖|转让|闲置|回血|白给|出售|处理|出|卖|甩|刀)"
    r"\s*(一|1|两|2|三|3)?\s*[台块个只]?\s*[0-9寸.成新kK]{0,4}$")

# 关键词"前面"窗口的求购/拒绝出售信号: 收/求/找/换/想要/有没有人/不卖/不出...
BUY_BEFORE_RE = re.compile(
    r"(有没有|有人出|有人要|有人收|有出|谁有|谁出|出不|出吗|出么|不出|没出|别出|勿出|不卖|不转让|"
    r"求购|求收|想收|想入|想要|想买|收购|收|求|找|换)"
    r"\s*[:：,，、]?\s*(一|1|两|2|三|3)?\s*[台块个只]?\s*([0-9寸.]{1,2}(新|成新)?|[0-9寸.]{0,2})$")

# 关键词"后面"紧跟的出售信号: 出了/出售/可刀/包邮/价格+出...
SELL_AFTER_RE = re.compile(
    r"^\s*[:：,，、;；]?\s*("
    r"出了|出售|转让|可刀|小刀|大刀|包邮|自提|回血|回点血|急出|急售|亏出|白菜价?|便宜出|"
    r"处理|清仓|甩卖|甩了|甩|低价|白送|免费送|免费|二手|有要的|有要|要的|换|刀|"
    r"出(?=$|[\s,，。!?!?;；了掉一二两三0-9])|"
    r"\d{2,4}\s*[rR元块]?(?=$|[\s,，。!?!?;；]|出了?|刀|甩|白给|包邮|自提|有要)"
    r")")

# 关键词只作为配件词出现时不算(显示器支架/挂架/膜/底座...)
ACCESSORY_SUFFIXES = ("支架", "挂架", "挂臂", "支臂", "底座", "垫", "膜", "盒",
                      "箱说", "包装箱", "箱", "线", "灯")

# 帖子卡片文本末尾的交互按钮字样(仅从尾部剔除, 不动正文里的同名词)
CARD_TAIL_RE = re.compile(r"\s*(点赞|评论|分享|全文|\.{3}|…)$")


# ---------------------------------------------------------------- 基础工具

def setup_logging():
    logger = logging.getLogger("monitor")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_PATH, encoding="utf-8")):
        h.setFormatter(fmt)
        logger.addHandler(h)
    return logger


def rotate_log():
    try:
        if os.path.getsize(LOG_PATH) > 5 * 1024 * 1024:
            with open(LOG_PATH, encoding="utf-8", errors="ignore") as f:
                data = f.read()
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.write(data[-2 * 1024 * 1024:])
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    saved = load_json(CONFIG_PATH, {})
    if not saved:
        save_json(CONFIG_PATH, cfg)
        return cfg
    cfg.update(saved)
    return cfg


# ---------------------------------------------------------------- 业务逻辑

def clean_text(text):
    return re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", " ", text or "").strip()


def is_match(text, cfg):
    """判定一个帖子是否命中(分层规则, 见模块 docstring)"""
    kws = [k for k in cfg.get("keywords", []) if k]
    if not kws:
        return False
    text = clean_text(text)

    # 第 1 层: 全局硬排除(求购/回收/出租/维修...)
    if any(w in text for w in cfg.get("buy_words", DEFAULT_CONFIG["buy_words"])):
        return False

    sell_words = cfg.get("sell_words", DEFAULT_CONFIG["sell_words"])
    sell_patterns = [re.compile(p) for p in cfg.get("sell_patterns", DEFAULT_CONFIG["sell_patterns"])]

    for kw in kws:
        if kw not in text:
            continue
        # 第 2 层: 关键词只出现在配件词里(显示器支架/显示器膜...) → 不算
        accessory = sum(text.count(kw + s) for s in ACCESSORY_SUFFIXES)
        if text.count(kw) - accessory <= 0:
            continue

        # 第 3 层: 邻接判定(逐个出现位置看前后文)
        buy_adjacent = False
        for m in re.finditer(re.escape(kw), text):
            before = text[max(0, m.start() - 8):m.start()]
            after = text[m.end():m.end() + 8]
            if BUY_BEFORE_RE.search(before):        # 收/求/找/换/有没有人... → 求购帖
                buy_adjacent = True
                continue
            if SELL_BEFORE_RE.search(before) or SELL_AFTER_RE.match(after):
                return True                          # 明确的出售说法

        # 第 4 层: 没有邻接关系时, 用交易信号兜底(可刀/包邮/99新/自提...)
        if not buy_adjacent and (any(p.search(text) for p in sell_patterns)
                                 or any(w in text for w in sell_words)):
            return True
    return False


def is_quiet_now(cfg):
    q = cfg.get("quiet", DEFAULT_CONFIG["quiet"])
    start, end = int(q.get("start", 0)), int(q.get("end", 8))
    h = datetime.now().hour
    if start <= end:
        return start <= h < end
    return h >= start or h < end          # 支持跨零点区间, 如 22-8


def parse_post_text(raw):
    """'作者 时间 正文 点赞 评论 ...' -> (作者, 时间, 正文)"""
    parts = (raw or "").replace("\n", " ").split()
    author, when = "", ""
    rest = parts
    if len(parts) >= 2 and TIME_TOKEN_RE.match(parts[1]):
        author, when, rest = parts[0], parts[1], parts[2:]
    elif len(parts) >= 3 and TIME_TOKEN_RE.match(parts[2]):
        author, when, rest = " ".join(parts[:2]), parts[2], parts[3:]
    content = " ".join(rest)
    # 只从末尾循环剔除卡片按钮字样, 不动正文中间的同名词
    while True:
        stripped = CARD_TAIL_RE.sub("", content).strip()
        if stripped == content:
            break
        content = stripped
    return author.strip(), when.strip(), re.sub(r"\s+", " ", content)[:400]


def fetch_channel_posts(ctx, entry, logger):
    """打开一个频道页, 返回该页可见帖子列表 [{id, url, text}]"""
    url = entry["url"] if isinstance(entry, dict) else entry
    name = entry.get("name", url) if isinstance(entry, dict) else url
    page = ctx.new_page()
    posts = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector('a[href*="/post/"]', timeout=30000)
        # 滚动两屏, 尽量多加载一些帖子
        for _ in range(2):
            page.evaluate(
                "() => { window.scrollBy(0, 2500);"
                " const m = document.querySelector('main'); if (m) m.scrollTop = 5000; }")
            page.wait_for_timeout(1200)
        items = page.evaluate(
            """() => [...document.querySelectorAll('a[href*="/post/"]')]
                 .map(a => ({ href: a.getAttribute('href') || '',
                             text: (a.innerText || '').trim() }))""")
        for it in items:
            m = POST_ID_RE.search(it["href"])
            if not m:
                continue
            href = it["href"]
            if href.startswith("/"):
                href = "https://pd.qq.com" + href
            posts.append({"id": m.group(1), "url": href,
                          "text": re.sub(r"\s+", " ", it["text"])[:500]})
        logger.info("[%s] 抓到 %d 条帖子", name, len(posts))
    finally:
        page.close()
    return name, posts


def run_check_cycle(cfg, state, logger):
    """跑一轮抓取, 返回 (新命中帖子列表, 各频道健康状态 {name: True/False})"""
    new_matches = []
    health = {}
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=True,
            user_agent=UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1280, "height": 900},
            args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-background-networking"],
        )
        try:
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            seen = state.setdefault("seen", [])
            first_run = not state.get("initialized")
            for entry in cfg["urls"]:
                name = entry.get("name", entry["url"]) if isinstance(entry, dict) else entry
                try:
                    name, posts = fetch_channel_posts(ctx, entry, logger)
                    if not posts:
                        # 能打开页面却解析不到帖子, 多半是挑战页/改版, 视为失败以便触发告警
                        raise RuntimeError("页面已加载但未解析到任何帖子")
                    health[name] = True
                except Exception:
                    logger.error("抓取失败 %s\n%s", entry, traceback.format_exc())
                    health[name] = False
                    continue
                for post in posts:
                    if post["id"] in seen:
                        continue
                    seen.append(post["id"])
                    post["channel"] = name
                    post["found"] = datetime.now().strftime("%m-%d %H:%M")
                    if first_run:
                        continue                     # 首轮只建基线
                    if is_match(post["text"], cfg):
                        new_matches.append(post)
                        logger.info("命中: [%s] %s", name, post["text"][:80])
            if len(seen) > 800:
                state["seen"] = seen[-800:]
            state["initialized"] = True
        finally:
            ctx.close()
    return new_matches, health


def check_fetch_health(cfg, state, health, logger):
    """同一频道连续多轮抓取失败时发告警邮件; 恢复后发恢复通知。
    属于系统健康告警, 不受静默时段限制。"""
    if not health:
        return
    threshold = int(cfg.get("fail_alert_after", 3))
    if threshold <= 0:
        return
    book = state.setdefault("health", {})
    for name, ok in health.items():
        h = book.setdefault(name, {"fail": 0, "alerted": False})
        if ok:
            if h.get("alerted") and smtp_ready(cfg["smtp"]):
                logger.info("[%s] 抓取已恢复", name)
                send_email(cfg, "【湖畔监控】%s 抓取已恢复" % name,
                           "频道「%s」此前连续抓取失败, 现已恢复正常。\n%s" % (name, datetime.now()),
                           logger)
            h["fail"] = 0
            h["alerted"] = False
            continue
        h["fail"] += 1
        logger.warning("[%s] 连续第 %d 轮抓取失败", name, h["fail"])
        if h["fail"] >= threshold and not h.get("alerted"):
            if not smtp_ready(cfg["smtp"]):
                logger.warning("邮箱未配置, 无法发送抓取失败告警")
                continue
            minutes = h["fail"] * int(cfg.get("interval_sec", 300)) // 60
            sent = send_email(cfg, "【湖畔监控】异常: %s 连续 %d 轮抓取失败" % (name, h["fail"]),
                              "频道「%s」已连续 %d 轮(约 %d 分钟)抓取失败。\n"
                              "可能原因: 反爬策略收紧/页面改版/服务器网络异常。\n"
                              "详细错误见服务器日志 %s\n%s"
                              % (name, h["fail"], minutes, LOG_PATH, datetime.now()),
                              logger)
            if sent:
                h["alerted"] = True


# ---------------------------------------------------------------- 邮件

def smtp_ready(smtp):
    return bool(smtp.get("user") and smtp.get("pass") and smtp.get("to"))


def send_email(cfg, subject, body, logger):
    smtp = cfg["smtp"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("湖畔监控", "utf-8")), smtp["user"]))
    msg["To"] = smtp["to"]
    try:
        if int(smtp.get("port", 465)) == 465:
            s = smtplib.SMTP_SSL(smtp["host"], int(smtp.get("port", 465)), timeout=30)
        else:
            s = smtplib.SMTP(smtp["host"], int(smtp.get("port", 587)), timeout=30)
            s.starttls()
        with s:
            s.login(smtp["user"], smtp["pass"])
            s.sendmail(smtp["user"], [smtp["to"]], msg.as_string())
        return True
    except Exception:
        logger.error("邮件发送失败\n%s", traceback.format_exc())
        return False


def build_digest(posts):
    lines = []
    for p in posts:
        author, when, content = parse_post_text(p["text"])
        lines.append("频道: %s\n作者: %s  (发帖时间: %s)\n内容: %s\n链接: %s\n%s"
                     % (p.get("channel", "?"), author or "?", when or "?",
                        content or p["text"][:120], p["url"], "-" * 46))
    return "\n".join(lines)


def maybe_notify(cfg, state, new_matches, logger):
    """静默期入队; 非静默且邮箱可用时, 新命中 + 队列一起发送"""
    pending = state.setdefault("pending", [])
    known = {p["id"] for p in pending}
    for p in new_matches:
        if p["id"] not in known:
            pending.append(p)
    if not pending:
        return
    if is_quiet_now(cfg):
        logger.info("静默时段(%d-%d点), %d 条待发, 时段结束后汇总发送",
                    int(cfg.get("quiet", {}).get("start", 0)),
                    int(cfg.get("quiet", {}).get("end", 8)), len(pending))
        return
    if not smtp_ready(cfg["smtp"]):
        logger.info("邮箱未配置, %d 条命中帖子暂存等待配置", len(pending))
        return
    kws = "/".join(cfg.get("keywords", [])) or "监控"
    subject = "【湖畔监控】发现 %d 条「%s」相关帖子" % (len(pending), kws)
    if send_email(cfg, subject, build_digest(pending), logger):
        logger.info("邮件已发送(%d 条)", len(pending))
        state["pending"] = []


# ---------------------------------------------------------------- 主流程

def main():
    once = "--once" in sys.argv
    if "--status" in sys.argv:
        state = load_json(STATE_PATH, {})
        print("initialized : %s" % state.get("initialized"))
        print("seen posts  : %d" % len(state.get("seen", [])))
        print("pending     : %d" % len(state.get("pending", [])))
        for p in state.get("pending", []):
            print("  - [%s] %s" % (p.get("channel", "?"), p["text"][:60]))
        for name, h in (state.get("health") or {}).items():
            print("health[%s] : 连续失败 %s 轮%s" % (name, h.get("fail", 0),
                  "(已告警)" if h.get("alerted") else ""))
        return

    if "--check" in sys.argv:
        cfg = load_config()
        idx = sys.argv.index("--check")
        text = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else ""
        print("命中" if is_match(text, cfg) else "不命中")
        return

    logger = setup_logging()
    if "--test-email" in sys.argv:
        cfg = load_config()
        ok = send_email(cfg, "【湖畔监控】测试邮件",
                        "这是一封测试邮件, 收到说明邮箱配置正确。\n%s" % datetime.now(), logger)
        logger.info("测试邮件 %s", "成功" if ok else "失败")
        return

    # 单实例锁, 防止 --once 手动测试与服务进程同时抓取导致重复发信
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("另一个实例正在运行, 退出")
        return

    logger.info("===== 监控启动 (pid %d) =====", os.getpid())
    while True:
        started = time.time()
        rotate_log()
        try:
            cfg = load_config()
            state = load_json(STATE_PATH, {})
            matches, health = run_check_cycle(cfg, state, logger)
            save_json(STATE_PATH, state)     # 先落盘已见列表, 避免发信后崩溃导致重复提醒
            check_fetch_health(cfg, state, health, logger)
            maybe_notify(cfg, state, matches, logger)
            save_json(STATE_PATH, state)
        except Exception:
            logger.error("本轮执行异常\n%s", traceback.format_exc())
        if once:
            logger.info("--once 模式, 本轮结束")
            break
        base = int(cfg.get("interval_sec", 300))
        jitter = int(cfg.get("jitter_sec", 0))
        period = base + random.uniform(-jitter, jitter) if jitter > 0 else base
        sleep = max(30, period - (time.time() - started))
        time.sleep(sleep)


if __name__ == "__main__":
    main()
