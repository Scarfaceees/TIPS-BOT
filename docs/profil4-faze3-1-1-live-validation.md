# Profil 4 – Fáze 3.1.1: kontrola v ostrém provozu

Datum: 22. 11. 2026, 03:38–04:10 UTC. Výchozí stav: commit `5792a16` (Fáze 3.1), Docker `tipsbot` + `tipsbot-auto`.

Modely A–D, váhy, EV, settlement ani rozhodovací prahy se nezměnily. Nic se nevsadilo, žádné historické záznamy
se nesmazaly a vyhodnocené výsledky se nepřepsaly. Databázová migrace nebyla potřeba, jen se nasadil nový kód.
Zálohy z Fáze 3.1 jsou v `backups/*.20261122-0308.bak`.

**Ostrý rozbor:** jeden standardní hloubkový rozbor přes existující CLI (`tipsbot profil4 analyze 1557472`) proběhl
v kontejneru nad živou databází. Zápas Hull City – Brighton (Premier League, fixture 1557472, výkop 22. 11. 14:00 UTC).

**API:** první rozbor použil 3 požadavky (`fixtures`, `odds`, `injuries`). Ostatní šlo z cache. Rozpočet API Budget
Manageru: 6 161 zbývajících požadavků z 7 500. Dva opakované rozbory nespotřebovaly nic (`api_log` zůstal na id 515).

## Výsledky jednotlivých kontrol

### 1. Ranking – první ostrý zápis (ŽIVÁ DATA)

| Kontrola | Výsledek | Důkaz |
|---|---|---|
| Stav před rozborem | `rankings` = 0 řádků | `SELECT COUNT(*) FROM rankings` → 0 |
| Zápis po rozboru | **118 řádků**, id 1–118, všechny `computed_at 2026-11-22T03:39:08+00:00`, verze `rank-v1 (22.11.2026)`, `valid` = 1 | 118 různých trhů a výběrů = 118 kandidátů, protože `predictions` mají pro tento rozbor 118 řádků na každý model A–D |
| Vazba na zápas a výběr | ✓ | např. id 17: fixture 1557472, AH_0.25 home, pinnacle 3.42, ranking 61, kvalita 95, WATCH; id 29: AH_1.5 home, 1xbet 1.66, ranking 65, WATCH |
| Rozhodnutí | 113× NO BET, 5× WATCH, beze změny proti decision engine | `GROUP BY decision` |
| Opakovaný identický sken (03:41) | **PROBLÉM 1:** přibylo 17 řádků (id 119–135) se stejným kurzem, kvalitou i rozhodnutím; ranking klesl o 1 bod jen stárnutím kurzu (složka „aktuálnost kurzu“) | např. AH_1.5 home 65 → 64 při kurzu 1.66 v obou případech |
| Po opravě: třetí identický sken (03:5x) | **0 nových řádků**, 0 požadavků na API | `rankings` stále 135 |
| Nový záznam při změně kurzu, rozhodnutí nebo rankingu o ≥ 3 body | jen simulace: ostrý kurz se v cache do 06:39 nezmění | testy `test_rankings_saved_for_all_candidates_deduped_and_updated_on_change`, `test_odds_ageing_alone_does_not_create_ranking_rows` |
| Export XLSX čte nové záznamy | ✓ po opravě problému 3 | list Opportunity Ranking: 5 aktuálních příležitostí (64 AH +1,5; 61 AH +0,25; 60 DNB; 59 AH +0,5; 58 DC 1X, vše WATCH, OVĚŘENO, čas výpočtu, verze); historie posledního rankingu: 118 výběrů |

**PROBLÉM 2 (opraven):** nejvýše ohodnocený kandidát rozboru (ranking 73) byl **NO BET**: 1X2 domácí 6.73 u 1xbet,
kurz mimo pásmo 1,20–5,00. Panel by ho ukázal jako nejlepší aktuální příležitost. Vysoké konzervativní EV ho
vytáhlo nahoru, i když ho decision engine správně zamítl. Teď se NO BET mezi aktuálními příležitostmi nezobrazuje.
Zůstává v tabulce `rankings` a ve validaci rankingu. Váhy ani rozhodnutí se nezměnily.

