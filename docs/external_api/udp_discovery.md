# UDP 发现服务 API 文档

## 功能概述

UDP 发现服务允许客户端应用程序在局域网内自动发现 Moonraker 设备，无需预先知道设备的 IP 地址或端口。

### 核心功能

- **设备自动发现**: 客户端通过 UDP 广播自动发现局域网内的 Moonraker 设备
- **即插即用**: 无需手动配置 IP 地址，设备上电后即可被上位机发现
- **跨平台支持**: 支持 Windows、Linux、macOS 等各平台的客户端

### 业务价值

- **简化配置**: 用户无需手动输入 IP 地址和端口
- **提升用户体验**: 新用户开箱即用，降低使用门槛
- **多设备支持**: 局域网内可同时发现多个 Moonraker 设备
- **灵活切换**: 轻松在不同设备间切换

---

## 快速开始

### 第一步：配置服务

在 Moonraker 配置文件中添加以下配置：

```ini
[udp_discovery]
enable: true        # 启用 UDP 发现服务（默认启用）
port: 7126          # UDP 监听端口（默认 7126，一般无需修改）
```

### 第二步：理解工作流程

```
1. 客户端发送 UDP 广播 → "MOONRAKER_DISCOVERY" → 255.255.255.255:7126
2. Moonraker 收到请求 → 解析验证
3. Moonraker 响应 → "MOONRAKER_RESPONSE,<ip>,<port>" → 客户端
4. 客户端解析 → 获取 IP 和端口
5. 客户端建立 TCP 连接 → http://<ip>:<port>
```

---

## 详细配置说明

| 配置项 | 说明 | 推荐值 | 备注 |
|--------|------|--------|------|
| `enable` | 是否启用 UDP 发现服务 | true | 建议启用，方便设备发现 |
| `port` | UDP 监听端口 | 7126 | 仅在默认端口被占用时修改 |

> **提示**: 大多数情况下，只需添加 `[udp_discovery]` 配置段即可，所有选项都使用默认值。

---

## 协议规范

### 发现请求

**格式**: `MOONRAKER_DISCOVERY`

**发送方式**: UDP 广播到 `255.255.255.255:7126`

### 发现响应

**格式**: `MOONRAKER_RESPONSE,<ip>,<port>`

**示例**: `MOONRAKER_RESPONSE,192.168.1.100,7125`

**字段说明**:
- `MOONRAKER_RESPONSE`: 固定前缀
- `<ip>`: Moonraker 服务器的 IP 地址
- `<port>`: Moonraker HTTP 服务端口（默认 7125）

---

## 客户端实现示例

### Python 客户端示例

```python
import socket
import time

# UDP 发现配置
UDP_DISCOVERY_PORT = 7126
DISCOVERY_REQUEST = "MOONRAKER_DISCOVERY"
BROADCAST_ADDRESS = "255.255.255.255"
TIMEOUT = 5  # 超时时间（秒）

def discover_moonraker_devices():
    """发现局域网内的 Moonraker 设备"""
    print("正在搜索 Moonraker 设备...")

    # 创建 UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(TIMEOUT)

    devices = []

    try:
        # 发送发现请求
        sock.sendto(DISCOVERY_REQUEST.encode(), (BROADCAST_ADDRESS, UDP_DISCOVERY_PORT))
        print(f"已发送发现请求到 {BROADCAST_ADDRESS}:{UDP_DISCOVERY_PORT}")

        # 等待响应（可接收多个设备）
        start_time = time.time()
        while time.time() - start_time < TIMEOUT:
            try:
                data, addr = sock.recvfrom(1024)
                response = data.decode().strip()

                # 解析响应
                if response.startswith("MOONRAKER_RESPONSE,"):
                    parts = response.split(",")
                    if len(parts) == 3:
                        device_ip = parts[1]
                        device_port = int(parts[2])
                        devices.append({
                            "ip": device_ip,
                            "port": device_port,
                            "url": f"http://{device_ip}:{device_port}"
                        })
                        print(f"发现设备: {device_ip}:{device_port}")
            except socket.timeout:
                break

    finally:
        sock.close()

    return devices

# 使用示例
if __name__ == "__main__":
    devices = discover_moonraker_devices()

    if devices:
        print(f"\n共发现 {len(devices)} 个设备:")
        for i, device in enumerate(devices, 1):
            print(f"{i}. {device['url']}")
    else:
        print("\n未发现任何 Moonraker 设备")
```

### C# 客户端示例

