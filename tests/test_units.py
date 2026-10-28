import ast, asyncio, math, os, re, threading
import pytest
from fleet_agent import commands, settings_schema as schema
from fleet_agent.biometrics import (BiometricError, Enrollment, EnrollmentStore, best_score, cosine,
                                    l2_normalize, verify)
from fleet_agent.joystick import JoystickMapper, JoystickMonitor, PadState, PygameBackend
from fleet_agent.security import COOKIE, Guard
from fleet_agent.server import AttemptLimiter
from fleet_agent.voice import VoiceController, VoiceGate, normalize


# ── commands ────────────────────────────────────────────────────────────────
def test_command_shape_matches_menus_js_conventions():
    c = commands.build_action("takeoff", "Alpha 7", takeoff_alt_m=30, source_hint="voice")
    assert c["type"] == "mavlink" and c["class"] == "COMMAND_LONG" and c["command"] == 22
    assert c["droneId"] == "Alpha 7" and c["sys_id"] == 7 and c["label"] == "Takeoff (Alpha 7)"
    assert c["params"] == {"param1": 0, "param2": 0, "param3": 0, "param4": 0, "param5": 0, "param6": 0, "param7": 30.0}
    assert c["source"] == "agent" and c["source_hint"] == "voice"
    assert commands.drone_sys_id("alpha") == 1 and commands.drone_sys_id("2975") == 2975
    assert commands.build_action("arm", "2")["params"]["param1"] == 1
    assert commands.build_action("disarm", "2")["params"]["param1"] == 0
    with pytest.raises(KeyError): commands.build_action("selfdestruct", "2")
    with pytest.raises(ValueError): commands.build_action("land", "2", source_hint="telepathy")

@pytest.mark.skipif(not os.getenv("CAPTAIN_SRC"), reason="set CAPTAIN_SRC to compare against command_parser.py")
def test_every_action_is_in_captains_allowlist():
    src = open(os.path.join(os.environ["CAPTAIN_SRC"], "captain/api/command_parser.py")).read()
    tree = ast.parse(src)
    allowed = next(ast.literal_eval(n.value.args[0]) for n in ast.walk(tree)
                   if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "ALLOWED_MAVLINK_COMMANDS")
    assert {a.command for a in commands.ACTIONS.values()} <= allowed

def test_only_motor_affecting_actions_need_confirmation():
    assert {n for n, a in commands.ACTIONS.items() if a.confirm} == {"arm", "disarm", "takeoff"}


# ── settings schema ─────────────────────────────────────────────────────────
def test_defaults_validate_and_nothing_that_starts_motors_is_bound():
    for scope, d in schema.DEFAULTS.items():
        assert schema.clean(scope, d) == d
    bound = set(schema.DEFAULTS["joystick"]["bindings"].values())
    assert not bound & {"arm", "disarm", "takeoff"}

@pytest.mark.parametrize("scope,bad", [
    ("joystick", {"hold_ms": 100}), ("joystick", {"hold_ms": True}), ("joystick", {"bindings": {"x": "land"}}),
    ("joystick", {"bindings": {"1": "format_disk"}}), ("joystick", {"bindings": {"99": "land"}}),
    ("joystick", {"takeoff_alt_m": 500}), ("voice", {"unlock_factors": []}), ("voice", {"unlock_factors": ["iris"]}),
    ("voice", {"unlock_factors": ["face", "face"]}), ("voice", {"unlock_ttl_s": 5}), ("voice", {"wake_word": "a;b"}),
    ("voice", {"language": "en US"}), ("voice", {"stt_engine": "../x"}), ("voice", {"require_wake_word": "yes"}),
    ("camera", {"device_id": "x" * 300}), ("nope", {}), ("voice", [1]),
])
def test_bad_settings_are_rejected(scope, bad):
    with pytest.raises(schema.SettingsError): schema.clean(scope, bad)

def test_unknown_keys_dropped_and_bad_stored_blob_falls_back_to_defaults():
    assert schema.clean("camera", {"device_id": "c", "evil": 1}) == {"device_id": "c"}
    assert schema.merged("voice", {"unlock_ttl_s": "soon"}) == schema.DEFAULTS["voice"]
    m = schema.merged("voice", {"wake_word": "Falcon"}); assert m["wake_word"] == "falcon" and m["unlock_ttl_s"] == 300
    m["unlock_factors"].append("face"); assert schema.DEFAULTS["voice"]["unlock_factors"] == ["password"]   # no aliasing


