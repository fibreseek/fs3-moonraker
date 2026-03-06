# Moonraker 错误索引插件使用说明

## 1. 插件简介
本插件用于实时监控 Moonraker/Klipper 的 G-code 输出，识别错误信息，并根据预定义的 CSV 错误数据库提供详细的错误描述和解决方案。

主要功能包括：
- **实时错误检测**：监控以 "!!" 开头的标准错误及自定义错误格式。
- **错误索引系统**：基于 CSV 文件提供多语言错误描述和 SOP 解决方案链接。
- **通知推送**：检测到错误时通过 WebSocket 推送包含错误代码、描述和解决方案的结构化通知。
- **多语言支持**：支持动态切换通知语言（默认为英文）。
- **REST API**：提供配置更新、错误列表查询和数据库重载接口。

## 2. 安装说明

1. **复制插件文件**
   将 `error_indexer.py` 文件复制到 Moonraker 的组件目录中：
   ```bash
   cp error_indexer.py /home/ix/Work/moonraker/moonraker/components/
   ```

2. **准备错误数据库**
   确保错误索引 CSV 文件存在于指定路径（默认：`/home/ix/Work/moonraker/error_info.CSV`）。

3. **修改配置文件**
   编辑 `moonraker.conf`，添加以下配置节以启用插件：

   ```ini
   [error_indexer]
   csv_path: /home/ix/Work/moonraker/error_info.CSV
   language: en
   ```

4. **重启 Moonraker**
   保存配置后重启 Moonraker 服务以加载插件。

## 3. 错误数据库 (CSV) 格式规范

CSV 文件应使用 UTF-8 编码，包含以下列（列名必须匹配）：

| 列名 | 说明 | 示例 |
| :--- | :--- | :--- |
| `Error Code` | 唯一错误代码 | `10001` |
| `报错信息` | 默认/中文错误匹配关键字 | `Timer too close` |
| `报错信息多语言` | 多语言描述，格式为 `lang:描述` | `de:Timer zu nah`<br>`zh:定时器太近` |
| `解决方法/sop链接（英文）` | 默认/英文解决方案链接 | `http://wiki.xyz/en/10001` |
| `解决方法/sop链接（多语言）` | 多语言解决方案，格式同上 | `zh:http://wiki.xyz/zh/10001` |
| `报错原因` | 错误原因说明（仅作记录） | `CPU过载` |

**多语言字段格式示例**：
```text
de:这是德语描述
zh:这是中文描述
fr:这是法语描述
```

## 4. WebSocket (JSON-RPC) 接口文档

本插件的所有 API 均支持通过 Moonraker 的 WebSocket 接口进行调用。JSON-RPC 方法名由 HTTP 路径转换而来（将 `/` 替换为 `.` 并去除开头的 `.`）。

### 4.1 更新配置
- **Method**: `server.error_indexer.config`
- **Params**:
  - `language`: 目标语言代码
- **示例**:
  ```json
  {
    "jsonrpc": "2.0",
    "method": "server.error_indexer.config",
    "params": {"language": "zh"},
    "id": 2
  }
  ```

## 5. 通知格式

当检测到错误时，插件会发送 WebSocket 事件 `notify_error_detected`。

**完整 Payload 示例**:
```json
{
  "jsonrpc": "2.0",
  "method": "notify_error_detected",
  "params": [
    {
      "error_code": "10001",
      "error_message": "!! Timer too close",
      "description": "Timer too close",  // 根据当前语言设置返回
      "timestamp": 1772592289.0313463,
      "solution_link": "",               // 根据当前语言设置返回
      "original_response": "!! !! Timer too close"
    }
  ]
}
```

**参数说明**:
- `params`: 一个数组，包含单个错误信息对象。
- `error_code`: 错误代码（从 CSV 获取）。
- `error_message`: 识别到的核心错误信息。
- `description`: 错误的详细描述（多语言）。
- `timestamp`: 错误发生的时间戳。
- `solution_link`: 解决方案或 SOP 的 URL。
- `original_response`: 原始的 G-code 响应内容。