```csharp
using System;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading.Tasks;

public class MoonrakerDiscovery
{
    private const int UdpDiscoveryPort = 7126;
    private const string DiscoveryRequest = "MOONRAKER_DISCOVERY";
    private const int Timeout = 5000; // 5 秒超时

    public async Task<Device[]> DiscoverDevicesAsync()
    {
        using (UdpClient client = new UdpClient())
        {
            client.EnableBroadcast = true;
            client.Client.ReceiveTimeout = Timeout;

            // 发送发现请求
            byte[] requestBytes = Encoding.UTF8.GetBytes(DiscoveryRequest);
            await client.SendAsync(requestBytes, requestBytes.Length,
                new IPEndPoint(IPAddress.Broadcast, UdpDiscoveryPort));

            Console.WriteLine($"已发送发现请求到 255.255.255.255:{UdpDiscoveryPort}");

            // 等待响应
            var devices = new System.Collections.Generic.List<Device>();
            var startTime = DateTime.Now;

            while ((DateTime.Now - startTime).TotalMilliseconds < Timeout)
            {
                try
                {
                    UdpReceiveResult result = await client.ReceiveAsync();
                    string response = Encoding.UTF8.GetString(result.Buffer).Trim();

                    if (response.StartsWith("MOONRAKER_RESPONSE,"))
                    {
                        string[] parts = response.Split(',');
                        if (parts.Length == 3)
                        {
                            devices.Add(new Device
                            {
                                Ip = parts[1],
                                Port = int.Parse(parts[2]),
                                Url = $"http://{parts[1]}:{parts[2]}"
                            });
                            Console.WriteLine($"发现设备: {parts[1]}:{parts[2]}");
                        }
                    }
                }
                catch (SocketException)
                {
                    break;
                }
            }

            return devices.ToArray();
        }
    }

    public class Device
    {
        public string Ip { get; set; }
        public int Port { get; set; }
        public string Url { get; set; }
    }
}
```

### C++ 客户端示例

```cpp
#include <iostream>
#include <string>
#include <vector>
#include <boost/asio.hpp>
#include <boost/bind/bind.hpp>

using boost::asio::ip::udp;
using namespace std;

class MoonrakerDiscovery {
private:
    const int UDP_DISCOVERY_PORT = 7126;
    const std::string DISCOVERY_REQUEST = "MOONRAKER_DISCOVERY";
    const int TIMEOUT = 5; // 秒

public:
    struct Device {
        string ip;
        int port;
        string url;

        Device(string i, int p) : ip(i), port(p),
            url("http://" + i + ":" + to_string(p)) {}
    };

    vector<Device> discoverDevices() {
        try {
            boost::asio::io_context io_context;

            // 创建 UDP socket
            udp::socket socket(io_context);
            socket.open(udp::v4());
            socket.set_option(boost::asio::socket_base::broadcast(true));

            // 发送发现请求
            udp::endpoint receiver(
                boost::asio::ip::address_v4::broadcast(), UDP_DISCOVERY_PORT);
            socket.send_to(boost::asio::buffer(DISCOVERY_REQUEST), receiver);

            cout << "已发送发现请求到 255.255.255.255:" << UDP_DISCOVERY_PORT << endl;

            // 接收响应
            vector<Device> devices;
            char recv_buffer[1024];
            udp::endpoint sender;

            // 设置超时（简化处理）
            for (int i = 0; i < 10; i++) {
                if (socket.available() > 0) {
                    size_t length = socket.receive_from(
                        boost::asio::buffer(recv_buffer), sender);
                    string response(recv_buffer, length);

                    if (response.find("MOONRAKER_RESPONSE,") == 0) {
                        size_t first_comma = response.find(',');
                        size_t second_comma = response.find(',', first_comma + 1);

                        string ip = response.substr(first_comma + 1,
                            second_comma - first_comma - 1);
                        int port = stoi(response.substr(second_comma + 1));

                        devices.emplace_back(ip, port);
                        cout << "发现设备: " << ip << ":" << port << endl;
                    }
                }
                this_thread::sleep_for(chrono::milliseconds(500));
            }

            return devices;
        }
        catch (exception& e) {
            cerr << "错误: " << e.what() << endl;
            return {};
        }
    }
};

int main() {
    MoonrakerDiscovery discovery;
    vector<MoonrakerDiscovery::Device> devices = discovery.discoverDevices();

    if (!devices.empty()) {
        cout << "\n共发现 " << devices.size() << " 个设备:" << endl;
        for (size_t i = 0; i < devices.size(); i++) {
            cout << (i + 1) << ". " << devices[i].url << endl;
        }
    } else {
        cout << "\n未发现任何 Moonraker 设备" << endl;
    }

    return 0;
}
```

---

## 典型应用场景

### 场景 1: 上位机自动发现设备

```
1. 用户启动上位机软件
2. 自动发送 UDP 发现请求
3. 等待响应并收集所有设备
4. 显示设备列表供用户选择
5. 用户选择设备后建立连接
```

### 场景 2: 多设备管理

```
1. 发送 UDP 发现请求
2. 接收多个设备的响应
3. 在界面显示所有设备及其状态
4. 允许用户快速切换设备
```

### 场景 3: 设备状态监控

```
1. 定期发送 UDP 发现请求
2. 检测局域网内设备的变化
3. 发现新设备时自动添加到列表
4. 设备离线时从列表移除
```

