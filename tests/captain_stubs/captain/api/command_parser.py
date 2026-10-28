"""TEST STAND-IN for captain.api.command_parser (the real one imports policy/IPC
packages that are not part of this project). Same public names server.py imports."""
class CommandError(Exception): pass
class CommandContext:
    def __init__(self, **kw): self.__dict__.update(kw)
RECEIVED = []
async def parse_and_dispatch(cmd, ctx):
    if cmd.get("droneId") == "bad":
        raise CommandError("drone 'bad' not heard on the mesh")
    RECEIVED.append({"cmd": cmd, "source": ctx.source, "operator": ctx.operator, "role": ctx.operator_role})
    return {"published": True}
