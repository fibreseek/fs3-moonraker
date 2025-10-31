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
        # 获取用户可选参数
        auto_leveling: bool = web_request.get_boolean('auto_leveling', False)
        timelapse: bool = web_request.get_boolean('timelapse', False)
        
        gcodes_dir = os.path.expanduser("~/printer_data/gcodes/")
         # 从 file_manager 组件获取 usb_mount_path
        file_manager = self.server.lookup_component("file_manager")
        media_dir = getattr(file_manager, 'usb_mount_path', "/media/anisoprint/")
        filename_without_path = os.path.basename(filename)
        gcode_path = os.path.join(gcodes_dir, filename_without_path)
        media_path = os.path.join(media_dir, filename)

        if not os.path.exists(gcode_path):
            if os.path.exists(media_path):
                logging.info(f"Found G-code file at media path: {media_path}, copying to {gcode_path}")
                shutil.copy2(media_path, gcode_path)
            else:
                logging.info(f"G-code file not found at media path: {media_path}")
        filename = filename_without_path
        return await self.start_print(
            filename, 
            user=user, 
            auto_leveling=auto_leveling,
            timelapse=timelapse
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
        timelapse: bool = False
    ) -> str:
        # WARNING: Do not call this method from within the following
        # event handlers when "wait_klippy_started" is set to True:
        # klippy_identified, klippy_started, klippy_ready, klippy_disconnect
        # Doing so will result in "wait_started" blocking for the specifed
        # timeout (default 20s) and returning False.
        # XXX - validate that file is on disk
        if filename[0] == '/':
            filename = filename[1:]
        
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
                f"SAVE_VARIABLE VARIABLE=printing_info_print_mode VALUE={print_mode}",
                f"SAVE_VARIABLE VARIABLE=printing_info_left_preheat_temp VALUE={left_temp}",
                f"SAVE_VARIABLE VARIABLE=printing_info_right_preheat_temp VALUE={right_temp}",
                f"SAVE_VARIABLE VARIABLE=printing_info_bed_preheat_temp VALUE={bed_temp}",
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
        
        # Escape existing double quotes in the file name
        filename = filename.replace("\"", "\\\"")
        script = f'SDCARD_PRINT_FILE FILENAME="{filename}"'
        if wait_klippy_started:
            await self.klippy.wait_started()
        logging.info(f"Requesting Job Start, filename = {filename}")
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
