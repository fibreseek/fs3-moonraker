# Printer Setting Component
#
# Copyright (C) 2026 Your Name <your.email@example.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import logging
import asyncio
from ..confighelper import ConfigHelper
from ..common import RequestType, WebRequest

# Annotation imports
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Set,
    Optional,
    List
)

if TYPE_CHECKING:
    from ..common import WebRequest, APITransport
    from .application import MoonrakerApp
    from .database import MoonrakerDatabase
    from .machine import Machine

class PrinterSetting:
    def __init__(self, config: ConfigHelper) -> None:
        self.config = config
        self.server = config.get_server()
        self.database: MoonrakerDatabase = self.server.lookup_component("database")
        self.machine: Machine = self.server.lookup_component("machine")
        
        self._setting_cache: Dict[str, Dict[str, Any]] = {}

        # Subscription management
        self._subscriber_namespaces: Dict[Any, Set[str]] = {}
        
        # Batch notification management for internal changes
        self._notification_queue: Dict[str, Dict[str, Any]] = {}
        self._notification_timer = None
        self._notification_lock = asyncio.Lock()
        self._notification_interval = 0.2  # 200 milliseconds
        
        # Register setting management endpoints
        self.server.register_endpoint(
            "/printer/get_setting", RequestType.GET, self._handle_get_setting
        )
        self.server.register_endpoint(
            "/printer/set_setting", RequestType.POST, self._handle_set_setting
        )
        self.server.register_endpoint(
            "/printer/subscribe_setting", RequestType.POST, self._handle_subscribe_setting
        )
        self.server.register_endpoint(
            "/printer/unsubscribe_setting", RequestType.POST, self._handle_unsubscribe_setting
        )
        
        # Register logs management endpoints
        self.server.register_endpoint(
            "/printer/logs/get_logs_url", RequestType.GET, self._handle_get_logs_url
        )
        # Log download rate limiting
        self._last_log_download_time = 0
        self._log_download_cooldown = 10  # 10 seconds cooldown

        # Register notification
        self.server.register_notification("printer:setting_changed")
        
        # Register event handler for client disconnect
        self.server.register_event_handler(
            "websockets:client_removed",
            lambda sc: self._remove_subscriber(sc)
        )
        
        # Register event handler for machine info changes (internal changes)
        self.server.register_event_handler(
            "machine:machine_name_changed",
            lambda value: self._queue_setting_change("fibretouch", {"machine_name": value})
        )
        
        self.server.register_event_handler(
            "machine:machine_id_changed",
            lambda value: self._queue_setting_change("fibretouch", {"machine_id": value})
        )

    async def _init_setting_cache(self) -> None:
        """Initialize setting cache with default values"""
        self._setting_cache["fibretouch"] = {
            "machine_name": self.machine.get_machine_name(),
            "machine_id": self.machine.get_machine_id(),
            "firmware_version": self.machine.get_firmware_version(),
            "serial_num": self.machine.get_serial_num(),
            "machine_type": self.machine.get_machine_type(),
            "fibretouch_system_verison": self.machine.get_fibretouch_system_verison(),
        }
        fibretouch_items = await self.database.ns_items("fibretouch")
        self._setting_cache["fibretouch"].update(fibretouch_items)


    async def _handle_get_setting(self, web_request: WebRequest) -> Dict[str, Any]:
        """Handle printer.get_setting request"""
        params = web_request.get_args()
        result = {}
        
        for namespace, keys in params.items():
            if not isinstance(keys, list):
                continue
            if namespace not in self._setting_cache:
                continue

            namespace_params = {}
            namespace_params_tmp = self._setting_cache.get(namespace, {})
            if not keys:
                # Get all parameters for the namespace
                namespace_params = namespace_params_tmp
            else:
                # Get specific parameters
                for key in keys:
                    namespace_params[key] = namespace_params_tmp.get(key, None)
        
            result[namespace] = namespace_params
        
        return result
    
    async def _handle_set_setting(self, web_request: WebRequest) -> Dict[str, Any]:
        """Handle printer.set_setting request"""
        params = web_request.get_args()
        result = {}
        
        for namespace, param_dict in params.items():
            if not isinstance(param_dict, dict):
                continue
            
            if namespace not in self._setting_cache:
                continue

            updated_params = {}
            if namespace == "fibretouch":
                for key, value in param_dict.items():
                    if key == "machine_name":
                        await self.machine.set_machine_name(value)
                        # machine_name change will be handled by the event handler
                        updated_params[key] = self.machine.get_machine_name()
                    elif key in (
                        "machine_id",
                        "firmware_version",
                        "serial_num",
                        "machine_type",
                        "fibretouch_system_verison",
                    ):
                        # machine_id/firmware_version/serial_num/machine_type/fibretouch_system_verison are not settable
                        continue
                    elif key == "deviceActivationTime":
                        # 设备激活时间只允许首次写入非空值，忽略清空或改写
                        existing = self._setting_cache[namespace].get(key, None)
                        existing_str = existing.strip() if isinstance(existing, str) else (
                            str(existing).strip() if existing is not None else ""
                        )
                        incoming_str = value.strip() if isinstance(value, str) else (
                            str(value).strip() if value is not None else ""
                        )
                        if existing_str:
                            logging.info(
                                "Ignore overwrite of fibretouch.deviceActivationTime"
                            )
                            continue
                        if not incoming_str:
                            logging.info(
                                "Ignore empty fibretouch.deviceActivationTime"
                            )
                            continue
                        await self.database.insert_item("fibretouch", key, incoming_str)
                        new_value = await self.database.get_item("fibretouch", key)
                        updated_params[key] = new_value
                        if new_value != self._setting_cache[namespace].get(key, None):
                            self._queue_setting_change(namespace, {key: new_value})
                    else:
                        # 其他参数直接设置到数据库中
                        if value != self._setting_cache[namespace].get(key, None):
                            await self.database.insert_item("fibretouch", key, value)
                            new_value = await self.database.get_item("fibretouch", key)
                            updated_params[key] = new_value
                            if new_value != self._setting_cache[namespace].get(key, None):
                                self._queue_setting_change(namespace, {key: new_value})
                        else:
                            updated_params[key] = value
            else:
                continue

            if updated_params:
                result[namespace] = updated_params

        return result
    
    async def _handle_subscribe_setting(self, web_request: WebRequest) -> Dict[str, Any]:
        """Handle printer.subscribe_setting request"""
        params = web_request.get_args()
        conn = web_request.get_subscribable()
        
        if conn is None:
            raise self.server.error("Subscription requires a persistent connection", 400)
        
        result = {}
        
        for namespace, _ in params.items():
            if namespace not in self._setting_cache:
                continue

            # Add subscriber
            if conn not in self._subscriber_namespaces:
                self._subscriber_namespaces[conn] = set()
            self._subscriber_namespaces[conn].add(namespace)
            
            # Get current values for the namespace
            namespace_params = self._setting_cache.get(namespace, {})
            result[namespace] = namespace_params
        
        return result
    
    async def _handle_unsubscribe_setting(self, web_request: WebRequest) -> Dict[str, Any]:
        """Handle printer.unsubscribe_setting request"""
        params = web_request.get_args()
        conn = web_request.get_subscribable()
        
        if conn is None:
            raise self.server.error("Unsubscription requires a persistent connection", 400)
        
        result = {}
        
        for namespace, _ in params.items():
            if namespace not in self._setting_cache:
                continue

            # Update subscriber's namespace list
            if conn in self._subscriber_namespaces:
                self._subscriber_namespaces[conn].discard(namespace)
                if not self._subscriber_namespaces[conn]:
                    del self._subscriber_namespaces[conn]
            
            result[namespace] = []
        
        return result
    
    def _queue_setting_change(self, namespace: str, changes: Dict[str, Any]) -> None:
        """Queue parameter changes for batch notification (for internal changes)"""
        asyncio.create_task(self._add_to_queue(namespace, changes))
    
    async def _add_to_queue(self, namespace: str, changes: Dict[str, Any]) -> None:
        """Add setting changes to the notification queue"""
        async with self._notification_lock:
            if namespace not in self._notification_queue:
                self._notification_queue[namespace] = {}
            # Update or add changes
            self._notification_queue[namespace].update(changes)

            # Update setting cache
            if namespace in self._setting_cache:
                self._setting_cache[namespace].update(changes)
            
            # Set the notification timer
            if self._notification_timer is None:
                self._notification_timer = self.server.get_event_loop().delay_callback(
                    self._notification_interval, self._batch_notify
                )
    
    async def _batch_notify(self) -> None:
        """Send batch notifications for all queued changes (for internal changes)"""
        async with self._notification_lock:
            if not self._notification_queue:
                return

            self._notify_all_changes_to_subscribers(self._notification_queue)
            self._notification_queue.clear()
            self._notification_timer = None
    
    def _notify_all_changes_to_subscribers(self, all_changes: Dict[str, Dict[str, Any]]) -> None:
        """Notify all changes to all subscribers"""
        notification_template = {
            "jsonrpc": "2.0",
            "method": "notify_printer_setting_changed",
            "params": {}
        }
        subscriber_namespaces_copy = self._subscriber_namespaces.copy()
        for conn in subscriber_namespaces_copy:
            notification_template["params"].clear()
            for namespace in subscriber_namespaces_copy[conn]:
                if namespace in all_changes.keys(): 
                    notification_template["params"][namespace] = all_changes.get(namespace, {})
            try:
                conn.queue_message(notification_template)
            except Exception as e:
                logging.exception(f"Failed to send setting notification: {e}")
                # Remove invalid connection from all subscriptions
                self._remove_subscriber(conn)
                continue

    def _remove_subscriber(self, connection: Any) -> None:
        """Remove a connection from all subscriptions"""
        if connection in self._subscriber_namespaces:
            del self._subscriber_namespaces[connection]
    
    def _create_log_archive(self, zip_path: str, logs_dir: str) -> None:
        """Create log archive in a background thread"""
        import zipfile
        import os
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(logs_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    # Calculate relative path for the zip archive
                    rel_path = os.path.relpath(file_path, logs_dir)
                    zf.write(file_path, rel_path)
    
    async def _handle_get_logs_url(self, web_request: WebRequest) -> Dict[str, Any]:
        """Handle machine.logs.download request"""
        import os
        import tempfile
        import time
        
        # 1. Rate limiting
        current_time = time.time()
        if current_time - self._last_log_download_time < self._log_download_cooldown:
            remaining = int(self._log_download_cooldown - (current_time - self._last_log_download_time))
            logging.info(f"Log download rate limited. Please try again in {remaining} seconds.")
            return {"error": f"Log download rate limited. Please try again in {remaining} seconds."}

        self._last_log_download_time = current_time
        
        # Define the logs directory path
        logs_dir = "/home/anisoprint/printer_data/logs"
        
        # Check if the logs directory exists
        if not os.path.exists(logs_dir) or not os.path.isdir(logs_dir):
            logging.error(f"Logs directory not found: {logs_dir}")
            return {"error": f"Logs directory not found: {logs_dir}"}
        
        # Create a temporary zip file
        suffix = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
        zip_filename = f"printer_logs_{suffix}.zip"
        
        temp_dir = tempfile.gettempdir()
        zip_path = os.path.join(temp_dir, zip_filename)
        
        try:
            # 3. Check disk space
            stat = os.statvfs(temp_dir)
            free_space = stat.f_bavail * stat.f_frsize
            # Ensure at least 300MB free space
            if free_space < 300 * 1024 * 1024:
                logging.error(f"Insufficient disk space. Required: 300MB, Available: {free_space // (1024 * 1024)}MB")
                return {"error": f"Insufficient disk space. Required: 300MB, Available: {free_space // (1024 * 1024)}MB"}
            
            # Run zip creation in background thread to avoid blocking event loop
            evt_loop = self.server.get_event_loop()
            await evt_loop.run_in_thread(lambda: self._create_log_archive(zip_path, logs_dir))
            
            # Get file size
            file_size = os.path.getsize(zip_path)
            
            # 2. Auto-cleanup function
            def cleanup_zip_file():
                """Clean up the temporary zip file after download"""
                try:
                    if os.path.exists(zip_path):
                        os.unlink(zip_path)
                        logging.info(f"Cleaned up temporary log zip file: {zip_path}")
                except Exception as e:
                    logging.exception(f"Error cleaning up temporary log zip file: {e}")
            
            # Register a temporary static file handler for the zip file
            app = self.server.lookup_component("application")
            temp_log_path = f"temp_logs/{zip_filename}"
            # Register the file handler without callback
            app.register_static_file_handler(temp_log_path, zip_path, force=True)
            
            # Schedule cleanup after a delay to allow download
            async def delayed_cleanup():
                """Clean up the temporary zip file after a delay"""
                await asyncio.sleep(600)  # 10 minutes delay
                cleanup_zip_file()
            
            asyncio.create_task(delayed_cleanup())
            
            # Return the zip file as a downloadable resource
            return {
                "filename": zip_filename,
                "size": file_size,
                "url": f"/server/files/{temp_log_path}"
            }
        except Exception as e:
            # Clean up if any error occurs
            if os.path.exists(zip_path):
                try:
                    os.unlink(zip_path)
                except Exception:
                    pass
            logging.exception(f"Error creating log archive: {e}")
            raise self.server.error(f"Failed to create log archive: {str(e)}")


    async def component_init(self) -> None:
        # Component initialization
        await self._init_setting_cache()


def load_component(config: ConfigHelper) -> PrinterSetting:
    """Load the printer_setting component"""
    return PrinterSetting(config)