# ── security guard ──────────────────────────────────────────────────────────
def test_guard():
    g = Guard(8780, "tok"); ok = dict(host="127.0.0.1:8780", origin=None, cookie_token="tok")
    assert g.check(**ok) is None and g.check(**{**ok, "host": "localhost:8780"}) is None
    assert g.check(**{**ok, "host": "evil.com:8780"}) == "bad host"                     # DNS rebinding
    assert g.check(**{**ok, "host": "127.0.0.1:9999"}) == "bad host"
    assert g.check(**{**ok, "origin": "http://evil.com"}) == "bad origin"               # cross-site page
    assert g.check(**{**ok, "origin": "http://127.0.0.1:8780"}) is None
    assert g.check(**{**ok, "cookie_token": None}) == "missing or wrong launch token"
    assert g.check(**{**ok, "cookie_token": "nope"}) == "missing or wrong launch token"
    assert g.launch_url() == "http://127.0.0.1:8780/?k=tok" and COOKIE


# ── biometrics math ─────────────────────────────────────────────────────────
class Emb:
    kind, model_id, threshold, needed_samples, consistency_floor = "face", "m1", 0.9, 3, 0.5
    def embed(self, d): return list(d)

def test_cosine_and_verify():
    assert math.isclose(sum(x * x for x in l2_normalize([3, 4])), 1.0)
    assert math.isclose(cosine(l2_normalize([1, 0]), l2_normalize([1, 1])), math.sqrt(0.5))
    tpl = {"model": "m1", "template": [l2_normalize([1, 0, 0, 0])]}
    assert verify(Emb(), [1, 0.05, 0, 0], tpl)[0] is True
    ok, score, msg = verify(Emb(), [0, 1, 0, 0], tpl); assert not ok and score == 0 and "does not match" in msg
    assert verify(Emb(), [1, 0], None)[0] is False                                        # nothing enrolled
    assert "re-enrol" in verify(Emb(), [1], {"model": "other", "template": [[1.0]]})[2]   # model mismatch
    with pytest.raises(BiometricError): best_score([1, 0], [[1, 0, 0]])                   # size mismatch
    with pytest.raises(BiometricError): l2_normalize([0, 0])

def test_enrollment_collects_and_rejects_a_different_person():
    e = Enrollment("u", "face", "root", needed=2, consistency_floor=0.5)
    e.add([1, 0, 0]); assert not e.ready
    with pytest.raises(BiometricError, match="does not look like"): e.add([0, 1, 0])
    with pytest.raises(BiometricError, match="need 2"): e.template()
    e.add([0.9, 0.1, 0]); assert e.ready and len(e.template()) == 2
    st = EnrollmentStore(); s = st.start(username="u", kind="face", admin="root", needed=1, consistency_floor=.5)
    assert st.get(s.session_id, "root") is s
    with pytest.raises(BiometricError): st.get(s.session_id, "someone-else")             # not yours
    st.drop(s.session_id)
    with pytest.raises(BiometricError): st.get(s.session_id, "root")


# ── voice ───────────────────────────────────────────────────────────────────
class Clock:
    t = 1000.0
    def __call__(self): return self.t

def make_voice(clock=None, **cfg):
    clock = clock or Clock(); sent = []
    settings = {"require_wake_word": True, "wake_word": "captain", "confirm_window_s": 8, **cfg}
    async def execute(a, d, s): sent.append((a, d, s)); return {"detail": "ok"}
    gate = VoiceGate(300, clock); gate.unlock(["password"])
    vc = VoiceController(gate, settings=lambda: settings, known_drones=lambda: ["2", "Alpha"],
                         default_drone=lambda: "2", execute=execute, clock=clock)
    return vc, gate, sent, clock

def run(coro): return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)

@pytest.mark.parametrize("text,action,drone", [
    ("Captain, land", "land", "2"), ("captain land drone two", "land", "2"), ("Capitan: return home", "rtl", "2"),
    ("hey captain hold position", "hold", "2"), ("captain return to launch drone alpha", "rtl", "Alpha"),
    ("captain land now please", "land", "2"), ("CAPTAIN pause mission", "pause", "2"), ("captain resume", "resume", "2"),
])
def test_safe_commands_run_immediately(text, action, drone):
    vc, gate, sent, _ = make_voice()
    r = run(vc.handle(text)); assert r.status == "executed", r
    assert sent == [(action, drone, "voice")]

