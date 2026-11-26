# Phase 3.4 – Decision Traceability & API Budget Control (NÁVRH, neimplementováno)

Stav k 26. 11. 2026, větev `dev`. Navazuje na `docs/phase-3-3-odds-freshness-audit.md`.

**Způsob práce:**
- Jen čtení kódu a databází: `profil4.db` a `tipsbot.db` otevřené v režimu `mode=ro`, zkušební zápis selhal.
- Žádný API dotaz, žádná změna produkce, prahů ani strategie.
- Implementace je samostatný krok a čeká na schválení.

Značení: **NAMĚŘENO** = dotaz nad produkčními daty; **KÓD** = zjištěno z kódu; **ODHAD**.

---

## 1. Shrnutí

| Otázka | Odpověď |
|---|---|
| Lze u tipu zpětně zjistit použitý kurz a jeho čas? | **Ano.** 550 z 552 tipů má přesně dohledatelný řádek v `odds_quotes`. |
| Pravděpodobnosti a fair odds? | **Částečně.** Uloženy jsou p A–D, fair odds C a p trhu. Chybí fair odds D a minimální kurz pro prah EV. |
| EV a splnění jednotlivých prahů? | **Ne.** EV C a D se ukládají, ale hodnoty prahů platných v daném okamžiku ne. Ukládá se jen `config_hash`, bez obsahu konfigurace. |
| Quality of Data a Robustness? | **Částečně.** Skóre je uloženo, Quality u 339 z 552 tipů (od 22. 11.). Složky robustnosti a stress scénáře uloženy nejsou. |
| Proč APPROVED / WATCH / NO BET? | **Tip sám důvody nemá.** Jsou jen v `rankings.detail.reasons_decision`, a to oříznuté na 6. Tip lze s rankingem spárovat u 338 z 552 tipů. |
| Byl tip zamítnut kvůli zastaralému kurzu? | **Jen nepřímo:** z `tips.odds_status` a textu důvodu. Strojově čitelný příznak ani protifaktická otázka „schválil by se s čerstvým kurzem?“ neexistuje. |
| Lze každé volání API přiřadit lize a účelu? | **API-Football ano** (`profil4.api_log`). **The Odds API ne.** Existuje jen poslední stav počítadla a hodnota `used` za den; 26. 11. mezi 01:56 a 06:00 UTC 5 kreditů nelze přiřadit. |
| Může historické rozhodnutí použít data stažená po rozhodnutí? | **V produkci nebyl únik nalezen.** `odds.latest(before=T)` ale filtruje podle času u poskytovatele, ne podle času stažení, takže by únik umožnil. Navíc `predicted_at` není skutečný okamžik rozhodnutí: u 413 z 552 tipů byl kurz stažen až po `predicted_at` (průměrně o 12 min, nejvýš o 34,7 min). |

**P0:** žádné. **P1:** 2 problémy. **P2:** 4 problémy. **P3:** 3 problémy (kap. 5).

---

## 2. Audit A – dohledatelnost rozhodnutí

### 2.1 Tok rozhodnutí (KÓD)

1. **Fotbal:**
   - `hunter.run_scan` (`profil4/hunter.py:114`) volá `analysis.deep_analysis` (`analysis.py`).
   - `gather` stáhne data, `build_candidates` sestaví kandidáty a pro každého zavolá `decision.decide`. Potom proběhne `decision.select`.
   - Po rozhodnutí `intelligence.annotate` doplní diagnostiku (rozhodnutí nemění).
2. **NHL:** `nhl.run` (`profil4/nhl.py:291`) volá `nhl.analyse`, ta `nhl.candidates`, a ta `decide`, `select` a `annotate`.
3. **Zápis (pro fotbal i NHL):** `history.record` (`profil4/history.py:36`) uloží:
   - `forecasts` (1X2);
   - `rankings` – všechny kandidáty, když se změní kurz, kancelář, kvalita nebo rozhodnutí, nebo ranking o víc než 3 body;
   - `tips` – schválené, kategorie A/B/C a BEST AVAILABLE;
   - `predictions`.

