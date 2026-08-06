import os
import json
import logging
from typing import Dict, Any, List, Optional
from ...common import JobEvent

class MaterialManager:
    def __init__(self, config):
        self.server = config.get_server()
        
        # 创建日志记录器
        self.logger = logging.getLogger("moonraker.material_manager")
        
        # 检查是否启用材料管理
        self.enabled = config.getboolean('enabled', True)
        if not self.enabled:
            self.logger.info("Material Manager is disabled")
            return
            
        # 从配置中获取路径，支持相对路径和绝对路径
        config_path = config.get('config_path', '~/printer_data/config')
        if config_path.startswith('~/'):
            self.config_path = os.path.expanduser(config_path)
        else:
            self.config_path = config_path
            
        self.materials_file = os.path.join(self.config_path, 'materials.json')
        self.spools_file = os.path.join(self.config_path, 'spools.json')
        
        self.materials = {}
        self.spools = {}
        
        self.logger.info("Initializing Material Manager")

        # 记录上一次的耗材使用量
        self.last_e_filament_used = 0
        self.last_u_filament_used = 0
        self.last_v_filament_used = 0
        
        # 加载材料和料卷数据
        self._load_materials()
        self._load_spools()
        
        # 注册JSON-RPC接口
        self._register_endpoints()

        self.server.register_event_handler(
            "job_state:state_changed", self._on_job_state_changed)
        
        self.logger.info("Material Manager initialized successfully")
    
    async def _on_job_state_changed(
        self,
        job_event: JobEvent,
        prev_stats: Dict[str, Any],
        new_stats: Dict[str, Any]
    ) -> None:
        if job_event == JobEvent.COMPLETE:
            await self.update_filament_used("completed", new_stats)
        elif job_event == JobEvent.ERROR:
            await self.update_filament_used("error", new_stats)
        elif job_event in (JobEvent.CANCELLED, JobEvent.STANDBY):
            # Cancel on "standby" for backward compatibility with
            # `CLEAR_PAUSE/SDCARD_RESET_FILE` workflow
            await self.update_filament_used("cancelled", new_stats)
        elif job_event == JobEvent.STARTED:
            self.last_e_filament_used = new_stats.get('e_filament_used', 0)
            self.last_u_filament_used = new_stats.get('u_filament_used', 0)
            self.last_v_filament_used = new_stats.get('v_filament_used', 0)
            logging.info(f"Job started with status '{job_event}': e_filament_used = {self.last_e_filament_used}, u_filament_used = {self.last_u_filament_used}, v_filament_used = {self.last_v_filament_used}")
        elif job_event == JobEvent.PAUSED:
            await self.update_filament_used("paused", new_stats)
    
    async def update_filament_used(self, status: str, new_stats: Dict[str, Any]) -> None:
        e_filament_used = new_stats.get('e_filament_used', 0)
        u_filament_used = new_stats.get('u_filament_used', 0)
        v_filament_used = new_stats.get('v_filament_used', 0)
        update_spools = False

        if e_filament_used > self.last_e_filament_used:
            temp_e_filament_used = e_filament_used - self.last_e_filament_used
            if 'spools' in self.spools and 'P2' in self.spools['spools']:
                spool_p2 = self.spools['spools']['P2']
                if 'filament_used_mm' not in spool_p2:
                    spool_p2['filament_used_mm'] = 0.0
                spool_p2['filament_used_mm'] += temp_e_filament_used
                self.last_e_filament_used = e_filament_used
                update_spools = True

        if u_filament_used > self.last_u_filament_used:
            temp_u_filament_used = u_filament_used - self.last_u_filament_used
            if 'spools' in self.spools and 'F' in self.spools['spools']:
                spool_f = self.spools['spools']['F']
                if 'filament_used_mm' not in spool_f:
                    spool_f['filament_used_mm'] = 0.0
                spool_f['filament_used_mm'] += temp_u_filament_used
                self.last_u_filament_used = u_filament_used
                update_spools = True
                self.logger.info(f"Job updated with status '{status}': u_filament_used = {u_filament_used}")
        if v_filament_used > self.last_v_filament_used:
            temp_v_filament_used = v_filament_used - self.last_v_filament_used
            if 'spools' in self.spools and 'P1' in self.spools['spools']:
                spool_p1 = self.spools['spools']['P1']
                if 'filament_used_mm' not in spool_p1:
                    spool_p1['filament_used_mm'] = 0.0
                spool_p1['filament_used_mm'] += temp_v_filament_used
                self.last_v_filament_used = v_filament_used
                update_spools = True
                self.logger.info(f"Job updated with status '{status}': Added {v_filament_used}mm to P1 spool, v total filament used: {spool_p1['filament_used_mm']}mm")
        if update_spools:
            self._save_spools()
            self.logger.info(f"Job updated with status '{status}': Updated spools data saved.")
        for key, value in new_stats.items():
            self.logger.info(f"Job updated with status '{status}': {key} = {value}")

    def _register_endpoints(self):
        """注册所有JSON-RPC端点"""
        # 使用moonraker的标准方法注册API端点
        self.server.register_endpoint(
            "/server/materials/list", ["GET"], self._handle_list_materials,
            transports=["http", "websocket"]
        )
        self.server.register_endpoint(
            "/server/materials/get", ["GET"], self._handle_get_material,
            transports=["http", "websocket"]
        )
        self.server.register_endpoint(
            "/server/spools/status", ["GET"], self._handle_get_spools_status,
            transports=["http", "websocket"]
        )
        self.server.register_endpoint(
            "/server/spools/update", ["POST"], self._handle_update_spool,
            transports=["http", "websocket"]
        )

    def _load_materials(self):
        """加载材料配置"""
        try:
            if os.path.exists(self.materials_file):
                with open(self.materials_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.materials = data.get('materials', {})
            else:
                self._create_default_materials()
        except Exception as e:
            logging.error(f"Failed to load materials: {e}")
            self._create_default_materials()

    def _load_spools(self):
        """加载料卷状态配置"""
        try:
            if os.path.exists(self.spools_file):
                with open(self.spools_file, 'r', encoding='utf-8') as f:
                    self.spools = json.load(f)
            else:
                self._create_default_spools()
        except Exception as e:
            logging.error(f"Failed to load spools: {e}")
            self._create_default_spools()

    def _create_default_materials(self):
        """创建默认材料配置"""
        default_data = {
            "version": "1.0",
            "materials": {
                "PLA_001": {
                    "id": "PLA_001",
                    "brand": "Generic",
                    "name": "PLA Basic",
                    "diameter": 1.75,
                    "density": 1.24,
                    "polymer_type": "PLA",
                    "extrusion_temp": 210
                },
                "PETG_001": {
                    "id": "PETG_001",
                    "brand": "Generic",
                    "name": "PETG Clear",
                    "diameter": 1.75,
                    "density": 1.27,
                    "polymer_type": "PETG",
                    "extrusion_temp": 240
                }
            }
        }
        self.materials = default_data["materials"]
        self._save_materials(default_data)

    def _create_default_spools(self):
        """创建默认料卷配置"""
        default_data = {
            "version": "1.0",
            "spools": {
                "F": {
                    "material_id": "",
                    "color": "",
                    "total_length_mm": 0.0,
                    "total_weight_mg": 0.0,
                    "remaining_weight": 0.0,
                    "filament_used_mm": 0.0
                },
                "P1": {
                    "material_id": "",
                    "color": "",
                    "total_length_mm": 0.0,
                    "total_weight_mg": 0.0,
                    "remaining_weight": 0.0,
                    "filament_used_mm": 0.0
                },
                "P2": {
                    "material_id": "",
                    "color": "",
                    "total_length_mm": 0.0,
                    "total_weight_mg": 0.0,
                    "remaining_weight": 0.0,
                    "filament_used_mm": 0.0
                }
            }
        }
        self.spools = default_data
        self._save_spools()

    def _save_materials(self, data):
        """保存材料配置"""
        os.makedirs(os.path.dirname(self.materials_file), exist_ok=True)
        with open(self.materials_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _save_spools(self):
        """保存料卷配置"""
        os.makedirs(os.path.dirname(self.spools_file), exist_ok=True)
        with open(self.spools_file, 'w', encoding='utf-8') as f:
            json.dump(self.spools, f, indent=2, ensure_ascii=False)

    async def _handle_list_materials(self, web_request):
        """获取材料列表"""
        return {"materials": self.materials}

    async def _handle_get_material(self, web_request):
        """获取指定材料信息"""
        # 支持HTTP GET参数和JSON-RPC params
        material_id = web_request.get_str('id', None)
        if material_id is None:
            # 尝试从JSON-RPC params获取
            params = getattr(web_request, 'params', {})
            if isinstance(params, dict):
                material_id = params.get('id')
        
        if not material_id:
            raise self.server.error("Missing required parameter: id", 400)
            
        if material_id in self.materials:
            return {"material": self.materials[material_id]}
        else:
            raise self.server.error(f"Material {material_id} not found", 404)

    def get_spool_material_name(self, spool_id: str) -> str:
        """返回指定通道（F/P1/P2）当前装载材料的名称，未装载或材料库缺失时返回空串。"""
        spool = self.spools.get("spools", {}).get(spool_id, {})
        material_id = spool.get("material_id", "")
        if not material_id:
            return ""
        return str(self.materials.get(material_id, {}).get("name", ""))

    async def _handle_get_spools_status(self, web_request):
        """获取料卷状态"""
        result = {}
        for spool_id, spool_data in self.spools.get("spools", {}).items():
            material_info = {}
            if spool_data.get("material_id"):
                material_info = self.materials.get(spool_data["material_id"], {})
            
            result[spool_id] = {
                "material_id": spool_data.get("material_id", ""),
                "color": spool_data.get("color", ""),
                "total_weight_mg": spool_data.get("total_weight_mg", 0.0),
                "filament_used_mm": spool_data.get("filament_used_mm", 0.0),
                "total_length_mm": spool_data.get("total_length_mm", 0.0),
                "material_info": material_info
            }
        return {"spools": result}

    async def _handle_update_spool(self, web_request):
        """更新料卷信息（包括设置材料）"""
        # 支持HTTP POST和JSON-RPC params
        spool_id = web_request.get_str('spool_id', None)
        color = web_request.get_str('color', None)
        total_weight_mg = web_request.get_float('total_weight_mg', None)
        total_length_mm = web_request.get_float('total_length_mm', None)
        filament_used_mm = web_request.get_float('filament_used_mm', None)
        material_id = web_request.get_str('material_id', None)
        
        # 如果HTTP参数为空，尝试从JSON-RPC params获取
        if spool_id is None:
            params = getattr(web_request, 'params', {})
            if isinstance(params, dict):
                spool_id = params.get('spool_id')
                color = params.get('color')
                total_weight_mg = params.get('total_weight_mg')
                total_length_mm = web_request.get_float('total_length_mm', None)
                filament_used_mm = web_request.get_float('filament_used_mm', None)
                material_id = params.get('material_id')
                if total_weight_mg is not None:
                    try:
                        total_weight_mg = float(total_weight_mg)
                    except (ValueError, TypeError):
                        raise self.server.error("Invalid total_weight value", 400)
                if total_length_mm is not None:
                    try:
                        total_length_mm = float(total_length_mm)
                    except (ValueError, TypeError):
                        raise self.server.error("Invalid total_length_mm value", 400)
                if filament_used_mm is not None:
                    try:
                        filament_used_mm = float(filament_used_mm)
                    except (ValueError, TypeError):
                        raise self.server.error("Invalid filament_used_mm value", 400)

        if not spool_id:
            raise self.server.error("Missing required parameter: spool_id", 400)
            
        if spool_id not in self.spools.get("spools", {}):
            raise self.server.error(f"Spool {spool_id} not found", 404)
        
        # 验证材料ID是否存在
        if material_id is not None and material_id and material_id not in self.materials:
            raise self.server.error(f"Material {material_id} not found", 404)
        
        # 更新料卷信息
        spool_data = self.spools["spools"][spool_id]
        
        if spool_id is not None:
            spool_data['spool_id'] = spool_id
        if color is not None:
            spool_data['color'] = str(color)
        if total_weight_mg is not None:
            spool_data['total_weight_mg'] = total_weight_mg
        if material_id is not None:
            spool_data['material_id'] = material_id
        if filament_used_mm is not None:
            spool_data['filament_used_mm'] = filament_used_mm
        if total_length_mm is not None:
            spool_data['total_length_mm'] = total_length_mm

        self._save_spools()
        
        # 获取材料信息
        material_info = {}
        if spool_data.get('material_id'):
            material_info = self.materials.get(spool_data['material_id'], {})
        
        return {
            "result": "success", 
            "spool": spool_data,
            "material_info": material_info
        }

def load_component(config):
    """组件加载函数，moonraker会调用此函数"""
    return MaterialManager(config)
