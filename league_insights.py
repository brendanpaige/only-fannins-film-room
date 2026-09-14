#!/usr/bin/env python3
"""
Pull the Sleeper league's season into league/league_data.js for the Film
Room dashboard (league/film_room.html).

    python3 league_insights.py                # Only Fannin's
    python3 league_insights.py <league_id>    # any other Sleeper league

Re-run it whenever you want fresh numbers (mid-game works: live games are
pulled with their game clock). Then open league/film_room.html directly --
no server needed, the data ships as a plain <script>.

Sources, all public and key-free:
  api.sleeper.app   league, rosters, users, matchups, draft, transactions
  api.sleeper.com   per-player weekly stats and projections
  ESPN scoreboard   kickoff times and live game clocks (Sleeper has neither)

Player points are scored here with the league's own scoring_settings, so
free agents and projections are on the same scale as Sleeper's matchup
points (checked against matchup players_points: identical).

Stdlib only, like server.py.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

from lib.sleeper import _SSL_CTX, _get

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "league")
CACHE_DIR = os.path.join(OUT_DIR, "cache")
OUT_JS = os.path.join(OUT_DIR, "league_data.js")

DEFAULT_LEAGUE = "1377872888375296000"   # Only Fannin's, 2026
SLEEPER_STATS = "https://api.sleeper.com"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
ESPN_ALIAS = {"WSH": "WAS"}          # ESPN -> Sleeper team codes
FREE_AGENTS_KEPT = 15                # best unrostered scorers per week


# ------------------------------------------------------------------ fetch

def espn_get(url, timeout=15):
    """ESPN's Akamai edge 403s lib.sleeper._get's custom User-Agent (and a
    browser one sent from Python) but accepts urllib's default. Sleeper's
    Cloudflare is the opposite, so Sleeper hosts keep using _get."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return json.loads(r.read().decode())


