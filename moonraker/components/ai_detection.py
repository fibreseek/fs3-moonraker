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

from ..common import RequestType
from ..utils import json_wrapper as jsonw

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest
    from ..eventloop import FlexTimer


class DetectionType(str, Enum):
    """AI 检测类型枚举"""
    DEFECT_DETECT = "defect_detect"  # 缺陷检测
    FOREIGN_OBJECT_DETECT = "preprint_object_detect"  # 平台异物检测


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

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DefectDetectionResult":
        return cls(
            is_valid=data.get("is_valid", False),
            boxes=data.get("boxes", []),
            classes=data.get("classes", []),
            scores=data.get("scores", []),
            warning=data.get("WARNING", data.get("warning", False))
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
        2 - 检测到手
    """
    is_valid: bool
    has_object: bool = False  # 是否有异物
    scores: List[float] = field(default_factory=list)  # 置信度
    boxes: List[List[float]] = field(default_factory=list)  # 异物目标框
    classes: List[int] = field(default_factory=list)  # 异物所属类别

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ForeignDetectionResult":
        return cls(
            is_valid=data.get("is_valid", False),
            has_object=data.get("has_object", False),
            scores=data.get("scores", []),
            boxes=data.get("boxes", []),
            classes=data.get("classes", [])
        )

    def to_dict(self) -> Dict[str, Any]:
        registry = get_class_registry()
        return {
            "is_valid": self.is_valid,
            "has_object": self.has_object,
            "scores": self.scores,
            "boxes": self.boxes,
            "classes": self.classes,
            "class_names": [registry.get_foreign_name(c) for c in self.classes]
        }


class AIDetectionClient:
    """AI 检测服务 WebSocket 客户端"""

    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()  # 获取 Moonraker 服务器实例
        self.eventloop = self.server.get_event_loop()  # 获取事件循环

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

        # 连接状态
        self._state: ConnectionState = ConnectionState.DISCONNECTED # 初始状态
        self._websocket: Optional[WebSocketClientProtocol] = None   # WebSocket 连接
        self._connect_task: Optional[asyncio.Task] = None           # 连接任务
        self._read_task: Optional[asyncio.Task] = None              # 读取任务
        self._closing: bool = False                                 # 关闭标志  
        self._connection_mutex: asyncio.Lock = asyncio.Lock()       # 连接互斥锁

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

        # 注册 API 端点
        # 状态和配置端点
        self.server.register_endpoint(
            "/server/ai_detection/status",
            RequestType.GET,
            self._handle_status_request
        )
        # 配置更新端点
        self.server.register_endpoint(
            "/server/ai_detection/config",
            RequestType.POST,
            self._handle_config_update_request
        )
        # 类别映射端点
        self.server.register_endpoint(
            "/server/ai_detection/class_mappings",
            RequestType.GET,
            self._handle_class_mappings_request
        )
        # 检测端点
        self.server.register_endpoint(
            "/server/ai_detection/detect/defect",
            RequestType.POST,
            self._handle_defect_detect_request
        )
        # 打印前平台异物检测端点
        self.server.register_endpoint(
            "/server/ai_detection/detect/foreign",
            RequestType.POST,
            self._handle_foreign_detect_request
        )
        # 连接管理端点
        self.server.register_endpoint(
            "/server/ai_detection/connect",
            RequestType.POST,
            self._handle_connect_request
        )
        # 断开连接端点
        self.server.register_endpoint(
            "/server/ai_detection/disconnect",
            RequestType.POST,
            self._handle_disconnect_request
        )

        # 注册通知
        self.server.register_notification("ai_detection:state_changed")
        self.server.register_notification("ai_detection:detection_result")
        self.server.register_notification("ai_detection:warning")

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

    async def component_init(self) -> None:
        """组件初始化，在 Moonraker 启动后调用"""
        if self.auto_connect:
            logging.info("AI Detection: Auto-connecting to server...")
            self._connect_task = self.eventloop.create_task(self._do_connect())

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
            except ConnectionRefusedError:
                self._stats["last_error"] = "Connection refused"
                logging.warning(
                    f"AI Detection: Connection refused by {ws_uri}"
                )
            except (InvalidURI, InvalidHandshake) as e:
                self._stats["last_error"] = f"WebSocket handshake failed: {e}"
                logging.warning(
                    f"AI Detection: WebSocket handshake failed: {e}"
                )
            except Exception as e:
                self._stats["last_error"] = str(e)
                logging.exception(
                    f"AI Detection: Failed to connect to {ws_uri}"
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
        except ConnectionClosed as e:
            logging.info(f"AI Detection: WebSocket connection closed: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.exception(f"AI Detection: Read loop error: {e}")
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
        is_lite: bool = False
    ) -> DefectDetectionResult:
        """执行打印缺陷检测"""
        params = DetectionParams(
            obj_thresh=obj_thresh if obj_thresh is not None else self.obj_thresh,
            nms_thresh=nms_thresh if nms_thresh is not None else self.nms_thresh,
            is_lite=is_lite
        )

        result_data = await self._send_request(DetectionType.DEFECT_DETECT, params)
        result = DefectDetectionResult.from_dict(result_data)

        # 发送检测结果通知
        self.server.send_event(
            "ai_detection:detection_result",
            {
                "type": DetectionType.DEFECT_DETECT.value,
                "result": result.to_dict()
            }
        )

        # 如果有警告，发送警告通知
        if result.warning:
            self.server.send_event(
                "ai_detection:warning",
                {
                    "type": DetectionType.DEFECT_DETECT.value,
                    "message": "Defect warning triggered",
                    "result": result.to_dict()
                }
            )
            logging.warning("AI Detection: Defect warning triggered!")

        return result

    async def detect_foreign_object(
        self,
        obj_thresh: Optional[float] = None,
        nms_thresh: Optional[float] = None
    ) -> ForeignDetectionResult:
        """执行打印前平台异物检测"""
        params = DetectionParams(
            obj_thresh=obj_thresh if obj_thresh is not None else self.obj_thresh,
            nms_thresh=nms_thresh if nms_thresh is not None else self.nms_thresh
        )

        result_data = await self._send_request(
            DetectionType.FOREIGN_OBJECT_DETECT, params
        )
        result = ForeignDetectionResult.from_dict(result_data)

        # 发送检测结果通知
        self.server.send_event(
            "ai_detection:detection_result",
            {
                "type": DetectionType.FOREIGN_OBJECT_DETECT.value,
                "result": result.to_dict()
            }
        )

        # 如果检测到异物，发送警告通知
        if result.has_object:
            self.server.send_event(
                "ai_detection:warning",
                {
                    "type": DetectionType.FOREIGN_OBJECT_DETECT.value,
                    "message": "Foreign object detected on print bed",
                    "result": result.to_dict()
                }
            )
            logging.warning(
                "AI Detection: Foreign object detected on print bed!")

        return result

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
            "state": self._state.value,
            "is_connected": self.is_connected,
            "host": self.host,
            "port": self.port,
            "stats": dict(self._stats),
            "config": {
                "reconnect_interval": self.reconnect_interval,
                "request_timeout": self.request_timeout,
                "auto_connect": self.auto_connect,
                "obj_thresh": self.obj_thresh,
                "nms_thresh": self.nms_thresh,
            },
            "class_mappings": self.class_registry.get_all_mappings()
        }

    # API 端点处理函数

    async def _handle_status_request(self, web_request: WebRequest) -> Dict[str, Any]:
        """处理状态请求"""
        return self.get_status()

    async def _handle_class_mappings_request(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        """处理类别映射请求"""
        return self.class_registry.get_all_mappings()

    async def _handle_config_update_request(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        """处理检测配置更新"""

        # JSON-RPC 的 params
        params = web_request.get_json() or {}

        if not isinstance(params, dict):
            raise self.server.error("Invalid params format, must be JSON object", 400)

        updated = False

        def _validate_range(name: str, value: float):
            if not (0.0 <= value <= 1.0):
                raise self.server.error(
                    f"{name} value {value} out of range [0.0, 1.0]", 400
                )

        # obj_thresh
        if "obj_thresh" in params:
            try:
                val = float(params["obj_thresh"])
            except Exception:
                raise self.server.error("obj_thresh must be a number", 400)

            _validate_range("obj_thresh", val)
            self.obj_thresh = val
            updated = True

        # nms_thresh
        if "nms_thresh" in params:
            try:
                val = float(params["nms_thresh"])
            except Exception:
                raise self.server.error("nms_thresh must be a number", 400)

            _validate_range("nms_thresh", val)
            self.nms_thresh = val
            updated = True

        if not updated:
            raise self.server.error(
                "No valid configuration parameters provided", 400
            )

        return {
            "obj_thresh": self.obj_thresh,
            "nms_thresh": self.nms_thresh
        }

    
    async def _handle_defect_detect_request(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        """处理打印缺陷检测请求"""
        obj_thresh = web_request.get_float("obj_thresh", None)
        nms_thresh = web_request.get_float("nms_thresh", None)
        is_lite = web_request.get_boolean("is_lite", False)

        result = await self.detect_defect(obj_thresh, nms_thresh, is_lite)
        return result.to_dict()

    async def _handle_foreign_detect_request(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        """处理打印前平台异物检测请求"""
        obj_thresh = web_request.get_float("obj_thresh", None)
        nms_thresh = web_request.get_float("nms_thresh", None)

        result = await self.detect_foreign_object(obj_thresh, nms_thresh)
        return result.to_dict()

    async def _handle_connect_request(self, web_request: WebRequest) -> Dict[str, Any]:
        """处理连接请求"""
        success = await self.connect()
        return {
            "success": success,
            "state": self._state.value
        }

    async def _handle_disconnect_request(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        """处理断开连接请求"""
        await self.disconnect()
        return {
            "success": True,
            "state": self._state.value
        }

    async def close(self) -> None:
        """关闭组件，在 Moonraker 关闭时调用"""
        logging.info("AI Detection: Closing client...")
        await self.disconnect()


def load_component(config: ConfigHelper) -> AIDetectionClient:
    return AIDetectionClient(config)
