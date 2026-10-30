"""KHL League updater.

Reads the official KHL standings and full season schedule from khl.ru and saves
them to the Supabase database. Runs on GitHub Actions every hour (see
.github/workflows/update.yml) and can also be run on your own computer:

    pip install -r updater/requirements.txt
    python -m playwright install chromium
    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python updater/update.py

khl.ru builds its pages with JavaScript and protects its data API with a
session token, so the script opens the site in a headless Chromium browser
and asks the site's own API for the data, exactly like the page does.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import requests
from playwright.sync_api import sync_playwright

KHL = "https://www.khl.ru"

# khl.ru club id -> our club code (must match supabase/schema.sql)
CLUB_IDS = {
    1: "LOK", 2: "CSKA", 7: "SPA", 24: "SKA", 25: "TRK", 26: "TOR", 29: "SIB",
    34: "AVG", 37: "MMG", 38: "SAL", 53: "AKB", 54: "AMU", 56: "SEV", 66: "LAD",
    71: "NKH", 190: "AVT", 198: "BAR", 207: "DMN", 418: "ADM", 451: "SOC",
    568: "DRA", 719: "DYN",
}

MSK = dt.timezone(dt.timedelta(hours=3))

# Runs inside the khl.ru page: calls the site's own API with its session token.
FETCH_JS = """
async (path) => {
  const sid = (window.BX && BX.message) ? BX.message('bitrix_sessid') : null;
  if (!sid) return {error: 'no_session_token'};
  const body = new URLSearchParams({sessid: sid});
  if (path === 'standings/regular') body.set('type', 'regular');
  const r = await fetch('/rest/' + path + '/', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
              'X-Requested-With': 'XMLHttpRequest'},
    body,
  });
  if (!r.ok) return {error: 'http_' + r.status};
  return await r.json();
}
"""


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def fetch_khl() -> tuple[dict, dict]:
    """Return (standings_data, calendar_data) from the khl.ru API."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            locale="ru-RU",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"),
        )
        try:
            page.goto(KHL + "/standings/", wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_function("window.BX && BX.message && BX.message('bitrix_sessid')", timeout=60_000)
        except Exception as exc:  # noqa: BLE001
            browser.close()
            fail(f"Could not open khl.ru ({exc}). The site may be down or blocking automated visits.")
        standings = page.evaluate(FETCH_JS, "standings/regular")
        calendar = page.evaluate(FETCH_JS, "calendar/list")
        browser.close()

    for name, resp in (("standings", standings), ("calendar", calendar)):
        if not isinstance(resp, dict) or resp.get("error") or resp.get("status") != "success":
            fail(f"khl.ru refused the {name} request: {resp if not isinstance(resp, dict) else resp.get('error') or resp.get('errors')}")
    return standings["data"], calendar["data"]


def parse_standings(data: dict) -> list[dict]:
    try:
        rows = data["json"]["league"][0]["rows"]
    except (KeyError, IndexError, TypeError):
        fail("The KHL standings format has changed (no league table found).")
    out = []
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for row in rows:
        cid = row.get("clubid")
        if cid is None:
            continue  # divider row between playoff and non-playoff places
        code = CLUB_IDS.get(int(cid))
        if not code:
            fail(f"Unknown KHL club id {cid} ({row.get('name')}). Add it to CLUB_IDS and to the clubs table.")
        s = row["s"]
        rec = {
            "code": code, "gp": int(s["gp"]), "w": int(s["w"]), "otw": int(s["otw"]), "sow": int(s["sow"]),
            "sol": int(s["sol"]), "otl": int(s["otl"]), "otl_en": int(s.get("otl_en", 0)), "l": int(s["l"]),
            "goals": str(s.get("g", "0-0")), "updated_at": now,
        }
        if rec["w"] + rec["otw"] + rec["sow"] + rec["sol"] + rec["otl"] + rec["otl_en"] + rec["l"] != rec["gp"]:
            fail(f"Standings numbers for {code} don't add up; not saving anything.")
        out.append(rec)
    if len(out) != len(CLUB_IDS):
        fail(f"Expected {len(CLUB_IDS)} clubs in the standings, found {len(out)}.")
    return out


def live_status(g: dict) -> str:
    """Best-effort label for an in-progress game, e.g. '2nd period' or 'Overtime'."""
    ots = (g.get("ots") or "").upper()
    if ots == "OT":
        return "Overtime"
    if ots == "SO":
        return "Shootout"
    for key in ("period_name", "periodName", "status_text", "statusText", "period", "currentPeriod", "status"):
        val = g.get(key)
        if val in (None, "", 0):
            continue
        if isinstance(val, int) or (isinstance(val, str) and val.isdigit()):
            n = int(val)
            return {1: "1st period", 2: "2nd period", 3: "3rd period", 4: "Overtime", 5: "Shootout"}.get(n, f"Period {n}")
        return str(val)
    periods = g.get("scP") or []
    if periods:
        n = len(periods)
        return {1: "1st period", 2: "2nd period", 3: "3rd period"}.get(n, "Overtime")
    return "In progress"


def parse_matches(data: dict) -> list[dict]:
    weeks = data.get("GAMES") or []
    out = []
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for week in weeks:
        if not week:
            continue
        for date_str, games in week.items():
            for g in games or []:
                home, away = CLUB_IDS.get(int(g.get("teama", 0))), CLUB_IDS.get(int(g.get("teamb", 0)))
                if not home or not away:
                    continue
                time_msk = (g.get("time_format") or "").strip()
                kickoff = None
                try:
                    hh, mm = (int(x) for x in time_msk.split(":"))
                    d = dt.date.fromisoformat(g.get("date") or date_str)
                    kickoff = dt.datetime(d.year, d.month, d.day, hh, mm, tzinfo=MSK).isoformat()
                except ValueError:
                    pass
                final = g.get("approved") == 1 and g.get("homeScore") is not None
                # A game with a score that is not yet approved is in progress.
                live = (not final) and g.get("homeScore") is not None
                ots = (g.get("ots") or "").upper()
                out.append({
                    "id": int(g["id"]),
                    "match_date": g.get("date") or date_str,
                    "time_msk": time_msk,
                    "kickoff": kickoff,
                    "home": home,
                    "away": away,
                    "home_score": int(g["homeScore"]) if final else None,
                    "away_score": int(g["visitorScore"]) if final else None,
                    "result_type": (ots if ots in ("OT", "SO") else "R") if final else None,
                    "final": final,
                    "live": live,
                    "live_home": int(g["homeScore"]) if live else None,
                    "live_away": int(g["visitorScore"]) if live and g.get("visitorScore") is not None else (0 if live else None),
                    "live_status": live_status(g) if live else None,
                    "updated_at": now,
                })
    if not out:
        fail("No matches found in the KHL calendar.")
    return out


class Supabase:
    def __init__(self) -> None:
        url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not url or not key:
            fail("Set SUPABASE_URL and SUPABASE_SERVICE_KEY (GitHub: Settings -> Secrets and variables -> Actions).")
        self.base = url + "/rest/v1/"
        self.h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def select(self, table: str, query: str) -> list[dict]:
        r = requests.get(self.base + table + "?" + query, headers=self.h, timeout=30)
        if not r.ok:
            fail(f"Reading {table} failed: {r.status_code} {r.text[:300]}")
        return r.json()

    def upsert(self, table: str, rows: list[dict], key: str) -> None:
        h = dict(self.h, Prefer="resolution=merge-duplicates,return=minimal")
        for i in range(0, len(rows), 500):
            r = requests.post(f"{self.base}{table}?on_conflict={key}", headers=h, json=rows[i:i + 500], timeout=60)
            if not r.ok:
                fail(f"Saving {table} failed: {r.status_code} {r.text[:300]}")


def main() -> None:
    db = Supabase()
    standings_raw, calendar_raw = fetch_khl()
    standings = parse_standings(standings_raw)
    matches = parse_matches(calendar_raw)

    # Safety check: games played never goes down during a season.
    old = {r["code"]: r["gp"] for r in db.select("standings", "select=code,gp")}
    for rec in standings:
        if rec["gp"] < old.get(rec["code"], 0):
            fail(f"{rec['code']} shows fewer games than before ({rec['gp']} < {old[rec['code']]}); not saving.")

    # Only send matches that are new or changed, to keep live updates light.
    fields = ("match_date", "time_msk", "kickoff", "home", "away", "home_score", "away_score", "result_type", "final",
              "live", "live_home", "live_away", "live_status")
    existing = {r["id"]: r for r in db.select("matches", "select=id," + ",".join(fields))}

    def same(new: dict, old: dict | None) -> bool:
        if old is None:
            return False
        for f in fields:
            a, b = new.get(f), old.get(f)
            if f == "kickoff" and a and b:
                a, b = dt.datetime.fromisoformat(a), dt.datetime.fromisoformat(b)
            if a != b:
                return False
        return True

    changed = [m for m in matches if not same(m, existing.get(m["id"]))]

    db.upsert("standings", standings, "code")
    if changed:
        db.upsert("matches", changed, "id")
    played = sum(1 for m in matches if m["final"])
    print(f"Saved standings for {len(standings)} clubs; {len(changed)} of {len(matches)} matches new or changed ({played} played).")


if __name__ == "__main__":
    main()
