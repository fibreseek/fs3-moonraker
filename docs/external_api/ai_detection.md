# AI 检测服务 API 文档

## 功能概述

AI 检测服务为 3D 打印机提供智能监控功能，通过连接 AI 检测服务器实现两大核心功能：

### 1. 打印缺陷检测（实时监控）
在打印过程中实时检测以下打印缺陷：
- **拉丝 (Stringing)**: 喷嘴移动时产生的细丝
- **疙瘩 (Zits)**: 表面凸起的小点
- **意面效应 (Spaghetti)**: 严重的打印失败，材料堆积成一团

### 2. 打印前异物检测（打印启动检查）
在开始打印前检测打印平台上是否有残留物或异物，避免打印失败。

### 业务价值
- **降低材料浪费**: 及时发现打印问题，避免继续浪费材料
- **提高打印成功率**: 打印前检查平台状态，确保良好的打印条件
- **减少人工监控**: 自动监控打印质量，无需人工盯守
- **远程监控支持**: 通过 WebSocket 实时推送检测结果和警告

---

## 快速开始

### 第一步：配置服务

在 Moonraker 配置文件中添加以下配置：

```ini
[ai_detection]
host: 127.0.0.1              # AI 服务器地址（通常是本地）
port: 8765                   # AI 服务器端口
auto_connect: True           # 启动时自动连接
default_obj_thresh: 0.35     # 检测灵敏度（0-1，数值越小越灵敏）
default_nms_thresh: 0.45     # 重复过滤阈值（0-1）

# 类别映射配置
# 格式: defect_class_<id>: <name>, <description>
# 缺陷检测类别
defect_class_0: spaghetti, Spaghetti-like defect
defect_class_1: zits, Zits/blobs
defect_class_2: stringing, Stringing
# 平台异物检测类别
foreign_class_0: object, Foreign object on bed
foreign_class_1: object, Foreign object on bed
foreign_class_2: hand, Hand detected
```

### 第二步：理解工作流程

```
1. 启动 Moonraker → 自动连接 AI 服务器
2. 打印前检查 → 调用异物检测 API
3. 打印过程中 → 定期调用缺陷检测 API
4. 发现问题 → WebSocket 推送警告通知
5. 上位机决策 → 暂停打印或继续监控
```

---

## 详细配置说明

| 配置项 | 说明 | 推荐值 | 备注 |
|--------|------|--------|------|
| `host` | AI 服务器地址 | 127.0.0.1 | 本地部署填 127.0.0.1 |
| `port` | AI 服务器端口 | 8765 | 与 AI 服务器配置保持一致 |
| `auto_connect` | 启动时自动连接 | True | 建议开启 |
| `reconnect_interval` | 断线重连间隔（秒） | 5.0 | 网络不稳定可适当增加 |
| `request_timeout` | 检测超时时间（秒） | 30.0 | AI 处理较慢可适当增加 |
| `default_obj_thresh` | 检测灵敏度 | 0.35 | 0.3-0.4 较为适中，可动态调整 |
| `default_nms_thresh` | 重复过滤阈值 | 0.45 | 一般无需修改 |
| `defect_class_<id>` | 缺陷类别映射 | - | 格式: `name, description` |
| `foreign_class_<id>` | 平台异物类别映射 | - | 格式: `name, description` |

> **提示**: `default_obj_thresh` 值越小检测越灵敏，但可能产生误报；值越大越严格，但可能漏检。建议根据实际效果调整。

### 类别映射配置

当 AI 模型更新添加新的检测类别时，可以通过配置文件添加类别映射，无需修改代码：

```ini
# 缺陷检测类别映射
defect_class_0: spaghetti, Spaghetti-like defect
defect_class_1: zits, Zits/blobs
defect_class_2: stringing, Stringing
defect_class_3: warping, Warping           # 新增类别
defect_class_4: layer_shift, Layer shift   # 新增类别

# 平台异物检测类别映射
foreign_class_0: object, Foreign object on bed
foreign_class_1: object, Foreign object on bed
foreign_class_2: hand, Hand detected
foreign_class_3: tool, Tool on bed        # 新增类别
```

