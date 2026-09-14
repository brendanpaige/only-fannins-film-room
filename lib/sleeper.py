"""
Sleeper live-draft sync. Stdlib only (urllib) so `server.py` keeps its
"no dependencies" promise.

Sleeper's public API is read-only and needs no key:
  https://docs.sleeper.com/

Flow:
  resolve_user(name)            -> user_id
  find_league(user_id, season)  -> league dict (has league_id, draft_id)
  crosswalk(db)                 -> fill players.sleeper_id from /players/nfl
  sync(db, draft_id, my_user)   -> pull picks, write drafted/mine on draft_board

`sync` is what the dashboard polls. It is additive: it only ever sets
drafted/mine = 1 for players Sleeper says are gone. Clearing is still the
"Reset drafted" button's job.
"""

import json
import os
import re
import sqlite3
import ssl
import time
import urllib.request

API = "https://api.sleeper.app/v1"


def _ssl_context():
    """The python.org macOS build ships without a usable CA bundle unless
    the user ran 'Install Certificates.command', so fall back to the
    system bundle (or certifi if something pulled it in)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    for path in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):
        if os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


_SSL_CTX = _ssl_context()

# Sleeper -> our data. Only Jacksonville actually differs; OAK is a dead
# code Sleeper still ships on a few retired players.
TEAM_ALIAS = {"JAX": "JAC", "OAK": "LV", "LAR": "LAR", "LA": "LAR"}

_SUFFIX = re.compile(r"\s+(jr|sr|ii|iii|iv|v)\.?$", re.I)
_PUNCT = re.compile(r"[.'’]")

OFFENSE = ("QB", "RB", "WR", "TE", "K")

# Our board vs Sleeper disagree on a few display names. Keys and values
# are both post-norm(). Applied last inside norm() so both sides converge.
# Add to this as the pre-draft check (sleeper_setup.py) turns up misses.
NICKNAME = {
    "hollywood brown": "marquise brown",
}


def team_ours(code):
    if not code:
        return None
    code = code.upper()
    return TEAM_ALIAS.get(code, code)


def norm(name):
    """Loose name key: lowercase, no punctuation, no generational suffix."""
    if not name:
        return ""
    s = _PUNCT.sub("", name.lower()).strip()
    s = _SUFFIX.sub("", s)
    s = re.sub(r"\s+", " ", s)
    return NICKNAME.get(s, s)


def sleeper_pos(p):
    """The fantasy position we care about. Sleeper files two-way players
    (Travis Hunter) under 'DB' with the real slot in fantasy_positions."""
    pos = p.get("position")
    if pos in OFFENSE or pos in ("DEF", "DST"):
        return "DEF" if pos == "DST" else pos
    for fp in (p.get("fantasy_positions") or []):
        if fp in OFFENSE:
            return fp
        if fp in ("DEF", "DST"):
            return "DEF"
    return None


def _get(path, timeout=8):
    url = path if path.startswith("http") else f"{API}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "draft-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- discovery

def resolve_user(username):
    u = _get(f"/user/{username}")
    if not u or not u.get("user_id"):
        raise LookupError(f"no Sleeper user '{username}'")
    return u["user_id"]


def find_league(user_id, season, name_hint=None):
    leagues = _get(f"/user/{user_id}/leagues/nfl/{season}") or []
    if not leagues:
        raise LookupError(f"user has no {season} NFL leagues")
    if name_hint:
        for lg in leagues:
            if name_hint.lower() in (lg.get("name") or "").lower():
                return lg
    return leagues[0]


def list_drafts(user_id, season):
    """Every NFL draft the user is in this season -- league drafts AND
    mock drafts (mocks come back with league_id = None)."""
    return _get(f"/user/{user_id}/drafts/nfl/{season}") or []


def league_draft_id(league_id):
    drafts = _get(f"/league/{league_id}/drafts") or []
    return drafts[0]["draft_id"] if drafts else None


# ---------------------------------------------------------------- crosswalk

def _our_index(conn):
    """Build the lookup tables used to resolve a Sleeper player to ours."""
    rows = conn.execute(
        "SELECT player_id, name, team, position FROM players").fetchall()
    idx = {
        "by_name_pos": {},   # (norm_name, pos) -> [player_id, ...]
        "by_name": {},       # norm_name       -> {player_id, ...}
        "by_def_team": {},   # team code        -> player_id
    }
    for pid, name, team, pos in rows:
        if pos == "DEF":
            if team:
                idx["by_def_team"][team.upper()] = pid
        else:
            n = norm(name)
            idx["by_name_pos"].setdefault((n, pos), []).append(pid)
            idx["by_name"].setdefault(n, set()).add(pid)
    return idx


def _match(pos, first, last, full, team, idx):
    """Resolve one Sleeper player descriptor to our player_id, or None."""
    if pos == "DST":
        pos = "DEF"
    if pos == "DEF":
        return idx["by_def_team"].get(team_ours(team) or "", None)
    name = norm(full or f"{first or ''} {last or ''}".strip())
    hits = idx["by_name_pos"].get((name, pos))
    if hits:
        return _one(hits)
    # Position disagreed (Sleeper has Travis Hunter as DB) -- fall back to
    # a name-only hit, but only when it is unambiguous.
    any_hits = idx["by_name"].get(name)
    if any_hits and len(any_hits) == 1:
        return next(iter(any_hits))
    return None


def _one(pids):
    # Two of our players sharing a normalized name + position is rare
    # enough (and hard to disambiguate safely) that we just take the
    # first; a wrong tie here only ever mis-flags one deep player.
    return pids[0]


def crosswalk(db_path, players_blob=None):
    """Fill players.sleeper_id by matching /players/nfl to our table.
    Returns (matched, total_players, [our rows still unresolved]).
    Idempotent -- clears and rebuilds the column every call."""
    blob = players_blob or _get(f"/players/nfl", timeout=30)
    conn = sqlite3.connect(db_path)
    try:
        idx = _our_index(conn)
        cur = conn.cursor()
        cur.execute("UPDATE players SET sleeper_id = NULL")
        taken = set()
        # Two passes so a rostered player always beats a retired namesake
        # with no team.
        for teamed_only in (True, False):
            for sid, p in blob.items():
                if bool(p.get("team")) != teamed_only:
                    continue
                pos = sleeper_pos(p)
                if not pos:
                    continue
                pid = _match(pos, p.get("first_name"), p.get("last_name"),
                             p.get("full_name"), p.get("team"), idx)
                if pid and pid not in taken:
                    cur.execute(
                        "UPDATE players SET sleeper_id = ? WHERE player_id = ?",
                        (str(sid), pid))
                    taken.add(pid)
        matched = len(taken)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        unresolved = conn.execute(
            "SELECT name, team, position FROM players "
            "WHERE sleeper_id IS NULL ORDER BY position, name").fetchall()
        return matched, total, unresolved
    finally:
        conn.close()


# ---------------------------------------------------------------- live sync

# The draft's order/settings never change once set and its status changes
# only a handful of times, so cache it briefly. That way a ~1s dashboard
# poll costs one Sleeper request (the picks list) instead of two.
_META_CACHE = {}   # draft_id -> (fetched_at, meta)
_META_TTL = 5


def _draft_meta(draft_id):
    hit = _META_CACHE.get(draft_id)
    if hit and time.time() - hit[0] < _META_TTL:
        return hit[1]
    meta = _draft_meta_fetch(draft_id)
    _META_CACHE[draft_id] = (time.time(), meta)
    return meta


def _draft_meta_fetch(draft_id):
    d = _get(f"/draft/{draft_id}")
    order = d.get("draft_order") or {}          # user_id -> slot
    s2r = d.get("slot_to_roster_id") or {}
    settings = d.get("settings") or {}
    total = int(settings.get("rounds", 0)) * int(settings.get("teams", 0))
    return {
        "status": d.get("status"),
        "type": d.get("type"),
        "rounds": settings.get("rounds"),
        "teams": settings.get("teams"),
        "total_picks": total,
        "start_time": d.get("start_time"),
        "draft_order": order,
        "slot_to_roster_id": s2r,
    }


def sync(db_path, draft_id, my_user_id=None):
    """Pull every pick, map to our players, mark drafted / mine.
    Returns a summary dict the dashboard renders as a status line."""
    meta = _draft_meta(draft_id)
    picks = _get(f"/draft/{draft_id}/picks") or []

    my_slot = None
    if my_user_id and meta["draft_order"]:
        my_slot = meta["draft_order"].get(str(my_user_id))

    conn = sqlite3.connect(db_path)
    try:
        idx = _our_index(conn)
        sid_map = {
            str(s): pid for s, pid in conn.execute(
                "SELECT sleeper_id, player_id FROM players "
                "WHERE sleeper_id IS NOT NULL")
        }
        cur = conn.cursor()
        matched = mine = 0
        unmatched = []
        for pk in picks:
            m = pk.get("metadata") or {}
            pid = sid_map.get(str(pk.get("player_id")))
            if not pid:
                pid = _match(m.get("position"), m.get("first_name"),
                             m.get("last_name"), None, m.get("team"), idx)
            if not pid:
                nm = f"{m.get('first_name','')} {m.get('last_name','')}".strip()
                unmatched.append(f"{nm or pk.get('player_id')} "
                                 f"({m.get('position','?')}, pick {pk.get('pick_no')})")
                continue
            is_mine = False
            if my_user_id and pk.get("picked_by"):
                is_mine = str(pk["picked_by"]) == str(my_user_id)
            elif my_slot is not None:
                is_mine = pk.get("draft_slot") == my_slot
            cur.execute("INSERT OR IGNORE INTO draft_board (player_id) VALUES (?)",
                        (pid,))
            cur.execute(
                "UPDATE draft_board SET drafted = 1, "
                + ("mine = 1, " if is_mine else "")
                + "updated_at = CURRENT_TIMESTAMP WHERE player_id = ?",
                (pid,))
            matched += 1
            mine += 1 if is_mine else 0
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "draft_status": meta["status"],
        "picks_made": len(picks),
        "total_picks": meta["total_picks"],
        "matched": matched,
        "mine": mine,
        "my_slot": my_slot,
        "on_the_clock_pick": (len(picks) + 1
                              if meta["status"] in ("drafting", "paused") else None),
        "unmatched": unmatched,
    }
