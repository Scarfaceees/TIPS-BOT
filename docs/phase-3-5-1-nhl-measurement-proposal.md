# Phase 3.5.1 – NHL Model Measurement Integrity (návrh ke schválení)

Stav k 26. 11. 2026. **Jen audit a implementační návrh** – nic se neimplementovalo.

Navazuje na [phase-3-5-nhl-model-0-audit.md](phase-3-5-nhl-model-0-audit.md). Čísla rozptylu vycházejí ze stejných ověřených záloh `backups/predB1-20261126-1715/` (čtení `mode=ro`).

**Co se nemění** (návrh to ani nepotřebuje):
- matematický model (`models/nhl.py`), váha 0,15 (`config.league_model_weight["NHL"]`), EV a schvalovací prahy;
- rozhodování hlavního bota i Profilu 4;
- historické tabulky (`predictions`, `tips`, `state.model_check:NHL`);
- počet API požadavků: **0 nových**. Měření používá jen data, která bot už stahuje.

---

## 1. Shrnutí

- Měření bude **zvláštní, jen přibývající evidence** v `profil4.db`, oddělená od rozhodování. Vznikne při každém skenu NHL Profilu 4 (`profil4/nhl.run`) a výsledky dostane při existujícím vyhodnocení (`profil4/nhl.settle`).
- U každého zápasu, trhu a okamžiku se uloží:
  - **čistý model** (bez ukotvení na trh);
  - **použitý model** (ukotvený, jak ho dnes systém používá);
  - **trh** (ostrý i konsenzus kanceláří);
  - **obě směsi** (hlavní bot, Profil 4);
  - všechny časy, zdroje a příznaky kvality.
- Každý záznam dostane **třídu nezávislosti**. Tržně ukotvená predikce se nikdy nevydává za nezávislou.
- Před sběrem se pevně stanoví **pravidla srovnatelnosti a plán vyhodnocení**: který vzorek, metrika a test, a kdy se smí rozhodnout. Tím se vyloučí dodatečné vybírání výsledků.
- **Velikost vzorku:** odhad 470 zápasů z auditu je pro rozhodnutí **nedostatečný**:
  - počítal se silou testu jen ~50 % a s efektem 0,01 log loss, který je nerealisticky velký;
  - realistické efekty potřebují tisíce zápasů;
  - test „přidává směs 15 % hodnotu“ je řádově citlivější než test „je model sám lepší než trh“ (kap. 7).

---

## 2. Výchozí stav – co dnes chybí

| Požadavek | Dnes | Doklad | Mezera |
|---|---|---|---|
| Čistý model před korekcí trhem | neukládá se | `pipeline.ModelProvider.probabilities` vrací jen ukotvený výstup `nhl.market_probabilities` | **chybí** |
| Pravděpodobnost trhu bez marže | `predictions.p_market` (Profil 4: Pinnacle, jinak medián ≥ 2 kanceláří), `model_check:NHL` (hlavní bot: ostré kanceláře) | `profil4/nhl.market_b`, `modelcheck.log_predictions` | dvě různé definice bez označení zdroje u řádku |
| Výsledná směs | `predictions` model C (lineární), hlavní bot ukládá jen u tipů (`tips.model_probability`) | `profil4/nhl.candidates`, `devig.assess_value` | směs hlavního bota pro všechny zápasy chybí |
| Ukotvení OU | jen `anchored` ve slovníku pravděpodobností, neukládá se | `models/nhl.market_probabilities` | **chybí** příznak, kotevní linie, cílová p, faktor `k` |
| Čas výpočtu modelu | `predictions.predicted_at` = začátek analýzy | `history.record_predictions` | ok, ale **přepisuje se** (jen poslední sken) |
| Čas získání kurzu | jen v `state.odds_cache:NHL` (poslední), u řádku ne | `Ledger.odds_cache` | **chybí** u záznamu |
| Čas poskytovatele | `predictions.odds_time` = čas nejlepší nabídky, ne referenčního trhu | `quotes_from_event` | chybí čas referenčních kanceláří (Pinnacle) |
| Kancelář a vstupy trhu | jen nejlepší nabídka | — | chybí seznam kanceláří a jejich váhy |
| Kvalita a fallback | `coverage` jako stav zdroje (JSON v `tips.data_sources`) | `CoverageReport` | chybí příznaky modelu (studený start, výchozí brankář, stáří MoneyPuck) |
| Vyřazení ze hodnocení | — | — | **chybí** |

