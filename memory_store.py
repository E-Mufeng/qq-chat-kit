"""按 partner_id 隔离的本地持久记忆（SQLite，纯本地、不联网、不上传）。

设计要点（防串号核心）：
- 每个对象一条 partners 记录，靠 partner_id（QQ号）严格分库。
- 上下文拼装只取「该对象档案 + 该对象最近 N 轮」，物理上不混入其他对象。
- 数据落在项目内 data/memory.db，权限默认，不触云。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "memory.db")
MAX_TURNS = 20  # 每个对象保留的最近对话轮数

_SCHEMA = """
CREATE TABLE IF NOT EXISTS partners (
    partner_id     TEXT PRIMARY KEY,
    display_name   TEXT DEFAULT '',
    stage          TEXT DEFAULT '初识',
    interests      TEXT DEFAULT '',
    last_topic     TEXT DEFAULT '',
    emotional_state TEXT DEFAULT '',
    dont_repeat    TEXT DEFAULT '[]',
    key_events     TEXT DEFAULT '[]',
    created_at     REAL,
    updated_at     REAL
);
CREATE TABLE IF NOT EXISTS turns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id TEXT,
    role       TEXT,
    content    TEXT,
    ts         REAL
);
CREATE INDEX IF NOT EXISTS idx_turns_pid ON turns(partner_id);
"""


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.executescript(_SCHEMA)
    return c


def _now() -> float:
    return time.time()


# ---------- partners ----------

def get_partner(partner_id: str) -> dict | None:
    c = _conn()
    try:
        row = c.execute(
            "SELECT partner_id,display_name,stage,interests,last_topic,"
            "emotional_state,dont_repeat,key_events,created_at,updated_at "
            "FROM partners WHERE partner_id=?", (partner_id,)
        ).fetchone()
    finally:
        c.close()
    if not row:
        return None
    return {
        "partner_id": row[0], "display_name": row[1], "stage": row[2],
        "interests": row[3], "last_topic": row[4], "emotional_state": row[5],
        "dont_repeat": json.loads(row[6]), "key_events": json.loads(row[7]),
        "created_at": row[8], "updated_at": row[9],
    }


def upsert_partner(partner_id: str, **fields) -> None:
    """更新对象档案字段；不存在则新建。fields 限 partners 表已知列。"""
    allowed = {"display_name", "stage", "interests", "last_topic",
               "emotional_state", "dont_repeat", "key_events"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    # json 字段序列化
    for k in ("dont_repeat", "key_events"):
        if k in sets and not isinstance(sets[k], str):
            sets[k] = json.dumps(sets[k], ensure_ascii=False)
    now = _now()
    c = _conn()
    try:
        exists = c.execute("SELECT 1 FROM partners WHERE partner_id=?",
                           (partner_id,)).fetchone()
        if exists:
            cols = ", ".join(f"{k}=?" for k in sets)
            c.execute(
                f"UPDATE partners SET {cols}, updated_at=? WHERE partner_id=?",
                tuple(sets.values()) + (now, partner_id),
            )
        else:
            cols = ["partner_id"] + list(sets)
            vals = [partner_id] + list(sets.values())
            ph = ", ".join("?" for _ in cols)
            c.execute(
                f"INSERT INTO partners ({', '.join(cols)}, created_at, updated_at) "
                f"VALUES ({ph}, ?, ?)",
                vals + [now, now],
            )
        c.commit()
    finally:
        c.close()


def forget_partner(partner_id: str) -> None:
    """硬删除该对象全部档案与对话（防串号清理用）。"""
    c = _conn()
    try:
        c.execute("DELETE FROM partners WHERE partner_id=?", (partner_id,))
        c.execute("DELETE FROM turns WHERE partner_id=?", (partner_id,))
        c.commit()
    finally:
        c.close()


def list_partners() -> list[dict]:
    c = _conn()
    try:
        rows = c.execute(
            "SELECT partner_id,display_name,stage,last_topic,emotional_state "
            "FROM partners ORDER BY updated_at DESC"
        ).fetchall()
    finally:
        c.close()
    return [
        {"partner_id": r[0], "display_name": r[1], "stage": r[2],
         "last_topic": r[3], "emotional_state": r[4]}
        for r in rows
    ]


# ---------- turns（滚动窗口） ----------

def append_turn(partner_id: str, role: str, content: str) -> None:
    c = _conn()
    try:
        c.execute("INSERT INTO turns(partner_id,role,content,ts) VALUES(?,?,?,?)",
                  (partner_id, role, content, _now()))
        # 超出窗口的旧轮次删除（保持每对象最多 MAX_TURNS 轮）
        cnt = c.execute("SELECT COUNT(*) FROM turns WHERE partner_id=?",
                        (partner_id,)).fetchone()[0]
        if cnt > MAX_TURNS:
            excess = cnt - MAX_TURNS
            old = c.execute(
                "SELECT id FROM turns WHERE partner_id=? ORDER BY ts ASC LIMIT ?",
                (partner_id, excess),
            ).fetchall()
            for (oid,) in old:
                c.execute("DELETE FROM turns WHERE id=?", (oid,))
        c.commit()
    finally:
        c.close()


def get_recent_turns(partner_id: str, n: int = MAX_TURNS) -> list[dict]:
    c = _conn()
    try:
        rows = c.execute(
            "SELECT role,content FROM turns WHERE partner_id=? "
            "ORDER BY ts ASC LIMIT ?", (partner_id, n)
        ).fetchall()
    finally:
        c.close()
    return [{"role": r[0], "content": r[1]} for r in rows]


# ---------- 上下文拼装（严格单对象） ----------

def build_context(partner_id: str) -> str:
    """只拼该对象的档案 + 最近 N 轮，绝不混入其他对象数据。"""
    p = get_partner(partner_id)
    lines = []
    if p:
        lines.append(f"对象档案（partner_id={partner_id}）：")
        lines.append(f"  称呼/备注：{p['display_name'] or '（未命名）'}")
        lines.append(f"  关系阶段：{p['stage']}")
        lines.append(f"  兴趣：{p['interests'] or '（未知）'}")
        lines.append(f"  上次话题：{p['last_topic'] or '（无）'}")
        lines.append(f"  情绪状态：{p['emotional_state'] or '（未知）'}")
        if p["dont_repeat"]:
            lines.append(f"  别重复：{', '.join(p['dont_repeat'])}")
        if p["key_events"]:
            lines.append(f"  关键事件：{'; '.join(p['key_events'])}")
    turns = get_recent_turns(partner_id)
    if turns:
        lines.append("最近对话：")
        for t in turns:
            who = "对方" if t["role"] == "user" else "我"
            lines.append(f"  {who}：{t['content']}")
    return "\n".join(lines) if lines else "（暂无该对象记忆）"
