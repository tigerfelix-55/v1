"""
proxy_checker.py - 代理解析 + 存活检测
支持 socks4/socks5/http/https 混合格式
"""

import re
import socket
import struct
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed


@dataclass
class Proxy:
    protocol: str  # socks4, socks5, http, https
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    alive: bool = False
    latency: float = 0.0  # ms

    def to_url(self) -> str:
        """转换为 URL 格式 protocol://[user:pass@]host:port"""
        if self.username and self.password:
            return f"{self.protocol}://{self.username}:{self.password}@{self.host}:{self.port}"
        return f"{self.protocol}://{self.host}:{self.port}"

    def to_selenium_arg(self) -> str:
        """转换为 Selenium Chrome --proxy-server 参数格式"""
        if self.protocol in ("http", "https"):
            return f"http://{self.host}:{self.port}"
        else:
            return f"{self.protocol}://{self.host}:{self.port}"

    def __str__(self):
        return self.to_url()


def parse_proxy_line(line: str) -> Optional[Proxy]:
    """
    解析单行代理文本，支持格式：
    - protocol://host:port
    - protocol://user:pass@host:port
    - host:port (默认 http)
    - user:pass@host:port (默认 http)
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # 去除可能的引号包裹
    line = line.strip('"').strip("'")

    protocol = "http"
    auth_user = None
    auth_pass = None

    # 提取协议前缀 (支持大小写混合)
    proto_match = re.match(r'^(socks4|socks5|https?|SOCKS4|SOCKS5|HTTPS?|HTTP?|Socks4|Socks5)://', line, re.IGNORECASE)
    if proto_match:
        protocol = proto_match.group(1).lower()
        # 标准化 http 变体
        if protocol in ("http", "https"):
            pass
        line = line[proto_match.end():]

    # 去除行内可能的空格
    line = line.strip()

    # 提取认证信息 (user:pass@host:port)
    if "@" in line:
        at_idx = line.rfind("@")
        auth_part = line[:at_idx]
        host_part = line[at_idx + 1:]
        if ":" in auth_part:
            auth_user, auth_pass = auth_part.split(":", 1)
        line = host_part

    # 提取 host:port - 支持 IPv4 和域名
    # 兼容末尾可能有空格或其他字符
    match = re.match(r'^([a-zA-Z0-9._\-]+):(\d+)\s*$', line)
    if not match:
        # 尝试更宽松的匹配
        match = re.match(r'^([^:]+):(\d+)', line)
    if not match:
        return None

    host = match.group(1).strip()
    try:
        port = int(match.group(2).strip())
    except ValueError:
        return None

    if port < 1 or port > 65535:
        return None

    if not host:
        return None

    return Proxy(
        protocol=protocol,
        host=host,
        port=port,
        username=auth_user,
        password=auth_pass
    )


def load_proxies_from_file(filepath: str) -> List[Proxy]:
    """从文件加载代理列表"""
    proxies = []
    try:
        # 尝试多种编码读取，处理 BOM
        content = None
        for encoding in ["utf-8-sig", "utf-8", "gbk", "latin-1"]:
            try:
                with open(filepath, "r", encoding=encoding) as f:
                    content = f.read()
                break
            except (UnicodeDecodeError, UnicodeError):
                continue

        if content is None:
            print(f"[错误] 无法读取代理文件: {filepath}")
            return proxies

        lines = content.splitlines()
        failed_lines = []
        for i, line in enumerate(lines, 1):
            # 去除所有不可见字符、空格、\r\n
            line = line.strip().replace('\r', '').replace('\n', '').replace('\t', '')
            # 去除零宽字符
            line = re.sub(r'[\u200b\u200c\u200d\ufeff\u00a0]', '', line)
            if not line:
                continue
            proxy = parse_proxy_line(line)
            if proxy:
                proxies.append(proxy)
            else:
                failed_lines.append((i, line[:60]))

        if failed_lines and len(failed_lines) <= 10:
            print(f"[警告] 以下行解析失败:")
            for ln, text in failed_lines:
                print(f"  第{ln}行: '{text}'")

    except FileNotFoundError:
        print(f"[错误] 代理文件未找到: {filepath}")
    except Exception as e:
        print(f"[错误] 读取代理文件失败: {e}")
    return proxies


def check_http_proxy(proxy: Proxy, timeout: float = 10.0) -> Tuple[bool, float]:
    """检测 HTTP/HTTPS 代理是否存活"""
    try:
        start = time.time()
        # 构建代理 handler
        proxy_url = proxy.to_url()
        proxy_handler = urllib.request.ProxyHandler({
            "http": proxy_url,
            "https": proxy_url
        })
        opener = urllib.request.build_opener(proxy_handler)
        opener.addheaders = [("User-Agent", "Mozilla/5.0")]

        # 尝试访问一个轻量页面
        response = opener.open("http://httpbin.org/ip", timeout=timeout)
        latency = (time.time() - start) * 1000
        if response.status == 200:
            return True, latency
        return False, 0
    except Exception:
        return False, 0


def check_socks4_proxy(proxy: Proxy, timeout: float = 10.0) -> Tuple[bool, float]:
    """检测 SOCKS4 代理是否存活"""
    try:
        start = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((proxy.host, proxy.port))

        # SOCKS4 连接请求 (连接到 httpbin.org:80)
        target_ip = socket.inet_aton(socket.gethostbyname("httpbin.org"))
        target_port = 80
        # VN=4, CD=1(connect), DSTPORT, DSTIP, USERID(\x00)
        packet = struct.pack(">BBH", 4, 1, target_port) + target_ip + b"\x00"
        sock.sendall(packet)

        response = sock.recv(8)
        sock.close()

        latency = (time.time() - start) * 1000
        # 第二字节 0x5A 表示成功
        if len(response) >= 2 and response[1] == 0x5A:
            return True, latency
        return False, 0
    except Exception:
        return False, 0


def check_socks5_proxy(proxy: Proxy, timeout: float = 10.0) -> Tuple[bool, float]:
    """检测 SOCKS5 代理是否存活"""
    try:
        start = time.time()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((proxy.host, proxy.port))

        # 握手：支持无认证(0x00)和用户名密码认证(0x02)
        if proxy.username and proxy.password:
            sock.sendall(b"\x05\x02\x00\x02")
        else:
            sock.sendall(b"\x05\x01\x00")

        response = sock.recv(2)
        if len(response) < 2 or response[0] != 0x05:
            sock.close()
            return False, 0

        # 如果需要认证
        if response[1] == 0x02 and proxy.username and proxy.password:
            user_bytes = proxy.username.encode()
            pass_bytes = proxy.password.encode()
            auth_packet = (
                b"\x01"
                + bytes([len(user_bytes)]) + user_bytes
                + bytes([len(pass_bytes)]) + pass_bytes
            )
            sock.sendall(auth_packet)
            auth_resp = sock.recv(2)
            if len(auth_resp) < 2 or auth_resp[1] != 0x00:
                sock.close()
                return False, 0
        elif response[1] != 0x00:
            sock.close()
            return False, 0

        sock.close()
        latency = (time.time() - start) * 1000
        return True, latency
    except Exception:
        return False, 0


def check_proxy(proxy: Proxy, timeout: float = 10.0) -> Proxy:
    """根据协议类型检测代理存活"""
    alive = False
    latency = 0.0

    if proxy.protocol in ("http", "https"):
        alive, latency = check_http_proxy(proxy, timeout)
    elif proxy.protocol == "socks4":
        alive, latency = check_socks4_proxy(proxy, timeout)
    elif proxy.protocol == "socks5":
        alive, latency = check_socks5_proxy(proxy, timeout)

    proxy.alive = alive
    proxy.latency = latency
    return proxy


def check_proxies_batch(
    proxies: List[Proxy],
    max_workers: int = 20,
    timeout: float = 10.0,
    callback=None
) -> List[Proxy]:
    """
    批量检测代理存活
    callback(proxy, index, total) - 每完成一个检测时回调
    """
    alive_proxies = []
    total = len(proxies)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_proxy = {
            executor.submit(check_proxy, proxy, timeout): (i, proxy)
            for i, proxy in enumerate(proxies)
        }

        for future in as_completed(future_to_proxy):
            idx, proxy = future_to_proxy[future]
            try:
                result = future.result()
                if result.alive:
                    alive_proxies.append(result)
                if callback:
                    callback(result, idx, total)
            except Exception:
                pass

    # 按延迟排序
    alive_proxies.sort(key=lambda p: p.latency)
    return alive_proxies


# ============ 命令行独立运行 ============
if __name__ == "__main__":
    import sys

    filepath = "proxies.txt"
    if len(sys.argv) > 1:
        filepath = sys.argv[1]

    print(f"[*] 加载代理文件: {filepath}")
    proxies = load_proxies_from_file(filepath)
    print(f"[*] 共加载 {len(proxies)} 个代理")

    if not proxies:
        print("[!] 没有可用代理")
        sys.exit(1)

    print("[*] 开始存活检测...")
    checked = [0]

    def on_checked(proxy, idx, total):
        checked[0] += 1
        status = "✓ 存活" if proxy.alive else "✗ 失败"
        latency_str = f" ({proxy.latency:.0f}ms)" if proxy.alive else ""
        print(f"  [{checked[0]}/{total}] {proxy.to_url()} -> {status}{latency_str}")

    alive = check_proxies_batch(proxies, max_workers=30, timeout=10, callback=on_checked)

    print(f"\n[*] 检测完成: {len(alive)}/{len(proxies)} 个代理存活")
    for p in alive:
        print(f"  ✓ {p.to_url()} ({p.latency:.0f}ms)")