Stávající data zůstanou, jak jsou. Při vyhodnocování se označí jako **„legacy – nesrovnatelné“**: chybí čistý model a příznak ukotvení a trh B byl v průměru ~14 h starý (audit F4). Doplnit je zpětně nejde a nebude se to dělat.

---

## 3. Datový model (nové tabulky v `profil4.db`, jen přidání)

**Proč `profil4.db`:**
- NHL zápasy tam mají stabilní id z NHL API;
- `nhl.settle` tam už stahuje výsledky;
- `Store` umí `CREATE TABLE IF NOT EXISTS` (`profil4/store.py`, `SCHEMA`).

Hlavní `tipsbot.db` se nemění.

### 3.1 `nhl_snapshots` – jeden řádek na sken × zápas (rozhodovací okamžik)

| Sloupec | Význam / zdroj |
|---|---|
| `id`, `scan_id` | `scan_id` = čas začátku `nhl.run` + krátký hash (všechny zápasy jednoho skenu) |
| `fixture_id`, `match`, `kickoff` | `Fixture` z `nhl.run` |
| `computed_at` | **skutečný čas výpočtu modelu** (`utcnow()` těsně po `provider.probabilities`) |
| `odds_fetched_at` | čas stažení kurzů = `ledger.odds_cache("NHL")[0]` |
| `odds_cost_source` | `cache` / `fresh` (kurzy z cache, nebo z čerstvého stažení v tomtéž průchodu) |
| `ref_sharp_books`, `ref_sharp_stamp_min`, `ref_sharp_stamp_max` | kanceláře férové linie hlavního bota a jejich `last_update` |
| `ref_p4_source`, `ref_p4_books`, `ref_p4_n` | Profil 4: „Pinnacle bez marže“ / medián N kanceláří a jejich seznam |
| `ref_age_min` | `computed_at − ref_sharp_stamp_max`, jinak u Profilu 4 nejnovější razítko referenčních kanceláří |
| `data_last_game` | poslední datum v MoneyPuck `all_teams.csv` (`NHLRatings.past["date"].max()`) |
| `data_file_mtime` | časy změny `all_teams.csv` a `goalies.csv` |
| `home_games`, `away_games` | počet zápasů aktuální sezóny v ratingu (`TeamRating.games`) |
| `cold_start_home/away` | rating míchaný s minulou sezónou (`games < cold_start_games`) |
| `goalie_home/away_src` | `manual` (panel) / `weighted` (podíl odchytaných) / `missing` (`Goalie()` = GSAx 0) |
| `b2b_home/away` | `GameContext.*_b2b_travel` |
| `lambda_raw_h/a` | `nhl.expected_goals(ctx)` (před ukotvením) |
| `anchored`, `anchor_market`, `anchor_target`, `anchor_k` | `pipeline._total_anchor(ev)`; `k = model["lambda_home"] / lambda_raw_h` |
| `lambda_used_h/a` | `model["lambda_home/away"]` (výstup použitý v rozhodování) |
| `missing` | JSON seznam chybějících vstupů (tým bez ratingu, chybí trh, chybí Pinnacle…) |
| `comparable`, `exclusion` | výsledek pravidel z kap. 5 (1/0 + důvody), přepočítatelný při vyhodnocení |
| `model_version`, `config_hash`, `code_rev` | verze; `code_rev` = commit image (proměnná prostředí z buildu, jinak `unknown`) |

### 3.2 `nhl_measurements` – jeden řádek na snímek × trh × výběr

| Sloupec | Význam |
|---|---|
| `snapshot_id`, `market`, `selection`, `market_family` | `MONEYLINE` / `PUCKLINE` / `OU` |
| `p_raw` | **čistý model**: `nhl.simulate(lambda_raw_h, lambda_raw_a, rng(seed))` – stejný seed, bez ukotvení |
| `p_used` | model použitý v rozhodování (`provider.probabilities` → `market_probabilities`) |
| `independence` | `independent` pro `p_raw`; `p_used` je `anchored` (při `anchored = 1`) nebo `independent` (bez ukotvení) |
| `p_mkt_sharp`, `p_mkt_sharp_method` | férová linie hlavního bota (`devig.consensus_line` nad `sharp_and_offers`), metoda `shin` / `shin+power` |
| `p_mkt_p4` | trh B Profilu 4 (`profil4/nhl.market_b`) |
| `p_blend_main` | `devig.blend_probability(p_used, p_mkt_sharp, w)`, `w` z konfigurace (dnes 0,15), uložené zvlášť |
| `p_blend_p4`, `p_cons_p4` | C = lineární směs (`w·A + (1 − w)·B`), D = `min(B, C)` – jak počítá Profil 4 |
| `w` | použitá váha (pro dohledatelnost; nemění se) |
| `best_odds`, `best_book`, `best_stamp` | nejlepší nabídka (pro CLV a ROI v kap. 6) |
| `in_decision` | zda trh v tomto okamžiku vstoupil do rozhodnutí (hlavní bot: kandidát existoval; Profil 4: kandidát existoval) |