---

## 网络配置说明

### 防火墙配置

确保 Moonraker 所在设备的防火墙允许 UDP 端口 7126 的入站连接：

**Linux (iptables/ufw)**:
```bash
# 允许 UDP 7126 端口
sudo ufw allow 7126/udp

# 或使用 iptables
sudo iptables -A INPUT -p udp --dport 7126 -j ACCEPT
```

**Windows**:
```
控制面板 → Windows Defender 防火墙 → 高级设置
→ 入站规则 → 新建规则 → 端口 → UDP → 7126 → 允许连接
```

### 跨网段发现

UDP 广播默认只在同一子网内传播。如需跨网段发现设备，需要：

1. 配置路由器支持 UDP 广播转发（不推荐）
2. 或使用其他发现机制（如 mDNS/Zeroconf）

---

## 故障排除

### 问题 1: 无法发现设备

**可能原因及解决方案**:

| 原因 | 解决方法 |
|------|----------|
| Moonraker 未启动 | 检查 Moonraker 是否正在运行 |
| UDP 发现服务未启用 | 确认配置文件中 `[udp_discovery]` 段存在且 `enable: true` |
| 防火墙阻止 | 检查防火墙设置，允许 UDP 7126 端口 |
| 不在同一网段 | 确保客户端和 Moonraker 在同一局域网 |
| 端口被占用 | 检查 7126 端口是否被其他程序占用 |

### 问题 2: 发现设备但无法连接

**可能原因及解决方案**:

| 原因 | 解决方法 |
|------|----------|
| HTTP 服务未启动 | 检查 Moonraker HTTP 服务是否正常运行 |
| 端口配置错误 | 确认返回的端口是 Moonraker 实际监听的端口 |
| 网络隔离 | 检查 VLAN 或网络隔离配置 |

### 问题 3: 收到多个响应

这是正常情况，说明局域网内有多个 Moonraker 设备。客户端应该：
- 解析所有响应
- 显示设备列表供用户选择
- 允许用户切换不同设备

---

## 与其他发现机制的对比

| 发现方式 | 优点 | 缺点 | 适用场景 |
|----------|------|------|----------|
| **UDP 发现** | 简单、跨平台、无依赖 | 仅支持同网段 | 局域网设备发现 |
| **mDNS/Zeroconf** | 支持跨网段、标准化 | 配置复杂、依赖系统服务 | 企业网络环境 |
| **手动配置** | 可靠、明确 | 用户操作复杂 | 固定 IP 场景 |

> **建议**: UDP 发现适合大多数家庭和小型办公网络，简单易用。

---

## 安全注意事项

### 1. 仅局域网使用

UDP 发现设计用于可信的局域网环境，不暴露到公网。

### 2. 无身份验证

UDP 发现不包含身份验证机制，任何人都可以发送发现请求。

### 3. 网络隔离

在非可信网络中，建议：
- 使用防火墙限制 UDP 发现的访问范围
- 或禁用 UDP 发现服务

---

## 完整工作流程示例

### 客户端完整流程

```
┌─────────────────────────────────────────────────────────────┐
│                    客户端启动                                │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  创建 UDP Socket，设置广播和超时                            │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  发送: "MOONRAKER_DISCOVERY" → 255.255.255.255:7126        │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  等待响应（超时 5 秒）                                        │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  收到: "MOONRAKER_RESPONSE,192.168.1.100,7125"       │   │
│  │  解析: IP=192.168.1.100, Port=7125                   │   │
│  │  添加到设备列表                                       │   │
│  └─────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  收到: "MOONRAKER_RESPONSE,192.168.1.101,7125"       │   │
│  │  解析: IP=192.168.1.101, Port=7125                   │   │
│  │  添加到设备列表                                       │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  显示设备列表:                                               │
│  1. http://192.168.1.100:7125                               │
│  2. http://192.168.1.101:7125                               │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  用户选择设备 1                                              │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  建立 TCP 连接到 192.168.1.100:7125                         │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  连接成功，开始正常通信                                       │
└─────────────────────────────────────────────────────────────┘
```

---

## 测试工具

### 使用测试脚本

项目提供了一个测试脚本 `test_udp_discovery.py`：

```bash
# 运行测试脚本
python3 test_udp_discovery.py

# 预期输出
# 发送UDP发现广播到 255.255.255.255:7126
# 已发送: MOONRAKER_DISCOVERY
# 等待响应中 (超时时间 5秒)...
# 收到来自 ('192.168.1.100', 57134) 的响应: MOONRAKER_RESPONSE,192.168.1.100,7125
# 总共收到 1 个响应
```

### 使用 netcat (nc)

```bash
# 发送广播请求
echo "MOONRAKER_DISCOVERY" | nc -u -b 255.255.255.255 7126

# 监听响应（在另一个终端）
nc -u -l 7126
```