### 2.2 Co se ukládá a co chybí

| Údaj | V paměti (`Candidate`) | `tips` | `rankings` | Chybí |
|---|---|---|---|---|
| Kurz, kancelář, zdroj | ano | `odds`, `bookmaker`, `odds_source` | `odds`, `bookmaker` | odkaz na `odds_quotes.id` |
| Čas kurzu u poskytovatele / čas stažení | `quote.stamp` / `fetched_at` | `odds_time`, `odds_fetched_at` | jen `odds_time` | – |
| Stáří a stav kurzu | `quote_status` | `odds_age_min`, `odds_status` | jen `valid` | stav v `rankings` |
| Pravděpodobnosti A–D, p trhu | `p`, `p_market` | `probs`, `p_market` | `detail.p`, `detail.p_market` | – |
| Fair odds C / D | ano | jen C (`fair_odds`) | ne | fair odds D |
| Minimální kurz pro EV | `min_odds` | ne | ne | ano |
| EV C / D / proti trhu | `ev_c`, `ev_d`, `ev_market` | C, D | C, D | EV proti trhu |
| Hodnoty prahů | `cfg.decision` | jen `config_hash` | ne | obsah konfigurace k otisku |
| Jednotlivé kontroly (pravidlo, hodnota, práh, tvrdá/měkká, splněno) | jen jako texty | ne | ne | **strukturovaný seznam** |
| Důvody rozhodnutí | `reasons`, `risks` | **ne** | `detail.reasons_decision[:6]` | úplné důvody u tipu |
| Nezávislá kontrola (integrity) | `validation` | v `intel` | `detail.integrity` | – |
| Quality of Data (skóre, složky) | `intel.quality` | `quality`, `intel` | `quality`, `detail.quality` | – (do 22. 11. chybí) |
| Robustnost (skóre, složky, stress) | `robustness`, `robustness_parts`, `stress` | jen skóre | jen skóre | složky a scénáře |
| Pojistka Modelu A | `guard` | v `intel` | `detail.model_guard` | – |
| Potvrzující kanceláře, pokrytí trhu | `confirmed_by`, `market_covered` | ne | ne | ano |
| Skutečný okamžik rozhodnutí | – | `predicted_at` = začátek skenu | `computed_at` = totéž | `decided_at` |

**NAMĚŘENO (profil4.db, 26. 11.):**
- 552 tipů: APPROVED 111, WATCH 150, NO BET 55, BEST AVAILABLE (WATCH) 189, BEST AVAILABLE (NO BET) 47.
- `rankings` má 19 532 řádků od 22. 11. Všechny mají `reasons_decision`.
- Tip a řádek rankingu se stejným časem jde spárovat u 338 z 552 tipů. Zbytek tipů je starší než tabulka `rankings`.
- `predictions` má 54 501 řádků. Opakovaný sken je **přepisuje** (`ON CONFLICT DO UPDATE`), takže zůstane jen poslední předzápasový stav. Pro kalibraci to stačí, pro audit stopy ne.
- Šest tipů z 21. 11. (21:36 UTC, výkop 14:30) má kurz i rozhodnutí až po výkopu. Jde o známou chybu opravenou 22. 11. (`history.py:56`). Statistiky je už vylučují (`history.py:460`, `intelligence.py:542`), takže tu nic dalšího dělat netřeba.

### 2.3 Zamítnutí kvůli zastaralým datům (KÓD)

- Stav ZASTARALÝ je jen měkká výhrada (`decision.py:79`). Tip proto skončí jako WATCH, ne jako APPROVED.
- Dnes se to pozná jen z `odds_status` a z textu „kurz není aktuální (zastaralý)“. Že by to byl **jediný** neplatný práh, nelze zjistit bez opakování výpočtu.
- Audit 3.3 to proto u 7 výběrů NHL musel jen aproximovat.

