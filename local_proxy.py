# -*- coding: utf-8 -*-
"""
本地 SOCKS5 代理转发器
- 每个 worker 一个本地端口
- Chrome 固定连本地端口
- 动态切换上游 socks5 代理 (不关浏览器!)
- 切换时断开所有旧连接, 新请求走新上游
"""
import socket
import struct
import threading
import time
import select


class LocalSocks5Forwarder:
    """
    本地 SOCKS5 转发器
    监听 127.0.0.1:local_port
    将流量转发到 upstream socks5 代理
    """

    def __init__(self, local_port, log_func=None):
        self.local_port = local_port
        self.upstream_host = None
        self.upstream_port = None
        self.log_func = log_func
        self._server_sock = None
        self._running = False
        self._lock = threading.Lock()
        self._connections = []  # 活跃连接列表
        self._thread = None

    def log(self, msg):
        if self.log_func:
            self.log_func(f"[转发:{self.local_port}] {msg}")

    def start(self):
        """启动本地监听"""
        self._running = True
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.settimeout(1.0)
        self._server_sock.bind(('127.0.0.1', self.local_port))
        self._server_sock.listen(50)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        self.log(f"已启动, 监听 127.0.0.1:{self.local_port}")

    def stop(self):
        """停止转发器"""
        self._running = False
        self._close_all_connections()
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
        self.log("已停止")

    def switch_upstream(self, host, port):
        """
        切换上游代理 (核心功能!)
        1. 更新上游地址
        2. 断开所有现有连接
        3. 新的请求会走新上游
        """
        with self._lock:
            self.upstream_host = host
            self.upstream_port = port
            # 断开所有现有连接, 迫使 Chrome 重新建立连接走新上游
            self._close_all_connections()
        self.log(f"上游已切换 -> {host}:{port}")

    def _close_all_connections(self):
        """关闭所有活跃连接"""
        for conn in self._connections:
            try:
                conn.close()
            except Exception:
                pass
        self._connections.clear()

    def _accept_loop(self):
        """接受新连接"""
        while self._running:
            try:
                client_sock, addr = self._server_sock.accept()
                client_sock.settimeout(30)
                t = threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True)
                t.start()
            except socket.timeout:
                continue
            except Exception:
                if self._running:
                    time.sleep(0.1)

    def _handle_client(self, client_sock):
        """处理单个客户端连接 (SOCKS5 握手 + 转发)"""
        remote_sock = None
        with self._lock:
            self._connections.append(client_sock)

        try:
            # === SOCKS5 握手 (客户端 -> 本地) ===
            # 1. 客户端发送: VER, NMETHODS, METHODS
            data = client_sock.recv(256)
            if not data or data[0] != 0x05:
                return

            # 回复: 不需要认证
            client_sock.sendall(b'\x05\x00')

            # 2. 客户端发送连接请求: VER, CMD, RSV, ATYP, DST.ADDR, DST.PORT
            data = client_sock.recv(256)
            if not data or len(data) < 7:
                return

            ver, cmd, rsv, atyp = data[0], data[1], data[2], data[3]
            if cmd != 0x01:  # 只支持 CONNECT
                client_sock.sendall(b'\x05\x07\x00\x01' + b'\x00' * 6)
                return

            # 解析目标地址
            if atyp == 0x01:  # IPv4
                dst_addr = socket.inet_ntoa(data[4:8])
                dst_port = struct.unpack('>H', data[8:10])[0]
            elif atyp == 0x03:  # 域名
                addr_len = data[4]
                dst_addr = data[5:5 + addr_len].decode()
                dst_port = struct.unpack('>H', data[5 + addr_len:7 + addr_len])[0]
            elif atyp == 0x04:  # IPv6
                dst_addr = socket.inet_ntop(socket.AF_INET6, data[4:20])
                dst_port = struct.unpack('>H', data[20:22])[0]
            else:
                client_sock.sendall(b'\x05\x08\x00\x01' + b'\x00' * 6)
                return

            # === 连接上游 SOCKS5 代理 ===
            with self._lock:
                up_host = self.upstream_host
                up_port = self.upstream_port

            if not up_host or not up_port:
                client_sock.sendall(b'\x05\x01\x00\x01' + b'\x00' * 6)
                return

            remote_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            remote_sock.settimeout(15)
            remote_sock.connect((up_host, up_port))

            with self._lock:
                self._connections.append(remote_sock)

            # 上游 SOCKS5 握手
            remote_sock.sendall(b'\x05\x01\x00')  # 无认证
            resp = remote_sock.recv(2)
            if not resp or resp[0] != 0x05 or resp[1] != 0x00:
                # 上游不支持无认证
                client_sock.sendall(b'\x05\x01\x00\x01' + b'\x00' * 6)
                return

            # 转发连接请求到上游
            remote_sock.sendall(data)
            resp = remote_sock.recv(256)
            if not resp or resp[1] != 0x00:
                client_sock.sendall(b'\x05\x01\x00\x01' + b'\x00' * 6)
                return

            # 告诉客户端连接成功
            client_sock.sendall(b'\x05\x00\x00\x01' + b'\x00' * 6)

            # === 双向转发数据 ===
            self._relay(client_sock, remote_sock)

        except Exception:
            pass
        finally:
            for s in [client_sock, remote_sock]:
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
            with self._lock:
                if client_sock in self._connections:
                    self._connections.remove(client_sock)
                if remote_sock and remote_sock in self._connections:
                    self._connections.remove(remote_sock)

    def _relay(self, sock1, sock2):
        """双向转发数据"""
        socks = [sock1, sock2]
        while self._running:
            try:
                readable, _, error = select.select(socks, [], socks, 5)
                if error:
                    break
                for s in readable:
                    data = s.recv(65536)
                    if not data:
                        return
                    target = sock2 if s is sock1 else sock1
                    target.sendall(data)
            except Exception:
                break
