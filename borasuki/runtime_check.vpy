"""Load the actual renderer dependencies and query each GPU through CUDA."""

import json
import os
import sys
from pathlib import Path

values = json.loads(Path(config).read_text(encoding="utf-8"))
plugins = Path(values["runtime"]["plugins"])
dll_handles = [os.add_dll_directory(str(path)) for path in (plugins, plugins / "vsmlrt-cuda")]
sys.path.insert(0, str(plugins))
import numpy as np
import vapoursynth as vs

core = vs.core
core.std.LoadPlugin(str(plugins / "ffms2.dll"))
core.std.LoadPlugin(str(plugins / "vstrt.dll"))
import vsmlrt

version = core.trt.Version()
devices = []
for gpu in values["runtime"]["gpus"]:
    properties = core.trt.DeviceProperties(gpu["id"])
    devices.append({"id": gpu["id"], "name": properties["name"].decode(), "major": properties["major"]})
blank = core.std.BlankClip(width=32, height=32, length=1, format=vs.RGBS)
with blank.get_frame(0) as frame:
    if np.asarray(frame[0]).shape != (32, 32):
        raise ValueError("NumPy frame access failed")
Path(values["report"]).write_text(json.dumps({"devices": devices, "tensorrt": int(version["tensorrt_version"]),
    "vsmlrt": vsmlrt.__version__, "numpy": np.__version__}), encoding="utf-8")
blank.set_output()