**Proč dvě tabulky:** zápasové údaje se v řádcích trhu neopakují, a jeden řádek trhu tak má ~20 sloupců.

**Odhad objemu:**
- ~15 zápasů × ~10 výběrů × ~8 skenů před zápasem (48h okno) ≈ 80 řádků na zápas;
- sezóna ~1 312 zápasů ≈ 105 000 řádků `nhl_measurements` a ~10 500 `nhl_snapshots`, tedy jednotky MB;
- `profil4.db` má dnes 199 MB.

### 3.3 `nhl_results` – výsledek zápasu jednou

`fixture_id` (PK), `final_h/a`, `reg_h/a`, `end` (`REG` / `OT` / `SO`), `source`, `fetched_at`.

- Zapisuje se v `profil4/nhl.settle` z **už stažených** dat (`g` v cyklu, `nhl.py:463–468`), takže **žádný další požadavek**.
- Výsledek trhu se při vyhodnocení odvodí `nhl_settle.grade` (stejná funkce jako dnes), takže se nic nepřepisuje.

**Pravidlo:**
- všechny tři tabulky jen **přibývají**;
- `nhl_results` se zapisuje `INSERT OR IGNORE`, takže první výsledek platí;
- oprava výsledku se zapisuje jako nový záznam s důvodem, ne jako přepis.

---

## 4. Implementace – soubory a funkce

| Soubor | Změna | Vliv na rozhodování |
|---|---|---|
| `src/tipsbot/profil4/nhl_measure.py` (**nový**) | `build(fx, parsed_event, quotes, provider, model, cache_meta, now, weight) -> (snapshot, rows)` – **čistá funkce**, volá jen existující funkce: `nhl.expected_goals`, `nhl.simulate`, `pipeline._total_anchor`, `oddsapi.sharp_and_offers`, `devig.consensus_line`, `devig.blend_probability`, `profil4.nhl.market_b`; `comparability(snapshot, row, kickoff) -> (bool, reasons)`; `record(store, snapshot, rows)`; `record_result(store, fid, g)` | žádný |
| `src/tipsbot/profil4/store.py` | `SCHEMA` + 3 tabulky a indexy (`fixture_id`, `snapshot_id`) | žádný |
| `src/tipsbot/profil4/nhl.py` – `run` | po `model = provider.probabilities(parsed)` a `analyse(...)`: `try: nhl_measure.record(...) except Exception: log.warning(...)`. Zápis až po rozhodnutí, ze stejných objektů. | žádný (chyba měření se jen zaloguje) |
| `src/tipsbot/profil4/nhl.py` – `settle` | v cyklu `for fid …` doplnit `nhl_measure.record_result(store, fid, g)` | žádný, 0 nových požadavků |
| `src/tipsbot/profil4/nhl_eval.py` (**nový**) | jen čtení: sestavení vzorků A/B/C podle kap. 6, metriky, bootstrap po zápasech, kalibrace, test vnoření, výkaz | žádný |
| `src/tipsbot/profil4/cli.py` | příkaz `nhl-measure-report` (jen čtení, `mode=ro`) | žádný |
| `docs/` | předem stanovený plán vyhodnocení (kap. 6–7) jako samostatná verze | — |

**Co se záměrně nemění:**
- `models/nhl.py`, `pipeline.py`, `devig.py`, `profil4/decision.py`, `config.py`;
- `history.record_predictions`, `modelcheck` – zůstávají jako legacy evidence.

