# Phase 3.3 – Odds Freshness & Cache Optimization: audit, analýza a simulace

Stav k 26. 11. 2026, data vytažena 07:13 UTC. Produkce běží na commitu 8e3033d (image `ace23bdde102`), `main` zůstává na 8b44073.
Tato fáze je **jen diagnostická**:

- žádný produkční kód, databáze, cache, prahy ani strategie se neměnily;
- nebyl proveden žádný nový API dotaz.

Všechna čísla pocházejí z kódu, z logů kontejneru `tipsbot-auto` a z read-only dotazů nad `tipsbot.db` a `profil4.db` (`sqlite` URI `mode=ro`; skript ověří, že zápis selže).

Značení v celém dokumentu:

- **NAMĚŘENO** = z dat;
- **ODHAD** = modelový výpočet s uvedenými předpoklady;
- **N/A** = nelze určit (s důvodem).

Reprodukce:

- `scripts/phase33_odds_audit.py` (návod v hlavičce skriptu);
- CSV v `docs/phase-3-3-*.csv`.

---

## 1. Executive summary

**Fotbal (API-Football): limit je u zdroje, ne v botovi.**

- Bot v předzápasových bodech stahuje mimo cache (`force=True`), každé stažení se zapíše a nové hodnoty se uloží.
- API-Football aktualizuje kurzy hromadně, v dávkách pro celou soutěž. Nejčastěji to je v hodinách 03, 04, 12, 06 a 08 UTC.
- **NAMĚŘENO:** podíl stažení, která přinesla novou verzi kurzu:

  | Bod | Nová verze | Podíl |
  |---|---|---|
  | 75 min | 66 ze 73 | 90 % |
  | 30 min | 15 ze 72 | 21 % |
  | 10 min | 4 ze 72 | 6 % |

- Při výkopu má poslední aktualizace u poskytovatele medián **206 min**. Jen **5,6 %** zápasů ji má nejvýš 60 min před výkopem.
- Liga mistrů 25. 11. dostala dvě dávky, ve 12:17:17 a v 18:00:34 UTC. Inter, Lille a PSG druhou dávku nedostaly, a proto měly při výkopu kurz starý 462,7 min.

**NHL (The Odds API): limit je v architektuře a v rozpočtu kreditů.**

- Cache NHL se reálně obnovuje jednou denně, při ranním průchodu kolem 06:00 UTC.
- Rozhoduje se v každém šestihodinovém průchodu, ale vždy z této ranní cache.
- Zápasy začínají převážně 00:00–03:00 UTC, tedy 18–21 h po obnově. Poslední rozhodnutí před zápasem proto stojí na kurzech starých medián **720 min**, což je ODHAD.
- Stažení 10 min před zápasem:
  - probíhá jen u zápasu se schváleným tipem;
  - slouží jen pro zavírací kurz a CLV, rozhodnutí se z něj znovu nepočítá.

**Rozpočet kurzového API je hlavní omezení.**

- Měsíční limit je 500 kreditů. Hodnota je odvozená ze součtu zbývajících a použitých kreditů v hlavičkách API (239 + 261 k 26. 11.).
- **NAMĚŘENO:** od 21. 11. se spotřebuje průměrně **21,6 kreditu denně** (27 / 19 / 15 / 21 / 26). Tempo 500 kreditů za 30 dní odpovídá 16,7 denně.
- Kdy se měsíční počítadlo nuluje, **nevíme**.

**Profil 4: stáří se počítá správně, z času poskytovatele.**

- Kurz se nemůže tvářit jako čerstvý jen proto, že byl znovu stažen (`OddsQuote.stamp`, viz kapitola 9).
- Zastaralý kurz (nad 360 min) vždy zablokuje APPROVED a vede na WATCH. Robustnost, Quality of Data ani EV nemusí rozhodovat.

**Doporučení:**

- **P0:** žádné.
- **P1:**
  1. Zavést tempo čerpání (pacing) podle známého resetu (1. den kalendářního měsíce, kap. 8.1).
  2. Teprve potom zvážit jedno podmíněné obnovení NHL před večerním programem (varianta E): +3 kredity za herní den a rozhodnutí z čerstvých kurzů u přibližně 84–88 % zápasů (ODHAD).
- **Nedoporučuji:**
  - varianty B, C a D – nevejdou se do 500 kreditů;
  - zahušťování fotbalových bodů – zdroj novější data nemá.

### Odpovědi na tři hlavní otázky

| Otázka | Odpověď |
|---|---|
| **NHL:** kolik by stálo častější obnovování a kolik zápasů by získalo aktuálnější data? | **ODHAD** nad 56 zápasy NHL z 21.–27. 11.:<br>• **A** (dnes): 3 kredity za herní den. Jen 9 % zápasů má rozhodnutí z aktuálních kurzů v posledních 6 h před zápasem.<br>• **E** (+1 obnova před prvním zápasem dne): 6 kreditů za den, 84 %.<br>• **B** (obnova v každém průchodu): 12 kreditů za den, 95 %.<br>• **C** (každé 3 h): 24 kreditů za den.<br>• **D** (75/30/10 min u každého zápasu): 87 kreditů za den.<br>Už varianta A je při dnešní celkové spotřebě nad tempem 500 kreditů měsíčně, takže E vyžaduje úsporu jinde. |
| **Fotbal:** dokáže API-Football poskytnout novější kurzy? | Většinou ne. Během posledních 75 min před výkopem se nová verze objeví u 21 % zápasů mezi body 75 a 30 a u 6 % mezi body 30 a 10. Bot ji vždy zachytí. Narazili jsme na limit zdroje. |
| **Profil 4:** jak rozdíl mezi stažením a aktualizací mění APPROVED / WATCH? | Rozhoduje čas aktualizace u poskytovatele, ne čas stažení.<br>• **Fotbal:** kurz bývá starý 100–200 min, takže zastarání (nad 360 min) rozhodnutí mění zřídka. Ze 450 uložených fotbalových výběrů je ZASTARALÝ jen 7.<br>• **NHL:** 38 uložených výběrů skončilo jako ZASTARALÝ:<br>  – 20 bylo dříve schváleno z čerstvých dat, zastarání jen změnilo pozdější označení;<br>  – **7 by podle uložených čísel prošlo ostatními prahy** (EV ≥ 3 %, konzervativní EV ≥ 0, robustnost ≥ 60), ale nikdy schváleny nebyly. Všech 7 je z jednoho průchodu 21. 11. v 00:30 UTC. Jde o aproximaci, důvody se neukládají.<br>Malý vzorek, z výsledků nelze nic vyvozovat. |

---

## 2. Současná architektura

### 2.1 Diagram datových toků

