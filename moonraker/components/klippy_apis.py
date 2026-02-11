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

    def _on_klippy_disconnect(self) -> None:
        self.host_subscription.clear()
        self.subscription_callbacks.clear()

    async def _gcode_pause(self, web_request: WebRequest) -> str:
        return await self.pause_print()

    async def _gcode_resume(self, web_request: WebRequest) -> str:
        return await self.resume_print()

    async def _gcode_cancel(self, web_request: WebRequest) -> str:
        return await self.cancel_print()

    async def _gcode_start_print(self, web_request: WebRequest) -> str:
        filename: str = web_request.get_str('filename')
        user = web_request.get_current_user()
        auto_leveling: bool = web_request.get_boolean('auto_leveling', False)
        timelapse: bool = web_request.get_boolean('timelapse', False)
        skip_foreign_detect: bool = web_request.get_boolean('skip_foreign_detect', False)

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
            skip_foreign_detect=skip_foreign_detect
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
        skip_foreign_detect: bool = False
    ) -> str:
        # WARNING: Do not call this method from within the following
        # event handlers when "wait_klippy_started" is set to True:
        # klippy_identified, klippy_started, klippy_ready, klippy_disconnect
        # Doing so will result in "wait_started" blocking for the specifed
        # timeout (default 20s) and returning False.
        # XXX - validate that file is on disk
        if filename[0] == '/':
            filename = filename[1:]

        # 保存带子文件夹路径的文件名，用于 SAVE_VARIABLE
        filename_with_path = filename
        filename_only = os.path.basename(filename)

        # 获取文件元数据
        file_manager = self.server.lookup_component("file_manager")
        metadata = file_manager.get_file_metadata(filename)

        # 预测下一个 job_id
        predicted_job_id = "UNKNOWN"
        try:
            history = self.server.lookup_component("history")
            # 获取历史记录中最大的 job_id
            max_job_id = await self._get_max_job_id(history)
            if max_job_id is not None:
                # 预测下一个 job_id (最大值 + 1)
                next_job_id = max_job_id + 1
                predicted_job_id = f"{next_job_id:06X}"
                logging.info(f"Predicted job_id for this print: {predicted_job_id} (decimal: {next_job_id})")
            else:
                logging.warning("Unable to predict job_id, no history records found")
        except Exception as e:
            logging.warning(f"Failed to predict job_id: {e}")
        
        
        if metadata:
            logging.info(f"Processing print parameters from metadata for file: {filename}")
            
            # 从元数据中提取打印参数
            print_mode = self._parse_print_mode(metadata.get("print_mode", ""))
            left_temp = metadata.get("first_layer_extr_temp", 0)
            right_temp = metadata.get("first_layer_extr_temp1", 0)
            bed_temp = metadata.get("first_layer_bed_temp", 0)
            
            bounding_box = metadata.get("bounding_box_size", {})
            x_min = bounding_box.get("x_min", 0.0)
            x_max = bounding_box.get("x_max", 0.0)
            y_min = bounding_box.get("y_min", 0.0)
            y_max = bounding_box.get("y_max", 0.0)
            
            # 构造 SAVE_VARIABLE 命令列表
            save_commands = [
                f"SAVE_VARIABLE VARIABLE=printing_info_job_id VALUE='\"{predicted_job_id}\"'",
                f"SAVE_VARIABLE VARIABLE=printing_info_filename VALUE='\"{filename_with_path}\"'",
                f"SAVE_VARIABLE VARIABLE=printing_info_print_mode VALUE={print_mode}",
                f"SAVE_VARIABLE VARIABLE=printing_info_left_preheat_temperature VALUE={left_temp}",
                f"SAVE_VARIABLE VARIABLE=printing_info_right_preheat_temperature VALUE={right_temp}",
                f"SAVE_VARIABLE VARIABLE=printing_info_bed_preheat_temperature VALUE={bed_temp}",
                f"SAVE_VARIABLE VARIABLE=printing_info_auto_leveling VALUE={1 if auto_leveling else 0}",
                f"SAVE_VARIABLE VARIABLE=printing_info_mode_x_max VALUE={x_max}",
                f"SAVE_VARIABLE VARIABLE=printing_info_mode_x_min VALUE={x_min}",
                f"SAVE_VARIABLE VARIABLE=printing_info_mode_y_max VALUE={y_max}",
                f"SAVE_VARIABLE VARIABLE=printing_info_mode_y_min VALUE={y_min}",
                f"_SET_TIMELAPSE_SETUP ENABLE={str(timelapse).lower()}"
            ]

            # 依次执行所有参数设置命令
            for cmd in save_commands:
                try:
                    await self.run_gcode(cmd)
                    logging.debug(f"Executed: {cmd}")
                except Exception as e:
                    logging.warning(f"Failed to execute command '{cmd}': {e}")
        else:
            logging.warning(f"No metadata found for file: {filename}, skipping parameter setup")
            # 即使没有元数据，也保存带路径的文件名
            try:
                await self.run_gcode(f"SAVE_VARIABLE VARIABLE=printing_info_filename VALUE='\"{filename_with_path}\"'")
            except Exception as e:
                logging.warning(f"Failed to save filename with path: {e}")

        # 打印前异物检测
        if not skip_foreign_detect:
            try:
                # 检查组件是否加载失败
                if "ai_detection" in self.server.failed_components:
                    raise self.server.error(
                        "AI Detection component failed to load, foreign object detection unavailable",
                        503
                    )
                ai_detection = self.server.lookup_component("ai_detection")
                # 检查总开关，未启用则跳过检测
                if not ai_detection.is_enabled():
                    logging.info("AI Detection is disabled, skipping foreign object detection")
                else:
                    # 检查连接状态
                    if not ai_detection.is_connected:
                        raise self.server.error(
                            "AI Detection service is not connected",
                            503
                        )
                    # 检查总开关和异物检测开关
                    if ai_detection.is_foreign_detect_enabled():
                        result = await ai_detection.detect_foreign_object(
                            send_notification=True  # 发送通知
                        )

                        if result.is_valid and result.has_object:
                            # 检测有效且有异物，抛出错误阻止打印启动
                            class_names = result.class_names if result.class_names else []
                            objects_str = ", ".join(class_names) if class_names else "unknown objects"
                            logging.warning(
                                f"AI Detection: Foreign object detected: {objects_str}, blocking print start"
                            )
                            raise self.server.error(
                                "Foreign object detected, please confirm before printing",
                                403
                            )
                        elif result.is_valid and not result.has_object:
                            # 检测有效且无异物，继续打印
                            logging.info("AI Detection: No foreign objects detected, proceeding with print")
                        else:
                            # 检测无效（可能未连接），记录警告但继续打印
                            logging.warning(
                                "AI Detection: Foreign object detection not available, "
                                "proceeding with print anyway"
                            )
                    else:
                        logging.info("AI Detection: Foreign object detection disabled, skipping...")
            except self.server.error:
                raise  # 重新抛出 server.error
            except Exception as e:
                # 异常记录警告但继续打印（容错处理）
                logging.warning(f"AI Detection: Foreign object check failed: {e}, proceeding with print")

        # Escape existing double quotes in the file name
        filename_only = filename_only.replace("\"", "\\\"")
        script = f'SDCARD_PRINT_FILE FILENAME="{filename_only}"'
        if wait_klippy_started:
            await self.klippy.wait_started()
        logging.info(f"Requesting Job Start, filename_with_path = {filename_with_path}, filename_only = {filename_only}")
        ret = await self.run_gcode(script)

        self.server.send_event("klippy_apis:job_start_complete", user)
        return ret


    async def _get_max_job_id(self, history) -> Optional[int]:
        """
        从历史记录表中获取最大的 job_id
        返回: 最大的 job_id (整数)，如果没有记录则返回 None
        """
        try:
            cursor = await history.history_table.execute(
                "SELECT MAX(job_id) FROM job_history"
            )
            result = await cursor.fetchone()
            if result and result[0] is not None:
                return result[0]
            return None
        except Exception as e:
            logging.error(f"Error getting max job_id: {e}")
            return None
     
    
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
