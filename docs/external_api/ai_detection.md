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

**重要说明**：打印前异物检测机制：
- 在打印启动前进行异物检测
- 如果检测到异物，**阻止打印启动**并返回 403 错误
- 用户需清理平台后重新发起打印
- 如果检测失败或服务不可用，记录警告但继续打印（容错机制）

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
# WebSocket 连接配置
host: 127.0.0.1              # AI 服务器地址（通常是本地）
port: 8765                   # AI 服务器端口
auto_connect: True           # 启动时自动连接

# 检测参数
default_obj_thresh: 0.35     # 检测灵敏度（0-1，数值越小越灵敏）
default_nms_thresh: 0.45     # 重复过滤阈值（0-1）

# AI总开关
enable: True                 # 总开关，设为 False 可禁用所有 AI 功能

# 检测开关
enable_foreign_detect: True  # 是否启用打印前异物检测
enable_defect_detect: True   # 是否启用打印中缺陷检测

# 检测参数
detect_interval: 10          # 缺陷检测间隔（秒）

# 自动暂停配置
auto_pause_on_defect: True        # 检测到缺陷时是否自动暂停（总开关）
auto_pause_on_spaghetti: True     # 意面效应自动暂停
auto_pause_on_zits: False         # 疙瘩仅提示，不自动暂停
auto_pause_on_stringing: False    # 拉丝仅提示，不自动暂停

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
2. 用户发起打印 → Moonraker 自动执行异物检测
3. 检测到异物 → 阻止打印启动 + 返回 403 错误
4. 上位机收到错误 → 显示提示用户清理平台
5. 用户清理后 → 重新发起打印
6. 无异物 → 继续启动打印
7. 打印开始 → Moonraker 自动启动缺陷检测定时器
8. 检测到缺陷 → 根据配置自动暂停或仅提示
```

> **提示**: 用户可通过 `skip_foreign_detect=true` 参数跳过异物检测，直接启动打印。适用于已确认平台清洁的场景。

---

## 详细配置说明

| 配置项 | 说明 | 推荐值 | 备注 |
|--------|------|--------|------|
| `host` | AI 服务器地址 | 127.0.0.1 | 本地部署填 127.0.0.1 |
| `port` | AI 服务器端口 | 8765 | 与 AI 服务器配置保持一致 |
| `auto_connect` | 启动时自动连接 | True | 建议开启 |
| `reconnect_interval` | 断线重连间隔（秒） | 5.0 | 网络不稳定可适当增加 |
| `request_timeout` | 检测超时时间（秒） | 30.0 | AI 处理较慢可适当增加 |

### 检测开关配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `enable` | AI 总开关，控制所有 AI 功能 | True |
| `enable_foreign_detect` | 是否启用打印前异物检测 | True |
| `enable_defect_detect` | 是否启用打印中缺陷检测 | True |
| `detect_interval` | 缺陷检测间隔（秒） | 10 |

> **提示**: 这些开关可通过 API 在运行时动态修改。当 `enable` 设为 `False` 时，所有 AI 检测功能都会被禁用，无论其他开关如何设置。

### 检测参数配置

| 配置项 | 说明 | 推荐值 | 备注 |
|--------|------|--------|------|
| `default_obj_thresh` | 检测灵敏度 | 0.35 | 0.3-0.4 较为适中，可动态调整 |
| `default_nms_thresh` | 重复过滤阈值 | 0.45 | 一般无需修改 |
| `defect_class_<id>` | 缺陷类别映射 | - | 格式: `name, description` |
| `foreign_class_<id>` | 平台异物类别映射 | - | 格式: `name, description` |

> **提示**: `default_obj_thresh` 值越小检测越灵敏，但可能产生误报；值越大越严格，但可能漏检。建议根据实际效果调整。

### 自动暂停配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `auto_pause_on_defect` | 是否启用自动暂停（总开关） | True |
| `auto_pause_on_spaghetti` | 意面效应是否自动暂停 | True |
| `auto_pause_on_zits` | 疙瘩是否自动暂停 | False |
| `auto_pause_on_stringing` | 拉丝是否自动暂停 | False |

> **重要说明**: 自动暂停的触发条件是 AI 后端返回的 `warning` 字段为 `true`。AI 后端采用连续检测机制，只有往前追溯6次，6次里面有4次检测到就报警，将 `warning` 设置为 `true`，从而触发自动暂停。
>
> **流程**: 检测到缺陷 → AI 后端连续计数 → 往前追溯6次，6次里面有4次后 `warning=true` → Moonraker 自动暂停

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
- **检测场景 (Scenario)**: 标识检测发生的场景，帮助上位机区分通知来源

### 检测场景类型

| 场景值 | 说明 | 上位机处理建议 |
|--------|------|---------------|
| `preprint_foreign_object` | 打印前异物检测 | 检测到异物时阻止打印启动，需提示用户清理平台 |
| `printing_defect_periodic` | 打印中缺陷检测（定时） | 检测到缺陷时根据配置自动暂停或仅提示 |

---

## API 接口

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
        "reconnect_interval": 5.0,
        "request_timeout": 30.0,
        "auto_connect": true,
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
    },
    "coordinator": {
        "config": {
            "enable": true,
            "enable_foreign_detect": true,
            "enable_defect_detect": true,
            "detect_interval": 10.0,
            "auto_pause_on_defect": true,
            "auto_pause_on_spaghetti": true,
            "auto_pause_on_zits": false,
            "auto_pause_on_stringing": false
        },
        "is_detecting": false
    }
}
```