---

## 3. Audit B – evidence spotřeby API

### 3.1 Všechna místa volání (KÓD)

**The Odds API (měsíční kredity, limit 500):**

| Místo | Endpoint | Náklad | Liga / účel známý? | Evidence dnes |
|---|---|---|---|---|
| `oddsapi.fetch_events` (`oddsapi.py:61`), volá `pipeline.py:298` | `/sports/{sport}/odds` | trhy × regiony: fotbal 2, NHL 3 | ano (parametr `league`), účel „obnova cache ligy“ | `odds_api_quota` (poslední stav), `odds_cache:<liga>.credits` |
| `oddsapi.fetch_scores` (`oddsapi.py:91`), volá `auto.py:85` | `/sports/{sport}/scores?daysFrom=3` | 2 (ODHAD z dokumentace) | ano, účel „výsledky pro vyhodnocení“ | jen `remaining`, `used=None` (`auto.py:89`) |
| `foreign.fetch` (`profil4/foreign.py:89`), volá `closing.verify_foreign` | `/events` a potom `/events/{id}/odds` | 0 (seznam podle dokumentace) + počet trhů | ano (zápas, trh), účel „zahraniční ověření CLV“ | `odds_api_quota` |
| `nhl._fetch_event` (`profil4/nhl.py:409`), volá `nhl.observe` | `/events/{id}/odds` | počet trhů | ano, účel „CLV NHL 10 min před zápasem“ | `odds_api_quota` |

**API-Football (denní kvóta 7500):**
- `profil4/apiclient.py` zapisuje každé volání do `api_log`: endpoint, parametry, priorita, `fixture_id`, HTTP, chyba, zbytek. Za běhu 9 endpointů a 1300 volání.
- Hlavní bot volá API-Football **mimo** `api_log`: `context.py:108/112/143`, `cups.py:80`, `propsodds.py:79/87`, `referees.py:56`, `settlement.py:249`, `teamreport.py:120`. Spotřeba je vidět jen souhrnně v hlavičce `remaining_day`. Kvóta je ale využitá jen na 1–5 %, takže P3.

**Zdroje zdarma:** ESPN, NHL API, MoneyPuck, Open-Meteo, Google News, football-data. Nejsou předmětem rozpočtu.

### 3.2 Nepřiřaditelná spotřeba (NAMĚŘENO)

Zbytek kreditů uložený u jednotlivých lig 26. 11. (chronologicky):

| Čas (UTC) | Ligy, zbytek kreditů po stažení |
|---|---|
| 01:56 | ENG1 261 → ESP1 259 → POR1 257 |
| 06:00 | GER1 252 → ITA1 250 → FRA1 248 → UCL 246 → UEL 244 → UECL 242 → NHL 239 |

- Mezi POR1 (257) a GER1 (252) zmizelo **5 kreditů**, které žádné obnově ligy nepatří. Pravděpodobně jde o výsledky (2) a zahraniční ověření, ale data to neprokážou.
- UCL za 2 kredity bez zápasu (audit 3.3, R3) je druhý příklad zbytečné obnovy.

### 3.3 Zbytečné aktualizace (KÓD + NAMĚŘENO)

1. **Soutěž bez blízkého zápasu:**
   - `pipeline.py:284–288` přeskočí ligu, jen když je uložené `next_kickoff` **za** horizontem 48 h.
   - Když `next_kickoff` už proběhlo (po kole soutěže), podmínka neplatí a liga se stáhne znovu.
   - NAMĚŘENO: UCL 26. 11. 06:00, 2 kredity, 0 zápasů.
2. **Výsledky (`fetch_scores`):**
   - Každá liga s otevřeným papírovým tipem nebo tiketem se stahuje každé 3 h, v okně 2,5 h až 3 dny po zápase, za 2 kredity.
   - Bez evidence po voláních nejde říct, kolikrát denně se to stane. Proto to nejdřív změříme (B1) a teprve potom optimalizujeme.
