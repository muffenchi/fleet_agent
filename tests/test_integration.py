"""
End-to-end: the real (patched) captain/api/server.py + routes/agent.py + routes/agent_settings.py
+ flight_db/db.py + models.py, driven by the real fleet_agent over HTTP and WebSocket.

Stand-ins (tests/captain_stubs): the WAL wrapper, fleet_bus, command_parser, zmq/msgpack, shm layout,
and a dict-backed UserStore. Everything else is the real code.

Run:  CAPTAIN_SRC=/path/to/patched/tree  pytest tests/test_integration.py
"""
import asyncio, os, sys, types, base64, socket
import pytest, httpx, uvicorn

SRC = os.getenv("CAPTAIN_SRC")
pytestmark = pytest.mark.skipif(not SRC, reason="set CAPTAIN_SRC to the patched Captain tree")
if SRC:
    sys.path[:0] = [os.path.join(os.path.dirname(__file__), "captain_stubs"), SRC]
    import conftest_stubs; conftest_stubs.install()

from fleet_agent.config import Config
from fleet_agent.joystick import PadState
from fleet_agent.security import Guard, COOKIE
from fleet_agent.server import create_app
from fleet_agent.biometrics import BiometricError

# ── dict-backed UserStore: only the methods server.py / users.py are seen to call ──
class FakeUserStore:
    def __init__(self):
        self.users = {}; self.sessions = {}; self._n = 0
        self.add("op1", "pw-op1", "operator"); self.add("root", "pw-root", "admin")
    def add(self, name, pw, role):
        self._n += 1; self.users[name] = types.SimpleNamespace(id=self._n, username=name, password=pw, role=role,
                                                               active=True, created_at_us=0, last_login_us=0)
        return self._n
    async def verify_password(self, name, pw):
        u = self.users.get(name); return u if u and u.password == pw else None
    async def create_session(self, uid):
        tok = f"tok{len(self.sessions)}-{uid}"; self.sessions[tok] = uid; return tok
    async def delete_session(self, tok): self.sessions.pop(tok, None)
    async def get_user_by_session(self, tok):
        uid = self.sessions.get(tok); return next((u for u in self.users.values() if u.id == uid), None)
    async def list_users(self): return list(self.users.values())
    async def get_user(self, uid): return next(u for u in self.users.values() if u.id == uid)
    async def create_user(self, name, pw, role="operator"):
        if name in self.users: raise ValueError("exists")
        return self.add(name, pw, role)

class FakePads:
    """Joystick backend stand-in: the test edits .pads."""
    def __init__(self): self.pads = []
    def poll(self): return [PadState(p.id, p.name, list(p.axes), list(p.buttons), list(p.hats)) for p in self.pads]
    def close(self): pass
def pad(buttons=None, name="Test Pad"): return PadState(1, name, [0.0] * 4, buttons or [False] * 8, [])

class FakeFace:
    kind, model_id, threshold, needed_samples, consistency_floor = "face", "fake-face", 0.9, 3, 0.5
    def embed(self, data):
        if data == b"noface": raise BiometricError("no face found")
        return [float(b) for b in data[:8]]            # the "image" bytes are the embedding
class FakeVoice(FakeFace):
    kind, model_id = "voice", "fake-voice"

def free_port():
    with socket.socket() as s: s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

class Rig:
    pass

@pytest.fixture
async def rig(tmp_path):
    from captain.api import server as captain_server
    from captain.api import command_parser
    from captain.api.fleet_stream import fleet_bus
    from captain.flight_db.db import FlightDB
    command_parser.RECEIVED.clear(); fleet_bus.events.clear()
    r = Rig(); r.bus, r.received = fleet_bus, command_parser.RECEIVED
    r.users = FakeUserStore()
    r.db = FlightDB(tmp_path / "events.db"); await r.db.connect()
    await r.db.save_drone_identity("hw-2975", "2")
    app = captain_server._build_app(user_store=r.users, policy_engine=types.SimpleNamespace(flight_db=r.db))
    port = free_port()
    r.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    r.task = asyncio.create_task(r.server.serve())
    while not r.server.started: await asyncio.sleep(0.02)
    r.url = f"http://127.0.0.1:{port}"
    yield r
    r.server.should_exit = True; await r.task; await r.db.close()