### 2. Quality of Data – nový záznam od začátku do konce (ŽIVÁ DATA)

Nový tip **id 214**: Hull City – Brighton, AH_0.25 home (domácí +0,25), pinnacle 3.42, WATCH, uloženo
2026-11-22T03:39:08Z.

| Hodnota | API odpověď | Interní objekt / DB (`tips.intel.quality`) | Panel | Excel (Kvalita dat) |
|---|---|---|---|---|
| Kanceláří s „Home +0.25“ | 5 (Bet365 3.30, Marathonbet 3.34, BetVictor 3.25, Pinnacle 3.42, 1xBet 3.42) | `books` = 5 | 5 kanc. | 5 |
| Medián / odchylka | medián 3.34 → 3.42 je +2,4 % | „nejlepší kurz +2.4 % proti mediánu 5 kanceláří“ | ✓ | Market Integrity: medián 3.34, odchylka +2,4 % |
| Poslední aktualizace u zdroje | `update` 2026-11-22T01:06:22+00:00 | `odds_time` / `updated` 01:06:22Z | 02:06 (pražský čas v textovém výstupu) | 2026-11-22 01:06 (datum, UTC) |
| Čas stažení | cache `fetched_at` 03:39:09Z | `odds_fetched_at` 03:39:09Z | 04:39 (pražský čas) | 2026-11-22 03:39 |
| Stáří kurzu | – | `odds_age_min` 152.77 | 153 min | 152,77 |
| Protistrana | „Away +0.25“ ze stejné aktualizace | `counterside` = True | ověřena | ověřena |
| Absence | `injuries` 9 záznamů | AVAILABLE | – | AVAILABLE |
| Sestavy | žádná odpověď lineups (výkop za 10 h) | NOT_YET_AVAILABLE, nejistota „sestavy nepotvrzené“ (3/5 b., ne 0) | – | NOT_YET_AVAILABLE |
| Kvalita kurzu / sport. data / úplnost | – | 96 / 92 / 100 | – | 96 / 92 / 100 |
| Kvalita dat celkem | – | `tips.quality` = 95 | – | 95 |

- **NULL ≠ 0:** u všech 213 starších tipů zůstává `quality`, `rank` i `intel` NULL. V Excelu jsou buňky prázdné
  s poznámkou „tip před Fází 3 – … neukládaly“. Zpětně nebyla doplněna žádná hodnota (0 z 213).
- **Chybějící sestavy** snižují jen jistotu (5 → 3 b.) a jsou uvedené jako nejistota, ne jako negativní sportovní
  informace.
- **PROBLÉM 3 (opraven):** tip 214 je opakovaný záznam výběru tipu **211** (stejný výběr, předchozí kurz 3.45, bez
  kvality). List Kvalita dat ukazoval jen první záznam výběru (211), takže nová kvalita v exportu nebyla vidět.
  Teď list ukazuje všechny uložené záznamy s ID, časem uložení a sloupcem „Počítá se do výsledků“. Do ROI se
  stále počítá jen první záznam (211).
- Postřeh bez změny: složka „zpoždění stažení“ (6.9/10) u kurzu z API v praxi znovu měří jeho stáří (staženo
  153 min po aktualizaci), takže stáří kurzu se v kvalitě dat projeví dvakrát. Váhy se bez souhlasu nemění,
  jen to zaznamenávám jako možnou úpravu.

### 3. Market Integrity – ostrý kandidát (ŽIVÁ DATA)