3. **Pojistky** `auto_credit_reserve` = 50 (`auto.py:41`, `closing.py:146`, `nhl.py:386`) a `cache_only` fungují. **Zůstanou beze změny.**

---

## 4. Audit C – integrita časových razítek

| Místo | Filtr | Hodnocení |
|---|---|---|
| `odds.latest(store, fid, before=T)` (`odds.py:298`) | `q.stamp >= before` → vynechat, kde `stamp` = `source_time` nebo `fetched_at` | **Latentní únik (P2).** `source_time` ≤ `fetched_at`, takže kurz stažený **po** T, ale aktualizovaný u poskytovatele před T, filtrem projde. Docstring přitom slibuje „získaný před časem `before`“. V produkci se `before` nepoužívá (`analysis.py:89`, `hunter.py:72`, `profile2.py:590`, `cli.py:159` volají bez něj), takže **škoda zatím nevznikla**. |
| `history._closing_same` (`history.py:248`) | `q.stamp < kickoff` a `q.stamp > rec_time` | V pořádku. Zavírací kurz je stav trhu před výkopem; kdy jsme ho stáhli, nevadí. Do rozhodnutí nevstupuje. |
| `foreign.reference` (`foreign.py:120`) | `q.stamp < kickoff` a `stamp > after` | V pořádku (jen CLV). |
| `history.observations` (`history.py:197`) | `observed_at < kickoff` | V pořádku. |
| `intelligence` kalibrace (`intelligence.py:314`) | `predicted_at < kickoff`, poslední před zápasem | V pořádku. Vyloučí i 6 tipů po výkopu. |
| `history.record` pojistka po výkopu (`history.py:56`) | `an.created >= kickoff` → neukládat | **Slabina (P2):** `an.created` je začátek skenu, ne okamžik rozhodnutí (viz dále). |

**`predicted_at` není okamžik rozhodnutí (NAMĚŘENO):**
- Sken fotbalu nastaví jedno `now` na začátku (`hunter.run_scan`) a kurzy stahuje během skenu.
- U 413 z 552 tipů je `odds_fetched_at` > `predicted_at`: minimum 0,02 min, průměr 12 min, maximum 34,7 min. U 23 tipů je po `predicted_at` dokonce čas kurzu u poskytovatele (až o 11,9 min).
- **Nejde o únik budoucí informace.** Kurz byl k dispozici dřív než skutečné rozhodnutí.
- **Problémy, které to působí:**
  1. Replay s `before=predicted_at` by použitý kurz nenašel, takže rozhodnutí nejde rekonstruovat.
  2. Stáří kurzu se počítá k začátku skenu. Záporné hodnoty ořízne `max(0, …)` v `odds.py:55`.
  3. Zápas, který začne během 35minutového skenu, se může rozhodnout po výkopu a přitom mít označení „před výkopem“. Mimo 6 známých tipů z 21. 11. se to zatím **nestalo** (NAMĚŘENO: 0).

**NHL (KÓD + NAMĚŘENO):**
- `quotes_from_event(ev, fid, now)` nastaví `fetched_at` = začátek průchodu, ne čas stažení cache.
- 171 řádků `oddsapi` má `fetched_at` < `source_time` (o 0,02–0,48 min). Je to kosmetické, ale porušuje to invariant `fetched_at` ≥ `source_time`.

---

## 5. Problémy a závažnost

