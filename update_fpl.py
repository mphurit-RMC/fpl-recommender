#!/usr/bin/env python3
"""Fetch official FPL bootstrap, fixtures and latest finished event points."""
from __future__ import annotations
import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

BASE = "https://fantasy.premierleague.com/api"
OUT = Path(__file__).with_name("data.json")
USER_AGENT = "Mozilla/5.0 (compatible; FPL-Recommender/1.0; +https://github.com/)"


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


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def per90(value, minutes):
    return round(number(value) * 90 / minutes, 2) if minutes else 0.0


def main():
    bootstrap = get_json("/bootstrap-static/")
    fixtures = get_json("/fixtures/")
    elements = bootstrap.get("elements", [])
    teams = {t["id"]: t for t in bootstrap.get("teams", [])}
    positions = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
    if len(elements) < 300 or len(teams) < 20:
        raise RuntimeError(f"Unexpectedly small FPL response: {len(elements)} players, {len(teams)} teams. Keeping previous data.json.")

    events = bootstrap.get("events", [])
    finished = [e for e in events if e.get("finished")]
    latest_finished = max(finished, key=lambda e: e.get("id", 0), default=None)
    latest_gw = latest_finished.get("id") if latest_finished else None
    live_points = {}
    if latest_gw:
        try:
            live = get_json(f"/event/{latest_gw}/live/")
            live_points = {x.get("id"): number((x.get("stats") or {}).get("total_points")) for x in live.get("elements", [])}
        except Exception as exc:
            # Live points are supplementary; do not lose the main refresh if this endpoint is temporarily unavailable.
            print(f"Warning: could not fetch GW{latest_gw} live points: {exc}", file=sys.stderr)

    players = []
    for p in elements:
        st = p.get("stats") or {}
        mins = int(number(st.get("minutes")))
        team = teams.get(p.get("team"), {})
        status = p.get("status") or "a"
        # Existing UI status codes: a=available, d=doubtful, i=injured, s=suspended, u=unavailable.
        if status not in {"a", "d", "i", "s", "u"}:
            status = "u"
        ict = number(st.get("ict_index"))
        xgi = number(st.get("expected_goal_involvements"))
        defensive = st.get("defensive_contribution", st.get("defensive_contributions", None))
        # If the official API does not publish a defensive-contribution field, keep it null;
        # the front end treats missing data as unavailable rather than inventing a value.
        dc90 = per90(defensive, mins) if defensive is not None else None
        players.append({
            "id": p.get("id"), "name": p.get("web_name") or p.get("second_name") or "Unknown",
            "first_name": p.get("first_name", ""), "second_name": p.get("second_name", ""),
            "team": team.get("short_name", "UNK"), "team_id": p.get("team"),
            "position": positions.get(p.get("element_type"), "MID"),
            "price": round(number(p.get("now_cost")) / 10, 1),
            "points": int(number(st.get("total_points"))), "form": round(number(st.get("form")), 1),
            "ownership": round(number(p.get("selected_by_percent")), 1),
            "expected_points": round(number(p.get("ep_next")), 1) if p.get("ep_next") not in (None, "") else None,
            "minutes": mins, "status": status, "news": p.get("news") or "",
            "xgi90": per90(xgi, mins), "dc90": dc90,
            "points90": per90(st.get("total_points"), mins),
            "ict": round(ict, 2), "influence90": per90(st.get("influence"), mins),
            "creativity90": per90(st.get("creativity"), mins), "threat90": per90(st.get("threat"), mins),
            "last_event_points": live_points.get(p.get("id")),
        })

    # Build four upcoming gameweeks from the official fixtures feed, preserving double gameweeks.
    current = next((e for e in events if e.get("is_current")), None)
    nxt = next((e for e in events if e.get("is_next")), None)
    first_gw = (current or nxt or {}).get("id", 1)
    if current and current.get("finished") and nxt:
        first_gw = nxt["id"]
    upcoming = sorted({f.get("event") for f in fixtures if f.get("event") and f.get("event") >= first_gw})[:4]
    fixture_map = {code: [] for code in [t.get("short_name") for t in teams.values()]}
    for f in fixtures:
        if f.get("event") not in upcoming or f.get("finished"):
            continue
        home = teams.get(f.get("team_h"), {})
        away = teams.get(f.get("team_a"), {})
        hcode, acode = home.get("short_name"), away.get("short_name")
        if hcode in fixture_map:
            fixture_map[hcode].append({"event": f["event"], "opponent": acode, "side": "H", "difficulty": f.get("team_h_difficulty")})
        if acode in fixture_map:
            fixture_map[acode].append({"event": f["event"], "opponent": hcode, "side": "A", "difficulty": f.get("team_a_difficulty")})
    for vals in fixture_map.values():
        vals.sort(key=lambda x: (x["event"], x["side"] != "H"))

    payload = {
        "meta": {"updated_at": datetime.now(timezone.utc).isoformat(), "season": bootstrap.get("game_settings", {}).get("season", ""),
                 "current_gameweek": first_gw, "latest_finished_gameweek": latest_gw,
                 "player_count": len(players), "fixture_gameweeks": upcoming,
                 "source": "Official Fantasy Premier League API", "defcon_available": any(p["dc90"] is not None for p in players)},
        "players": players, "fixtures": fixture_map,
        "teams": [{"id": t["id"], "name": t.get("name"), "short_name": t.get("short_name"),
                   "strength_attack_home": t.get("strength_attack_home"), "strength_attack_away": t.get("strength_attack_away"),
                   "strength_defence_home": t.get("strength_defence_home"), "strength_defence_away": t.get("strength_defence_away")}
                  for t in teams.values()],
    }
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT)  # Atomic replacement: a failed refresh never damages the last good snapshot.
    print(f"Updated {OUT.name}: {len(players)} players, GW{latest_gw} last finished, upcoming {upcoming}")


if __name__ == "__main__":
    main()
