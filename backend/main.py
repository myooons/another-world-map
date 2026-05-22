import asyncio
import time
import sqlite3
from datetime import datetime
from typing import List, Optional, Dict, Tuple

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DATABASE_FILE = "suggestions.db"
WIKIDATA_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
CACHE_TTL_SECONDS = 6 * 60 * 60

AGE_MIN = 30
AGE_MAX = 90
COLOR_YOUNG = "#FFD700"
COLOR_OLD = "#111111"


class Cache:
    def __init__(self, ttl_seconds: int):
        self._cache: Dict[str, Tuple] = {}
        self._lock = asyncio.Lock()
        self._ttl = ttl_seconds

    async def get(self, key: str):
        async with self._lock:
            if key in self._cache:
                value, timestamp = self._cache[key]
                if time.time() - timestamp < self._ttl:
                    return value
                del self._cache[key]
        return None

    async def set(self, key: str, value):
        async with self._lock:
            self._cache[key] = (value, time.time())


app_cache = Cache(ttl_seconds=CACHE_TTL_SECONDS)


def init_db():
    with sqlite3.connect(DATABASE_FILE) as conn:
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


def get_db():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb: Tuple[float, float, float]) -> str:
    return '#{:02x}{:02x}{:02x}'.format(
        int(max(0, min(255, rgb[0]))),
        int(max(0, min(255, rgb[1]))),
        int(max(0, min(255, rgb[2])))
    )


def interpolate_color(age: float) -> str:
    clamped = max(AGE_MIN, min(AGE_MAX, age))
    t = (clamped - AGE_MIN) / (AGE_MAX - AGE_MIN)
    r_min, g_min, b_min = hex_to_rgb(COLOR_YOUNG)
    r_max, g_max, b_max = hex_to_rgb(COLOR_OLD)
    return rgb_to_hex((
        r_min * (1 - t) + r_max * t,
        g_min * (1 - t) + g_max * t,
        b_min * (1 - t) + b_max * t,
    ))


def calculate_age(birth_date_str: str) -> Optional[int]:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            bd = datetime.strptime(birth_date_str, fmt)
            today = datetime.now()
            return today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
        except ValueError:
            continue
    return None


app = FastAPI(title="World Age Map API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

httpx_client = httpx.AsyncClient(timeout=30.0)


@app.on_event("startup")
async def startup():
    init_db()


@app.on_event("shutdown")
async def shutdown():
    await httpx_client.aclose()


class LeaderData(BaseModel):
    iso2: str
    country: str
    leader_name: str
    age: int
    hex_color: str


class ParliamentData(BaseModel):
    iso2: str
    country: str
    average_age: float
    hex_color: str


class SuggestionCreate(BaseModel):
    user_name: str = Field(..., min_length=1, max_length=100)
    topic: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)


class Suggestion(BaseModel):
    id: int
    user_name: str
    topic: str
    description: Optional[str]
    votes: int


class HallOfFameEntry(BaseModel):
    user_name: str
    total_votes: int


async def query_wikidata(query: str, cache_key: str) -> Dict:
    cached = await app_cache.get(cache_key)
    if cached:
        return cached
    try:
        resp = await httpx_client.post(
            WIKIDATA_SPARQL_ENDPOINT,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"}
        )
        resp.raise_for_status()
        data = resp.json()
        await app_cache.set(cache_key, data)
        return data
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Wikidata error: {e.response.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Network error: {str(e)}")


LEADERS_SPARQL = """
SELECT DISTINCT ?countryLabel ?isoAlpha2 ?leaderLabel ?birthDate WHERE {
  ?country wdt:P31 wd:Q6256 ;
           wdt:P297 ?isoAlpha2 ;
           wdt:P35 ?leader .
  ?leader wdt:P569 ?birthDate .
  MINUS { ?leader wdt:P570 [] }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
ORDER BY ?countryLabel
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
}
GROUP BY ?countryLabel ?isoAlpha2
HAVING (COUNT(?member) > 5)
ORDER BY ?countryLabel
"""


@app.get("/api/leaders", response_model=List[LeaderData])
async def get_leaders():
    data = await query_wikidata(LEADERS_SPARQL, "leaders")
    results = {}
    for b in data["results"]["bindings"]:
        iso2 = b["isoAlpha2"]["value"]
        if iso2 in results:
            continue
        age = calculate_age(b["birthDate"]["value"])
        if age is None:
            continue
        results[iso2] = LeaderData(
            iso2=iso2,
            country=b["countryLabel"]["value"],
            leader_name=b["leaderLabel"]["value"],
            age=age,
            hex_color=interpolate_color(age)
        )
    return list(results.values())


@app.get("/api/parliament", response_model=List[ParliamentData])
async def get_parliament():
    data = await query_wikidata(PARLIAMENT_SPARQL, "parliament")
    results = []
    for b in data["results"]["bindings"]:
        avg_age = float(b["avgAge"]["value"])
        results.append(ParliamentData(
            iso2=b["isoAlpha2"]["value"],
            country=b["countryLabel"]["value"],
            average_age=round(avg_age, 1),
            hex_color=interpolate_color(avg_age)
        ))
    return results


@app.post("/api/suggest", response_model=Suggestion, status_code=201)
async def create_suggestion(s: SuggestionCreate):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO suggestions (user_name, topic, description, votes) VALUES (?, ?, ?, 0)",
            (s.user_name, s.topic, s.description)
        )
        conn.commit()
        return Suggestion(id=cur.lastrowid, votes=0, **s.model_dump())


@app.get("/api/suggestions", response_model=List[Suggestion])
async def list_suggestions():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, user_name, topic, description, votes FROM suggestions ORDER BY votes DESC"
        ).fetchall()
        return [Suggestion(**dict(r)) for r in rows]


@app.post("/api/vote/{suggestion_id}", response_model=Suggestion)
async def vote(suggestion_id: int):
    with get_db() as conn:
        conn.execute("UPDATE suggestions SET votes = votes + 1 WHERE id = ?", (suggestion_id,))
        conn.commit()
        row = conn.execute(
            "SELECT id, user_name, topic, description, votes FROM suggestions WHERE id = ?",
            (suggestion_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Suggestion not found")
        return Suggestion(**dict(row))


@app.get("/api/hall-of-fame", response_model=List[HallOfFameEntry])
async def hall_of_fame():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT user_name, SUM(votes) AS total_votes FROM suggestions GROUP BY user_name ORDER BY total_votes DESC LIMIT 10"
        ).fetchall()
        return [HallOfFameEntry(**dict(r)) for r in rows]
