# Moonraker 错误索引插件使用说明

## 1. 插件简介

本插件用于实时监控 Moonraker/Klipper 的 G-code 输出，识别错误信息，并根据预定义的 **xlsx 错误数据库** 提供多语言错误描述和解决方案。

主要功能包括：
- **实时错误检测**：监控以 "!!" 开头的标准错误及 webhooks 状态消息。
- **错误索引系统**：基于 xlsx 文件（含占位符 %1、%2…）提供多语言描述与解决方案。
- **通知推送**：检测到错误时通过 WebSocket 推送 `error_indexer:error_detected`，含错误码、描述、解决方案链接。
- **多语言支持**：支持多语言错误描述（ISO 639-1 + ISO 3166-1，如 zh-CN、en-US），语言设置从 `fibretouch.language` 动态获取。

**依赖**：需安装 `openpyxl` 以读取 xlsx（如 `pip install openpyxl`）。

---

## 2. 安装与配置

### 2.1 安装插件

1. 将 `error_indexer.py` 放到 Moonraker 组件目录，例如：
   ```bash
   cp error_indexer.py /path/to/moonraker/moonraker/components/
   ```
2. 确保已安装 openpyxl：`pip install openpyxl`

### 2.2 准备错误数据库

将错误表 **xlsx** 放到可访问路径（如 Moonraker 同目录下的 `error_info.xlsx`）。xlsx 内需包含名为 **klipper** 的表格（可在配置中改为其他名称）。

### 2.3 配置文件 (moonraker.conf)

在 `moonraker.conf` 中增加 `[error_indexer]` 段即可启用。以下为可选配置项：

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `xlsx_path` | 错误数据库 xlsx 文件路径（推荐） | 组件目录上级的 `error_info.xlsx` |
| `file_path` | 同 xlsx 路径，与 `xlsx_path` 二选一 | 同上 |
| `csv_path` | 兼容旧配置的路径键，优先级低于上述两项 | 同上 |
| `sheet_name` | xlsx 内要读取的表格名称 | `klipper` |
| `ai_sheet_name` | AI 错误的表格名称 | `ai` |

**示例**：

```ini
[error_indexer]
xlsx_path: /home/pi/printer_data/config/error_info.xlsx
sheet_name: klipper
```

仅使用默认路径时也可只写：

```ini
[error_indexer]
```

配置完成后重启 Moonraker 使插件生效。

---

## 3. 错误数据库 (xlsx) 格式规范

- 使用 **Excel xlsx**，内含至少一个表格；默认读取名为 **klipper** 的表格（可由 `sheet_name` 修改）。
- **第一行为表头，会被跳过**；从第二行起为数据行。
- 列含义如下（列索引从 0 起）：

| 列索引 | 说明 | 示例 |
| :---: | :--- | :--- |
| 0 | 错误代码（唯一） | `10052` |
| 1 | 匹配模板，占位符为 %1、%2、%3… | `Move out of range on %1 axis: %2` |
| 2 | 多语言描述（每行一种语言） | 见下方多语言格式 |
| 3 | 解决方法：多语言文本或单个 http(s) 链接 | 同多语言格式或 `https://...` |

**第二列（匹配模板）**：
- 与 Klipper 实际输出一致，可变部分用 `%1`、`%2`、`%3`… 表示。
- 匹配时会按顺序捕获实际值，并回填到第三列各语言描述中的相同 `%1`、`%2`…

**多语言格式（第三列、第四列）**：每行一种语言，格式为 `语言代码：内容`，支持全角冒号 `：`。

```text
zh-CN：右喷嘴偏移值校准失败
en-US：Right nozzle offset calibration failed
zh-TW：右噴嘴偏移值校準失敗
ja-JP：右ノズルのオフセットキャリブレーションに失敗しました
```

语言代码需为 **xx-XX**（如 zh-CN、en-US）。描述中可使用与第二列相同的占位符（如 %1、%2），推送通知时会用本次匹配到的实际值替换。

---

## 4. 语言设置

### 4.1 设置语言

本插件从 `fibretouch.language` 动态获取语言设置，默认为 `en-US`。

**通过 printer_setting API 设置语言**：

```json
{
  "jsonrpc": "2.0",
  "method": "printer.set_setting",
  "params": {
    "fibretouch": {
      "language": "zh-CN"
    }
  },
  "id": 1
}
```

**订阅语言变化通知**：

```json
{
  "jsonrpc": "2.0",
  "method": "printer.subscribe_setting",
  "params": {
    "fibretouch": []
  },
  "id": 2
}
```

语言变化时会收到 `notify_printer_setting_changed` 通知。

---

## 5. 通知格式

检测到错误时，插件会发送 WebSocket 通知 **`error_indexer:error_detected`**（JSON-RPC 中为 `notify_error_detected`）。

**Payload 示例**：

```json
{
  "jsonrpc": "2.0",
  "method": "notify_error_detected",
  "params": [
    {
      "error_code": "10052",
      "error_message": "Move out of range on x axis: -1000.000",
      "description": "在 x 轴上移动超出范围：-1000.000",
      "timestamp": 1773029719.7468395,
      "solution_link": "https://...",
      "original_response": "!! Move out of range on x axis: -1000.000"
    }
  ]
}
```

- `error_code`：表格第一列错误码；未匹配时为 `UNKNOWN`。
- `error_message`：原始错误信息。
- `description`：按当前语言及占位符回填后的描述。
- `solution_link`：当前语言的解决方法或链接。
- `original_response`：原始 G-code 或 webhooks 响应内容。
