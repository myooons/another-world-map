import os
import time
import csv
import io
import requests
from datetime import datetime, timezone

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {
    "Accept": "application/sparql-results+json",
    "User-Agent": "AnotherWorldMap/1.0 (https://github.com/myooons/another-world-map)",
}

COLOR_YOUNG = (0xFF, 0xD7, 0x00)
COLOR_OLD   = (0x11, 0x11, 0x11)

# (v_min, v_max, reverse)  reverse=True → high value = yellow (good)
METRIC_SCALES = {
    "leaders":              (30,  90,   False),
    "median_age":           (15,  55,   False),
    "life_expectancy":      (50,  85,   True),
    "happiness":            (2.0, 8.0,  True),
    "press_freedom":        (0,   2,    False),
    "military_expenditure": (0,   10,   False),
    "corruption_index":     (0,   100,  True),
}

OWID_URLS = {
    "median_age":           "https://ourworldindata.org/grapher/median-age.csv?tab=chart&time=latest",
    "happiness":            "https://ourworldindata.org/grapher/happiness-cantril-ladder.csv?tab=chart&time=latest",
    "press_freedom":        "https://ourworldindata.org/grapher/freedom-of-the-press.csv?tab=chart&time=latest",
    "military_expenditure": "https://ourworldindata.org/grapher/military-expenditure-as-share-of-gdp.csv?tab=chart&time=latest",
    "corruption_index":     "https://ourworldindata.org/grapher/corruption-perception-index.csv?tab=chart&time=latest",
}

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

LIFE_EXPECTANCY_SPARQL = """
SELECT DISTINCT ?isoAlpha2 ?countryLabel ?value WHERE {
  ?country wdt:P31 wd:Q6256 ;
           wdt:P297 ?isoAlpha2 ;
           wdt:P2250 ?value .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
} ORDER BY ?isoAlpha2
"""

ISO3_MAP_SPARQL = """
SELECT ?iso2 ?iso3 WHERE {
  ?country wdt:P31 wd:Q6256 ;
           wdt:P297 ?iso2 ;
           wdt:P298 ?iso3 .
}
"""


def interpolate_color(value: float, v_min: float, v_max: float, reverse: bool = False) -> str:
    t = max(0.0, min(1.0, (value - v_min) / (v_max - v_min)))
    if reverse:
        t = 1.0 - t
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


def sparql_fetch(query: str, retries: int = 3):
    for attempt in range(retries):
        try:
            resp = requests.get(SPARQL_ENDPOINT, params={"query": query}, headers=HEADERS, timeout=90)
            resp.raise_for_status()
            return resp.json()["results"]["bindings"]
        except Exception as e:
            print(f"  Attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(15 * (attempt + 1))
    return None


def supabase_upsert(sb_url: str, sb_key: str, table: str, record: dict):
    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    resp = requests.post(f"{sb_url}/rest/v1/{table}", headers=headers, json=record, timeout=30)
    resp.raise_for_status()


def get_iso3_to_iso2():
    bindings = sparql_fetch(ISO3_MAP_SPARQL)
    if not bindings:
        return {}
    return {b["iso3"]["value"]: b["iso2"]["value"] for b in bindings}


def fetch_owid_latest(metric_key: str) -> dict:
    """Returns {iso3: (entity_name, value)} for most recent year per country."""
    resp = requests.get(OWID_URLS[metric_key], timeout=60,
                        headers={"User-Agent": HEADERS["User-Agent"]})
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    value_col = next((k for k in (reader.fieldnames or []) if k not in ("Entity", "Code", "Year")), None)
    if not value_col:
        return {}
    latest = {}  # iso3 -> (year, entity, value)
    for row in reader:
        iso3 = row.get("Code", "").strip()
        year_str = row.get("Year", "").strip()
        entity = row.get("Entity", "").strip()
        val_str = row.get(value_col, "").strip()
        if not iso3 or not year_str or not val_str:
            continue
        try:
            year = int(year_str)
            value = float(val_str)
        except ValueError:
            continue
        if iso3 not in latest or year > latest[iso3][0]:
            latest[iso3] = (year, entity, value)
    return {k: (v[1], v[2]) for k, v in latest.items()}


def main():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    now = datetime.now(timezone.utc).isoformat()

    # Leaders
    print("Fetching leaders...")
    bindings = sparql_fetch(LEADERS_SPARQL)
    if bindings is not None:
        v_min, v_max, reverse = METRIC_SCALES["leaders"]
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
                "hex_color": interpolate_color(age, v_min, v_max, reverse),
            }
        leaders = list(seen.values())
        print(f"  {len(leaders)} leaders")
        supabase_upsert(url, key, "wikidata_cache", {"key": "leaders", "data": leaders, "updated_at": now})
        print("  Saved.")
    else:
        print("  Failed, skipping.")

    # Life Expectancy (Wikidata)
    print("Fetching life expectancy...")
    bindings = sparql_fetch(LIFE_EXPECTANCY_SPARQL)
    if bindings is not None:
        v_min, v_max, reverse = METRIC_SCALES["life_expectancy"]
        seen = {}
        for b in bindings:
            iso2 = b["isoAlpha2"]["value"]
            if iso2 in seen:
                continue
            try:
                val = float(b["value"]["value"])
            except (ValueError, KeyError):
                continue
            seen[iso2] = {
                "iso2": iso2,
                "country": b["countryLabel"]["value"],
                "value": round(val, 1),
                "hex_color": interpolate_color(val, v_min, v_max, reverse),
            }
        life_exp = list(seen.values())
        print(f"  {len(life_exp)} countries")
        supabase_upsert(url, key, "wikidata_cache", {"key": "life_expectancy", "data": life_exp, "updated_at": now})
        print("  Saved.")
    else:
        print("  Failed, skipping.")

    # ISO3 → ISO2 mapping (for OWID data)
    print("Fetching ISO3 mapping...")
    iso3_to_iso2 = get_iso3_to_iso2()
    print(f"  {len(iso3_to_iso2)} entries")

    # OWID metrics
    for metric_key in ["median_age", "happiness", "press_freedom", "military_expenditure", "corruption_index"]:
        print(f"Fetching {metric_key}...")
        try:
            owid_data = fetch_owid_latest(metric_key)
            v_min, v_max, reverse = METRIC_SCALES[metric_key]
            result = []
            for iso3, (name, val) in owid_data.items():
                iso2 = iso3_to_iso2.get(iso3)
                if not iso2:
                    continue
                result.append({
                    "iso2": iso2,
                    "country": name,
                    "value": round(val, 2),
                    "hex_color": interpolate_color(val, v_min, v_max, reverse),
                })
            print(f"  {len(result)} countries")
            supabase_upsert(url, key, "wikidata_cache", {"key": metric_key, "data": result, "updated_at": now})
            print("  Saved.")
        except Exception as e:
            print(f"  Failed: {e}")

    print("Done.")


if __name__ == "__main__":
    main()
