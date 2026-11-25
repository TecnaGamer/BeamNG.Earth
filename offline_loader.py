"""Load the integration module offline, stubbing unrelated server services only."""
import importlib.util,sys,types
from pathlib import Path

def load():
    import norway_signs_v073
    for name in ['norway_nvdb_signs_v06','norway_signs_v073.catalog','norway_signs_v073.coverage','norway_signs_v073.facing']:
        if name not in sys.modules:sys.modules[name]=types.ModuleType(name)
    spec=importlib.util.spec_from_file_location('offline_destinations',Path(__file__).parent/'norway_nvdb_destinations_v07.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
