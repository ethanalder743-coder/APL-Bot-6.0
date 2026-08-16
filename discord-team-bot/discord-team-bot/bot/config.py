import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def required_int(name: str) -> int:
    value = os.getenv(name, "").strip()
    if not value.isdigit():
        raise RuntimeError(f"{name} must be set to a Discord numeric ID.")
    return int(value)


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    manager_role_id: int
    owner_ids: frozenset[int]
    log_level: str
    auto_migrate: bool
    backup_dir: Path
    database_path: Path = Path("data/bot.db")

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError("DISCORD_TOKEN is missing.")
        owners = frozenset(
            int(item.strip()) for item in os.getenv("OWNER_IDS", "").split(",")
            if item.strip().isdigit()
        )
        return cls(
            token=token,
            guild_id=required_int("GUILD_ID"),
            manager_role_id=required_int("MANAGER_ROLE_ID"),
            owner_ids=owners,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            auto_migrate=os.getenv("AUTO_MIGRATE", "true").lower() in {"1", "true", "yes"},
            backup_dir=Path(os.getenv("BACKUP_DIR", "backups")),
        )