| ID | Závažnost | Problém | Důkaz |
|---|---|---|---|
| F1 | **P1** | Spotřebu The Odds API nelze rozepsat po voláních (liga, účel, náklad). | 5 nepřiřazených kreditů 26. 11.; `fetch_scores` neukládá `used` |
| F2 | **P1** | U tipu chybí strukturované důvody a hodnoty prahů; `config_hash` nemá uložený obsah. | `tips` bez důvodů; párování s `rankings` jen 338 z 552 |
| F3 | P2 | `odds.latest(before=)` filtruje podle `source_time` místo `fetched_at` (latentní únik dat). | `odds.py:302` |
| F4 | P2 | `predicted_at` ≠ skutečné rozhodnutí; pojistka po výkopu se opírá o začátek skenu. | 413 z 552 tipů, max 34,7 min |
| F5 | P2 | Ranking ořezává důvody na 6; složky robustnosti a stress scénáře se neukládají; chybí fair odds D, minimální kurz, EV proti trhu, potvrzující kanceláře. | `history.py:126` |
| F6 | P2 | Obnova soutěže, jejíž `next_kickoff` už proběhlo. | UCL 26. 11., 2 kredity |
| F7 | P3 | NHL `fetched_at` = začátek průchodu. | 171 řádků |
| F8 | P3 | Volání API-Football z hlavního bota nejsou v `api_log`. | 9 míst volání |
| F9 | P3 | `predictions` se přepisuje, historie pravděpodobností je jen v `tips` a `rankings`. | `history.py:176` |

---

## 6. Návrh cílového řešení

**Zásada:** engine počítá stejně. Mění se jen to, **co se zapisuje**. Každý nový záznam je append-only a dosavadní řádky se nemění.

### 6.1 Strukturované kontroly v `decide` (A)

- `decide()` dnes skládá texty do `fails_hard`, `fails_soft` a `ok`. Navrhuji, aby každou kontrolu zapsala nejdřív jako záznam a **dosavadní texty se z těchto záznamů odvodily**.
- Záznam má tvar `Check(rule, value, threshold, outcome)`, kde `outcome` je `ok`, `soft` nebo `hard`. Seznam se uloží do nového pole `Candidate.checks`.
- Rozhodovací podmínky, pořadí i texty zůstanou bajtově stejné. Hlídá to regresní test (T1–T3).
- Pravidla (`rule`):
  - `odds_current`, `odds_band`;
  - `ev_min`, `ev_suspicious`, `ev_conservative`;
  - `models_complete`, `model_gap`, `logit_gap`;
  - `robustness_min`, `data_quality_min`;
  - `validation_hard`, `validation_soft`, `market_covered`, `market_edge_unconfirmed`, `model_guard`.
- Odvozené příznaky bez nového výpočtu:
  - `blocking` – seznam pravidel, která brání APPROVED;
  - `stale_only` – `True`, když je jediné blokující pravidlo `odds_current`. Odpovídá na otázku „byl tip zamítnut jen kvůli zastaralému kurzu?“.

### 6.2 Uložení stopy (A)

1. **Nová tabulka `decision_trace` v `profil4.db`** (append-only, přidá ji `CREATE TABLE IF NOT EXISTS`; existující tabulky se nemění).
   - Jeden řádek na kandidáta a rozbor se stejným pravidlem změny jako `rankings`, aby se databáze zbytečně nenafukovala. Tipy se zapisují vždy.
   - Sloupce:
     - identifikace: `id`, `decided_at` (skutečný čas, viz 6.4), `scan_at` (= dnešní `predicted_at`), `fixture_id`, `market`, `selection`, `tip_id` (nebo NULL), `ranking_id`;
     - kurz: `quote_id` (odkaz na `odds_quotes.id`);
     - verze a rozhodnutí: `config_hash`, `model_version`, `decision`;
     - `trace` (JSON).
   - Obsah `trace`:
     - kurz: kancelář, kurz, zdroj, `source_time`, `fetched_at`, stáří a stav vůči `decided_at` i vůči `scan_at`;
     - pravděpodobnosti: p A–D, p trhu;
     - fair odds C/D a minimální kurz;
     - EV C, D a proti trhu;
     - `checks`, `blocking`, `stale_only`;
     - kvalita dat: skóre a složky;
     - robustnost: skóre, složky, stress scénáře;
     - integrity hard/soft, guard;
     - potvrzující kanceláře, `market_covered`;
     - úplné důvody a rizika, kategorie.
