#!/usr/bin/env python3
"""
UDP Discovery Service 测试脚本

此脚本发送 MOONRAKER_DISCOVERY 广播并等待响应
"""
import socket
import sys

# UDP发现服务端口
UDP_DISCOVERY_PORT = 7126
# 发现请求消息
DISCOVERY_REQUEST = "MOONRAKER_DISCOVERY"
# 广播地址
BROADCAST_ADDRESS = "255.255.255.255"
# 等待响应的超时时间（秒）
TIMEOUT = 5


def test_discovery():
    """发送UDP广播发现请求并等待响应"""
    print(f"发送UDP发现广播到 {BROADCAST_ADDRESS}:{UDP_DISCOVERY_PORT}")

    # 创建UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # 允许广播
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # 设置超时时间
    sock.settimeout(TIMEOUT)

    try:
        # 发送发现请求
        sock.sendto(DISCOVERY_REQUEST.encode(), (BROADCAST_ADDRESS, UDP_DISCOVERY_PORT))
        print(f"已发送: {DISCOVERY_REQUEST}")

        # 等待响应
        print(f"\n等待响应中 (超时时间 {TIMEOUT}秒)...")
        responses = []

        while True:
            try:
                data, addr = sock.recvfrom(1024)
                response = data.decode().strip()
                responses.append((response, addr))
                print(f"收到来自 {addr} 的响应: {response}")
            except socket.timeout:
                break

        if responses:
            print(f"\n总共收到 {len(responses)} 个响应")
            return 0
        else:
            print("\n未收到任何响应")
            print("请确保Moonraker正在运行并且已启用UDP发现服务")
            return 1

    except Exception as e:
        print(f"错误: {e}")
        return 1
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(test_discovery())
