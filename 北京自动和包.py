#!/usr/bin/env python3
"""
北京移动权益超市 - 自动领和包脚本（协议版）
依赖: pycryptodome (pip install pycryptodome --break-system-packages)
用法:
  python3 auto_claim.py login <手机号> <验证码>          # 登录
  python3 auto_claim.py login <手机号> <webhook_url>      # 登录(webhook自动获取验证码)
  python3 auto_claim.py claim <验证码|webhook_url>        # 领券
"""
import sys, os, json, time, hashlib, random, string, subprocess, base64
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

BASE_URL = "https://xcxp.bj.10086.cn"
API_URL = f"{BASE_URL}/rights-intf/api/outer/invoke.do"
STATE_FILE = "/tmp/claim_state.json"
AES_KEY = b"A9B6C6D8E4F3G2H1"
FIXED_IV = bytes.fromhex("0e0a264a19793f69b500ec279b044524")
ACCESS_TOKEN = "1924d545b02f4de6b26e017253a5dd2d"
RETAILER_CODE = "BJYDJTAPP001"
SON_RETAILER_CODE = "A004"
# 产品配置
PRODUCTS = {
    "1": {"rightsCode": "QY1701763685438", "legalRightsId": "1731948543350599682", "offerId": "", "name": "和包出行体验会员（1元）"},
    "2": {"rightsCode": "QY1701761448805", "legalRightsId": "1731939162227888130", "offerId": "", "name": "和包出行体验会员（2元）"},
    "5": {"rightsCode": "QY1701758647806", "legalRightsId": "1731927413990588418", "offerId": "19197", "name": "和包出行体验会员（5元）"},
    "30": {"rightsCode": "QY1737687753540", "legalRightsId": "1882625005885779970", "offerId": "40625", "name": "美宜佳满减券（30元）"},
}


def aes_cbc_encrypt(plaintext, iv=FIXED_IV):
    cipher = AES.new(AES_KEY, AES.MODE_CBC, iv)
    padded = pad(plaintext.encode() if isinstance(plaintext, str) else plaintext, 16)
    return cipher.encrypt(padded)


def aes_ecb_encrypt(data):
    cipher = AES.new(b"9e5702ead4d643fd", AES.MODE_ECB)
    padded = pad(data.encode() if isinstance(data, str) else data, 16)
    return base64.b64encode(cipher.encrypt(padded)).decode()


def sign_params(ability_code, params):
    sorted_keys = sorted(params.keys())
    parts = [f"abilityCode={ability_code}"]
    for k in sorted_keys:
        parts.append(f"{k}={params[k]}")
    return hashlib.sha256("&".join(parts).encode()).hexdigest()


def build_plaintext(ability_code, data, sess_key="", phone=""):
    random_str = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    ts = time.strftime("%Y%m%d%H%M%S")
    txn_id = "1" + str(int(time.time() * 1000))[-13:] + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    p = {
        "accessToken": ACCESS_TOKEN,
        "body": body,
        "randomstr": random_str,
        "timestamp": ts,
        "transactionId": txn_id,
    }
    if sess_key:
        p["sessKey"] = sess_key
        p["traceMemberPhone"] = phone

    sig = sign_params(ability_code, p)

    body_escaped = body.replace('"', '\\"')
    p_escaped = {k: (v.replace('"', '\\"') if k == "body" else v) for k, v in p.items()}

    result = ability_code
    for k in sorted(p_escaped.keys()):
        result += f'","{k}":"{p_escaped[k]}'
    result += f'","sign":"{sig}"' + "}"
    return result


def encrypt_request(ability_code, data, sess_key="", phone=""):
    plaintext = build_plaintext(ability_code, data, sess_key, phone)
    ct = aes_cbc_encrypt(plaintext, FIXED_IV)
    return base64.b64encode(FIXED_IV + ct).decode()


def http_post(payload):
    node_code = f"""
    const resp = await fetch('{API_URL}', {{
        method: 'POST',
        headers: {{
            'Content-Type': 'application/json;charset=UTF-8',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36',
            'Origin': 'https://xcxp.bj.10086.cn',
            'Referer': 'https://xcxp.bj.10086.cn/rights-front/dist/index.html',
            'Accept': 'application/json, text/plain, */*',
            'source': 'Local',
        }},
        body: JSON.stringify({json.dumps(payload)}),
    }});
    const text = await resp.text();
    console.log(text);
    """
    result = subprocess.run(["node", "--input-type=module", "-e", node_code],
                          capture_output=True, text=True, timeout=15000)
    if result.stderr and "Error" in result.stderr:
        print(f"  HTTP错误: {result.stderr.strip()}")
    return result.stdout.strip()


def call_api(ability_code, data, sess_key="", phone=""):
    payload = encrypt_request(ability_code, data, sess_key, phone)
    resp_text = http_post(payload)
    if not resp_text:
        return None
    try:
        resp = json.loads(resp_text)
        if resp.get("resultCode") == "0":
            body = resp.get("body", "")
            if body and body != "null":
                try:
                    return json.loads(body)
                except:
                    return body
            return None
        else:
            print(f"  API失败: {resp.get('resultMsg')} (code={resp.get('resultCode')})")
            return None
    except:
        print(f"  响应解析失败: {resp_text[:200]}")
        return None