2. **Tabulka `config_snapshots(config_hash PRIMARY KEY, created_at, config JSON)`:** při prvním výskytu otisku se uloží celé `asdict(cfg)` bez cest. Hodnoty prahů pak jdou dohledat přes `config_hash`.
3. **Tip a ranking:** stopa se k nim jen připojí přes `tip_id` a `ranking_id`, jejich sloupce zůstávají. Starých 552 tipů se **nedoplňuje**, protože historii neměníme. U starých tipů zůstane označení „stopa neúplná (před Phase 3.4)“.
4. **Čtení:** funkce `trace.explain(tip_id)` a CLI `python -m tipsbot.profil4.cli explain <tip_id>` vypíšou „proč“ v češtině. Panel může přidat odkaz ve Výsledcích až v další fázi.

### 6.3 Evidence spotřeby API (B)

1. **Tabulka `odds_api_calls` v `tipsbot.db`** (nový model SQLAlchemy, vytvoří ho `create_all`; existující tabulky beze změny).
   - Sloupce: `ts`, `endpoint`, `league`, `purpose` (`refresh_league`, `scores`, `foreign_clv`, `nhl_clv`, `events_list`), `fixture_id`, `markets`, `http`, `ok`, `error` (bez klíče, přes `oddsapi.redact`), `remaining`, `used`, `cost`.
   - `cost` se bere z hlavičky `x-requests-last`. Když chybí, dopočítá se z rozdílu `used`. Hlavičku pak ověříme na prvním běžném volání, ne testovacím dotazem.
2. **Jedna funkce `oddsapi.log_call(...)`** se zavolá ze všech 4 míst ze 3.1. `fetch_scores` začne vracet i `used`.
3. **Přehled:** `freshness.report` a karta Spotřeba API v panelu ukážou kredity za den po ligách a účelech. Nepřiřazený zbytek uvidíme jako rozdíl součtu `cost` a změny `used`.
4. **Zbytečné obnovy (F6):** ligu přeskočit i tehdy, když uložené `next_kickoff` **už proběhlo**, liga nemá otevřený tip a poslední cache neobsahuje žádný budoucí zápas do 48 h. Rozhoduje se z uložené cache, takže to stojí 0 kreditů.
5. **Výsledky:** zatím beze změny. Až budou z B1 data za 7 dní, rozhodneme podle čísel.
6. **Rezerva 50 a `cache_only`:** beze změny. Tempo čerpání (R1 z 3.3) je **samostatný návrh** k pozdějšímu schválení, v této fázi se neimplementuje.

### 6.4 Časová razítka (C)

1. **`decided_at`:** skutečný čas (`utcnow()`) těsně před `decide` pro daný zápas. Ukládá se do stopy vedle `scan_at`.
   - Stáří a stav kurzu **pro rozhodnutí** se dál počítají k `now` skenu, takže se strategie nemění.
   - Stopa ale ukáže obě hodnoty a příznak `odds_after_scan_start`.
   - Zda rozhodovat k `decided_at`, je změna strategie (kurzy by stárly o 0–35 min víc). Proto jen jako otevřená otázka pro další fázi.
2. **Pojistka po výkopu:** v `history.record` přidat druhou podmínku: neukládat ani při `decided_at >= kickoff`. Tím se jen zabrání uložení; rozhodování se nemění.
3. **`odds.latest(before=T)`:** filtrovat podle `fetched_at < T` (kurz byl k dispozici). Mezi verzemi téže kanceláře vybírat dál podle `stamp`. V produkci se `before` nepoužívá, takže dopad je nulový; oprava je prevence pro replay a backtest.
4. **NHL `fetched_at`:** brát čas uložení cache (`odds_cache.t`), ne začátek průchodu. Platí jen pro nové řádky.

