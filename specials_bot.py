#!/usr/bin/env python3
"""
PlateRoulette — a shared Telegram bot for tracking restaurant daily specials.

Specials live in a *household*. Anyone you invite sees and edits the same board.
Each member keeps their own digest time and timezone, so you can get the morning
list at 08:30 and someone else at 10:00 off the same data.

SETUP
  python3 -m venv .venv && source .venv/bin/activate
  pip install "python-telegram-bot[job-queue]>=21.6"
  export TELEGRAM_BOT_TOKEN="123456:ABC..."     # from @BotFather
  python specials_bot.py

  Optional:
    export SPECIALS_DB=/path/to/specials.db     # defaults to ./specials.db
    export SPECIALS_TZ=America/Toronto          # default tz for new members

SHARING
  /invite   → a join link and a 6-character code
  /start join-XXXXXX  (or /join XXXXXX) → joins that household
  /household → who's in, and how to leave

  On joining, if you already had specials of your own, the bot offers to bring
  them across and merges same-named restaurants rather than duplicating them.

QUICK ADD SYNTAX
  restaurant | item | price | days | tags

    Otto's Pizza | 2 slices + pop | 9.50 | mon,wed | lunch,cheap
    Sushi Ito | Chirashi bowl | 18 | 2026-09-22

  Only the first two fields are required. Paste many lines at once.
  Days: mon…sun | weekdays | weekends | daily | today | tomorrow | YYYY-MM-DD.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import os
import random
import re
import secrets
import sqlite3
from contextlib import closing
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("plateroulette")

def _default_db_path() -> str:
    """Prefer a Railway volume at /data when present so SQLite survives deploys."""
    if os.environ.get("SPECIALS_DB"):
        return os.environ["SPECIALS_DB"]
    if os.path.isdir("/data"):
        return "/data/specials.db"
    return "specials.db"


DB_PATH = _default_db_path()
DEFAULT_TZ = os.environ.get("SPECIALS_TZ", "America/Toronto")
DEFAULT_DIGEST_TIME = "08:30"

HTML = ParseMode.HTML
RULE = "━━━━━━━━━━━━━━━"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday"]

# gold/silver/bronze for the podium, circled numerals for the rest of the ten
RANK_MARKS = ["🥇", "🥈", "🥉", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]
PODIUM_VISIBLE = 6      # shown up front; the rest go in an expandable block


def rank_mark(i: int) -> str:
    return RANK_MARKS[i] if i < len(RANK_MARKS) else "▪️"

DAY_ALIASES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

EMPTY_DAY_LINES = [
    "Nothing on the board. Chef's choice: whatever is in your fridge.",
    "No specials today. A tragic day for your wallet's good fortune.",
    "Empty slate. Leftovers day, maybe.",
    "Nothing scheduled — add some next time you walk past a sign.",
]

ROLL_FLAVOUR = [
    "The wheel says:", "Tonight you're having:", "Decision made:",
    "Fate has spoken:", "Don't argue with the dice:", "Locked in:",
]

DISH_EMOJI: list[tuple[tuple[str, ...], str]] = [
    (("kebab", "kabab", "koobideh", "kubideh", "joojeh", "shishlik", "skewer", "digi"), "🍢"),
    (("pizza", "slice"), "🍕"),
    (("burger",), "🍔"),
    (("sandwich", "wrap", "sub", "jambon"), "🥪"),
    (("sushi", "maki", "chirashi", "sashimi"), "🍣"),
    (("taco", "burrito", "quesadilla"), "🌮"),
    (("kookoo", "kuku", "omelet", "egg"), "🥚"),
    (("potato", "fries", "patties"), "🥔"),
    (("noodle", "ramen", "pho", "spaghetti", "macaroni", "mocaroni", "pasta", "lasagna"), "🍝"),
    (("stew", "khoresh", "khoresht", "ghormeh", "gheymeh", "fesenjan", "karafs", "bademjan"), "🍲"),
    (("soup", "ash ", "dizi", "broth"), "🥣"),
    (("fish", "mahi", "salmon", "tuna"), "🐟"),
    (("chicken", "morgh", "wing", "nardoon"), "🍗"),
    (("lamb", "shank", "neck", "beef", "steak", "kotlet", "koofteh", "veal"), "🥩"),
    (("polo", "polow", "rice", "biryani", "chelo", "tahchin", "tahdig"), "🍚"),
    (("salad", "tofu", "veg"), "🥗"),
    (("coffee", "latte", "espresso"), "☕"),
    (("cake", "dessert", "ice cream", "bastani"), "🍰"),
]


def dish_emoji(title: str, tags: str = "") -> str:
    blob = f"{title} {tags}".lower()
    for keywords, emoji in DISH_EMOJI:
        if any(k in blob for k in keywords):
            return emoji
    return "🍽"


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS households (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    join_code   TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    chat_id       INTEGER PRIMARY KEY,
    household_id  INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    label         TEXT NOT NULL DEFAULT '',
    joined_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS restaurants (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id  INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    muted         INTEGER NOT NULL DEFAULT 0,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    UNIQUE (household_id, name COLLATE NOCASE)
);

-- manual ranking of specials, one ordering per weekday, shared by the household
CREATE TABLE IF NOT EXISTS day_order (
    household_id  INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    weekday       INTEGER NOT NULL,          -- 0 = Monday … 6 = Sunday
    special_id    INTEGER NOT NULL REFERENCES specials(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    PRIMARY KEY (household_id, weekday, special_id)
);

CREATE TABLE IF NOT EXISTS specials (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id   INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    title          TEXT NOT NULL,
    price          REAL,
    tags           TEXT NOT NULL DEFAULT '',
    schedule_type  TEXT NOT NULL,        -- 'always' | 'weekly' | 'date'
    weekdays       TEXT NOT NULL DEFAULT '',
    on_date        TEXT,
    favorite       INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1,
    added_by       INTEGER,
    added_by_name  TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eaten (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id   INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    special_id     INTEGER,
    restaurant     TEXT NOT NULL,
    title          TEXT NOT NULL,
    price          REAL,
    ate_on         TEXT NOT NULL,
    rating         INTEGER,
    eaten_by       INTEGER,
    eaten_by_name  TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    chat_id      INTEGER PRIMARY KEY,
    digest_time  TEXT NOT NULL,
    tz           TEXT NOT NULL,
    paused       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_specials_house ON specials (household_id, active);
CREATE INDEX IF NOT EXISTS idx_eaten_house ON eaten (household_id, ate_on);
CREATE INDEX IF NOT EXISTS idx_day_order ON day_order (household_id, weekday, position);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def new_code(conn: sqlite3.Connection) -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not conn.execute(
            "SELECT 1 FROM households WHERE join_code = ?", (code,)
        ).fetchone():
            return code


def migrate_v1(conn: sqlite3.Connection) -> None:
    """Old single-user schema keyed data by chat_id. Move it into households."""
    if not _table_exists(conn, "specials"):
        return
    if "chat_id" not in _cols(conn, "specials"):
        return

    log.info("migrating v1 database to shared households")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    chat_ids = {
        r[0] for t in ("restaurants", "specials", "eaten", "settings")
        if _table_exists(conn, t)
        for r in conn.execute(f"SELECT DISTINCT chat_id FROM {t}")
    }

    for table in ("restaurants", "specials", "eaten"):
        if "household_id" not in _cols(conn, table):
            conn.execute(f"ALTER TABLE {table} RENAME COLUMN chat_id TO household_id")

    # temporarily negate so old chat_ids can't collide with new household ids
    for table in ("restaurants", "specials", "eaten"):
        conn.execute(f"UPDATE {table} SET household_id = -household_id")

    for chat_id in chat_ids:
        cur = conn.execute(
            "INSERT INTO households (name, join_code, created_at) VALUES (?,?,?)",
            ("Our specials", new_code(conn), now),
        )
        hid = cur.lastrowid
        conn.execute(
            "INSERT OR REPLACE INTO members (chat_id, household_id, label, joined_at) "
            "VALUES (?,?,?,?)",
            (chat_id, hid, "", now),
        )
        for table in ("restaurants", "specials", "eaten"):
            conn.execute(
                f"UPDATE {table} SET household_id = ? WHERE household_id = ?",
                (hid, -chat_id),
            )

    for table, cols in (
        ("specials", (("added_by", "INTEGER"), ("added_by_name", "TEXT"))),
        ("eaten", (("eaten_by", "INTEGER"), ("eaten_by_name", "TEXT"))),
    ):
        existing = _cols(conn, table)
        for col, coltype in cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    conn.commit()
    log.info("migration done: %d household(s)", len(chat_ids))


def init_db() -> None:
    with closing(db()) as conn:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS households ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,"
            "join_code TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS members ("
            "chat_id INTEGER PRIMARY KEY, household_id INTEGER NOT NULL,"
            "label TEXT NOT NULL DEFAULT '', joined_at TEXT NOT NULL);"
        )
        migrate_v1(conn)
        conn.executescript(SCHEMA)
        if "sort_order" not in _cols(conn, "restaurants"):
            log.info("adding restaurants.sort_order")
            conn.execute(
                "ALTER TABLE restaurants ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        conn.commit()


# ---- households -----------------------------------------------------------

def household_for(chat_id: int, label: str = "") -> int:
    """Chat's household, creating a private one on first contact."""
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT household_id FROM members WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row:
            if label:
                conn.execute(
                    "UPDATE members SET label = ? WHERE chat_id = ? AND label = ''",
                    (label, chat_id),
                )
                conn.commit()
            return row["household_id"]
        cur = conn.execute(
            "INSERT INTO households (name, join_code, created_at) VALUES (?,?,?)",
            ("Our specials", new_code(conn), now),
        )
        hid = cur.lastrowid
        conn.execute(
            "INSERT INTO members (chat_id, household_id, label, joined_at) VALUES (?,?,?,?)",
            (chat_id, hid, label, now),
        )
        conn.commit()
        return hid