> **提示**: 如果不配置类别映射，将使用内置的默认映射。

---

## 核心概念

- **JSON-RPC**: 通过 WebSocket 与 Moonraker 通信的协议
- **请求 (Request)**: 上位机向 Moonraker 发送的命令
- **响应 (Response)**: Moonraker 返回的结果
- **通知 (Notification)**: Moonraker 主动推送的消息（无需请求）

---

## 常用 API 接口

### 1. 查询服务状态

**用途**: 检查 AI 检测服务是否正常运行，查看统计信息

**JSON-RPC 请求**:
```json
{
    "jsonrpc": "2.0",
    "method": "server.ai_detection.status",
    "id": 1
}
```

**返回示例**:
```json
{
    "state": "connected",
    "is_connected": true,
    "host": "127.0.0.1",
    "port": 8765,
    "stats": {
        "total_requests": 10,
        "successful_requests": 9,
        "failed_requests": 1,
        "last_error": null
    },
    "config": {
        "obj_thresh": 0.35,
        "nms_thresh": 0.45
    },
    "class_mappings": {
        "defect_classes": {
            "0": {"name": "spaghetti", "description": "Spaghetti-like defect"},
            "1": {"name": "zits", "description": "Zits/blobs"},
            "2": {"name": "stringing", "description": "Stringing"}
        },
        "foreign_classes": {
            "0": {"name": "object", "description": "Foreign object on bed"},
            "1": {"name": "object", "description": "Foreign object on bed"},
            "2": {"name": "hand", "description": "Hand detected"}
        }
    }
}
```

---

### 2. 查询类别映射

**用途**: 获取当前配置的检测类别映射

**JSON-RPC 请求**:
```json
{
    "jsonrpc": "2.0",
    "method": "server.ai_detection.class_mappings",
    "id": 2
}
```

**返回示例**:
```json
{
    "defect_classes": {
        "0": {"name": "spaghetti", "description": "Spaghetti-like defect"},
        "1": {"name": "zits", "description": "Zits/blobs"},
        "2": {"name": "stringing", "description": "Stringing"}
    },
    "foreign_classes": {
        "0": {"name": "object", "description": "Foreign object on bed"},
        "1": {"name": "object", "description": "Foreign object on bed"},
        "2": {"name": "hand", "description": "Hand detected"}
    }
}
```

**使用场景**:
- 上位机启动时获取类别信息，用于 UI 显示
- 验证配置的类别映射是否正确
- 支持动态适配 AI 模型的类别变化

---

### 3. 调整检测灵敏度（动态配置）

**用途**: 在运行过程中调整检测灵敏度，无需重启服务

**JSON-RPC 请求**:
```json
{
    "jsonrpc": "2.0",
    "method": "server.ai_detection.config",
    "params": {
        "obj_thresh": 0.4,
        "nms_thresh": 0.5
    },
    "id": 3
}
```

**使用场景**:
- 误报太多 → 增大 `obj_thresh` 到 0.4-0.5
- 漏检严重 → 减小 `obj_thresh` 到 0.25-0.3

**返回示例**:
```json
{
    "obj_thresh": 0.4,
    "nms_thresh": 0.5
}
```

---

### 4. 打印缺陷检测（核心功能）

**用途**: 检测打印过程中是否出现缺陷（拉丝、疙瘩、意面效应）

**JSON-RPC 请求**:
```json
{
    "jsonrpc": "2.0",
    "method": "server.ai_detection.detect.defect",
    "params": {
        "obj_thresh": 0.35,
        "nms_thresh": 0.45,
        "is_lite": false
    },
    "id": 4
}
```