| Kandidát | Kontrola | Výsledek |
|---|---|---|
| AH_0.25 home, pinnacle 3.42 (rankings id 17) | Trh a strana | API „Home +0.25“ = domácí +0,25 (linie z pohledu domácích, znaménko neotočené), protistrana „Away +0.25“ 1.34 |
| | Ruční nezávislý přepočet čtvrtinové linie: půl na 0, půl na +0,5 | z 1X2 modelu C (p1 = 0,1613; pX = 0,2133): a = p1 + ½pX, b = ½pX → EV = **0,022931**; uloženo 0,022931; férový kurz 3,3344 = uložený; p_eff 0,2999 = uložená p(C) |
| | Verdikt a rozhodnutí | OVĚŘENO, nezávislý přepočet „souhlasí“; WATCH (EV +2,3 % pod prahem 3 %) |
| AH_1.5 home, 1xbet 1.66 (id 29) | Strana, marže, EV | API „Home +1.5“ 1.66 / „Away +1.5“ 2.42, součet 101,6 % (jeden trh); EV = p·kurz − 1 = 0,022732 = uloženo; medián 1,59 z 8 kanceláří; OVĚŘENO; WATCH |
| 1X2 home, 1xbet 6.73 (id 1) | EV | 0,1613 · 6,73 − 1 = 0,085257 = uloženo; NO BET (kurz mimo pásmo) |
| Všech 118 kandidátů rozboru | Nezávislý přepočet (`integrity.validate`) | **118 kontrol, 0 neshod**: 108 OVĚŘENO, 10 PODEZŘELÝ KURZ (vždy NO BET, např. hosté −3,5 za 11.60) |
| Settlement (z Fáze 3.1, živá DB) | nezávislý přepočet ze skóre | 109 tipů a 15 032 předpovědí, 0 neshod |

### 4. Portfolio Risk (ŽIVÁ DATA)

Případ **Atalanta – Inter** (výkop 22. 11. 19:45): tipy 142 (1X2 hosté 1.65, WATCH), 143 (1X2 domácí 5.28, NO BET)
a 144 (AH 0 domácí 4.21, BEST AVAILABLE (WATCH)).

- Před opravou systém označil protichůdnost jen ve stejném trhu (1X2:1 × 1X2:2) a do čekajících pozic počítal
  i NO BET tip 143. To je **PROBLÉM 4**: CLI a panel hlásily 54 čekajících tipů, Excel je bez NO BET počítal správně.
- Po opravě (`never_both_win`: neexistuje skóre, při kterém by vyhrály oba, ověřeno nezávislým settlementem):
  - CLI a panel: „Atalanta – Inter – 2 tipů (1X2:2, AH_0.0:home) PROTICHŮDNÉ: 1X2:2 × AH_0.0:home“.
  - Čekajících virtuálních pozic je 40 na 20 zápasech; NO BET se nepočítá.
  - Excel Portfolio Risk: oba řádky „PROTICHŮDNÉ výběry stejného zápasu (nemohou vyhrát oba)“, virtuální vklad
    1 j., skutečný vklad prázdný.
- Korelace a koncentrace: 13 zápasů s více tipy, například Hull City – Brighton se 3 tipy: OU 3.5 under,
  AH +1,5 a AH +0,25.
- Virtuální a skutečné vklady jsou oddělené. Skutečné tikety (2, výsledek −20 Kč) jsou jen ve sloupcích Kč,
  virtuální jednotky ve sloupcích j.

### 5. Export a panel (ŽIVÁ DATA)

- **PROBLÉM 5 (opraven, vážný):** od prvního zápisu do `rankings` (03:39) export XLSX padal s HTTP 500
  (`can't compare offset-naive and offset-aware datetimes`). Panel předává čas hlavní databáze bez časového pásma
  a nový souhrn integrity ho porovnával s časem s pásmem. Testy Fáze 3.1 používaly jen čas s pásmem, proto chybu
  nezachytily. Oprava `_aware()` je v souhrnu integrity, aktuálním rankingu a portfoliu. Regresní test
  `test_export_with_naive_utc_now_from_main_database` chybu hlídá.
