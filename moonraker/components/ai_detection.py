from __future__ import annotations
import asyncio
import logging
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Union,
    Callable,
    Coroutine,
)

import websockets
from websockets.client import WebSocketClientProtocol
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidURI,
    InvalidHandshake,
)

from ..common import RequestType, JobEvent
from ..utils import json_wrapper as jsonw

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest
    from ..eventloop import FlexTimer


class DetectionType(str, Enum):
    """AI 检测类型枚举"""
    DEFECT_DETECT = "defect_detect"  # 缺陷检测
    FOREIGN_OBJECT_DETECT = "preprint_object_detect"  # 平台异物检测


class DetectionScenario(str, Enum):
    """AI 检测场景枚举"""
    # 打印前异物检测
    PREPRINT_FOREIGN_OBJECT = "preprint_foreign_object"
    # 打印中缺陷检测（定时）
    PRINTING_DEFECT_PERIODIC = "printing_defect_periodic"
    # 打印中缺陷检测（手动）
    PRINTING_DEFECT_MANUAL = "printing_defect_manual"
    # 未知场景（用于兼容）
    UNKNOWN = "unknown"


@dataclass
class ClassMapping:
    """类别映射数据类"""
    name: str
    description: str


class ClassMappingRegistry:
    """类别映射注册表，支持从配置文件加载"""

    # Default defect detection class mappings
    DEFAULT_DEFECT_CLASSES: Dict[int, ClassMapping] = {
        0: ClassMapping("spaghetti", "Spaghetti-like defect"),
        1: ClassMapping("zits", "Zits/blobs"),
        2: ClassMapping("stringing", "Stringing"),
    }

    # Default foreign object detection class mappings
    DEFAULT_FOREIGN_CLASSES: Dict[int, ClassMapping] = {
        0: ClassMapping("object", "Foreign object on bed"),
        1: ClassMapping("object", "Foreign object on bed"),
        2: ClassMapping("hand", "Hand detected"),
    }

    def __init__(self) -> None:
        self.defect_classes: Dict[int, ClassMapping] = dict(
            self.DEFAULT_DEFECT_CLASSES)
        self.foreign_classes: Dict[int, ClassMapping] = dict(
            self.DEFAULT_FOREIGN_CLASSES)

    def load_from_config(self, config: "ConfigHelper") -> None:
        """从配置文件加载类别映射

        配置格式示例:
            defect_class_0: spaghetti, 炒面/意大利面状缺陷
            defect_class_1: zits, 疙瘩/斑点
            foreign_class_0: object, 底板异物
        """
        # 加载缺陷检测类别
        self._load_class_mappings(config, "defect_class_", self.defect_classes)

        # 加载平台异物检测类别
        self._load_class_mappings(
            config, "foreign_class_", self.foreign_classes)

    def _load_class_mappings(
        self,
        config: "ConfigHelper",
        prefix: str,
        target: Dict[int, ClassMapping]
    ) -> None:
        """加载指定前缀的类别映射"""
        # 获取所有配置选项
        options = config.get_options()

        for option in options:
            if option.startswith(prefix):
                try:
                    # 解析类别 ID
                    class_id_str = option[len(prefix):]
                    class_id = int(class_id_str)

                    # 获取配置值并解析
                    value = config.get(option, None)
                    if value:
                        parts = [p.strip() for p in value.split(",", 1)]
                        name = parts[0]
                        description = parts[1] if len(parts) > 1 else name
                        target[class_id] = ClassMapping(name, description)
                        logging.debug(
                            f"Loaded class mapping: {prefix}{class_id} = "
                            f"{name} ({description})"
                        )
                except (ValueError, IndexError) as e:
                    logging.warning(
                        f"Invalid class mapping config '{option}': {e}"
                    )

    def get_defect_name(self, class_id: int) -> str:
        """获取缺陷类别名称"""
        if class_id in self.defect_classes:
            return self.defect_classes[class_id].name
        return f"unknown_{class_id}"

    def get_defect_description(self, class_id: int) -> str:
        """获取缺陷类别描述"""
        if class_id in self.defect_classes:
            return self.defect_classes[class_id].description
        return f"unknown_{class_id}"

    def get_foreign_name(self, class_id: int) -> str:
        """获取平台异物类别名称"""
        if class_id in self.foreign_classes:
            return self.foreign_classes[class_id].name
        return f"unknown_{class_id}"

    def get_foreign_description(self, class_id: int) -> str:
        """获取平台异物类别描述"""
        if class_id in self.foreign_classes:
            return self.foreign_classes[class_id].description
        return f"unknown_{class_id}"

    def get_all_mappings(self) -> Dict[str, Any]:
        """获取所有类别映射，用于 API 返回"""
        return {
            "defect_classes": {
                class_id: {"name": m.name, "description": m.description}
                for class_id, m in self.defect_classes.items()
            },
            "foreign_classes": {
                class_id: {"name": m.name, "description": m.description}
                for class_id, m in self.foreign_classes.items()
            }
        }


# 全局类别映射注册表，在 AIDetectionClient 初始化时设置
_class_registry: Optional[ClassMappingRegistry] = None


def get_class_registry() -> ClassMappingRegistry:
    """获取全局类别映射注册表"""
    global _class_registry
    if _class_registry is None:
        _class_registry = ClassMappingRegistry()
    return _class_registry


