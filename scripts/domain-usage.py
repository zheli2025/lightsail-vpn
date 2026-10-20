#!/usr/bin/env python3
#
# domain-usage - 按域名记录 Hysteria 代理流量的上传/下载字节数
#
# 数据来源:
#   Hysteria 流量统计 API /dump/streams（活跃 TCP 流快照，含 req_addr、
#   hooked_req_addr、tx/rx 字节计数）。Hysteria 只在流存活期间记账，所以
#   本采集器以固定间隔（默认 5 秒）轮询并对每条流做增量差分，避免漏记。
#
# 数据存储:
#   SQLite 按 (日期, 分类, 域名) 累计 upload/download，另存流状态供
#   采集器重启后恢复基线；历史明细定期清理，汇总数据长期保留。
#
# 用法:
#   domain-usage collect   采集循环（systemd 服务运行）
#   domain-usage status    查看采集状态
#   domain-usage report [today|month|all|topN] [--json]
#   domain-usage prune     清理过期明细（维护任务）
#
import argparse
import curses
import datetime
import locale
import json
import os
import re
import signal
import sqlite3
import unicodedata
import sys
import time
import urllib.error
import urllib.request

API_URL = os.environ.get("DU_API_URL", "http://127.0.0.1:25413/dump/streams")
SECRET_FILE = os.environ.get("DU_SECRET_FILE", "/etc/hysteria/.traffic-stats-secret")
DB_FILE = os.environ.get("DU_DB_FILE", "/var/lib/domain-usage/usage.db")
LOG_FILE = os.environ.get("DU_LOG_FILE", "/var/log/domain-usage.log")
RUNTIME_DIR = os.environ.get("DU_RUNTIME_DIR", "/run/domain-usage")
STOP_FILE = os.path.join(RUNTIME_DIR, "stop")

POLL_INTERVAL = float(os.environ.get("DU_POLL_INTERVAL", "5"))
HTTP_TIMEOUT = float(os.environ.get("DU_HTTP_TIMEOUT", "4"))
MAX_BACKOFF = 60.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily (
    day TEXT NOT NULL,
    category TEXT NOT NULL,
    domain TEXT NOT NULL,
    upload INTEGER NOT NULL DEFAULT 0,
    download INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, category, domain)
);
CREATE INDEX IF NOT EXISTS idx_daily_day ON daily(day);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# 域名 -> 分类。所有规则做“域及子域”匹配（www.google.com -> google）。
DOMAIN_RULES = {
    "google": [
        "google.com", "googleapis.com", "gstatic.com", "googlevideo.com",
        "ggpht.com", "googleusercontent.com", "youtube.com", "ytimg.com",
        "yt.be", "gvt1.com", "gvt2.com", "googletagmanager.com",
        "google-analytics.com", "doubleclick.net", "googleadservices.com",
        "googleads.g.doubleclick.net", "googlemail.com", "gmail.com",
        "recaptcha.net", "google.cn", "g.cn", "googlesyndication.com",
    ],
    "openai": [
        "openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com",
        "openai.org", "challenges.cloudflare.com", "auth.openai.com",
        "sentry.io", "segment.io", "intercom.io",
    ],
    "anthropic": [
        "anthropic.com", "claude.ai", "claudeusercontent.com",
        "aws.amazon.com", "bedrock.amazonaws.com",
    ],
    "meta": [
        "facebook.com", "fbcdn.net", "fb.com", "instagram.com", "cdninstagram.com",
        "whatsapp.com", "whatsapp.net", "messenger.com", "meta.com",
    ],
    "microsoft": [
        "microsoft.com", "live.com", "office.com", "office365.com",
        "windows.com", "windowsupdate.com", "msftconnecttest.com", "bing.com",
        "bing.net", "skype.com", "github.com", "githubusercontent.com",
        "githubassets.com", "azure.com", "azureedge.net", "azurefd.net",
        "msn.com", "microsoftonline.com", "sharepoint.com", "onedrive.com",
    ],
    "apple": [
        "apple.com", "icloud.com", "icloud-content.com", "mzstatic.com",
        "aaplimg.com", "apple-cloudkit.com", "appstore.com",
    ],
    "amazon": [
        "amazon.com", "amazonaws.com", "amazon-adsystem.com", "awsstatic.com",
        "cloudfront.net", "amazonvideo.com", "primevideo.com", "twitch.tv",
        "twitchcdn.net",
    ],
    "twitter": [
        "twitter.com", "x.com", "twimg.com", "t.co", "twtrcdn.net",
    ],
    "telegram": [
        "telegram.org", "t.me", "telegram.me", "tg.dev", "telesco.pe",
    ],
    "discord": [
        "discord.com", "discord.gg", "discordapp.com", "discord.media",
    ],
    "netflix": [
        "netflix.com", "nflxvideo.net", "nflxext.com", "nflximg.net",
        "nflxso.net",
    ],
    "spotify": [
        "spotify.com", "scdn.co", "spotifycdn.com", "spotify.link",
    ],
    "cloudflare": [
        "cloudflare.com", "cloudflare-dns.com", "cloudflare.net",
        "cf-ns.com", "cloudflareclient.com", "workers.dev",
    ],
    "tiktok": [
        "tiktok.com", "tiktokcdn.com", "tiktokv.com", "tiktokcdn-us.com",
        "byteoversea.com", "musical.ly",
    ],
    "steam": [
        "steampowered.com", "steamstatic.com", "steamcontent.com",
        "steamcommunity.com", "steam-chat.com",
    ],
    "epic": [
        "epicgames.com", "epicgames.dev", "unrealengine.com",
    ],
    "nvidia": [
        "nvidia.com", "nvidia.cn", "nvidiagrid.net",
    ],
}

