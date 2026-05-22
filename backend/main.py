import sqlite3
import json
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, abort
from flask_cors import CORS

DATABASE_FILE = "suggestions.db"
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
CACHE_HOURS = 6

AGE_MIN, AGE_MAX = 30, 90
COLOR_YOUNG = (0xFF, 0xD7, 0x00)  # #FFD700
COLOR_OLD   = (0x11, 0x11, 0x11)  # #111111

app = Flask(__name__)
CORS(app)


# ── DB ───────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                ts TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name TEXT NOT NULL,
                topic TEXT NOT NULL,
                description TEXT,
                votes INTEGER DEFAULT 0
            )
        """)
        conn.commit()


# ── Helpers ──────────────────────────────────────────────────────────────────

def interpolate_color(age: float) -> str:
    t = max(0.0, min(1.0, (age - AGE_MIN) / (AGE_MAX - AGE_MIN)))
    r = int(COLOR_YOUNG[0] * (1 - t) + COLOR_OLD[0] * t)
    g = int(COLOR_YOUNG[1] * (1 - t) + COLOR_OLD[1] * t)
    b = int(COLOR_YOUNG[2] * (1 - t) + COLOR_OLD[2] * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def calc_age(birth_str: str):
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%Y"):
        try:
            bd = datetime.strptime(birth_str, fmt)
            today = datetime.now()
            return today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
        except ValueError:
            continue
    return None


def sparql_fetch(key: str, query: str):
    with get_db() as conn:
        row = conn.execute("SELECT data, ts FROM cache WHERE key=?", (key,)).fetchone()
        if row:
            ts = datetime.fromisoformat(row["ts"])
            if datetime.now() - ts < timedelta(hours=CACHE_HOURS):
                return json.loads(row["data"])

    resp = requests.get(
        SPARQL_ENDPOINT,
        params={"query": query, "format": "json"},
        headers={
            "Accept": "application/sparql-results+json",
            "User-Agent": "AnotherWorldMap/1.0 (https://github.com/myooons/another-world-map) python-requests",
        },
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json()

    with get_db() as conn:
        conn.execute(
            "REPLACE INTO cache (key, data, ts) VALUES (?,?,?)",
            (key, json.dumps(data), datetime.now().isoformat())
        )
        conn.commit()
    return data


# ── SPARQL queries ────────────────────────────────────────────────────────────

LEADERS_SPARQL = """
SELECT DISTINCT ?countryLabel ?isoAlpha2 ?leaderLabel ?birthDate WHERE {
  ?country wdt:P31 wd:Q6256 ;
           wdt:P297 ?isoAlpha2 ;
           wdt:P35 ?leader .
  ?leader wdt:P569 ?birthDate .
  MINUS { ?leader wdt:P570 [] }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
} ORDER BY ?countryLabel
"""

PARLIAMENT_SPARQL = """
SELECT ?countryLabel ?isoAlpha2 (AVG(?ageYears) AS ?avgAge) WHERE {
  ?country wdt:P31 wd:Q6256 ;
           wdt:P297 ?isoAlpha2 .
  ?member p:P39 ?stmt .
  ?stmt pq:P2937 ?legislature .
  ?legislature wdt:P17 ?country .
  ?member wdt:P31 wd:Q5 ;
          wdt:P569 ?birthDate .
  MINUS { ?member wdt:P570 [] }
  OPTIONAL { ?stmt pq:P582 ?endDate }
  FILTER(!BOUND(?endDate) || ?endDate > NOW())
  BIND(YEAR(NOW()) - YEAR(?birthDate) AS ?ageYears)
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
} GROUP BY ?countryLabel ?isoAlpha2
  HAVING (COUNT(?member) > 5)
  ORDER BY ?countryLabel
"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/leaders")
def get_leaders():
    data = sparql_fetch("leaders", LEADERS_SPARQL)
    seen = {}
    for b in data["results"]["bindings"]:
        iso2 = b["isoAlpha2"]["value"]
        if iso2 in seen:
            continue
        age = calc_age(b["birthDate"]["value"])
        if age is None:
            continue
        seen[iso2] = {
            "iso2": iso2,
            "country": b["countryLabel"]["value"],
            "leader_name": b["leaderLabel"]["value"],
            "age": age,
            "hex_color": interpolate_color(age),
        }
    return jsonify(list(seen.values()))


@app.route("/api/parliament")
def get_parliament():
    data = sparql_fetch("parliament", PARLIAMENT_SPARQL)
    results = []
    for b in data["results"]["bindings"]:
        avg = float(b["avgAge"]["value"])
        results.append({
            "iso2": b["isoAlpha2"]["value"],
            "country": b["countryLabel"]["value"],
            "average_age": round(avg, 1),
            "hex_color": interpolate_color(avg),
        })
    return jsonify(results)


@app.route("/api/suggest", methods=["POST"])
def create_suggestion():
    body = request.get_json(silent=True) or {}
    user_name = (body.get("user_name") or "").strip()
    topic = (body.get("topic") or "").strip()
    description = (body.get("description") or "").strip() or None
    if not user_name or not topic:
        abort(400, "user_name and topic required")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO suggestions (user_name, topic, description) VALUES (?,?,?)",
            (user_name, topic, description)
        )
        conn.commit()
        row = conn.execute("SELECT * FROM suggestions WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/suggestions")
def list_suggestions():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM suggestions ORDER BY votes DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/vote/<int:sid>", methods=["POST"])
def vote(sid):
    with get_db() as conn:
        conn.execute("UPDATE suggestions SET votes = votes + 1 WHERE id=?", (sid,))
        conn.commit()
        row = conn.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
    if not row:
        abort(404, "Suggestion not found")
    return jsonify(dict(row))


@app.route("/api/hall-of-fame")
def hall_of_fame():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT user_name, SUM(votes) AS total_votes
            FROM suggestions
            GROUP BY user_name
            ORDER BY total_votes DESC
            LIMIT 10
        """).fetchall()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
