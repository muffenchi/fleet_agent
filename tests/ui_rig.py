"""Starts the real patched Captain server and the real agent (fake joystick, fake face/voice engines) on
localhost for the jsdom UI test (tests/ui_smoke.mjs). Prints one JSON line with ports, then serves until killed.
   CAPTAIN_SRC=/path/to/patched/tree python tests/ui_rig.py"""
import asyncio, json, os, sys, types, tempfile
sys.path[:0] = [os.path.join(os.path.dirname(__file__), "captain_stubs"), os.environ["CAPTAIN_SRC"],
                os.path.join(os.path.dirname(__file__), "..")]
import conftest_stubs; conftest_stubs.install()
import uvicorn
from test_integration import FakeUserStore, FakePads, FakeFace, FakeVoice, pad, free_port
from captain.api import server as captain_server, command_parser
from captain.flight_db.db import FlightDB
from fleet_agent.config import Config
from fleet_agent.security import Guard
from fleet_agent.server import create_app

async def main():
    users = FakeUserStore(); db = FlightDB(os.path.join(tempfile.mkdtemp(), "e.db")); await db.connect()
    await db.save_drone_identity("hw-2975", "2"); await db.save_drone_identity("hw-3000", "alpha")
    cap = captain_server._build_app(user_store=users, policy_engine=types.SimpleNamespace(flight_db=db))
    pads = FakePads()
    @cap.get("/_rig/plug")
    async def plug(on: int = 1, n: int = 8): pads.pads = [pad([False] * n)] if on else []; return {"ok": True}
    @cap.get("/_rig/press")
    async def press(i: int, down: int = 1): pads.pads[0].buttons[i] = bool(down); return {"ok": True}
    @cap.get("/_rig/received")
    async def received(): return [{"cmd": r["cmd"], "source": r["source"]} for r in command_parser.RECEIVED]
    @cap.get("/_rig/settings")
    async def settings(u: str = "op1"): return await db.get_agent_settings(u)
    cport, aport = free_port(), free_port()
    guard = Guard(aport, "ui-token")
    engines = {"face": {"engine": FakeFace(), "reason": ""}, "voice": {"engine": None, "reason": "install resemblyzer"}}
    app, agent = create_app(Config(port=aport, captain_url=f"http://127.0.0.1:{cport}", state_interval_s=0.5),
                            engines=engines, backend_factory=lambda: pads, guard=guard)
    servers = [uvicorn.Server(uvicorn.Config(a, host="127.0.0.1", port=p, log_level="error")) for a, p in ((cap, cport), (app, aport))]
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    while not all(s.started for s in servers): await asyncio.sleep(0.05)
    print(json.dumps({"captain": cport, "agent": aport, "token": "ui-token"}), flush=True)
    await asyncio.gather(*tasks)
asyncio.run(main())
