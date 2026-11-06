#!/usr/bin/env python3
"""Refresh FPL data.json from the official Fantasy Premier League API.

The bootstrap-static endpoint exposes season statistics as top-level element
fields. Some mirrors/exported payloads may wrap them under `stats`, so this
script supports both shapes and refuses to publish an all-zero season snapshot.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://fantasy.premierleague.com/api"
OUT = Path(__file__).with_name("data.json")
USER_AGENT = "Mozilla/5.0 (compatible; FPL-Recommender/1.1; +https://github.com/)"
POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def get_json(path: str):
    url = BASE + path
    last_error = None
    for attempt in range(4):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(req, timeout=30) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status} from {url}")
                return json.loads(response.read().decode("utf-8"))
        except (URLError, HTTPError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"Unable to fetch {url}: {last_error}")


def number(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        result = float(value)
        return result if result == result else default  # reject NaN
    except (TypeError, ValueError):
        return default


def stat_value(player: dict, key: str, default=None):
    """Read a statistic from the official flat schema, with nested-stats fallback."""
    value = player.get(key)
    if value not in (None, ""):
        return value
    nested = player.get("stats")
    if isinstance(nested, dict):
        value = nested.get(key)
        if value not in (None, ""):
            return value
    return default


def per90(value, minutes: int) -> float:
    return round(number(value) * 90 / minutes, 2) if minutes > 0 else 0.0


def main() -> None:
    bootstrap = get_json("/bootstrap-static/")
    fixture_rows = get_json("/fixtures/")
    elements = bootstrap.get("elements", [])
    team_rows = bootstrap.get("teams", [])
    teams = {t["id"]: t for t in team_rows if t.get("id") is not None}

    if len(elements) < 300 or len(teams) < 20:
        raise RuntimeError(
            f"Unexpected FPL response: {len(elements)} players, {len(teams)} teams; "
            "keeping the previous data.json."
        )

    events = bootstrap.get("events", [])
    finished = [e for e in events if e.get("finished")]
    latest_finished = max(finished, key=lambda e: e.get("id", 0), default=None)
    latest_gw = latest_finished.get("id") if latest_finished else None

    # Latest event points are a supplementary field; season points remain total_points.
    live_points = {}
    if latest_gw:
        try:
            live = get_json(f"/event/{latest_gw}/live/")
            live_points = {
                item.get("id"): number((item.get("stats") or {}).get("total_points"))
                for item in live.get("elements", [])
            }
        except Exception as exc:  # keep the main refresh if this optional endpoint fails
            print(f"Warning: could not fetch GW{latest_gw} live points: {exc}", file=sys.stderr)

    players = []
    for p in elements:
        minutes = max(0, int(number(stat_value(p, "minutes"))))
        team = teams.get(p.get("team"), {})
        status = p.get("status") or "a"
        if status not in {"a", "d", "i", "s", "u"}:
            status = "u"

        total_points = max(0, int(number(stat_value(p, "total_points"))))
        form = round(number(stat_value(p, "form")), 1)
        ict = number(stat_value(p, "ict_index"))
        influence = number(stat_value(p, "influence"))
        creativity = number(stat_value(p, "creativity"))
        threat = number(stat_value(p, "threat"))
        xgi = number(stat_value(p, "expected_goal_involvements"))

        defensive = stat_value(p, "defensive_contribution")
        if defensive is None:
            defensive = stat_value(p, "defensive_contributions")
        dc90 = per90(defensive, minutes) if defensive is not None else None

        expected_next = p.get("ep_next")
        players.append({
            "id": p.get("id"),
            "name": p.get("web_name") or p.get("second_name") or "Unknown",
            "first_name": p.get("first_name", ""),
            "second_name": p.get("second_name", ""),
            "team": team.get("short_name", "UNK"),
            "team_id": p.get("team"),
            "position": POSITIONS.get(p.get("element_type"), "MID"),
            "price": round(number(p.get("now_cost")) / 10, 1),
            "points": total_points,
            "form": form,
            "ownership": round(number(p.get("selected_by_percent")), 1),
            "expected_points": round(number(expected_next), 1) if expected_next not in (None, "") else None,
            "minutes": minutes,
            "status": status,
            "news": p.get("news") or "",
            "xgi90": per90(xgi, minutes),
            "dc90": dc90,
            "points90": per90(total_points, minutes),
            "ict": round(ict, 2),
            "influence90": per90(influence, minutes),
            "creativity90": per90(creativity, minutes),
            "threat90": per90(threat, minutes),
            "last_event_points": live_points.get(p.get("id")),
        })

    # Quality gate: once at least one GW is finished, an all-zero stats snapshot
    # is almost certainly a schema/mapping failure. Never overwrite good data with it.
    played_count = sum(1 for p in players if p["minutes"] > 0)
    total_season_points = sum(p["points"] for p in players)
    if latest_gw and (played_count < 20 or total_season_points <= 0):
        raise RuntimeError(
            "Season-stat validation failed: "
            f"latest finished GW={latest_gw}, players with minutes={played_count}, "
            f"total season points={total_season_points}. Previous data.json was preserved."
        )

    current = next((e for e in events if e.get("is_current")), None)
    nxt = next((e for e in events if e.get("is_next")), None)
    first_gw = (current or nxt or {}).get("id")
    if first_gw is None:
        first_gw = (latest_gw + 1) if latest_gw else 1
    if current and current.get("finished") and nxt:
        first_gw = nxt["id"]

    upcoming = sorted({
        f.get("event") for f in fixture_rows
        if f.get("event") is not None and f.get("event") >= first_gw and not f.get("finished")
    })[:4]
    fixture_map = {t.get("short_name"): [] for t in team_rows if t.get("short_name")}
    for f in fixture_rows:
        if f.get("event") not in upcoming or f.get("finished"):
            continue
        home = teams.get(f.get("team_h"), {})
        away = teams.get(f.get("team_a"), {})
        home_code, away_code = home.get("short_name"), away.get("short_name")
        if home_code in fixture_map and away_code:
            fixture_map[home_code].append({
                "event": f["event"], "opponent": away_code, "side": "H",
                "difficulty": f.get("team_h_difficulty"),
            })
        if away_code in fixture_map and home_code:
            fixture_map[away_code].append({
                "event": f["event"], "opponent": home_code, "side": "A",
                "difficulty": f.get("team_a_difficulty"),
            })
    for fixtures in fixture_map.values():
        fixtures.sort(key=lambda item: (item["event"], item["side"] != "H"))

    payload = {
        "meta": {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "season": bootstrap.get("game_settings", {}).get("season", ""),
            "current_gameweek": first_gw,
            "latest_finished_gameweek": latest_gw,
            "player_count": len(players),
            "played_player_count": played_count,
            "total_season_points": total_season_points,
            "fixture_gameweeks": upcoming,
            "source": "Official Fantasy Premier League API",
            "defcon_available": any(p["dc90"] is not None for p in players),
        },
        "players": players,
        "fixtures": fixture_map,
        "teams": [{
            "id": t["id"], "name": t.get("name"), "short_name": t.get("short_name"),
            "strength_attack_home": t.get("strength_attack_home"),
            "strength_attack_away": t.get("strength_attack_away"),
            "strength_defence_home": t.get("strength_defence_home"),
            "strength_defence_away": t.get("strength_defence_away"),
        } for t in team_rows],
    }

    temp_path = OUT.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temp_path.replace(OUT)  # atomic replacement; the old snapshot survives a failed refresh

    print(
        f"Updated {OUT.name}: {len(players)} players; {played_count} with minutes; "
        f"{total_season_points} total season points; GW{latest_gw} last finished; "
        f"upcoming {upcoming}"
    )


if __name__ == "__main__":
    main()
