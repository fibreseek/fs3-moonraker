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
    FLOOR_DETECT = "floor_detect"  # 底板检测
    OBJECT_DETECT = "object_detect"  # 打印前异物/手部/底板异常检测
    DEFECT_DETECT = "defect_detect"  # 缺陷检测


class DetectionScenario(str, Enum):
    """AI 检测场景枚举"""
    PREPRINT_FLOOR = "preprint_floor"
    PREPRINT_OBJECT = "preprint_object"
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

    # Default object detection class mappings
    DEFAULT_OBJECT_CLASSES: Dict[int, ClassMapping] = {
        0: ClassMapping("object", "Foreign object on bed"),
        1: ClassMapping("hand", "Hand detected"),
        2: ClassMapping("floor", "Build plate anomaly"),
    }

    def __init__(self) -> None:
        self.defect_classes: Dict[int, ClassMapping] = dict(
            self.DEFAULT_DEFECT_CLASSES)
        self.object_classes: Dict[int, ClassMapping] = dict(
            self.DEFAULT_OBJECT_CLASSES)

    def load_from_config(self, config: "ConfigHelper") -> None:
        """从配置文件加载类别映射

        配置格式示例:
            defect_class_0: spaghetti, 炒面/意大利面状缺陷
            defect_class_1: zits, 疙瘩/斑点
            object_class_0: object, 底板异物
        """
        # 加载缺陷检测类别
        self._load_class_mappings(config, "defect_class_", self.defect_classes)

        # 加载打印前异物检测类别
        self._load_class_mappings(
            config, "object_class_", self.object_classes)

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

    def get_object_name(self, class_id: int) -> str:
        """获取打印前物体检测类别名称"""
        if class_id in self.object_classes:
            return self.object_classes[class_id].name
        return f"unknown_{class_id}"

    def get_object_description(self, class_id: int) -> str:
        """获取打印前物体检测类别描述"""
        if class_id in self.object_classes:
            return self.object_classes[class_id].description
        return f"unknown_{class_id}"

    def get_all_mappings(self) -> Dict[str, Any]:
        """获取所有类别映射，用于 API 返回"""
        return {
            "defect_classes": {
                class_id: {"name": m.name, "description": m.description}
                for class_id, m in self.defect_classes.items()
            },
            "object_classes": {
                class_id: {"name": m.name, "description": m.description}
                for class_id, m in self.object_classes.items()
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
    obj_thresh: Union[float, Dict[str, float]] = 0.35  # 目标检出的阈值
    nms_thresh: float = 0.45  # 检测框的阈值
    is_lite: Optional[bool] = None  # 是否使用lite模型（仅缺陷检测有效）

    def to_dict(self) -> Dict[str, Union[float, Dict[str, float], bool]]:
        data: Dict[str, Union[float, Dict[str, float], bool]] = {
            "obj_thresh": self.obj_thresh,
            "nms_thresh": self.nms_thresh,
        }
        if self.is_lite is not None:
            data["is_lite"] = self.is_lite
        return data


# 上位机三档灵敏度：对应下发给 AI 服务的 obj_thresh（置信度门槛）
SENSITIVITY_OBJ_THRESH: Dict[str, float] = {
    "high": 0.35,
    "medium": 0.55,
    "low": 0.7,
}

_SENSITIVITY_LABEL_TO_KEY: Dict[str, str] = {
    "high": "high",
    "medium": "medium",
    "low": "low",
}
_OBJECT_CLASS_IDS = ("0", "1", "2")
_DEFECT_CLASS_IDS = ("0", "1", "2")

# 检测投票：最多 N 次；累计或连续达到阈值即报错，并支持提前结束
DETECT_VOTE_ATTEMPTS = 5
DETECT_VOTE_ERROR_THRESHOLD = 3


@dataclass
class _VoteRoundResult:
    should_fail: bool
    trigger_count: int
    attempts_used: int


def _canonical_sensitivity_key(value: str) -> str:
    """将 API 档位标签规范为 high|medium|low（英文，大小写不敏感）。非法则抛 ValueError。"""
    key = value.strip().lower()
    if key in _SENSITIVITY_LABEL_TO_KEY:
        return _SENSITIVITY_LABEL_TO_KEY[key]
    raise ValueError(f"invalid sensitivity label: {value!r}")


def _expand_threshold(threshold: float, class_ids: tuple[str, ...]) -> Dict[str, float]:
    return {class_id: threshold for class_id in class_ids}


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
class ObjectDetectionResult:
    """打印前物体检测结果数据类"""
    is_valid: bool
    has_object: bool = False  # 是否有异物
    scores: List[float] = field(default_factory=list)  # 置信度
    boxes: List[List[float]] = field(default_factory=list)  # 异物目标框
    classes: List[int] = field(default_factory=list)  # 异物所属类别
    build_plate_issue: bool = False  # 是否检测到底板异常

    @property
    def class_names(self) -> List[str]:
        """获取类别名称列表"""
        registry = get_class_registry()
        return [registry.get_object_name(c) for c in self.classes]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ObjectDetectionResult":
        classes: List[int] = data.get("classes", []) or []
        build_plate_issue = 2 in classes
        return cls(
            is_valid=data.get("is_valid", False),
            has_object=data.get("has_object", False),
            scores=data.get("scores", []),
            boxes=data.get("boxes", []),
            classes=classes,
            build_plate_issue=build_plate_issue,
        )

    def to_dict(self) -> Dict[str, Any]:
        registry = get_class_registry()
        return {
            "is_valid": self.is_valid,
            "has_object": self.has_object,
            "scores": self.scores,
            "boxes": self.boxes,
            "classes": self.classes,
            "class_names": [registry.get_object_name(c) for c in self.classes],
            "build_plate_issue": self.build_plate_issue,
        }


@dataclass
class FloorDetectionResult:
    """打印前底板检测结果数据类"""
    is_valid: bool
    has_build_plate: bool = False
    scores: List[float] = field(default_factory=list)
    boxes: List[List[float]] = field(default_factory=list)
    classes: List[int] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FloorDetectionResult":
        classes: List[int] = data.get("classes", []) or []
        has_build_plate = bool(data.get("has_object", False) or 2 in classes)
        return cls(
            is_valid=data.get("is_valid", False),
            has_build_plate=has_build_plate,
            scores=data.get("scores", []),
            boxes=data.get("boxes", []),
            classes=classes,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "has_build_plate": self.has_build_plate,
            "scores": self.scores,
            "boxes": self.boxes,
            "classes": self.classes,
        }


@dataclass
class CoordinatorConfig:
    """AI检测协调器配置"""
    # AI总开关
    enable: bool = True

    # 检测开关
    enable_floor_detect: bool = True
    enable_object_detect: bool = True
    enable_defect_detect: bool = True

    # 检测间隔（秒）
    detect_interval: float = 10.0

    @classmethod
    def from_config(cls, config: "ConfigHelper") -> "CoordinatorConfig":
        """从配置文件加载配置"""
        return cls(
            enable=config.getboolean("enable", True),
            enable_floor_detect=config.getboolean(
                "enable_floor_detect", True
            ),
            enable_object_detect=config.getboolean(
                "enable_object_detect", True
            ),
            enable_defect_detect=config.getboolean(
                "enable_defect_detect", True
            ),
            detect_interval=config.getfloat(
                "detect_interval", 10.0, minval=1.0
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "enable": self.enable,
            "enable_floor_detect": self.enable_floor_detect,
            "enable_object_detect": self.enable_object_detect,
            "enable_defect_detect": self.enable_defect_detect,
            "detect_interval": self.detect_interval,
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

        # 运行时检测参数
        self.nms_thresh: float = 0.45
        self.floor_sensitivity: str = _canonical_sensitivity_key(
            config.get("floor_sensitivity", "medium")
        )
        self.object_sensitivity: str = _canonical_sensitivity_key(
            config.get("object_sensitivity", "medium")
        )
        self.defect_sensitivity: str = _canonical_sensitivity_key(
            config.get("defect_sensitivity", "medium")
        )

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

    def _resolve_threshold(self, override: Optional[float], sensitivity: str) -> float:
        if override is not None:
            return float(override)
        return SENSITIVITY_OBJ_THRESH[sensitivity]

    def _effective_floor_obj_thresh(self, override: Optional[float] = None) -> float:
        return self._resolve_threshold(override, self.floor_sensitivity)

    def _effective_object_obj_thresh(
        self, override: Optional[float] = None
    ) -> Dict[str, float]:
        threshold = self._resolve_threshold(override, self.object_sensitivity)
        return _expand_threshold(threshold, _OBJECT_CLASS_IDS)

    def _effective_defect_obj_thresh(
        self, override: Optional[float] = None
    ) -> Dict[str, float]:
        threshold = self._resolve_threshold(override, self.defect_sensitivity)
        return _expand_threshold(threshold, _DEFECT_CLASS_IDS)

    def _effective_thresholds(self) -> Dict[str, Any]:
        return {
            "floor_detect": self._effective_floor_obj_thresh(),
            "object_detect": self._effective_object_obj_thresh(),
            "defect_detect": self._effective_defect_obj_thresh(),
        }

    def _config_notification_payload(self) -> Dict[str, Any]:
        return {
            **self.coordinator_config.to_dict(),
            "floor_sensitivity": self.floor_sensitivity,
            "object_sensitivity": self.object_sensitivity,
            "defect_sensitivity": self.defect_sensitivity,
            "effective_thresholds": self._effective_thresholds(),
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

    async def set_enabled(self, enable: bool) -> Dict[str, Any]:
        """更新 AI 总开关并持久化到数据库。"""
        old_enable = self.coordinator_config.enable
        if old_enable == enable:
            return self._config_notification_payload()

        self.coordinator_config.enable = enable
        await self._persist_config({
            "coordinator_config": self.coordinator_config.to_dict()
        })

        self.server.send_event(
            "ai_detection:config_updated",
            self._config_notification_payload(),
        )

        if not old_enable and self.coordinator_config.enable:
            self._maybe_start_defect_detection()
        elif old_enable and not self.coordinator_config.enable and self._is_detecting:
            logging.info("AI Detection: Disabled, stopping defect detection timer...")
            self._stop_defect_timer()

        return self._config_notification_payload()

    def is_floor_detect_enabled(self) -> bool:
        """检查是否启用底板检测"""
        return (
            self.coordinator_config.enable
            and self.coordinator_config.enable_floor_detect
            and self.is_connected
        )

    def is_object_detect_enabled(self) -> bool:
        """检查是否启用打印前物体检测"""
        return (
            self.coordinator_config.enable
            and self.coordinator_config.enable_object_detect
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
                    if request_id.startswith(DetectionType.FLOOR_DETECT.value):
                        detection_type = "floor"
                    elif request_id.startswith(DetectionType.OBJECT_DETECT.value):
                        detection_type = "object"
                    if request_id.startswith(DetectionType.DEFECT_DETECT.value):
                        detection_type = "defect"

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

    async def _run_vote_detection_round(
        self,
        label: str,
        detect_once: Callable[[], Coroutine[Any, Any, bool]],
        *,
        count_exceptions_as_trigger: bool = True,
    ) -> _VoteRoundResult:
        """多轮检测投票：连续达到阈值或累计无法再达标时提前结束。"""
        trigger_count = 0
        consecutive_triggers = 0

        for attempt in range(1, DETECT_VOTE_ATTEMPTS + 1):
            triggered = False
            try:
                triggered = await detect_once()
            except Exception as e:
                if count_exceptions_as_trigger:
                    triggered = True
                logging.warning(
                    "AI Detection: %s attempt %d/%d failed: %s",
                    label,
                    attempt,
                    DETECT_VOTE_ATTEMPTS,
                    e,
                )

            if triggered:
                trigger_count += 1
                consecutive_triggers += 1
                status = "triggered"
            else:
                consecutive_triggers = 0
                status = "ok"

            logging.info(
                "AI Detection: %s attempt %d/%d: %s",
                label,
                attempt,
                DETECT_VOTE_ATTEMPTS,
                status,
            )

            if consecutive_triggers >= DETECT_VOTE_ERROR_THRESHOLD:
                logging.warning(
                    "AI Detection: %s stopped early after %d consecutive "
                    "triggers (%d/%d attempts used)",
                    label,
                    consecutive_triggers,
                    trigger_count,
                    attempt,
                )
                return _VoteRoundResult(True, trigger_count, attempt)

            remaining = DETECT_VOTE_ATTEMPTS - attempt
            if trigger_count + remaining < DETECT_VOTE_ERROR_THRESHOLD:
                logging.info(
                    "AI Detection: %s passed early after %d attempts "
                    "(%d triggers, cannot reach threshold)",
                    label,
                    attempt,
                    trigger_count,
                )
                return _VoteRoundResult(False, trigger_count, attempt)

        should_fail = trigger_count >= DETECT_VOTE_ERROR_THRESHOLD
        return _VoteRoundResult(should_fail, trigger_count, DETECT_VOTE_ATTEMPTS)

    async def _detect_defect_once(
        self,
        obj_thresh: Optional[float] = None,
        nms_thresh: Optional[float] = None,
        is_lite: bool = False,
    ) -> DefectDetectionResult:
        """执行单次缺陷检测，不发送通知。"""
        params = DetectionParams(
            obj_thresh=self._effective_defect_obj_thresh(obj_thresh),
            nms_thresh=nms_thresh if nms_thresh is not None else self.nms_thresh,
            is_lite=is_lite
        )
        result_data = await self._send_request(DetectionType.DEFECT_DETECT, params)
        return DefectDetectionResult.from_dict(result_data)

    async def detect_defect(
        self,
        obj_thresh: Optional[float] = None,
        nms_thresh: Optional[float] = None,
        is_lite: bool = False,
        scenario: DetectionScenario = DetectionScenario.PRINTING_DEFECT_MANUAL
    ) -> DefectDetectionResult:
        """执行打印缺陷检测

        Args:
            obj_thresh: 检测阈值
            nms_thresh: NMS阈值
            is_lite: 是否使用lite模型
            scenario: 检测场景

        Returns:
            DefectDetectionResult: 缺陷检测结果
        """
        if not self.is_connected:
            return DefectDetectionResult(is_valid=False, warning=False)

        result = await self._detect_defect_once(
            obj_thresh=obj_thresh,
            nms_thresh=nms_thresh,
            is_lite=is_lite,
        )

        if result.warning:
            logging.warning(
                f"AI 检测: 检测到缺陷 - "
                f"类别: {result.classes}, 位置: {result.boxes}, "
                f"置信度: {result.scores}"
            )

        return result

    async def _detect_floor_once(
        self,
        obj_thresh: Optional[float] = None,
        nms_thresh: Optional[float] = None,
    ) -> tuple[str, Optional[str]]:
        """执行单次底板检测，不发送通知。返回 (status, error_message)。"""
        params = DetectionParams(
            obj_thresh=self._effective_floor_obj_thresh(obj_thresh),
            nms_thresh=nms_thresh if nms_thresh is not None else self.nms_thresh
        )
        result_data = await self._send_request(DetectionType.FLOOR_DETECT, params)
        result = FloorDetectionResult.from_dict(result_data)
        if result.has_build_plate:
            return "ok", None

        return "error", "AI Detection: Build plate not detected"

    async def detect_floor(
        self,
        obj_thresh: Optional[float] = None,
        nms_thresh: Optional[float] = None,
        scenario: DetectionScenario = DetectionScenario.PREPRINT_FLOOR
    ) -> str:
        """执行打印前底板检测（连续多次，多数失败才报错）"""
        if not self.is_connected:
            msg = "AI Detection service is not connected"
            self._send_detection_notification(
                scenario=scenario,
                detection_type="floor",
                message=msg
            )
            return "error"
        if not self.coordinator_config.enable or not self.coordinator_config.enable_floor_detect:
            logging.info("AI Detection is disabled, skipping floor detection")
            return "ok"

        last_error_msg = "AI Detection: Build plate not detected"

        async def _once() -> bool:
            nonlocal last_error_msg
            try:
                status, error_msg = await self._detect_floor_once(
                    obj_thresh=obj_thresh,
                    nms_thresh=nms_thresh,
                )
            except Exception as e:
                last_error_msg = str(e)
                raise
            if status == "error":
                if error_msg:
                    last_error_msg = error_msg
                return True
            return False

        vote = await self._run_vote_detection_round(
            "Floor detection", _once
        )

        if vote.should_fail:
            logging.warning(
                "AI Detection: Build plate check failed "
                "(%d/%d attempts triggered)",
                vote.trigger_count,
                vote.attempts_used,
            )
            self._send_detection_notification(
                scenario=scenario,
                detection_type="floor",
                message=last_error_msg,
            )
            return "error"

        logging.info(
            "AI Detection: Build plate detected "
            "(%d/%d attempts triggered, below threshold)",
            vote.trigger_count,
            vote.attempts_used,
        )
        return "ok"

    async def _detect_object_once(
        self,
        obj_thresh: Optional[float] = None,
        nms_thresh: Optional[float] = None,
    ) -> tuple[str, Optional[str]]:
        """执行单次异物检测，不发送通知。返回 (status, error_message)。"""
        params = DetectionParams(
            obj_thresh=self._effective_object_obj_thresh(obj_thresh),
            nms_thresh=nms_thresh if nms_thresh is not None else self.nms_thresh
        )

        result_data = await self._send_request(DetectionType.OBJECT_DETECT, params)
        result = ObjectDetectionResult.from_dict(result_data)

        logging.warning(
            f"AI Detection: Object detection result - "
            f"Class: {result.classes}, Box: {result.boxes}, "
            f"Score: {result.scores}"
        )

        if result.build_plate_issue:
            return "error", "AI Detection: Build plate not detected"

        if result.is_valid and result.has_object:
            if 1 in result.classes:
                return "error", "AI Detection: Hand detected"
            return "error", "AI Detection: Foreign object detected"

        return "ok", None

    async def detect_object(
        self,
        obj_thresh: Optional[float] = None,
        nms_thresh: Optional[float] = None,
        scenario: DetectionScenario = DetectionScenario.PREPRINT_OBJECT
    ) -> str:
        """执行打印前异物/手部/底板异常检测（连续多次，多数失败才报错）"""
        if not self.is_connected:
            msg = "AI Detection service is not connected"
            self._send_detection_notification(
                scenario=scenario,
                detection_type="object",
                message=msg
            )
            return "error"
        if not self.coordinator_config.enable or not self.coordinator_config.enable_object_detect:
            logging.info("AI Detection is disabled, skipping object detection")
            return "ok"

        last_error_msg = "AI Detection: Object detection failed"

        async def _once() -> bool:
            nonlocal last_error_msg
            try:
                status, error_msg = await self._detect_object_once(
                    obj_thresh=obj_thresh,
                    nms_thresh=nms_thresh,
                )
            except Exception as e:
                last_error_msg = str(e)
                raise
            if status == "error":
                if error_msg:
                    last_error_msg = error_msg
                return True
            return False

        vote = await self._run_vote_detection_round(
            "Object detection", _once
        )

        if vote.should_fail:
            logging.warning(
                "AI Detection: Object check failed "
                "(%d/%d attempts triggered)",
                vote.trigger_count,
                vote.attempts_used,
            )
            self._send_detection_notification(
                scenario=scenario,
                detection_type="object",
                message=last_error_msg,
            )
            return "error"

        logging.info(
            "AI Detection: No objects detected "
            "(%d/%d attempts triggered, below threshold)",
            vote.trigger_count,
            vote.attempts_used,
        )
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
                # AI检测配置
                "floor_sensitivity": self.floor_sensitivity,
                "object_sensitivity": self.object_sensitivity,
                "defect_sensitivity": self.defect_sensitivity,
                "effective_thresholds": self._effective_thresholds(),
                # 协调器配置
                "enable": self.coordinator_config.enable,
                "enable_floor_detect": self.coordinator_config.enable_floor_detect,
                "enable_object_detect": self.coordinator_config.enable_object_detect,
                "enable_defect_detect": self.coordinator_config.enable_defect_detect,
                "detect_interval": self.coordinator_config.detect_interval,
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
        """统一处理AI检测配置更新。"""
        params = web_request.get_args() or {}

        if not isinstance(params, dict):
            raise self.server.error("Invalid params format, must be JSON object", 400)

        if not params:
            raise self.server.error("No configuration parameters provided", 400)

        allowed_keys = {
            "enable",
            "enable_floor_detect",
            "enable_object_detect",
            "enable_defect_detect",
            "floor_sensitivity",
            "object_sensitivity",
            "defect_sensitivity",
            "detect_interval",
        }
        invalid_keys = sorted(set(params.keys()) - allowed_keys)
        if invalid_keys:
            raise self.server.error(
                f"Unsupported configuration fields: {', '.join(invalid_keys)}", 400
            )

        sensitivity_fields = (
            "floor_sensitivity",
            "object_sensitivity",
            "defect_sensitivity",
        )
        bool_fields = (
            "enable",
            "enable_floor_detect",
            "enable_object_detect",
            "enable_defect_detect",
        )
        coord_updates: Dict[str, Any] = {}
        persist_updates: Dict[str, Any] = {}
        old_enable = self.coordinator_config.enable

        for field in sensitivity_fields:
            if field not in params:
                continue
            value = params[field]
            if not isinstance(value, str):
                raise self.server.error(f"{field} must be high, medium, or low", 400)
            try:
                canon = _canonical_sensitivity_key(value)
            except ValueError:
                raise self.server.error(f"{field} must be high, medium, or low", 400)
            setattr(self, field, canon)
            persist_updates[field] = canon

        for field in bool_fields:
            if field not in params:
                continue
            value = params[field]
            if isinstance(value, bool):
                coord_updates[field] = value
            elif isinstance(value, str) and value.lower() in ("true", "1", "yes", "false", "0", "no"):
                coord_updates[field] = value.lower() in ("true", "1", "yes")
            else:
                raise self.server.error(f"{field} must be a boolean", 400)

        if "detect_interval" in params:
            value = params["detect_interval"]
            if not isinstance(value, (int, float)):
                raise self.server.error("detect_interval must be a number", 400)
            value = float(value)
            if value < 1.0:
                raise self.server.error("detect_interval must be at least 1.0", 400)
            coord_updates["detect_interval"] = value

        if not persist_updates and not coord_updates:
            raise self.server.error("No valid configuration parameters provided", 400)

        if coord_updates:
            self.coordinator_config.update(**coord_updates)
            persist_updates["coordinator_config"] = self.coordinator_config.to_dict()

        await self._persist_config(persist_updates)
        self.server.send_event(
            "ai_detection:config_updated",
            self._config_notification_payload(),
        )

        if not old_enable and self.coordinator_config.enable:
            self._maybe_start_defect_detection()
        elif old_enable and not self.coordinator_config.enable and self._is_detecting:
            logging.info("AI Detection: Disabled, stopping defect detection timer...")
            self._stop_defect_timer()

        return self._config_notification_payload()

    async def _load_config_from_database(self) -> None:
        """从数据库加载持久化的配置"""
        try:
            for field in ("floor_sensitivity", "object_sensitivity", "defect_sensitivity"):
                value = await self.database.get_item("ai_detection", field, None)
                if value is None:
                    continue
                try:
                    setattr(self, field, _canonical_sensitivity_key(str(value)))
                except ValueError:
                    logging.warning(
                        "AI Detection: invalid %s in DB, ignoring: %r",
                        field,
                        value,
                    )

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
            for key, value in config_updates.items():
                if key in ("floor_sensitivity", "object_sensitivity", "defect_sensitivity"):
                    self.database.insert_item("ai_detection", key, value)
                elif key == "coordinator_config":
                    self.database.insert_item("ai_detection", "coordinator_config", value)

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
        """执行一轮缺陷检测（连续多次，多数 warning 才报警并暂停）"""
        if not self.is_connected:
            logging.warning(
                "AI Detection: Not connected for defect detection, skipping..."
            )
            return

        logging.debug("AI Detection: Performing defect detection...")
        last_warning_result: Optional[DefectDetectionResult] = None

        async def _once() -> bool:
            nonlocal last_warning_result
            result = await self._detect_defect_once()
            if result.warning:
                last_warning_result = result
                return True
            return False

        vote = await self._run_vote_detection_round(
            "Defect detection",
            _once,
            count_exceptions_as_trigger=False,
        )

        if not vote.should_fail:
            logging.debug(
                "AI Detection: Defect check passed "
                "(%d/%d attempts warned, below threshold)",
                vote.trigger_count,
                vote.attempts_used,
            )
            return

        result = last_warning_result or DefectDetectionResult(
            is_valid=False, warning=True
        )
        logging.warning(
            "AI Detection: Defect check failed "
            "(%d/%d attempts warned) - "
            "Classes: %s, Boxes: %s",
            vote.trigger_count,
            vote.attempts_used,
            result.classes,
            result.boxes,
        )

        class_registry = get_class_registry()
        class_names = [class_registry.get_defect_name(cls) for cls in result.classes]
        defect_types = ", ".join(class_names) if class_names else "unknown"
        message = f"Defect detected: {defect_types}"
        logging.info(f"AI Detection: Defect message: {message}")

        self._send_detection_notification(
            scenario=DetectionScenario.PRINTING_DEFECT_PERIODIC,
            detection_type="defect",
            message=message
        )

        await self._pause_print()

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

    async def _pause_print(self) -> None:
        """暂停打印。"""
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