---

### 2. 更新配置（统一管理）

**用途**: 动态更新检测和协调器配置，无需重启服务

**JSON-RPC 请求**:
```json
{
    "jsonrpc": "2.0",
    "method": "server.ai_detection.config",
    "params": {
        "obj_thresh": 0.4,
        "enable": true,
        "detect_interval": 15,
        "auto_pause_on_spaghetti": true
    },
    "id": 2
}
```

**支持的配置项**:
- `enable`: AI 总开关（控制所有 AI 功能）
- `obj_thresh`: 检测灵敏度 (0.0-1.0)
- `nms_thresh`: 重复过滤阈值 (0.0-1.0)
- `enable_foreign_detect`: 是否启用异物检测
- `enable_defect_detect`: 是否启用缺陷检测
- `detect_interval`: 检测间隔（秒，最小值 1.0）
- `auto_pause_on_defect`: 是否自动暂停（总开关）
- `auto_pause_on_spaghetti`: 意面效应自动暂停
- `auto_pause_on_zits`: 疙瘩自动暂停
- `auto_pause_on_stringing`: 拉丝自动暂停

**返回示例**:
```json
{
    "enable": true,
    "obj_thresh": 0.4,
    "nms_thresh": 0.45,
    "enable_foreign_detect": true,
    "enable_defect_detect": true,
    "detect_interval": 15.0,
    "auto_pause_on_defect": true,
    "auto_pause_on_spaghetti": true,
    "auto_pause_on_zits": false,
    "auto_pause_on_stringing": false
}
```

**使用场景**:
- 误报太多 → 增大 `obj_thresh` 到 0.4-0.5
- 漏检严重 → 减小 `obj_thresh` 到 0.25-0.3
- 临时禁用 AI → 设置 `enable: false`
- 调整检测频率 → 修改 `detect_interval`

> **配置持久化**: 通过此 API 更新的配置会自动保存到 Moonraker 数据库，重启后配置仍然生效。配置文件中的值作为默认值，数据库中的值会覆盖配置文件的值。

---

## 实时通知（WebSocket 推送）

> **重要**: 以下通知由 Moonraker **主动推送**给上位机，无需上位机请求
>
> **注意**: 通知中包含 `scenario` 字段，用于标识检测场景，帮助上位机区分通知来源

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

### 通知 2: 检测结果

**触发时机**: 每次检测后，会推送检测结果