```text
FOTBAL (Profil 4)                                     NHL (Profil 4) a fotbal hlavního bota (profily A–D)
API-Football /odds  (dávkové aktualizace, pole        The Odds API /sports/{key}/odds  (last_update za
"update" = 1 čas za celý zápas)                       kancelář a trh; cena 1 kredit za trh a region)
   │                                                     │
   ▼                                                     ▼
ApiFootballClient.get / get_all                       oddsapi.fetch_raw / fetch_events
  profil4/apiclient.py:156–252                          oddsapi.py:51–83, 190–200
  cache api_cache (TTL odds 3 h), force obchází          ▲ volá jen pipeline.scan_league (pipeline.py:284–302),
  budget BudgetManager.check (budget.py:92–106)          │ když auto.wants_fresh_odds (auto.py:35–44) = True
  evidence api_log + budget (budget.py:122–138)          │
   │                                                     ▼
   ▼                                                  Ledger.set_odds_cache → tabulka state "odds_cache:<liga>"
parse_api_odds → OddsQuote(fetched_at, source_time)      storage.py:255–268 (JEDNA verze na ligu, přepisuje se)
  profil4/odds.py:158–207  (map_bet = normalizace trhů)   kredity: Ledger.set_odds_api_quota (storage.py:287–314)
   │                                                     │
   ▼                                                     ▼
odds.save → odds_quotes (jen nové: stejný kurz se      nhl.run (profil4/nhl.py:291–347): z cache →
  stejným source_time se neukládá) odds.py:268–294      quotes_from_event (nhl.py:101–120, čas = last_update)
   │                                                     → odds.save → odds_quotes (source "oddsapi")
   ▼                                                     │
Předvýkopové body 75/30/10 (closing.due/observe,        Předvýkopové body (nhl.observe, nhl.py:354–405):
  closing.py:48–113) → odds_observations                 kurzy z cache hlavního bota (0 kreditů); v bodu 10 min
  + verify_foreign (closing.py:116–154) = kurzové API    JEN u schváleného tipu se starou cache → /events/{id}/odds
  u schváleného tipu se starým kurzem                    → odds_observations
   │                                                     │
   ▼                                                     ▼
Rozbor a rozhodnutí: ranní sken Profilu 4               Rozbor a rozhodnutí: nhl.run_and_store v KAŽDÉM
  (closing.morning_scan → hunter.run_scan;              průchodu automatu (auto.py:132–142) z aktuální cache
  analysis.py:160–186) + ruční skeny                      │
   │                                                     │
   └──────────────►  decision.decide (decision.py:76–117): APPROVED BET / WATCH / NO BET
                     history.record → tips, predictions, rankings (history.py:36–…)
                     zavírací kurz a CLV: history._closing (history.py:202–…)
```

### 2.2 Dvanáct bodů mapování

| # | Otázka | Kde a jak (soubor:řádek) |
|---|---|---|
| 1 | Získání fotbalových kurzů z API-Football | `profil4/provider.py:187` `odds_fixture` (jeden zápas) a `:197` `odds_league` volají `ApiFootballClient.get_all("odds", …)` (`profil4/apiclient.py:231`). |
| 2 | Získání NHL kurzů z The Odds API | Hlavní bot: `pipeline.scan_league` (`pipeline.py:298`) volá `oddsapi.fetch_events` (`oddsapi.py:190`). Trhy h2h, totals a spreads, region eu (`oddsapi.py:66`). Profil 4 navíc stahuje kurzy jedné události v `nhl._fetch_event` (`nhl.py:408`). |
| 3 | Uložení | **Fotbal:** `profil4.db/odds_quotes` (vkládá se, nic se nepřepisuje), dále `api_cache` (poslední odpověď) a `api_log`.<br>**Kurzové API:** `tipsbot.db/state` `odds_cache:<liga>`. Drží jen jednu verzi, která se přepíše. Profil 4 si kopii NHL kurzů ukládá do `odds_quotes`. |
| 4 | Jak funguje cache | **API-Football:** `api_cache` s TTL podle endpointu, pro `odds` 3 h (`profil4/config.py:88`). `force=True` cache obchází (`apiclient.py:161`).<br>**Kurzové API:** `odds_cache` plus pravidlo v `pipeline.py:293`. Při `cache_only` se nestahuje vůbec, jinak se stahuje, když je cache starší než `cache_seconds`, tedy 600 s (`config.py:248`). |
| 5 | Stáří kurzu | `OddsQuote.age_minutes` = teď − `stamp`, kde `stamp` = `source_time` nebo `fetched_at` (`odds.py:52–56`). |
| 6 | Stáří posledního stažení | **Fotbal:** `odds_quotes.fetched_at` a `odds_observations.observed_at`.<br>**Kurzové API:** čas `t` v `odds_cache`. V logu se zobrazuje jako „kurzy z cache (N min staré)“ (`pipeline.py:296`). |
| 7 | Timestamp poskytovatele | **API-Football:** pole `update` za celý zápas, `odds.py:179`.<br>**Kurzové API:** `last_update` trhu, jinak kanceláře, `nhl.py:111–116`. |
| 8 | Kdy je cache zastaralá | **Kurzové API v automatu:** `auto.wants_fresh_odds` (`auto.py:35–44`). Obnova proběhne, když je cache starší než 24 h (`auto_odds_hours`), při ranním průchodu už od 6 h (`auto_morning_odds_hours`). Pod rezervou 50 kreditů se nestahuje nikdy.<br>**Kurz Profilu 4:** nad `max_age_api_min` = 360 min je ZASTARALÝ (`odds.py:58–64`). |
| 9 | Kdy se dělá nový request | **Kurzové API:** soutěž, která má zápas do 48 h nebo otevřený tip (`pipeline.py:285–288`), a zároveň `wants_fresh_odds` = True.<br>**API-Football:** předvýkopové body (vždy), ranní sken a ruční skeny (podle TTL). |
| 10 | API Budget Manager | `profil4/budget.py`. Řeší:<br>• denní kvótu z hlaviček (7 500 denně);<br>• rezervy podle priority (HIGH 2 %, MEDIUM 15 %, LOW 40 %);<br>• strop 60 požadavků na sken (ranní sken 500) a 12 na zápas za den;<br>• minutový limit.<br>Kurzové API má jen rezervu `auto_credit_reserve` = 50 (`config.py:165`) a žádné denní tempo. |
| 11 | Evidence kreditů | **API-Football:** tabulka `budget` a každý request v `api_log`.<br>**Kurzové API:** `Ledger.set_odds_api_quota` ukládá zbývající a použité kredity z hlaviček a denní stav počítadla `used` (`storage.py:287–314`).<br>**Spotřeba po jednotlivých voláních se neeviduje.** |
| 12 | Předvýkopové ověřování Profilu 4 | `closing.tick` každou minutu (`closing.py:157`) volá `due` (`:48`), tedy zápasy s tipem nebo predikcí a dosaženými body 75/30/10. Pak `observe` (`:77`):<br>• **fotbal:** API-Football `force`, priorita MEDIUM u schváleného tipu, jinak LOW, plus `verify_foreign`;<br>• **NHL:** `nhl.observe`. |

