# Helper for Moonraker to Klippy API calls.
#
# Copyright (C) 2020 Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import logging
import os
import shutil
from ..utils import Sentinel
from ..common import WebRequest, APITransport, RequestType

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Any,
    Union,
    Optional,
    Dict,
    List,
    TypeVar,
    Mapping,
    Callable,
    Coroutine
)
if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import UserInfo
    from .klippy_connection import KlippyConnection as Klippy

    Subscription = Dict[str, Optional[List[Any]]]
    SubCallback = Callable[[Dict[str, Dict[str, Any]], float], Optional[Coroutine]]
    _T = TypeVar("_T")

INFO_ENDPOINT = "info"
ESTOP_ENDPOINT = "emergency_stop"
LIST_EPS_ENDPOINT = "list_endpoints"
GC_OUTPUT_ENDPOINT = "gcode/subscribe_output"
GCODE_ENDPOINT = "gcode/script"
SUBSCRIPTION_ENDPOINT = "objects/subscribe"
STATUS_ENDPOINT = "objects/query"
OBJ_LIST_ENDPOINT = "objects/list"
REG_METHOD_ENDPOINT = "register_remote_method"

class KlippyAPI(APITransport):
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.klippy: Klippy = self.server.lookup_component("klippy_connection")
        self.eventloop = self.server.get_event_loop()
        app_args = self.server.get_app_args()
        self.version = app_args.get('software_version')
        # Maintain a subscription for all moonraker requests, as
        # we do not want to overwrite them
        self.host_subscription: Subscription = {}
        self.subscription_callbacks: List[SubCallback] = []

        # Register GCode Aliases
        self.server.register_endpoint(
            "/printer/print/pause", RequestType.POST, self._gcode_pause
        )
        self.server.register_endpoint(
            "/printer/print/resume", RequestType.POST, self._gcode_resume
        )
        self.server.register_endpoint(
            "/printer/print/cancel", RequestType.POST, self._gcode_cancel
        )
        self.server.register_endpoint(
            "/printer/print/start", RequestType.POST, self._gcode_start_print
        )
        self.server.register_endpoint(
            "/printer/restart", RequestType.POST, self._gcode_restart
        )
        self.server.register_endpoint(
            "/printer/firmware_restart", RequestType.POST, self._gcode_firmware_restart
        )
        self.server.register_event_handler(
            "server:klippy_disconnect", self._on_klippy_disconnect
        )
        self.server.register_event_handler(
            "history:history_changed", self._handle_history_changed
        )

    def _on_klippy_disconnect(self) -> None:
        self.host_subscription.clear()
        self.subscription_callbacks.clear()

    async def _handle_history_changed(
        self, history_event: Dict[str, Any]
    ) -> None:
        if history_event.get("action") != "added":
            return
        job = history_event.get("job", {})
        if not isinstance(job, Mapping):
            return
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return
        await self._save_printing_info_job_id(job_id)

    async def _save_printing_info_job_id(self, job_id: str) -> None:
        try:
            await self.run_gcode(
                f"SAVE_VARIABLE VARIABLE=printing_info_job_id VALUE='\"{job_id}\"'"
            )
            logging.info(f"Updated printing_info_job_id from history: {job_id}")
        except Exception as e:
            logging.warning(f"Failed to save actual job_id '{job_id}': {e}")

    async def _gcode_pause(self, web_request: WebRequest) -> str:
        return await self.pause_print()

    async def _gcode_resume(self, web_request: WebRequest) -> str:
        return await self.resume_print()

    async def _gcode_cancel(self, web_request: WebRequest) -> str:
        return await self.cancel_print()

    async def _gcode_start_print(self, web_request: WebRequest) -> str:
        logging.info(" enter _gcode_start_print")
        filename: str = web_request.get_str('filename')
        user = web_request.get_current_user()
        auto_leveling: bool = web_request.get_boolean('auto_leveling', False)
        timelapse: bool = web_request.get_boolean('timelapse', False)
        skip_foreign_detect: bool = web_request.get_boolean('skip_foreign_detect', False)
        # 是否跳过整个预检查流程（包括 AI、传感器等），主要用于用户确认后“忽略并继续”
        skip_precheck: bool = web_request.get_boolean('skip_precheck', False)

        gcodes_dir = os.path.expanduser("~/printer_data/gcodes/")
        file_manager = self.server.lookup_component("file_manager")
        media_dir = getattr(file_manager, 'usb_mount_path', "/media/anisoprint/")

        # 处理文件路径：移除开头的 "gcodes/"，保留子文件夹路径
        if filename.startswith('gcodes/'):
            filename = filename[7:]
        elif filename.startswith('/'):
            filename = filename[1:]

        # 纯文件名（不带路径）
        filename_only = os.path.basename(filename)

        # 检查本地文件（支持子文件夹）
        local_path = os.path.join(gcodes_dir, filename)
        usb_path = os.path.join(media_dir, filename)

        # 如果本地不存在，尝试从 USB 复制
        if not os.path.exists(local_path):
            if os.path.exists(usb_path):
                # USB 文件复制到 gcodes 根目录
                dest_path = os.path.join(gcodes_dir, filename_only)
                logging.info(f"Copying from USB: {usb_path} -> {dest_path}")
                shutil.copy2(usb_path, dest_path)
                filename = filename_only  # USB 文件不带路径
            else:
                logging.warning(f"File not found: {local_path} or {usb_path}")

        return await self.start_print(
            filename,
            user=user,
            auto_leveling=auto_leveling,
            timelapse=timelapse,
            skip_foreign_detect=skip_foreign_detect,
            skip_precheck=skip_precheck,
        )

    async def _gcode_restart(self, web_request: WebRequest) -> str:
        return await self.do_restart("RESTART")

    async def _gcode_firmware_restart(self, web_request: WebRequest) -> str:
        return await self.do_restart("FIRMWARE_RESTART")

    async def _send_klippy_request(
        self,
        method: str,
        params: Dict[str, Any],
        default: Any = Sentinel.MISSING,
        transport: Optional[APITransport] = None
    ) -> Any:
        try:
            req = WebRequest(method, params, transport=transport or self)
            result = await self.klippy.request(req)
        except self.server.error:
            if default is Sentinel.MISSING:
                raise
            result = default
        return result

    async def run_gcode(self,
                        script: str,
                        default: Any = Sentinel.MISSING
                        ) -> str:
        params = {'script': script}
        result = await self._send_klippy_request(
            GCODE_ENDPOINT, params, default)
        return result

    async def start_print(
        self,
        filename: str,
        wait_klippy_started: bool = False,
        user: Optional[UserInfo] = None,
        auto_leveling: bool = False,
        timelapse: bool = False,
        skip_foreign_detect: bool = False,
        skip_precheck: bool = False,
    ) -> str:
        # WARNING: Do not call this method from within the following
        # event handlers when "wait_klippy_started" is set to True:
        # klippy_identified, klippy_started, klippy_ready, klippy_disconnect
        # Doing so will result in "wait_started" blocking for the specifed
        # timeout (default 20s) and returning False.
        # XXX - validate that file is on disk
        logging.info(" enter start_print")
        if filename[0] == '/':
            filename = filename[1:]

        await self._validate_print_start_state()

        # 保存带子文件夹路径的文件名，用于 SAVE_VARIABLE
        filename_with_path = filename
        filename_only = os.path.basename(filename)

        # 获取文件元数据
        file_manager = self.server.lookup_component("file_manager")
        metadata = await file_manager.ensure_file_metadata(filename)

        save_commands: List[str] = []

        # 初始化变量，确保即使没有元数据也有默认值
        print_mode: int = -1
        chamber_temp: float = 0.0
        if metadata:
            logging.info(f"Processing print parameters from metadata for file: {filename}")
            
            # 从元数据中提取打印参数
            print_mode = self._parse_print_mode(metadata.get("print_mode", -1))
            left_temp = metadata.get("first_layer_extr_temp", 0)
            right_temp = metadata.get("first_layer_extr_temp1", 0)
            bed_temp = metadata.get("first_layer_bed_temp", 0)
            chamber_temp = metadata.get("chamber_temp", 0.0) or 0.0
            
            bounding_box = metadata.get("bounding_box_size", {})
            x_min = bounding_box.get("x_min", 0.0)
            x_max = bounding_box.get("x_max", 0.0)
            y_min = bounding_box.get("y_min", 0.0)
            y_max = bounding_box.get("y_max", 0.0)
            
            # 构造 SAVE_VARIABLE 命令列表
            save_commands.extend([
                f"SAVE_VARIABLE VARIABLE=printing_info_filename VALUE='\"{filename_with_path}\"'",
                f"SAVE_VARIABLE VARIABLE=printing_info_print_mode VALUE={print_mode}",
                f"SAVE_VARIABLE VARIABLE=printing_info_left_preheat_temperature VALUE={left_temp}",
                f"SAVE_VARIABLE VARIABLE=printing_info_right_preheat_temperature VALUE={right_temp}",
                f"SAVE_VARIABLE VARIABLE=printing_info_bed_preheat_temperature VALUE={bed_temp}",
                f"SAVE_VARIABLE VARIABLE=printing_info_chamber_temperature VALUE={chamber_temp}",
                f"SAVE_VARIABLE VARIABLE=printing_info_auto_leveling VALUE={1 if auto_leveling else 0}",
                f"SAVE_VARIABLE VARIABLE=printing_info_mode_x_max VALUE={x_max}",
                f"SAVE_VARIABLE VARIABLE=printing_info_mode_x_min VALUE={x_min}",
                f"SAVE_VARIABLE VARIABLE=printing_info_mode_y_max VALUE={y_max}",
                f"SAVE_VARIABLE VARIABLE=printing_info_mode_y_min VALUE={y_min}",
                f"_SET_TIMELAPSE_SETUP ENABLE={str(timelapse).lower()}"
            ])

        else:
            logging.warning(f"No metadata found for file: {filename}, skipping parameter setup")
            # 即使没有元数据，也保存带路径的文件名
            save_commands.append(
                f"SAVE_VARIABLE VARIABLE=printing_info_filename "
                f"VALUE='\"{filename_with_path}\"'"
            )
        # 统一执行参数设置命令
        for cmd in save_commands:
            try:
                await self.run_gcode(cmd)
                logging.info(f"Executed: {cmd}")
            except Exception as e:
                logging.warning(f"Failed to execute command '{cmd}': {e}")

        # 打印前预检查（AI / 传感器等）
        if not skip_precheck:
            await self._run_precheck(
                skip_foreign_detect=skip_foreign_detect,
                print_mode=print_mode,
                chamber_temp=chamber_temp,
            )

        # Escape existing double quotes in the file name
        filename_only = filename_only.replace("\"", "\\\"")
        script = f'SDCARD_PRINT_FILE FILENAME="{filename_only}"'
        if wait_klippy_started:
            await self.klippy.wait_started()
        logging.info(f"Requesting Job Start, filename_with_path = {filename_with_path}, filename_only = {filename_only}")
        ret = await self.run_gcode(script)

        self.server.send_event("klippy_apis:job_start_complete", user)
        logging.info(" leave start_print")
        return ret

    async def _validate_print_start_state(self) -> None:
        """
        开始打印前校验打印机状态，仅允许 standby 状态发起打印。
        """
        try:
            status = await self.query_objects({"print_stats": ["state"]})
        except Exception as e:
            logging.warning(f"Unable to query print_stats.state before start: {e}")
            raise self.server.error(
                "Unable to verify printer state before start_print", 503
            )

        print_stats = status.get("print_stats", {}) if isinstance(status, dict) else {}
        state = str(print_stats.get("state", "")).strip().lower()
        if state == "standby":
            return
        if state == "complete" or state == "cancelled":
            # 上一次打印已完成，将 print_stats.state 重置为 standby 后继续执行，不记录错误
            logging.info("Printer state is 'complete' or 'cancelled', resetting printer to standby before start")
            try:
                await self.run_gcode("SET_PRINTER_STANDBY")
            except Exception as e:
                logging.warning(f"Failed to reset print_stats.state to standby: {e}")
            return
        msg = f"Printer state '{state or 'unknown'}' does not allow start printing"
        logging.warning(msg)
        raise self.server.error(msg, 409)

    async def _run_precheck(
        self,
        skip_foreign_detect: bool = False,
        print_mode: int = -1,
        chamber_temp: float = 0.0,
    ) -> None:
        """
        打印前预检查流程入口。
        当前实现：
          - AI 平台异物检测（可通过 skip_foreign_detect 跳过）
          - 打印底板安放检测（AI）
          - 耗材存在检测（传感器，基于 print_mode 检查对应耗材传感器）
        预留扩展：
          - 加热腔门/顶盖关闭检测

        前端接口（供上位机如 anisotouch-sk3 后续接入）：
        - WebSocket 订阅: 方法名 notify_precheck_status，payload 形如:
          {"step": "overall"|"bed_foreign_object"|"build_plate"|"filament"|"enclosure",
           "status": "started"|"running"|"ok"|"failed"|"skipped"|"unavailable",
           "message": "<可选>", "data": "<可选>"}
        - Klipper 状态: objects/query 或 notify_status_update 中 print_stats.state
          可为 "precheck"，表示处于预检查阶段；预检查通过并下发 SDCARD_PRINT_FILE 后
          会变为 "printing"。
        - 忽略并继续: POST /printer/print/start 传 skip_foreign_detect=true 或
          skip_precheck=true 可跳过对应检查后继续打印。
        """
        try:
            await self.run_gcode("SET_PRINT_PRECHECK_STATE")
        except Exception as e:
            # 该指令失败不应阻断打印，仅记录日志
            logging.warning(f"Precheck: failed to set print state to precheck: {e}")

        try:
            # 1. 平台异物检测 + 打印底板检测（AI）
            if skip_foreign_detect:
                logging.info("Precheck: skipping foreign object detection due to skip_foreign_detect flag")
            else:
                try:
                    # 检查组件是否加载失败
                    if "ai_detection" in self.server.failed_components:
                        msg = ("AI Detection component failed to load")
                        self.server.send_event(
                            "precheck:failed",
                            {"message": msg, "step": "ai_detection", "http_code": 503},
                        )
                        raise self.server.error(msg, 503)
                    ai_detection = self.server.lookup_component("ai_detection")
                    if ai_detection.is_foreign_detect_enabled():
                        # 调用 AI 异物检测，由 ai_detection 模块统一解析:
                        result = await ai_detection.detect_foreign_object(send_notification=True)

                        # 1.1 平台异物检查结果
                        if result == "error":
                            msg = ("AI Detection: Foreign object detected, skipping print")
                            logging.warning(msg)
                            raise self.server.error(msg, 403)
                        elif result == "ok":
                            logging.info("AI Detection: No foreign objects detected, proceeding with print")
                    else:
                        logging.info("AI Detection: Foreign object detection disabled, skipping...")
                except self.server.error:
                    raise
                except Exception as e:
                    # 异常记录警告但继续打印（容错处理）
                    logging.warning(f"AI Detection: Foreign object check failed: {e}, proceeding with print")
            # 2. 耗材存在检测（传感器）
            if print_mode < 0:
                # 无有效 print_mode（例如元数据缺失），直接跳过耗材检查
                logging.info("Filament precheck: invalid or missing print_mode, skipping filament presence check")
            else:
                try:
                    # 根据 print_mode 选择需要检查的传感器
                    filament_sensors: List[str] = []
                    sensor_missing_texts: Dict[str, str] = {}
                    if print_mode == 0:  # Plastic Only
                        filament_sensors = [
                            "filament_switch_sensor filament_sensor_3",
                        ]
                        sensor_missing_texts = {
                            "filament_switch_sensor filament_sensor_3": "Right plastic missing",
                        }
                    elif print_mode == 1: # Plastic and Composite
                        filament_sensors = [
                            "filament_switch_sensor filament_sensor",
                            "filament_switch_sensor filament_sensor_1",
                            "filament_switch_sensor filament_sensor_3",
                        ]
                        sensor_missing_texts = {
                            "filament_switch_sensor filament_sensor": "Left fiber missing",
                            "filament_switch_sensor filament_sensor_1": "Left plastic missing",
                            "filament_switch_sensor filament_sensor_3": "Right plastic missing",
                        }
                    elif print_mode == 2: # Composite Only
                        filament_sensors = [
                            "filament_switch_sensor filament_sensor",
                            "filament_switch_sensor filament_sensor_1",
                        ]
                        sensor_missing_texts = {
                            "filament_switch_sensor filament_sensor": "Left fiber missing",
                            "filament_switch_sensor filament_sensor_1": "Left plastic missing",
                        }

                    if not filament_sensors:
                        logging.info(f"Filament precheck: no sensors configured for print_mode={print_mode}, skipping filament presence check")
                    else:
                        # 查询传感器状态
                        query_objects: Dict[str, Optional[List[str]]] = {
                            name: None for name in filament_sensors
                        }
                        try:
                            status = await self.query_objects(query_objects)
                        except Exception as e:
                            logging.warning(f"Filament precheck: failed to query filament sensors: {e}, proceeding with print anyway")
                        else:
                            missing: List[str] = []
                            for name in filament_sensors:
                                obj = status.get(name, {})
                                filament_present = bool(obj.get("filament_detected", True))
                                enabled = bool(obj.get("enabled", True))
                                # 仅在传感器启用且检测到无料时视为缺料
                                if enabled and not filament_present:
                                    missing.append(name)

                            if missing:
                                # 按传感器配置顺序拼接缺料提示，确保提示文本稳定可预期
                                missing_msgs = [
                                    sensor_missing_texts.get(name, f"Filament missing at {name}")
                                    for name in filament_sensors
                                    if name in missing
                                ]
                                def _lower_initial(text: str) -> str:
                                    if not text:
                                        return text
                                    return text[0].lower() + text[1:]
                                if len(missing_msgs) == 1:
                                    msg = missing_msgs[0]
                                elif len(missing_msgs) == 2:
                                    msg = f"{missing_msgs[0]} and {_lower_initial(missing_msgs[1])}"
                                else:
                                    middle_msgs = [_lower_initial(item) for item in missing_msgs[1:-1]]
                                    last_msg = _lower_initial(missing_msgs[-1])
                                    msg = f"{missing_msgs[0]}, {', '.join(middle_msgs)}, and {last_msg}"
                                logging.warning("%s. Missing sensors: %s", msg, ", ".join(missing))
                                self.server.send_event(
                                    "precheck:failed",
                                    {
                                        "message": msg,
                                        "step": "filament",
                                        "http_code": 403,
                                        "data": {
                                            "print_mode": print_mode,
                                            "missing_sensors": missing,
                                        },
                                    },
                                )
                                raise self.server.error("filament", 403)
                            else:
                                logging.info("Filament precheck: all required sensors report filament present")
                except self.server.error:
                    raise
                except Exception as e:
                    logging.warning(f"Filament precheck: failed: {e}, proceeding with print anyway")

            # 4. 加热腔门/顶盖检测
            try:
                if not chamber_temp or chamber_temp <= 0.0:
                    logging.info("Enclosure precheck: no chamber temperature configured, skipping door/top cover check")
                else:
                    # 查询门传感器状态
                    try:
                        status = await self.query_objects(
                            {"filament_switch_sensor door_hall_sensor": None}
                        )
                    except Exception as e:
                        logging.warning(f"Enclosure precheck: failed to query door_hall_sensor: {e}, proceeding with print anyway")
                    else:
                        obj = status.get("filament_switch_sensor door_hall_sensor", {})
                        door_closed = bool(obj.get("filament_detected", True))
                        enabled = bool(obj.get("enabled", True))

                        if enabled and not door_closed:
                            msg = "Door is not closed with the chamber temperature"
                            logging.warning(msg)
                            self.server.send_event(
                                "precheck:failed",
                                {
                                    "message": msg,
                                    "step": "enclosure_door",
                                    "http_code": 403,
                                    "data": {
                                        "sensor": "filament_switch_sensor door_hall_sensor",
                                        "chamber_temp": chamber_temp,
                                    },
                                },
                            )
                            raise self.server.error("enclosure_door", 403)
                        else:
                            logging.info("Enclosure precheck: door and top cover are closed, proceeding with print")
            except self.server.error:
                logging.warning("Precheck failed on enclosure check")
                raise
            except Exception as e:
                logging.warning(f"Enclosure precheck: check failed: {e}, proceeding with print")
        except self.server.error:
            # 预检查失败时，将 print_stats.state 恢复为 standby，避免长时间停留在 precheck/error 状态
            try:
                await self.run_gcode("SET_PRINT_STATS_INFO STATE=standby")
            except Exception as e:
                logging.warning(f"Precheck: failed to reset print_stats.state to standby: {e}")
            raise
    def _parse_print_mode(self, mode_str: str) -> int:
        """
        解析打印模式字符串为数值
        - "Plastic Only" → 0 (高速模式)
        - "Plastic and Composite" → 1 (高强度模式)
        - "Composite Only" → 2 (超强模式)
        """
        mode_map = {
            "Plastic Only": 0,
            "Plastic and Composite": 1,
            "Composite Only": 2,
        }
        return mode_map.get(mode_str, 0)

    async def pause_print(
        self, default: Union[Sentinel, _T] = Sentinel.MISSING
    ) -> Union[_T, str]:
        self.server.send_event("klippy_apis:pause_requested")
        logging.info("Requesting job pause...")
        return await self._send_klippy_request(
            "pause_resume/pause", {}, default)

    async def resume_print(
        self, default: Union[Sentinel, _T] = Sentinel.MISSING
    ) -> Union[_T, str]:
        self.server.send_event("klippy_apis:resume_requested")
        logging.info("Requesting job resume...")
        return await self._send_klippy_request(
            "pause_resume/resume", {}, default)

    async def cancel_print(
        self, default: Union[Sentinel, _T] = Sentinel.MISSING
    ) -> Union[_T, str]:
        self.server.send_event("klippy_apis:cancel_requested")
        logging.info("Requesting job cancel...")
        return await self._send_klippy_request(
            "pause_resume/cancel", {}, default)

    async def do_restart(
        self, gc: str, wait_klippy_started: bool = False
    ) -> str:
        # WARNING: Do not call this method from within the following
        # event handlers when "wait_klippy_started" is set to True:
        # klippy_identified, klippy_started, klippy_ready, klippy_disconnect
        # Doing so will result in "wait_started" blocking for the specifed
        # timeout (default 20s) and returning False.
        if wait_klippy_started:
            await self.klippy.wait_started()
        try:
            result = await self.run_gcode(gc)
        except self.server.error as e:
            if str(e) == "Klippy Disconnected":
                result = "ok"
            else:
                raise
        return result

    async def list_endpoints(self,
                             default: Union[Sentinel, _T] = Sentinel.MISSING
                             ) -> Union[_T, Dict[str, List[str]]]:
        return await self._send_klippy_request(
            LIST_EPS_ENDPOINT, {}, default)

    async def emergency_stop(self) -> str:
        return await self._send_klippy_request(ESTOP_ENDPOINT, {})

    async def get_klippy_info(self,
                              send_id: bool = False,
                              default: Union[Sentinel, _T] = Sentinel.MISSING
                              ) -> Union[_T, Dict[str, Any]]:
        params = {}
        if send_id:
            ver = self.version
            params = {'client_info': {'program': "Moonraker", 'version': ver}}
        return await self._send_klippy_request(INFO_ENDPOINT, params, default)

    async def get_object_list(self,
                              default: Union[Sentinel, _T] = Sentinel.MISSING
                              ) -> Union[_T, List[str]]:
        result = await self._send_klippy_request(
            OBJ_LIST_ENDPOINT, {}, default)
        if isinstance(result, dict) and 'objects' in result:
            return result['objects']
        if default is not Sentinel.MISSING:
            return default
        raise self.server.error("Invalid response received from Klippy", 500)

    async def query_objects(self,
                            objects: Mapping[str, Optional[List[str]]],
                            default: Union[Sentinel, _T] = Sentinel.MISSING
                            ) -> Union[_T, Dict[str, Any]]:
        params = {'objects': objects}
        result = await self._send_klippy_request(
            STATUS_ENDPOINT, params, default)
        if isinstance(result, dict) and "status" in result:
            return result["status"]
        if default is not Sentinel.MISSING:
            return default
        raise self.server.error("Invalid response received from Klippy", 500)

    async def subscribe_objects(
        self,
        objects: Mapping[str, Optional[List[str]]],
        callback: Optional[SubCallback] = None,
        default: Union[Sentinel, _T] = Sentinel.MISSING
    ) -> Union[_T, Dict[str, Any]]:
        # The host transport shares subscriptions amongst all components
        for obj, items in objects.items():
            if obj in self.host_subscription:
                prev = self.host_subscription[obj]
                if items is None or prev is None:
                    self.host_subscription[obj] = None
                else:
                    uitems = list(set(prev) | set(items))
                    self.host_subscription[obj] = uitems
            else:
                self.host_subscription[obj] = items
        params = {"objects": dict(self.host_subscription)}
        result = await self._send_klippy_request(SUBSCRIPTION_ENDPOINT, params, default)
        if isinstance(result, dict) and "status" in result:
            if callback is not None:
                self.subscription_callbacks.append(callback)
            return result["status"]
        if default is not Sentinel.MISSING:
            return default
        raise self.server.error("Invalid response received from Klippy", 500)

    async def subscribe_from_transport(
        self,
        objects: Mapping[str, Optional[List[str]]],
        transport: APITransport,
        default: Union[Sentinel, _T] = Sentinel.MISSING,
        full_response: bool = False
    ) -> Union[_T, Dict[str, Any]]:
        params = {"objects": dict(objects)}
        result = await self._send_klippy_request(
            SUBSCRIPTION_ENDPOINT, params, default, transport
        )
        if isinstance(result, dict) and "status" in result:
            if full_response:
                return result
            return result["status"]
        if default is not Sentinel.MISSING:
            return default
        raise self.server.error("Invalid response received from Klippy", 500)

    async def subscribe_gcode_output(self) -> str:
        template = {'response_template':
                    {'method': "process_gcode_response"}}
        return await self._send_klippy_request(GC_OUTPUT_ENDPOINT, template)

    async def register_method(self, method_name: str) -> str:
        return await self._send_klippy_request(
            REG_METHOD_ENDPOINT,
            {'response_template': {"method": method_name},
             'remote_method': method_name})

    def send_status(
        self, status: Dict[str, Any], eventtime: float
    ) -> None:
        for cb in self.subscription_callbacks:
            self.eventloop.register_callback(cb, status, eventtime)
        self.server.send_event("server:status_update", status)

def load_component(config: ConfigHelper) -> KlippyAPI:
    return KlippyAPI(config)