def test_chatter_and_near_misses_do_nothing():
    vc, gate, sent, _ = make_voice()
    for t in ["land", "we will land at noon", "captain please do not land the plane tomorrow", "captain", "captain launch the report",
              "the captain said land", "", "   ", "captain land drone", "return to launch", "captain return 2 launch"]:
        r = run(vc.handle(t))
        assert r.status in ("ignored", "rejected"), (t, r)
    assert sent == []
    assert run(vc.handle("captain land", final=False)).status == "ignored"             # interim transcript

def test_number_words_only_count_after_the_word_drone():
    vc, gate, sent, _ = make_voice()
    run(vc.handle("captain return to launch"))                                         # "to" must stay "to"
    assert sent == [("rtl", "2", "voice")]
    assert run(vc.handle("captain land drone 9")).status == "rejected" and len(sent) == 1       # unknown drone

def test_locked_gate_blocks_and_ttl_relocks():
    vc, gate, sent, clock = make_voice()
    clock.t += 299; assert run(vc.handle("captain land")).status == "executed"          # touch() slides the window
    clock.t += 299; assert gate.unlocked and run(vc.handle("captain land")).status == "executed"
    clock.t += 301; assert not gate.unlocked
    r = run(vc.handle("captain land")); assert r.status == "locked" and len(sent) == 2
    assert run(vc.handle("some chatter")).status == "ignored"                           # chatter not reported as locked
    gate.unlock(["password"]); gate.lock(); assert run(vc.handle("captain land")).status == "locked"

def test_motor_commands_need_confirmation_and_it_expires():
    vc, gate, sent, clock = make_voice()
    r = run(vc.handle("captain takeoff")); assert r.status == "needs_confirm" and sent == []
    assert vc.pending["action"] == "takeoff"
    assert run(vc.handle("confirm")).status == "executed" and sent == [("takeoff", "2", "voice")]
    assert run(vc.handle("confirm")).status == "ignored"                                # nothing pending any more
    run(vc.handle("captain arm")); clock.t += 9
    assert run(vc.handle("confirm")).status == "expired" and len(sent) == 1             # window passed
    run(vc.handle("captain arm")); assert run(vc.handle("cancel")).status == "cancelled" and len(sent) == 1
    run(vc.handle("captain arm")); run(vc.handle("captain land"))                       # a new command replaces the pending one
    assert vc.pending is None and sent[-1][0] == "land"
    run(vc.handle("captain disarm")); gate.lock()
    assert run(vc.confirm(True)).status == "locked" and len(sent) == 2                  # gate closed while waiting

def test_wake_word_is_configurable_and_can_be_switched_off():
    vc, _, sent, _ = make_voice(wake_word="falcon")
    assert run(vc.handle("captain land")).status == "ignored" and run(vc.handle("falcon land")).status == "executed"
    vc, _, sent, _ = make_voice(require_wake_word=False)
    assert run(vc.handle("land")).status == "executed"

def test_dispatch_failure_is_reported_not_raised():
    vc, gate, sent, clock = make_voice()
    async def boom(a, d, s): raise RuntimeError("Captain refused")
    vc._execute = boom
    r = run(vc.handle("captain land")); assert r.status == "rejected" and "Captain refused" in r.detail

def test_normalize():
    assert normalize("  Captain,  LAND!! ") == "captain land"


# ── attempt limiter ─────────────────────────────────────────────────────────
def test_attempt_limiter():
    c = Clock(); lim = AttemptLimiter(3, 100, 60, c)
    lim.fail(); lim.fail(); assert lim.blocked_for() == 0
    lim.fail(); assert 59 <= lim.blocked_for() <= 60
    c.t += 61; assert lim.blocked_for() == 0
    lim.fail(); c.t += 200; lim.fail(); lim.fail(); assert lim.blocked_for() == 0       # old failures age out
    lim.reset(); assert lim.blocked_for() == 0


# ── joystick mapper + monitor ───────────────────────────────────────────────
async def _mapper(present=True, hold_ms=120, bindings=None):
    sent, events = [], []
    async def dispatch(a): sent.append(a)
    m = JoystickMapper(settings=lambda: {"hold_ms": hold_ms, "bindings": bindings or {"0": "land", "1": "arm"}},
                       present=lambda: present, dispatch=dispatch, emit=events.append)
    return m, sent, events