**Čistý model bez úpravy modelu:**
- `market_probabilities` už vrací `lambda_home/away` po ukotvení;
- `expected_goals` je veřejná funkce;
- čistý výstup = `simulate(expected_goals(ctx), rng = default_rng(seed))` se stejným seedem `crc32(match_id)`. To je přesně větev, kterou `market_probabilities` použije bez kotvy (ověří test T2);
- kontext `ctx` měření získá stejně jako `ModelProvider.probabilities` (`ratings.context(home, away, day)`), ze stejné instance `provider` – žádné druhé načtení dat;
- **cena:** jedna Monte Carlo simulace (5 000 iterací) navíc na zápas a sken, řádově milisekundy.

**Trh hlavního bota v Profilu 4:** `build` přepočítá férovou linii ostrých kanceláří ze stejné `parsed` události (čistá funkce). Hlavní bot a Profil 4 tak mají v jednom snímku obě reference ze **stejného stažení**.

---

## 5. Časová konzistence a srovnatelnost (stanoveno předem)

**Rozhodovací okamžik** = `computed_at` snímku. Model i obě reference trhu jsou ze stejného snímku a stejného `odds_fetched_at`, takže nikdy nejde o model a trh z různých stažení.

Snímek a trh jsou **srovnatelné** (`comparable = 1`), jen když platí vše:

| Pravidlo | Podmínka | Důvod vyřazení |
|---|---|---|
| S1 | `computed_at < kickoff` | predikce po začátku |
| S2 | reference trhu existuje (pro vzorek A/C: `p_mkt_sharp`, pro variantu P4: `p_mkt_p4` s `ref_p4_n ≥ 3` nebo Pinnacle) | chybí trh |
| S3 | `ref_age_min ≤ 180` | zastaralý trh |
| S4 | oba týmy mají rating, `missing` je prázdné | neúplný model |
| S5 | `kickoff.date − data_last_game ≤ 3 dny` | zastaralá data MoneyPuck |
| S6 | výsledek existuje a `grade` nevrátil vrácení (`push` / `void`) | bez výsledku |

**Příznaky** (nevyřazují, ale ve výkazu jdou zvlášť jako podskupiny):
- `cold_start`;
- `goalie_src = missing / weighted / manual`;
- `b2b`;
- `anchored`.

Konstanty 180 min a 3 dny patří do `nhl_measure` (měření), **ne** do rozhodovací konfigurace. Změnit je smí jen nová verze plánu vyhodnocení, nikdy zpětně.

**Výběr snímku pro hlavní vzorek** (jeden snímek na zápas, aby se zápasy nepočítaly vícekrát):
- **primárně poslední srovnatelný snímek před začátkem**;
- sekundárně první srovnatelný snímek (časná linie) pro test pohybu trhu (kap. 6.4).

---

## 6. Plán vyhodnocení

**Obecně pro všechny vzorky:**
- jedna strana na trh (domácí / `home−1,5` / `home+1,5` / over), aby se doplňkové páry nepočítaly dvakrát;
- **trhy se vyhodnocují zvlášť:**
  - MONEYLINE a PUCKLINE spolu silně souvisí (korelace rozdílu log loss po zápasech 0,56 na 41 zápasech), takže je nelze sčítat jako nezávislé důkazy;
  - souhrn přes trhy jen s bootstrapem po zápasech (cluster = zápas);
- intervaly: 95 % bootstrap po zápasech, 4 000 opakování;
- statistické rozhodnutí jen v předem daných kontrolních bodech (kap. 7.5).

### 6.1 A – vlastní NHL model proti trhu
- **Vzorek:**
  - snímky `comparable = 1`, `p_raw` (nezávislá predikce) proti `p_mkt_sharp` ze stejného snímku;
  - trhy MONEYLINE a PUCKLINE;
  - OU zvlášť jako „čistý model totalů“, s vědomím, že ho systém bez kotvy nepoužívá.
- **Metriky:**
  - rozdíl log loss a Brier (párově po zápasech);
  - kalibrace: koše po 0,1 s počty, sklon a průsečík kalibrace (logistická regrese `y ~ logit(p)`), ECE.
- **Hlavní test – vnoření (encompassing):**
  - logistická regrese `y ~ β_m·logit(p_mkt) + β_r·logit(p_raw)`;
  - model **přidává informaci**, jen když je `β_r` průkazně > 0 (jednostranně);
  - test odpovídá přímo na otázku „má model informaci navíc oproti trhu“ a nepotřebuje, aby byl model sám lepší než trh.
- `p_used` u OU (ukotvený) se do A **nezahrnuje nikdy**.