---

## 7. Seznam změn (po schválení)

| # | Soubor | Změna | Úkol |
|---|---|---|---|
| 1 | `profil4/decision.py` | `Check`, `Candidate.checks`, `decide` zapisuje záznamy a texty z nich odvozuje; `blocking`, `stale_only` | A |
| 2 | `profil4/store.py` | `decision_trace`, `config_snapshots` (CREATE IF NOT EXISTS, bez ALTER existujících tabulek) | A |
| 3 | `profil4/trace.py` (nový) | sestavení JSON stopy z kandidáta, `explain(tip_id)` | A |
| 4 | `profil4/history.py` | `record` a `record_rankings` zapisují stopu a snapshot konfigurace; pojistka `decided_at >= kickoff` | A, C |
| 5 | `profil4/analysis.py`, `profil4/nhl.py` | předání `decided_at` (jen záznam) | C |
| 6 | `profil4/cli.py` | příkaz `explain` | A |
| 7 | `storage.py` | model `OddsApiCall` | B |
| 8 | `oddsapi.py` | `log_call`, `fetch_scores` vrací i `used`, čtení `x-requests-last` | B |
| 9 | `pipeline.py`, `auto.py`, `profil4/closing.py`, `profil4/nhl.py`, `profil4/foreign.py` | volání `log_call` s ligou a účelem; F6 v `pipeline.py:284–288` | B |
| 10 | `freshness.py`, `web.py` (karta Spotřeba API) | rozpis kreditů po lize a účelu | B |
| 11 | `profil4/odds.py` | `latest(before=)` podle `fetched_at` | C |
| 12 | `profil4/nhl.py` | `fetched_at` z času cache | C |
| 13 | `tests/` | viz kap. 8 | vše |

**Nemění se:**
- `mathcore`, `models`, `stress`, `intelligence`;
- prahy v `config.py`, profily A/B/C/D a Profil 5;
- rezerva a cache-only;
- existující řádky v obou databázích.

**Nasazení:**
- záloha obou DB, nové tabulky se vytvoří samy;
- deploy až po schválení, nejdřív `dev`, `main` jen po „ano“.

---

## 8. Testovací plán

**Regresní testy (rozhodnutí se nesmí změnit):**

| ID | Test |
|---|---|
| T1 | Pro zlatou sadu kandidátů, uloženou ze současného kódu před změnou (fotbal i NHL, všechny větve `decide`), dá nový `decide` **stejné** `decision`, `reasons` i `risks` (bajtově). |
| T2 | `select` dá stejné pořadí, kategorie A/B/C a BEST AVAILABLE. |
| T3 | `config_hash()` se nezmění (nová pole nejsou v `Profil4Config`). |
| T4 | Celá stávající sada pytest (dnes 799 testů) projde. |

**Nové testy:**

| ID | Test |
|---|---|
| T5 | Každá větev `decide` vytvoří právě jeden `Check` se správným pravidlem, hodnotou, prahem a výsledkem. Texty z checků se rovnají původním. |
| T6 | `stale_only` = True jen tehdy, když jediné blokující pravidlo je `odds_current`. Kontrola na kandidátovi, kterému čerstvý kurz dá APPROVED (syntetická data). |
| T7 | `record` zapíše stopu ke každému tipu (`tip_id`) a ke každému novému řádku rankingu; opakovaný sken beze změny nic nepřidá. |
| T8 | `config_snapshots`: stejný otisk se uloží jen jednou; z uloženého JSON jde zpětně spočítat stejný `config_hash`. |
| T9 | `explain(tip_id)` u nového tipu vypíše kurz s časy, p, fair odds, EV, všechny prahy, kvalitu, robustnost a důvod. U starého tipu vypíše „stopa neúplná“ a nespadne. |
| T10 | `latest(before=T)`: kurz se `source_time` < T a `fetched_at` > T se **nevrátí**; kurz s `fetched_at` < T ano. |
| T11 | `decided_at >= kickoff` → `record` nic neuloží ani při `scan_at < kickoff`. |
| T12 | `log_call` u všech 4 míst volání (mockované HTTP, žádné skutečné volání): liga, účel, náklad z `x-requests-last`, při chybějící hlavičce náklad z rozdílu `used`, chyba bez klíče v textu. |
| T13 | F6: liga s `next_kickoff` v minulosti, bez tipu a bez budoucího zápasu v cache se nestáhne; s budoucím zápasem nebo otevřeným tipem se stáhne. |
| T14 | Rezerva 50 a `cache_only` se chovají jako dnes (stávající testy + jeden test na hranici 50/51). |
| T15 | Append-only: snímek `tips`, `rankings` a `odds_quotes` před a po běhu; staré řádky se nezmění. |
| T16 | NHL: nové kurzy mají `fetched_at` ≥ `source_time`. |

