"""Company profiling. Preserve stdlib profile symbols for cProfile/torch imports."""
import importlib.util
from pathlib import Path
import sysconfig
_spec = importlib.util.spec_from_file_location('_stdlib_profile', Path(sysconfig.get_path('stdlib'))/'profile.py')
_stdlib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_stdlib)
run, runctx, _Utils = _stdlib.run, _stdlib.runctx, _stdlib._Utils