- **PROBLÉM 6 (opraven):** list Market Integrity byl oříznutý na 60 ze 118 řádků bez upozornění. Teď má Excel
  všechny řádky a panel píše „v panelu prvních 60 …, všechny jsou v Excelu“.
- Po opravách: `/export/xlsx`, `/export/csv` i `/export/pdf` vracejí 200.
  - XLSX má **15 listů**.
  - Opportunity Ranking: 5 aktuálních příležitostí, 118 výběrů v historii.
  - Kvalita dat: 214 řádků, z toho 1 s kompletní kvalitou (tip 214).
  - Market Integrity: 118 řádků, 0 neshod přepočtu.
- **Panel odpovídá databázi:**
  - ranking 64 a kvalita 94 u AH +1,5 (poslední řádek id 119+);
  - Market Integrity 108 OVĚŘENO / 10 PODEZŘELÝ KURZ / 118 výběrů;
  - Ranking Validation „MÁLO DAT – RANKING ZATÍM NEOVĚŘEN“ (0 vyhodnocených rankingů);
  - Portfolio 40 / 20 / 111.

## Opravy v této fázi

- `src/tipsbot/profil4/history.py`: `RANK_STEP = 3`. Samotné stárnutí kurzu nový řádek rankingu nezakládá.
- `src/tipsbot/profil4/intelligence.py`:
  - NO BET mimo aktuální příležitosti;
  - `_aware()` pro čas bez pásma;
  - `integrity_summary(limit=None)`;
  - `never_both_win` a protichůdnost napříč trhy;
  - portfolio bez NO BET (`pending_nobet`).
- `src/tipsbot/export.py`:
  - Kvalita dat ukazuje všechny uložené záznamy (ID, čas uložení, „Počítá se do výsledků“);
  - Market Integrity má všechny řádky;
  - protichůdnost v Portfolio Risk podle kódu výběru (`Rec.code`).
- `src/tipsbot/web.py`: upozornění na zkrácený seznam Market Integrity.
- `tests/test_profil4_phase31.py`: regresní testy problémů 1–6.

## Souhrn

| Ukazatel | Hodnota |
|---|---|
| Nových rankingových záznamů (ostrý provoz) | **135**: 118 z prvního rozboru a 17 z druhého, vzniklých před opravou problému 1. Historické řádky se nemažou. |
| Nových záznamů s kompletní kvalitou dat | **1 tip** (id 214) a 135 záznamů rankingu s uloženou kvalitou dat a detaily |
| Nezávislých kontrol Market Integrity | **118** automatických a 3 ruční z odpovědi API (AH +0,25, AH +1,5, 1X2) |
| Neshod nezávislého výpočtu | **0** |
| Nalezených problémů | 6, všechny opravené a pokryté testem (1 vážný: pád exportu) |
| Testy | **626 prošlo** (celá sada, 0 selhání; Fáze 3.1 měla 623, přibyly 3 regresní testy a 2 se rozšířily) |
| Docker | `tipsbot` a `tipsbot-auto` běží s opraveným kódem; export XLSX, CSV a PDF vrací 200 |

## Je fáze ověřena v ostrém provozu?

**Částečně.**

- **Ověřeno na živých datech:**
  - první zápis rankingu;
  - vazby a verze;
  - deduplikace po opravě;
  - kvalita dat nového tipu z API až do Excelu;
  - nezávislá kontrola integrity včetně ruční kontroly čtvrtinové linie;
  - portfolio na reálném protichůdném případu;
  - export a panel.
- **Ověřeno jen simulací:** nový záznam rankingu po změně kurzu nebo rozhodnutí.
- **Neověřeno:**
  - automatický ranní sken automatu s novým kódem (proběhne v 9:00);
  - **účinnost rankingu**, protože zatím žádný ranking není vyhodnocený. Ranking Validation je správně
    ve stavu „MÁLO DAT – RANKING ZATÍM NEOVĚŘEN“ a k závěru je potřeba aspoň 100 nezávislých zápasů.