class ConnectionState(str, Enum):
    """WebSocket 连接状态"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


@dataclass
class DetectionParams:
    """检测参数数据类"""
    obj_thresh: float = 0.35  # 目标检出的阈值
    nms_thresh: float = 0.45  # 检测框的阈值
    is_lite: bool = False     # 是否使用lite模型（仅缺陷检测有效）

    def to_dict(self) -> Dict[str, Union[float, bool]]:
        return {
            "obj_thresh": self.obj_thresh,
            "nms_thresh": self.nms_thresh,
            "is_lite": self.is_lite
        }


# 上位机三档灵敏度：对应下发给 AI 服务的 obj_thresh（置信度门槛）
SENSITIVITY_OBJ_THRESH: Dict[str, float] = {
    "high": 0.5,
    "medium": 0.65,
    "low": 0.8,
}

_SENSITIVITY_LABEL_TO_KEY: Dict[str, str] = {
    "high": "high",
    "medium": "medium",
    "low": "low",
}


def _canonical_sensitivity_key(value: str) -> str:
    """将 API 档位标签规范为 high|medium|low（英文，大小写不敏感）。非法则抛 ValueError。"""
    key = value.strip().lower()
    if key in _SENSITIVITY_LABEL_TO_KEY:
        return _SENSITIVITY_LABEL_TO_KEY[key]
    raise ValueError(f"invalid sensitivity label: {value!r}")


@dataclass
class DefectDetectionResult:
    """缺陷检测结果数据类

    缺陷类型:
        0 - spaghetti (炒面/意大利面状缺陷)
        1 - zits (疙瘩/斑点)
        2 - stringing (拉丝)
    """
    is_valid: bool
    boxes: List[List[float]] = field(
        default_factory=list)  # 缺陷目标框 [x1, y1, x2, y2]
    classes: List[int] = field(default_factory=list)  # 缺陷所属类别 ID
    scores: List[float] = field(default_factory=list)  # 置信度
    warning: bool = False  # 是否需要报警

    @property
    def class_names(self) -> List[str]:
        """获取类别名称列表"""
        registry = get_class_registry()
        return [registry.get_defect_name(c) for c in self.classes]

    @classmethod
    def _parse_bool(cls, value: Any, default: bool = False) -> bool:
        """解析布尔值，支持字符串形式

        Args:
            value: 要解析的值
            default: 默认值

        Returns:
            bool: 解析后的布尔值
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return bool(value) if value is not None else default

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DefectDetectionResult":
        # 解析 warning 字段（处理字符串形式的布尔值）
        warning_value = data.get("WARNING", data.get("warning", False))
        warning_bool = cls._parse_bool(warning_value, False)

        return cls(
            is_valid=data.get("is_valid", False),
            boxes=data.get("boxes", []),
            classes=data.get("classes", []),
            scores=data.get("scores", []),
            warning=warning_bool
        )

    def to_dict(self) -> Dict[str, Any]:
        registry = get_class_registry()
        return {
            "is_valid": self.is_valid,
            "boxes": self.boxes,
            "classes": self.classes,
            "class_names": [registry.get_defect_name(c) for c in self.classes],
            "scores": self.scores,
            "warning": self.warning
        }

    def get_defects_summary(self) -> List[Dict[str, Any]]:
        """获取缺陷摘要列表"""
        registry = get_class_registry()
        defects = []
        for i, (box, cls_id, score) in enumerate(zip(self.boxes, self.classes, self.scores)):
            defects.append({
                "index": i,
                "box": box,
                "class_id": cls_id,
                "class_name": registry.get_defect_name(cls_id),
                "class_description": registry.get_defect_description(cls_id),
                "score": score
            })
        return defects


@dataclass
class ForeignDetectionResult:
    """平台异物检测结果数据类

    异物类别由配置文件定义，默认:
        0/1 - 底板上的异物
        2   - 未检测到打印底板（或相关场景）
    """
    is_valid: bool
    has_object: bool = False  # 是否有异物
    scores: List[float] = field(default_factory=list)  # 置信度
    boxes: List[List[float]] = field(default_factory=list)  # 异物目标框
    classes: List[int] = field(default_factory=list)  # 异物所属类别
    no_build_plate: bool = False  # 是否未检测到打印底板

    @property
    def class_names(self) -> List[str]:
        """获取类别名称列表"""
        registry = get_class_registry()
        return [registry.get_foreign_name(c) for c in self.classes]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ForeignDetectionResult":
        classes: List[int] = data.get("classes", []) or []
        # 约定: 类别 ID 为 2 表示未检测到打印底板
        no_build_plate = 2 in classes
        return cls(
            is_valid=data.get("is_valid", False),
            has_object=data.get("has_object", False),
            scores=data.get("scores", []),
            boxes=data.get("boxes", []),
            classes=classes,
            no_build_plate=no_build_plate,
        )

    def to_dict(self) -> Dict[str, Any]:
        registry = get_class_registry()
        return {
            "is_valid": self.is_valid,
            "has_object": self.has_object,
            "scores": self.scores,
            "boxes": self.boxes,
            "classes": self.classes,
            "class_names": [registry.get_foreign_name(c) for c in self.classes],
            "no_build_plate": self.no_build_plate,
        }


@dataclass
class CoordinatorConfig:
    """AI检测协调器配置"""
    # AI总开关
    enable: bool = True

    # 检测开关
    enable_foreign_detect: bool = True
    enable_defect_detect: bool = True

    # 检测间隔（秒）
    detect_interval: float = 10.0

    # 自动暂停配置
    auto_pause_on_defect: bool = True
    auto_pause_on_spaghetti: bool = True
    auto_pause_on_zits: bool = False
    auto_pause_on_stringing: bool = False

    @classmethod
    def from_config(cls, config: "ConfigHelper") -> "CoordinatorConfig":
        """从配置文件加载配置"""
        return cls(
            enable=config.getboolean("enable", False),
            enable_foreign_detect=config.getboolean(
                "enable_foreign_detect", True
            ),
            enable_defect_detect=config.getboolean(
                "enable_defect_detect", True
            ),
            detect_interval=config.getfloat(
                "detect_interval", 10.0, minval=1.0
            ),
            auto_pause_on_defect=config.getboolean(
                "auto_pause_on_defect", True
            ),
            auto_pause_on_spaghetti=config.getboolean(
                "auto_pause_on_spaghetti", True
            ),
            auto_pause_on_zits=config.getboolean(
                "auto_pause_on_zits", False
            ),
            auto_pause_on_stringing=config.getboolean(
                "auto_pause_on_stringing", False
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "enable": self.enable,
            "enable_foreign_detect": self.enable_foreign_detect,
            "enable_defect_detect": self.enable_defect_detect,
            "detect_interval": self.detect_interval,
            "auto_pause_on_defect": self.auto_pause_on_defect,
            "auto_pause_on_spaghetti": self.auto_pause_on_spaghetti,
            "auto_pause_on_zits": self.auto_pause_on_zits,
            "auto_pause_on_stringing": self.auto_pause_on_stringing,
        }

    def update(self, **kwargs) -> None:
        """更新配置"""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)