def household_row(hid: int) -> Optional[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute("SELECT * FROM households WHERE id = ?", (hid,)).fetchone()


def members_of(hid: int) -> list[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM members WHERE household_id = ? ORDER BY joined_at", (hid,)
        ).fetchall()


def household_by_code(code: str) -> Optional[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM households WHERE join_code = ? COLLATE NOCASE", (code.strip(),)
        ).fetchone()


def household_size(hid: int) -> int:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM specials WHERE household_id = ? AND active = 1", (hid,)
        ).fetchone()["c"]


def merge_households(src: int, dst: int) -> None:
    """Move everything from src into dst, folding same-named restaurants together."""
    with closing(db()) as conn:
        for rest in conn.execute(
            "SELECT * FROM restaurants WHERE household_id = ?", (src,)
        ).fetchall():
            twin = conn.execute(
                "SELECT id FROM restaurants WHERE household_id = ? AND name = ? COLLATE NOCASE",
                (dst, rest["name"]),
            ).fetchone()
            if twin:
                conn.execute(
                    "UPDATE specials SET restaurant_id = ? WHERE restaurant_id = ?",
                    (twin["id"], rest["id"]),
                )
                conn.execute("DELETE FROM restaurants WHERE id = ?", (rest["id"],))
            else:
                conn.execute(
                    "UPDATE restaurants SET household_id = ? WHERE id = ?", (dst, rest["id"])
                )
        conn.execute("UPDATE specials SET household_id = ? WHERE household_id = ?", (dst, src))
        conn.execute("UPDATE eaten SET household_id = ? WHERE household_id = ?", (dst, src))
        conn.execute("DELETE FROM households WHERE id = ?", (src,))
        conn.commit()


def move_member(chat_id: int, hid: int, label: str = "") -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO members (chat_id, household_id, label, joined_at) VALUES (?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET household_id = excluded.household_id",
            (chat_id, hid, label, now),
        )
        conn.commit()


# ---- per-chat settings ----------------------------------------------------

def get_settings(chat_id: int) -> sqlite3.Row:
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM settings WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO settings (chat_id, digest_time, tz, paused) VALUES (?,?,?,0)",
                (chat_id, DEFAULT_DIGEST_TIME, DEFAULT_TZ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM settings WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return row


def update_settings(chat_id: int, **fields) -> None:
    get_settings(chat_id)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with closing(db()) as conn:
        conn.execute(
            f"UPDATE settings SET {cols} WHERE chat_id = ?", (*fields.values(), chat_id)
        )
        conn.commit()


def chat_tz(chat_id: int) -> ZoneInfo:
    try:
        return ZoneInfo(get_settings(chat_id)["tz"])
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TZ)


def today_for(chat_id: int) -> dt.date:
    return dt.datetime.now(chat_tz(chat_id)).date()


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

class ParseError(ValueError):
    pass


def parse_price(raw: str) -> Optional[float]:
    raw = raw.strip().lstrip("$").rstrip("$").replace(",", "")
    if not raw:
        return None
    try:
        return round(float(raw), 2)
    except ValueError:
        raise ParseError(f"I couldn't read “{raw}” as a price.")


def parse_days(raw: str, tz: ZoneInfo) -> tuple[str, str, Optional[str]]:
    raw = raw.strip().lower()
    if not raw:
        return "always", "", None
    if raw in {"daily", "everyday", "every day", "all", "always"}:
        return "weekly", "0,1,2,3,4,5,6", None
    if raw in {"weekdays", "weekday"}:
        return "weekly", "0,1,2,3,4", None
    if raw in {"weekends", "weekend"}:
        return "weekly", "5,6", None

    today = dt.datetime.now(tz).date()
    if raw == "today":
        return "date", "", today.isoformat()
    if raw == "tomorrow":
        return "date", "", (today + dt.timedelta(days=1)).isoformat()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            dt.date.fromisoformat(raw)
        except ValueError:
            raise ParseError(f"“{raw}” isn't a real date.")
        return "date", "", raw

    days: list[int] = []
    for part in re.split(r"[,/&+ ]+", raw):
        part = part.strip()
        if not part:
            continue
        if part not in DAY_ALIASES:
            raise ParseError(
                f"I don't recognise “{part}” as a day. Try mon/tue/wed…, weekdays, "
                "weekends, daily, today, tomorrow, or 2026-09-22."
            )
        days.append(DAY_ALIASES[part])
    if not days:
        return "always", "", None
    return "weekly", ",".join(str(d) for d in sorted(set(days))), None


def clean_tags(raw: str) -> str:
    return ",".join(t.strip().lower().lstrip("#") for t in raw.split(",") if t.strip())


def parse_special_line(line: str, tz: ZoneInfo) -> dict:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ParseError(
            "I need at least a restaurant and an item, split by “|”. Example:\n"
            "<code>Otto's Pizza | 2 slices + pop | 9.50 | mon,wed</code>"
        )
    parts += [""] * (5 - len(parts))
    restaurant, title, price_raw, days_raw, tags_raw = parts[:5]
    sched, weekdays, on_date = parse_days(days_raw, tz)
    return {
        "restaurant": restaurant,
        "title": title,
        "price": parse_price(price_raw),
        "tags": clean_tags(tags_raw),
        "schedule_type": sched,
        "weekdays": weekdays,
        "on_date": on_date,
    }


