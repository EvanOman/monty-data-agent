import uuid
from datetime import UTC, datetime

import aiosqlite

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New conversation',
    mode TEXT NOT NULL DEFAULT 'standard',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    message_id TEXT REFERENCES messages(id),
    code TEXT NOT NULL,
    monty_state BLOB,
    result_json TEXT,
    result_type TEXT,
    error TEXT,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


class SQLiteStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def _connect(self) -> aiosqlite.Connection:
        """Open a fresh connection and ensure schema exists."""
        db = await aiosqlite.connect(self._path)
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA_SQL)
        await db.commit()
        return db

    async def _get_db(self) -> aiosqlite.Connection:
        """Return a healthy connection, reconnecting if needed."""
        if self._db is not None:
            try:
                await self._db.execute("SELECT 1")
                return self._db
            except Exception:
                try:
                    await self._db.close()
                except Exception:
                    pass
                self._db = None
        self._db = await self._connect()
        return self._db

    async def initialize(self) -> None:
        self._db = await self._connect()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # --- Conversations ---

    async def create_conversation(
        self, title: str = "New conversation", mode: str = "standard"
    ) -> dict:
        db = await self._get_db()
        cid = _uuid()
        now = _now()
        await db.execute(
            "INSERT INTO conversations (id, title, mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (cid, title, mode, now, now),
        )
        await db.commit()
        return {"id": cid, "title": title, "mode": mode, "created_at": now, "updated_at": now}

    async def list_conversations(self) -> list[dict]:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT id, title, mode, created_at, updated_at FROM conversations ORDER BY updated_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_conversation(self, conversation_id: str) -> dict | None:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT id, title, mode, created_at, updated_at FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_conversation_title(self, conversation_id: str, title: str) -> None:
        db = await self._get_db()
        await db.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, _now(), conversation_id),
        )
        await db.commit()

    async def touch_conversation(self, conversation_id: str) -> None:
        db = await self._get_db()
        await db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (_now(), conversation_id),
        )
        await db.commit()

    # --- Messages ---

    async def add_message(self, conversation_id: str, role: str, content: str) -> dict:
        db = await self._get_db()
        mid = _uuid()
        now = _now()
        await db.execute(
            "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (mid, conversation_id, role, content, now),
        )
        await db.commit()
        await self.touch_conversation(conversation_id)
        return {
            "id": mid,
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "created_at": now,
        }

    async def get_messages(self, conversation_id: str) -> list[dict]:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT id, conversation_id, role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- Artifacts ---

    async def save_artifact(
        self,
        conversation_id: str,
        message_id: str | None,
        code: str,
        monty_state: bytes | None = None,
        result_json: str | None = None,
        result_type: str | None = None,
        error: str | None = None,
    ) -> dict:
        db = await self._get_db()
        aid = _uuid()
        now = _now()
        await db.execute(
            """INSERT INTO artifacts
               (id, conversation_id, message_id, code, monty_state, result_json, result_type, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                aid,
                conversation_id,
                message_id,
                code,
                monty_state,
                result_json,
                result_type,
                error,
                now,
            ),
        )
        await db.commit()
        return {
            "id": aid,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "code": code,
            "result_json": result_json,
            "result_type": result_type,
            "error": error,
            "created_at": now,
        }

    async def get_artifact(self, artifact_id: str) -> dict | None:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT id, conversation_id, message_id, code, monty_state, result_json, result_type, error, created_at FROM artifacts WHERE id = ?",
            (artifact_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_artifacts_for_conversation(self, conversation_id: str) -> list[dict]:
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT id, conversation_id, message_id, code, result_json, result_type, error, created_at FROM artifacts WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