### 6.2 B – výsledná kombinace
- **Vzorek:** stejný jako A, ale `p_blend_main` (hlavní bot) a zvlášť `p_blend_p4` (Profil 4) proti `p_mkt_sharp`, resp. `p_mkt_p4`.
- **Metriky:** rozdíl log loss a Brier, kalibrace.
- **Otázka:** zlepšuje směs 15 % trh? Je to citlivější test než A, protože rozdíl směsi má asi 7× menší rozptyl (kap. 7).
- **Doplňkově:** profil vah 0 / 0,05 / 0,10 / 0,15 / 0,25 jen jako **popis**, nikdy jako automatická změna váhy.

### 6.3 C – tržní benchmark samotný
- **Vzorek:** všechny snímky s trhem (i bez modelu).
- **Metriky:**
  - kalibrace `p_mkt_sharp` a `p_mkt_p4`;
  - log loss podle stáří trhu (`ref_age_min` po pásmech 0–60 / 60–180 / 180+ min) a podle předstihu před zápasem.
- **Účel:** ukázat, jak moc se zlepší benchmark s čerstvostí. Pokud stáří mění log loss trhu víc než rozdíl model − trh, vzorky se stářím nad 180 min neporovnávají (pravidlo S3 se ověří daty).

### 6.4 CLV – jen pro ceny a pohyb trhu, ne pro kvalitu modelu
- **CLV tipů** (`tips.clv`):
  - měří, zda **cena** kanceláře porazila zavírací trh;
  - u NHL tipů Profilu 4 jsou téměř jen OU, kde model ≈ trh, takže CLV hodnotí výběr ceny, **ne model**;
  - ve výkazu zůstává zvlášť.
- **Pohyb trhu k modelu** (bez kreditů, z následujících snímků):
  - korelace `(p_raw − p_mkt_t0)` s `(p_mkt_last − p_mkt_t0)`, kde t0 = první srovnatelný snímek a „last“ = poslední snímek před začátkem;
  - kladná průkazná korelace znamená, že trh se pohybuje směrem k modelu (nezávislý doklad informace);
  - **omezení:** poslední snímek není skutečně zavírací (cache ~6 h). Ukládá se `ref_age_min` a výkaz to uvádí.

### 6.5 ROI a yield
- **Definice:** 1 jednotka na sázku; zisk z `mathcore.unit_profit`; ROI = Σ zisk / počet vyhodnocených sázek (vrácení jako 0 v čitateli i jmenovateli).
- **Vzorek:** jen skutečná rozhodnutí (`tips` podle `decision`), zvlášť APPROVED a WATCH a zvlášť trhy.
- **Síla:**
  - směrodatná odchylka zisku na sázku je ~1,03 (80 vyhodnocených NHL tipů);
  - ROI 3 % se průkazně odhalí až po ~9 200 sázkách;
  - ROI se proto **vykazuje popisně a nikdy není podkladem pro rozhodnutí o modelu**.

---

## 7. Velikost vzorku

### 7.1 Původ čísla 470
- Audit použil `n = (1,96 · σ / δ)²`:
  - σ = 0,110 je směrodatná odchylka rozdílu log loss model − trh na zápas (MONEYLINE, hlavní bot, 37 zápasů);
  - δ = 0,01.
- To je počet, při kterém má 95% interval poloviční šířku 0,01. **Síla testu je tedy jen ~50 %**: při skutečném rozdílu 0,01 by se průkaznost ukázala zhruba v polovině případů.
- Předpoklady výpočtu:
  - oboustranný test, α = 0,05;
  - nezávislé zápasy;
  - σ odhadnuté z 37 zápasů, takže samo nejisté (±25 %);
  - jeden trh.

### 7.2 Jaký efekt hledáme
- **Měřítko z historie (neověřené, z komentáře v konfiguraci):** celé zlepšení trhu od otevírací k zavírací linii bylo 0,6701 → 0,6677 = **0,0024** log loss.
- Efekt 0,01 by znamenal, že model je čtyřikrát informativnější než veškerý pohyb trhu před zápasem. To je u efektivního trhu NHL **nerealistické**.
- **Realistické cíle:**

| Otázka | δ (log loss) | Proč |
|---|---|---|
| A – model sám lepší než trh | 0,002–0,005 | v řádu pohybu trhu před zápasem |
| B – směs 15 % lepší než trh | 0,0005–0,002 | směs mění pravděpodobnost jen o ~15 % rozdílu model − trh |