**Ověření v provozu (jen čtení, po schválení deploye):**
- po prvním průchodu: kolik tipů má stopu (cíl 100 %);
- součet `cost` za den proti změně `used` (cíl: nepřiřazeno 0);
- žádné nové API volání navíc.

---

## 9. Rizika a dopady

| Riziko | Dopad | Opatření |
|---|---|---|
| Úprava `decide` nechtěně změní rozhodnutí. | Změna strategie. | T1–T3 na zlaté sadě vytvořené **před** změnou; kontroly jen zapisují, podmínky zůstávají. |
| Růst `profil4.db` | ODHAD: stopa jako `rankings` (asi 4–5 tisíc řádků denně, 1–3 kB JSON) = 5–15 MB denně. | Stejné pravidlo změny jako `rankings`; JSON bez duplicit (kurzy jen odkazem). Při potřebě později archivace, ne mazání. |
| Delší zápis a zámky DB (22. 11. padal sken na zámku). | Zpoždění skenu. | Zápis stopy uvnitř stávajícího `store.batch()`. |
| Hlavička `x-requests-last` chybí nebo znamená něco jiného. | Nepřesný náklad. | Záložní výpočet z rozdílu `used`; ověřit na prvním běžném volání. |
| F6 přeskočí ligu, která má nový zápas, ale cache o něm neví. | Zápas se načte o jeden průchod později (do 6 h). | Jen když chybí budoucí zápas v cache i otevřený tip; T13. |
| Snapshot uloží citlivé údaje. | Únik klíče. | `Profil4Config` klíče neobsahuje; pojistka: vynechat pole s `key`, `token`, `secret`; test. |
| Staré tipy zůstanou bez stopy. | Neúplná historie do nasazení. | Výslovné označení; historii nedopočítáváme ani neměníme. |
| Rozhodovat podle `decided_at` | Mění stáří kurzů, tedy strategii. | **V této fázi ne**, jen evidence. |

---

## 10. Doporučené pořadí (po schválení)

1. **B1:** evidence volání The Odds API (#7–#10 bez F6). Hotové dříve, než 1. 12. nastane reset, takže prosincová spotřeba bude rozepsaná od začátku.
2. **C:** `latest(before=)`, `decided_at`, pojistka po výkopu, NHL `fetched_at` (#5, #11, #12).
3. **A:** checks, stopa, snapshot konfigurace, `explain` (#1–#4, #6).
4. **F6** po týdnu dat z B1.

Každý krok: testy, commit na `dev`, deploy až po potvrzení.

---

## 11. Otevřené otázky

1. Má se rozhodovat podle skutečného času rozhodnutí (`decided_at`) místo začátku skenu? Je to změna strategie, a proto patří do samostatné fáze.
2. Má panel ukazovat „proč“ u každého tipu už v této fázi, nebo zatím stačí CLI?
3. Jak dlouho držet `decision_trace` (navrhuji bez mazání, případně archivaci po 6 měsících)?