---

## 3. Mapa zdrojů

| Zdroj | Co dodává | Cena | Obnova v botovi | Timestamp poskytovatele |
|---|---|---|---|---|
| API-Football `/odds` | Fotbal Profilu 4: kurzy, přibližně 540–605 kurzů na zápas | 1 request na stránku; limit 7 500 denně; **NAMĚŘENO** 78–358 denně | Ranní sken (TTL 3 h) a body 75/30/10 (vždy mimo cache) | `update`, jeden čas za celý zápas, platí pro všechny kanceláře najednou |
| The Odds API `/odds` (liga) | Fotbal hlavního bota a NHL pro hlavní bota i Profil 4 | Fotbal 2 kredity (h2h, totals), NHL 3 kredity (navíc spreads). **NAMĚŘENO** 26. 11.: 242 → 239 | Nejvýš jednou za 24 h, při ranním průchodu už od 6 h | `last_update` za kancelář a trh |
| The Odds API `/events/{id}/odds` | Zavírací kurz NHL a zahraniční ověření fotbalu | 1 kredit za trh a region | Jen bod 10 min a jen u schváleného tipu se starou cache | `last_update` |
| The Odds API `/scores` | Vyhodnocení fotbalu | 2 kredity za soutěž (`daysFrom`), nejvýš každé 3 h (`auto.py:81`) | Jen když na výsledek čeká tip | – |

---

## 4. Analýza časových razítek

| Požadovaný pojem | Skutečný název a uložení | Zdroj hodnoty |
|---|---|---|
| fetched_at | `OddsQuote.fetched_at`, `odds_quotes.fetched_at`; u kurzového API `odds_cache.t` | Bot (čas stažení) |
| provider_updated_at | `OddsQuote.source_time`, `odds_quotes.source_time`, `odds_observations.latest_update` | API (`update` nebo `last_update`) |
| stored_at | **Neexistuje.** Uložení proběhne hned po stažení, takže v praxi platí stored_at = fetched_at | – |
| event_start_at | `kickoff` (`odds_quotes`, `tips`, `odds_observations`) | API nebo rozpis NHL API |
| evaluated_at | `tips.predicted_at` a `rankings.computed_at`; stáří kurzu v tu chvíli je v `tips.odds_age_min` | Bot |

Odpovědi na otázky 1–7:

1. **Dává API timestamp?**
   - API-Football ano, ale jen jeden čas za zápas (pole `update`). **NAMĚŘENO:** každé ze 704 stažení mělo právě jeden `source_time`.
   - Kurzové API ano, za každou kancelář a trh.
   - Ručně zadané a importované kurzy timestamp poskytovatele nemají. U nich se `source_time` nastaví na čas zadání (`odds.py:213`, `:259`), záměrně.
2. **Je převzatý, nebo vytvořený botem?** U `api` a `oddsapi` vždy převzatý z API. **NAMĚŘENO:** `source_time IS NULL` = 0 ze 176 538 a 0 z 5 891 řádků.
3. **Aktualizuje se při každém requestu?**
   - Fotbal: ne, mění se jen s dávkou poskytovatele.
   - Kurzové API: prakticky ano. **NAMĚŘENO:** v cache NHL z 26. 11. 06:00 mají všechny kanceláře `last_update` mezi 06:00 a 06:03, tedy hned po stažení. `last_update` tak odpovídá času, kdy kurzové API kancelář naposledy zkontrolovalo, ne času poslední změny ceny.
4. **Zachová se při neúspěšném requestu?** Ano.
   - Neúspěšné stažení nic nepřepíše: `odds_quotes` se jen doplňuje.
   - `odds_cache` se přepíše jen při `payload is not None` (`pipeline.py:301`).
   - Neúspěch se zapíše jako `odds_observations.status` s poznámkou.
5. **Jde rozlišit nový kurz od znovu staženého stejného?** Ano.
   - `odds.save` neuloží řádek se stejným (zápas, kancelář, trh, výběr, kurz, zdroj, source_time) (`odds.py:268–294`). Funkce vrací počet nových řádků, v logu „N nových“.
   - Nová verze se stejnou hodnotou se uloží, protože má jiný `source_time`.
   - Změna hodnoty se dá spočítat srovnáním s předchozí verzí (sloupec `changed_values` v CSV).
6. **Ukládá se změna historicky?**
   - V Profilu 4 ano: `odds_quotes` je insert-only.
   - V hlavním botovi ne: `odds_cache` drží jen poslední verzi.
7. **Jde rekonstruovat stav v okamžiku T?**
   - Profil 4 ano: `odds.history` a `odds.latest`.
   - Pozor: `latest(before=T)` filtruje podle `stamp`, ne podle `fetched_at`. Při zpětném přehrávání by se tak mohly použít kurzy stažené až po T, ale aktualizované před T. V produkčním kódu se parametr `before` nepoužívá (ověřeno grepem), riziko úniku dat zatím není.
   - Hlavní bot to nedokáže, kromě kopie NHL v Profilu 4.

---

## 5. Fotbal – API-Football

Zdroj: 219 pozorování u 73 zápasů z 20.–26. 11. Kompletní tabulka je v `docs/phase-3-3-prematch-observations.csv`.

### 5.1 Body 75 / 30 / 10 (NAMĚŘENO)

| Bod | Stažení (první bod stažení) | Nová verze | Kurzů na zápas (medián) | Nových řádků celkem | Změněných hodnot | Stažení před výkopem (medián) |
|---|---|---|---|---|---|---|
| 75 min | 73 | 66 (90 %) | 540 | 35 421 | 21 589 | 74 min |
| 30 min | 72 | 15 (21 %) | 540 | 8 138 | 3 209 | 29 min |
| 10 min | 72 | 4 (6 %) | 540 | 2 106 | 1 055 | 9,2 min |

Poznámky k tabulce:

- V bodu 75 se „nová verze“ počítá proti poslednímu uloženému stavu z ranního skenu.
- Modul Odds Freshness ji počítá proti předchozímu pozorování, proto u bodu 75 uvádí 56. U bodů 30 a 10 se oba výpočty shodují.
- Stažení se 3× nezdařilo (UNAVAILABLE), všechna 24. 11. kvůli síťové chybě (`api_log`: 2× odds, 2× injuries, 1× headtohead).
- **API requesty:** medián 5 requestů `/odds` na zápas, tedy ranní sken, tři body a případně ruční sken. Strop je 12 na zápas za den.

### 5.2 Proč je v bodech 30 a 10 min „0 nových“

