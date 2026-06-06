# AI 检测外部接口

## 概述

Moonraker 当前对外提供 3 类 AI 检测能力：

- `floor_detect`：打印前底板检测
- `object_detect`：打印前异物 / 手部 / 底板异常检测
- `defect_detect`：打印中缺陷检测

当前运行时配置已经统一为：

- 4 个开关：
  - `enable`
  - `enable_floor_detect`
  - `enable_object_detect`
  - `enable_defect_detect`
- 3 个灵敏度：
  - `floor_sensitivity`
  - `object_sensitivity`
  - `defect_sensitivity`
- 1 个检测间隔：
  - `detect_interval`

以下旧字段已经移除，提交时会被拒绝：

- `obj_thresh`
- `nms_thresh`
- `sensitivity`
- `enable_foreign_detect`
- 所有 `auto_pause_*`

打印中缺陷检测的自动暂停不再对外开放配置。当 AI 后端返回 `warning=true` 时，Moonraker 会发送缺陷通知，并在打印中自动暂停。

## 配置文件

```ini
[ai_detection]
host: 127.0.0.1
port: 8765
auto_connect: True

enable: True
enable_floor_detect: True
enable_object_detect: True
enable_defect_detect: True

floor_sensitivity: medium
object_sensitivity: medium
defect_sensitivity: medium

detect_interval: 10

# 可选：类别名称映射
defect_class_0: spaghetti, Spaghetti-like defect
defect_class_1: zits, Zits/blobs
defect_class_2: stringing, Stringing

object_class_0: object, Foreign object on bed
object_class_1: hand, Hand detected
object_class_2: floor, Build plate anomaly
```

## 灵敏度映射

3 个灵敏度字段都使用同一组枚举值：

| 值 | 阈值 |
|---|---|
| `high` | `0.5` |
| `medium` | `0.65` |
| `low` | `0.8` |

Moonraker 内部会展开成：

- `floor_detect`：标量 `obj_thresh`
- `object_detect`：`{"0": x, "1": x, "2": x}`
- `defect_detect`：`{"0": x, "1": x, "2": x}`

`nms_thresh` 目前固定为内部值，不通过运行时接口对外暴露。

## 状态接口

### HTTP

- `GET /server/ai_detection/status`

### JSON-RPC

```json
{
  "jsonrpc": "2.0",
  "method": "server.ai_detection.status",
  "id": 1
}
```

### 示例响应

```json
{
  "state": "connected",
  "is_connected": true,
  "host": "127.0.0.1",
  "port": 8765,
  "stats": {
    "total_requests": 3,
    "successful_requests": 3,
    "failed_requests": 0,
    "last_request_time": 1710000000.0,
    "last_error": null,
    "connection_attempts": 1,
    "successful_connections": 1
  },
  "config": {
    "floor_sensitivity": "high",
    "object_sensitivity": "medium",
    "defect_sensitivity": "low",
    "effective_thresholds": {
      "floor_detect": 0.5,
      "object_detect": {"0": 0.65, "1": 0.65, "2": 0.65},
      "defect_detect": {"0": 0.8, "1": 0.8, "2": 0.8}
    },
    "enable": true,
    "enable_floor_detect": true,
    "enable_object_detect": true,
    "enable_defect_detect": true,
    "detect_interval": 10.0
  },
  "class_mappings": {
    "defect_classes": {
      "0": {"name": "spaghetti", "description": "Spaghetti-like defect"},
      "1": {"name": "zits", "description": "Zits/blobs"},
      "2": {"name": "stringing", "description": "Stringing"}
    },
    "object_classes": {
      "0": {"name": "object", "description": "Foreign object on bed"},
      "1": {"name": "hand", "description": "Hand detected"},
      "2": {"name": "floor", "description": "Build plate anomaly"}
    }
  },
  "is_detecting": false
}
```

## 配置更新接口

### HTTP

- `POST /server/ai_detection/config`

### JSON-RPC

```json
{
  "jsonrpc": "2.0",
  "method": "server.ai_detection.config",
  "params": {
    "enable": true,
    "enable_floor_detect": true,
    "enable_object_detect": true,
    "enable_defect_detect": true,
    "floor_sensitivity": "high",
    "object_sensitivity": "medium",
    "defect_sensitivity": "low",
    "detect_interval": 10
  },
  "id": 2
}
```