async def make_agent(engines=None):
    r = types.SimpleNamespace()
    r.pads = FakePads(); port = 8781
    r.guard = Guard(port, "test-token")
    engines = engines or {"face": {"engine": FakeFace(), "reason": ""}, "voice": {"engine": FakeVoice(), "reason": ""}}
    r.app, r.agent = create_app(Config(port=port, state_interval_s=0.3), engines=engines,
                                backend_factory=lambda: r.pads, guard=r.guard)
    r.agent.start()
    r.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=r.app), base_url=f"http://127.0.0.1:{port}",
                               headers={"Host": f"127.0.0.1:{port}"}, cookies={COOKIE: "test-token"})
    return r

async def until(fn, timeout=3.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        v = fn()
        if v: return v
        await asyncio.sleep(0.03)
    raise AssertionError("timed out waiting for condition")

async def close_agent(a):
    await a.http.aclose(); await a.agent.stop()

def events(rig, kind): return [e for e in rig.bus.events if e["msg_type"] == f"AGENT_EVENT.{kind}"]

# ── tests ────────────────────────────────────────────────────────────────────

async def test_login_presence_and_joystick_icons(rig):
    a = await make_agent()
    r = await a.http.post("/api/login", json={"url": rig.url, "username": "op1", "password": "pw-op1"})
    assert r.status_code == 200 and r.json()["captain"]["logged_in"]
    await until(lambda: events(rig, "CONNECTED"))                       # agent-link-status goes green
    assert events(rig, "CONNECTED")[0]["fields"]["operator"] == "op1"
    assert not events(rig, "JOYSTICK") or not events(rig, "JOYSTICK")[-1]["fields"]["present"]
    a.pads.pads = [pad()]                                               # plug a joystick in
    await until(lambda: any(e["fields"]["present"] for e in events(rig, "JOYSTICK")))   # joystick-status green
    assert events(rig, "JOYSTICK")[-1]["fields"]["name"] == "Test Pad"
    a.pads.pads = []                                                    # unplug
    await until(lambda: not events(rig, "JOYSTICK")[-1]["fields"]["present"])
    assert (await a.http.post("/api/login", json={"url": rig.url, "username": "op1", "password": "nope"})).status_code == 401
    await a.http.post("/api/logout")
    await until(lambda: events(rig, "DISCONNECTED"))
    await close_agent(a)

async def test_wrong_login_and_bad_url(rig):
    a = await make_agent()
    r = await a.http.post("/api/login", json={"url": "ftp://x", "username": "a", "password": "b"})
    assert r.status_code in (404, 422, 502) and "http" in r.json()["detail"]
    r = await a.http.post("/api/login", json={"url": "http://127.0.0.1:1", "username": "a", "password": "b"})
    assert r.status_code == 502 and "Cannot reach" in r.json()["detail"]
    await close_agent(a)

async def login(a, rig, user="op1", pw="pw-op1"):
    r = await a.http.post("/api/login", json={"url": rig.url, "username": user, "password": pw}); assert r.status_code == 200, r.text

async def test_joystick_commands_flash_icon_and_hold_to_confirm(rig):
    a = await make_agent(); await login(a, rig)
    assert (await a.http.post("/api/target", json={"drone_id": "2"})).status_code == 200
    r = await a.http.put("/api/settings/joystick", json={"value": {"hold_ms": 300, "bindings": {"2": "land", "4": "arm"}}})
    assert r.status_code == 200, r.text
    a.pads.pads = [pad()]; await until(lambda: a.agent.monitor.info["present"])
    p = a.pads.pads[0]
    p.buttons[2] = True                                                  # safe action: fires on press
    await until(lambda: rig.received)
    got = rig.received[-1]
    assert got["cmd"]["command"] == 21 and got["cmd"]["droneId"] == "2" and got["cmd"]["source_hint"] == "joystick"
    assert got["source"] == "agent" and got["operator"] == "op1"
    await until(lambda: events(rig, "COMMAND_SENT"))
    assert events(rig, "COMMAND_SENT")[-1]["fields"] == {"operator": "op1", "label": "Land (2)", "ok": True, "source": "joystick"}
    p.buttons[2] = False; n = len(rig.received)
    p.buttons[4] = True; await asyncio.sleep(0.1); p.buttons[4] = False       # tap: released before hold_ms
    await asyncio.sleep(0.5); assert len(rig.received) == n                     # arm NOT sent
    p.buttons[4] = True                                                         # hold
    await until(lambda: len(rig.received) == n + 1)
    assert rig.received[-1]["cmd"]["command"] == 400 and rig.received[-1]["cmd"]["params"]["param1"] == 1
    await close_agent(a)

async def test_joystick_commands_need_the_hardware(rig):
    a = await make_agent(); await login(a, rig)
    await a.http.post("/api/target", json={"drone_id": "2"})
    from fleet_agent.server import DispatchError
    with pytest.raises(DispatchError, match="No joystick"): await a.agent.execute("land", None, "joystick")
    await close_agent(a)

async def test_captain_rejection_is_reported_and_flashes_red(rig):
    a = await make_agent(); await login(a, rig)
    with pytest.raises(Exception, match="not heard"): await a.agent.execute("land", "bad", "agent")
    await until(lambda: events(rig, "COMMAND_SENT"))
    f = events(rig, "COMMAND_SENT")[-1]["fields"]; assert f["ok"] is False and f["source"] == "agent"
    await close_agent(a)

async def test_voice_gate_wake_word_confirm_and_tts_source(rig, caplog):
    caplog.set_level("INFO")
    a = await make_agent(); await login(a, rig)
    await a.http.post("/api/target", json={"drone_id": "2"})
    say = lambda t: a.http.post("/api/voice/transcript", json={"text": t, "final": True})
    assert (await say("captain land")).json()["status"] == "locked"
    assert (await say("we should land at noon")).json()["status"] == "ignored"
    bad = (await a.http.post("/api/voice/unlock", json={"password": "wrong"})).json()
    assert bad["unlocked"] is False and not rig.received
    ok = (await a.http.post("/api/voice/unlock", json={"password": "pw-op1"})).json()
    assert ok["unlocked"] is True
    assert (await say("land")).json()["status"] == "ignored"                 # no wake word
    res = (await say("Captain, land drone two")).json()
    assert res["status"] == "executed" and res["drone_id"] == "2"
    assert "voice transcript received (final=True): 'Captain, land drone two'" in caplog.text
    assert "voice transcript result: status=executed" in caplog.text
    assert "sending command to Captain via POST /command: Land (2) source=voice" in caplog.text
    assert "Captain accepted command via POST /command: Land (2) source=voice" in caplog.text
    await until(lambda: events(rig, "COMMAND_SENT"))
    assert events(rig, "COMMAND_SENT")[-1]["fields"]["source"] == "voice"    # → tts-status flickers in app.js
    res = (await say("captain takeoff")).json(); assert res["status"] == "needs_confirm"
    n = len(rig.received); assert (await say("confirm")).json()["status"] == "executed"
    assert len(rig.received) == n + 1 and rig.received[-1]["cmd"]["command"] == 22
    assert (await say("captain arm")).json()["status"] == "needs_confirm"
    assert (await a.http.post("/api/voice/confirm", json={"ok": False})).json()["status"] == "cancelled"
    assert (await say("captain land drone 99")).json()["status"] == "rejected"
    await a.http.post("/api/voice/lock")
    assert (await say("captain land")).json()["status"] == "locked"
    await close_agent(a)

async def test_unlock_brute_force_brake(rig):
    a = await make_agent(); await login(a, rig)
    for _ in range(5): await a.http.post("/api/voice/unlock", json={"password": "x"})
    r = await a.http.post("/api/voice/unlock", json={"password": "pw-op1"})
    assert r.status_code == 429
    await close_agent(a)

async def test_settings_persist_in_captain_and_restore_on_a_fresh_agent(rig):
    a = await make_agent(); await login(a, rig)
    await a.http.put("/api/settings/voice", json={"value": {"wake_word": "falcon", "unlock_ttl_s": 120}})
    await a.http.put("/api/settings/joystick", json={"value": {"bindings": {"5": "rtl"}, "deadzone": 0.2}})
    assert (await a.http.put("/api/settings/joystick", json={"value": {"hold_ms": 5}})).status_code == 422
    assert (await a.http.put("/api/settings/camera", json={"value": {"device_id": "cam-A"}})).status_code == 200
    stored = await rig.db.get_agent_settings("op1")
    assert stored["voice"]["value"]["wake_word"] == "falcon" and stored["joystick"]["value"]["bindings"] == {"5": "rtl"}
    await close_agent(a)
    b = await make_agent(); await login(b, rig)                                   # "a different laptop"
    st = (await b.http.get("/api/status")).json()
    assert st["settings"]["voice"]["wake_word"] == "falcon" and st["settings"]["voice"]["unlock_ttl_s"] == 120
    assert st["settings"]["camera"]["device_id"] == "cam-A" and st["settings"]["joystick"]["deadzone"] == 0.2
    assert st["saved_ts"]["voice"] is not None and b.agent.gate.ttl_s == 120
    await close_agent(b)
    c = await make_agent(); await login(c, rig, "root", "pw-root")                # another operator: own settings
    assert (await c.http.get("/api/status")).json()["settings"]["voice"]["wake_word"] == "captain"
    await close_agent(c)

async def test_unlock_factor_changes_cannot_lock_you_out_or_be_bypassed(rig):
    a = await make_agent(); await login(a, rig)
    put = lambda v, **kw: a.http.put("/api/settings/voice", json={"value": {"unlock_factors": v}, **kw})
    # a factor with no enrolled template would make unlocking impossible -> refused up front
    r = await put(["password", "voiceprint"]); assert r.status_code == 422 and "no voice template" in r.json()["detail"]
    await rig.db.put_agent_biometric("op1", "voice", "fake-voice", [[0.1] * 8], "root")
    assert (await put(["password", "voiceprint"])).status_code == 200
    # weakening while locked needs the Captain password; wrong/missing is refused
    assert (await put(["password"])).status_code == 403
    assert (await put(["password"], password="wrong")).status_code == 403
    # ... and reset-to-defaults is not a way around that
    assert (await a.http.post("/api/settings/voice/reset")).status_code == 403
    assert a.agent.settings["voice"]["unlock_factors"] == ["password", "voiceprint"]
    assert (await put([])).status_code == 422
    assert (await put(["password"], password="pw-op1")).status_code == 200
    # an engine that is not installed cannot be required
    b = await make_agent({"face": {"engine": None, "reason": "model file missing"}, "voice": {"engine": FakeVoice(), "reason": ""}})
    await b.http.post("/api/login", json={"url": rig.url, "username": "op1", "password": "pw-op1"})
    await rig.db.put_agent_biometric("op1", "face", "fake-face", [[0.1] * 8], "root")
    r = await b.http.put("/api/settings/voice", json={"value": {"unlock_factors": ["password", "face"]}})
    assert r.status_code == 422 and "model file missing" in r.json()["detail"]
    await close_agent(b); await close_agent(a)

async def test_register_user_with_face_and_voice_then_unlock_by_voiceprint(rig):
    adm = await make_agent(); await login(adm, rig, "root", "pw-root")
    assert (await adm.http.post("/api/register/user", json={"username": "newbie", "password": "pw-new", "role": "operator"})).status_code == 200
    for kind, eng_samples in (("face", [b"\x01\x02\x03\x04\x05\x06\x07\x08", b"\x01\x02\x03\x04\x05\x06\x07\x09", b"\x01\x02\x03\x04\x05\x06\x08\x08"]),
                              ("voice", [b"\x08\x07\x06\x05\x04\x03\x02\x01"] * 3)):
        s = (await adm.http.post("/api/register/start", json={"username": "newbie", "kind": kind})).json()
        assert s["needed"] == 3
        bad = await adm.http.post("/api/register/sample", json={"session_id": s["session_id"], "data_b64": base64.b64encode(b"noface").decode()})
        assert bad.status_code == 422 and "no face" in bad.json()["detail"]
        early = await adm.http.post("/api/register/commit", json={"session_id": s["session_id"]})
        assert early.status_code == 422                                          # not enough samples yet
        for smp in eng_samples:
            r = await adm.http.post("/api/register/sample", json={"session_id": s["session_id"], "data_b64": base64.b64encode(smp).decode()})
            assert r.status_code == 200, r.text
        assert r.json()["ready"] is True
        assert (await adm.http.post("/api/register/commit", json={"session_id": s["session_id"]})).json()["enrolled"]
    stored = await rig.db.get_agent_biometrics("newbie")
    assert set(stored) == {"face", "voice"} and stored["face"]["model"] == "fake-face" and len(stored["face"]["template"]) == 3
    # a different person is swapped in mid-enrolment → rejected
    s = (await adm.http.post("/api/register/start", json={"username": "newbie", "kind": "face"})).json()
    await adm.http.post("/api/register/sample", json={"session_id": s["session_id"], "data_b64": base64.b64encode(b"\x09\x00\x00\x00\x00\x00\x00\x00").decode()})
    r = await adm.http.post("/api/register/sample", json={"session_id": s["session_id"], "data_b64": base64.b64encode(b"\x00\x09\x00\x00\x00\x00\x00\x00").decode()})
    assert r.status_code == 422 and "does not look like" in r.json()["detail"]
    await close_agent(adm)

    # the new user logs in elsewhere: voiceprint + password unlock
    u = await make_agent(); await login(u, rig, "newbie", "pw-new")
    await u.http.put("/api/settings/voice", json={"value": {"unlock_factors": ["password", "voiceprint"]}})
    b64 = lambda b: base64.b64encode(b).decode()
    r = (await u.http.post("/api/voice/unlock", json={"password": "pw-new", "voice_wav_b64": b64(b"\x01\x01\x01\x01\x01\x01\x01\x01")})).json()
    assert r["unlocked"] is False and r["factors"]["voiceprint"]["ok"] is False and r["factors"]["password"]["ok"] is True
    r = (await u.http.post("/api/voice/unlock", json={"password": "pw-new", "voice_wav_b64": b64(b"\x08\x07\x06\x05\x04\x03\x02\x01")})).json()
    assert r["unlocked"] is True and r["gate"]["unlocked_by"] == ["password", "voiceprint"]
    # faceless factor requested but no sample → fails closed
    await u.http.post("/api/voice/lock")
    await u.http.put("/api/settings/voice", json={"value": {"unlock_factors": ["password", "voiceprint", "face"]}})
    r = (await u.http.post("/api/voice/unlock", json={"password": "pw-new", "voice_wav_b64": b64(b"\x08\x07\x06\x05\x04\x03\x02\x01")})).json()
    assert r["unlocked"] is False and r["factors"]["face"]["message"] == "no sample provided"
    # non-admin cannot register
    assert (await u.http.post("/api/register/user", json={"username": "x", "password": "y"})).status_code == 403
    assert (await u.http.post("/api/register/start", json={"username": "newbie", "kind": "face"})).status_code == 403
    await close_agent(u)

async def test_register_when_engine_missing_says_why(rig):
    a = await make_agent({"face": {"engine": None, "reason": "model file missing: x.onnx"}, "voice": {"engine": None, "reason": "install resemblyzer"}})
    await login(a, rig, "root", "pw-root")
    r = await a.http.post("/api/register/start", json={"username": "op1", "kind": "face"})
    assert r.status_code == 409 and "model file missing" in r.json()["detail"]
    assert (await a.http.get("/api/status")).json()["engines"]["face"] == {"available": False, "reason": "model file missing: x.onnx", "needed_samples": None}
    await close_agent(a)

async def test_session_expiry_resets_agent(rig):
    a = await make_agent(); await login(a, rig)
    await a.http.post("/api/voice/unlock", json={"password": "pw-op1"}); assert a.agent.gate.unlocked
    rig.users.sessions.clear()                                                   # Captain forgets every session
    r = await a.http.get("/api/drones"); assert r.status_code == 401
    await until(lambda: not a.agent.link.logged_in)
    assert not a.agent.gate.unlocked                                             # voice relocked
    assert (await a.http.get("/api/status")).json()["captain"]["logged_in"] is False
    await close_agent(a)

async def test_local_guard(rig):
    a = await make_agent()
    c = lambda **kw: httpx.AsyncClient(transport=httpx.ASGITransport(app=a.app), base_url="http://127.0.0.1:8781", **kw)
    async with c(headers={"Host": "127.0.0.1:8781"}) as anon:                    # no cookie
        assert (await anon.get("/api/status")).status_code == 403
        r = await anon.get("/?k=wrong"); assert r.status_code == 403
        r = await anon.get("/?k=test-token"); assert r.status_code == 303 and COOKIE in r.headers["set-cookie"]
        assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    async with c(headers={"Host": "evil.example:8781"}, cookies={COOKIE: "test-token"}) as rebind:
        assert (await rebind.get("/api/status")).status_code == 403
    async with c(headers={"Host": "127.0.0.1:8781", "Origin": "http://evil.example"}, cookies={COOKIE: "test-token"}) as xs:
        assert (await xs.post("/api/voice/lock")).status_code == 403
    assert (await a.http.get("/api/status")).status_code == 200
    await close_agent(a)
