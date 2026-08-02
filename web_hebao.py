#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
北京移动权益超市 - 和包自动领券 · Web 版
=========================================
在原有「协议版」脚本基础上，增加：
  * 一个本地 Web 控制台（实时状态 / 日志）
  * 一个对外 Webhook 地址：你把收到的短信原文 POST 到这里
  * 程序自动从短信里识别 6 位验证码 -> 自动登录 -> 自动领取所有面额

依赖:
  pip install pycryptodome        （AES 加密，原脚本就用它）
  node  (用于发起 HTTP 请求，原脚本就用它)

启动:
  python web_hebao.py
  PORT=8080 python web_hebao.py     # 自定义端口

使用流程:
  1. 打开 http://<本机IP>:8000
  2. 填手机号，点「开始领取」-> 程序发送登录短信
  3. 在手机上安装短信转发 App（如「短信转发器」「Notify」等），
     把收到的短信转发到页面上显示的 Webhook 地址（POST）。
     因为短信是按顺序收到的，程序用队列按序消费：
       第 1 条短信 = 登录验证码
       之后每条短信 = 对应面额的领取验证码
  4. 页面实时显示登录 / 领取进度。
  也支持在页面手动填入验证码作为兜底。
"""

import os
import sys
import json
import time
import re
import socket
import threading
import queue
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _ensure_crypto():
    """尽量保证 Crypto 可导入。

    当用非 venv 的 Python 直接启动本脚本时，Crypto 可能不在 sys.path 里。
    这里自动把「受管 venv」的 site-packages 加进来，避免依赖缺失报错。
    """
    try:
        import Crypto  # noqa: F401
        return
    except Exception:
        pass
    ver = "%d.%d" % sys.version_info[:2]
    exe_dir = os.path.dirname(sys.executable)
    candidates = []
    # 沿目录树向上最多 6 层，寻找 envs/default 的 site-packages
    cur = exe_dir
    for _ in range(6):
        for tail in (["envs", "default", "Lib", "site-packages"],
                     ["envs", "default", "lib", "python" + ver, "site-packages"]):
            candidates.append(os.path.normpath(os.path.join(cur, *tail)))
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    # 解释器自身的 site-packages
    for base in (getattr(sys, "base_prefix", ""), getattr(sys, "prefix", "")):
        if not base:
            continue
        for tail in (["Lib", "site-packages"],
                     ["lib", "python" + ver, "site-packages"]):
            candidates.append(os.path.normpath(os.path.join(base, *tail)))
    # VIRTUAL_ENV 环境变量
    ve = os.environ.get("VIRTUAL_ENV", "")
    if ve:
        for tail in (["Lib", "site-packages"], ["lib", "python" + ver, "site-packages"]):
            candidates.append(os.path.normpath(os.path.join(ve, *tail)))
    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        if os.path.isdir(c) and c not in sys.path:
            sys.path.insert(0, c)
    try:
        import Crypto  # noqa: F401
    except Exception:
        pass


_ensure_crypto()

# ----------------------------------------------------------------------------
# 1. 导入原脚本的核心逻辑（加密 / 签名 / API 调用）
# ----------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
CORE_FILE = os.path.join(HERE, "北京自动和包.py")

core = None
CORE_ERROR = ""
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("hebao_core", CORE_FILE)
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
except Exception as e:  # 缺依赖 / 文件缺失时仍能启动 Web 服务并提示
    core = None
    CORE_ERROR = f"无法加载核心脚本: {e}"

SESSION_FILE = os.path.join(HERE, "web_claim_state.json")        # 旧版单会话文件(兼容迁移)
ACCOUNTS_FILE = os.path.join(HERE, "accounts.json")             # 多账号 + 定时配置
CONFIG_FILE = os.path.join(HERE, "web_hebao_config.json")
PORT = int(os.environ.get("PORT", "8000"))


def get_lan_ip():
    """获取本机对外（局域网）IP，供手机等外接设备访问 Webhook 使用。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 不真正发包，仅借路由表拿到出网网卡的 IP
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


# ----------------------------------------------------------------------------
# 自定义 Webhook 地址（预留）：配置后优先使用；未配置则回退到自带地址。
# 优先级：环境变量 WEBHOOK_URL > PUBLIC_URL > 配置文件 webhook_url。
# ----------------------------------------------------------------------------
def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            return cfg
    except Exception:
        pass
    return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


CONFIG = load_config()


def resolve_custom_webhook():
    """返回用户自定义的 webhook 地址（去尾斜杠）；未设置返回 ''。
    优先级：WEBHOOK_URL 环境变量 > PUBLIC_URL 环境变量 > 配置文件 webhook_url。"""
    for src in (os.environ.get("WEBHOOK_URL"),
                os.environ.get("PUBLIC_URL"),
                CONFIG.get("webhook_url")):
        v = (src or "").strip().rstrip("/")
        if v:
            return v
    return ""


def build_webhook_url():
    """Webhook 对外地址：有自定义则用之，否则用本机局域网 IP（自带地址）。
    约定：外接短信转发 POST 到 '/api/hooks'（用户约定的 JSON 地址），
    '/' 与 '/webhook/sms' 为兼容别名。
    自定义地址可能填「完整地址(含 /api/hooks)」或「仅域名/根」：
    前者直接作为地址，后者自动补 /api/hooks。"""
    custom = resolve_custom_webhook()
    if custom:
        c = custom.rstrip("/")
        if re.search(r"(/api/hooks|/webhook/sms)$", c):
            return c + "/"
        return c + "/api/hooks"
    return f"http://{get_lan_ip()}:{PORT}/api/hooks"

# ----------------------------------------------------------------------------
# 2. 共享状态 & 日志
# ----------------------------------------------------------------------------
web_state = {
    "running": False,
    "logged_in": False,
    "phone": "",
    "sessKey": "",
    "step": "空闲",
    "logs": [],
    "started_at": None,
    "webhookUrl": "",
    "customWebhook": "",
    "webhookSource": "builtin",
    "lastCode": "",
    "lastCodeAt": None,
    "lastCodeTime": "",
    "usedCodes": [],
}
code_queue = queue.Queue()          # 识别到的验证码队列（FIFO，按短信到达顺序）
stop_ev = threading.Event()          # 用于「停止」时唤醒等待中的线程
_lock = threading.Lock()


# ----------------------------------------------------------------------------
# 多账号存储 + 定时配置（accounts.json）
# ----------------------------------------------------------------------------
accounts = []          # [{"phone","sessKey","logged_in","last_login","label","level"}]
schedule_cfg = {       # 定时自动运行配置
    "enabled": False,
    "mode": "daily",           # "daily" | "interval"
    "time": "08:00",           # daily 模式的每日执行时间 HH:MM
    "interval_min": 360,       # interval 模式：每 N 分钟执行一次
    "phones": [],              # 定时运行哪些账号
    "selected": [],            # 定时运行哪些商品(空=全部)
    "repeat": 1,
    "interval_sec": 60,
    "last_run": None,          # 上次触发时间戳(运行时记录，不持久化也行)
}


def load_accounts():
    """从 accounts.json 加载多账号 + 定时配置；兼容旧版单会话文件迁移。"""
    global accounts, schedule_cfg
    data = {}
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    accounts = data.get("accounts", []) if isinstance(data, dict) else []
    if isinstance(data, dict) and isinstance(data.get("schedule"), dict):
        schedule_cfg.update(data["schedule"])
    # 兼容旧版单会话文件
    if not accounts and os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("sessKey") and d.get("phone"):
                accounts = [{
                    "phone": d["phone"], "sessKey": d["sessKey"],
                    "logged_in": True, "last_login": d.get("time", 0),
                    "label": "", "level": "",
                }]
        except Exception:
            pass