| Možnost | Verdikt | Důkaz |
|---|---|---|
| 1. API vrací stále stejné hodnoty | **ANO** | `odds_observations.latest_update` je ve všech třech bodech stejný (příklad v 5.3) a počet kurzů se nemění (605 / 605 / 605). |
| 2. Parser stejné hodnoty správně deduplikuje | **ANO, a je to správně** | `odds.save` (`odds.py:268–294`) neuloží stejný kurz se stejným `source_time`. Při nové verzi se nové řádky uloží (21 % a 6 % případů). |
| 3. API vrací nové hodnoty, ale ukládání je ignoruje | **NE** | Kdyby API vracelo nový `update`, změnil by se `latest_update` v `odds_observations`. Ten se ukládá z odpovědi před deduplikací (`closing.py:99–104`). |
| 4. Nevhodný cache klíč | **NE** | Body stahují s `force=True` (`closing.py:96`), cache se obchází (`apiclient.py:161`). Každé stažení je v `api_log` s `cached=0`. |
| 5. API novější data neposkytuje | **ANO, příčina** | Aktualizace chodí v dávkách za celou soutěž. Rozložení dávek podle hodiny UTC (počet verzí): 03 → 110, 04 → 86, 12 → 45, 06 a 08 → 36. Medián intervalu mezi pozorovanými verzemi je 471 min.<br>Interval je jen horní odhad: verze, kterou bot nestáhl, nevidíme. |
| 6. Některé trhy nebo kanceláře chybí | **Částečně, bez vlivu na „0 nových“** | Počet kurzů je mezi body stabilní. Nepodporované trhy se vynechávají už v `map_bet` (`odds.py:137–155`). |

### 5.3 Liga mistrů 25. 11. 2026 (NAMĚŘENO)

| Zápas | Výkop (UTC) | Stažení 75 / 30 / 10 | Timestamp poskytovatele | Kurzů | Nových (75 / 30 / 10) | Změněných (75) | Stáří při výkopu |
|---|---|---|---|---|---|---|---|
| Sabah FA – Barcelona | 17:45 | 16:30:42 / 17:15:40 / 17:35:19 | 12:17:17 | 605 | 605 / 0 / 0 | 463 | 327,7 min |
| Slavia Praha – Villarreal | 17:45 | 16:30:42 / 17:15:40 / 17:35:19 | 12:17:17 | 568 | 568 / 0 / 0 | 475 | 327,7 min |
| Atlético Madrid – Viking | 20:00 | 18:45:29 / 19:30:55 / 19:50:46 | 18:00:34 | 570 | 570 / 0 / 0 | 301 | 119,4 min |
| Club Brugge – Liverpool | 20:00 | 18:45:29 / 19:30:55 / 19:50:46 | 18:00:34 | 596 | 596 / 0 / 0 | 417 | 119,4 min |
| Inter – Stuttgart | 20:00 | 18:45:29 / 19:30:55 / 19:50:46 | 12:17:17 | 589 | 589 / 0 / 0 | 436 | 462,7 min |
| Lille – Bayern | 20:00 | 18:45:29 / 19:30:55 / 19:50:46 | 12:17:17 | 543 | 543 / 0 / 0 | 435 | 462,7 min |
| PSG – AS Roma | 20:00 | 18:45:29 / 19:30:55 / 19:50:46 | 12:17:17 | 588 | 588 / 0 / 0 | 486 | 462,7 min |
| Shakhtar – Fenerbahçe | 20:00 | 18:45:29 / 19:30:55 / 19:50:46 | 18:00:34 | 539 | 539 / 0 / 0 | 491 | 119,4 min |
| Sporting – Manchester United | 20:00 | 18:45:29 / 19:30:55 / 19:50:46 | 18:00:34 | 569 | 569 / 0 / 0 | 442 | 119,4 min |

Zápasy se stejným výkopem dostaly buď dávku v 18:00:34, nebo zůstaly na dávce z 12:17:17. Rozhoduje poskytovatel, ne bot.

Bot požádal o data 3× a vždy dostal poslední verzi, kterou poskytovatel měl. U Sabah – Barcelona a Club Brugge – Liverpool se v bodu 10 navíc stáhly kurzy zahraniční kanceláře (26 a 34 kurzů, sloupec `foreign_rows`).

### 5.4 Podle soutěže – stáří poslední aktualizace při výkopu (NAMĚŘENO; řádky soutěží = modul Odds Freshness, tj. stejná čísla jako list Excelu)

| Soutěž | Zápasů | Medián | Min | Max | Aktualizace ≤ 60 min |
|---|---|---|---|---|---|
| Liga mistrů | 18 | 332 | 119 | 467 | 0 % |
| Premier League | 10 | 313 | 119 | 358 | 0 % |
| Czech Liga | 8 | 236 | 47 | 287 | 12,5 % |
| Bundesliga | 9 | 193 | 45 | 283 | 22 % |
| Ligue 1 | 8 | 178 | 105 | 316 | 0 % |
| La Liga | 10 | 174 | 59 | 414 | 10 % |
| Serie A | 10 | 146 | 83 | 267 | 0 % |
| **Celkem (bod 10 min)** | 72 | **206** | 45 | 467 | **5,6 %** |

**Rozdíly mezi kancelářemi:** **N/A**. API-Football dává jeden `update` za zápas, ne za kancelář, takže stáří podle kanceláře z tohoto zdroje určit nejde.

---

## 6. NHL – The Odds API

### 6.1 Jak to dnes funguje (kód)

1. **Obnova cache** (`auto.wants_fresh_odds`, `auto.py:35–44`):
   - mimo ranní průchod, když je cache starší než 24 h;
   - v ranním průchodu (od 07:00 Praha), když je starší než 6 h;
   - pod 50 kredity nikdy.

   **NAMĚŘENO:** obnovy NHL proběhly 22., 23., 24., 25. i 26. 11. vždy kolem 06:00 UTC (`rankings.computed_at` s čerstvými kurzy, stáří 1–2 min). Jedna obnova stojí 3 kredity.
2. **Rozhodnutí:** `nhl.run_and_store` v každém průchodu (`auto.py:132–142`) z té cache, jaká zrovna je.
3. **Body 75 a 30 min:** kurzy z cache hlavního bota, 0 kreditů (`nhl.py:368–374`).
4. **Bod 10 min:** stažení události (`/events/{id}/odds`) jen při splnění všech podmínek (`nhl.py:375–392`):
   - tip je APPROVED;
   - jde o poslední bod;
   - cache je starší než 60 min;
   - je dost kreditů nad rezervou 50.

   Stahují se jen trhy schválených tipů. Výsledek se uloží jako zavírací kurz. **Rozhodnutí se znovu nepočítá.**

### 6.2 NAMĚŘENO (41 zápasů NHL, 123 pozorování)

| Bod | Nová verze kurzu | Stáří kurzu v okamžiku stažení (medián) |
|---|---|---|
| 75 min | 0 ze 41 | 1 002 min |
| 30 min | 0 ze 41 | 1 046 min |
| 10 min | 22 ze 41 (stažení události u schváleného tipu) | 0,15 min |

