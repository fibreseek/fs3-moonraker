# UDP Discovery Service for Moonraker
# UDP发现服务 - 允许客户端通过广播自动发现局域网内的Moonraker设备
#
# Copyright (C) 2025 Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations
import socket
import asyncio
import logging
from typing import TYPE_CHECKING, Optional, Tuple, Any, Coroutine

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper

# UDP发现服务默认端口
UDP_DISCOVERY_PORT = 7126
# 客户端发送的发现请求消息
DISCOVERY_REQUEST = "MOONRAKER_DISCOVERY"
# 服务端响应消息前缀，完整格式: MOONRAKER_RESPONSE,<ip>,<port>
DISCOVERY_RESPONSE_PREFIX = "MOONRAKER_RESPONSE,"


class UDPDiscoveryServer(asyncio.protocols.DatagramProtocol):
    """UDP发现服务器 - 处理局域网内的Moonraker设备发现请求"""

    def __init__(self, config: ConfigHelper) -> None:
        """
        初始化UDP发现服务器

        Args:
            config: 配置助手对象，用于获取服务器配置
        """
        self.server = config.get_server()
        self.config = config
        # 是否启用UDP发现服务，默认启用
        self.enabled: bool = config.getboolean("enable", True)
        # 服务是否正在运行
        self.running: bool = False
        # 关闭时的Future对象，用于等待连接完全关闭
        self.close_fut: Optional[asyncio.Future] = None
        # UDP传输层对象
        self.transport: Optional[asyncio.DatagramTransport] = None
        # 监听的UDP端口，默认7126
        self.discovery_port: int = config.getint("port", UDP_DISCOVERY_PORT)

    async def component_init(self) -> None:
        """
        组件异步初始化 - 由Moonraker服务器启动时调用
        """
        if not self.enabled:
            logging.info("UDP Discovery Service disabled by configuration")
            return

        # 获取服务器的主机信息（IP地址和端口）
        self.host_info = self.server.get_host_info()
        self.response_ip: str = self.host_info["address"]
        self.response_port: int = self.host_info["port"]

        await self.start()

    def _create_discovery_socket(self) -> socket.socket:
        """
        创建UDP发现服务的socket

        Returns:
            配置好的UDP socket对象
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 允许地址重用，避免端口占用问题
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            # 允许端口重用（Linux系统支持）
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        return sock

    async def start(self) -> None:
        """
        启动UDP发现服务
        """
        if self.running:
            return
        try:
            sock = self._create_discovery_socket()
            sock.settimeout(0)
            sock.setblocking(False)
            # 绑定到所有网络接口的指定端口
            sock.bind(("0.0.0.0", self.discovery_port))
            # 创建asyncio数据报端点
            _loop = asyncio.get_running_loop()
            ret = await _loop.create_datagram_endpoint(lambda: self, sock=sock)
            self.transport, _ = ret
            self.running = True
            logging.info(
                f"UDP Discovery Service started on port {self.discovery_port}"
            )
        except (socket.error, OSError) as e:
            logging.error(f"Failed to start UDP Discovery Service: {e}")
            raise

    async def stop(self) -> None:
        """
        停止UDP发现服务
        """
        if not self.running:
            return
        self.running = False
        # 关闭传输层并等待连接完全关闭
        if self.transport is not None and not self.transport.is_closing():
            self.transport.close()
            self.close_fut = self.server.get_event_loop().create_future()
            try:
                await asyncio.wait_for(self.close_fut, 2.0)
            except asyncio.TimeoutError:
                pass
            self.close_fut = None
        logging.info("UDP Discovery Service stopped")

    def connection_made(self, transport: asyncio.transports.BaseTransport) -> None:
        """
        连接建立时的回调 - asyncio DatagramProtocol接口实现

        Args:
            transport: 传输层对象
        """
        self.transport = transport
        logging.debug("UDP Discovery Server Connected")

    def connection_lost(self, exc: Exception | None) -> None:
        """
        连接丢失时的回调 - asyncio DatagramProtocol接口实现

        Args:
            exc: 异常对象，如果没有异常则为None
        """
        logging.debug("UDP Discovery Server Disconnected")
        if self.close_fut is not None:
            self.close_fut.set_result(None)

    def _get_local_ip(self, client_addr: str) -> str:
        """
        获取与客户端通信的本地IP地址

        当配置的地址是0.0.0.0时，需要动态确定实际的本机IP地址
        通过创建临时UDP连接到客户端来确定本地接口IP

        Args:
            client_addr: 客户端IP地址

        Returns:
            实际的本地IP地址
        """
        # 如果配置的是具体IP地址，直接返回
        if self.response_ip != "0.0.0.0" and self.response_ip != "::":
            return self.response_ip

        # 配置的是0.0.0.0（所有接口），需要确定实际IP
        try:
            # 创建临时UDP socket
            temp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # 尝试连接到客户端地址（不发送数据，只为了确定本地接口）
                temp_sock.connect((client_addr, 80))
                local_ip = temp_sock.getsockname()[0]
            finally:
                temp_sock.close()
            return local_ip
        except Exception:
            # 失败时回退到使用主机名解析
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                # 最后回退到127.0.0.1
                return "127.0.0.1"

    def datagram_received(self, data: bytes, addr: Tuple[str | object, int]) -> None:
        """
        接收到UDP数据报时的回调 - 处理发现请求

        Args:
            data: 接收到的数据
            addr: 发送方地址 (IP, 端口)
        """
        if not self.running:
            return

        try:
            # 解码接收到的消息
            message = data.decode("utf-8").strip()
        except (UnicodeDecodeError, AttributeError):
            logging.debug(f"Received invalid data from {addr}")
            return

        # 检查是否为有效的发现请求
        if message != DISCOVERY_REQUEST:
            logging.debug(f"Received non-discovery message: {message} from {addr}")
            return

        logging.info(f"Received discovery request from {addr}")

        # 获取客户端IP地址
        client_ip = addr[0] if isinstance(addr[0], str) else str(addr[0])
        # 获取实际的本地IP地址
        actual_ip = self._get_local_ip(client_ip)

        # 构造响应消息: MOONRAKER_RESPONSE,<ip>,<port>
        response = f"{DISCOVERY_RESPONSE_PREFIX}{actual_ip},{self.response_port}"
        response_bytes = response.encode("utf-8")

        # 发送响应给请求方
        if self.transport is not None:
            try:
                self.transport.sendto(response_bytes, addr)
                logging.info(f"Sent discovery response to {addr}: {response}")
            except (socket.error, OSError) as e:
                logging.error(f"Failed to send discovery response to {addr}: {e}")

    def error_received(self, exc: Exception) -> None:
        """
        接收到错误时的回调 - asyncio DatagramProtocol接口实现

        Args:
            exc: 异常对象
        """
        logging.error(f"UDP Discovery Server Error: {exc}")

    async def close(self) -> None:
        """
        关闭组件 - 由Moonraker服务器关闭时调用
        """
        await self.stop()


def load_component(config: ConfigHelper) -> UDPDiscoveryServer:
    """
    组件加载函数 - Moonraker组件系统入口

    Args:
        config: 配置助手对象

    Returns:
        UDP发现服务器实例
    """
    return UDPDiscoveryServer(config)