def insert_special(hid: int, data: dict, by_id: Optional[int] = None,
                   by_name: str = "") -> int:
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with closing(db()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO restaurants (household_id, name, created_at) VALUES (?,?,?)",
            (hid, data["restaurant"], now),
        )
        rid = conn.execute(
            "SELECT id FROM restaurants WHERE household_id = ? AND name = ? COLLATE NOCASE",
            (hid, data["restaurant"]),
        ).fetchone()["id"]
        cur = conn.execute(
            """INSERT INTO specials
               (household_id, restaurant_id, title, price, tags, schedule_type,
                weekdays, on_date, added_by, added_by_name, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (hid, rid, data["title"], data["price"], data["tags"],
             data["schedule_type"], data["weekdays"], data["on_date"],
             by_id, by_name, now),
        )
        conn.commit()
        return cur.lastrowid


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------

def specials_on(hid: int, day: dt.date, include_muted: bool = False) -> list[sqlite3.Row]:
    """That day's specials, in the household's chosen order for that weekday.

    Anything without a hand-picked position falls in behind the ranked items,
    using the restaurant order as the tiebreak."""
    wd = day.weekday()
    mute_clause = "" if include_muted else "AND r.muted = 0"
    with closing(db()) as conn:
        return conn.execute(
            f"""SELECT s.*, r.name AS restaurant, r.sort_order AS rest_order,
                       o.position AS position
                FROM specials s
                JOIN restaurants r ON r.id = s.restaurant_id
                LEFT JOIN day_order o ON o.special_id = s.id
                     AND o.weekday = ? AND o.household_id = ?
                WHERE s.household_id = ? AND s.active = 1 {mute_clause}
                  AND (s.schedule_type = 'always'
                       OR (s.schedule_type = 'date' AND s.on_date = ?)
                       OR (s.schedule_type = 'weekly'
                           AND (',' || s.weekdays || ',') LIKE ?))
                ORDER BY o.position IS NULL, o.position,
                         r.sort_order, s.favorite DESC, r.name COLLATE NOCASE,
                         s.price IS NULL, s.price""",
            (wd, hid, hid, day.isoformat(), f"%,{wd},%"),
        ).fetchall()


def next_date_for(chat_id: int, weekday: int) -> dt.date:
    """A real date landing on that weekday — today if it matches, else the next one."""
    today = today_for(chat_id)
    return today + dt.timedelta(days=(weekday - today.weekday()) % 7)


def has_order(hid: int, weekday: int) -> bool:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT 1 FROM day_order WHERE household_id = ? AND weekday = ? LIMIT 1",
            (hid, weekday),
        ).fetchone() is not None


def materialize_order(hid: int, weekday: int, day: dt.date) -> None:
    """Freeze the current implicit order into day_order so it can be nudged."""
    rows = specials_on(hid, day, include_muted=True)
    with closing(db()) as conn:
        conn.execute("DELETE FROM day_order WHERE household_id = ? AND weekday = ?",
                     (hid, weekday))
        conn.executemany(
            "INSERT INTO day_order (household_id, weekday, special_id, position) "
            "VALUES (?,?,?,?)",
            [(hid, weekday, r["id"], i) for i, r in enumerate(rows)],
        )
        conn.commit()


def move_in_order(hid: int, weekday: int, day: dt.date, sid: int, delta: int) -> None:
    if not has_order(hid, weekday):
        materialize_order(hid, weekday, day)
    with closing(db()) as conn:
        ids = [r["special_id"] for r in conn.execute(
            "SELECT special_id FROM day_order WHERE household_id = ? AND weekday = ? "
            "ORDER BY position", (hid, weekday))]
        if sid not in ids:
            ids.append(sid)
        i = ids.index(sid)
        j = i + delta
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        conn.execute("DELETE FROM day_order WHERE household_id = ? AND weekday = ?",
                     (hid, weekday))
        conn.executemany(
            "INSERT INTO day_order (household_id, weekday, special_id, position) "
            "VALUES (?,?,?,?)",
            [(hid, weekday, s, k) for k, s in enumerate(ids)],
        )
        conn.commit()


def clear_order(hid: int, weekday: int) -> None:
    with closing(db()) as conn:
        conn.execute("DELETE FROM day_order WHERE household_id = ? AND weekday = ?",
                     (hid, weekday))
        conn.commit()


def move_restaurant(hid: int, rid: int, delta: int) -> None:
    with closing(db()) as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM restaurants WHERE household_id = ? "
            "ORDER BY sort_order, name COLLATE NOCASE", (hid,))]
        if rid not in ids:
            return
        i = ids.index(rid)
        j = i + delta
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
        for k, r in enumerate(ids):
            conn.execute("UPDATE restaurants SET sort_order = ? WHERE id = ?", (k, r))
        conn.commit()


def get_special(hid: int, sid: int) -> Optional[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute(
            """SELECT s.*, r.name AS restaurant FROM specials s
               JOIN restaurants r ON r.id = s.restaurant_id
               WHERE s.id = ? AND s.household_id = ?""",
            (sid, hid),
        ).fetchone()


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def money(price: Optional[float]) -> str:
    if price is None:
        return ""
    return f"${price:,.2f}".replace(".00", "")


def schedule_label(row) -> str:
    if row["schedule_type"] == "date":
        return dt.date.fromisoformat(row["on_date"]).strftime("%a %b %-d")
    if row["schedule_type"] == "weekly":
        days = [int(d) for d in row["weekdays"].split(",") if d != ""]
        if len(days) == 7:
            return "every day"
        if days == [0, 1, 2, 3, 4]:
            return "weekdays"
        if days == [5, 6]:
            return "weekends"
        return " ".join(WEEKDAY_NAMES[d] for d in days)
    return "always"


def item_line(row, show_days: bool = False) -> str:
    emoji = dish_emoji(row["title"], row["tags"])
    star = "⭐" if row["favorite"] else ""
    price = f"  <b>{money(row['price'])}</b>" if row["price"] is not None else ""
    meta = []
    if show_days:
        meta.append(schedule_label(row))
    if row["tags"]:
        meta.append(" ".join(f"#{t}" for t in row["tags"].split(",")))
    sub = f"\n     <i>{esc(' · '.join(meta))}</i>" if meta else ""
    return f"{emoji} {esc(row['title'])}{price} {star}{sub}"


def day_title(chat_id: int, day: dt.date) -> str:
    delta = (day - today_for(chat_id)).days
    prefix = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get(delta, "")
    stamp = day.strftime("%a %b %-d")
    return f"{prefix} · {stamp}" if prefix else stamp


def ranked_line(i: int, row) -> str:
    price = f"  <b>{money(row['price'])}</b>" if row["price"] is not None else ""
    star = " ⭐" if row["favorite"] else ""
    meta = [row["restaurant"]]
    if row["tags"]:
        meta.append(" ".join(f"#{t}" for t in row["tags"].split(",")))
    return (
        f"{rank_mark(i)} {dish_emoji(row['title'], row['tags'])} "
        f"{esc(row['title'])}{price}{star}\n"
        f"      <i>{esc(' · '.join(meta))}</i>"
    )


def render_day_body(chat_id: int, hid: int, day: dt.date) -> str:
    rows = specials_on(hid, day)
    head = f"🍽 <b>{esc(day_title(chat_id, day))}</b>\n{RULE}"
    if not rows:
        return f"{head}\n\n<i>{esc(random.choice(EMPTY_DAY_LINES))}</i>"

    lines = [ranked_line(i, r) for i, r in enumerate(rows)]
    body = "<blockquote>" + "\n".join(lines[:PODIUM_VISIBLE]) + "</blockquote>"
    if len(lines) > PODIUM_VISIBLE:
        tail = lines[PODIUM_VISIBLE:]
        body += (f"\n<blockquote expandable><b>{len(tail)} more</b>\n"
                 + "\n".join(tail) + "</blockquote>")

    prices = [r["price"] for r in rows if r["price"] is not None]
    footer = f"{len(rows)} choices"
    if prices:
        footer += f" · from {money(min(prices))}"
    if not has_order(hid, day.weekday()):
        footer += " · default order"
    return f"{head}\n{body}\n<i>{footer}</i>"


# --------------------------------------------------------------------------
# views  (each returns (text, keyboard))
# --------------------------------------------------------------------------

def btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def view_home(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    day = today_for(chat_id)
    rows = specials_on(hid, day)
    cfg = get_settings(chat_id)
    people = len(members_of(hid))
    with closing(db()) as conn:
        n_sp = conn.execute(
            "SELECT COUNT(*) c FROM specials WHERE household_id = ? AND active = 1", (hid,)
        ).fetchone()["c"]
        n_r = conn.execute(
            "SELECT COUNT(*) c FROM restaurants WHERE household_id = ?", (hid,)
        ).fetchone()["c"]

    bell = "🔔" if not cfg["paused"] else "🔕"
    shared = f"\n👥 shared with {people - 1} other" + ("s" if people > 2 else "") if people > 1 else ""
    text = (
        f"🎰 <b>PlateRoulette</b>\n{RULE}\n"
        f"<b>{len(rows)}</b> specials on today\n"
        f"{n_sp} saved across {n_r} places\n"
        f"{bell} your digest at {cfg['digest_time']}{shared}\n\n"
        f"<i>Tip: paste lines like</i>\n"
        f"<code>Otto's | Wing night | 0.75 | thu</code>\n"
        f"<i>to add several at once.</i>"
    )
    kb = InlineKeyboardMarkup([
        [btn("🍽 Today", f"day:{day.isoformat()}"), btn("📅 Week", "week")],
        [btn("🎲 Pick for me", f"roll:{day.isoformat()}"),
         btn("✅ Log a meal", f"logmenu:{day.isoformat()}")],
        [btn("📖 Browse", "browse"), btn("📊 Stats", "stats")],
        [btn("➕ Add", "addhint"), btn("👥 Household", "house")],
        [btn("⚙️ Settings", "settings")],
    ])
    return text, kb


def view_day(chat_id: int, day: dt.date) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    prev_day = (day - dt.timedelta(days=1)).isoformat()
    next_day = (day + dt.timedelta(days=1)).isoformat()
    rows = specials_on(hid, day)
    kb = [[btn("◀", f"day:{prev_day}"),
           btn("Today", f"day:{today_for(chat_id).isoformat()}"),
           btn("▶", f"day:{next_day}")]]
    if rows:
        kb.append([btn("🎲 Pick for me", f"roll:{day.isoformat()}"),
                   btn("✅ Log a meal", f"logmenu:{day.isoformat()}")])
        kb.append([btn("↕️ Reorder this day", f"ord:{day.weekday()}")])
    kb.append([btn("🏠 Home", "home"), btn("📅 Week", "week")])
    return render_day_body(chat_id, hid, day), InlineKeyboardMarkup(kb)


REORDER_LIMIT = 10


def view_reorder(chat_id: int, weekday: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    day = next_date_for(chat_id, weekday)
    rows = specials_on(hid, day, include_muted=True)

    head = (
        f"↕️ <b>{WEEKDAY_FULL[weekday]}'s running order</b>\n{RULE}\n"
        f"<i>Top of the list wins the medal. Everyone in the household sees "
        f"the same order, and it sticks every {WEEKDAY_FULL[weekday]}.</i>"
    )
    if not rows:
        kb = InlineKeyboardMarkup([[btn(WEEKDAY_NAMES[d], f"ord:{d}") for d in range(4)],
                                   [btn(WEEKDAY_NAMES[d], f"ord:{d}") for d in range(4, 7)],
                                   [btn("🏠 Home", "home")]])
        return f"{head}\n\n<i>Nothing on this day yet.</i>", kb

    shown = rows[:REORDER_LIMIT]
    listing = "\n".join(
        f"{rank_mark(i)} {esc(r['title'])} <i>— {esc(r['restaurant'])}</i>"
        for i, r in enumerate(shown)
    )
    extra = (f"\n<i>+{len(rows) - REORDER_LIMIT} further down, unranked</i>"
             if len(rows) > REORDER_LIMIT else "")

    kb = []
    for i, row in enumerate(shown):
        label = row["title"] if len(row["title"]) <= 20 else row["title"][:19] + "…"
        kb.append([
            btn("▲", f"mv:{weekday}:{row['id']}:u") if i else btn("·", "noop"),
            btn(f"{rank_mark(i)} {label}", f"sp:{row['id']}"),
            btn("▼", f"mv:{weekday}:{row['id']}:d") if i < len(shown) - 1 else btn("·", "noop"),
        ])
    kb.append([btn(WEEKDAY_NAMES[d], f"ord:{d}") for d in range(4)])
    kb.append([btn(WEEKDAY_NAMES[d], f"ord:{d}") for d in range(4, 7)])
    kb.append([btn("↺ Reset to default", f"ordreset:{weekday}"),
               btn("✔️ Done", f"day:{day.isoformat()}")])
    return f"{head}\n\n<blockquote>{listing}</blockquote>{extra}", InlineKeyboardMarkup(kb)


def view_week(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    start = today_for(chat_id)
    blocks = []
    for i in range(7):
        day = start + dt.timedelta(days=i)
        rows = specials_on(hid, day)
        label = "Today" if i == 0 else day.strftime("%a %-d")
        if not rows:
            blocks.append(f"<b>{esc(label)}</b>  <i>— nothing</i>")
            continue
        lines = "\n".join(
            f"  {dish_emoji(r['title'], r['tags'])} {esc(r['title'])}"
            f"{('  ' + money(r['price'])) if r['price'] is not None else ''}"
            f"  <i>{esc(r['restaurant'])}</i>"
            for r in rows[:4]
        )
        more = f"\n  <i>+{len(rows) - 4} more</i>" if len(rows) > 4 else ""
        blocks.append(f"<b>{esc(label)}</b>\n{lines}{more}")
    text = f"📅 <b>The week ahead</b>\n{RULE}\n" + "\n\n".join(blocks)
    kb = InlineKeyboardMarkup([[btn("🍽 Today", f"day:{start.isoformat()}"),
                               btn("🏠 Home", "home")]])
    return text, kb


def view_browse(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    with closing(db()) as conn:
        rows = conn.execute(
            """SELECT r.id, r.name, r.muted, COUNT(s.id) c
               FROM restaurants r
               LEFT JOIN specials s ON s.restaurant_id = r.id AND s.active = 1
               WHERE r.household_id = ?
               GROUP BY r.id ORDER BY r.sort_order, r.name COLLATE NOCASE""",
            (hid,),
        ).fetchall()
    if not rows:
        return (
            "📖 <b>Nothing saved yet</b>\n\nSend a line like\n"
            "<code>Otto's | 2 slices + pop | 9.50 | mon,wed</code>",
            InlineKeyboardMarkup([[btn("🏠 Home", "home")]]),
        )
    kb = []
    for i, r in enumerate(rows):
        name = r["name"] if len(r["name"]) <= 22 else r["name"][:21] + "…"
        kb.append([
            btn("▲", f"rmv:{r['id']}:u") if i else btn("·", "noop"),
            btn(f"{'🔕 ' if r['muted'] else ''}{name} ({r['c']})", f"rest:{r['id']}"),
            btn("▼", f"rmv:{r['id']}:d") if i < len(rows) - 1 else btn("·", "noop"),
        ])
    kb.append([btn("🏠 Home", "home")])
    text = (
        f"📖 <b>Your places</b>\n{RULE}\n"
        "Tap a name to edit its specials, or ▲▼ to set the house order.\n"
        "<i>This is the tiebreak — each day's own ranking wins over it.</i>"
    )
    return text, InlineKeyboardMarkup(kb)


def view_restaurant(chat_id: int, rid: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    with closing(db()) as conn:
        rest = conn.execute(
            "SELECT * FROM restaurants WHERE id = ? AND household_id = ?", (rid, hid)
        ).fetchone()
        if rest is None:
            return "That place is gone.", InlineKeyboardMarkup([[btn("🏠 Home", "home")]])
        rows = conn.execute(
            """SELECT * FROM specials WHERE restaurant_id = ? AND active = 1
               ORDER BY favorite DESC, title COLLATE NOCASE""", (rid,)
        ).fetchall()

    head = f"🏪 <b>{esc(rest['name'])}</b>\n{RULE}"
    if rest["muted"]:
        head += "\n<i>Muted — hidden from everyone's digest.</i>"
    body = "\n".join(item_line(r, show_days=True) for r in rows) or "<i>No specials yet.</i>"
    kb = [[btn(f"{'⭐ ' if r['favorite'] else ''}{r['title'][:40]}", f"sp:{r['id']}")]
          for r in rows]
    kb.append([btn("✏️ Rename", f"ren:{rid}"),
               btn("🔔 Unmute" if rest["muted"] else "🔕 Mute", f"mute:{rid}")])
    kb.append([btn("◀ Places", "browse"), btn("🏠 Home", "home")])
    return f"{head}\n{body}", InlineKeyboardMarkup(kb)


def view_special(chat_id: int, sid: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    row = get_special(hid, sid)
    if row is None:
        return "That special is gone.", InlineKeyboardMarkup([[btn("🏠 Home", "home")]])
    tags = " ".join(f"#{t}" for t in row["tags"].split(",")) if row["tags"] else "—"
    added = f"\n✍️ added by {esc(row['added_by_name'])}" if row["added_by_name"] else ""
    text = (
        f"{dish_emoji(row['title'], row['tags'])} <b>{esc(row['title'])}</b>\n{RULE}\n"
        f"🏪 {esc(row['restaurant'])}\n"
        f"💵 {money(row['price']) or 'no price set'}\n"
        f"📆 {esc(schedule_label(row))}\n"
        f"🏷 {esc(tags)}{added}"
    )
    kb = InlineKeyboardMarkup([
        [btn("⭐ Unstar" if row["favorite"] else "⭐ Star", f"fav:{sid}"),
         btn("✅ I ate this", f"log:{sid}")],
        [btn("🗑 Delete", f"askdel:{sid}")],
        [btn("◀ Back", f"rest:{row['restaurant_id']}"), btn("🏠 Home", "home")],
    ])
    return text, kb


def view_stats(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    kb = InlineKeyboardMarkup([[btn("📤 Export CSV", "export"), btn("🏠 Home", "home")]])
    with closing(db()) as conn:
        total = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(price),0) s, AVG(price) a "
            "FROM eaten WHERE household_id = ?", (hid,)
        ).fetchone()
        if total["c"] == 0:
            return ("📊 <b>Nothing logged yet</b>\n\nTap <b>I ate this</b> on a pick and "
                    "I'll start tracking spend, streaks and favourites.", kb)
        month_start = today_for(chat_id).replace(day=1).isoformat()
        month = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(price),0) s FROM eaten "
            "WHERE household_id = ? AND ate_on >= ?", (hid, month_start)
        ).fetchone()
        top_rest = conn.execute(
            "SELECT restaurant, COUNT(*) c FROM eaten WHERE household_id = ? "
            "GROUP BY restaurant COLLATE NOCASE ORDER BY c DESC LIMIT 1", (hid,)
        ).fetchone()
        top_item = conn.execute(
            "SELECT title, COUNT(*) c FROM eaten WHERE household_id = ? "
            "GROUP BY title COLLATE NOCASE ORDER BY c DESC LIMIT 1", (hid,)
        ).fetchone()
        best = conn.execute(
            "SELECT title, AVG(rating) r FROM eaten WHERE household_id = ? "
            "AND rating IS NOT NULL GROUP BY title COLLATE NOCASE "
            "ORDER BY r DESC, COUNT(*) DESC LIMIT 1", (hid,)
        ).fetchone()
        by_person = conn.execute(
            "SELECT COALESCE(NULLIF(eaten_by_name,''),'someone') who, COUNT(*) c, "
            "COALESCE(SUM(price),0) s FROM eaten WHERE household_id = ? "
            "GROUP BY who ORDER BY c DESC", (hid,)
        ).fetchall()
        recent = conn.execute(
            "SELECT ate_on, title, rating, eaten_by_name FROM eaten "
            "WHERE household_id = ? ORDER BY id DESC LIMIT 3", (hid,)
        ).fetchall()

    lines = [f"📊 <b>The household</b>\n{RULE}"]
    lines.append(f"🍴 {total['c']} meals · {money(round(total['s'], 2))} all time")
    lines.append(f"📆 This month: {month['c']} · {money(round(month['s'], 2))}")
    if total["a"]:
        lines.append(f"💵 Average: {money(round(total['a'], 2))}")
    lines.append(f"🔥 Your streak: {current_streak(chat_id, hid, today_for(chat_id))} days")
    if top_rest:
        lines.append(f"🏪 Most visited: {esc(top_rest['restaurant'])} ({top_rest['c']}×)")
    if top_item:
        lines.append(f"🥇 Most eaten: {esc(top_item['title'])} ({top_item['c']}×)")
    if best:
        lines.append(f"⭐ Best rated: {esc(best['title'])} — {best['r']:.1f}")
    if len(by_person) > 1:
        lines.append("\n<b>Who's eating</b>")
        lines.append("<blockquote>" + "\n".join(
            f"{esc(p['who'])} — {p['c']} meals · {money(round(p['s'], 2))}"
            for p in by_person
        ) + "</blockquote>")
    if recent:
        lines.append("\n<b>Recently</b>")
        lines.append("<blockquote>" + "\n".join(
            f"{r['ate_on'][5:]} · {esc(r['title'])}"
            + (f" {'⭐' * r['rating']}" if r["rating"] else "")
            + (f" <i>({esc(r['eaten_by_name'])})</i>" if r["eaten_by_name"] else "")
            for r in recent
        ) + "</blockquote>")
    return "\n".join(lines), kb


def view_household(chat_id: int, bot_username: str = "") -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    house = household_row(hid)
    people = members_of(hid)
    roster = "\n".join(
        f"• {esc(m['label'] or 'someone')}" + (" <i>(you)</i>" if m["chat_id"] == chat_id else "")
        for m in people
    )
    link = f"https://t.me/{bot_username}?start=join-{house['join_code']}" if bot_username else ""
    text = (
        f"👥 <b>{esc(house['name'])}</b>\n{RULE}\n"
        f"{roster}\n\n"
        f"<b>Invite code</b>\n<code>{house['join_code']}</code>\n"
        + (f"\n<a href=\"{link}\">Tap here to share the join link</a>\n" if link else "")
        + f"\n<i>They open the bot and send</i> <code>/join {house['join_code']}</code>\n"
        f"<i>Everyone edits the same board. Digest times stay personal.</i>"
    )
    kb = InlineKeyboardMarkup([
        [btn("✏️ Rename household", "renamehint")],
        [btn("🚪 Leave household", "askleave")],
        [btn("🏠 Home", "home")],
    ])
    return text, kb


TIME_PRESETS = ["07:00", "08:00", "08:30", "09:00", "10:00", "11:30"]


def view_settings(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    cfg = get_settings(chat_id)
    state = "paused" if cfg["paused"] else f"on, {cfg['digest_time']} daily"
    text = (
        f"⚙️ <b>Your settings</b>\n{RULE}\n"
        f"🔔 Digest: {esc(state)}\n"
        f"🌍 Timezone: {esc(cfg['tz'])}\n\n"
        f"<i>These are yours alone — other members keep their own.</i>\n\n"
        f"<code>/settime 07:45</code>\n"
        f"<code>/tz America/Toronto</code>\n"
        f"<code>/rename old place | new place</code>"
    )
    rows = [
        [btn(("✅ " if cfg["digest_time"] == t else "") + t, f"time:{t}") for t in TIME_PRESETS[:3]],
        [btn(("✅ " if cfg["digest_time"] == t else "") + t, f"time:{t}") for t in TIME_PRESETS[3:]],
        [btn("🔕 Pause digest" if not cfg["paused"] else "🔔 Resume digest", "togglepause")],
        [btn("📤 Export CSV", "export"), btn("🏠 Home", "home")],
    ]
    return text, InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------
# roulette + meal log
# --------------------------------------------------------------------------

def weighted_pick(rows: list[sqlite3.Row]) -> sqlite3.Row:
    """Still random, but the podium and your starred items come up more often."""
    pool = []
    for i, row in enumerate(rows):
        weight = 1 + (1 if row["favorite"] else 0) + (1 if i < 3 else 0)
        pool.extend([row] * weight)
    return random.choice(pool)


def view_roll(chat_id: int, day: dt.date) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    rows = specials_on(hid, day)
    if not rows:
        return "🎲 Nothing to pick from that day.", \
            InlineKeyboardMarkup([[btn("🏠 Home", "home")]])
    pick = weighted_pick(rows)
    price = f"\n💵 {money(pick['price'])}" if pick["price"] is not None else ""
    text = (
        f"🎲 <i>{random.choice(ROLL_FLAVOUR)}</i>\n{RULE}\n"
        f"{dish_emoji(pick['title'], pick['tags'])} <b>{esc(pick['title'])}</b>\n"
        f"🏪 {esc(pick['restaurant'])}{price}"
    )
    kb = InlineKeyboardMarkup([
        [btn("🎲 Again", f"roll:{day.isoformat()}"), btn("✅ I ate this", f"log:{pick['id']}")],
        [btn("🍽 Full list", f"day:{day.isoformat()}"), btn("🏠 Home", "home")],
    ])
    return text, kb


def view_logmenu(chat_id: int, day: dt.date) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    rows = specials_on(hid, day, include_muted=True)
    if not rows:
        return "Nothing on that day's board to log.", \
            InlineKeyboardMarkup([[btn("🏠 Home", "home")]])
    kb = [[btn(f"{dish_emoji(r['title'], r['tags'])} {r['title']} · {r['restaurant']}"[:60],
               f"log:{r['id']}")] for r in rows[:25]]
    kb.append([btn("◀ Back", f"day:{day.isoformat()}"), btn("🏠 Home", "home")])
    return f"✅ <b>What did you have?</b>\n{RULE}", InlineKeyboardMarkup(kb)


def record_meal(chat_id: int, hid: int, sid: int, who_id: int,
                who_name: str) -> Optional[tuple[int, sqlite3.Row]]:
    row = get_special(hid, sid)
    if row is None:
        return None
    with closing(db()) as conn:
        cur = conn.execute(
            """INSERT INTO eaten (household_id, special_id, restaurant, title, price,
                                  ate_on, eaten_by, eaten_by_name)
               VALUES (?,?,?,?,?,?,?,?)""",
            (hid, row["id"], row["restaurant"], row["title"], row["price"],
             today_for(chat_id).isoformat(), who_id, who_name),
        )
        conn.commit()
        return cur.lastrowid, row


def view_rating(log_id: int, row: sqlite3.Row) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"✅ Logged <b>{esc(row['title'])}</b>"
        f"{' · ' + money(row['price']) if row['price'] is not None else ''}\n\nHow was it?"
    )
    kb = InlineKeyboardMarkup([
        [btn("⭐" * n, f"rate:{log_id}:{n}") for n in range(1, 6)],
        [btn("Skip", "home")],
    ])
    return text, kb


def current_streak(chat_id: int, hid: int, today: dt.date) -> int:
    with closing(db()) as conn:
        days = {r["ate_on"] for r in conn.execute(
            "SELECT DISTINCT ate_on FROM eaten WHERE household_id = ? AND eaten_by = ?",
            (hid, chat_id))}
    if not days:
        return 0
    cursor = today if today.isoformat() in days else today - dt.timedelta(days=1)
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= dt.timedelta(days=1)
    return streak


# --------------------------------------------------------------------------
# digest scheduling — one job per member chat
# --------------------------------------------------------------------------

def job_name(chat_id: int) -> str:
    return f"digest:{chat_id}"


def schedule_digest(app: Application, chat_id: int) -> None:
    for job in app.job_queue.get_jobs_by_name(job_name(chat_id)):
        job.schedule_removal()
    cfg = get_settings(chat_id)
    if cfg["paused"]:
        return
    hh, mm = (int(x) for x in cfg["digest_time"].split(":"))
    try:
        tz = ZoneInfo(cfg["tz"])
    except ZoneInfoNotFoundError:
        tz = ZoneInfo(DEFAULT_TZ)
    app.job_queue.run_daily(
        send_digest, time=dt.time(hour=hh, minute=mm, tzinfo=tz),
        name=job_name(chat_id), chat_id=chat_id,
    )
    log.info("digest scheduled for %s at %s %s", chat_id, cfg["digest_time"], cfg["tz"])


async def send_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    text, kb = view_day(chat_id, today_for(chat_id))
    await context.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb)


# --------------------------------------------------------------------------
# reply helpers
# --------------------------------------------------------------------------

async def show(update: Update, text: str, kb: Optional[InlineKeyboardMarkup]) -> None:
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.warning("edit failed, sending fresh: %s", exc)
            await update.callback_query.message.reply_text(text, parse_mode=HTML, reply_markup=kb)
            return
    await update.effective_message.reply_text(text, parse_mode=HTML, reply_markup=kb)


def who(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return ""
    return user.first_name or user.username or ""


async def notify_others(context: ContextTypes.DEFAULT_TYPE, hid: int,
                        exclude: int, text: str) -> None:
    for member in members_of(hid):
        if member["chat_id"] == exclude:
            continue
        try:
            await context.bot.send_message(member["chat_id"], text, parse_mode=HTML)
        except Exception as exc:  # blocked the bot, left the chat, etc.
            log.warning("could not notify %s: %s", member["chat_id"], exc)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

HELP = f"""🎰 <b>PlateRoulette</b>
{RULE}
<b>Adding</b>
Just send lines — no command needed:
<code>Otto's | Wing night | 0.75 | thu | wings</code>
Fields: <i>restaurant | item | price | days | tags</i>
Only the first two are required. Paste many lines at once.
/add on its own walks you through it with buttons.

<b>Getting around</b>
/home /today /tomorrow /week /browse
/roll — let the bot decide
/order mon — rank Monday's choices 🥇🥈🥉
/find pizza · /cheap 12
/ate — log a meal · /stats

<b>Sharing</b>
/invite — code and link for someone to join
/join CODE — join their board
/household — who's in · /leave

<b>Admin</b>
/rename Old Place | New Place
/settime 08:30 · /tz America/Toronto
/pause /resume /export
"""


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    payload = context.args[0] if context.args else ""
    if payload.startswith("join-"):
        return await do_join(update, context, payload[5:])
    household_for(chat_id, who(update))
    get_settings(chat_id)
    schedule_digest(context.application, chat_id)
    text, kb = view_home(chat_id)
    await update.effective_message.reply_text(text, parse_mode=HTML, reply_markup=kb)


async def do_join(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    chat_id = update.effective_chat.id
    label = who(update)
    target = household_by_code(code)
    if target is None:
        await update.effective_message.reply_text(
            "That code doesn't match any household. Codes look like <code>K7M2QX</code>.",
            parse_mode=HTML)
        return

    mine = household_for(chat_id, label)
    if mine == target["id"]:
        await update.effective_message.reply_text("You're already in that household.")
        return

    carry = household_size(mine)
    solo = len(members_of(mine)) == 1
    move_member(chat_id, target["id"], label)
    get_settings(chat_id)
    schedule_digest(context.application, chat_id)

    await notify_others(
        context, target["id"], chat_id,
        f"👋 <b>{esc(label or 'Someone')}</b> joined the household.")

    if carry and solo:
        text = (
            f"✅ Joined <b>{esc(target['name'])}</b>.\n\n"
            f"You had <b>{carry}</b> specials of your own. Bring them across?\n"
            f"<i>Same-named restaurants get folded together, not duplicated.</i>"
        )
        kb = InlineKeyboardMarkup([
            [btn("📦 Bring them over", f"mrg:{mine}"), btn("Leave them", "home")]
        ])
        await update.effective_message.reply_text(text, parse_mode=HTML, reply_markup=kb)
        return

    text, kb = view_home(chat_id)
    await update.effective_message.reply_text(
        f"✅ Joined <b>{esc(target['name'])}</b>.\n\n{text}", parse_mode=HTML, reply_markup=kb)


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "Send the code, e.g. <code>/join K7M2QX</code>", parse_mode=HTML)
        return
    await do_join(update, context, context.args[0])


async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = view_household(update.effective_chat.id, context.bot.username)
    await show(update, text, kb)


async def cmd_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🚪 <b>Leave this household?</b>\n\n"
        "You'll start a fresh, empty board of your own. The shared one stays put "
        "for everyone else — you can rejoin with the code."
    )
    kb = InlineKeyboardMarkup([[btn("Yes, leave", "leave"), btn("Cancel", "house")]])
    await show(update, text, kb)


async def cmd_sethouse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text(
            "What should I call it? e.g. <code>/sethouse The Rezaei Kitchen</code>",
            parse_mode=HTML)
        return
    hid = household_for(update.effective_chat.id, who(update))
    with closing(db()) as conn:
        conn.execute("UPDATE households SET name = ? WHERE id = ?", (name[:60], hid))
        conn.commit()
    text, kb = view_household(update.effective_chat.id, context.bot.username)
    await update.effective_message.reply_text(text, parse_mode=HTML, reply_markup=kb)


async def cmd_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show(update, *view_home(update.effective_chat.id))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, parse_mode=HTML)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await show(update, *view_day(chat_id, today_for(chat_id)))


async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await show(update, *view_day(chat_id, today_for(chat_id) + dt.timedelta(days=1)))


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show(update, *view_week(update.effective_chat.id))


async def cmd_browse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show(update, *view_browse(update.effective_chat.id))


async def cmd_roll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await show(update, *view_roll(chat_id, today_for(chat_id)))


async def cmd_ate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await show(update, *view_logmenu(chat_id, today_for(chat_id)))


async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    arg = (context.args[0].lower()[:3] if context.args else "")
    weekday = DAY_ALIASES.get(arg, today_for(chat_id).weekday())
    await show(update, *view_reorder(chat_id, weekday))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show(update, *view_stats(update.effective_chat.id))


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show(update, *view_settings(update.effective_chat.id))


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "Search for what? e.g. <code>/find wings</code>", parse_mode=HTML)
        return
    hid = household_for(update.effective_chat.id)
    q = f"%{' '.join(context.args).lower()}%"
    with closing(db()) as conn:
        rows = conn.execute(
            """SELECT s.*, r.name AS restaurant
               FROM specials s JOIN restaurants r ON r.id = s.restaurant_id
               WHERE s.household_id = ? AND s.active = 1
                 AND (lower(s.title) LIKE ? OR lower(s.tags) LIKE ? OR lower(r.name) LIKE ?)
               ORDER BY r.name COLLATE NOCASE LIMIT 30""",
            (hid, q, q, q),
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("No matches.")
        return
    body = "\n".join(f"{item_line(r, show_days=True)}\n     <i>{esc(r['restaurant'])}</i>"
                     for r in rows)
    kb = InlineKeyboardMarkup(
        [[btn(r["title"][:40], f"sp:{r['id']}")] for r in rows[:8]] + [[btn("🏠 Home", "home")]])
    await update.effective_message.reply_text(
        f"🔎 <b>{len(rows)} matches</b>\n{RULE}\n{body}", parse_mode=HTML, reply_markup=kb)


async def cmd_cheap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    hid = household_for(chat_id)
    try:
        ceiling = float(context.args[0].lstrip("$")) if context.args else 15.0
    except ValueError:
        await update.effective_message.reply_text("Give me a number, e.g. /cheap 12")
        return
    rows = [r for r in specials_on(hid, today_for(chat_id))
            if r["price"] is not None and r["price"] <= ceiling]
    if not rows:
        await update.effective_message.reply_text(f"Nothing under {money(ceiling)} today.")
        return
    body = "\n".join(f"{item_line(r)}\n     <i>{esc(r['restaurant'])}</i>" for r in rows)
    await update.effective_message.reply_text(
        f"💸 <b>Under {money(ceiling)} today</b>\n{RULE}\n{body}", parse_mode=HTML)


RENAME_PREFIX = re.compile(r"^/rename(@\S+)?\s*", re.I)


def apply_rename(hid: int, old: str, new: str) -> str:
    """Returns a status line for one rename attempt."""
    if not old or not new:
        return f"⚠️ Need both names either side of “|”: <code>{esc(old or '?')}</code>"
    if len(new) > 60 or "\n" in new:
        return f"⚠️ “{esc(new[:30])}…” is too long for a name."
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT id FROM restaurants WHERE household_id = ? AND name = ? COLLATE NOCASE",
            (hid, old)).fetchone()
        if row is None:
            return f"⚠️ No place called “{esc(old)}”."
        try:
            conn.execute("UPDATE restaurants SET name = ? WHERE id = ?", (new, row["id"]))
            conn.commit()
        except sqlite3.IntegrityError:
            return f"⚠️ There's already a place called “{esc(new)}”."
    return f"🏪 {esc(old)} → <b>{esc(new)}</b>"


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hid = household_for(update.effective_chat.id)
    text = update.effective_message.text or ""

    # one rename per line, so pasting three at once does the right thing
    lines = [RENAME_PREFIX.sub("", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines or not any("|" in ln for ln in lines):
        await update.effective_message.reply_text(
            "Use <code>/rename Old Name | New Name</code>\n\n"
            "<i>One per line if you're doing several. Or open /browse, tap the "
            "place, and hit ✏️ Rename — no pipes needed.</i>", parse_mode=HTML)
        return

    results = []
    for line in lines:
        if "|" not in line:
            results.append(f"⚠️ Skipped “{esc(line[:40])}” — no “|” in it.")
            continue
        old, new = (p.strip() for p in line.split("|", 1))
        results.append(apply_rename(hid, old, new))
    await update.effective_message.reply_text("\n".join(results), parse_mode=HTML)


async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args or not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", context.args[0]):
        await update.effective_message.reply_text("Use 24h time, e.g. /settime 08:30")
        return
    hh, mm = context.args[0].split(":")
    update_settings(chat_id, digest_time=f"{int(hh):02d}:{mm}", paused=0)
    schedule_digest(context.application, chat_id)
    text, kb = view_settings(chat_id)
    await update.effective_message.reply_text(text, parse_mode=HTML, reply_markup=kb)


async def cmd_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.effective_message.reply_text(
            f"Currently {get_settings(chat_id)['tz']}. Change with "
            "<code>/tz America/Toronto</code>", parse_mode=HTML)
        return
    try:
        ZoneInfo(context.args[0])
    except ZoneInfoNotFoundError:
        await update.effective_message.reply_text("I don't know that timezone — use an IANA name.")
        return
    update_settings(chat_id, tz=context.args[0])
    schedule_digest(context.application, chat_id)
    text, kb = view_settings(chat_id)
    await update.effective_message.reply_text(text, parse_mode=HTML, reply_markup=kb)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    update_settings(chat_id, paused=1)
    schedule_digest(context.application, chat_id)
    text, kb = view_settings(chat_id)
    await update.effective_message.reply_text(text, parse_mode=HTML, reply_markup=kb)


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    update_settings(chat_id, paused=0)
    schedule_digest(context.application, chat_id)
    text, kb = view_settings(chat_id)
    await update.effective_message.reply_text(text, parse_mode=HTML, reply_markup=kb)


async def do_export(hid: int, send) -> bool:
    with closing(db()) as conn:
        specials = conn.execute(
            """SELECT s.id, r.name AS restaurant, s.title, s.price, s.tags,
                      s.schedule_type, s.weekdays, s.on_date, s.favorite,
                      s.active, s.added_by_name
               FROM specials s JOIN restaurants r ON r.id = s.restaurant_id
               WHERE s.household_id = ? ORDER BY r.name""", (hid,)).fetchall()
        meals = conn.execute(
            "SELECT ate_on, restaurant, title, price, rating, eaten_by_name "
            "FROM eaten WHERE household_id = ? ORDER BY ate_on", (hid,)).fetchall()
    sent = False
    for rows, filename in ((specials, "specials.csv"), (meals, "meal_log.csv")):
        if not rows:
            continue
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(rows[0].keys())
        writer.writerows([tuple(r) for r in rows])
        await send(InputFile(io.BytesIO(buf.getvalue().encode()), filename=filename))
        sent = True
    return sent


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hid = household_for(update.effective_chat.id)
    if not await do_export(hid, update.effective_message.reply_document):
        await update.effective_message.reply_text("Nothing to export yet.")


# --------------------------------------------------------------------------
# guided add
# --------------------------------------------------------------------------

A_REST, A_ITEM, A_PRICE, A_DAYS, A_TAGS = range(5)


def weekday_keyboard(selected: set[int]) -> InlineKeyboardMarkup:
    def cell(i: int) -> InlineKeyboardButton:
        return btn(f"{'✅ ' if i in selected else ''}{WEEKDAY_NAMES[i]}", f"ad:tog:{i}")
    return InlineKeyboardMarkup([
        [cell(0), cell(1), cell(2), cell(3)],
        [cell(4), cell(5), cell(6)],
        [btn("Every day", "ad:all"), btn("Weekdays", "ad:wk"), btn("Just today", "ad:today")],
        [btn("✔️ Done", "ad:done"), btn("Always available", "ad:none")],
    ])


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    household_for(chat_id, who(update))
    if context.args:
        await add_lines(update, context, " ".join(context.args))
        return ConversationHandler.END
    context.user_data["draft"] = {}
    context.user_data["days"] = set()
    await update.effective_message.reply_text("🏪 Which restaurant?\n<i>/cancel to stop</i>",
                                    parse_mode=HTML)
    return A_REST


async def add_rest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["draft"]["restaurant"] = update.effective_message.text.strip()
    await update.effective_message.reply_text("🍽 What's the special?")
    return A_ITEM


async def add_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["draft"]["title"] = update.effective_message.text.strip()
    await update.effective_message.reply_text("💵 Price? (or send <code>-</code>)", parse_mode=HTML)
    return A_PRICE


async def add_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.effective_message.text.strip().lower()
    if raw in {"-", "skip", "none"}:
        context.user_data["draft"]["price"] = None
    else:
        try:
            context.user_data["draft"]["price"] = parse_price(raw)
        except ParseError as exc:
            await update.effective_message.reply_text(f"{exc} Try again, or send <code>-</code>.",
                                            parse_mode=HTML)
            return A_PRICE
    await update.effective_message.reply_text("📆 Which days?", reply_markup=weekday_keyboard(set()))
    return A_DAYS


async def add_days_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    selected: set[int] = context.user_data.setdefault("days", set())
    draft = context.user_data["draft"]

    if action.startswith("tog:"):
        selected.symmetric_difference_update({int(action.split(":")[1])})
        await query.edit_message_reply_markup(weekday_keyboard(selected))
        return A_DAYS
    if action == "all":
        draft.update(schedule_type="weekly", weekdays="0,1,2,3,4,5,6", on_date=None)
    elif action == "wk":
        draft.update(schedule_type="weekly", weekdays="0,1,2,3,4", on_date=None)
    elif action == "today":
        draft.update(schedule_type="date", weekdays="",
                     on_date=today_for(update.effective_chat.id).isoformat())
    elif action == "none":
        draft.update(schedule_type="always", weekdays="", on_date=None)
    elif action == "done":
        if not selected:
            await query.answer("Pick a day, or tap Always available.", show_alert=True)
            return A_DAYS
        draft.update(schedule_type="weekly",
                     weekdays=",".join(str(d) for d in sorted(selected)), on_date=None)

    await query.edit_message_text(
        f"📆 {esc(schedule_label(draft))}\n\n🏷 Any tags? (comma separated)",
        parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup([[btn("No tags", "ad:notags")]]))
    return A_TAGS


async def finish_add(update: Update, context: ContextTypes.DEFAULT_TYPE, tags: str) -> str:
    chat_id = update.effective_chat.id
    hid = household_for(chat_id)
    label = who(update)
    draft = context.user_data.pop("draft")
    context.user_data.pop("days", None)
    draft["tags"] = tags
    insert_special(hid, draft, chat_id, label)
    price = f" · {money(draft['price'])}" if draft["price"] is not None else ""
    await notify_others(
        context, hid, chat_id,
        f"➕ <b>{esc(label or 'Someone')}</b> added {dish_emoji(draft['title'], tags)} "
        f"<b>{esc(draft['title'])}</b> at {esc(draft['restaurant'])} "
        f"<i>({esc(schedule_label(draft))})</i>")
    return (
        f"✅ Saved {dish_emoji(draft['title'], tags)} <b>{esc(draft['title'])}</b>\n"
        f"🏪 {esc(draft['restaurant'])}{price}\n📆 {esc(schedule_label(draft))}"
    )


async def add_tags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.effective_message.text.strip()
    tags = "" if raw.lower() in {"-", "skip", "none"} else clean_tags(raw)
    msg = await finish_add(update, context, tags)
    await update.effective_message.reply_text(
        msg, parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup([[btn("🏠 Home", "home")]]))
    return ConversationHandler.END


async def add_notags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    msg = await finish_add(update, context, "")
    await update.callback_query.edit_message_text(
        msg, parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup([[btn("🏠 Home", "home")]]))
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("draft", None)
    context.user_data.pop("days", None)
    await update.effective_message.reply_text("Dropped it.")
    return ConversationHandler.END


async def add_lines(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    chat_id = update.effective_chat.id
    label = who(update)
    hid = household_for(chat_id, label)
    tz = chat_tz(chat_id)
    added, errors = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        try:
            data = parse_special_line(line, tz)
            insert_special(hid, data, chat_id, label)
            added.append(data)
        except ParseError as exc:
            errors.append(str(exc))

    if not added and not errors:
        return
    out = []
    if added:
        out.append(f"✅ <b>Added {len(added)}</b>\n{RULE}")
        out.append("<blockquote>" + "\n".join(
            f"{dish_emoji(d['title'], d['tags'])} {esc(d['title'])}"
            f"{' · ' + money(d['price']) if d['price'] is not None else ''}"
            f"\n     <i>{esc(d['restaurant'])} · {esc(schedule_label(d))}</i>"
            for d in added[:15]) + "</blockquote>")
        if len(added) > 15:
            out.append(f"<i>…and {len(added) - 15} more</i>")
        await notify_others(
            context, hid, chat_id,
            f"➕ <b>{esc(label or 'Someone')}</b> added {len(added)} "
            f"special{'s' if len(added) > 1 else ''} to the board.")
    out += [f"⚠️ {e}" for e in errors]
    await update.effective_message.reply_text(
        "\n".join(out), parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup([[btn("🍽 Today", "today"), btn("🏠 Home", "home")]]))


async def on_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ignore edits: re-running an edited paste would duplicate everything
    if update.message is None:
        return
    text = update.message.text or ""

    rid = context.user_data.pop("rename_rid", None)
    if rid is not None:
        new = text.strip()
        hid = household_for(update.effective_chat.id)
        with closing(db()) as conn:
            rest = conn.execute(
                "SELECT name FROM restaurants WHERE id = ? AND household_id = ?",
                (rid, hid)).fetchone()
        if rest is None:
            await update.message.reply_text("That place is gone.")
            return
        result = apply_rename(hid, rest["name"], new)
        await update.message.reply_text(
            result, parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[btn("◀ Places", "browse"),
                                                btn("🏠 Home", "home")]]))
        return

    if "|" in text:
        await add_lines(update, context, text)


# --------------------------------------------------------------------------
# callback router
# --------------------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id
    hid = household_for(chat_id, who(update))
    data = query.data or ""
    await query.answer()

    if data == "home":
        return await show(update, *view_home(chat_id))
    if data == "today":
        return await show(update, *view_day(chat_id, today_for(chat_id)))
    if data == "week":
        return await show(update, *view_week(chat_id))
    if data == "browse":
        return await show(update, *view_browse(chat_id))
    if data == "stats":
        return await show(update, *view_stats(chat_id))
    if data == "settings":
        return await show(update, *view_settings(chat_id))
    if data == "house":
        return await show(update, *view_household(chat_id, context.bot.username))

    if data == "addhint":
        text = (
            f"➕ <b>Adding specials</b>\n{RULE}\n"
            "Send one line per special:\n"
            "<code>Place | Item | Price | Days | Tags</code>\n\n"
            "<code>Otto's | Wing night | 0.75 | thu</code>\n"
            "<code>Khaki | Dizi | 24.99 | sat | stew</code>\n\n"
            "Paste a whole block at once. Everyone in the household sees it.\n"
            "Or use /add for a guided walkthrough."
        )
        return await show(update, text, InlineKeyboardMarkup([[btn("🏠 Home", "home")]]))

    if data == "renamehint":
        return await show(
            update,
            "✏️ Name the household with\n<code>/sethouse The Kitchen Table</code>",
            InlineKeyboardMarkup([[btn("◀ Back", "house")]]))

    if data == "askleave":
        return await show(
            update,
            "🚪 <b>Leave this household?</b>\n\nYou'll start a fresh, empty board. "
            "The shared one stays for everyone else — rejoin any time with the code.",
            InlineKeyboardMarkup([[btn("Yes, leave", "leave"), btn("Cancel", "house")]]))

    if data == "leave":
        label = who(update)
        with closing(db()) as conn:
            conn.execute("DELETE FROM members WHERE chat_id = ?", (chat_id,))
            conn.commit()
        await notify_others(context, hid, chat_id,
                            f"👋 <b>{esc(label or 'Someone')}</b> left the household.")
        household_for(chat_id, label)
        return await show(update, *view_home(chat_id))

    if data.startswith("mrg:"):
        src = int(data[4:])
        if src != hid:
            merge_households(src, hid)
        await notify_others(context, hid, chat_id,
                            f"📦 <b>{esc(who(update) or 'Someone')}</b> brought their "
                            f"specials into the shared board.")
        return await show(update, *view_home(chat_id))

    if data == "noop":
        return

    if data.startswith("ord:"):
        return await show(update, *view_reorder(chat_id, int(data[4:])))

    if data.startswith("mv:"):
        _, wd, sid, direction = data.split(":")
        weekday = int(wd)
        move_in_order(hid, weekday, next_date_for(chat_id, weekday), int(sid),
                      -1 if direction == "u" else 1)
        return await show(update, *view_reorder(chat_id, weekday))

    if data.startswith("ordreset:"):
        weekday = int(data[9:])
        clear_order(hid, weekday)
        return await show(update, *view_reorder(chat_id, weekday))

    if data.startswith("rmv:"):
        _, rid, direction = data.split(":")
        move_restaurant(hid, int(rid), -1 if direction == "u" else 1)
        return await show(update, *view_browse(chat_id))

    if data.startswith("day:"):
        return await show(update, *view_day(chat_id, dt.date.fromisoformat(data[4:])))
    if data.startswith("roll:"):
        return await show(update, *view_roll(chat_id, dt.date.fromisoformat(data[5:])))
    if data.startswith("logmenu:"):
        return await show(update, *view_logmenu(chat_id, dt.date.fromisoformat(data[8:])))
    if data.startswith("rest:"):
        context.user_data.pop("rename_rid", None)
        return await show(update, *view_restaurant(chat_id, int(data[5:])))
    if data.startswith("sp:"):
        return await show(update, *view_special(chat_id, int(data[3:])))

    if data.startswith("fav:"):
        sid = int(data[4:])
        with closing(db()) as conn:
            row = conn.execute(
                "SELECT favorite FROM specials WHERE id = ? AND household_id = ?",
                (sid, hid)).fetchone()
            if row:
                conn.execute("UPDATE specials SET favorite = ? WHERE id = ?",
                             (0 if row["favorite"] else 1, sid))
                conn.commit()
        return await show(update, *view_special(chat_id, sid))

    if data.startswith("ren:"):
        rid = int(data[4:])
        with closing(db()) as conn:
            rest = conn.execute(
                "SELECT name FROM restaurants WHERE id = ? AND household_id = ?",
                (rid, hid)).fetchone()
        if rest is None:
            return await show(update, *view_browse(chat_id))
        context.user_data["rename_rid"] = rid
        return await show(
            update,
            f"✏️ Renaming <b>{esc(rest['name'])}</b>\n{RULE}\n"
            "Send me the new name on its own — just the name, nothing else.",
            InlineKeyboardMarkup([[btn("Cancel", f"rest:{rid}")]]))

    if data.startswith("mute:"):
        rid = int(data[5:])
        with closing(db()) as conn:
            row = conn.execute(
                "SELECT muted FROM restaurants WHERE id = ? AND household_id = ?",
                (rid, hid)).fetchone()
            if row:
                conn.execute("UPDATE restaurants SET muted = ? WHERE id = ?",
                             (0 if row["muted"] else 1, rid))
                conn.commit()
        return await show(update, *view_restaurant(chat_id, rid))

    if data.startswith("askdel:"):
        sid = int(data[7:])
        row = get_special(hid, sid)
        if row is None:
            return await show(update, *view_home(chat_id))
        return await show(
            update,
            f"🗑 Remove <b>{esc(row['title'])}</b> from {esc(row['restaurant'])}?\n"
            f"<i>This affects everyone in the household.</i>",
            InlineKeyboardMarkup([[btn("Yes, delete", f"del:{sid}"),
                                   btn("Cancel", f"sp:{sid}")]]))

    if data.startswith("del:"):
        sid = int(data[4:])
        row = get_special(hid, sid)
        with closing(db()) as conn:
            conn.execute("UPDATE specials SET active = 0 WHERE id = ? AND household_id = ?",
                         (sid, hid))
            conn.commit()
        if row:
            await notify_others(
                context, hid, chat_id,
                f"🗑 <b>{esc(who(update) or 'Someone')}</b> removed "
                f"<b>{esc(row['title'])}</b> from {esc(row['restaurant'])}.")
            return await show(update, *view_restaurant(chat_id, row["restaurant_id"]))
        return await show(update, *view_home(chat_id))

    if data.startswith("log:"):
        result = record_meal(chat_id, hid, int(data[4:]), chat_id, who(update))
        if result is None:
            return await show(update, "That one's gone from the board.",
                              InlineKeyboardMarkup([[btn("🏠 Home", "home")]]))
        return await show(update, *view_rating(*result))

    if data.startswith("rate:"):
        _, log_id, stars = data.split(":")
        with closing(db()) as conn:
            conn.execute("UPDATE eaten SET rating = ? WHERE id = ? AND household_id = ?",
                         (int(stars), int(log_id), hid))
            conn.commit()
        return await show(update, *view_stats(chat_id))

    if data.startswith("time:"):
        update_settings(chat_id, digest_time=data[5:], paused=0)
        schedule_digest(context.application, chat_id)
        return await show(update, *view_settings(chat_id))

    if data == "togglepause":
        cfg = get_settings(chat_id)
        update_settings(chat_id, paused=0 if cfg["paused"] else 1)
        schedule_digest(context.application, chat_id)
        return await show(update, *view_settings(chat_id))

    if data == "export":
        if not await do_export(hid, query.message.reply_document):
            await query.message.reply_text("Nothing to export yet.")
        return


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("home", "Dashboard"),
        BotCommand("today", "Today's specials"),
        BotCommand("tomorrow", "Tomorrow's specials"),
        BotCommand("week", "The week ahead"),
        BotCommand("roll", "Pick something for me"),
        BotCommand("browse", "Browse places and set their order"),
        BotCommand("order", "Rank a day\u2019s choices"),
        BotCommand("add", "Add a special"),
        BotCommand("find", "Search specials"),
        BotCommand("cheap", "Today's board under a price"),
        BotCommand("ate", "Log a meal"),
        BotCommand("stats", "Spend, streak, who's eating"),
        BotCommand("invite", "Share the board with someone"),
        BotCommand("join", "Join a shared board"),
        BotCommand("household", "Who's in this household"),
        BotCommand("settings", "Your digest time and timezone"),
        BotCommand("help", "How this works"),
    ])
    with closing(db()) as conn:
        chat_ids = [r["chat_id"] for r in conn.execute("SELECT chat_id FROM members")]
    for chat_id in chat_ids:
        get_settings(chat_id)
        schedule_digest(app, chat_id)
    log.info("restored %d digest job(s)", len(chat_ids))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something broke on my end.")


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN first (get one from @BotFather).")

    init_db()
    app = ApplicationBuilder().token(token).post_init(post_init).build()

    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            A_REST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_rest)],
            A_ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_item)],
            A_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_price)],
            A_DAYS: [CallbackQueryHandler(add_days_cb, pattern=r"^ad:")],
            A_TAGS: [CallbackQueryHandler(add_notags, pattern=r"^ad:notags$"),
                     MessageHandler(filters.TEXT & ~filters.COMMAND, add_tags)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
        per_message=False,
    ))

    for name, fn in [
        ("start", cmd_start), ("home", cmd_home), ("help", cmd_help),
        ("today", cmd_today), ("tomorrow", cmd_tomorrow), ("week", cmd_week),
        ("browse", cmd_browse), ("menu", cmd_browse),
        ("find", cmd_find), ("cheap", cmd_cheap), ("roll", cmd_roll),
        ("ate", cmd_ate), ("stats", cmd_stats), ("settings", cmd_settings),
        ("order", cmd_order), ("reorder", cmd_order),
        ("invite", cmd_invite), ("join", cmd_join), ("household", cmd_invite),
        ("leave", cmd_leave), ("sethouse", cmd_sethouse),
        ("rename", cmd_rename), ("settime", cmd_settime), ("tz", cmd_tz),
        ("pause", cmd_pause), ("resume", cmd_resume), ("export", cmd_export),
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_free_text))
    app.add_error_handler(on_error)

    log.info("PlateRoulette running — db at %s", DB_PATH)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()