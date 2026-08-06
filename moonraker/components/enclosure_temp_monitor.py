# Enclosure / chamber temperature monitor
#
# Uses the toolhead board sensor (temperature_sensor toolhead_temp) as a proxy
# for in-chamber ambient temperature while printing heat-sensitive plastics
# (PLA / PLA_CF / PETG). When temperature exceeds a per-material threshold,
# pauses the print and raises an alarm that the chamber is too hot.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import logging
import re
import time
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

from ..common import JobEvent, RequestType

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest
    from .klippy_apis import KlippyAPI
    from .job_state import JobState

# Default per-material enclosure temperature limits (°C)
DEFAULT_MATERIAL_LIMITS: Dict[str, float] = {
    "PLA": 40.0,
    "PLA_CF": 40.0,
    "PETG": 45.0,
}

# Normalize "PLA CF", "PLA-CF", "pla_cf" → "PLA_CF"
_NORMALIZE_RE = re.compile(r"[\s\-]+")


def normalize_polymer_type(raw: Optional[str]) -> Optional[str]:
    if not raw or not isinstance(raw, str):
        return None
    value = _NORMALIZE_RE.sub("_", raw.strip()).upper()
    return value or None


class EnclosureTempMonitor:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.enable = config.getboolean("enable", True)
        # 机头板温度传感器，侧面反映舱内环境温度
        self.sensor = config.get("sensor", "temperature_sensor toolhead_temp")
        self.check_interval = config.getfloat(
            "check_interval", 5.0, minval=1.0
        )
        self.pause_on_alarm = config.getboolean("pause_on_alarm", True)
        self.hysteresis = config.getfloat("hysteresis", 2.0, minval=0.0)
        self.alarm_cooldown = config.getfloat(
            "alarm_cooldown", 60.0, minval=0.0
        )
        self.use_file_metadata = config.getboolean("use_file_metadata", True)
        spool_ids = config.getlist(
            "spool_ids", ["P1", "P2"], separator=","
        )
        self.spool_ids: List[str] = [
            s.strip() for s in spool_ids if s and s.strip()
        ]

        # Per-material limits: start from defaults, override from config
        monitored = config.getlist(
            "monitored_materials",
            list(DEFAULT_MATERIAL_LIMITS.keys()),
            separator=",",
        )
        monitored_keys = [
            normalize_polymer_type(m) for m in monitored
        ]
        monitored_keys = [m for m in monitored_keys if m]

        self.material_limits: Dict[str, float] = {}
        for key in monitored_keys:
            default = DEFAULT_MATERIAL_LIMITS.get(key, 40.0)
            # Options: max_temp_PLA / max_temp_PLA_CF / max_temp_PETG
            option = f"max_temp_{key}"
            limit = config.getfloat(option, default, minval=0.0)
            if limit > 0:
                self.material_limits[key] = limit

        self._chamber_temp: Optional[float] = None
        self._alarm_active = False
        self._last_alarm_time = 0.0
        self._last_alarm_material: Optional[str] = None
        self._last_alarm_temp: Optional[float] = None
        self._timer = None
        self._monitoring = False

        eventloop = self.server.get_event_loop()
        self._timer = eventloop.register_timer(self._check_timer)

        self.server.register_event_handler(
            "server:klippy_ready", self._handle_klippy_ready
        )
        self.server.register_event_handler(
            "server:klippy_disconnect", self._handle_klippy_disconnect
        )
        self.server.register_event_handler(
            "job_state:state_changed", self._on_job_state_changed
        )

        self.server.register_endpoint(
            "/server/enclosure_temp_monitor/status",
            RequestType.GET,
            self._handle_status_request,
        )
        self.server.register_notification(
            "enclosure_temp_monitor:alarm",
            "enclosure_temp_monitor_alarm",
        )
        self.server.register_notification(
            "enclosure_temp_monitor:cleared",
            "enclosure_temp_monitor_cleared",
        )

        logging.info(
            "EnclosureTempMonitor: loaded (enable=%s, sensor=%s, "
            "limits=%s, interval=%.1fs)",
            self.enable,
            self.sensor,
            self.material_limits,
            self.check_interval,
        )

    async def component_init(self) -> None:
        if not self.enable:
            logging.info("EnclosureTempMonitor: disabled by config")
            return
        if not self.material_limits:
            logging.warning(
                "EnclosureTempMonitor: no material limits configured, "
                "monitor will not trigger"
            )

    async def _handle_klippy_ready(self) -> None:
        if not self.enable:
            return
        # Subscribe to chamber temperature updates
        try:
            kapis: KlippyAPI = self.server.lookup_component("klippy_apis")
            result = await kapis.subscribe_objects(
                {self.sensor: ["temperature", "target"]},
                self._on_sensor_update,
            )
            sensor_data = result.get(self.sensor, {})
            temp = sensor_data.get("temperature")
            if temp is not None:
                self._chamber_temp = float(temp)
            logging.info(
                "EnclosureTempMonitor: subscribed to %s (temp=%.1f)",
                self.sensor,
                self._chamber_temp if self._chamber_temp is not None else -1,
            )
        except Exception as e:
            logging.warning(
                "EnclosureTempMonitor: failed to subscribe %s: %s",
                self.sensor, e
            )

        if self._is_printing():
            self._start_monitor()

    def _handle_klippy_disconnect(self) -> None:
        self._stop_monitor()
        self._chamber_temp = None
        self._alarm_active = False

    async def _on_job_state_changed(
        self,
        job_event: JobEvent,
        prev_stats: Dict[str, Any],
        new_stats: Dict[str, Any],
    ) -> None:
        if not self.enable:
            return
        if job_event in (JobEvent.STARTED, JobEvent.RESUMED):
            self._start_monitor()
        elif job_event in (
            JobEvent.COMPLETE,
            JobEvent.CANCELLED,
            JobEvent.ERROR,
            JobEvent.STANDBY,
        ):
            self._stop_monitor()
            self._alarm_active = False
        elif job_event == JobEvent.PAUSED:
            # Keep timer running so we can clear alarm when temp drops,
            # but do not re-pause while already paused.
            pass

    def _on_sensor_update(
        self, data: Dict[str, Any], _: float
    ) -> None:
        if self.sensor not in data:
            return
        temp = data[self.sensor].get("temperature")
        if temp is not None:
            self._chamber_temp = float(temp)

    def _start_monitor(self) -> None:
        if self._monitoring or not self.enable:
            return
        self._monitoring = True
        if self._timer is not None:
            self._timer.start(delay=self.check_interval)
        logging.info("EnclosureTempMonitor: monitoring started")

    def _stop_monitor(self) -> None:
        if not self._monitoring:
            return
        self._monitoring = False
        if self._timer is not None:
            self._timer.stop()
        logging.info("EnclosureTempMonitor: monitoring stopped")

    def _is_printing(self) -> bool:
        try:
            job_state: JobState = self.server.lookup_component("job_state")
            return job_state.get_last_job_event() in (
                JobEvent.STARTED,
                JobEvent.RESUMED,
            )
        except Exception:
            return False

    async def _check_timer(self, eventtime: float) -> float:
        if self._monitoring and self.enable:
            try:
                await self._evaluate()
            except Exception:
                logging.exception("EnclosureTempMonitor: check failed")
        return eventtime + self.check_interval

    async def _evaluate(self) -> None:
        temp = await self._get_chamber_temp()
        if temp is None:
            return

        active = self._get_active_polymers()
        if not active:
            # No monitored material in use; clear any stale alarm quietly
            if self._alarm_active:
                await self._clear_alarm(temp, silent=True)
            return

        # Among active monitored materials, find the most restrictive breach
        breach: Optional[Tuple[str, float]] = None
        for polymer in active:
            limit = self.material_limits.get(polymer)
            if limit is None:
                continue
            if temp > limit:
                if breach is None or limit < breach[1]:
                    breach = (polymer, limit)

        if breach is not None:
            polymer, limit = breach
            await self._trigger_alarm(temp, polymer, limit)
            return

        # Below all limits — clear with hysteresis
        if self._alarm_active and self._last_alarm_material:
            limit = self.material_limits.get(
                self._last_alarm_material, 0.0
            )
            if temp <= (limit - self.hysteresis):
                await self._clear_alarm(temp)

    async def _get_chamber_temp(self) -> Optional[float]:
        if self._chamber_temp is not None:
            return self._chamber_temp
        try:
            kapis: KlippyAPI = self.server.lookup_component("klippy_apis")
            status = await kapis.query_objects(
                {self.sensor: ["temperature"]}, {}
            )
            sensor_data = status.get(self.sensor, {})
            temp = sensor_data.get("temperature")
            if temp is not None:
                self._chamber_temp = float(temp)
                return self._chamber_temp
        except Exception as e:
            logging.debug(
                "EnclosureTempMonitor: query temperature failed: %s", e
            )
        return None

    def _get_active_polymers(self) -> Set[str]:
        """Return polymer types currently relevant for monitoring."""
        polymers: Set[str] = set()

        # 1) material_manager spools (primary)
        try:
            mm = self.server.lookup_component("material_manager", None)
            if mm is not None and getattr(mm, "enabled", True):
                spools = getattr(mm, "spools", {}) or {}
                materials = getattr(mm, "materials", {}) or {}
                spool_map = spools.get("spools", {})
                for spool_id in self.spool_ids:
                    spool = spool_map.get(spool_id, {})
                    material_id = spool.get("material_id") or ""
                    if not material_id:
                        continue
                    info = materials.get(material_id, {})
                    polymer = normalize_polymer_type(
                        info.get("polymer_type") or info.get("name")
                    )
                    if polymer and polymer in self.material_limits:
                        polymers.add(polymer)
        except Exception as e:
            logging.debug(
                "EnclosureTempMonitor: material_manager lookup failed: %s", e
            )

        # 2) gcode metadata filament_type (fallback / supplement)
        if self.use_file_metadata:
            meta_type = self._get_print_filament_type()
            if meta_type and meta_type in self.material_limits:
                polymers.add(meta_type)

        return polymers

    def _get_print_filament_type(self) -> Optional[str]:
        try:
            job_state: JobState = self.server.lookup_component("job_state")
            stats = job_state.get_last_stats()
            filename = (stats or {}).get("filename") or ""
            if not filename:
                return None
            fm = self.server.lookup_component("file_manager", None)
            if fm is None:
                return None
            # Prefer sync metadata cache if available
            get_meta = getattr(fm, "get_file_metadata", None)
            if get_meta is None:
                return None
            meta = get_meta(filename)
            if not isinstance(meta, dict):
                return None
            return normalize_polymer_type(meta.get("filament_type"))
        except Exception as e:
            logging.debug(
                "EnclosureTempMonitor: filament_type from metadata failed: %s",
                e,
            )
            return None

    async def _trigger_alarm(
        self, temp: float, polymer: str, limit: float
    ) -> None:
        now = time.time()
        should_notify = True
        if self._alarm_active:
            # Avoid notification spam while still over limit
            if (now - self._last_alarm_time) < self.alarm_cooldown:
                should_notify = False
        else:
            self._alarm_active = True

        self._last_alarm_material = polymer
        self._last_alarm_temp = temp

        # 消息格式需与 error_info.xlsx 中错误码模板一致，供 error_indexer 匹配
        # 提醒舱内（打印腔）温度过高，不以机头壳内为唯一语义
        message = (
            f"Chamber temperature too high: {temp:.1f}C "
            f"(limit {limit:.1f}C for {polymer})"
        )
        payload = {
            "message": message,
            "temperature": round(temp, 2),
            "limit": limit,
            "material": polymer,
            "sensor": self.sensor,
            "paused": False,
        }

        # Always re-pause if user resumed while still over limit
        if self.pause_on_alarm and self._is_printing():
            paused = await self._pause_print()
            payload["paused"] = paused
            if not should_notify and paused:
                logging.warning(
                    "EnclosureTempMonitor: re-paused print (still over limit)"
                )

        if should_notify:
            self._last_alarm_time = now
            logging.warning("EnclosureTempMonitor: %s", message)
            # 通过 error_indexer 统一检索并推送 notify_error_indexer_error_detected
            self.server.send_event("enclosure_temp_monitor:alarm", payload)

    async def _clear_alarm(
        self, temp: float, silent: bool = False
    ) -> None:
        if not self._alarm_active:
            return
        material = self._last_alarm_material
        self._alarm_active = False
        logging.info(
            "EnclosureTempMonitor: alarm cleared (temp=%.1f, material=%s)",
            temp, material,
        )
        if silent:
            return
        payload = {
            "message": (
                f"Chamber temperature recovered: {temp:.1f}C"
                + (f" (was monitoring {material})" if material else "")
            ),
            "temperature": round(temp, 2),
            "material": material,
            "sensor": self.sensor,
        }
        self.server.send_event("enclosure_temp_monitor:cleared", payload)

    async def _pause_print(self) -> bool:
        try:
            kapis: KlippyAPI = self.server.lookup_component("klippy_apis")
            await kapis.pause_print()
            logging.warning(
                "EnclosureTempMonitor: print paused due to high enclosure temp"
            )
            return True
        except Exception as e:
            logging.error(
                "EnclosureTempMonitor: failed to pause print: %s", e
            )
            return False

    async def _handle_status_request(
        self, web_request: WebRequest
    ) -> Dict[str, Any]:
        return {
            "enable": self.enable,
            "monitoring": self._monitoring,
            "sensor": self.sensor,
            "temperature": self._chamber_temp,
            "material_limits": dict(self.material_limits),
            "active_materials": sorted(self._get_active_polymers()),
            "alarm_active": self._alarm_active,
            "last_alarm_material": self._last_alarm_material,
            "last_alarm_temp": self._last_alarm_temp,
            "pause_on_alarm": self.pause_on_alarm,
            "check_interval": self.check_interval,
            "hysteresis": self.hysteresis,
        }

    async def close(self) -> None:
        self._stop_monitor()


def load_component(config: ConfigHelper) -> EnclosureTempMonitor:
    return EnclosureTempMonitor(config)