async def test_safe_button_fires_on_press_with_repeat_guard():
    m, sent, ev = await _mapper()
    await m.on_edge(1, 0, True); await m.on_edge(1, 0, False); await m.on_edge(1, 0, True)
    assert sent == ["land"]                                                            # second press inside the guard window
    await m.on_edge(1, 5, True); assert sent == ["land"]                               # unbound button

async def test_motor_button_needs_the_full_hold():
    m, sent, ev = await _mapper()
    await m.on_edge(1, 1, True); await asyncio.sleep(0.05); await m.on_edge(1, 1, False)
    await asyncio.sleep(0.2); assert sent == []                                        # released early
    assert {"type": "joystick.hold", "index": 1, "state": "cancelled"} in ev
    await m.on_edge(1, 1, True); await asyncio.sleep(0.25); assert sent == ["arm"]
    await m.on_edge(1, 1, False)

async def test_unplugging_during_a_hold_cancels_it():
    present = {"v": True}; sent = []
    async def dispatch(a): sent.append(a)
    m = JoystickMapper(settings=lambda: {"hold_ms": 100, "bindings": {"1": "arm"}}, present=lambda: present["v"],
                       dispatch=dispatch, emit=lambda e: None)
    await m.on_edge(1, 1, True); present["v"] = False; await asyncio.sleep(0.2)
    assert sent == []

async def test_no_hardware_no_commands_and_dispatch_errors_are_contained():
    m, sent, ev = await _mapper(present=False); await m.on_edge(1, 0, True); assert sent == []
    async def boom(a): raise RuntimeError("no target")
    m2 = JoystickMapper(settings=lambda: {"hold_ms": 300, "bindings": {"0": "land"}}, present=lambda: True, dispatch=boom, emit=ev.append)
    await m2.on_edge(1, 0, True)
    assert any(e["type"] == "joystick.error" and e["detail"] == "no target" for e in ev)

class FakePads:
    def __init__(self): self.pads = []
    def poll(self): return [PadState(p.id, p.name, list(p.axes), list(p.buttons)) for p in self.pads]
    def close(self): self.closed = True

async def test_monitor_reports_hotplug_and_button_edges():
    loop = asyncio.get_running_loop(); pres, edges = [], []
    async def on_edge(pid, i, down): edges.append((pid, i, down))
    fake = FakePads(); mon = JoystickMonitor(lambda: fake, loop, on_presence=pres.append, on_edge=on_edge, poll_hz=200)
    mon.start(); await asyncio.sleep(0.05); assert mon.info == {"present": False, "name": "", "count": 0}
    fake.pads = [PadState(7, "Xbox Controller", [0.0] * 4, [False] * 4)]; await asyncio.sleep(0.1)
    assert mon.info == {"present": True, "name": "Xbox Controller", "count": 1} and pres[-1]["present"] is True
    fake.pads[0].buttons[2] = True; await asyncio.sleep(0.1); fake.pads[0].buttons[2] = False; await asyncio.sleep(0.1)
    assert edges == [(7, 2, True), (7, 2, False)]
    fake.pads = []; await asyncio.sleep(0.1); assert pres[-1]["present"] is False
    await asyncio.get_running_loop().run_in_executor(None, mon.stop); assert fake.closed

async def test_monitor_survives_a_missing_backend():
    loop = asyncio.get_running_loop()
    def broken(): raise ImportError("No module named 'pygame'")
    mon = JoystickMonitor(broken, loop, on_presence=lambda i: None, on_edge=lambda *a: asyncio.sleep(0))
    mon.start(); await asyncio.sleep(0.1)
    assert mon.info["present"] is False and "pygame" in mon.backend_error

def test_pygame_backend_starts_headless_with_no_hardware():
    """Smoke test only: proves SDL initialises with no window and reports zero pads here.
    Real hot-plug, axis ranges and button numbering are NOT verified without a physical controller."""
    pytest.importorskip("pygame"); out = {}
    def go(): b = PygameBackend(); out["pads"] = b.poll(); b.close()
    t = threading.Thread(target=go); t.start(); t.join(10)
    assert out == {"pads": []}