- Kurzů v bodech 75 a 30 bylo 68–80 na zápas. Stažení v bodu 10 přineslo 10–28 kurzů, všechny nové, protože jde jen o trhy tipu.
- Stáří při výkopu v bodu 10: medián 9,9 min u 22 stažených zápasů. U zbylých 19 zůstalo 1 071–1 267 min (podle logu).

### 6.3 Architektonické omezení (potvrzeno)

1. **Zápas bez schváleného tipu nikdy nedostane čerstvé kurzy v bodu 10 min.** Podmínka `r["approved"]` je v `nhl.py:375` a `closing.py:123`.
2. **Ani zápas se schváleným tipem nedostane nové rozhodnutí.** Čerstvé kurzy z bodu 10 slouží jen k CLV a zavíracímu kurzu.
3. **Rozhodnutí o NHL tedy padá jen v průchodech.** Zápas v 00:30 UTC má poslední rozhodnutí kolem 00:00 nebo 01:56 UTC z cache staré přibližně 18–20 h. Kurz je ZASTARALÝ, takže **v posledních 18 h před zápasem nemůže vzniknout nové APPROVED.**

   **NAMĚŘENO:** od rána 25. 11. bylo v průchodech 13:46, 19:55 a 02:04 schváleno 0 sázek, všechny kurzy byly ZASTARALÝ.
4. **Riziko:** čerstvější data by mohla změnit WATCH na APPROVED (kap. 9.3: 7 kandidátů) nebo naopak zrušit již schválený tip po pohybu kurzu. To se dnes nezjistí.

   Navíc NHL API nedává brankáře předem, takže brankáře v ranním rozboru nevíme. Večerní rozbor by brankářské zprávy z odpoledne USA mohl zachytit (ODHAD, neověřeno).

---

## 7. Simulace cache strategií (ODHAD)

Skript: `scripts/phase33_odds_audit.py simulate`. Výstup: `docs/phase-3-3-cache-simulation.csv`. Vstupem je 56 skutečných zápasů NHL v 6 herních dnech z 21.–27. 11.

Předpoklady:

- průchody v 06, 12, 18 a 00 UTC;
- obnova ligy NHL stojí 3 kredity;
- kurzy události stojí 1 kredit za trh, ve variantě D 3 trhy za zápas;
- limit aktuálnosti je 360 min;
- rozhoduje se v každém průchodu a ve variantách B, C a E i při každé obnově. Varianta D rozbor znovu nespouští.

| Ukazatel | A (dnes) | B (6 h) | C (3 h) | D (75/30/10) | E (adaptivní) |
|---|---|---|---|---|---|
| Modelované requesty za herní den | 1 | 4 | 8 | 29 | 2 |
| Modelované kredity za herní den | 3 | 12 | 24 | 87 | 6 |
| Kredity NHL za 30 dní | 90 | 360 | 720 | 2 610 | 180 (při 28 herních dnech přibližně 170) |
| Stáří cache při výkopu (medián) | 1 080 min | 180 min | 155 min | 10 min | 245 min |
| Stáří kurzu při posledním rozhodnutí (medián) | 720 min | 0 min | 0 min | 10 min | 90 min |
| Zápasy s rozhodnutím z aktuálních kurzů do 6 h před zápasem | 9 % | 95 % | 100 % | 80 % ¹ | 84 % |
| Riziko překročení rozpočtu (500 / měsíc) | **už teď nad tempem** (kap. 8) | vysoké | jisté | jisté | střední, jen s úsporou jinde |
| Riziko nedostupnosti dat | při rezervě 50 se zastaví všechny ligy | rychlejší dosažení rezervy | velmi vysoké | velmi vysoké | nízké, pokud je hlídané tempem |

¹ Varianta D vyžaduje navíc nový rozbor v bodech (změna logiky); bez něj se mění jen CLV, ne rozhodnutí.

**Varianta E (návrh modelu, neimplementovat):** jedna obnova ligy NHL přibližně 90 min před prvním zápasem herního dne a nový rozbor `nhl.run_and_store`. Provede se jen při splnění všech podmínek:

1. V nejbližších 6 h je zápas.
2. Cache je starší než 360 min, takže by jinak byla ZASTARALÁ.
3. Z posledního rozboru existuje kandidát WATCH s EV ≥ 3 % (důležitost trhu), nebo otevřený APPROVED tip (kontrola pohybu kurzu).
4. Zbytek kreditů minus 3 je aspoň rezerva plus denní tempo (kap. 8).
5. Ve skenu jsou aspoň 2 kanceláře mimo burzy pro tržní model B.

Podmínka 3 by spotřebu dál snížila. Kolik herních dnů ji splní, **nelze z 6 dnů spolehlivě určit**; v datech byl takový kandidát 21. 11.

---

## 8. Rozpočet API (ODHAD nad NAMĚŘENÝMI daty)

**API-Football:**

- 78–358 requestů denně (**NAMĚŘENO**) z 7 500, tedy nejvýš 4,8 %.
- Rozpočet není omezením: další bod na zápas by stál přibližně 10–30 requestů denně.
- Rezervy podle priority fungují. **NAMĚŘENO:** 0× SKIPPED_BUDGET.

**The Odds API:**

- Limit je 500 za měsíc. Ke dni 26. 11. 07:13 UTC zbývá 239 kreditů a použito je 261.
- Denní spotřeba podle počítadla `used`: 27 / 19 / 15 / 21 / 26, průměr **21,6** (**NAMĚŘENO**, 5 dní).
- Rozložení (**NAMĚŘENO**, 25. 11. 13:38 až 26. 11. 06:01, 38 kreditů):

  | Položka | Kredity |
  |---|---|
  | Obnovy fotbalových lig (12 × 2) | 24 |
  | NHL (1 × 3) | 3 |
  | Profil 4 – stažení NHL událostí a dvě zahraniční ověření fotbalu | 11 (po voláních se neeviduje) |

- **Plýtvání (NAMĚŘENO):** ranní průchod 26. 11. obnovil UCL za 2 kredity, přestože žádný zápas do 48 h nebyl. Uložené `next_kickoff` bylo ještě 25. 11., takže kontrola v `pipeline.py:286` soutěž nepřeskočila. Totéž nastává po každém kole soutěže.

Výstup: `docs/phase-3-3-api-budget-simulation.csv`. Předpoklad: 30 dní; ostatní spotřeba = 21,6 − 3 = 18,6 kreditu denně.

| Scénář | Varianta | Kreditů denně | Kreditů za 30 dní | Zbývá z 500 | Vejde se s rezervou 50? |
|---|---|---|---|---|---|
| Konzervativní | A + tempo čerpání (pacing) | 21,6 | 648 | −148 | **ne** – už dnešní stav vyžaduje strop nebo úsporu |
| Vyvážený | E (podmíněně) | 24,6 | 738 | −238 | ne bez úspory nebo vyššího plánu |
| Agresivní | B se stropem | 30,6 | 918 | −418 | ne |