### 7.3 Potřebné počty zápasů (α = 0,05 oboustranně, síla 80 %: `n = ((1,96 + 0,84)·σ/δ)²`)

σ jsou odhady z 37–41 zápasů a po 300 zápasech se přepočítají:

| Vzorek | σ na zápas | δ = 0,002 | δ = 0,005 | δ = 0,01 |
|---|---|---|---|---|
| A, MONEYLINE, model − trh (hlavní bot) | 0,110 | 23 900 | 3 830 | 960 |
| A, MONEYLINE, A − B (Profil 4) | 0,113 | 25 200 | 4 040 | 1 010 |
| A, PUCKLINE, A − B | 0,105 | 21 500 | 3 430 | 860 |
| A, OU, A − B (*ukotvené – jen ilustrace*) | 0,064 | 8 100 | 1 300 | 330 |
| B, MONEYLINE, směs 0,15 − trh (hlavní bot) | 0,016 | 530 | 85 | 22 |
| B, MONEYLINE, C − B | 0,016 | 510 | 82 | 21 |
| B, PUCKLINE, C − B | 0,015 | 430 | 70 | 18 |

Pro B je rozumné δ 0,0005–0,001: 0,0005 vyžaduje ~8 500 zápasů, 0,001 ~2 100 zápasů.

### 7.4 Co z toho plyne
- Jedna základní část sezóny NHL má 1 312 zápasů. MoneyPuck má do 24. 11. 385 odehraných zápasů (770 týmových řádků), do konce dubna jich tedy zbývá ~930.
- **Do konce sezóny** bude test A schopen odhalit jen velký efekt (≥ ~0,01) a test B efekt kolem 0,0015.
- Čistý model proti trhu **nelze v jedné sezóně spolehlivě potvrdit ani vyvrátit** při realistickém efektu. Proto je hlavním testem pro A **test vnoření** (6.1), který používá i rozptyl trhu, a **pohyb trhu k modelu** (6.4).
- Jejich sílu nejde z dnešních 41 zápasů poctivě spočítat. Odhadne se po 300 zápasech.
- **Rozdělení vzorků je nutné:** MONEYLINE a PUCKLINE zvlášť (souvislost 0,56), OU zvlášť a v A jen čistý model.
  - Souhrnný test přes trhy jen s bootstrapem po zápasech;
  - souhrn **nesmí** nahradit test po trzích, protože OU by výsledek přetížil (víc linií na zápas).
- **470 není dostačující hranice pro žádný trh.**

### 7.5 Kontrolní body (aby průběžné nahlížení nezkreslilo výsledky)
- Kontrolní body: **300, 600 a 950 srovnatelných zápasů** (orientačně konec prosince, polovina února a konec dubna).
- Hladiny podle O'Brien–Fleming (Lan–DeMets): přibližně α₁ ≈ 0,001, α₂ ≈ 0,015, α₃ ≈ 0,045 při celkové α = 0,05. Přesné hodnoty pro skutečné podíly informace se spočítají v M3 a zafixují v plánu v1.
- Mezi body se výkaz smí zobrazovat, ale **nerozhoduje se**.
- V bodě 300 se přepočítá σ a tabulka 7.3 a podle toho se případně upraví další body. Úprava jen novou verzí plánu, nikdy dodatečně podle výsledku.

---

## 8. Rizika

| Riziko | Opatření |
|---|---|
| Měření ovlivní rozhodování | Zápis až po `analyse`, jen čtení stejných objektů, `try/except` s varováním; zlatý test T7 (stejné tipy a predikce s měřením i bez něj) |
| Čistý model se spočítá jinak než v produkci | T2: bez kotvy `p_raw == p_used` (stejný seed); s kotvou `k = λ_used/λ_raw` a `simulate(λ_raw·k)` = `p_used` |
| Zámek DB (`database is locked`) | Jedna transakce na sken (`store` dávka), `BUSY_TIMEOUT_S = 60` jako dnes; žádný zápis během stahování |
| Souběžný start kontejnerů | `CREATE TABLE IF NOT EXISTS` v `SCHEMA` (sqlite3 bez SQLAlchemy); test souběhu jako u B1 |
| Růst DB | ~105 000 řádků na sezónu; indexy jen `snapshot_id` a `fixture_id` |
| CPU | +1 simulace (5 000) na zápas a sken; ověří se změřením času skenu před a po |
| Nové API požadavky | 0: výsledky z existujícího `settle`, kurzy z existující cache; testy s pojistkami `_no_network` a `_no_paid_api` |
| Dodatečné vybírání vzorku | Pravidla S1–S6 a výběr snímku stanovené předem (kap. 5); verze plánu se ukládá k výkazu |
| Záměna závěrů o ceně a o modelu | Výkaz odděluje A (model), B (směs), C (trh) a CLV/ROI (rozhodnutí) |
| Legacy data omylem v hodnocení | `nhl_eval` čte jen nové tabulky; `predictions` a `model_check` jen v sekci „legacy“ |

