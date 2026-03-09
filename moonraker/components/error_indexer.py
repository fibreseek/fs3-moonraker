import logging
import re
import os
import time
from typing import Dict, Any, List, Optional, Tuple

from ..confighelper import ConfigHelper
from ..common import RequestType, WebRequest
from ..utils import ServerError


DB_NAMESPACE = "moonraker"
DB_LANGUAGE_KEY = "error_indexer.language"
WEBHOOK_ERROR_STATES = ("shutdown", "error")
PLACEHOLDER_RE = re.compile(r"%(\d+)")


class ErrorIndexer:
    def __init__(self, config: ConfigHelper) -> None:
        self.server = config.get_server()
        self.name = config.get_name()
        # 获取数据库组件
        self.database = self.server.lookup_component("database")
        # 获取 JSON-RPC 组件
        self.jsonrpc = self.server.lookup_component("jsonrpc")
        # 获取错误数据库文件路径
        self.file_path = self._get_file_path(config)
        # 默认读取 xlsx 中名为 "klipper" 的表格，可通过配置覆盖
        self.sheet_name = config.get("sheet_name", "klipper")
        # 获取语言设置
        self.language = config.get("language", "en-US")
        # 错误数据库
        self.error_db: Dict[str, Dict[str, Any]] = {}
        # 加载错误数据库
        self._load_error_db()
        logging.info(
            "Error Indexer: 组件已加载 (module=%s, file_path=%s, language=%s, db_size=%d)",
            __file__, self.file_path, self.language, len(self.error_db)
        )
        # 注册事件处理程序
        self._register_handlers()
        # 注册 REST API 端点
        self._register_endpoints()
        # 注册 WebSocket 通知
        self._register_notifications()
        # 挂钩 printer.gcode.script API
        self._hook_gcode_api()

    def _get_file_path(self, config: ConfigHelper) -> str:
        """xlsx 路径：优先 xlsx_path，其次 file_path、csv_path，最后默认 error_info.xlsx。"""
        default_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../../error_info.xlsx")
        )
        return str(config.get(
            "xlsx_path",
            config.get("file_path", config.get("csv_path", default_path)),
        ))

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
                self._handle_gcode_response(f"!! {str(e)}")
                raise
        api_def.callback = wrapped_callback
        logging.info("Error Indexer: 已挂钩 printer.gcode.script API")

    def _row_get(self, row: Tuple[Any, ...], idx: int) -> str:
        """取行第 idx 列，转为 str 并 strip。"""
        if idx < len(row):
            val = row[idx]
            return str(val).strip() if val is not None else ""
        return ""

    def _parse_placeholders(self, template: str) -> List[str]:
        """从第二列模板按首次出现顺序解析占位符，如 ['%1','%2','%3']。"""
        return list(dict.fromkeys(f"%{m.group(1)}" for m in PLACEHOLDER_RE.finditer(template)))

    def _build_msg_re(self, template: str, placeholders: List[str]) -> Optional[re.Pattern]:
        """构建匹配用正则：%N 首次出现为 (.*?)，再次出现为反向引用。re.escape 不转义 %，故按 %N 分割后分段转义。"""
        if not template:
            return None
        if not placeholders:
            try:
                return re.compile(re.escape(template), re.IGNORECASE)
            except re.error:
                return None
        parts = re.split(r"(%\d+)", template)
        group_for_ph: Dict[str, int] = {}
        next_group = 1
        result_parts: List[str] = []
        for part in parts:
            if PLACEHOLDER_RE.fullmatch(part):
                ph = part
                if ph in group_for_ph:
                    result_parts.append("\\" + str(group_for_ph[ph]))
                else:
                    group_for_ph[ph] = next_group
                    result_parts.append("(.*?)")
                    next_group += 1
            else:
                result_parts.append(re.escape(part))
        try:
            return re.compile("".join(result_parts), re.IGNORECASE)
        except re.error:
            logging.warning("Error Indexer: Invalid regex from template: %s", template[:80])
            return None

    def _load_error_db(self) -> None:
        """从 xlsx 指定 sheet 加载错误表（跳过首行）。"""
        if not os.path.exists(self.file_path):
            logging.warning("Error Indexer: 在 %s 未找到 xlsx 文件", self.file_path)
            return
        try:
            from openpyxl import load_workbook
        except ImportError:
            logging.error("Error Indexer: 需要 openpyxl 才能读取 xlsx，请安装 openpyxl")
            return
        try:
            wb = load_workbook(self.file_path, read_only=True, data_only=True)
            if self.sheet_name not in wb.sheetnames:
                logging.warning(
                    "Error Indexer: xlsx 中未找到表格 %r，可用: %s",
                    self.sheet_name, wb.sheetnames,
                )
                wb.close()
                return
            ws = wb[self.sheet_name]
            self.error_db = {}
            # 无论内容如何都先跳过第一行（表头）
            for row in ws.iter_rows(min_row=2, values_only=True):
                row = tuple(row) if row else ()
                if not row:
                    continue
                code = self._row_get(row, 0)
                if code.startswith("\ufeff"):
                    code = code[1:]
                if len(row) < 2:
                    continue
                if not code:
                    continue
                msg_match = self._row_get(row, 1)
                placeholders = self._parse_placeholders(msg_match)
                msg_re = self._build_msg_re(msg_match, placeholders)
                entry = {
                    "code": code,
                    "msg_match": msg_match,
                    "placeholders": placeholders,
                    "msg_re": msg_re,
                    "msg_multi": self._parse_multi_lang(self._row_get(row, 2)),
                    "solution_multi": self._parse_solution(self._row_get(row, 3)),
                }
                self.error_db[code] = entry
            wb.close()
            if self.error_db:
                logging.info(
                    "Error Indexer: 从 xlsx 成功加载 %d 条错误记录。",
                    len(self.error_db),
                )
        except Exception as e:
            logging.error("Error Indexer: 加载 xlsx 失败: %s", e)

    def _parse_multi_lang(self, text: str) -> Dict[str, str]:
        """解析多语言字符串，支持 xx-XX: 或 xx-XX： 开头，每行一种语言。"""
        if not text:
            return {}
        text = text.replace("：", ":").replace("\r\n", "\n")
        # 按「语言行」切分，成对得到 (lang, content)
        parts = re.split(r"(?m)^([A-Za-z]{2}[-_][A-Za-z]{2}):", text)
        result = {}
        for i in range(1, len(parts), 2):
            if i + 1 < len(parts):
                lang = self._normalize_locale(parts[i].strip())
                if lang:
                    result[lang] = parts[i + 1].strip()
        return result

    def _normalize_locale(self, locale: str) -> str:
        """规范化为 xx-XX（小写语言+大写地区，如 zh-CN / en-US）。"""
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
        """返回候选 locale 列表（当前仅精确匹配，如 zh-CN）。"""
        norm = self._normalize_locale(locale)
        return [norm] if norm else []

    def _parse_solution(self, text: str) -> Dict[str, str]:
        """解析解决方法列：多语言（同 _parse_multi_lang）或单个 http(s) 链接。"""
        raw = (text or "").strip()
        if not raw:
            return {}
        if raw.startswith(("http://", "https://")):
            return {"en-US": raw}
        return self._parse_multi_lang(raw)

    def _match_error_code(self, message: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
        """匹配错误消息，返回 (code, 占位符捕获值) 或 None。"""
        for code, entry in self.error_db.items():
            msg_re = entry.get("msg_re")
            if msg_re is None:
                continue
            m = msg_re.search(message)
            if not m:
                continue
            groups = m.groups() if m.lastindex else ()
            return (code, groups)
        return None

    def _handle_gcode_response(self, response: str) -> None:
        """处理 G-code 响应，!! 开头视为标准错误并匹配。"""
        logging.debug(f"Error Indexer 收到响应: {response}")

        clean_response = response.strip()
        clean_msg = clean_response[2:].strip() if clean_response.startswith("!!") else clean_response
        match_result = self._match_error_code(clean_msg)
        if match_result:
            code, groups = match_result
            self._process_error(clean_msg, response, code=code, placeholder_values=groups)
        elif clean_response.startswith("!!"):
            logging.info("Error Indexer: 标准错误未命中数据库，按 UNKNOWN 处理: %s", clean_msg)
            self._process_error(clean_msg, response, None)

    async def _handle_status_update(self, status: Dict[str, Any]) -> None:
        """webhooks.state 为 shutdown/error 时，用 state_message 匹配并通知。"""
        wh = status.get("webhooks")
        if not isinstance(wh, dict):
            return
        state = wh.get("state")
        if state not in WEBHOOK_ERROR_STATES:
            return
        state_message = wh.get("state_message", "")
        if not state_message:
            return
        match_result = self._match_error_code(state_message)
        original_response = f"webhooks.state={state}, state_message={state_message}"
        if match_result:
            code, groups = match_result
            self._process_error(state_message, original_response, code=code, placeholder_values=groups)
        else:
            self._process_error(state_message, original_response, None)

    def _process_error(
        self,
        error_msg: str,
        original_response: str,
        code: Optional[str] = None,
        placeholder_values: Optional[Tuple[str, ...]] = None,
    ) -> None:
        """处理检测到的错误。"""
        if not code:
            match_result = self._match_error_code(error_msg)
            if match_result:
                code, placeholder_values = match_result
            else:
                for c, ent in self.error_db.items():
                    msg_match = ent.get("msg_match")
                    if msg_match and msg_match in error_msg:
                        code = c
                        break
        if not code:
            code = "UNKNOWN"
        if placeholder_values is None:
            placeholder_values = ()

        entry = self.error_db.get(code, {})

        if code == "UNKNOWN":
            description = error_msg
            solution = ""
        else:
            description = self._get_description(
                entry, self.language, placeholder_values=placeholder_values
            )
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

    def _get_description(
        self,
        entry: Dict[str, Any],
        lang: str,
        placeholder_values: Optional[Tuple[str, ...]] = None,
    ) -> str:
        """获取错误描述。"""
        msg_multi = entry.get("msg_multi", {}) or {}
        for cand in self._locale_candidates(lang):
            template = msg_multi.get(cand, "")
            if not template:
                continue
            if placeholder_values and entry.get("placeholders"):
                placeholders = entry["placeholders"]
                filled = template
                for i, ph in enumerate(placeholders):
                    if i < len(placeholder_values) and ph in filled:
                        filled = filled.replace(ph, placeholder_values[i])
                return filled
            return template
        return ""

    def _get_solution(self, entry: Dict[str, Any], lang: str) -> str:
        """获取解决方法：当前语言 → en-US 兜底 → 任意非空。"""
        sol_multi = entry.get("solution_multi", {}) or {}
        for cand in self._locale_candidates(lang):
            if sol_multi.get(cand, ""):
                return sol_multi[cand]
        if sol_multi.get("en-US", ""):
            return sol_multi["en-US"]
        return next((v for v in sol_multi.values() if v), "")

    async def _handle_config_update(self, web_request: WebRequest) -> Dict[str, Any]:
        """POST 更新语言等配置。"""
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
