"""TEST STAND-IN for captain.api.fleet_stream (not uploaded): records publish_event calls."""
class _Bus:
    def __init__(self): self.events = []
    def publish_event(self, **kw): self.events.append(kw)
fleet_bus = _Bus()
def jsonable(x): return x
