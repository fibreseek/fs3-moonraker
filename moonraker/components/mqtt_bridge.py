# MQTT Bridge Component
# 用于管理上位机与MQTT服务之间的绑定关系
# 文档：https://dcnb6knrfczq.feishu.cn/wiki/B7TdwkUzKiuDTaknvOfcR8mVnRs

from __future__ import annotations
import time
import logging
from ..common import RequestType
from typing import Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest

# 生命周期状态常量
STATE_UNBOUND = "unbound"
STATE_BINDING = "binding"
STATE_BOUND = "bound"
STATE_FAILED = "fail"

# 绑定超时时间（秒）
BIND_TIMEOUT = 30.0


class MQTTBridge:
    """
    MQTT桥接组件

    负责管理上位机与MQTT服务器之间的绑定状态，
    提供绑定请求、解绑和状态查询等功能。
    """

    def __init__(self, config: ConfigHelper) -> None:
        """初始化MQTT桥接组件"""
        self.server = config.get_server()
        self.database = self.server.lookup_component("database")

        # 初始化状态字典
        self._status: Dict[str, Any] = {
            "lifecycle_state": STATE_UNBOUND,
            "auth_code": "",
            "user_info": {},
            "error_msg": "",
            "request_ts": 0.0,
            "unbind_source": ""  # 记录解绑发起方: "host" | "mqtt" | ""
        }

        # 绑定超时配置（秒）
        self.bind_timeout = config.getfloat("bind_timeout", BIND_TIMEOUT)
        # 失败后延迟3秒回退到 unbound
        self.fail_revert_delay = 3.0
        # 保存可能存在的回退任务
        self._fail_revert_handle: Optional[object] = None
        self.eventloop = self.server.get_event_loop()

        # 注册 WebSocket 端点
        self.server.register_endpoint(
            "/server/mqtt_bridge/request_bind",
            RequestType.POST,
            self._handle_request_bind
        )
        self.server.register_endpoint(
            "/server/mqtt_bridge/request_unbind",
            RequestType.POST,
            self._handle_request_unbind
        )
        self.server.register_endpoint(
            "/server/mqtt_bridge/report_result",
            RequestType.POST,
            self._handle_report_result
        )
        self.server.register_endpoint(
            "/server/mqtt_bridge/status",
            RequestType.GET,
            self._handle_get_status
        )

        # 注册状态通知 - 会生成 notify_mqtt_bridge_status 通知
        self.server.register_notification(
            "mqtt_bridge:status_changed", "mqtt_bridge_status")

        logging.info("MQTT Bridge 组件已初始化")

    async def component_init(self) -> None:
        """组件初始化完成后的回调 - 恢复持久化状态"""
        # 从数据库恢复状态
        stored_state = await self.database.get_item(
            "moonraker", "mqtt_bridge.state", None
        )

        if stored_state and stored_state.get("lifecycle_state") == STATE_BOUND:
            self._status.update(stored_state)
            # 重启后不恢复 request_ts，避免误触发超时
            self._status["request_ts"] = 0.0
            self._status["unbind_source"] = ""

            logging.info(
                f"恢复绑定状态: {stored_state.get('user_info', {}).get('username', 'unknown')}"
            )

            # 广播状态通知
            self._notify_status_changed()
        
        # 注册后端定时超时检测（watchdog）
        self.timeout_timer = self.eventloop.register_timer(self._check_bind_timeout)
        self.timeout_timer.start()  

    async def _handle_request_bind(self, web_request: WebRequest) -> Dict[str, str]:
        """
        处理上位机端发起的绑定请求

        Args:
            web_request: Web请求对象

        Returns:
            包含操作结果的字典
        """
        code = web_request.get_str("code")

        # 验证输入
        if not code:
            raise self.server.error("Verification code must not be empty.", 400)

        code = code.strip()
        if not code:
            raise self.server.error("Verification code must not be blank.", 400)

        # 更新状态
        self._status["lifecycle_state"] = STATE_BINDING
        self._status["auth_code"] = code
        self._status["request_ts"] = time.monotonic()
        self._status["error_msg"] = ""

        # 发送状态变更通知
        self._notify_status_changed()

        logging.info(f"收到绑定请求，验证码: {code[:4]}****")

        return {"result": "ok"}

    async def _handle_request_unbind(self, web_request: WebRequest) -> Dict[str, str]:
        """
        处理解绑请求（支持双向发起）

        Args:
            web_request: Web请求对象，可包含 source 参数标识发起方

        Returns:
            包含操作结果的字典
        """
        # 获取发起方标识（"host" 或 "mqtt"），默认为 "host"
        source = web_request.get_str("source", "host")

        if source not in ["host", "mqtt"]:
            raise self.server.error("Invalid 'source' parameter. Expected 'host' or 'mqtt'.", 400)

        previous_state = self._status["lifecycle_state"]

        # 状态检查：在 unbound 状态下记录无效解绑
        if previous_state == STATE_UNBOUND:
            logging.info(
                f"忽略解绑请求：设备已处于 unbound 状态（发起方: {source}）"
            )
            return {"result": "ok", "message": "already unbound"}

        # 重置状态
        self._status["lifecycle_state"] = STATE_UNBOUND
        self._status["auth_code"] = ""
        self._status["user_info"] = {}
        self._status["error_msg"] = ""
        self._status["request_ts"] = time.monotonic()
        self._status["unbind_source"] = source

        # 清除持久化状态
        await self._persist_state()

        # 发送状态变更通知
        self._notify_status_changed()

        logging.info(f"执行解绑操作，发起方: {source}，先前状态: {previous_state}")

        # 清除 unbind_source，避免影响下次绑定
        self._status["unbind_source"] = ""

        # 取消计划中的失败回退
        self._cancel_fail_revert()

        return {"result": "ok"}

    async def _handle_report_result(self, web_request: WebRequest) -> Dict[str, str]:
        """
        处理MQTT端汇报的绑定结果（仅在 binding 状态接受）

        Args:
            web_request: Web请求对象

        Returns:
            包含操作结果的字典
        """
        # 状态校验：仅在 binding 状态接受报告
        if self._status["lifecycle_state"] != STATE_BINDING:
            current_state = self._status["lifecycle_state"]
            logging.warning(
                f"拒绝 report_result：当前状态为 '{current_state}'，该接口仅在 lifecycle_state='binding' 时接受请求。"
            )
            raise self.server.error(
                f"Cannot accept bind result while current state is '{current_state}'.",
                400
            )

        success = web_request.get_boolean("success")
        user_data = web_request.get("user_data", None)
        error = web_request.get_str("error", "")

        if success:
            self._status["lifecycle_state"] = STATE_BOUND
            self._status["user_info"] = user_data if user_data else {}
            self._status["error_msg"] = ""
            self._status["auth_code"] = ""  # 清空验证码

            logging.info("MQTT绑定成功")

            # 持久化绑定状态
            await self._persist_state()
            # 取消失败回退
            self._cancel_fail_revert()
        else:
            self._status["lifecycle_state"] = STATE_FAILED
            self._status["error_msg"] = error or "unknown error"
            self._status["auth_code"] = ""  

            logging.warning(f"MQTT绑定失败: {self._status['error_msg']}")

            # 在 fail 状态下计划自动回退到 unbound
            self._schedule_fail_revert()

        # 发送状态变更通知
        self._notify_status_changed()

        return {"result": "ok"}

    async def _handle_get_status(self, web_request: WebRequest) -> Dict[str, Any]:
        """
        获取当前MQTT桥接状态

        Args:
            web_request: Web请求对象

        Returns:
            包含当前状态的字典
        """
        return dict(self._status)

    def _notify_status_changed(self) -> None:
        """发送状态变更通知给所有订阅客户端"""
        self.server.send_event("mqtt_bridge:status_changed", self._status)

    def _check_bind_timeout(self, eventtime: float) -> float:
        """
        定时检查绑定是否超时
        
        Returns:
            float: 下次执行间隔时间（秒）
        """

        if self._status["lifecycle_state"] != STATE_BINDING:
            return eventtime + 1.0
        
        if self._status["request_ts"] == 0.0:
            return eventtime + 1.0
        
        elapsed = time.monotonic() - self._status["request_ts"]
        
        if elapsed > self.bind_timeout:
            logging.warning(f"绑定超时 ({elapsed:.1f}s > {self.bind_timeout}s)")
            
            self._status["lifecycle_state"] = STATE_FAILED
            self._status["error_msg"] = "Binding operation timed out."
            self._status["auth_code"] = ""  # 清空验证码
            
            # 发送状态变更通知给所有客户端
            self._notify_status_changed()
            # 计划自动回退
            self._schedule_fail_revert()
        
        return eventtime + 1.0  # 每秒检查一次

    def _schedule_fail_revert(self) -> None:
        """Schedule an automatic revert from fail -> unbound after delay."""
        # Cancel existing if any
        self._cancel_fail_revert()

        def _do_revert():
            if self._status.get("lifecycle_state") == STATE_FAILED:
                logging.info("自动回退：将 fail 状态恢复为 unbound")
                self._status["lifecycle_state"] = STATE_UNBOUND
                error_msg = self._status.get("error_msg", "")
                self._status["error_msg"] = ""
                self._status["auth_code"] = ""
                self._status["request_ts"] = time.monotonic()

                self.eventloop.create_task(self._persist_state())
                self._notify_status_changed()

        try:
            self._fail_revert_handle = self.eventloop.delay_callback(
                float(self.fail_revert_delay), _do_revert
            )
        except Exception:
            async def _bg():
                await self.eventloop.run_in_thread(lambda: time.sleep(self.fail_revert_delay))
                _do_revert()
            self.eventloop.create_task(_bg())

    def _cancel_fail_revert(self) -> None:
        """Cancel any scheduled automatic fail->unbound revert."""
        try:
            if self._fail_revert_handle is not None:
                try:
                    self._fail_revert_handle.cancel()
                except Exception:
                    pass
                self._fail_revert_handle = None
        except Exception:
            pass

    async def _persist_state(self) -> None:
        """持久化当前状态（仅持久化 bound 状态）"""
        if self._status["lifecycle_state"] == STATE_BOUND:
            # 只持久化必要字段
            persistent_data = {
                "lifecycle_state": self._status["lifecycle_state"],
                "user_info": self._status["user_info"],
                "auth_code": "",  # 不持久化验证码
                "error_msg": "",
                "request_ts": 0.0,
                "unbind_source": ""
            }

            await self.database.insert_item(
                "moonraker", "mqtt_bridge.state", persistent_data
            )
            logging.debug("已持久化绑定状态")
        else:
            # 非 bound 状态，清除持久化数据
            try:
                await self.database.delete_item("moonraker", "mqtt_bridge.state")
            except Exception as e:
                # 避免在后台任务中抛出未捕获异常，记录并继续
                logging.debug(f"Unable to delete persisted mqtt_bridge state: {e}")
            logging.debug("已清除持久化状态")


def load_component(config: ConfigHelper) -> MQTTBridge:
    return MQTTBridge(config)
