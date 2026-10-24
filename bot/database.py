import sqlite3
import json
from pathlib import Path

AUTO_MOD_DEFAULTS = {
    "anti_spam": False,
    "anti_flood": False,
    "anti_mention": False,
    "anti_link": False,
    "blocked_words": False,
    "anti_repeat": False,
    "anti_raid": False,
}


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    log_channel_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS moderator_roles (
                    guild_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, role_id)
                );
                CREATE TABLE IF NOT EXISTS warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS auto_mod_settings (
                    guild_id INTEGER PRIMARY KEY,
                    config TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS blocked_words (
                    guild_id INTEGER NOT NULL,
                    word TEXT NOT NULL,
                    PRIMARY KEY (guild_id, word)
                );
                CREATE TABLE IF NOT EXISTS moderation_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    details TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS channel_locks (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    send_messages INTEGER,
                    PRIMARY KEY (guild_id, channel_id)
                );
                CREATE TABLE IF NOT EXISTS stream_notifications (
                    guild_id INTEGER NOT NULL,
                    streamer_login TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    last_stream_id TEXT,
                    PRIMARY KEY (guild_id, streamer_login)
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(guild_settings)")
            }
            if "muted_role_id" not in columns:
                connection.execute(
                    "ALTER TABLE guild_settings ADD COLUMN muted_role_id INTEGER"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        return connection

    def get_log_channel_id(self, guild_id: int) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT log_channel_id FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return row["log_channel_id"] if row else None

    def set_log_channel_id(self, guild_id: int, channel_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO guild_settings (guild_id, log_channel_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id
                """,
                (guild_id, channel_id),
            )

    def add_moderator_role(self, guild_id: int, role_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO moderator_roles (guild_id, role_id) VALUES (?, ?)",
                (guild_id, role_id),
            )

    def remove_moderator_role(self, guild_id: int, role_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM moderator_roles WHERE guild_id = ? AND role_id = ?",
                (guild_id, role_id),
            )
        return cursor.rowcount > 0

    def has_moderator_role(self, guild_id: int, role_ids: set[int]) -> bool:
        if not role_ids:
            return False
        placeholders = ",".join("?" for _ in role_ids)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT 1 FROM moderator_roles WHERE guild_id = ? "
                f"AND role_id IN ({placeholders}) LIMIT 1",
                (guild_id, *role_ids),
            ).fetchone()
        return row is not None

    def add_warning(
        self, guild_id: int, user_id: int, moderator_id: int, reason: str
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO warnings (guild_id, user_id, moderator_id, reason)
                VALUES (?, ?, ?, ?)
                """,
                (guild_id, user_id, moderator_id, reason),
            )
        return int(cursor.lastrowid)

    def get_warnings(self, guild_id: int, user_id: int) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT id, moderator_id, reason, created_at
                FROM warnings WHERE guild_id = ? AND user_id = ?
                ORDER BY id DESC LIMIT 25
                """,
                (guild_id, user_id),
            ).fetchall()

    def count_warnings(self, guild_id: int, user_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM warnings WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
        return int(row["total"])

    def clear_warnings(self, guild_id: int, user_id: int) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
        return cursor.rowcount

    def add_case(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        action: str,
        reason: str,
        details: str | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO moderation_cases
                    (guild_id, user_id, moderator_id, action, reason, details)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, moderator_id, action, reason, details),
            )
        return int(cursor.lastrowid)

    def get_case(self, guild_id: int, case_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT id, user_id, moderator_id, action, reason, details, created_at
                FROM moderation_cases WHERE guild_id = ? AND id = ?
                """,
                (guild_id, case_id),
            ).fetchone()

    def get_auto_mod_settings(self, guild_id: int) -> dict[str, bool]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT config FROM auto_mod_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        config = AUTO_MOD_DEFAULTS.copy()
        if row:
            saved = json.loads(row["config"])
            config.update(
                {key: bool(value) for key, value in saved.items() if key in config}
            )
        return config

    def set_auto_mod_setting(self, guild_id: int, setting: str, enabled: bool) -> None:
        if setting not in AUTO_MOD_DEFAULTS:
            raise ValueError(f"Configuração de automod desconhecida: {setting}")
        config = self.get_auto_mod_settings(guild_id)
        config[setting] = enabled
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auto_mod_settings (guild_id, config)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET config = excluded.config
                """,
                (guild_id, json.dumps(config)),
            )

    def add_blocked_word(self, guild_id: int, word: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO blocked_words (guild_id, word) VALUES (?, ?)",
                (guild_id, word.casefold()),
            )
        return cursor.rowcount > 0

    def remove_blocked_word(self, guild_id: int, word: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM blocked_words WHERE guild_id = ? AND word = ?",
                (guild_id, word.casefold()),
            )
        return cursor.rowcount > 0

    def get_blocked_words(self, guild_id: int) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT word FROM blocked_words WHERE guild_id = ? ORDER BY word",
                (guild_id,),
            ).fetchall()
        return [row["word"] for row in rows]

    def set_muted_role_id(self, guild_id: int, role_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO guild_settings (guild_id, muted_role_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET muted_role_id = excluded.muted_role_id
                """,
                (guild_id, role_id),
            )

    def get_muted_role_id(self, guild_id: int) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT muted_role_id FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return row["muted_role_id"] if row else None

    def save_channel_lock(
        self, guild_id: int, channel_id: int, send_messages: bool | None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO channel_locks
                    (guild_id, channel_id, send_messages)
                VALUES (?, ?, ?)
                """,
                (guild_id, channel_id, None if send_messages is None else int(send_messages)),
            )

    def get_channel_lock(self, guild_id: int, channel_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT send_messages FROM channel_locks
                WHERE guild_id = ? AND channel_id = ?
                """,
                (guild_id, channel_id),
            ).fetchone()

    def get_channel_locks(self, guild_id: int) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                "SELECT channel_id, send_messages FROM channel_locks WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()

    def remove_channel_lock(self, guild_id: int, channel_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM channel_locks WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )

    def add_stream_notification(
        self, guild_id: int, streamer_login: str, channel_id: int
    ) -> bool:
        existing = self.get_stream_notification(guild_id, streamer_login)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO stream_notifications
                    (guild_id, streamer_login, channel_id)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, streamer_login)
                DO UPDATE SET channel_id = excluded.channel_id
                """,
                (guild_id, streamer_login, channel_id),
            )
        return existing is not None

    def get_stream_notification(
        self, guild_id: int, streamer_login: str
    ) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT streamer_login, channel_id, last_stream_id
                FROM stream_notifications
                WHERE guild_id = ? AND streamer_login = ?
                """,
                (guild_id, streamer_login),
            ).fetchone()

    def get_stream_notifications(self, guild_id: int) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT streamer_login, channel_id, last_stream_id
                FROM stream_notifications
                WHERE guild_id = ?
                ORDER BY streamer_login
                """,
                (guild_id,),
            ).fetchall()

    def get_all_stream_notifications(self) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT guild_id, streamer_login, channel_id, last_stream_id
                FROM stream_notifications
                ORDER BY guild_id, streamer_login
                """
            ).fetchall()

    def remove_stream_notification(
        self, guild_id: int, streamer_login: str
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM stream_notifications
                WHERE guild_id = ? AND streamer_login = ?
                """,
                (guild_id, streamer_login),
            )
        return cursor.rowcount > 0

    def set_stream_notification_last_id(
        self,
        guild_id: int,
        streamer_login: str,
        stream_id: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE stream_notifications
                SET last_stream_id = ?
                WHERE guild_id = ? AND streamer_login = ?
                """,
                (stream_id, guild_id, streamer_login),
            )
