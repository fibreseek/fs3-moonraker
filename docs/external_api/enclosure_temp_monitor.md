# 舱内温度监控 (enclosure_temp_monitor)

打印 PLA / PLA_CF / PETG 等热敏感耗材时，周期性读取机头板温度传感器（`temperature_sensor toolhead_temp`）作为舱内环境温度的侧面指标；超过配置阈值后暂停打印，并通过 `error_indexer` 提醒用户「舱内温度过高」。

> 说明：监测点在机头板，语义是舱内（打印腔）环境偏热，而不是仅指机头壳内部局部温度。

## 配置

在 `moonraker.conf` 中增加：

```ini
[enclosure_temp_monitor]
enable: True
# 默认使用机头板温度，侧面反映舱内温度
sensor: temperature_sensor toolhead_temp
# 检测周期（秒）
check_interval: 5.0
# 超温时是否自动暂停
pause_on_alarm: True
# 温度回落后清除报警的滞后（°C）
hysteresis: 2.0
# 同一报警通知的最小间隔（秒）
alarm_cooldown: 60.0
# 是否同时参考当前打印文件元数据中的 filament_type
use_file_metadata: True
# 从哪些料卷槽读取材料（逗号分隔）
spool_ids: P1,P2
# 需要监控的 polymer_type（与 materials.json 一致）
monitored_materials: PLA,PLA_CF,PETG
# 各材料舱内温度上限（°C），设为 0 可禁用该材料
max_temp_PLA: 40
max_temp_PLA_CF: 40
max_temp_PETG: 45
```

模块通过配置段名自动加载；删除整段即可关闭组件。

## 行为

1. 仅在作业状态为打印中（`started` / `resumed`）时开启监控。
2. 当前材料来源：
   - `material_manager` 中 `spool_ids` 指定料卷的 `polymer_type`
   - 可选：当前 gcode 元数据 `filament_type`
3. 仅当活跃材料属于 `monitored_materials` 且传感器温度高于对应 `max_temp_*` 时触发。
4. 触发后：
   - 发送事件 `enclosure_temp_monitor:alarm`
   - 由 `error_indexer` 匹配错误码（`10076`）并推送 `notify_error_indexer_error_detected`
   - 若 `pause_on_alarm: True`，调用暂停打印
5. 温度降至 `limit - hysteresis` 以下后清除内部报警状态（不走 error_indexer）。

## HTTP / JSON-RPC

### 查询状态

`GET /server/enclosure_temp_monitor/status`

或 JSON-RPC：`server.enclosure_temp_monitor.status`

响应示例：

```json
{
  "enable": true,
  "monitoring": true,
  "sensor": "temperature_sensor toolhead_temp",
  "temperature": 38.2,
  "material_limits": {"PLA": 40.0, "PLA_CF": 40.0, "PETG": 45.0},
  "active_materials": ["PLA"],
  "alarm_active": false,
  "last_alarm_material": null,
  "last_alarm_temp": null,
  "pause_on_alarm": true,
  "check_interval": 5.0,
  "hysteresis": 2.0
}
```

## 报警通知（经 error_indexer）

超温时模块发出 `enclosure_temp_monitor:alarm`，`error_indexer` 监听后匹配 `error_info.xlsx` 错误码 **10076**，再推送：

### `notify_error_indexer_error_detected`

```json
{
  "error_code": "10076",
  "error_message": "Chamber temperature too high: 41.2C (limit 40.0C for PLA)",
  "description": "舱内温度过高：41.2°C（材料 PLA 上限 40.0°C）",
  "timestamp": 1710000000.0,
  "solution_link": "打印已暂停。舱内温度过高（由机头板温度侧面反映）...",
  "original_response": "enclosure_temp_monitor:alarm material=PLA, ..."
}
```

匹配模板（xlsx 报错信息列）：

`Chamber temperature too high: %1C (limit %2C for %3)`

### 原始事件（可选）

模块仍注册 `notify_enclosure_temp_monitor_alarm`，便于调试；上位机展示请以 `error_indexer` 为准。

```json
{
  "message": "Chamber temperature too high: 41.2C (limit 40.0C for PLA)",
  "temperature": 41.2,
  "limit": 40.0,
  "material": "PLA",
  "sensor": "temperature_sensor toolhead_temp",
  "paused": true
}
```

### `notify_enclosure_temp_monitor_cleared`

温度恢复后的内部清除事件（不进入 error_indexer）：

```json
{
  "message": "Chamber temperature recovered: 37.5C (was monitoring PLA)",
  "temperature": 37.5,
  "material": "PLA",
  "sensor": "temperature_sensor toolhead_temp"
}
```