# 域名解析失败时的兜底分类
IP_CATEGORY = "ip"
DEFAULT_CATEGORY = "其他"
PRIVATE_CATEGORY = "内网"

PRIVATE_PREFIXES = (
    "10.", "127.", "169.254.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.2", "172.3", "192.168.",
)
LOCALHOST = ("localhost", "127.0.0.1", "::1", "[::1]")


def log(msg):
    line = f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} {msg}"
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def rotate_log():
    try:
        if os.path.getsize(LOG_FILE) > 2_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".1")
            open(LOG_FILE, "a", encoding="utf-8").close()
    except OSError:
        pass


def read_secret():
    try:
        with open(SECRET_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError as exc:
        log(f"WARN 无法读取 API secret {SECRET_FILE}: {exc}")
        return ""


def fetch_streams():
    secret = read_secret()
    req = urllib.request.Request(
        API_URL,
        headers={"Authorization": secret} if secret else {},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = json.load(resp)
    return data.get("streams") or []


def extract_host(addr):
    """从 'example.com:80' / '1.2.3.4:443' / '[::1]:443' 提取主机名。"""
    if not addr:
        return ""
    host = addr.strip()
    if host.startswith("["):
        host = host.split("]", 1)[0].lstrip("[")
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    else:
        host = host.rstrip(".")
    return host.lower()


def host_category(host):
    """把主机名归类到 google/openai 等；返回 (category, domain)。"""
    if not host:
        return DEFAULT_CATEGORY, ""
    host = host.rstrip(".")
    if host in LOCALHOST:
        return PRIVATE_CATEGORY, host
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host):
        if host.startswith(PRIVATE_PREFIXES):
            return PRIVATE_CATEGORY, host
        return IP_CATEGORY, host
    if ":" in host and not host.startswith("["):
        return IP_CATEGORY, host
    for category, rules in DOMAIN_RULES.items():
        for rule in rules:
            if host == rule or host.endswith("." + rule):
                return category, host
    return DEFAULT_CATEGORY, host


def connect_db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def today_str():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def upsert_daily(conn, day, category, domain, upload, download):
    if upload <= 0 and download <= 0:
        return
    conn.execute(
        """INSERT INTO daily (day, category, domain, upload, download)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(day, category, domain) DO UPDATE SET
             upload = daily.upload + excluded.upload,
             download = daily.download + excluded.download""",
        (day, category, domain, upload, download),
    )


def cmd_collect():
    rotate_log()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    log("== domain-usage 采集器启动 ==")
    log(f"API: {API_URL}")
    log(f"轮询间隔: {POLL_INTERVAL}s，数据库: {DB_FILE}")

    conn = connect_db()
    state = {}
    backoff = 1.0
    last_poll_ok = None

    while True:
        if os.path.exists(STOP_FILE):
            log("检测到 stop 标记，退出")
            break
        started = time.monotonic()
        try:
            streams = fetch_streams()
            deltas = []
            current = {}
            seen = set()

            for entry in streams:
                conn_id = entry.get("connection")
                stream_id = entry.get("stream")
                if conn_id is None or stream_id is None:
                    continue
                key = f"{conn_id}:{stream_id}"
                seen.add(key)
                tx = int(entry.get("tx") or 0)
                rx = int(entry.get("rx") or 0)
                prev = state.get(key)
                base_tx = prev[0] if prev else 0
                base_rx = prev[1] if prev else 0
                dtx = tx - base_tx if tx >= base_tx else 0
                drx = rx - base_rx if rx >= base_rx else 0

                hooked = entry.get("hooked_req_addr") or ""
                req = entry.get("req_addr") or ""
                host = extract_host(hooked) or extract_host(req)
                category, domain = host_category(host)
                if domain:
                    deltas.append((category, domain, dtx, drx))

                state[key] = (tx, rx)

            # 消失的流：基线归零；下次重新出现会按新流记账
            for key in list(state):
                if key not in seen:
                    del state[key]

            day = today_str()
            with conn:
                for category, domain, dtx, drx in deltas:
                    upsert_daily(conn, day, category, domain, dtx, drx)
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('last_poll_ok', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (datetime.datetime.now(datetime.timezone.utc).isoformat(),),
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('last_stream_count', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(len(streams)),),
                )

            if last_poll_ok is None:
                log(f"首次轮询成功: {len(streams)} 条活跃流")
            last_poll_ok = time.time()
            backoff = 1.0
        except Exception as exc:
            log(f"ERROR 轮询失败: {type(exc).__name__}: {exc}")
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

        elapsed = time.monotonic() - started
        sleep_for = max(POLL_INTERVAL - elapsed, 0.1)
        time.sleep(sleep_for)


def load_meta(conn):
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {k: v for k, v in rows}


def fmt_bytes(n):
    n = int(n or 0)
    if n >= 1024**3:
        return f"{n / 1024**3:.2f} GB"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def query_rows(period):
    """返回 (rows, total_up, total_dn)；rows 为按合计降序的 (分类, 域名, 上传, 下载)。"""
    conn = connect_db()
    if period == "today":
        where, params = "WHERE day = ?", [today_str()]
    elif period == "month":
        now = datetime.datetime.now(datetime.timezone.utc)
        prefix = f"{now.year:04d}-{now.month:02d}-"
        where, params = "WHERE day LIKE ?", [prefix + "%"]
    elif period == "all":
        where, params = "", []
    else:
        die(f"未知统计周期: {period}")
    rows = conn.execute(
        f"""SELECT category, domain, SUM(upload), SUM(download)
            FROM daily {where}
            GROUP BY category, domain
            ORDER BY SUM(upload + download) DESC""",
        params,
    ).fetchall()
    total_up = sum(int(r[2] or 0) for r in rows)
    total_dn = sum(int(r[3] or 0) for r in rows)
    return rows, total_up, total_dn


def cmd_report(period, as_json):
    rows, total_up, total_dn = query_rows(period)

    if as_json:
        conn = connect_db()
        updated_at = load_meta(conn).get("last_poll_ok")
        conn.close()
        print(json.dumps(
            {
                "period": period,
                "updated_at": updated_at,
                "upload": total_up,
                "download": total_dn,
                "total": total_up + total_dn,
                "top": [
                    {
                        "category": r[0],
                        "domain": r[1],
                        "upload": r[2],
                        "download": r[3],
                    }
                    for r in rows[:20]
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
        return

    print(f"周期: {period}    总计: 上传 {fmt_bytes(total_up)} / 下载 {fmt_bytes(total_dn)}")
    print(f"{'分类':<10} {'域名':<42} {'上传':>12} {'下载':>12} {'合计':>12}")
    for category, domain, up, dn in rows[:30]:
        print(
            f"{category:<10} {domain:<42} {fmt_bytes(up):>12} "
            f"{fmt_bytes(dn):>12} {fmt_bytes(up + dn):>12}"
        )
    if len(rows) > 30:
        print(f"（仅显示前 30 条，共 {len(rows)} 个域名）")


def cmd_status():
    print("采集器: ", end="")
    try:
        active = subprocess_run(["systemctl", "is-active", "domain-usage.service"])
    except OSError:
        active = "未知"
    print(active)
    print("数据库:", DB_FILE)
    if not os.path.exists(DB_FILE):
        print("状态: 尚未采集到数据")
        return
    conn = connect_db()
    meta = load_meta(conn)
    day = today_str()
    row = conn.execute(
        "SELECT SUM(upload), SUM(download) FROM daily WHERE day = ?", [day]
    ).fetchone()
    print("最后轮询:", meta.get("last_poll_ok") or "-")
    print("活跃流:", meta.get("last_stream_count") or "0")
    print(f"今日: 上传 {fmt_bytes(row[0])} / 下载 {fmt_bytes(row[1])}")
    print("数据库大小:", fmt_bytes(os.path.getsize(DB_FILE)))


def subprocess_run(cmd):
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def cmd_prune(days=180):
    conn = connect_db()
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=days)
    ).date().isoformat()
    with conn:
        cur = conn.execute("DELETE FROM daily WHERE day < ?", [cutoff])
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    log(f"PRUNE 清理 {cur.rowcount} 条早于 {cutoff} 的明细")
    print(f"已清理 {cur.rowcount} 条早于 {cutoff} 的记录")


# ---------------- TUI ----------------

TUI_PERIODS = [("today", "今日"), ("month", "本月"), ("all", "全部")]
TUI_SORTS = [("total", "合计"), ("upload", "上传"), ("download", "下载"), ("domain", "域名")]


def _tui_width(s):
    return sum(
        2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s)
    )


def _tui_pad(s, width):
    s = str(s)
    return s + " " * max(width - _tui_width(s), 0)


def _tui_clip(s, width):
    s = str(s)
    while s and _tui_width(s) > width:
        s = s[:-1]
    return s


def _tui_time(iso):
    try:
        return (
            datetime.datetime.fromisoformat(iso)
            .astimezone()
            .strftime("%H:%M:%S")
        )
    except (TypeError, ValueError):
        return "-"


def _tui_lightsail():
    path = os.environ.get("TRAFFIC_STATE_FILE", "/var/lib/traffic/state.json")
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("total"):
            return d
    except (OSError, ValueError):
        pass
    return {}


def _tui_sorted(rows, mode):
    if mode == "upload":
        return sorted(rows, key=lambda r: (-int(r[2] or 0), r[1]))
    if mode == "download":
        return sorted(rows, key=lambda r: (-int(r[3] or 0), r[1]))
    if mode == "domain":
        return sorted(rows, key=lambda r: (r[1].lower(), -int(r[2] or 0) - int(r[3] or 0)))
    return sorted(rows, key=lambda r: (-int(r[2] or 0) - int(r[3] or 0), r[1]))


def _tui_add(stdscr, y, x, text, attr=0):
    """越界安全写入：自动裁剪到终端范围，避免 curses 抛 ERR。"""
    try:
        maxy, maxx = stdscr.getmaxyx()
    except Exception:
        return
    if y < 0 or y >= maxy or x < 0 or x >= maxx:
        return
    text = str(text)
    avail = maxx - x
    if avail <= 0:
        return
    while text and _tui_width(text) > avail:
        text = text[:-1]
    if not text:
        return
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def _tui_colors(stdscr):
    colors = {}
    if curses.has_colors():
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass
        colors["title"] = curses.color_pair(1) | curses.A_BOLD
        colors["head"] = curses.color_pair(2) | curses.A_BOLD
        colors["ok"] = curses.color_pair(3)
        colors["warn"] = curses.color_pair(4)
        colors["dim"] = curses.color_pair(5)
        colors["foot"] = curses.color_pair(6) | curses.A_BOLD
        colors["bar"] = curses.color_pair(7) | curses.A_BOLD
        fgs = [
            curses.COLOR_YELLOW, curses.COLOR_GREEN, curses.COLOR_CYAN,
            curses.COLOR_MAGENTA, curses.COLOR_RED, curses.COLOR_BLUE,
        ]
        for i, fg in enumerate(fgs):
            try:
                curses.init_pair(10 + i, fg, -1)
            except curses.error:
                pass
        colors["cat"] = [
            curses.color_pair(10 + i) for i in range(6)
        ]
    else:
        colors["title"] = curses.A_BOLD
        colors["head"] = curses.A_BOLD
        colors["ok"] = curses.A_NORMAL
        colors["warn"] = curses.A_NORMAL
        colors["dim"] = curses.A_DIM
        colors["foot"] = curses.A_REVERSE
        colors["bar"] = curses.A_BOLD
        colors["cat"] = [curses.A_NORMAL] * 6
    return colors


def _tui_draw(stdscr, state):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if h < 8 or w < 60:
        _tui_add(stdscr, 0, 0, f"终端窗口太小（当前 {w}x{h}），建议至少 60x12")
        stdscr.refresh()
        return
    colors = state["colors"]

    # 列宽
    cat_w, up_w, dn_w, tot_w, pct_w, sep = 10, 12, 12, 12, 7, 2
    dom_w = w - cat_w - up_w - dn_w - tot_w - pct_w - 4 * sep
    if dom_w < 12:
        up_w = max(8, w // 14)
        dn_w = max(8, w // 14)
        tot_w = max(8, w // 14)
        cat_w = max(6, w // 12)
        pct_w = 0
        dom_w = w - cat_w - up_w - dn_w - tot_w - 4 * sep
        if dom_w < 8:
            dom_w = 8

    # 标题 / 周期页签
    y = 0
    tab = "  ".join(
        f"[{name}]" if period == state["period"] else f" {name} "
        for period, name in TUI_PERIODS
    )
    _tui_add(stdscr, y, 0, "Hysteria 按域名流量统计", colors["title"])
    _tui_add(stdscr, y, w - _tui_width(tab), tab, colors["ok"])

    # 汇总行
    y += 1
    total = state["up"] + state["dn"]
    line1 = (
        f"上传 {fmt_bytes(state['up'])} · 下载 {fmt_bytes(state['dn'])}"
        f" · 合计 {fmt_bytes(total)}"
    )
    _tui_add(stdscr, y, 0, _tui_clip(line1, w), colors["head"])
    meta = state["meta"]
    right = f"更新 {_tui_time(meta.get('last_poll_ok'))} · 活跃流 {meta.get('last_stream_count', '-')}"
    _tui_add(stdscr, y, w - _tui_width(right), right, colors["dim"])

    # 套餐 / 错误行
    y += 1
    ls = state["ls"]
    if state["err"]:
        _tui_add(stdscr, y, 0, _tui_clip("采集失败: " + state["err"], w), colors["warn"])
    elif ls.get("total"):
        remain = ls.get("remaining", 0)
        used = ls.get("used", 0)
        quota = (
            f"Lightsail 套餐: 剩余 {remain / 1024 ** 3:.1f} GB / "
            f"{ls['total'] / 1024 ** 3:.0f} GB · 已用 {used / 1024 ** 3:.2f} GB"
        )
        _tui_add(stdscr, y, 0, _tui_clip(quota, w), colors["ok"])
    else:
        _tui_add(stdscr, y, 0, "Lightsail 套餐信息未配置", colors["dim"])

    # 分隔线
    y += 1
    _tui_add(stdscr, y, 0, "─" * w, colors["dim"])

    # 表头
    y += 1
    head_row = (
        _tui_pad("分类", cat_w) + " " * sep
        + _tui_pad("域名", dom_w) + " " * sep
        + _tui_pad("上传", up_w) + " " * sep
        + _tui_pad("下载", dn_w) + " " * sep
        + _tui_pad("合计", tot_w)
    )
    if pct_w:
        head_row += " " * sep + _tui_pad("占比", pct_w)
    _tui_add(stdscr, y, 0, _tui_clip(head_row, w), colors["head"])

    # 数据行
    rows = state["rows"]
    body_h = h - y - 2  # 表头后 + 底部提示
    max_scroll = max(0, len(rows) - body_h)
    state["scroll"] = max(0, min(state["scroll"], max_scroll))
    y += 1
    if not rows:
        _tui_add(stdscr, y, 0, "暂无数据（等待采集器记录流量）", colors["dim"])
    for r in rows[state["scroll"]: state["scroll"] + body_h]:
        if y >= h - 1:
            break
        cat, dom, up, dn = r[0], r[1], int(r[2] or 0), int(r[3] or 0)
        x = 0
        seg = _tui_pad(_tui_clip(cat, cat_w), cat_w)
        idx = (sum(ord(ch) for ch in cat) + len(cat)) % 6
        _tui_add(stdscr, y, x, seg, colors["cat"][idx])
        x += cat_w + sep
        _tui_add(stdscr, y, x, _tui_pad(_tui_clip(dom, dom_w), dom_w))
        x += dom_w + sep
        _tui_add(stdscr, y, x, _tui_pad(fmt_bytes(up), up_w))
        x += up_w + sep
        _tui_add(stdscr, y, x, _tui_pad(fmt_bytes(dn), dn_w))
        x += dn_w + sep
        _tui_add(stdscr, y, x, _tui_pad(fmt_bytes(up + dn), tot_w))
        if pct_w:
            x += tot_w + sep
            pct = (up + dn) / total * 100 if total else 0.0
            _tui_add(stdscr, y, x, _tui_pad(f"{pct:5.1f}%", pct_w))
        y += 1

    # 底部提示 / 搜索输入
    sort_name = dict(TUI_SORTS)[state["sort"]]
    if state["searching"]:
        prompt = "搜索: " + state["buf"] + "▌"
        hint = _tui_pad("Enter 确认 · ESC 取消", 24)
    else:
        prompt = (
            "q 退出 · t/m/a 周期 · ←/→ 切换 · s 排序(" + sort_name
            + ") · / 搜索 · r 刷新 · ↑/↓ 滚动"
        )
        if state["search"]:
            prompt += f" · 过滤: {state['search']}"
        hint = _tui_pad("", 0)
    _tui_add(stdscr, h - 1, 0, _tui_clip(prompt, w - _tui_width(hint)), colors["foot"])
    _tui_add(stdscr, h - 1, w - _tui_width(hint), hint, colors["dim"])
    stdscr.refresh()


def _tui_main(stdscr):
    colors = _tui_colors(stdscr)
    state = {
        "period": "today",
        "sort": "total",
        "search": "",
        "buf": "",
        "searching": False,
        "scroll": 0,
        "rows": [],
        "up": 0,
        "dn": 0,
        "meta": {},
        "ls": {},
        "err": None,
        "colors": colors,
        "tick": 0,
        "loaded": False,
    }
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.timeout(1000)

    while True:
        if state["tick"] % 5 == 0 or not state["loaded"]:
            try:
                rows, up, dn = query_rows(state["period"])
                if state["search"]:
                    needle = state["search"].lower()
                    rows = [
                        r for r in rows
                        if needle in r[0].lower() or needle in r[1].lower()
                    ]
                state["rows"] = _tui_sorted(rows, state["sort"])
                state["up"], state["dn"] = up, dn
                state["meta"] = load_meta(connect_db())
                state["ls"] = _tui_lightsail()
                state["err"] = None
                state["loaded"] = True
            except Exception as exc:
                state["err"] = str(exc)

        _tui_draw(stdscr, state)
        c = stdscr.getch()
        state["tick"] += 1
        if c == -1:
            continue
        if c == curses.KEY_RESIZE:
            continue

        if state["searching"]:
            if c in (10, 13, getattr(curses, "KEY_ENTER", 10)):
                state["search"] = state["buf"]
                state["searching"] = False
                state["scroll"] = 0
                state["loaded"] = False
            elif c in (27,):
                state["buf"] = state["search"]
                state["searching"] = False
            elif c in (8, 127, getattr(curses, "KEY_BACKSPACE", 8)):
                state["buf"] = state["buf"][:-1]
            elif 32 <= c < 127:
                state["buf"] += chr(c)
            continue

        if c in (ord("q"), ord("Q"), 27):
            break
        if c == ord("/"):
            state["searching"] = True
            state["buf"] = ""
        elif c in (ord("r"), ord("R")):
            state["tick"] = 0
            state["loaded"] = False
        elif c == ord("t"):
            state["period"] = "today"
            state["scroll"] = 0
            state["loaded"] = False
        elif c == ord("m"):
            state["period"] = "month"
            state["scroll"] = 0
            state["loaded"] = False
        elif c == ord("a"):
            state["period"] = "all"
            state["scroll"] = 0
            state["loaded"] = False
        elif c == ord("s"):
            names = [n for n, _ in TUI_SORTS]
            state["sort"] = names[(names.index(state["sort"]) + 1) % len(names)]
            state["loaded"] = False
        elif c in (curses.KEY_LEFT, curses.KEY_RIGHT):
            names = [n for n, _ in TUI_PERIODS]
            idx = names.index(state["period"])
            idx = (idx + 1) % len(names) if c == curses.KEY_RIGHT else (idx - 1) % len(names)
            state["period"] = names[idx]
            state["scroll"] = 0
            state["loaded"] = False
        elif c == curses.KEY_UP:
            state["scroll"] = max(0, state["scroll"] - 1)
        elif c == curses.KEY_DOWN:
            state["scroll"] += 1
        elif c == curses.KEY_PPAGE:
            state["scroll"] = max(0, state["scroll"] - 10)
        elif c == curses.KEY_NPAGE:
            state["scroll"] += 10
        elif c == curses.KEY_HOME:
            state["scroll"] = 0
        elif c == curses.KEY_END:
            state["scroll"] = 10**9


def cmd_tui():
    import curses
    import locale

    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        try:
            locale.setlocale(locale.LC_ALL, "C.UTF-8")
        except locale.Error:
            pass
    if os.environ.get("TERM", "").lower() in ("", "dumb"):
        os.environ["TERM"] = "xterm-256color"
    try:
        curses.wrapper(_tui_main)
    except curses.error as exc:
        print(f"error: 终端初始化失败（{exc}），请在支持 ANSI 的终端中运行", file=sys.stderr)
        sys.exit(1)



def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="按域名记录 Hysteria 流量")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("collect")
    sub.add_parser("tui")
    status_p = sub.add_parser("status")
    status_p.add_argument("--json", action="store_true")
    report_p = sub.add_parser("report")
    report_p.add_argument("period", nargs="?", default="today")
    report_p.add_argument("--json", action="store_true")
    prune_p = sub.add_parser("prune")
    prune_p.add_argument("--days", type=int, default=180)
    args = parser.parse_args()

    if args.cmd == "collect":
        cmd_collect()
    elif args.cmd == "tui":
        cmd_tui()
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "report":
        cmd_report(args.period, args.json)
    elif args.cmd == "prune":
        cmd_prune(args.days)
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
