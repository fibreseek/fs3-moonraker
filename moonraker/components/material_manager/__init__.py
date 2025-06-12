from .material_manager import MaterialManager

def load_component(config):
    return MaterialManager(config)