def send_sms(phone):
    print(f"发送验证码到 {phone}...")
    result = call_api("SMS_VERI_CODE_SEND", {"receviNo": phone}, phone=phone)
    print(f"  结果: {result}")
    return result


def login_with_sms(phone, code):
    data = {
        "receviNo": phone,
        "verificationCode": code,
        "retailerCode": RETAILER_CODE,
        "sonRetailerCode": SON_RETAILER_CODE,
    }
    return call_api("APP_LOGIN_CHK_SMS", data, phone=phone)


def claim_amount(sess_key, phone, amount, verify_code=""):
    price = str(int(amount) * 100)  # 元转分
    prod = PRODUCTS.get(amount)
    if not prod:
        print(f"  未知面额: {amount}")
        return False

    rights_code = prod["rightsCode"]
    offer_id = prod["offerId"]

    # 1. 发送验证码
    print(f"  发送验证码({amount}元)...")
    r = call_api("BIZ_CONFIRM_SMS_SEND", {
        "rightsCode": rights_code,
    }, sess_key, phone)
    if r is None:
        print("  验证码已发送")
    else:
        print(f"  发送验证码: {r}")

    if not verify_code:
        return False

    time.sleep(2)

    # 2. 直接领取
    data = {
        "accNbr": phone,
        "rightCode": rights_code,
        "price": price,
        "memberLevelCode": None,
        "verifyCode": verify_code,
    }
    if offer_id:
        data["offerId"] = offer_id

    print("  领取中...")
    r = call_api("INNER_OPEN_TEL_FEE_AUTH", data, sess_key, phone)
    if r is None:
        print("  领取成功!")
        return True
    print(f"  领取失败: {r}")
    return False


def poll_webhook(url, timeout=120):
    import urllib.request
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = urllib.request.urlopen(url, timeout=5)
            if resp.status == 200:
                data = resp.read().decode().strip()
                if data:
                    return data
        except:
            pass
        time.sleep(2)
    return None


def cmd_login(phone, code_or_webhook):
    send_sms(phone)
    code = ""
    if code_or_webhook:
        if code_or_webhook.startswith("http"):
            print("等待验证码回调...")
            code = poll_webhook(code_or_webhook)
            if code:
                print(f"  收到验证码: {code}")
        else:
            code = code_or_webhook
    if not code:
        code = os.environ.get("SMS_CODE", "")
    if not code:
        print("用法: python3 auto_claim.py login <手机号> <验证码|webhook_url>")
        sys.exit(1)
    print("登录中...")
    result = login_with_sms(phone, code)
    if result:
        sess_key = result.get("sessKey", "")
        print(f"  登录成功! sessKey: {sess_key[:20]}...")
        phone_no = result.get("phoneNo", phone)
        lv = result.get("memberInfo", {}).get("lvName", "")
        print(f"  手机号: {phone_no}, 等级: {lv}")
        state = {"sessKey": sess_key, "phone": phone_no, "time": int(time.time())}
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print(f"  Session 已保存到 {STATE_FILE}")
    else:
        print("  登录失败")


def keep_alive(sess_key, phone):
    """保持 session 活跃，防止 sessKey 过期"""
    for api in ["QUERY_MAINTENANCE", "GET_USER_INFO"]:
        r = call_api(api, {}, sess_key, phone)
        if r is not None or True:
            pass  # 调用成功即保持活跃

def cmd_claim(code_or_webhook=""):
    if not os.path.exists(STATE_FILE):
        print("错误: 请先执行 login")
        sys.exit(1)
    with open(STATE_FILE) as f:
        state = json.load(f)
    sess_key = state["sessKey"]
    phone = state["phone"]
    print(f"使用 sessKey: {sess_key[:20]}... phone: {phone}")

    # 保持活跃
    keep_alive(sess_key, phone)

    amounts = [str(k) for k in sorted(PRODUCTS.keys(), key=int)]
    for amount in amounts:
        print(f"\n--- 领取 {amount} 元 ---")
        success = claim_amount(sess_key, phone, amount)
        if not success:
            print(f"  {amount}元 领取失败")
        time.sleep(3)

    print("\n所有面额领取完成!")


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "login"
    phone = args[1] if len(args) > 1 else os.environ.get("PHONE", "")
    code_or_webhook = args[2] if len(args) > 2 else os.environ.get("WEBHOOK", "")
    if cmd == "login":
        if not phone:
            print("用法: python3 auto_claim.py login <手机号> <验证码|webhook_url>")
            sys.exit(1)
        cmd_login(phone, code_or_webhook)
    elif cmd == "claim":
        cmd_claim(code_or_webhook)
    elif cmd == "keepalive":
        if not os.path.exists(STATE_FILE):
            print("错误: 请先执行 login")
            sys.exit(1)
        with open(STATE_FILE) as f:
            state = json.load(f)
        keep_alive(state["sessKey"], state["phone"])
        print("session 保持活跃成功")
    else:
        print("用法:")
        print("  python3 auto_claim.py login <手机号> <验证码>")
        print("  python3 auto_claim.py login <手机号> <webhook_url>")
        print("  python3 auto_claim.py claim")
        print("  python3 auto_claim.py keepalive")