**参数说明**:
- `obj_thresh`: 可选，目标检测阈值 (0.0-1.0)，不提供时使用配置默认值
- `nms_thresh`: 可选，NMS 阈值 (0.0-1.0)，不提供时使用配置默认值
- `is_lite`: 可选，是否使用 lite 模型（默认 false，使用正常模型）

**返回示例**:
```json
{
    "is_valid": true,
    "boxes": [[209.5, 173.75, 667.5, 513.25]],
    "classes": [2],
    "class_names": ["stringing"],
    "scores": [0.456],
    "warning": true
}
```

**字段说明**:
- `is_valid`: 检测是否成功
- `boxes`: 缺陷位置（格式: [x1, y1, x2, y2]）
- `classes`: 缺陷类型编号（由配置的类别映射定义）
- `class_names`: 缺陷类型名称（从类别映射中获取）
- `scores`: 检测置信度 (0-1)
- `warning`: 是否触发警告（往前追溯记录判断）

**默认类别说明**:
| 类别 ID | 名称 | 说明 |
|---------|------|------|
| 0 | spaghetti | 意面效应 |
| 1 | zits | 疙瘩/斑点 |
| 2 | stringing | 拉丝 |

> **提示**: 类别可通过配置文件自定义或扩展。

**结果解读**:
- `warning: false` → 打印正常，无缺陷
- `warning: true` → 检测到缺陷，建议暂停或人工确认
- `classes: [0]` → 意面效应
- `classes: [1]` → 疙瘩
- `classes: [2]` → 拉丝

---

### 5. 打印前异物检测

**用途**: 在开始打印前检查平台是否干净

**JSON-RPC 请求**:
```json
{
    "jsonrpc": "2.0",
    "method": "server.ai_detection.detect.foreign",
    "params": {
        "obj_thresh": 0.35,
        "nms_thresh": 0.45
    },
    "id": 5
}
```

**返回示例**:
```json
{
    "is_valid": true,
    "has_object": true,
    "scores": [0.456],
    "boxes": [[209.5, 173.75, 667.5, 513.25]],
    "classes": [1],
    "class_names": ["object"]
}
```

**字段说明**:
- `is_valid`: 检测是否成功
- `has_object`: 平台上是否有异物
- `scores`: 检测置信度
- `boxes`: 异物位置
- `classes`: 异物类型编号（由配置的类别映射定义）
- `class_names`: 异物类型名称（从类别映射中获取）

**默认类别说明**:
| 类别 ID | 名称 | 说明 |
|---------|------|------|
| 0 | object | 底板异物 |
| 1 | object | 底板异物 |
| 2 | hand | 检测到手 |

> **提示**: 类别可通过配置文件自定义或扩展。

**结果解读**:
- `has_object: false` → 平台干净，可以开始打印
- `has_object: true` → 发现异物，建议清理后再打印
- `classes: [2]` → 检测到手，请离开平台

---

### 6. 手动连接/断开 AI 服务

**用途**: 在不重启 Moonraker 的情况下，手动控制与 AI 服务器的连接

**连接请求**:
```json
{
    "jsonrpc": "2.0",
    "method": "server.ai_detection.connect",
    "id": 6
}
```

**断开请求**:
```json
{
    "jsonrpc": "2.0",
    "method": "server.ai_detection.disconnect",
    "id": 7
}
```

**使用场景**:
- AI 服务器维护时主动断开
- AI 服务器重启后重新连接
- 临时禁用检测功能

---

## 实时通知（WebSocket 推送）

> **重要**: 以下通知由 Moonraker **主动推送**给上位机，无需上位机请求

### 通知 1: 连接状态变化

**触发时机**: AI 服务器连接状态改变时（连接成功/断开/重连等）

**通知内容**:
```json
{
    "jsonrpc": "2.0",
    "method": "notify_ai_detection_state_changed",
    "params": [{
        "old_state": "disconnected",
        "new_state": "connected"
    }]
}
```