---

## 9. Testovací plán (mocky a fixture data, žádné skutečné API)

- **T1 `build`:** fixní zápas → snímek má všechny časy (`computed_at`, `odds_fetched_at`, razítka referenčních kanceláří), `ref_age_min`, kanceláře a příznaky kvality.
- **T2 Čistý model:** bez kotvy `p_raw == p_used` přesně; s kotvou `anchored = 1`, správná `anchor_market` a `target`, `k` v ⟨0,6; 1,6⟩ a přepočet ze `k` dá `p_used`.
- **T3 Třída nezávislosti:** OU s kotvou → `p_used` je `anchored` a ve vzorku A se neobjeví; `p_raw` vždy `independent`.
- **T4 Fallbacky:** tým bez ratingu → `missing`, `comparable = 0`; brankář chybí → `goalie_src = missing`; studený start a `b2b` jsou označené.
- **T5 Pravidla S1–S6:** každé zvlášť vyřadí záznam se správným důvodem; hraniční hodnoty 180 min a 3 dny.
- **T6 Výsledky:** `settle` s napodobeným `fetch` zapíše `nhl_results` jednou (`INSERT OR IGNORE`), **počet volání `fetch` se nezmění**.
- **T7 Zlatý test rozhodování:** `nhl.run` na stejných datech s měřením a bez něj (měření vypnuté monkeypatchem) → shodné `tips`, `predictions` i `rankings`.
- **T8 Selhání měření:** výjimka v `nhl_measure.record` → varování v logu, sken a zápis tipů proběhnou.
- **T9 Jen přibývání:** druhý sken přidá nový snímek, starý se nezmění; žádný `UPDATE` / `DELETE` nad tabulkami měření.
- **T10 `nhl_eval` na syntetických datech:**
  - model = pravda → A průkazně lepší;
  - model = trh → rozdíl 0, `β_r ≈ 0`;
  - model = šum → `β_r ≤ 0`;
  - bootstrap po zápasech (dva trhy jednoho zápasu = jeden cluster).
- **T11 Migrace:** nová i stará `profil4.db`, opakovaný a souběžný start; existující tabulky a řádky beze změny (otisky jako u B1).
- **T12 Síť:** celá sada s pojistkou `_no_network` – 0 pokusů.
- **Celá sada** 2× za sebou s izolovanými DB (jako u B1).

---

## 10. Doporučená posloupnost (každý krok ke schválení zvlášť)

1. **M1 – schéma a `build`** (bez napojení): tabulky v `SCHEMA`, `nhl_measure.build` a `comparability`, testy T1–T5, T11. Produkce se nemění.
2. **M2 – napojení:** volání v `nhl.run` a `nhl.settle`, testy T6–T9, T12, změření času skenu.
3. **M3 – výkaz:** `nhl_eval`, CLI `nhl-measure-report` (jen čtení), test T10; dokument „Plán vyhodnocení v1“ (kap. 5–7) jako pevná verze.
4. **M4 – nasazení** (samostatné schválení, postup jako B1): zálohy obou DB, ověření obnovy, postupný restart, kontrola prvních snímků. Bez dalších kreditů, NHL se stahuje stejně jako dnes.
5. **M5 – kontrolní bod 300 zápasů:** přepočet σ a síly, kontrola pravidel S3 a S5 na datech (kap. 6.3), případně plán v2. Teprve **po bodu 950** (nebo dříve jen při překročení hranice O'Brien–Fleming) rozhodnutí o modelu: ponechat / kalibrovat / doplnit / nový model.

**Mimo tento návrh** (samostatná témata):
- Pinnacle u NHL > 36 h před zápasem chybí (audit F1). Měření to jen zaznamená (`missing`, S2), nic nestahuje navíc;
- váha 0,15 zůstává; žádné zvýšení vlivu modelu jen proto, že počítá vlastní čísla.
