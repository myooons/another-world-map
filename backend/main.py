import os
import json
import time
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, abort
from flask_cors import CORS
from supabase import create_client, Client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
CACHE_HOURS = 6

AGE_MIN, AGE_MAX = 30, 90
COLOR_YOUNG = (0xFF, 0xD7, 0x00)
COLOR_OLD   = (0x11, 0x11, 0x11)

app = Flask(__name__)
CORS(app)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# in-memory Wikidata cache
_cache: dict = {}


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
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < CACHE_HOURS * 3600:
        return entry["data"]

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
    _cache[key] = {"data": data, "ts": time.time()}
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
    row = supabase.table("suggestions").insert({
        "user_name": user_name,
        "topic": topic,
        "description": description,
        "votes": 0,
    }).execute()
    return jsonify(row.data[0]), 201


@app.route("/api/suggestions")
def list_suggestions():
    rows = supabase.table("suggestions").select("*").order("votes", desc=True).execute()
    return jsonify(rows.data)


@app.route("/api/vote/<int:sid>", methods=["POST"])
def vote(sid):
    existing = supabase.table("suggestions").select("votes").eq("id", sid).execute()
    if not existing.data:
        abort(404, "Suggestion not found")
    new_votes = existing.data[0]["votes"] + 1
    row = supabase.table("suggestions").update({"votes": new_votes}).eq("id", sid).execute()
    return jsonify(row.data[0])


@app.route("/api/hall-of-fame")
def hall_of_fame():
    rows = supabase.table("suggestions").select("user_name, votes").execute()
    totals: dict = {}
    for r in rows.data:
        totals[r["user_name"]] = totals.get(r["user_name"], 0) + r["votes"]
    ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:10]
    return jsonify([{"user_name": u, "total_votes": v} for u, v in ranked])


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=8080)
