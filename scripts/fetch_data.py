import os
import json
import requests
from datetime import datetime, timezone
from supabase import create_client

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {
    "Accept": "application/sparql-results+json",
    "User-Agent": "AnotherWorldMap/1.0 (https://github.com/myooons/another-world-map)",
}

AGE_MIN, AGE_MAX = 30, 90
COLOR_YOUNG = (0xFF, 0xD7, 0x00)
COLOR_OLD   = (0x11, 0x11, 0x11)

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


def sparql_fetch(query: str):
    resp = requests.get(SPARQL_ENDPOINT, params={"query": query}, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.json()["results"]["bindings"]


def main():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    sb = create_client(url, key)

    # Leaders
    print("Fetching leaders...")
    bindings = sparql_fetch(LEADERS_SPARQL)
    seen = {}
    for b in bindings:
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
    leaders = list(seen.values())
    print(f"  {len(leaders)} leaders processed")

    # Parliament
    print("Fetching parliament...")
    bindings = sparql_fetch(PARLIAMENT_SPARQL)
    parliament = []
    for b in bindings:
        avg = float(b["avgAge"]["value"])
        parliament.append({
            "iso2": b["isoAlpha2"]["value"],
            "country": b["countryLabel"]["value"],
            "average_age": round(avg, 1),
            "hex_color": interpolate_color(avg),
        })
    print(f"  {len(parliament)} countries processed")

    # Upsert to Supabase
    now = datetime.now(timezone.utc).isoformat()
    sb.table("wikidata_cache").upsert({"key": "leaders",    "data": leaders,    "updated_at": now}, on_conflict="key").execute()
    sb.table("wikidata_cache").upsert({"key": "parliament", "data": parliament, "updated_at": now}, on_conflict="key").execute()
    print("Saved to Supabase.")


if __name__ == "__main__":
    main()
