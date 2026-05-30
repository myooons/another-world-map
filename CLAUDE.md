# another-world-map

## 스택
React 18 (CDN, no build), D3.js v7, TopoJSON, Tailwind CSS (CDN), Supabase JS SDK, Google AdSense

## 구조
```
frontend/index.html   — 단일 파일 앱 (React + D3 choropleth map)
scripts/              — 데이터 수집 스크립트
docs/                 — 문서
.github/              — GitHub Actions (Wikidata 크론 등)
```

## 실행
빌드 없음. `frontend/index.html` 브라우저에서 직접 열거나 GitHub Pages 배포.

## 주요 컨텍스트
- 세계 지도 choropleth: 국가 지도자 나이 / 의회 평균 나이 시각화
- Wikidata SPARQL API로 데이터 수집 → Supabase `wikidata_cache` 테이블에 저장
- GitHub Actions 크론이 데이터 갱신 (scripts/ 내 Python)
- ISO numeric ↔ alpha-2 매핑 테이블이 index.html 내 인라인으로 존재
- Supabase URL: foawimpskcoxgunilpzf.supabase.co
- 탭: Leaders / Parliament / Suggestions / Hall of Fame
- 광고: Google AdSense (ca-pub-3931196285292252)
- 환경변수: Supabase URL/KEY (.env 없음, JS에서 직접 하드코딩 or GitHub Secrets)