Výklad pro jednotlivé scénáře:

- **Konzervativní:**
  - Přínos: žádný nový.
  - Riziko: při dnešním tempu dojde k rezervě 50 přibližně za (239 − 50) / 21,6 ≈ 8,7 dne, tedy kolem 4.–5. 12., pokud se počítadlo dřív nevynuluje. **Doplněno (kap. 8.1):** reset je 1. 12., takže v listopadu k rezervě nedojde; při stejném tempu by ale v prosinci došlo k rezervě kolem 21. 12.
  - Pod rezervou automat přepne všechny ligy na `cache_only`. Kurzy pak stárnou a Profil 4 nic neschválí.
- **Vyvážený:**
  - Přínos: u NHL rozhodnutí z čerstvých dat (84 %).
  - Náklad: přibližně +3 kredity za herní den.
  - Proveditelný, jen když se jinde ušetří aspoň stejně (například vynechání obnov soutěží bez zápasu) (reset 1. dne měsíce nic neušetří, jen určí, kdy počítadlo začne znovu).
- **Agresivní:** s limitem 500 neproveditelný.

### 8.1 Měsíční reset kreditů (doplněno 26. 11. 2026)

**Zdroj:** oficiální FAQ The Odds API (https://the-odds-api.com/manage/faqs.html), ověřil uživatel.

- Kredity se automaticky resetují **první den každého kalendářního měsíce**.
- Fakturace je samostatná a probíhá v den výročí založení předplatného; s resetem kreditů nesouvisí.
- Přesnou hodinu a časové pásmo resetu dokumentace neuvádí.
- **Očekávané datum resetu: 1. 12. 2026.**

**Dopad na odhady (ODHAD nad NAMĚŘENÝMI daty):**

- 26. 11. 06:00 UTC: remaining 239, used 261, součet 500 = plánovaný limit (NAMĚŘENO z uložených hlaviček).
- Do 1. 12. zbývá přibližně 4,7 dne × 21,6 ≈ 100 kreditů → k resetu zůstane zhruba 130–140 kreditů. **V listopadu k rezervě 50 nedojde**, riziko „4.–5. 12.“ z první verze auditu odpadá.
- **Prosinec:** (500 − 50) / 21,6 ≈ 20,8 dne → při dnešním tempu by se rezerva 50 dosáhla kolem **21. 12.** a zbytek měsíce by automat jel jen z cache. R1 (tempo čerpání) proto zůstává P1, jen s menší naléhavostí.
- Denní strop pro R1 lze počítat jako (remaining − rezerva) / dny do 1. dne dalšího měsíce.

**Ověření po resetu (jen čtení, žádné testovací volání, žádné kredity navíc):**

1. Počkat na první běžné volání automatu po resetu. Průchody jsou zhruba v 00, 06, 12 a 18 UTC; protože hodina a pásmo resetu nejsou známé, průchod v 00 UTC 1. 12. může ještě proběhnout před resetem.
2. Spustit `docker exec -i tipsbot python - < scripts/phase33_quota_check.py` (DB `mode=ro`, ověří, že zápis selže). Skript vypíše poslední hlavičky `x-requests-remaining` / `x-requests-used`, `used` po dnech a zbytek uložený u každé ligy v okamžiku jejího stažení.
3. Hodnoty prvního volání po resetu: `docker logs -t tipsbot-auto | grep "zbyva kreditu"` (zbytek po každém volání s časem). Stav `odds_api_quota` drží jen poslední hodnotu, ne první.
4. **Očekávání:** `remaining + used = 500`, `used` po resetu malé (náklad volání od resetu, liga 2–3 kredity), `used` za 1. 12. nižší než za 30. 11.
5. **Pokud nesouhlasí:** nic neopravovat. Diagnostika (skript ji vypíše): jiný součet než 500 (jiný tarif nebo klíč), `used` neklesl (reset ještě neproběhl, jiné časové pásmo – zkontrolovat znovu po 06 UTC a 2. 12.), nečekaně vysoké `used` (spotřeba mimo bota nebo sdílený klíč).

---

## 9. Dopad na Profil 4

### 9.1 Kde a z čeho se penalizuje stáří (kód)

| Prvek | Metrika stáří | Vliv |
|---|---|---|
| Stav kurzu (`odds.py:58–64`) | `stamp` = čas poskytovatele | Nad 360 min ZASTARALÝ, po výkopu HISTORICKÝ |
| APPROVED / WATCH / NO BET (`decision.py:79–80`) | stav kurzu | Kurz, který není AKTUÁLNÍ, je měkké selhání: **nikdy APPROVED**. Při EV > 0 a bez tvrdého selhání WATCH, jinak NO BET. |
| EV a fair odds | **žádná** | EV se počítá z uložené ceny bez ohledu na stáří (`analysis.py:164–171`, `nhl.py:219–228`) |
| Robustnost – fotbal (`stress.py:86–112`, `113–117`) | čas poskytovatele | Složka „odds_freshness“ s váhou 10 ze 100: do 180 min plná, při 360 min ztráta přibližně 3,3 bodu, při 720 min ztráta 10 bodů |
| Robustnost – NHL (`nhl.py:197–203`) | **žádná** | Jen scénáře EV |
| Quality of Data (`intelligence.py:77–83`) | čas poskytovatele a zpoždění stažení | „aktuálnost kurzu“ 20 bodů; „zpoždění stažení“ 10 bodů (stažení více než 60 min po aktualizaci). Bez vlivu na rozhodnutí (Fáze 3). |
| Opportunity Ranking (`intelligence.py:234–235`) | čas poskytovatele | „aktuálnost kurzu“ 10 bodů, lineárně 1 − stáří/360. Bez vlivu na rozhodnutí. |
| Zavírací kurz a CLV (`history.py:202–281`) | čas poskytovatele a čas pozorování | Pozorování nejvýš 20 min a aktualizace nejvýš 60 min před výkopem. Jinak „zastaralý“ nebo „jen poslední pozorovaný“. |
| Pokrytí `odds` (`analysis.py:88`) | čas stažení | Jen informace; `CoverageReport.quality()` stáří nepoužívá a `oldest()` se nikde nevolá |

**Může se kurz jevit čerstvý jen proto, že byl znovu stažen?** U API-Football a kurzového API **ne**: všechny výpočty jdou přes `stamp`.

Výjimky:

1. Ruční a importovaný kurz (čas zadání), záměrně.
2. Kurzové API samo posouvá `last_update` při každé kontrole kanceláře (kap. 4, bod 3). U NHL proto „stáří“ znamená, jak dávno kurzové API cenu naposledy potvrdilo, ne kdy se cena naposledy změnila. Pro rozhodování je to správný údaj (cena platila v tu chvíli), jen ho nesmíme číst jako dobu stability trhu.

### 9.2 Rozhodnutí podle běhu (NAMĚŘENO, `rankings`)

| Běh (UTC) | Sport | Stáří kurzu (medián) | APPROVED | WATCH | NO BET |
|---|---|---|---|---|---|
| 25. 11. 06 | NHL | 2 min | 8 | 3 | 89 |
| 25. 11. 09 | NHL | 187 min | 8 | 3 | 89 |
| 25. 11. 10 | NHL | 269 min | 8 | 3 | 89 |
| 25. 11. 13:45 | NHL | 457 min (ZASTARALÝ) | 0 | 11 | 89 |
| 25. 11. 19:55 | NHL | 826 min (ZASTARALÝ) | 0 | 11 | 89 |
| 26. 11. 06 | NHL | 2 min | 7 | 8 | 93 |
| 25. 11. 06 | fotbal | 162 min | 9 | 328 | 5 101 |
| 26. 11. 06 | fotbal | 107 min | 11 | 279 | 4 784 |

Běhy 13:45 a 19:55 jsou v `rankings` uložené s `valid = 0`, protože kurz nebyl aktuální. Průchod 26. 11. 02:04 v `rankings` záznam nemá (N/A). Podle logu v něm bylo 0 schválených.

U fotbalu na stáří prakticky nezáleží: rozhoduje jen ranní sken a stáří je tam kolem 100–160 min.

### 9.3 Uložené výběry se ZASTARALÝM kurzem (NAMĚŘENO, `tips`)

**Fotbal:** 7 z 450 výběrů (4 WATCH, 3 BEST AVAILABLE (WATCH)). Stáří je pod limitem i u 78 schválených sázek (medián 152 min).

**NHL:** 38 výběrů:

| Skupina | Počet |
|---|---|
| Stejný výběr byl dříve schválen z čerstvých dat | 20 |
| Neschválen nikdy, ostatní uložené prahy nesplňuje | 11 |
| **Neschválen nikdy, ale uložené EV ≥ 3 %, konzervativní EV ≥ 0 a robustnost ≥ 60** | **7** |

Všech 7 kandidátů z poslední skupiny je ze stejného průchodu 21. 11. 00:30 UTC (OU 6,5 Over, kurzy 2,08–2,28).

Jde o **aproximaci**:

- důvody rozhodnutí se neukládají;
- shoda modelu a trhu, kvalita dat a integrita se ověřit nedají;
- nevíme, jestli by čerstvý kurz byl stejný.

Výsledky (6 výher, 1 prohra) jsou **bez statistické váhy** a nesmí sloužit jako argument pro změnu strategie.

---

## 10. Doporučení (neimplementováno)

| ID | Priorita | Doporučení | Důkaz | Dotčené soubory | Přínos | Riziko | Náklad | Rollback |
|---|---|---|---|---|---|---|---|---|
| R1 | **P1** | Zavést **tempo čerpání**: denní strop = (zbytek − rezerva) / dny do resetu (reset = 1. den kalendářního měsíce, kap. 8.1). Pod stropem obnovovat jen soutěže se zápasem do 24 h. | 21,6 kreditu denně proti tempu 16,7; rezerva 50 vypne všechny ligy (`auto.py:41`) | `auto.py` (`wants_fresh_odds`), `config.py` (`TimingConfig`), `storage.py` (`odds_api_quota`) | Brání výpadku kurzů koncem měsíce | Méně častá obnova některých lig | 0 kreditů navíc | Vrátit commit; konfigurace |
| R2 | **P1** (po R1) | **Varianta E pro NHL:** podmíněná obnova ligy přibližně 90 min před prvním zápasem herního dne a nový rozbor. | Kap. 6.3 a 7: 9 % → 84 % zápasů s rozhodnutím z aktuálních kurzů do 6 h | `auto.py` (`run_forever`), `profil4/nhl.py` (`run_and_store`), `config.py` | Rozhodnutí blíž zápasu; zachytí pohyb kurzu i zprávy o brankářích | +3 kredity za herní den; nové APPROVED pozdě večer (upozornění) | ~170 kreditů měsíčně bez podmínky, méně s podmínkou | Vypínač v konfiguraci |
| R3 | P2 | Neobnovovat soutěž, jejíž uložené `next_kickoff` už proběhlo a nemá otevřený tip. Nejdřív zjistit rozpis levně (API-Football fixtures, nebo stav z poslední cache). | 26. 11. 06:00: UCL za 2 kredity, 0 zápasů | `pipeline.py:284–288` | Přibližně 2 kredity po každém kole soutěže | Nový první zápas soutěže by se načetl o průchod později | Úspora | Vrátit commit |
| R4 | P2 | Evidovat spotřebu kurzového API po volání (účel, liga, kredity podle rozdílu `x-requests-used`). | Kap. 8: 11 kreditů nejde rozepsat | `storage.py`, `oddsapi.py`, `profil4/nhl.py`, `profil4/closing.py` | Přesný rozpočet a kontrola | Žádné | 0 kreditů | Vrátit commit |
| R5 | P2 | Opravit dokumentaci a komentáře: API-Football „obnovuje po ~3 h“ (`profil4/config.py:88`, `:97`, `closing.py:11–12`). Naměřené jsou dávky v hodinách 03, 04, 12, 06 a 08 UTC a medián intervalu pozorovaných verzí přibližně 7,8 h (horní odhad). **Prahy neměnit.** | Kap. 5.2 | komentáře, `docs/` | Přesný popis zdroje | Žádné | 0 | – |
| R6 | P3 | Volitelně ukládat důvody rozhodnutí (`reasons`) do `tips`, aby šlo přesně spočítat, kolik WATCH má jako jedinou vadu zastaralý kurz. | Kap. 9.3 (jen aproximace) | `profil4/history.py`, `profil4/store.py` (nový sloupec, migrace se zálohou) | Přesné měření dopadu | Migrace DB | 0 kreditů | Sloupec lze ignorovat |
| R7 | P3 | Fotbal: další bod 3–5 min před výkopem **nedoporučuji**. | Mezi body 30 a 10 jen 6 % nových verzí | `profil4/config.py` (`closing_slots_min`) | Malý: odhadem nejvýš jednotky procent zápasů | Mění logiku posledního bodu (NHL, zahraniční ověření) | +1 request API-Football na zápas | Konfigurace |

**1. Co je nutné opravit:**

- Žádná chyba v kódu, která by kurzy kazila, nalezena nebyla (P0 = 0).
- Nutné je vyřešit rozpočet kurzového API (R1).

**2. Co je vhodné optimalizovat:** R2, R3, R4.

**3. Co není problém:**

- cache klíč a obcházení cache v bodech;
- deduplikace a ukládání;
- práce s časem poskytovatele;
- rozpočet API-Football;
- chyby API: 5 síťových chyb za 7 dní, 0 chyb kvóty, 0 SKIPPED_BUDGET.

**4. Limit poskytovatele:**

- API-Football: dávkové aktualizace a jeden čas za zápas.
- Kurzové API: měsíční limit 500 kreditů; `last_update` znamená čas kontroly kanceláře, ne změny ceny.

**5. Limit architektury:**

- Odds API cache hlavního bota drží jen jednu verzi na ligu.
- NHL se rozhoduje jen v průchodech.
- Bod 10 min slouží jen pro CLV a jen u schváleného tipu.
- Spotřeba po voláních se neeviduje.

**6. Nejlepší poměr přínos / cena:**

- R1, R3 a R4 – stojí 0 kreditů;
- R2 až po R1.

**7. Co zvýší spotřebu API:**

- R2 (+3 kredity za herní den);
- varianty B, C a D;
- R7 (jen API-Football, zanedbatelné).

**8. Co by mohlo zhoršit kvalitu tipů:**

- **R2:** nová APPROVED vznikající těsně před zápasem z jednoho stažení. Proto je potřeba podmínka aspoň 2 kanceláří.
- **R1:** příliš přísný strop by mohl nechat fotbalové ligy zastarat.
- Snížení limitu 360 min bez vyšší obnovy by zablokovalo i dnešní fotbalová APPROVED (medián 152 min) jen málo, ale u NHL všechna kromě ranního průchodu.

---

## 11. Testovací plán pro budoucí implementaci

Všechny testy offline, s falešným transportem nebo funkcí `fetch` (vzor `tests/test_profil4*.py`), bez sítě a bez kreditů.

| # | Scénář | Očekávání |
|---|---|---|
| T1 | Stažení v T2, `source_time` = T1 < T2 | `age_minutes` se počítá od T1; stav podle T1; v ranking a quality je zpoždění stažení = T2 − T1 |
| T2 | Opakované stažení stejného kurzu se stejným `source_time` | `save` vrátí 0; žádný nový řádek; pozorování se zapíše s `n_quotes` > 0 |
| T3 | Stejná kancelář, nový `source_time`, jiná hodnota | Nový řádek; `latest` vrátí novou hodnotu; starý řádek zůstane beze změny |
| T4 | Chybí timestamp poskytovatele (`update` nebo `last_update` = None) | `stamp` = `fetched_at`; v Quality „čas aktualizace u zdroje = čas stažení“; nesmí spadnout |
| T5 | Neplatný timestamp (nesmysl, budoucnost) | Řádek se odmítne nebo `stamp` se omezí na `fetched_at` (rozhodnout v implementaci); žádné záporné stáří |
| T6 | Zastaralá cache (> 360 min) | Kurz ZASTARALÝ, rozhodnutí nikdy APPROVED |
| T7 | Vyčerpaný API budget (kurzové API pod rezervou; API-Football `remaining` = 0) | Žádný request; `SKIPPED_BUDGET` nebo `cache_only` s poznámkou; tempo čerpání (R1) nepustí obnovu nad denní strop |
| T8 | Nedostupné API (výjimka, 5xx, 429) | Pozorování se stavem a poznámkou bez klíče (`redact`); cache se nepřepíše; staré `odds_quotes` beze změny |
| T9 | Zápas bez APPROVED tipu v bodu 10 | Dnes: žádné stažení události. Po R2: jen pokud splní podmínky varianty E. |
| T10 | Zápas s APPROVED tipem v bodu 10 | Stažení jen trhů tipu; uloží se jako zavírací kurz; po R2 se rozhodnutí přepočítá jen v samostatném běhu, ne v bodu |
| T11 | Souběžné stahování (vlákno předvýkopových bodů a průchod automatu) | Žádná duplicita řádků; žádné `database is locked`; počítadlo kreditů monotónní |
| T12 | Duplicitní odpověď API (stejný výběr dvakrát) | Stejný kurz = jeden řádek; různý kurz = výběr vynechán (`parse_api_odds`) |
| T13 | Časová pásma a UTC | Všechny časy UTC-aware; `next_kickoff` naivní z `tipsbot.db` se porovnává správně |
| T14 | Přechod letního a zimního času | Ranní průchod v 07:00 Praha = 05:00 UTC v létě a 06:00 UTC v zimě; denní strop počítán po dnech UTC |
| T15 | Zákaz přepisu historie a úniku dat | Snímek `odds_quotes` a `tips` před a po běhu: staré řádky bajtově stejné; rozbor v čase T nepoužije kurz s `fetched_at` > T (test na `latest(before=)` podle `fetched_at`) |

---

## 12. Otevřené otázky a chybějící data

1. ~~Datum měsíčního resetu kreditů~~ **Vyřešeno (kap. 8.1):** 1. den kalendářního měsíce, další 1. 12. 2026. Otevřená zůstává jen hodina a časové pásmo resetu; ověří se read-only po resetu.
2. **Spotřebu po voláních** nelze rozepsat, protože kredity se evidují jen jako stav počítadla.
3. **Skutečná frekvence aktualizací API-Football** je jen horní odhad: verze mezi stáhnutími nevidíme. Přesnější obraz by dal jen častější stahovací test, a ten tato fáze zakazuje.
4. **Důvody rozhodnutí** se neukládají, takže dopad zastarání na WATCH je jen aproximace (kap. 9.3).
5. **Historie obnov cache hlavního bota** neexistuje, `odds_cache` drží jednu verzi. Obnovy NHL se odvozují z `rankings` a `odds_quotes`.
6. **Vzorek** je 6 herních dnů NHL a 73 fotbalových zápasů s body, tedy jeden týden. Odhady přenášené na celý měsíc (spotřeba 21,6 kreditu denně) mohou kolísat podle programu pohárů.
7. **Mimo rozsah, k samostatnému ověření:** ranní průchod 26. 11. hlásí u hlavního bota „NHL: zapasu 15, s ferovou linii 0, s modelem 0“, zatímco předchozí průchod měl 3 ze 3. Profil 4 NHL ve stejnou chvíli rozhodoval normálně. Příčina nebyla zkoumána, s čerstvostí kurzů nesouvisí.

---

## 13. Závěrečná kontrola

| Kontrola | Výsledek |
|---|---|
| Produkce nezměněna (image, kontejnery, restarty) | viz závěrečný report (ověřeno po dokončení) |
| Databáze nezměněny | Jen `mode=ro`; zkušební zápis selhal („attempt to write a readonly database“) |
| Žádné nové API requesty | Skript ani `freshness.report` síť nepoužívají; zbytek kreditů se během auditu měnil jen provozem automatu |
| Strategie, profily a prahy | Beze změny (změněny jen `docs/` a `scripts/phase33_odds_audit.py`) |
| Historické záznamy | Beze změny |
| `main` | Beze změny (8b44073) |

Pomocný skript `scripts/phase33_odds_audit.py`:

- **`extract`** běží v kontejneru a čte DB pouze pro čtení.
- **`simulate`** běží lokálně nad JSON.
- Není součástí balíčku `tipsbot`: Dockerfile instaluje jen `src/`.
