"""TEST STAND-IN for shared.db.sqlite_wal.WalConnection (not uploaded).
Implements only what flight_db/db.py is observed to use: connect(schema_sql=),
.conn (aiosqlite-style), .write_lock, close()."""
import asyncio, aiosqlite
class WalConnection:
    def __init__(self, path): self._path = str(path); self.conn = None; self.write_lock = asyncio.Lock()
    async def connect(self, schema_sql=""):
        self.conn = await aiosqlite.connect(self._path)
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(schema_sql); await self.conn.commit()
    async def close(self):
        if self.conn: await self.conn.close()
