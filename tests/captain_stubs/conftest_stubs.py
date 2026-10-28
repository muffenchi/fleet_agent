"""Installs stand-ins for modules the real Captain server.py imports but that live
outside the files this project touches (zmq, msgpack, shared.*, captain.ipc, ...)."""
import sys, types
def install():
    for name in ("zmq", "zmq.asyncio", "msgpack"):
        if name not in sys.modules:
            try: __import__(name)
            except ImportError:
                m = types.ModuleType(name); sys.modules[name] = m
                if name == "zmq": m.SUB = 2; m.asyncio = types.ModuleType("zmq.asyncio"); sys.modules["zmq.asyncio"] = m.asyncio