**打印中缺陷检测**: 
```json
{
    "jsonrpc": "2.0",
    "method": "notify_ai_detection_detection_result",
    "params": [{
        "scenario": "printing_defect_periodic",
        "detection_type": "defect",
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

**打印前异物检测**:
```json
{
    "jsonrpc": "2.0",
    "method": "notify_ai_detection_detection_result",
    "params": [{
        "scenario": "preprint_foreign_object",
        "detection_type": "foreign_object",
        "result": {
            "is_valid": true,
            "has_object": true,
            "scores": [0.89],
            "boxes": [[100, 150, 300, 400]],
            "classes": [1],
            "class_names": ["object"]
        }
    }]
}
```

**字段说明**:
| 字段 | 类型 | 说明 |
|-----|------|------|
| `scenario` | string | 检测场景，见上方场景类型表 |
| `detection_type` | string | 检测类型：`defect` 或 `foreign_object` |
| `result` | object | 检测结果详情 |

**用途**: 上位机可以根据 `scenario` 字段区分：
- 打印前的异物检测（检测到异物时阻止启动，需提示用户清理平台）
- 打印中的缺陷检测（需要显示警告或已自动暂停）

**触发条件**:
- 打印前异物检测：始终发送通知
- 打印中缺陷检测：**仅在 `warning=true` 时发送通知**

---

### 通知 3: 配置更新

**触发时机**: 当通过 API 更新配置后

**通知内容**:
```json
{
    "jsonrpc": "2.0",
    "method": "notify_ai_detection_config_updated",
    "params": [{
        "enable": true,
        "enable_foreign_detect": true,
        "enable_defect_detect": true,
        "detect_interval": 15.0,
        "auto_pause_on_defect": true,
        "auto_pause_on_spaghetti": true,
        "auto_pause_on_zits": false,
        "auto_pause_on_stringing": false,
        "obj_thresh": 0.4,
        "nms_thresh": 0.45
    }]
}
```

**字段说明**:
| 字段 | 类型 | 说明 |
|-----|------|------|
| `enable` | boolean | AI 总开关 |
| `enable_foreign_detect` | boolean | 异物检测开关 |
| `enable_defect_detect` | boolean | 缺陷检测开关 |
| `detect_interval` | number | 检测间隔（秒） |
| `auto_pause_on_defect` | boolean | 缺陷时自动暂停（总开关） |
| `auto_pause_on_spaghetti` | boolean | 意面效应自动暂停 |
| `auto_pause_on_zits` | boolean | 疙瘩自动暂停 |
| `auto_pause_on_stringing` | boolean | 拉丝自动暂停 |
| `obj_thresh` | number | 检测阈值 (0.0-1.0) |
| `nms_thresh` | number | NMS阈值 (0.0-1.0) |

**用途**: 上位机可以实时同步配置状态到 UI，多个客户端之间保持配置一致，避免轮询状态接口。

---

## 架构说明

AI 检测模块采用单一组件架构设计：

| 组件 | 文件 | 职责 |
|------|------|------|
| **AIDetection** | ai_detection.py | WebSocket 连接管理、检测调度、通知发送、自动暂停逻辑 |
| **集成层** | KlippyAPIs | klippy_apis.py | 打印流程集成、打印前异物检测 |

### 功能模块

- **通信层**: WebSocket 连接管理、检测请求发送、响应处理
- **调度层**: 作业状态监听、定时检测循环、检测开关管理
- **业务层**: 检测结果处理、自动暂停决策、通知发送
- **配置层**: 统一配置管理、动态配置更新
- 所有通知都带有 `scenario` 字段标识检测场景，上位机可据此区分通知来源

## 打印工作流程

1. **用户发起打印** → 调用 `POST /printer/print/start`（可选 `skip_foreign_detect=true` 跳过异物检测）
2. **打印前异物检测** → 检测到异物则阻止启动（返回 403 错误）
3. **启动打印** → 执行 `SDCARD_PRINT_FILE` 命令
4. **打印开始** → 自动启动缺陷检测定时器
5. **缺陷检测** → 每隔 `detect_interval` 秒检测一次
6. **检测到缺陷** → 根据配置自动暂停或仅提示

---

## 错误处理

### 常见错误

| 错误码 | 错误信息 | 原因 | 解决方案 |
|---------|-----------|------|----------|
| 400 | Invalid params format, must be JSON object | 请求格式错误 | 检查请求 JSON 格式 |
| 400 | No configuration parameters provided | 没有有效的配置参数 | 检查参数名称 |
| 400 | `<name>` must be a number | 参数类型错误 | 确保数字类型 |
| 400 | `<name>` must be between 0.0 and 1.0 | 参数超出范围 | 检查数值范围 |
| 403 | Foreign object detected, please confirm before printing | 检测到异物 | 清理平台后重试，或使用 `skip_foreign_detect=true` 强制启动 |
| 503 | AI Detection client not available | 协调器未初始化 | 检查配置 |
| 503 | AI Detection connection closed | 连接丢失 | 检查 AI 服务器是否运行 |
| 504 | AI Detection request timed out | 请求超时 | 检查 AI 服务器是否响应 |

---

## 最佳实践

### 1. 监控连接状态

监听 `notify_ai_detection_state_changed` 通知来跟踪 AI 服务器连接状态。

### 2. 处理检测结果

根据 `scenario` 字段处理不同的检测场景：

```javascript
moonraker.on("notify_ai_detection_detection_result", (data) => {
    const { scenario, result } = data[0];

    if (scenario === "preprint_foreign_object") {
        // 打印前异物检测 - 打印已被阻止
        showForeignObjectWarning(result);
    } else if (scenario === "printing_defect_periodic") {
        // 打印中缺陷检测
        showDefectWarning(result);
    }
});
```

### 3. 动态调整配置

根据实际效果调整检测灵敏度：
- 误报太多 → 增大 `obj_thresh` 到 0.4-0.5
- 漏检严重 → 减小 `obj_thresh` 到 0.25-0.3

---

## 附录

### 缺陷检测结果格式

```json
{
    "is_valid": true,
    "boxes": [[x1, y1, x2, y2], ...],
    "classes": [class_id1, class_id2, ...],
    "class_names": ["class_name1", "class_name2", ...],
    "scores": [score1, score2, ...],
    "warning": true/false
}
```

### 异物检测结果格式

```json
{
    "is_valid": true,
    "has_object": true/false,
    "scores": [score1, score2, ...],
    "boxes": [[x1, y1, x2, y2], ...],
    "classes": [class_id1, class_id2, ...],
    "class_names": ["class_name1", "class_name2", ...]
}
```

### 坐标说明

- `x1, y1`: 左上角坐标
- `x2, y2`: 右下角坐标
- 坐标系原点 (0,0) 在图像左上角
