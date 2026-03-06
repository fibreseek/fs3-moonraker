import logging
import csv
import re
import os
import time
from typing import Dict, Any, List, Optional

from ..confighelper import ConfigHelper
from ..common import RequestType, WebRequest
from ..utils import ServerError


DB_NAMESPACE = "moonraker"
DB_LANGUAGE_KEY = "error_indexer.language"
WEBHOOK_ERROR_STATES = ("shutdown", "error")
CSV_ENCODINGS = ("gb2312", "gbk", "utf-8", "utf-8-sig", "gb18030")


class ErrorIndexer:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.name = config.get_name()
        self.database = self.server.lookup_component("database")
        self.jsonrpc = self.server.lookup_component("jsonrpc")

        self.csv_path = self._get_csv_path(config)
        self.language = config.get("language", "en")
        self.error_db: Dict[str, Dict[str, Any]] = {}

        self._load_error_db()
        logging.info(
            "Error Indexer: 组件已加载 (module=%s, csv_path=%s, language=%s, db_size=%d)",
            __file__, self.csv_path, self.language, len(self.error_db)
        )

        self._register_handlers()
        self._register_endpoints()
        self._register_notifications()
        self._hook_gcode_api()

    def _get_csv_path(self, config: ConfigHelper) -> str:
        default_csv = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../../error_info.CSV")
        )
        return str(config.get("csv_path", default_csv))

    # 注册事件处理程序
    def _register_handlers(self) -> None:
        # 1) 捕获 webhooks 的停机/错误信息
        self.server.register_event_handler(
            "server:status_update", self._handle_status_update
        )
        # 2) 捕获 Klipper 输出的 G-code 响应（含 "!!" 标准错误）
        self.server.register_event_handler(
            "server:gcode_response", self._handle_gcode_response
        )

    # 注册 REST API 端点
    def _register_endpoints(self) -> None:
        self.server.register_endpoint(
            "/server/error_indexer/config",
            RequestType.POST,
            self._handle_config_update,
        )

    # 注册 WebSocket 通知
    def _register_notifications(self) -> None:
        self.server.register_notification("error_indexer:error_detected")

    # 组件初始化
    async def component_init(self) -> None:
        """组件初始化：从数据库恢复语言设置。"""
        await self._load_persisted_language()

    # 从数据库恢复语言设置
    async def _load_persisted_language(self) -> None:
        try:
            stored_lang = await self.database.get_item(
                DB_NAMESPACE, DB_LANGUAGE_KEY, None
            )
        except Exception as e:
            logging.warning(
                "Error Indexer: 恢复语言设置失败，将使用当前配置的语言 (%s)，错误: %s",
                self.language, e
            )
            return

        if isinstance(stored_lang, str) and stored_lang:
            self.language = stored_lang
            logging.info("Error Indexer: 从数据库恢复语言设置为 %s", self.language)

    # 持久化语言设置
    async def _persist_language(self) -> None:
        try:
            await self.database.insert_item(DB_NAMESPACE, DB_LANGUAGE_KEY, self.language)
        except Exception as e:
            logging.warning(
                "Error Indexer: 持久化语言设置失败 (%s): %s",
                self.language, e
            )

    # 挂钩 printer.gcode.script API 以捕获错误响应。
    def _hook_gcode_api(self) -> None:
        """挂钩 printer.gcode.script API 以捕获错误响应。"""
        method_info = self.jsonrpc.get_method("printer.gcode.script")
        if not method_info:
            return

        _, api_def = method_info
        orig_callback = api_def.callback

        async def wrapped_callback(web_request: WebRequest) -> Any:
            try:
                return await orig_callback(web_request)
            except ServerError as e:
                error_msg = str(e)
                self._handle_gcode_response(f"!! {error_msg}")
                raise
                
        api_def.callback = wrapped_callback
        logging.info("Error Indexer: 已挂钩 printer.gcode.script API")

    def _csv_get(self, row: List[str], idx: int) -> str:
        if idx < len(row):
            return row[idx].strip()
        return ""

    def _load_error_db(self) -> None:
        """从 CSV 文件加载错误信息。"""
        if not os.path.exists(self.csv_path):
            logging.warning(f"Error Indexer: 在 {self.csv_path} 未找到 CSV 文件")
            return

        for encoding in CSV_ENCODINGS:
            try:
                logging.info(f"Error Indexer: 尝试使用 {encoding} 编码加载 CSV...")
                with open(self.csv_path, 'r', encoding=encoding) as f:
                    reader = csv.reader(f)
                    self.error_db = {}
                    header_skipped = False
                    for row in reader:
                        if not row:
                            continue

                        if not header_skipped:
                            first_col = self._csv_get(row, 0)
                            if first_col.startswith('\ufeff'):
                                first_col = first_col[1:]
                            
                            if "Error Code" in first_col or "Code" in first_col:
                                header_skipped = True
                                continue

                            if not first_col.isdigit():
                                header_skipped = True
                                continue

                        if len(row) < 2:
                            continue

                        code = self._csv_get(row, 0)
                        if code.startswith('\ufeff'):
                            code = code[1:]
                             
                        if not code:
                            continue

                        entry = {
                            'code': code,
                            # 第二列：报错信息（模板/匹配）
                            'msg_match': self._csv_get(row, 1),
                            # 第三列：报错信息多语言
                            'msg_multi': self._parse_multi_lang(self._csv_get(row, 2)),
                            # 第四列：解决方法（多语言文字或单个网址）
                            'solution_multi': self._parse_solution(self._csv_get(row, 3)),
                        }
                        self.error_db[code] = entry
                        
                if len(self.error_db) > 0:
                    logging.info(f"Error Indexer: 使用 {encoding} 成功加载 {len(self.error_db)} 条错误记录。")
                    return
                else:
                    logging.warning(f"Error Indexer: 使用 {encoding} 解码成功但未找到有效记录。")
                    continue
                
            except UnicodeDecodeError:
                continue
            except Exception as e:
                logging.error(f"Error Indexer: 加载 CSV 失败 ({encoding}): {e}")
                continue

        logging.error("Error Indexer: 所有编码尝试均失败，无法加载 CSV 文件。")

    def _parse_multi_lang(self, text: str) -> Dict[str, str]:
        """解析多语言字符串，支持 'zh:内容' 或 'zh：内容'。"""
        if not text:
            return {}
            
        result = {}
        text = text.replace('：', ':')
        lines = text.replace('\r\n', '\n').split('\n')
        current_lang = 'en'
        buffer = []
        
        for line in lines:
            line = line.strip()
            # 仅支持 ISO 639-1 + ISO 3166-1（如 zh-CN/en-US）
            match = re.match(r'^([A-Za-z]{2}[-_][A-Za-z]{2}):', line)
            if match:
                if buffer and current_lang:
                    result[current_lang] = '\n'.join(buffer).strip()
                current_lang = self._normalize_locale(match.group(1))
                buffer = [line[len(match.group(0)):].strip()]
            else:
                buffer.append(line)
        
        if buffer and current_lang:
            result[current_lang] = '\n'.join(buffer).strip()
            
        return result

    def _normalize_locale(self, locale: str) -> str:
        """
        规范化语言标识：
        - 仅支持 ISO 639-1 + ISO 3166-1：如 zh-CN / en-US（以及 zh_CN 形式）
        规则：language 小写，region 大写，分隔符统一为 '-'
        """
        raw = (locale or "").strip()
        if not raw:
            return ""
        raw = raw.replace("_", "-")
        parts = [p for p in raw.split("-") if p]
        if len(parts) != 2:
            return ""
        language = parts[0].lower()
        region = parts[1].upper()
        if not re.fullmatch(r"[a-z]{2}", language):
            return ""
        if not re.fullmatch(r"[A-Z]{2}", region):
            return ""
        return f"{language}-{region}"

    def _locale_candidates(self, locale: str) -> List[str]:
        """
        返回候选 locale 列表，用于在同一多语言列内做兼容回退：
        - 仅精确 locale（如 zh-CN），不兼容旧格式，也不做语言级回退
        """
        norm = self._normalize_locale(locale)
        if not norm:
            return []
        return [norm]

    def _parse_solution(self, text: str) -> Dict[str, str]:
        """
        解析解决方法列：
        - 多语言文字：de：...\nit：...\nzh：...
        - 或单个网址：https://...
        """
        raw = (text or "").strip()
        if not raw:
            return {}
        if raw.startswith("http://") or raw.startswith("https://"):
            # 单个链接视为通用解决方案，使用标准 locale key
            return {"en-US": raw}
        return self._parse_multi_lang(raw)

    # 根据错误消息在 CSV 数据库中匹配错误码。
    def _match_error_code(self, message: str) -> Optional[str]:
        """
        根据错误消息在 CSV 数据库中匹配错误码。

        使用第二列 msg_match 作为模板，通过正则归一化数字和占位符。
        """
        matched_code: Optional[str] = None
        for code, entry in self.error_db.items():
            db_msg = entry.get('msg_match', '').strip()
            if not db_msg:
                continue

            # 1. 数字归一化为占位符
            pattern_str = re.sub(r'[\d\.\-]+', r'___NUM___', db_msg)
            # 2. 转义普通字符
            pattern_str = re.escape(pattern_str)
            # 3. 还原数字占位符为通用正则
            pattern_str = pattern_str.replace(r'___NUM___', r'[\d\.\-]+')
            # 4. 处理 %s / %d / %f 占位符
            pattern_str = (
                pattern_str
                .replace("%s", r".*?")
                .replace("%d", r"\d+")
                .replace("%f", r"[\d\.]+")
            )

            try:
                if re.search(pattern_str, message, re.IGNORECASE):
                    matched_code = code
                    break
            except re.error:
                logging.warning(
                    f"Error Indexer: Invalid regex pattern from CSV: {pattern_str}"
                )
                continue

        return matched_code

    # 处理传入的 G-code 响应。
    def _handle_gcode_response(self, response: str) -> None:
        """处理传入的 G-code 响应。"""
        logging.debug(f"Error Indexer 收到响应: {response}")

        clean_response = response.strip()
        is_standard_error = clean_response.startswith("!!")
        
        if is_standard_error:
            clean_msg = clean_response[2:].strip()
        else:
            clean_msg = clean_response

        matched_code = self._match_error_code(clean_msg)
        
        if matched_code:
            self._process_error(clean_msg, response, matched_code)
        elif is_standard_error:
            logging.info(
                "Error Indexer: 标准错误未命中 CSV，按 UNKNOWN 处理: %s",
                clean_msg
            )
            self._process_error(clean_msg, response, None)

    # 处理来自 server:status_update 的状态通知。
    async def _handle_status_update(self, status: Dict[str, Any]) -> None:
        """
        处理来自 server:status_update 的状态通知。

        当 webhooks.state 为 shutdown/error 时，提取 state_message 发送通知。
        """
        wh = status.get("webhooks")
        if not isinstance(wh, dict):
            return

        state = wh.get("state")
        if state not in WEBHOOK_ERROR_STATES:
            return

        state_message = wh.get("state_message", "")
        if not state_message:
            return

        matched_code = self._match_error_code(state_message)
        original_response = f"webhooks.state={state}, state_message={state_message}"
        self._process_error(state_message, original_response, matched_code)

    # 处理检测到的错误。
    def _process_error(self, error_msg: str, original_response: str, code: Optional[str] = None) -> None:
        """处理检测到的错误。"""
        if not code:
            for c, entry in self.error_db.items():
                if entry.get('msg_match') and entry.get('msg_match') in error_msg:
                    code = c
                    break
        
        if not code:
            code = "UNKNOWN"

        entry = self.error_db.get(code, {})
        
        # 根据语言获取描述和解决方案
        if code == "UNKNOWN":
            description = error_msg
            solution = ""
        else:
            description = self._get_description(entry, self.language)
            solution = self._get_solution(entry, self.language)
        
        payload = {
            "error_code": code,
            "error_message": error_msg,
            "description": description,
            "timestamp": time.time(),
            "solution_link": solution,
            "original_response": original_response
        }
        
        self.server.send_event("error_indexer:error_detected", payload)
        logging.info(f"Error Indexer: 检测到错误 {code} - {error_msg}")

    def _get_description(self, entry: Dict[str, Any], lang: str) -> str:
        msg_multi = entry.get('msg_multi', {}) or {}
        for cand in self._locale_candidates(lang):
            value = msg_multi.get(cand, "")
            if value:
                return value
        return ""

    def _get_solution(self, entry: Dict[str, Any], lang: str) -> str:
        sol_multi = entry.get('solution_multi', {}) or {}
        for cand in self._locale_candidates(lang):
            value = sol_multi.get(cand, "")
            if value:
                return value

        # 英文兜底优先顺序：en-US
        for cand in ("en-US",):
            value = sol_multi.get(cand, "")
            if value:
                return value

        # 最后兜底返回任意非空值
        for val in sol_multi.values():
            if val:
                return val
        return ""

    async def _handle_config_update(self, web_request: WebRequest) -> Dict[str, Any]:
        """更新配置的 API。"""
        lang = web_request.get_str("language", None)
        if lang:
            normalized = self._normalize_locale(lang)
            if not normalized:
                raise self.server.error(
                    "Invalid language. Expected ISO 639-1 + ISO 3166-1, e.g. 'zh-CN' or 'en-US'.",
                    400,
                )
            self.language = normalized
            logging.info("Error Indexer: 语言切换为 %s", self.language)
            await self._persist_language()
            
        return {"language": self.language}

def load_component(config: ConfigHelper) -> ErrorIndexer:
    return ErrorIndexer(config)