def save_accounts():
    try:
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump({"accounts": accounts, "schedule": schedule_cfg},
                      f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log(f"保存账号数据失败: {e}")
        return False


def get_account(phone):
    for a in accounts:
        if a.get("phone") == phone:
            return a
    return None


def upsert_account(phone, sess_key, login_res=None):
    """新增或更新账号会话；login_res 为 core.login_with_sms 的返回(含 memberInfo)。"""
    a = get_account(phone)
    lv = ""
    if login_res and isinstance(login_res, dict):
        lv = login_res.get("memberInfo", {}).get("lvName", "")
    if a:
        a["sessKey"] = sess_key
        a["logged_in"] = bool(sess_key)
        a["last_login"] = int(time.time())
        if lv:
            a["level"] = lv
    else:
        accounts.append({
            "phone": phone, "sessKey": sess_key,
            "logged_in": bool(sess_key), "last_login": int(time.time()),
            "label": "", "level": lv,
        })
    save_accounts()


def delete_account(phone):
    global accounts
    accounts = [a for a in accounts if a.get("phone") != phone]
    save_accounts()


def clear_codes():
    """丢弃队列中所有已缓存的验证码，防止旧短信/旧测试污染新流程。"""
    n = 0
    while True:
        try:
            code_queue.get_nowait()
            n += 1
        except queue.Empty:
            break
    if n:
        log(f"已清空 {n} 条过期验证码缓存")


def log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _lock:
        web_state["logs"].append(line)
        if len(web_state["logs"]) > 400:
            web_state["logs"] = web_state["logs"][-400:]
    print(line, flush=True)


def set_step(s):
    with _lock:
        web_state["step"] = s


def save_session():
    """保存当前 phone 的会话到多账号存储。"""
    if web_state.get("phone") and web_state.get("sessKey"):
        upsert_account(web_state["phone"], web_state["sessKey"])


def load_session():
    """加载多账号数据；若存在已登录账号，把第一个设为当前会话。"""
    load_accounts()
    for a in accounts:
        if a.get("sessKey"):
            web_state["sessKey"] = a["sessKey"]
            web_state["phone"] = a["phone"]
            web_state["logged_in"] = True
            return True
    return False


# load_accounts 在模块加载时由 load_session 触发；此处先加载一次保证 accounts 可用
load_accounts()


# ----------------------------------------------------------------------------
# 3. 核心业务（复用原脚本的 call_api / send_sms / login_with_sms）
# ----------------------------------------------------------------------------
def require_core():
    if core is None:
        raise RuntimeError(CORE_ERROR or "核心脚本未加载")


def send_claim_sms(sess_key, phone, amount):
    """领取前发送验证码 (BIZ_CONFIRM_SMS_SEND)"""
    require_core()
    prod = core.PRODUCTS[amount]
    return core.call_api("BIZ_CONFIRM_SMS_SEND",
                          {"rightsCode": prod["rightsCode"]},
                          sess_key, phone)


def do_claim(sess_key, phone, amount, code):
    """用验证码领取 (INNER_OPEN_TEL_FEE_AUTH)，返回 (是否成功, API原始响应)。

    成功判定：API 返回体含 `custOrderId`（生成了订单号）即视为成功。
    旧约定"返回 None=成功"是错的——实测成功会返回 {'custOrderId':'OPEN_AUTH...'}，
    而 None 反而多为网络/会话异常。错误码(验证码错误/已领过/频率限制)返回带错误信息的 dict（无 custOrderId）。"""
    require_core()
    prod = core.PRODUCTS[amount]
    price = str(int(amount) * 100)  # 元 -> 分
    data = {
        "accNbr": phone,
        "rightCode": prod["rightsCode"],
        "price": price,
        "memberLevelCode": None,
        "verifyCode": code,
    }
    if prod["offerId"]:
        data["offerId"] = prod["offerId"]
    r = core.call_api("INNER_OPEN_TEL_FEE_AUTH", data, sess_key, phone)
    ok = isinstance(r, dict) and "custOrderId" in r
    return ok, r


def _now_hms():
    return time.strftime("%H:%M:%S")


def _looks_like_vcode_sms(body):
    """判断一条短信是否像「验证码短信」，避免把「来电提醒」等里的日期(2026)误当验证码。"""
    if not body:
        return False
    return bool(re.search(r"(验证码|动态码|校验码|随机码|口令|短信密码|verification\s*code)", body, re.I))


def _is_freq(resp):
    """判断 API 响应是否表示「验证码发送频繁/操作过快」类限流。"""
    s = str(resp) if resp is not None else ""
    return bool(re.search(r"频繁|稍后|稍候|过于|过快|too many|too frequent|次数过多|操作过快|限制", s, re.I))


def _fetch_hook_list(poll_url):
    """GET 外部 Webhook 接收器暴露的列表接口，返回条目数组（最新在前）。
    不同平台返回结构可能不同，这里尽量兼容：裸数组 / {"data": [...]}。"""
    req = urllib.request.Request(poll_url, headers={"User-Agent": "hebao/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = r.read().decode("utf-8", "replace")
    arr = json.loads(data)
    if isinstance(arr, list):
        return arr
    if isinstance(arr, dict):
        for k in ("data", "list", "items", "messages", "records"):
            v = arr.get(k)
            if isinstance(v, list):
                return v
    return []


def _hms_to_sec(hms):
    """把 'HH:MM:SS' 转成当天秒数，便于数值比较（避免字符串字典序误判，如 49<52 实际是 49>52 的分钟秒比较陷阱）。"""
    try:
        h, m, s = hms.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except Exception:
        return 0


def _poll_once(poll_url, since, skip_code=None, digits=None, used_codes=None):
    """轮询一次外部 Webhook，返回最新一条「本次发送之后且未被用过」的验证码；没有则返回 None。

    - since: 本次发送短信的时刻(HH:MM:SS)，只认该时刻之后的短信（数值比较）。
    - skip_code: 上一次已消费的验证码（如登录码），绝不重复使用——这是防止「登录码被拿去领券」的核心保险。
    - digits: 指定期望位数（登录=6 / 兑换=4），只匹配该位数的码，杜绝位数混用读错。
    - used_codes: 本次运行已消费过的全部码集合，延迟重复投递的旧码也绝不复用。
    """
    try:
        arr = _fetch_hook_list(poll_url)
    except Exception:
        return None
    if not arr:
        return None
    since_sec = _hms_to_sec(since) if since else 0
    used = used_codes or set()
    for e in arr:  # 平台返回最新在前
        t = e.get("time", "") if isinstance(e, dict) else ""
        if since_sec and t and _hms_to_sec(t) < since_sec:
            continue  # 早于本次发送时刻，属于历史短信，跳过
        body = (e.get("body") or "") if isinstance(e, dict) else str(e)
        if not _looks_like_vcode_sms(body):
            continue
        c = extract_code(body, digits)
        if not c:
            continue
        if c in used:
            continue  # 本次运行已用过该码（含登录码、前几次兑换码、延迟重复投递的旧码），绝不复用
        if skip_code and c == skip_code:
            continue  # 双保险：上一步刚用过的码
        web_state["lastCodeTime"] = t  # 记录该码的平台时间，供下一步推进 since，杜绝读回旧码
        return c
    return None


def wait_for_code(timeout, poll_url=None, since=None, skip_code=None, digits=None):
    """等待验证码。

    - 优先取「手动/推送」进来的码（code_queue）
    - 若配置了外部 Webhook 地址，则主动轮询它的列表接口自动取码
    - since: 本次发送短信的时刻(HH:MM:SS)，只认该时刻之后的短信（数值比较）
    - skip_code: 上一次已消费的码，绝不重复使用（防止登录码被拿去领券）
    - digits: 期望位数（登录=6 / 兑换=4），只匹配该位数
    - 任何被消费的码都会记入 usedCodes，本次运行内绝不重复使用
    """
    end = time.time() + timeout
    polled_logged = False
    while True:
        remaining = end - time.time()
        if remaining <= 0 or stop_ev.is_set():
            return None
        # 1) 手动 / 推送进来的码优先（兜底）
        try:
            code = code_queue.get_nowait()
            web_state["lastCode"] = code
            web_state["lastCodeAt"] = int(time.time())
            if code not in web_state["usedCodes"]:
                web_state["usedCodes"].append(code)
            return code
        except queue.Empty:
            pass
        # 2) 主动轮询外部 Webhook 自动取码
        if poll_url:
            if not polled_logged:
                log(f"📡 已开启外部 Webhook 自动拉取: {poll_url}")
                polled_logged = True
            code = _poll_once(poll_url, since, skip_code=web_state.get("lastCode") or skip_code,
                              digits=digits, used_codes=set(web_state.get("usedCodes", [])))
            if code:
                web_state["lastCode"] = code
                web_state["lastCodeAt"] = int(time.time())
                if code not in web_state["usedCodes"]:
                    web_state["usedCodes"].append(code)
                log(f"📡 已从外部 Webhook 自动读取到验证码: {code}")
                return code
        stop_ev.wait(min(remaining, 3.0))


def claim_with_retry(sess_key, phone, amount, retries=3, poll_url=None):
    """返回 (是否成功, 最后一次API原始响应)。
    detail 为 None 通常意味着会话失效/网络异常(需重登)；为 dict(无custOrderId) 多为业务错误(验证码错/已领过/频率)。"""
    name = core.PRODUCTS[amount]["name"]
    last_detail = None
    for i in range(retries):
        set_step(f"领取 {amount}元 · {name} — 发送验证码(第{i+1}次)")
        try:
            sr = send_claim_sms(sess_key, phone, amount)
        except Exception as e:
            log(f"{name}: 发送验证码异常 {e}")
            sr = None
        # 发送失败(非None=业务错误，如频繁/限流)：不空等验证码，按情况延迟后重试发送
        if sr is not None:
            if _is_freq(sr):
                log(f"{name}: 验证码发送频繁，等待 120 秒后重试（API返回: {sr}）")
                stop_ev.wait(120)
                if stop_ev.is_set():
                    return False, last_detail
            else:
                log(f"{name}: 发送验证码失败（API返回: {sr}），重试")
            continue
        log(f"{name}: 已发送领取验证码" + ("（程序将自动从外部 Webhook 读取）" if poll_url else "，请通过 Webhook 传入短信"))
        clear_codes()                  # 只认本条短信之后到达的验证码
        set_step(f"领取 {amount}元 · {name} — 等待验证码")
        # since 推进到「上一条已消费码的平台时间」：即便上步的码仍残留在列表里也不会被读回，
        # 只认本次发送之后、且比上一条更新的码；兑换码严格 4 位，绝不读非 4 位。
        eff_since = web_state.get("lastCodeTime") or _now_hms()
        code = wait_for_code(300, poll_url=poll_url, since=eff_since, digits=4)
        if not code:
            log(f"{name}: 验证码等待超时")
            continue
        log(f"{name}: 识别到验证码 {code}，领取中…")
        stop_ev.wait(2)   # 给平台一点时间登记验证码，避免提交过快被拒（原脚本亦有此等待）
        if stop_ev.is_set():
            log(f"{name}: 已手动停止")
            return False, last_detail
        try:
            ok, detail = do_claim(sess_key, phone, amount, code)
        except Exception as e:
            log(f"{name}: 领取异常 {e}")
            ok, detail = False, str(e)
        last_detail = detail
        if ok:
            log(f"{name}: 领取成功，订单号 {detail.get('custOrderId') if isinstance(detail, dict) else ''}")
            return True, detail
        if detail is None:
            # None = 当日领取次数已达上限/不可领取(非验证码问题)：重试只会浪费短信验证码，直接交上层跳过
            log(f"{name}: 当日领取次数已达上限或不可领取（API返回空），不再重试")
            return False, None
        log(f"{name}: 领取失败（API返回: {detail}），自动重试")
    return False, last_detail


def login_flow(phone, poll_url):
    """执行登录流程(发短信→读6位码→登录)，成功返回 sessKey 并写入多账号存储；失败/停止返回 None。"""
    max_tries = 3
    for attempt in range(max_tries):
        if stop_ev.is_set():
            return None
        set_step(f"发送登录验证码(第{attempt+1}次)")
        try:
            sms_res = core.send_sms(phone)
        except Exception as e:
            log(f"发送登录短信异常: {e}")
            return None
        if not sms_res or "成功" not in str(sms_res):
            # 发送频繁限流：延迟 120 秒再重试，避免连续触发更严限流
            if sms_res and _is_freq(sms_res):
                log("⚠️ 登录验证码发送频繁，等待 120 秒后重试")
                stop_ev.wait(120)
                if stop_ev.is_set():
                    return None
                continue
            log(f"❌ 发送登录短信失败（API返回: {sms_res}），将重新发送")
            continue
        log("✅ 已发送登录验证码，请查收手机短信" + ("（程序将自动从外部 Webhook 读取，无需手动粘贴）" if poll_url else "，请将短信转发到 Webhook 地址（10 分钟内有效）"))
        clear_codes()              # 只认本条短信之后到达的验证码
        set_step(f"等待登录验证码(第{attempt+1}次)")
        code = wait_for_code(600, poll_url=poll_url, since=_now_hms(), digits=6)  # 登录码严格 6 位
        if not code:
            if stop_ev.is_set():
                return None
            log("登录验证码等待超时，将重新发送")
            continue
        log(f"识别到登录验证码: {code}")
        set_step("登录中")
        try:
            res = core.login_with_sms(phone, code)
        except Exception as e:
            log(f"登录异常: {e}")
            res = None
        if res:
            sk = res.get("sessKey", "")
            web_state["sessKey"] = sk
            web_state["phone"] = res.get("phoneNo", phone)
            web_state["logged_in"] = True
            upsert_account(phone, sk, res)
            lv = res.get("memberInfo", {}).get("lvName", "")
            log(f"登录成功，手机号 {web_state['phone']}" + (f"，等级 {lv}" if lv else ""))
            return sk
        log("登录失败（验证码可能错误），将重新发送")
    return None


def run_flow(phone, skip_login, selected=None, repeat=1, interval=60):
    web_state["running"] = True
    web_state["phone"] = phone
    web_state["started_at"] = int(time.time())
    stop_ev.clear()
    clear_codes()                     # 丢弃任何历史残留验证码
    web_state["lastCode"] = ""        # 每次运行干净起步，避免上次残留码污染本次 since/skip_code
    web_state["lastCodeTime"] = ""
    web_state["usedCodes"] = []       # 清空本次运行已用码集合，杜绝延迟重复投递的旧码被复用
    # 配置了外部 Webhook 地址时，开启「主动轮询拉取」模式（无需手动粘短信）
    poll_url = web_state.get("customWebhook") or ""
    if not poll_url:
        poll_url = None
    relogin_done = False
    try:
        # 取该账号已保存会话；有则复用，无则走登录流程
        acct = get_account(phone) or {}
        saved_sess = acct.get("sessKey", "") if acct else ""
        if skip_login and saved_sess:
            web_state["sessKey"] = saved_sess
            log(f"复用已保存会话，跳过登录（账号 {phone}）")
        else:
            sk = login_flow(phone, poll_url)
            if not sk:
                log("登录失败，流程终止")
                return finish()

        # 领取所选面额（支持多选 + 每个重复次数 + 兑换间隔）
        sess = web_state["sessKey"]
        ph = web_state["phone"] or phone
        amounts = sorted(core.PRODUCTS.keys(), key=int)
        if selected:
            sel = [a for a in amounts if str(a) in {str(x) for x in selected}]
            if not sel:
                log("⚠️ 所选商品均不存在，改为领取全部")
                sel = amounts
        else:
            sel = amounts
        repeat_n = max(1, int(repeat))
        total = len(sel) * repeat_n
        done = 0
        first = True
        consec_none = 0   # 连续「None 失败」计数：≥2 多为会话失效(单商品上限不会连续多个)，自动重登一次
        for amount in sel:
            name = core.PRODUCTS[amount]["name"]
            for _ in range(repeat_n):
                if stop_ev.is_set():
                    log("已手动停止，终止领取")
                    return finish()
                if not first:
                    log(f"⏳ 兑换间隔等待 {interval} 秒（平台要求间隔）…")
                    stop_ev.wait(interval)   # 可被「停止」打断
                    if stop_ev.is_set():
                        log("已手动停止，终止领取")
                        return finish()
                first = False
                ok, detail = claim_with_retry(sess, ph, amount, poll_url=poll_url)
                done += 1
                if ok:
                    consec_none = 0
                    log(f"✅ {amount}元 · {name} 领取成功（{done}/{total}）")
                elif detail is None:
                    # None = 当日次数上限/不可领取：直接跳过该商品，不再获取验证码
                    consec_none += 1
                    if consec_none >= 2 and not relogin_done and not stop_ev.is_set():
                        # 连续多个商品都返回空 → 疑似会话过期(而非单商品上限)，自动重登一次再试本商品
                        log("⚠️ 连续多个商品领取返回空，疑似会话失效，自动重新登录…")
                        new_sess = login_flow(phone, poll_url)
                        if new_sess:
                            sess = new_sess
                            relogin_done = True
                            consec_none = 0
                            ok, detail = claim_with_retry(sess, ph, amount, poll_url=poll_url)
                            log(f"{'✅' if ok else '❌'} {amount}元 · {name} 重登后{'领取成功' if ok else '仍失败'}（{done}/{total}）")
                        else:
                            log(f"⏭️ {amount}元 · {name} 当日领取次数已达上限，跳过（{done}/{total}）")
                    else:
                        log(f"⏭️ {amount}元 · {name} 当日领取次数已达上限，跳过（{done}/{total}）")
                else:
                    consec_none = 0
                    log(f"❌ {amount}元 · {name} 领取失败（API返回: {detail}）（{done}/{total}）")
        set_step("完成")
        log("全部面额处理完毕")
    except Exception as e:
        log(f"流程异常: {e}")
    finally:
        web_state["running"] = False


def finish():
    web_state["running"] = False
    set_step("已停止")


# ----------------------------------------------------------------------------
# 4. 从短信文本识别验证码
# ----------------------------------------------------------------------------
def extract_code(text, digits=None):
    if not text:
        return None
    # 必须带「验证码」类关键词，再取其后紧跟的数字。
    # 这样能避免把「来电提醒」短信里的日期(如 2026)或手机号误当成验证码。
    # digits: 指定位数时（登录=6 / 兑换=4）只匹配「恰好该位数」的码，且前后都不是数字，
    #         杜绝从更长的数字里截取前 N 位（如 6 位登录码被当 4 位截断）。
    kw = r"(?:验证码|动态码|校验码|随机码|口令|短信密码|verification\s*code|code)"
    if digits:
        pat = kw + r"[^\d]{0,8}?(?<!\d)(\d{" + str(digits) + r"})(?!\d)"
    else:
        pat = kw + r"[^\d]{0,8}?(\d{4,8})(?!\d)"
    m = re.search(pat, text, re.I)
    if m:
        return m.group(1)
    return None


def _collect_strings(o, depth=0, out=None):
    """递归收集 JSON 中的所有字符串值（限深 3 层），用于适配未知的 webhook 报文结构。"""
    if out is None:
        out = []
    if depth > 3:
        return out
    if isinstance(o, str):
        out.append(o)
    elif isinstance(o, dict):
        for v in o.values():
            _collect_strings(v, depth + 1, out)
    elif isinstance(o, list):
        for v in o:
            _collect_strings(v, depth + 1, out)
    return out


def push_sms(raw_text, query):
    """处理 Webhook 进来的短信，返回 (code_or_None, note)。
    兼容多种 JSON 报文结构：常见字段 / 任意嵌套字符串字段 / 纯文本 / ?code= 直传。"""
    code = query.get("code")
    candidates = []
    if raw_text:
        candidates.append(raw_text)
        try:
            obj = json.loads(raw_text)
            if isinstance(obj, dict):
                for k in ("text", "sms", "content", "body", "message", "msg", "data", "content_text"):
                    if obj.get(k):
                        candidates.append(str(obj[k]))
                # 兜底：遍历任意嵌套字符串字段
                candidates.extend(_collect_strings(obj))
        except Exception:
            pass
    if not code:
        for c in candidates:
            found = extract_code(c)
            if found:
                code = found
                break
    if code:
        code_queue.put(code)
        web_state["lastCode"] = code
        web_state["lastCodeAt"] = int(time.time())
        log(f"📩 收到短信，识别出验证码: {code}")
        return code, "ok"
    snippet = (raw_text or "")[:80].replace("\n", " ")
    log("📩 收到短信，但未识别到验证码（原始: " + snippet + "）")
    return None, "no code"


# ----------------------------------------------------------------------------
# 5. HTTP 服务
# ----------------------------------------------------------------------------
PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>和包自动领券 · Web 控制台</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
         background:#f4f6fb; color:#1f2937; }
  .wrap { max-width:880px; margin:0 auto; padding:24px 16px 60px; }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub { color:#6b7280; font-size:13px; margin-bottom:20px; }
  .card { background:#fff; border:1px solid #e5e7eb; border-radius:14px; padding:18px 20px; margin-bottom:16px;
          box-shadow:0 1px 3px rgba(0,0,0,.04); }
  label { display:block; font-size:13px; color:#374151; margin-bottom:6px; font-weight:600; }
  input[type=text] { width:100%; padding:10px 12px; border:1px solid #d1d5db; border-radius:9px; font-size:15px; outline:none; }
  input[type=text]:focus { border-color:#2563eb; box-shadow:0 0 0 3px rgba(37,99,235,.15); }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  button { border:none; border-radius:9px; padding:10px 18px; font-size:14px; font-weight:600; cursor:pointer; }
  .btn-primary { background:#2563eb; color:#fff; }
  .btn-primary:hover { background:#1d4ed8; }
  .btn-primary:disabled { background:#9ca3af; cursor:not-allowed; }
  .btn-ghost { background:#eef2ff; color:#3730a3; }
  .btn-ghost:hover { background:#e0e7ff; }
  .chk { display:flex; align-items:center; gap:6px; font-size:13px; color:#374151; }
  .webhook { background:#0f172a; color:#e2e8f0; border-radius:9px; padding:12px 14px; font-family:ui-monospace,Menlo,Consolas,monospace;
             font-size:13px; word-break:break-all; display:flex; justify-content:space-between; align-items:center; gap:10px; }
  .copy { background:#334155; color:#fff; border:none; border-radius:6px; padding:6px 10px; font-size:12px; cursor:pointer; white-space:nowrap; }
  .copy:hover { background:#475569; }
  .tag { font-size:11px; color:#0ea5e9; background:rgba(14,165,233,.12); border:1px solid rgba(14,165,233,.35);
         border-radius:6px; padding:2px 8px; white-space:nowrap; }
  .tag.builtin { color:#94a3b8; background:rgba(148,163,184,.12); border-color:rgba(148,163,184,.3); }
  .status { display:flex; align-items:center; gap:10px; }
  .dot { width:10px; height:10px; border-radius:50%; background:#9ca3af; }
  .dot.run { background:#22c55e; animation:pulse 1s infinite; }
  .dot.idle { background:#9ca3af; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
  .step { font-size:15px; font-weight:600; }
  .meta { font-size:12px; color:#6b7280; margin-top:2px; }
  pre.log { background:#0b1220; color:#cbd5e1; border-radius:10px; padding:14px; height:300px; overflow:auto;
            font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12.5px; line-height:1.6; margin:0; white-space:pre-wrap; }
  .hint { font-size:12px; color:#6b7280; line-height:1.7; }
  .badge { display:inline-block; background:#dcfce7; color:#166534; border-radius:6px; padding:2px 8px; font-size:12px; font-weight:600; }
  .err { background:#fef2f2; color:#b91c1c; border:1px solid #fecaca; border-radius:9px; padding:10px 12px; font-size:13px; margin-bottom:16px; }
  .prod-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(200px,1fr)); gap:8px 14px; }
  .prod-item { display:flex; align-items:center; gap:8px; font-size:14px; background:#f8fafc; border:1px solid #e5e7eb; border-radius:9px; padding:8px 10px; cursor:pointer; }
  .prod-item input { width:16px; height:16px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>和包自动领券 · Web 控制台</h1>
  <div class="sub">短信通过 Webhook 传入 → 自动识别验证码 → 自动登录 → 自动领取</div>

  <div id="coreErr"></div>

  <div class="card">
    <label>Webhook 地址（把收到的短信 POST 到这里）</label>
    <div class="webhook">
      <span id="whUrl">加载中…</span>
      <span id="whTag" class="tag">自带</span>
      <button class="copy" onclick="copyWh()">复制</button>
    </div>
    <div class="hint" style="margin-top:8px">
      <span id="pollBadge" class="tag" style="background:#16a34a">自动拉取：未知</span>
      配置了外部 Webhook 地址时，程序会<b>主动轮询</b>该地址的列表接口自动读取验证码（无需手动粘贴）；未配置则仅被动接收推送。
    </div>
    <div class="hint" style="margin-top:8px">
      上面是<b>当前生效地址</b>：未配置自定义时为本机局域网 IP（自带地址），配置后显示你自定义的地址。<br>
      请填到手机短信转发 App，方法 POST（默认 <code>/api/hooks</code>，兼容 <code>/</code> 与 <code>/webhook/sms</code>）。<br>
      支持：JSON <code>{"text":"短信内容"}</code> / 表单 / 纯文本；也可直接 <code>?code=123456</code> 传码。<br>
      短信按接收顺序自动排队：第 1 条=登录码，之后每条=对应面额领取码。
    </div>
  </div>

  <div class="card">
    <label>自定义 Webhook 地址（预留，可选）</label>
    <div class="row">
      <input type="text" id="customWh" placeholder="留空则使用自带地址（局域网 / PUBLIC_URL）" style="flex:1; min-width:220px">
      <button class="btn-ghost" onclick="saveWebhook()">保存并生效</button>
    </div>
    <div class="hint" style="margin-top:8px">
      填入即优先使用该地址（如你的公网隧道 <code>https://xxx.online</code>）；留空保存则回退到<b>自带地址</b>。保存后即时生效，无需重启。
    </div>
  </div>

  <div class="card">
    <label>账号管理（多账号保存，登录信息自动留存）</label>
    <div class="row" style="margin-bottom:10px">
      <input type="text" id="newAcctPhone" placeholder="新账号手机号" inputmode="numeric" style="flex:1; min-width:160px">
      <input type="text" id="newAcctLabel" placeholder="备注(可选)" style="flex:1; min-width:120px">
      <button class="btn-ghost" onclick="addAccount()">添加账号</button>
    </div>
    <div id="acctList" style="display:flex; flex-direction:column; gap:6px"></div>
    <div class="hint" style="margin-top:8px">首次登录后会自动保存会话；会话过期时程序会自动重发验证码重新登录并更新保存。</div>
  </div>

  <div class="card">
    <div class="row" style="margin-bottom:12px">
      <div style="flex:1; min-width:200px">
        <label>选择账号 / 手机号</label>
        <select id="acctSel" onchange="onAcctSel()" style="width:100%; padding:9px; border:1px solid #d1d5db; border-radius:9px; font-size:14px"></select>
      </div>
      <div style="flex:1; min-width:160px">
        <label>或手动输入手机号</label>
        <input type="text" id="phone" placeholder="11 位手机号" inputmode="numeric">
      </div>
      <label class="chk" style="align-self:flex-end; margin-bottom:10px">
        <input type="checkbox" id="skipLogin"> 复用会话
      </label>
    </div>
    <div class="row">
      <button class="btn-primary" id="startBtn" onclick="start()">开始领取</button>
      <button class="btn-ghost" id="stopBtn" onclick="stopFlow()" disabled>停止</button>
    </div>
  </div>

  <div class="card">
    <label>选择要领取的商品（可单选 / 多选；不选 = 全部）</label>
    <div class="row" style="margin-bottom:10px">
      <button class="btn-ghost" onclick="selAll(true)">全选</button>
      <button class="btn-ghost" onclick="selAll(false)">全不选</button>
      <span style="align-self:center; font-size:13px; color:#6b7280">已选 <b id="selCount">0</b> 项</span>
    </div>
    <div id="productList" class="prod-grid"><span style="color:#9ca3af; font-size:13px">加载商品中…</span></div>
    <div class="row" style="margin-top:12px">
      <div style="flex:1; min-width:160px">
        <label>每个商品重复次数</label>
        <input type="number" id="repeatN" min="1" max="20" value="1" style="width:100%">
      </div>
      <div style="flex:1; min-width:160px">
        <label>兑换间隔(秒)</label>
        <input type="number" id="intervalS" min="0" max="600" value="60" style="width:100%">
      </div>
    </div>
    <div class="hint" style="margin-top:8px">平台要求每个商品兑换间隔约 1 分钟，默认填 60 秒；间隔会在每次兑换前等待（可被「停止」打断）。</div>
  </div>

  <div class="card">
    <label>定时自动运行</label>
    <div class="row" style="margin-bottom:10px; align-items:center">
      <label class="chk"><input type="checkbox" id="schedEnable"> 启用定时</label>
      <select id="schedMode" style="padding:7px; border:1px solid #d1d5db; border-radius:8px; font-size:13px">
        <option value="daily">每日定时</option>
        <option value="interval">每隔N分钟</option>
      </select>
      <input type="text" id="schedTime" placeholder="08:00" style="width:90px; padding:7px; border:1px solid #d1d5db; border-radius:8px">
      <input type="number" id="schedInterval" placeholder="分钟" min="1" style="width:90px; padding:7px; border:1px solid #d1d5db; border-radius:8px">
      <button class="btn-ghost" onclick="saveSchedule()">保存定时</button>
    </div>
    <div class="hint" style="margin-bottom:8px">每日定时填 HH:MM（如 08:00）；每隔模式填分钟数。下方勾选要定时运行的账号与商品。</div>
    <label style="font-size:13px; color:#374151">定时账号</label>
    <div id="schedAcctList" class="prod-grid" style="margin-bottom:10px"></div>
    <label style="font-size:13px; color:#374151">定时商品（不选=全部）</label>
    <div id="schedProdList" class="prod-grid"></div>
    <div class="row" style="margin-top:10px">
      <div style="flex:1; min-width:140px"><label>重复次数</label><input type="number" id="schedRepeat" min="1" max="20" value="1" style="width:100%"></div>
      <div style="flex:1; min-width:140px"><label>兑换间隔(秒)</label><input type="number" id="schedIntervalSec" min="0" max="600" value="60" style="width:100%"></div>
    </div>
    <div class="hint" style="margin-top:8px">定时触发时会依次处理所选账号（复用已保存会话，过期自动重登）。<span id="schedStatus"></span></div>
  </div>

  <div class="card">
    <div class="status">
      <span class="dot idle" id="dot"></span>
      <div>
        <div class="step" id="step">空闲</div>
        <div class="meta" id="meta"></div>
      </div>
      <span class="badge" id="runBadge" style="margin-left:auto; display:none">运行中</span>
    </div>
  </div>

  <div class="card">
    <label>运行日志</label>
    <pre class="log" id="log">（暂无日志）</pre>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let whUrl = window.location.origin + "/api/hooks";  // 先用浏览器地址兜底，状态返回后替换为外接地址
let web_state_running = false;   // 与后端 running 同步，用于 start 按钮兜底恢复
$("whUrl").textContent = whUrl;

function copyWh(){
  navigator.clipboard.writeText(whUrl).then(()=>{
    const b=document.querySelector(".webhook .copy"); const t=b.textContent;
    b.textContent="已复制"; setTimeout(()=>b.textContent=t,1200);
  });
}

async function saveWebhook(){
  const v = $("customWh").value.trim();
  const lv = v.toLowerCase();
  if(v && !(lv.startsWith("http://") || lv.startsWith("https://"))){ alert("地址需以 http:// 或 https:// 开头"); return; }
  const r = await fetch("/api/config",{
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({webhook_url: v})
  }).then(r=>r.json()).catch(()=>({ok:false}));
  if(r.ok){ alert(r.msg + "\\n当前生效地址：\\n" + r.webhookUrl); }
  else { alert("保存失败"); }
}

async function start(){
  // 优先用账号下拉的选中值，否则用手动输入
  let phone = $("phone").value.trim();
  const selAcct = $("acctSel");
  if(!phone && selAcct && selAcct.value){ phone = selAcct.value; $("phone").value = phone; }
  if(!/^\\d{11}$/.test(phone)){ alert("请选择或输入 11 位手机号"); return; }
  const skip = $("skipLogin").checked;
  // 收集勾选的商品
  const sel = [];
  document.querySelectorAll("#productList input[type=checkbox]:checked").forEach(c=>{ if(c.value) sel.push(c.value); });
  const repeat = parseInt($("repeatN").value||"1",10) || 1;
  const interval = parseInt($("intervalS").value||"60",10) || 60;
  const btn = $("startBtn");
  btn.disabled = true; btn.textContent = "启动中…";
  try{
    const resp = await fetch("/api/start",{
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({phone, skipLogin:skip, selected:sel, repeat, interval})
    });
    const j = await resp.json().catch(()=>({ok:false, msg:"服务器返回异常（非JSON）"}));
    if(j.ok){ $("phone").disabled=true; $("stopBtn").disabled=false; }
    else { alert(j.msg||"启动失败"); }
  }catch(e){
    alert("请求失败: " + e.message + "。请确认本页面地址与服务端口一致，并已硬刷新(Ctrl+F5)");
  }finally{
    // 若任务未真正启动（失败/异常），恢复按钮；refresh 也会按 running 状态接管
    if(!web_state_running){ btn.disabled = false; btn.textContent = "开始领取"; }
  }
}

// ---- 账号管理 ----
async function addAccount(){
  const phone = $("newAcctPhone").value.trim();
  const label = $("newAcctLabel").value.trim();
  if(!/^\\d{11}$/.test(phone)){ alert("请输入 11 位手机号"); return; }
  await fetch("/api/accounts",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({action:"add", phone, label})});
  $("newAcctPhone").value=""; $("newAcctLabel").value="";
}
async function deleteAccount(phone){
  if(!confirm("删除账号 "+phone+" ？保存的登录信息也会清除。")) return;
  await fetch("/api/accounts",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({action:"delete", phone})});
}
function onAcctSel(){
  const v = $("acctSel").value;
  if(v){ $("phone").value = v; $("skipLogin").checked = true; }
}
function renderAccounts(accts){
  // 下拉
  const sel = $("acctSel");
  const cur = sel.value;
  sel.innerHTML = '<option value="">— 选择已保存账号 —</option>' +
    accts.map(a=>`<option value="${a.phone}">${a.phone}${a.label?(' · '+a.label):''}${a.logged_in?' ✅':''}</option>`).join("");
  if(cur && accts.some(a=>a.phone===cur)) sel.value = cur;
  // 列表
  const box = $("acctList");
  box.innerHTML = accts.length ? accts.map(a=>{
    const t = a.last_login ? new Date(a.last_login*1000).toLocaleString() : "未登录";
    return `<div class="prod-item" style="justify-content:space-between">
      <span>${a.phone}${a.label?(' · '+a.label):''} ${a.logged_in?'<b style="color:#16a34a">已登录</b>':'<span style="color:#9ca3af">未登录</span>'}${a.level?(' · '+a.level):''}</span>
      <span><span style="font-size:11px; color:#9ca3af; margin-right:8px">${t}</span>
      <button class="btn-ghost" style="padding:4px 10px" onclick="deleteAccount('${a.phone}')">删除</button></span>
    </div>`;
  }).join("") : '<span style="color:#9ca3af; font-size:13px">暂无账号，可在上方添加</span>';
}

// ---- 定时 ----
async function saveSchedule(){
  const phones = [];
  document.querySelectorAll("#schedAcctList input[type=checkbox]:checked").forEach(c=>{ if(c.value) phones.push(c.value); });
  const selected = [];
  document.querySelectorAll("#schedProdList input[type=checkbox]:checked").forEach(c=>{ if(c.value) selected.push(c.value); });
  const body = {
    enabled: $("schedEnable").checked,
    mode: $("schedMode").value,
    time: $("schedTime").value.trim(),
    interval_min: parseInt($("schedInterval").value||"360",10)||360,
    phones, selected,
    repeat: parseInt($("schedRepeat").value||"1",10)||1,
    interval_sec: parseInt($("schedIntervalSec").value||"60",10)||60,
  };
  const r = await fetch("/api/schedule",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const j = await r.json().catch(()=>({}));
  if(j.ok){ alert("定时已保存："+(body.enabled?"已启用":"已关闭")); }
}
function renderSchedule(cfg, accts, products){
  $("schedEnable").checked = !!cfg.enabled;
  $("schedMode").value = cfg.mode==="interval"?"interval":"daily";
  $("schedTime").value = cfg.time||"08:00";
  $("schedInterval").value = cfg.interval_min||360;
  $("schedRepeat").value = cfg.repeat||1;
  $("schedIntervalSec").value = cfg.interval_sec||60;
  const phSet = new Set(cfg.phones||[]);
  $("schedAcctList").innerHTML = accts.length ? accts.map(a=>
    `<label class="prod-item"><input type="checkbox" value="${a.phone}" ${phSet.has(a.phone)?'checked':''}> ${a.phone}${a.label?(' · '+a.label):''}</label>`).join("")
    : '<span style="color:#9ca3af; font-size:13px">请先在上方添加账号</span>';
  const prSet = new Set((cfg.selected||[]).map(String));
  $("schedProdList").innerHTML = (products||[]).map(p=>
    `<label class="prod-item"><input type="checkbox" value="${p.amount}" ${prSet.has(String(p.amount))?'checked':''}> ${p.amount}元 · ${p.name}</label>`).join("");
  const ss = $("schedStatus");
  if(ss){ ss.textContent = cfg.enabled ? "（当前：已启用）" : "（当前：已关闭）"; }
}

async function stopFlow(){
  await fetch("/api/stop",{method:"POST"}).catch(()=>{});
}

function clearLog(){ /* 仅前端视觉清空，服务端仍保留 */ $("log").textContent="（已清空显示）"; }

let lastLen = 0;
function renderProducts(products){
  const box = $("productList");
  if(!box) return;
  if(box.dataset.rendered === "1"){ updateSelCount(); return; }  // 只渲染一次
  box.dataset.rendered = "1";
  box.innerHTML = "";
  products.forEach(p=>{
    const id = "prod_"+p.amount;
    const lab = document.createElement("label");
    lab.className = "prod-item";
    lab.innerHTML = '<input type="checkbox" value="'+p.amount+'" checked> '+p.amount+'元 · '+p.name;
    box.appendChild(lab);
  });
  updateSelCount();
  box.addEventListener("change", updateSelCount);
}
function updateSelCount(){
  const n = document.querySelectorAll("#productList input[type=checkbox]:checked").length;
  const el = $("selCount"); if(el) el.textContent = n;
}
function selAll(on){
  document.querySelectorAll("#productList input[type=checkbox]").forEach(c=>{ c.checked = on; });
  updateSelCount();
}
async function refresh(){
  try{
    const s = await fetch("/api/status").then(r=>r.json());
    if(s.webhookUrl && s.webhookUrl !== whUrl){ whUrl = s.webhookUrl; $("whUrl").textContent = whUrl; }
    const tag = $("whTag");
    if(tag){
      const custom = s.webhookSource === "custom";
      tag.textContent = custom ? "自定义" : "自带";
      tag.className = "tag" + (custom ? "" : " builtin");
    }
    const cw = $("customWh");
    if(cw && document.activeElement !== cw){ cw.value = (s.customWebhook || ""); }
    const pb = $("pollBadge");
    if(pb){
      if(s.externalPolling){ pb.textContent = "自动拉取：开启"; pb.style.background = "#16a34a"; }
      else { pb.textContent = "自动拉取：关闭"; pb.style.background = "#9ca3af"; }
    }
    if(s.coreError){ $("coreErr").innerHTML = '<div class="err">核心依赖缺失：'+s.coreError+
      '<br>请先 <code>pip install pycryptodome</code> 并确保 node 可用，再重启本程序。</div>'; }
    $("step").textContent = s.step;
    $("meta").textContent = s.phone ? ("手机号: "+s.phone+"  "+(s.logged_in?"· 已登录":"· 未登录")) : "";
    $("dot").className = "dot " + (s.running ? "run" : "idle");
    $("runBadge").style.display = s.running ? "inline-block" : "none";
    web_state_running = s.running;
    $("startBtn").disabled = s.running;
    $("stopBtn").disabled = !s.running;
    $("phone").disabled = s.running;
    if(Array.isArray(s.products) && s.products.length){
      renderProducts(s.products);
    }
    if(Array.isArray(s.accounts)){
      renderAccounts(s.accounts);
    }
    if(s.schedule){
      renderSchedule(s.schedule, s.accounts||[], s.products||[]);
    }
    if(s.logs && s.logs.length){
      const txt = s.logs.join("\\n");
      if(txt.length !== lastLen){ $("log").textContent = txt; $("log").scrollTop = $("log").scrollHeight; lastLen = txt.length; }
    }
  }catch(e){}
  setTimeout(refresh, 1500);
}
refresh();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return b""
        data = self.rfile.read(length)
        # 先按 UTF-8 解码，失败再尝试 GBK/GB18030（部分短信网关/转发 App 用 GBK）
        for enc in ("utf-8", "gb18030", "gbk", "latin-1"):
            try:
                return data.decode(enc)
            except Exception:
                continue
        return data.decode("utf-8", "ignore")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path.rstrip("/")
        if p in ("", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif p == "/api/status":
            with _lock:
                data = {
                    "running": web_state["running"],
                    "logged_in": web_state["logged_in"],
                    "phone": web_state["phone"],
                    "step": web_state["step"],
                    "logs": list(web_state["logs"]),
                    "coreError": CORE_ERROR or None,
                    "webhookUrl": web_state["webhookUrl"],
                    "customWebhook": web_state["customWebhook"],
                    "webhookSource": web_state["webhookSource"],
                    "externalPolling": bool(web_state["customWebhook"]),
                    "lastCode": web_state["lastCode"],
                    "lastCodeAt": web_state["lastCodeAt"],
                    "products": (
                        [{"amount": a, "name": core.PRODUCTS[a]["name"]}
                         for a in sorted(core.PRODUCTS.keys(), key=int)]
                        if core else []
                    ),
                    "accounts": [
                        {"phone": a.get("phone", ""), "label": a.get("label", ""),
                         "logged_in": bool(a.get("sessKey")), "level": a.get("level", ""),
                         "last_login": a.get("last_login", 0)}
                        for a in accounts
                    ],
                    "schedule": dict(schedule_cfg),
                    "schedulerRunning": scheduler_thread_running,
                }
            self._send(200, data)
        elif p == "/healthz":
            self._send(200, {"ok": True})
        elif p in ("/webhook/sms", "/api/hooks"):
            self._send(200, {"ok": True, "msg": "GET 仅用于探活，请使用 POST 发送短信"})
        elif p == "/api/accounts":
            self._send(200, {"accounts": [
                {"phone": a.get("phone", ""), "label": a.get("label", ""),
                 "logged_in": bool(a.get("sessKey")), "level": a.get("level", ""),
                 "last_login": a.get("last_login", 0)}
                for a in accounts]})
        elif p == "/api/schedule":
            self._send(200, dict(schedule_cfg))
        else:
            self._send(404, {"ok": False, "msg": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path.rstrip("/")
        q = urllib.parse.parse_qs(parsed.query)
        query = {k: v[0] for k, v in q.items()}
        raw = self._read_body()

        if p in ("/webhook/sms", "/api/hooks", ""):
            code, note = push_sms(raw, query)
            self._send(200, {"ok": code is not None, "code": code, "note": note})

        elif parsed.path == "/api/code":
            try:
                obj = json.loads(raw) if raw else {}
            except Exception:
                obj = {}
            code = query.get("code") or str(obj.get("code", "")).strip()
            if re.fullmatch(r"\d{4,8}", code):
                clear_codes()         # 手动提交时，丢弃可能因停等残留的旧码
                code_queue.put(code)
                log(f"📝 手动提交验证码: {code}")
                self._send(200, {"ok": True, "code": code})
            else:
                self._send(400, {"ok": False, "msg": "验证码格式不正确"})

        elif parsed.path == "/api/start":
            if web_state["running"]:
                self._send(409, {"ok": False, "msg": "已有任务在运行"})
                return
            if core is None:
                self._send(500, {"ok": False, "msg": "核心依赖缺失: " + CORE_ERROR})
                return
            try:
                obj = json.loads(raw) if raw else {}
            except Exception:
                obj = {}
            phone = str(obj.get("phone", "") or query.get("phone", "")).strip()
            skip = bool(obj.get("skipLogin", False))
            if not re.fullmatch(r"\d{11}", phone):
                self._send(400, {"ok": False, "msg": "手机号格式不正确"})
                return
            selected = obj.get("selected") or []
            if not isinstance(selected, list):
                selected = []
            try:
                repeat = int(obj.get("repeat", 1) or 1)
            except Exception:
                repeat = 1
            try:
                interval = int(obj.get("interval", 60) or 60)
            except Exception:
                interval = 60
            interval = min(max(interval, 0), 600)
            t = threading.Thread(
                target=run_flow,
                args=(phone, skip),
                kwargs={"selected": selected, "repeat": repeat, "interval": interval},
                daemon=True,
            )
            t.start()
            self._send(200, {"ok": True, "msg": "已启动"})

        elif parsed.path == "/api/stop":
            stop_ev.set()
            clear_codes()
            web_state["running"] = False
            set_step("已停止")
            log("已手动停止流程")
            self._send(200, {"ok": True, "msg": "已停止"})

        elif parsed.path == "/api/config":
            # 预留自定义 Webhook 地址：配置后优先使用，留空则回退自带地址
            try:
                obj = json.loads(raw) if raw else {}
            except Exception:
                obj = {}
            url = str(obj.get("webhook_url", "") or "").strip().rstrip("/")
            CONFIG["webhook_url"] = url
            if not save_config(CONFIG):
                self._send(500, {"ok": False, "msg": "保存配置失败（无写入权限）"})
                return
            web_state["webhookUrl"] = build_webhook_url()
            web_state["customWebhook"] = resolve_custom_webhook()
            web_state["webhookSource"] = "custom" if web_state["customWebhook"] else "builtin"
            tag = "（自定义）" if web_state["webhookSource"] == "custom" else "（自带/局域网）"
            log("Webhook 地址已更新: " + web_state["webhookUrl"] + tag)
            self._send(200, {
                "ok": True,
                "webhookUrl": web_state["webhookUrl"],
                "webhookSource": web_state["webhookSource"],
                "msg": "已保存" + tag,
            })

        elif parsed.path == "/api/accounts":
            # 多账号管理：{"action":"add"|"delete", "phone":"...", "label":"..."}
            try:
                obj = json.loads(raw) if raw else {}
            except Exception:
                obj = {}
            action = obj.get("action", "")
            phone = str(obj.get("phone", "")).strip()
            if action == "add":
                if not re.fullmatch(r"\d{11}", phone):
                    self._send(400, {"ok": False, "msg": "手机号格式不正确"})
                    return
                if not get_account(phone):
                    accounts.append({"phone": phone, "sessKey": "", "logged_in": False,
                                     "last_login": 0, "label": str(obj.get("label", "")), "level": ""})
                    save_accounts()
                    log(f"已添加账号 {phone}")
                self._send(200, {"ok": True})
            elif action == "delete":
                delete_account(phone)
                log(f"已删除账号 {phone}")
                self._send(200, {"ok": True})
            elif action == "clearsess":
                a = get_account(phone)
                if a:
                    a["sessKey"] = ""; a["logged_in"] = False
                    save_accounts()
                self._send(200, {"ok": True})
            else:
                self._send(400, {"ok": False, "msg": "未知操作"})

        elif parsed.path == "/api/schedule":
            # 定时配置：{"enabled":bool,"mode":"daily"|"interval","time":"HH:MM","interval_min":N,
            #          "phones":[...],"selected":[...],"repeat":N,"interval_sec":N}
            try:
                obj = json.loads(raw) if raw else {}
            except Exception:
                obj = {}
            schedule_cfg["enabled"] = bool(obj.get("enabled", False))
            schedule_cfg["mode"] = obj.get("mode", "daily") if obj.get("mode") in ("daily", "interval") else "daily"
            schedule_cfg["time"] = str(obj.get("time", "08:00"))[:5]
            try:
                schedule_cfg["interval_min"] = max(1, int(obj.get("interval_min", 360)))
            except Exception:
                pass
            ph = obj.get("phones", [])
            schedule_cfg["phones"] = [str(x) for x in ph if re.fullmatch(r"\d{11}", str(x))] if isinstance(ph, list) else []
            sel = obj.get("selected", [])
            schedule_cfg["selected"] = [str(x) for x in sel] if isinstance(sel, list) else []
            try:
                schedule_cfg["repeat"] = max(1, int(obj.get("repeat", 1)))
            except Exception:
                pass
            try:
                schedule_cfg["interval_sec"] = min(max(int(obj.get("interval_sec", 60)), 0), 600)
            except Exception:
                pass
            save_accounts()
            log(f"定时配置已更新：{('开启' if schedule_cfg['enabled'] else '关闭')} · {schedule_cfg['mode']}"
                + (f" {schedule_cfg['time']}" if schedule_cfg['mode'] == "daily" else f" 每{schedule_cfg['interval_min']}分钟"))
            self._send(200, {"ok": True, "schedule": dict(schedule_cfg)})

        else:
            self._send(404, {"ok": False, "msg": "not found"})

    def log_message(self, fmt, *args):
        pass  # 静默


# ----------------------------------------------------------------------------
# 定时自动运行调度器
# ----------------------------------------------------------------------------
scheduler_thread_running = False
_sched_last_daily = ""        # 已触发过的 daily 日期+时间，防同一分钟重复触发
_sched_last_interval_ts = 0   # interval 模式上次触发时间戳


def scheduler_loop():
    """后台守护线程：按 schedule_cfg 定时对选定账号依次执行领取。"""
    global scheduler_thread_running, _sched_last_daily, _sched_last_interval_ts
    scheduler_thread_running = True
    while True:
        try:
            cfg = schedule_cfg
            if (cfg.get("enabled") and not web_state["running"]
                    and core is not None and not stop_ev.is_set()):
                phones = [p for p in cfg.get("phones", []) if get_account(p)]
                if phones:
                    now = time.localtime()
                    should_run = False
                    if cfg.get("mode") == "daily":
                        hhmm = time.strftime("%H:%M", now)
                        key = time.strftime("%Y-%m-%d", now) + " " + hhmm
                        if hhmm == cfg.get("time", "08:00") and key != _sched_last_daily:
                            should_run = True
                            _sched_last_daily = key
                    else:  # interval
                        if time.time() - _sched_last_interval_ts >= cfg.get("interval_min", 360) * 60:
                            should_run = True
                            _sched_last_interval_ts = time.time()
                    if should_run:
                        log(f"⏰ 定时任务触发，将依次处理 {len(phones)} 个账号")
                        for ph in phones:
                            if web_state["running"] or stop_ev.is_set():
                                break
                            log(f"⏰ 定时开始处理账号 {ph}")
                            run_flow(ph, True,
                                     selected=cfg.get("selected") or [],
                                     repeat=cfg.get("repeat", 1),
                                     interval=cfg.get("interval_sec", 60))
                        log("⏰ 定时任务本轮处理完毕")
        except Exception as e:
            print(f"scheduler error: {e}", flush=True)
        time.sleep(20)


def main():
    load_session()
    web_state["webhookUrl"] = build_webhook_url()
    web_state["customWebhook"] = resolve_custom_webhook()
    web_state["webhookSource"] = "custom" if web_state["customWebhook"] else "builtin"
    if CORE_ERROR:
        log("⚠️ 核心脚本加载失败，Web 服务仍可启动，但领券功能不可用: " + CORE_ERROR)
    else:
        log("核心脚本加载成功，和包协议已就绪")
    if web_state["logged_in"]:
        log("检测到已保存的登录会话，可使用「复用会话」直接领取")
    threading.Thread(target=scheduler_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log(f"Web 控制台已启动: http://0.0.0.0:{PORT}")
    log("Webhook 地址(外接用): " + web_state["webhookUrl"])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("已停止")


if __name__ == "__main__":
    main()