### 支持字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `enable` | boolean | AI 总开关 |
| `enable_floor_detect` | boolean | 打印前底板检测开关 |
| `enable_object_detect` | boolean | 打印前异物检测开关 |
| `enable_defect_detect` | boolean | 打印中缺陷检测开关 |
| `floor_sensitivity` | string | `high` / `medium` / `low` |
| `object_sensitivity` | string | `high` / `medium` / `low` |
| `defect_sensitivity` | string | `high` / `medium` / `low` |
| `detect_interval` | number | 缺陷检测轮询间隔，单位秒，最小 `1.0` |

### 示例响应

`notify_ai_detection_config_updated` 和配置更新接口的返回结构一致，例如：

```json
{
  "enable": true,
  "enable_floor_detect": true,
  "enable_object_detect": true,
  "enable_defect_detect": true,
  "detect_interval": 10.0,
  "floor_sensitivity": "high",
  "object_sensitivity": "medium",
  "defect_sensitivity": "low",
  "effective_thresholds": {
    "floor_detect": 0.5,
    "object_detect": {"0": 0.65, "1": 0.65, "2": 0.65},
    "defect_detect": {"0": 0.8, "1": 0.8, "2": 0.8}
  }
}
```

### 被拒绝的旧字段

请求里包含以下字段时，会返回 HTTP / JSON-RPC `400`：

- `obj_thresh`
- `nms_thresh`
- `sensitivity`
- `enable_foreign_detect`
- `auto_pause_on_defect`
- `auto_pause_on_spaghetti`
- `auto_pause_on_zits`
- `auto_pause_on_stringing`

## 通知

Moonraker 会发送：

- `notify_ai_detection_state_changed`
- `notify_ai_detection_detection_result`
- `notify_ai_detection_config_updated`

### 状态变化通知

```json
{
  "jsonrpc": "2.0",
  "method": "notify_ai_detection_state_changed",
  "params": [
    {
      "old_state": "connecting",
      "new_state": "connected"
    }
  ]
}
```

### 检测结果通知

#### 底板检测失败示例

```json
{
  "jsonrpc": "2.0",
  "method": "notify_ai_detection_detection_result",
  "params": [
    {
      "scenario": "preprint_floor",
      "detection_type": "floor",
      "message": "AI Detection: Build plate not detected"
    }
  ]
}
```

#### 异物检测失败示例

```json
{
  "jsonrpc": "2.0",
  "method": "notify_ai_detection_detection_result",
  "params": [
    {
      "scenario": "preprint_object",
      "detection_type": "object",
      "message": "AI Detection: Foreign object detected"
    }
  ]
}
```

#### 缺陷检测报警示例

```json
{
  "jsonrpc": "2.0",
  "method": "notify_ai_detection_detection_result",
  "params": [
    {
      "scenario": "printing_defect_periodic",
      "detection_type": "defect",
      "message": "Defect detected: spaghetti"
    }
  ]
}
```

## 运行时行为

### 打印前预检

Moonraker 在打印前会按下面顺序执行 AI 预检：

1. `floor_detect`
2. `object_detect`

如果 `floor_detect` 失败，会直接停止，不再继续执行 `object_detect`。

#### 底板检测成功条件

当前底板检测的成功判定只看是否检测到底板：

- `FloorDetectionResult.has_build_plate == true`

其中 `has_build_plate` 来自以下任一条件：

- 后端返回 `has_object = true`
- 后端返回的 `classes` 中包含 `2`

也就是说，**底板检测当前不再把 `is_valid` 当成成功的硬条件**。

#### 异物检测失败条件

`object_detect` 在以下情况下会判定失败：

- 检测到底板异常：`class=2`
- `is_valid == true` 且 `has_object == true`，并识别为：
  - 手部
  - 异物

### 打印中缺陷检测

当打印开始或恢复时，Moonraker 会按 `detect_interval` 周期调用 `defect_detect`。

如果 AI 后端返回 `warning=true`：

1. Moonraker 发送 `notify_ai_detection_detection_result`
2. 如果当前处于打印中，Moonraker 自动暂停打印

## 与打印启动接口的关系

AI 是否参与打印前预检，当前推荐由 AI 配置控制，而不是依赖
`printer.print.start` 的临时参数。

当前远程起打如果需要控制 AI，应使用：

- `server.ai_detection.config`

而不是在 `printer.print.start` 中传 AI 控制字段。

为兼容旧客户端，`printer.print.start` 仍接受：

- `skip_foreign_detect`

其语义保持旧版本行为：

- `skip_foreign_detect=true` 时，跳过 AI 预检
- 跳过范围包括底板检测和异物检测
- 耗材等其他预检仍继续执行

如果需要跳过整个预检查流程，应继续使用：

- `skip_precheck=true`