**状态说明**:
- `disconnected` - 已断开
- `connecting` - 正在连接
- `connected` - 已连接
- `reconnecting` - 正在重连

---

### 通知 2: 检测结果（每次检测后推送）

**触发时机**: 每次调用检测 API 后，会推送检测结果

**通知内容**:
```json
{
    "jsonrpc": "2.0",
    "method": "notify_ai_detection_detection_result",
    "params": [{
        "type": "defect_detect",
        "result": {
            "is_valid": true,
            "boxes": [[209.5, 173.75, 667.5, 513.25]],
            "classes": [2],
            "class_names": ["stringing"],
            "scores": [0.456],
            "warning": true
        }
    }]
}
```

**用途**: 上位机可以同时接收检测结果，用于记录日志、更新 UI 等

---

### 通知 3: 警告通知（发现问题时触发）

**触发时机**: 检测到缺陷或异物时，会额外发送一条警告通知

**通知内容**:
```json
{
    "jsonrpc": "2.0",
    "method": "notify_ai_detection_warning",
    "params": [{
        "type": "defect_detect",
        "message": "Defect warning triggered",
        "result": {
            "is_valid": true,
            "boxes": [[209.5, 173.75, 667.5, 513.25]],
            "classes": [2],
            "class_names": ["stringing"],
            "scores": [0.456],
            "warning": true
        }
    }]
}
```

**处理建议**:
1. 收到警告 → 弹出提示或播放警报音
2. 显示缺陷类型和位置
3. 询问用户：继续打印 / 暂停检查 / 停止打印
4. 记录到日志系统

---

## 架构说明

### Moonraker AI Detection 模块的职责
**只负责检测和推送**：
- 连接到 AI 服务器
- 执行检测请求
- 返回检测结果
- 通过 WebSocket 推送通知

### 上位机的职责
**实现业务决策逻辑**：
- 监听 WebSocket 通知
- 根据检测结果做出决策
- 控制打印机行为（暂停/停止/继续）
- 与用户交互（弹窗/警报等）

---

## 典型应用场景

### 场景 1: 打印启动前检查
上位机应该：
```
1. 用户点击"开始打印"按钮
2. 调用 server.ai_detection.detect.foreign API
3. 等待响应
4. 如果 has_object = true:
   → 显示提示："请清理打印平台"
   → 阻止打印启动
5. 如果 has_object = false:
   → 允许打印启动
```

### 场景 2: 打印过程监控
上位机应该：
```
1. 打印开始后，定时调用 server.ai_detection.detect.defect API
2. 监听 notify_ai_detection_warning 推送通知
3. 收到警告时:
   → 根据缺陷类型决策：
     - 意面效应 (class=0) → 自动暂停打印
     - 拉丝/疙瘩 (class=1,2) → 弹窗提示用户
4. 用户确认后继续或停止打印
```

### 场景 3: 灵敏度自动调整
上位机可以：
```
1. 记录检测的误报率和漏检率
2. 根据统计数据调整阈值
3. 调用 server.ai_detection.config API 动态更新
4. 无需重启 Moonraker
```

---

## 错误处理说明

所有 API 请求失败时，会返回标准错误格式：

```json
{
    "error": {
        "code": 503,
        "message": "AI Detection service not connected"
    }
}
```

### 常见错误及处理方法

| 错误码 | 错误说明 | 可能原因 | 解决方法 |
|--------|----------|----------|----------|
| 503 | 服务不可用 | AI 服务器未连接 | 1. 检查 AI 服务器是否启动<br>2. 检查配置中的 host 和 port<br>3. 调用连接 API 重新连接 |
| 504 | 请求超时 | AI 处理时间过长 | 1. 增加 `request_timeout` 配置<br>2. 检查 AI 服务器负载<br>3. 优化图像分辨率 |
| 500 | 服务器错误 | AI 服务器内部错误 | 1. 查看 AI 服务器日志<br>2. 检查输入参数是否正确<br>3. 联系技术支持 |


