import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Team:
    guild_id: int
    role_id: int
    manager_id: int


@dataclass(frozen=True)
class Offer:
    id: int
    guild_id: int
    team_role_id: int
    manager_id: int
    player_id: int
    message_id: int
    status: str


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()

    async def migrate(self) -> None:
        async with self.lock:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS teams (
                    guild_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    manager_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, role_id)
                );
                CREATE TABLE IF NOT EXISTS offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    team_role_id INTEGER NOT NULL,
                    manager_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TEXT
                );
            """)
            self.connection.commit()

    async def upsert_team(self, team: Team) -> None:
        async with self.lock:
            self.connection.execute(
                "INSERT INTO teams(guild_id, role_id, manager_id) VALUES(?,?,?) "
                "ON CONFLICT(guild_id, role_id) DO UPDATE SET manager_id=excluded.manager_id",
                (team.guild_id, team.role_id, team.manager_id),
            )
            self.connection.commit()

    async def get_team(self, guild_id: int, role_id: int) -> Team | None:
        async with self.lock:
            row = self.connection.execute(
                "SELECT guild_id, role_id, manager_id FROM teams WHERE guild_id=? AND role_id=?",
                (guild_id, role_id),
            ).fetchone()
        return Team(**dict(row)) if row else None

    async def create_offer(self, guild_id: int, role_id: int, manager_id: int, player_id: int) -> int:
        async with self.lock:
            cursor = self.connection.execute(
                "INSERT INTO offers(guild_id, team_role_id, manager_id, player_id) VALUES(?,?,?,?)",
                (guild_id, role_id, manager_id, player_id),
            )
            self.connection.commit()
            return int(cursor.lastrowid)

    async def set_offer_message(self, offer_id: int, message_id: int) -> None:
        async with self.lock:
            self.connection.execute("UPDATE offers SET message_id=? WHERE id=?", (message_id, offer_id))
            self.connection.commit()

    async def pending_offers(self) -> list[Offer]:
        async with self.lock:
            rows = self.connection.execute(
                "SELECT id,guild_id,team_role_id,manager_id,player_id,message_id,status "
                "FROM offers WHERE status='pending' AND message_id != 0"
            ).fetchall()
        return [Offer(**dict(row)) for row in rows]

    async def get_offer(self, offer_id: int) -> Offer | None:
        async with self.lock:
            row = self.connection.execute(
                "SELECT id,guild_id,team_role_id,manager_id,player_id,message_id,status FROM offers WHERE id=?",
                (offer_id,),
            ).fetchone()
        return Offer(**dict(row)) if row else None

    async def resolve_offer(self, offer_id: int, status: str) -> bool:
        async with self.lock:
            cursor = self.connection.execute(
                "UPDATE offers SET status=?, resolved_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                (status, offer_id),
            )
            self.connection.commit()
            return cursor.rowcount == 1