class AIDetectionClient:
    """AI 检测服务 WebSocket 客户端"""

    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()  # 获取 Moonraker 服务器实例
        self.eventloop = self.server.get_event_loop()  # 获取事件循环
        self.database = self.server.lookup_component("database")  # 获取数据库组件

        # 初始化类别映射注册表
        global _class_registry  
        _class_registry = ClassMappingRegistry()
        _class_registry.load_from_config(config)
        self.class_registry = _class_registry

        # 配置参数
        self.host: str = config.get("host", "127.0.0.1")
        self.port: int = config.getint("port", 8765)
        self.reconnect_interval: float = config.getfloat(
            "reconnect_interval", 5.0, minval=1.0
        )
        self.request_timeout: float = config.getfloat(
            "request_timeout", 30.0, minval=5.0
        )
        self.auto_connect: bool = config.getboolean("auto_connect", True)

        # 检测参数
        self.obj_thresh: float = config.getfloat(
            "default_obj_thresh", 0.35, minval=0.0, maxval=1.0
        )
        self.nms_thresh: float = config.getfloat(
            "default_nms_thresh", 0.45, minval=0.0, maxval=1.0
        )
        # 三档灵敏度（high/medium/low）；None 表示走 default_obj_thresh / 数值调参流程
        self.sensitivity: Optional[str] = None

        # 连接状态
        self._state: ConnectionState = ConnectionState.DISCONNECTED # 初始状态
        self._websocket: Optional[WebSocketClientProtocol] = None   # WebSocket 连接
        self._connect_task: Optional[asyncio.Task] = None           # 连接任务
        self._read_task: Optional[asyncio.Task] = None              # 读取任务
        self._closing: bool = False                                 # 关闭标志  
        self._connection_mutex: asyncio.Lock = asyncio.Lock()       # 连接互斥锁
        self._connection_error_notified: bool = False               # 当前断连周期是否已发送过连接错误通知

        # 请求管理
        self._pending_requests: Dict[str, asyncio.Future] = {}      # 待处理请求
        self._request_counter: int = 0                              # 请求计数器

        # 统计信息
        self._stats: Dict[str, Any] = {
            "total_requests": 0,                                    # 总请求数
            "successful_requests": 0,                               # 成功请求数
            "failed_requests": 0,                                   # 失败请求数
            "last_request_time": None,                              # 上次请求时间
            "last_error": None,                                     # 上次错误信息
            "connection_attempts": 0,                               # 连接尝试次数 
            "successful_connections": 0,                            # 成功连接次数
        }

        # 重连定时器
        self._reconnect_timer: Optional[FlexTimer] = None

        # 协调器配置（尝试从配置文件加载，如果不存在则使用默认值）
        try:
            self.coordinator_config = CoordinatorConfig.from_config(config)
        except Exception:
            # 如果配置文件中没有 coordinator 配置，使用默认值
            self.coordinator_config = CoordinatorConfig()

        # 打印状态和检测定时器
        self._is_detecting: bool = False
        self._defect_timer: Optional[asyncio.Task] = None

        # 注册 API 端点
        self.server.register_endpoint(
            "/server/ai_detection/status",
            RequestType.GET,
            self._handle_status_request
        )
        self.server.register_endpoint(
            "/server/ai_detection/config",
            RequestType.POST,
            self._handle_config_update_request
        )

        # 注册通知（第二个参数是通知名，会生成 notify_<notify_name>）
        self.server.register_notification("ai_detection:state_changed", "ai_detection_state_changed")
        self.server.register_notification("ai_detection:detection_result", "ai_detection_detection_result")
        self.server.register_notification("ai_detection:config_updated", "ai_detection_config_updated")

        # 监听作业状态变化
        self.server.register_event_handler(
            "job_state:state_changed",
            self._on_job_state_changed
        )

        logging.info(
            f"AI Detection Client configured: {self.host}:{self.port}"
        )

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return (
            self._state == ConnectionState.CONNECTED
            and self._websocket is not None
            and self._websocket.open
        )

    def _generate_request_id(self, detection_type: DetectionType) -> str:
        """生成唯一的请求 ID"""
        self._request_counter += 1
        timestamp = int(time.time() * 1000)
        return f"{detection_type.value}_{self._request_counter}_{timestamp}"

    def _effective_obj_thresh(self, override: Optional[float]) -> float:
        """显式参数 > 灵敏度档位 > self.obj_thresh"""
        if override is not None:
            return float(override)
        if self.sensitivity is not None:
            return SENSITIVITY_OBJ_THRESH[self.sensitivity]
        return self.obj_thresh

    def _config_notification_payload(self) -> Dict[str, Any]:
        return {
            **self.coordinator_config.to_dict(),
            "obj_thresh": self.obj_thresh,
            "nms_thresh": self.nms_thresh,
            "sensitivity": self.sensitivity,
            "effective_obj_thresh": self._effective_obj_thresh(None),
        }

    async def component_init(self) -> None:
        """组件初始化，在 Moonraker 启动后调用"""
        # 从数据库加载持久化的配置
        await self._load_config_from_database()

        if self.auto_connect:
            logging.info("AI Detection: Auto-connecting to server...")
            self._connect_task = self.eventloop.create_task(self._do_connect())

    def is_enabled(self) -> bool:
        """检查 AI 检测总开关是否启用（不考虑连接状态）"""
        return self.coordinator_config.enable

    def is_foreign_detect_enabled(self) -> bool:
        """检查是否启用异物检测"""
        return (
            self.coordinator_config.enable
            and self.coordinator_config.enable_foreign_detect
            and self.is_connected
        )

    def is_defect_detect_enabled(self) -> bool:
        """检查是否启用缺陷检测"""
        return (
            self.coordinator_config.enable
            and self.coordinator_config.enable_defect_detect
            and self.is_connected
        )

    def _send_detection_notification(
        self,
        scenario: DetectionScenario,
        detection_type: str,
        message: str
    ) -> None:
        """发送带场景标识的检测结果通知

        Args:
            scenario: 检测场景
            detection_type: 检测类型 (defect 或 foreign_object)
            message: 检测结果消息
        """
        notification_data = {
            "scenario": scenario.value,
            "detection_type": detection_type,
            "message": message
        }
        self.server.send_event(
            "ai_detection:detection_result",
            notification_data
        )

    def _notify_connection_error(self, message: str) -> None:
        """发送 AI 服务连接类错误通知，供 error_indexer 统一索引。"""
        if self._connection_error_notified:
            return
        self._connection_error_notified = True
        self._send_detection_notification(
            scenario=DetectionScenario.UNKNOWN,
            detection_type="unknown",
            message=message,
        )

    async def _do_connect(self) -> bool:
        """执行 WebSocket 连接逻辑"""
        async with self._connection_mutex:
            if self._closing:
                return False

            self._stats["connection_attempts"] += 1
            self._set_state(ConnectionState.CONNECTING)

            ws_uri = f"ws://{self.host}:{self.port}"
            try:
                self._websocket = await asyncio.wait_for(
                    websockets.connect(
                        ws_uri,
                        ping_interval=20,      # 心跳间隔 20 秒
                        ping_timeout=10,       # 心跳超时 10 秒
                        close_timeout=5,       # 关闭超时 5 秒
                        max_size=10 * 1024 * 1024,  # 最大消息 10MB
                    ),
                    timeout=10.0
                )
                self._set_state(ConnectionState.CONNECTED)
                self._stats["successful_connections"] += 1
                logging.info(
                    f"AI Detection: Connected to {ws_uri}"
                )

                # 启动读取任务
                self._read_task = self.eventloop.create_task(self._read_loop())
                return True

            except asyncio.TimeoutError:
                self._stats["last_error"] = "Connection timeout"
                logging.warning(
                    f"AI Detection: Connection to {ws_uri} timed out"
                )
                self._notify_connection_error(
                    "AI Detection connection timeout"
                )
            except ConnectionRefusedError:
                self._stats["last_error"] = "Connection refused"
                logging.warning(
                    f"AI Detection: Connection refused by {ws_uri}"
                )
                self._notify_connection_error(
                    "AI Detection connection refused"
                )
            except (InvalidURI, InvalidHandshake) as e:
                self._stats["last_error"] = f"WebSocket handshake failed: {e}"
                logging.warning(
                    f"AI Detection: WebSocket handshake failed: {e}"
                )
                self._notify_connection_error(
                    "AI Detection websocket handshake failed"
                )
            except Exception as e:
                self._stats["last_error"] = str(e)
                logging.exception(
                    f"AI Detection: Failed to connect to {ws_uri}"
                )
                self._notify_connection_error(
                    "AI Detection websocket connection error"
                )

            self._websocket = None
            self._set_state(ConnectionState.DISCONNECTED)
            self._schedule_reconnect()
            return False

    def _schedule_reconnect(self) -> None:
        """安排重连"""
        if self._closing or self._reconnect_timer is not None:
            return

        self._set_state(ConnectionState.RECONNECTING)
        self._reconnect_timer = self.eventloop.delay_callback(
            self.reconnect_interval,
            self._reconnect_callback
        )

    def _reconnect_callback(self) -> None:
        """重连回调"""
        self._reconnect_timer = None
        if not self._closing:
            self._connect_task = self.eventloop.create_task(self._do_connect())

    async def _read_loop(self) -> None:
        """WebSocket 消息读取循环"""
        if self._websocket is None:
            return

        try:
            async for message in self._websocket:
                if self._closing:
                    break
                try:
                    if isinstance(message, bytes):
                        message = message.decode('utf-8')
                    response = jsonw.loads(message)
                    self._process_response(response)
                except jsonw.JSONDecodeError as e:
                    logging.warning(
                        f"AI Detection: Invalid JSON response: {e}"
                    )
                except Exception as e:
                    logging.exception(f"AI Detection: Error processing message: {e}")

        except ConnectionClosedOK:
            logging.info("AI Detection: WebSocket closed normally")
        except ConnectionClosedError as e:
            logging.warning(
                f"AI Detection: WebSocket closed with error: {e.code} {e.reason}"
            )
            self._notify_connection_error(
                "AI Detection websocket read loop error"
            )
        except ConnectionClosed as e:
            logging.info(f"AI Detection: WebSocket connection closed: {e}")
            self._notify_connection_error(
                "AI Detection websocket read loop error"
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.exception(f"AI Detection: Read loop error: {e}")
            self._notify_connection_error(
                "AI Detection websocket read loop error"
            )
        finally:
            if not self._closing:
                await self._handle_disconnect()

    def _process_response(self, response: Dict[str, Any]) -> None:
        """处理服务端响应"""
        request_id = response.get("request_id")
        response_type = response.get("type")

        if request_id and request_id in self._pending_requests:
            fut = self._pending_requests.pop(request_id)
            if not fut.done():
                if response_type == "success":
                    fut.set_result(response.get("data", {}))
                elif response_type == "error":
                    error_msg = response.get("message", "Unknown error")
                    logging.warning(f"AI Detection: response error - {error_msg}")

                    # 根据 request_id 粗略推断检测类型，用于统一错误通知
                    detection_type = "unknown"
                    if request_id.startswith(DetectionType.DEFECT_DETECT.value):
                        detection_type = "defect"
                    elif request_id.startswith(DetectionType.FOREIGN_OBJECT_DETECT.value):
                        detection_type = "foreign_object"

                    # 通过 ai_detection:detection_result 推送错误信息，
                    # 由 error_indexer 组件进行统一检索和上报
                    self._send_detection_notification(
                        scenario=DetectionScenario.UNKNOWN,
                        detection_type=detection_type,
                        message=error_msg,
                    )

                    fut.set_exception(
                        self.server.error(
                            f"AI Detection error: {error_msg}", 500)
                    )
                else:
                    fut.set_exception(
                        self.server.error(
                            f"Unknown response type: {response_type}", 500)
                    )
        else:
            logging.debug(
                f"AI Detection: Received response without pending request: {response}")

    async def _handle_disconnect(self) -> None:
        """处理 WebSocket 连接断开"""
        async with self._connection_mutex:
            if self._websocket is not None:
                try:
                    await self._websocket.close()
                except Exception:
                    pass
                self._websocket = None

            # 取消所有待处理请求
            for fut in self._pending_requests.values():
                if not fut.done():
                    fut.set_exception(
                        self.server.error("Connection lost", 503)
                    )
            self._pending_requests.clear()

            self._set_state(ConnectionState.DISCONNECTED)

            if not self._closing:
                self._schedule_reconnect()

    def _set_state(self, new_state: ConnectionState) -> None:
        """设置连接状态并发送通知"""
        if self._state != new_state:
            old_state = self._state
            self._state = new_state
            if new_state == ConnectionState.CONNECTED:
                self._connection_error_notified = False
            logging.debug(
                f"AI Detection: State changed from {old_state.value} to {new_state.value}"
            )
            self.server.send_event(
                "ai_detection:state_changed",
                {"old_state": old_state.value, "new_state": new_state.value}
            )

    async def _send_request(
        self,
        detection_type: DetectionType,
        params: DetectionParams
    ) -> Dict[str, Any]:
        """通过 WebSocket 发送检测请求"""
        if not self.is_connected:
            raise self.server.error("AI Detection service not connected", 503)

        request_id = self._generate_request_id(detection_type)
        request = {
            "type": detection_type.value,
            "request_id": request_id,
            "params": params.to_dict()
        }

        # 创建 Future 等待响应
        fut: asyncio.Future = self.eventloop.create_future()
        self._pending_requests[request_id] = fut

        # 更新统计
        self._stats["total_requests"] += 1
        self._stats["last_request_time"] = time.time()

        try:
            # 通过 WebSocket 发送请求
            data = jsonw.dumps(request)
            if isinstance(data, bytes):
                data = data.decode('utf-8')
            await self._websocket.send(data)

            # 等待响应
            result = await asyncio.wait_for(fut, timeout=self.request_timeout)
            self._stats["successful_requests"] += 1
            return result

        except ConnectionClosed as e:
            self._pending_requests.pop(request_id, None)
            self._stats["failed_requests"] += 1
            self._stats["last_error"] = f"WebSocket closed: {e}"
            raise self.server.error("AI Detection connection closed", 503)
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            self._stats["failed_requests"] += 1
            self._stats["last_error"] = "Request timeout"
            raise self.server.error("AI Detection request timed out", 504)
        except Exception as e:
            self._pending_requests.pop(request_id, None)
            self._stats["failed_requests"] += 1
            self._stats["last_error"] = str(e)
            raise

    async def detect_defect(
        self,
        obj_thresh: Optional[float] = None,
        nms_thresh: Optional[float] = None,
        is_lite: bool = False,
        scenario: DetectionScenario = DetectionScenario.PRINTING_DEFECT_MANUAL,
        send_notification: bool = True
    ) -> DefectDetectionResult:
        """执行打印缺陷检测

        Args:
            obj_thresh: 检测阈值
            nms_thresh: NMS阈值
            is_lite: 是否使用lite模型
            scenario: 检测场景
            send_notification: 是否发送通知

        Returns:
            DefectDetectionResult: 缺陷检测结果
        """
        if not self.is_connected:
            return DefectDetectionResult(is_valid=False, warning=False)

        params = DetectionParams(
            obj_thresh=self._effective_obj_thresh(obj_thresh),
            nms_thresh=nms_thresh if nms_thresh is not None else self.nms_thresh,
            is_lite=is_lite
        )

        result_data = await self._send_request(DetectionType.DEFECT_DETECT, params)
        result = DefectDetectionResult.from_dict(result_data)

        if result.warning:
            logging.warning(
                f"AI 检测: 检测到缺陷 - "
                f"类别: {result.classes}, 位置: {result.boxes}, "
                f"置信度: {result.scores}"
            )

        return result

    async def detect_foreign_object(
        self,
        obj_thresh: Optional[float] = None,
        nms_thresh: Optional[float] = None,
        scenario: DetectionScenario = DetectionScenario.PREPRINT_FOREIGN_OBJECT,
        send_notification: bool = True
    ) -> str:
        """执行打印前平台异物检测

        Args:
            obj_thresh: 检测阈值
            nms_thresh: NMS阈值
            scenario: 检测场景
            send_notification: 是否发送通知

        Returns:
            str: 异物检测结果 ok 或 error
        """
        if not self.is_connected:
            msg = "AI Detection service is not connected"
            self._send_detection_notification(
                scenario=DetectionScenario.PREPRINT_FOREIGN_OBJECT,
                detection_type="foreign_object",
                message=msg
            )
            return "error"
        # AI 检测是否启用
        if not self.is_enabled():
            logging.info("AI Detection is disabled, skipping foreign object detection")
            return "ok"

        params = DetectionParams(
            obj_thresh=self._effective_obj_thresh(obj_thresh),
            nms_thresh=nms_thresh if nms_thresh is not None else self.nms_thresh
        )

        result_data = await self._send_request(
            DetectionType.FOREIGN_OBJECT_DETECT, params
        )
        result = ForeignDetectionResult.from_dict(result_data)

        logging.warning(
            f"AI Detection: Foreign object detected - "
            f"Class: {result.classes}, Box: {result.boxes}, "
            f"Score: {result.scores}"
        )

        # 1.1 平台异物检查结果
        if result.is_valid:
            if result.has_object:
                if result.no_build_plate:
                    msg = ("AI Detection: Build plate not detected")
                    logging.warning(msg)
                    self._send_detection_notification(
                        scenario=DetectionScenario.PREPRINT_FOREIGN_OBJECT,
                        detection_type="foreign_object",
                        message=msg
                    )
                    return "error"
                else:
                    msg = ("AI Detection: Foreign object detected")
                    logging.warning(msg)
                    self._send_detection_notification(
                        scenario=DetectionScenario.PREPRINT_FOREIGN_OBJECT,
                        detection_type="foreign_object",
                        message=msg
                    )
                    return "error"
            else:
                logging.info("AI Detection: No foreign objects detected")
                return "ok"
        else:
            # 未检测到异物，继续打印
            msg = (
                "AI Detection: No foreign objects detected"
            )
            logging.info(msg)

        return "ok"

    async def connect(self) -> bool:
        """手动连接到 AI 服务"""
        if self.is_connected:
            return True

        if self._connect_task is not None and not self._connect_task.done():
            return await self._connect_task

        self._connect_task = self.eventloop.create_task(self._do_connect())
        return await self._connect_task

    async def disconnect(self) -> None:
        """断开与 AI 服务的 WebSocket 连接"""
        self._closing = True

        # 取消重连定时器
        if self._reconnect_timer is not None:
            self._reconnect_timer.cancel()
            self._reconnect_timer = None

        # 取消连接任务
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
            self._connect_task = None

        # 取消读取任务
        if self._read_task is not None and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None

        # 关闭 WebSocket 连接
        async with self._connection_mutex:
            if self._websocket is not None:
                try:
                    await self._websocket.close()
                except Exception:
                    pass
                self._websocket = None

            # 取消所有待处理请求
            for fut in self._pending_requests.values():
                if not fut.done():
                    fut.set_exception(
                        self.server.error("Disconnected by user", 503)
                    )
            self._pending_requests.clear()

        self._set_state(ConnectionState.DISCONNECTED)
        self._closing = False

    def get_status(self) -> Dict[str, Any]:
        """获取服务状态"""
        return {
            # 连接状态
            "state": self._state.value,
            "is_connected": self.is_connected,
            "host": self.host,
            "port": self.port,
            "stats": dict(self._stats),
            # 配置（合并客户端配置和协调器配置）
            "config": {
                # 客户端连接配置
                "reconnect_interval": self.reconnect_interval,
                "request_timeout": self.request_timeout,
                "auto_connect": self.auto_connect,
                # AI检测配置
                "obj_thresh": self.obj_thresh,
                "nms_thresh": self.nms_thresh,
                "sensitivity": self.sensitivity,
                "effective_obj_thresh": self._effective_obj_thresh(None),
                # 协调器配置
                "enable": self.coordinator_config.enable,
                "enable_foreign_detect": self.coordinator_config.enable_foreign_detect,
                "enable_defect_detect": self.coordinator_config.enable_defect_detect,
                "detect_interval": self.coordinator_config.detect_interval,
                "auto_pause_on_defect": self.coordinator_config.auto_pause_on_defect,
                "auto_pause_on_spaghetti": self.coordinator_config.auto_pause_on_spaghetti,
                "auto_pause_on_zits": self.coordinator_config.auto_pause_on_zits,
                "auto_pause_on_stringing": self.coordinator_config.auto_pause_on_stringing,
            },
            "class_mappings": self.class_registry.get_all_mappings(),
            # 协调器状态
            "is_detecting": self._is_detecting,
        }

    # API 端点处理函数

    async def _handle_status_request(self, web_request: WebRequest) -> Dict[str, Any]:
        """处理状态请求，返回完整的AI检测状态信息"""
        return self.get_status()

    async def _handle_config_update_request(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        """统一处理AI检测配置更新

        支持的配置项：
        - obj_thresh: 检测阈值 (0.0-1.0)
        - nms_thresh: NMS阈值 (0.0-1.0)
        - enable: AI总开关
        - enable_foreign_detect: 异物检测开关
        - enable_defect_detect: 缺陷检测开关
        - detect_interval: 检测间隔(秒)
        - auto_pause_on_defect: 缺陷时自动暂停
        - auto_pause_on_spaghetti: 意面效应时自动暂停
        - auto_pause_on_zits: 疙瘩时自动暂停
        - auto_pause_on_stringing: 拉丝时自动暂停
        - sensitivity: 三档灵敏度 high|medium|low（英文，大小写不敏感）；null 清除并恢复数值阈值模式
        """
        params = web_request.get_args() or {}

        if not isinstance(params, dict):
            raise self.server.error("Invalid params format, must be JSON object", 400)

        if not params:
            raise self.server.error("No configuration parameters provided", 400)

        result = {}
        updated = False
        coordinator_updated = False  # 标记 coordinator 配置是否更新
        thresh_updates: Dict[str, Any] = {}  # 阈值更新字典，用于持久化
        sensitivity_in_request = False

        def _validate_thresh(name: str, value: Any) -> float:
            if not isinstance(value, (int, float)):
                raise self.server.error(f"{name} must be a number", 400)
            val = float(value)
            if not (0.0 <= val <= 1.0):
                raise self.server.error(f"{name} must be between 0.0 and 1.0", 400)
            return val

        if "obj_thresh" in params:
            # 显式数值阈值优先：退出灵敏度档位模式
            self.sensitivity = None
            try:
                await self.database.delete_item(
                    "ai_detection", "default_sensitivity"
                )
            except Exception:
                pass
            self.obj_thresh = _validate_thresh("obj_thresh", params["obj_thresh"])
            result["obj_thresh"] = self.obj_thresh
            thresh_updates["default_obj_thresh"] = self.obj_thresh
            updated = True

        if "nms_thresh" in params:
            self.nms_thresh = _validate_thresh("nms_thresh", params["nms_thresh"])
            result["nms_thresh"] = self.nms_thresh
            thresh_updates["default_nms_thresh"] = self.nms_thresh
            updated = True

        if "sensitivity" in params and "obj_thresh" not in params:
            sensitivity_in_request = True
            sraw = params["sensitivity"]
            if sraw is None:
                self.sensitivity = None
                try:
                    await self.database.delete_item(
                        "ai_detection", "default_sensitivity"
                    )
                except Exception:
                    pass
            else:
                if not isinstance(sraw, str):
                    raise self.server.error(
                        "sensitivity must be a string or null", 400
                    )
                try:
                    canon = _canonical_sensitivity_key(sraw)
                except ValueError:
                    raise self.server.error(
                        "sensitivity must be high, medium, or low", 400
                    )
                self.sensitivity = canon
                await self.database.insert_item(
                    "ai_detection", "default_sensitivity", canon
                )
            updated = True

        # 处理 coordinator 配置
        coordinator_params = {
            k: v for k, v in params.items()
            if k in [
                "enable",
                "enable_foreign_detect",
                "enable_defect_detect",
                "detect_interval",
                "auto_pause_on_defect",
                "auto_pause_on_spaghetti",
                "auto_pause_on_zits",
                "auto_pause_on_stringing",
            ]
        }
        if coordinator_params:
            coordinator_result = self._update_coordinator_config(**coordinator_params)
            result.update(coordinator_result)
            updated = True
            coordinator_updated = True

        if not updated:
            raise self.server.error("No valid configuration parameters provided", 400)

        result["sensitivity"] = self.sensitivity
        result["effective_obj_thresh"] = self._effective_obj_thresh(None)

        if updated and thresh_updates:
            await self._persist_config(thresh_updates)

        # 未改协调器时由本处发通知；协调器更新时由 _update_coordinator_config 发送（含灵敏度字段）
        if updated and not coordinator_updated and (
            thresh_updates or sensitivity_in_request
        ):
            self.server.send_event(
                "ai_detection:config_updated",
                self._config_notification_payload(),
            )

        return result

    async def _load_config_from_database(self) -> None:
        """从数据库加载持久化的配置"""
        try:
            # 加载检测阈值配置
            db_obj_thresh = await self.database.get_item(
                "ai_detection", "default_obj_thresh", None
            )
            if db_obj_thresh is not None:
                self.obj_thresh = float(db_obj_thresh)

            db_nms_thresh = await self.database.get_item(
                "ai_detection", "default_nms_thresh", None
            )
            if db_nms_thresh is not None:
                self.nms_thresh = float(db_nms_thresh)

            db_sensitivity = await self.database.get_item(
                "ai_detection", "default_sensitivity", None
            )
            if db_sensitivity is not None:
                try:
                    self.sensitivity = _canonical_sensitivity_key(str(db_sensitivity))
                except ValueError:
                    logging.warning(
                        "AI Detection: invalid default_sensitivity in DB, ignoring: %r",
                        db_sensitivity,
                    )
                    self.sensitivity = None

            # 加载协调器配置
            db_coordinator_config = await self.database.get_item(
                "ai_detection", "coordinator_config", None
            )
            if db_coordinator_config:
                self.coordinator_config.update(**db_coordinator_config)

            logging.info("AI Detection: Config loaded from database")
        except Exception as e:
            logging.warning(f"AI Detection: Failed to load config from database: {e}")

    async def _persist_config(self, config_updates: Dict[str, Any]) -> bool:
        """持久化配置到数据库

        Args:
            config_updates: 要保存的配置字典，键为配置项名，值为配置值

        Returns:
            bool: 是否成功保存
        """
        try:
            # 将配置项分别保存到数据库
            for key, value in config_updates.items():
                if key == "default_obj_thresh":
                    self.database.insert_item("ai_detection", "default_obj_thresh", value)
                elif key == "default_nms_thresh":
                    self.database.insert_item("ai_detection", "default_nms_thresh", value)
                elif key == "default_sensitivity":
                    if value is None:
                        self.database.delete_item(
                            "ai_detection", "default_sensitivity"
                        )
                    else:
                        self.database.insert_item(
                            "ai_detection", "default_sensitivity", value
                        )
                elif key in [
                    "enable",
                    "enable_foreign_detect",
                    "enable_defect_detect",
                    "detect_interval",
                    "auto_pause_on_defect",
                    "auto_pause_on_spaghetti",
                    "auto_pause_on_zits",
                    "auto_pause_on_stringing",
                ]:
                    # 协调器配置保存为一个整体
                    coordinator_data = self.coordinator_config.to_dict()
                    self.database.insert_item("ai_detection", "coordinator_config", coordinator_data)

            logging.info(f"AI Detection: Config persisted to database: {config_updates}")
            return True
        except Exception as e:
            logging.error(f"AI Detection: Failed to persist config: {e}")
            return False

    def _is_printing(self) -> bool:
        """检查是否正在打印"""
        try:
            job_state = self.server.lookup_component("job_state")
            stats = job_state.get_last_stats()
            state = stats.get("state", "")
            return state in ("printing", "paused")
        except Exception:
            return False

    def _update_coordinator_config(self, **kwargs) -> Dict[str, Any]:
        """更新协调器配置

        Args:
            **kwargs: 要更新的配置项

        Returns:
            Dict[str, Any]: 更新后的完整配置

        Raises:
            ValueError: 配置值无效时抛出
        """
        # 记录 enable 的旧值
        old_enable = self.coordinator_config.enable

        # 更新配置
        updates = {}
        for key in [
            "enable",
            "enable_foreign_detect",
            "enable_defect_detect",
            "detect_interval",
            "auto_pause_on_defect",
            "auto_pause_on_spaghetti",
            "auto_pause_on_zits",
            "auto_pause_on_stringing",
        ]:
            if key in kwargs:
                value = kwargs[key]
                # 类型验证
                if key == "detect_interval":
                    if not isinstance(value, (int, float)):
                        raise ValueError(f"{key} must be a number")
                    value = float(value)
                    if value < 1.0:
                        raise ValueError(f"{key} must be at least 1.0")
                elif isinstance(value, bool):
                    pass
                elif isinstance(value, str):
                    if value.lower() in ("true", "1", "yes"):
                        value = True
                    elif value.lower() in ("false", "0", "no"):
                        value = False
                    else:
                        raise ValueError(f"{key} must be a boolean")
                else:
                    raise ValueError(f"{key} must be a boolean")

                updates[key] = value

        if not updates:
            raise ValueError("No valid configuration parameters provided")

        # 应用更新
        self.coordinator_config.update(**updates)

        logging.info(
            f"AI Detection: Coordinator config updated: {updates}"
        )

        # 持久化配置到文件
        self.eventloop.create_task(self._persist_config(updates))

        # 发送配置更新通知（包含所有配置）
        self.server.send_event(
            "ai_detection:config_updated",
            self._config_notification_payload(),
        )

        # 如果 enable 从 false 变为 true，检查是否需要启动定时检测
        if not old_enable and self.coordinator_config.enable:
            self._maybe_start_defect_detection()
        # 如果 enable 从 true 变为 false，停止定时检测
        elif old_enable and not self.coordinator_config.enable:
            if self._is_detecting:
                logging.info("AI Detection: Disabled, stopping defect detection timer...")
                self._stop_defect_timer()

        return self.coordinator_config.to_dict()

    def _maybe_start_defect_detection(self) -> None:
        """如果条件满足，启动定时缺陷检测"""
        if (
            self.is_defect_detect_enabled()
            and self._is_printing()
            and not self._is_detecting
        ):
            logging.info(
                "AI Detection: Enabled during printing, starting defect detection timer..."
            )
            self._start_defect_timer()

    # 作业状态监听和定时检测

    async def _on_job_state_changed(
        self,
        job_event: JobEvent,
        prev_stats: Dict[str, Any],
        new_stats: Dict[str, Any]
    ) -> None:
        """监听作业状态变化"""
        logging.debug(
            f"AI Detection: Job state changed to {job_event}"
        )

        if job_event in (JobEvent.STARTED, JobEvent.RESUMED):
            # 打印开始或恢复
            if self.is_defect_detect_enabled():
                logging.info(
                    f"AI Detection: Starting defect detection timer "
                    f"(event: {job_event}, interval: {self.coordinator_config.detect_interval}s)"
                )
                self._start_defect_timer()
        elif job_event in (JobEvent.PAUSED, JobEvent.COMPLETE,
                           JobEvent.ERROR, JobEvent.CANCELLED,
                           JobEvent.STANDBY):
            # 打印暂停、完成、错误、取消或回到 standby
            if self._is_detecting:
                logging.info(
                    f"AI Detection: Stopping defect detection timer "
                    f"(event: {job_event})"
                )
                self._stop_defect_timer()

    def _start_defect_timer(self) -> None:
        """启动缺陷检测定时器"""
        if self._is_detecting:
            return

        self._is_detecting = True
        self._defect_timer = self.eventloop.create_task(
            self._defect_detection_loop()
        )

    def _stop_defect_timer(self) -> None:
        """停止缺陷检测定时器"""
        logging.info("Stop the defect detection timer")
        self._is_detecting = False
        if self._defect_timer is not None:
            self._defect_timer.cancel()
            self._defect_timer = None

    async def _defect_detection_loop(self) -> None:
        """缺陷检测定时循环"""
        try:
            while self._is_detecting:
                try:
                    await self._perform_defect_detection()
                except Exception as e:
                    logging.exception(
                        f"AI Detection: Error in defect detection loop: {e}"
                    )

                await asyncio.sleep(self.coordinator_config.detect_interval)
        except asyncio.CancelledError:
            logging.debug("AI Detection: Defect detection loop cancelled")
        except Exception as e:
            logging.exception(f"AI Detection: Defect detection loop error: {e}")
        finally:
            self._is_detecting = False

    async def _perform_defect_detection(self) -> None:
        """执行一次缺陷检测（打印中定时检测）"""
        if not self.is_connected:
            logging.warning(
                "AI Detection: Not connected for defect detection, skipping..."
            )
            return

        logging.debug("AI Detection: Performing defect detection...")
        result = await self.detect_defect(
            scenario=DetectionScenario.PRINTING_DEFECT_PERIODIC,
            send_notification=False  # 先不发送通知，根据warning决定
        )

        # 只在warning时发送通知
        if result.warning:
            logging.warning(
                f"AI Detection: Defect detected! "
                f"Classes: {result.classes}, Boxes: {result.boxes}"
            )

            # 根据result解析
            class_registry = get_class_registry()
            class_names = [class_registry.get_defect_name(cls) for cls in result.classes]
            defect_types = ", ".join(class_names)
            message = f"Defect detected: {defect_types}"
            logging.info(f"AI Detection: Defect message: {message}")

            # 发送通知
            self._send_detection_notification(
                scenario=DetectionScenario.PRINTING_DEFECT_PERIODIC,
                detection_type="defect",
                message=message
            )

            # 判断是否需要自动暂停
            if self._should_auto_pause(result):
                await self._pause_print(result)

    def _should_auto_pause(self, result: DefectDetectionResult) -> bool:
        """判断是否需要自动暂停

        Args:
            result: DefectDetectionResult

        Returns:
            bool: 是否需要自动暂停
        """
        if not self.coordinator_config.auto_pause_on_defect:
            return False

        for cls in result.classes:
            if cls == 0 and self.coordinator_config.auto_pause_on_spaghetti:
                # 意面效应
                return True
            elif cls == 1 and self.coordinator_config.auto_pause_on_zits:
                # 疙瘩
                return True
            elif cls == 2 and self.coordinator_config.auto_pause_on_stringing:
                # 拉丝
                return True

        return False

    def _can_auto_pause_print(self) -> bool:
        """仅在实际打印中允许 AI 发起自动暂停。"""
        try:
            job_state = self.server.lookup_component("job_state")
            job_event = job_state.get_last_job_event()
        except Exception as e:
            logging.warning(
                f"AI Detection: Unable to determine job state before auto-pause: {e}"
            )
            return False
        return job_event in (JobEvent.STARTED, JobEvent.RESUMED)

    async def _pause_print(self, result: DefectDetectionResult) -> None:
        """暂停打印

        Args:
            result: DefectDetectionResult
        """
        if not self._can_auto_pause_print():
            logging.info(
                "AI Detection: Ignoring auto-pause request because printer is not actively printing"
            )
            return

        logging.warning("AI Detection: Auto-pausing print due to defect...")

        # 获取 klippy_apis 组件并暂停
        klippy_apis = self.server.lookup_component("klippy_apis")
        if klippy_apis:
            try:
                await klippy_apis.pause_print()
                logging.info("AI Detection: Print paused successfully")
            except Exception as e:
                logging.error(
                    f"AI Detection: Failed to pause print: {e}"
                )
        else:
            logging.error(
                "AI Detection: klippy_apis component not available for pause"
            )

    async def close(self) -> None:
        """关闭组件，在 Moonraker 关闭时调用"""
        logging.info("AI Detection: Closing client...")
        # 停止检测定时器
        self._stop_defect_timer()
        await self.disconnect()


def load_component(config: ConfigHelper) -> AIDetectionClient:
    return AIDetectionClient(config)