def players_blob():
    """/players/nfl is ~15 MB; Sleeper asks callers to pull it at most daily."""
    path = os.path.join(CACHE_DIR, "players_nfl.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
        with open(path) as f:
            return json.load(f)
    blob = _get("/players/nfl", timeout=90)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(blob, f)
    return blob


def stat_rows(kind, season, week):
    """kind is 'stats' or 'projections'. Returns {player_id: row}."""
    pos = "&".join(f"position%5B%5D={p}" for p in POSITIONS)
    rows = _get(f"{SLEEPER_STATS}/{kind}/nfl/{season}/{week}"
                f"?season_type=regular&{pos}", timeout=30)
    return {r["player_id"]: r for r in rows}


def espn_games(season, week):
    data = espn_get(f"{ESPN_SCOREBOARD}?dates={season}&seasontype=2&week={week}")
    games = []
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        side = {c["homeAway"]: c for c in comp["competitors"]}
        status = ev["status"]
        phase = status["type"]["state"]          # pre / in / post
        if phase == "post":
            progress = 1.0
        elif phase == "in":
            period = min(status.get("period") or 1, 4)
            left = float(status.get("clock") or 0)
            progress = max(0.0, min(0.99, ((period - 1) * 900 + (900 - left)) / 3600))
        else:
            progress = 0.0

        def code(c):
            abbr = c["team"]["abbreviation"]
            return ESPN_ALIAS.get(abbr, abbr)

        away, home = code(side["away"]), code(side["home"])
        games.append({
            "key": f"{away}@{home}",
            "away": away, "home": home,
            "as": int(side["away"].get("score") or 0),
            "hs": int(side["home"].get("score") or 0),
            "kickoff": ev["date"],
            "state": phase,
            "progress": round(progress, 3),
            "detail": status["type"].get("shortDetail", ""),
        })
    return sorted(games, key=lambda g: g["kickoff"])


# ---------------------------------------------------------------- scoring

def score(stats, rules):
    return round(sum(v * rules[k] for k, v in (stats or {}).items()
                     if k in rules and isinstance(v, (int, float))), 2)


# ------------------------------------------------------------------ build

def build(league_id):
    state = _get("/state/nfl")
    league = _get(f"/league/{league_id}")
    season = league["season"]
    rules = league["scoring_settings"]
    slots = [s for s in league["roster_positions"] if s not in ("BN", "IR", "TAXI")]
    users = {u["user_id"]: u for u in _get(f"/league/{league_id}/users")}
    rosters = _get(f"/league/{league_id}/rosters")
    picks = _get(f"/draft/{league['draft_id']}/picks") if league.get("draft_id") else []
    blob = players_blob()

    last_week = int(state.get("display_week") or state.get("week") or 0)
    if state.get("season") != season or state.get("season_type") == "pre":
        last_week = 0

    referenced = set()
    teams = []
    for r in rosters:
        u = users.get(r.get("owner_id"), {})
        s = r.get("settings") or {}
        teams.append({
            "rid": r["roster_id"],
            "owner": u.get("display_name") or f"Team {r['roster_id']}",
            "name": (u.get("metadata") or {}).get("team_name") or u.get("display_name")
                    or f"Team {r['roster_id']}",
            "user_id": r.get("owner_id"),
        })

    draft = []
    for p in picks:
        draft.append([p["pick_no"], p["round"], p["roster_id"], p["player_id"]])
        referenced.add(p["player_id"])

    weeks = []
    for week in range(1, last_week + 1):
        games = espn_games(season, week)
        if not games or all(g["state"] == "pre" for g in games):
            continue
        stats = stat_rows("stats", season, week)
        proj = stat_rows("projections", season, week)
        team_game = {}
        for g in games:
            team_game[g["away"]] = team_game[g["home"]] = g["key"]

        def game_of(pid):
            row = stats.get(pid) or proj.get(pid) or {}
            team = row.get("team") or (blob.get(pid) or {}).get("team") or pid
            return team_game.get(team)

        matchups = {}
        rostered = set()
        pts_all = {pid: score(r.get("stats"), rules) for pid, r in stats.items()}
        for m in _get(f"/league/{league_id}/matchups/{week}"):
            pp = m.get("players_points") or {}
            pts_all.update(pp)                  # Sleeper's own numbers win
            starters = m.get("starters") or []
            players = m.get("players") or []
            rostered.update(players)
            referenced.update(players)
            side = {
                "rid": m["roster_id"],
                "pts": round(m.get("points") or 0, 2),
                "starters": [[pid, slots[i] if i < len(slots) else "FLEX"]
                             for i, pid in enumerate(starters)],
                "bench": [p for p in players if p not in starters],
            }
            matchups.setdefault(m.get("matchup_id"), []).append(side)

        free_agents = sorted((pid for pid in pts_all if pid not in rostered),
                             key=lambda p: -pts_all[p])[:FREE_AGENTS_KEPT]
        referenced.update(free_agents)

        wanted = rostered | set(free_agents) | {d[3] for d in draft}
        players = {}
        for pid in wanted:
            players[pid] = [round(pts_all.get(pid, 0), 2),
                            score((proj.get(pid) or {}).get("stats"), rules),
                            game_of(pid)]

        txs = []
        for t in _get(f"/league/{league_id}/transactions/{week}"):
            if t.get("status") != "complete":
                continue
            txs.append({
                "type": t["type"],
                "adds": t.get("adds") or {},
                "drops": t.get("drops") or {},
                "ts": t.get("status_updated") or t.get("created"),
                "bid": (t.get("settings") or {}).get("waiver_bid"),
            })
            referenced.update((t.get("adds") or {}).keys())
            referenced.update((t.get("drops") or {}).keys())

        weeks.append({
            "week": week,
            "games": games,
            "matchups": [{"mid": mid, "sides": sides}
                         for mid, sides in sorted(matchups.items(),
                                                  key=lambda kv: (kv[0] is None, kv[0] or 0))],
            "players": players,
            "free_agents": free_agents,
            "transactions": txs,
        })

    directory = {}
    for pid in referenced:
        p = blob.get(pid) or {}
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid
        directory[pid] = {"n": name, "p": p.get("position") or "?",
                          "t": p.get("team") or "FA"}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "league": {
            "id": league_id,
            "name": league["name"],
            "season": season,
            "status": league.get("status"),
            "teams": league["settings"].get("num_teams"),
            "slots": slots,
            "playoff_week_start": league["settings"].get("playoff_week_start"),
            "scoring": {k: rules.get(k) for k in ("rec", "rush_fd", "rec_fd", "rush_att", "pass_td")},
        },
        "teams": teams,
        "draft": draft,
        "players": directory,
        "weeks": weeks,
    }


def main():
    league_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LEAGUE
    data = build(league_id)
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_JS, "w") as f:
        f.write("window.LEAGUE_DATA = ")
        json.dump(data, f, separators=(",", ":"))
        f.write(";\n")
    weeks = data["weeks"]
    print(f"{data['league']['name']}: {len(weeks)} week(s) with games, "
          f"{len(data['draft'])} draft picks, {len(data['players'])} players")
    for w in weeks:
        pending = [g["key"] for g in w["games"] if g["state"] != "post"]
        print(f"  week {w['week']}: {len(w['matchups'])} matchups"
              + (f", not final: {', '.join(pending)}" if pending else ", all games final"))
    print(f"wrote {os.path.relpath(OUT_JS, ROOT)} ({os.path.getsize(OUT_JS) // 1024} KB)")


if __name__ == "__main__":
    main()
