# Phase 3.5 – NHL „Model 0“: technický a matematický audit

Stav k 26. 11. 2026. **Pouze audit** – nic se neimplementovalo ani neopravovalo. Model, profily, prahy, historická data i produkce zůstaly beze změny.

**Metodika:**
- zdrojový kód na `dev` (commit `baf9503`);
- historická data jen z ověřených záloh `backups/predB1-20261126-1715/` (pořízené 26. 11. 17:15 UTC, integrita ověřena, `VERIFY OK`), otevřených s `mode=ro`;
- jedna diagnostika aktuální cache kurzů v kontejneru `tipsbot`: DB `mode=ro`, síť v procesu zablokovaná, výpočet jen z uložených dat;
- žádné API požadavky, žádný zápis, žádný restart.

Skripty analýzy jsou mimo repozitář (scratchpad sezení). Použité dotazy a výpočty jsou popsané v příloze A, aby šly zopakovat.

---

## 1. Shrnutí současného stavu

1. **„Model 0“ v kódu neexistuje.** Žádný identifikátor, verze ani konfigurace se tak nejmenuje.
   - Název pochází z řádku logu automatu `NHL: zapasu 15, s ferovou linii 0, s modelem 0` (`pipeline.ScanReport.summary`, `src/tipsbot/pipeline.py:35`).
   - „s modelem 0“ je **počet zápasů**, u kterých byl k dispozici model **a zároveň** férová linie trhu (`pipeline.py:349`: `with_model += int(model is not None and has_line)`).
2. **NHL model sám funguje a počítá vlastní pravděpodobnosti.**
   - Diagnostika aktuální cache (26. 11., 15 zápasů): model dal pravděpodobnosti pro **15 z 15** zápasů, 32 týmů má rating a 32 týmů brankáře.
   - Nula vznikla jinde: u žádného z 15 zápasů nebyla v kurzech ostrá kancelář (Pinnacle, Betfair, Sbobet), takže nešla sestavit férová linie. Bez ní hlavní bot zápas vůbec nehodnotí a model se nepoužije.
3. **Příčina nuly je časová, ne modelová.**
   - V kurzech NHL od kurzového API (region `eu`) se Pinnacle u zápasů **víc než 36 h před začátkem neobjevil ani jednou** (0 z 12 stažení). U zápasů 12–24 h před začátkem byl ve 49 z 50 stažení.
   - Cache z 06:00 UTC 26. 11. obsahovala jen zápasy 27./28. 11. (36 h a víc dopředu). Ostatní dny měly „s modelem“ 2–14.
4. **Co model dělá:**
   - xG z dat MoneyPuck + brankáři (GSAx) + únava, pak Monte Carlo simulace zápasu včetně prodloužení a nájezdů;
   - do tipu hlavního bota jde **jen s váhou 15 %**, zbytek tvoří trh (logit směs). V Profilu 4 jde také 15 %, ale lineární směsí;
   - **u počtu gólů (OU) je úroveň gólů ukotvená na tržní total.** Model tam z konstrukce téměř kopíruje trh.
5. **Vlastní predikční schopnost se z dostupných dat doložit nedá.**
   - Hlavní bot, vítěz zápasu (MONEYLINE), 37 zápasů: log loss modelu 0,6668 proti trhu 0,6775. Rozdíl −0,011, 95% interval (−0,043; +0,024), tedy neprůkazné.
   - Profil 4, 41 zápasů, všechny trhy: rozdíl A − B −0,004, interval (−0,019; +0,015), také neprůkazné. U OU je model horší než trh (+0,007).
   - Při zjištěném rozptylu je k průkaznosti rozdílu 0,01 potřeba **~470 zápasů**, pro 0,005 ~1 900 zápasů.
   - Jediný velký test (10 664 zápasů 2012–2021) je jen v komentáři konfigurace a v repozitáři ho nejde zopakovat. Podle něj byl samotný model **horší** než otevírací i zavírací linie.
6. **Schválené NHL tipy Profilu 4 (30) a všech 8 NHL tipů hlavního bota jsou téměř výhradně OU.** Tam je model ukotvený na trh.
   - Hodnota těchto tipů pochází z ceny kanceláře proti konsenzu trhu (lepší kurz u jedné kanceláře), **ne z modelu**.
   - Výsledky Profilu 4 (APPROVED: 23 vyhodnocených, ROI +7 %, interval −33 % až +54 %) jsou na závěr příliš malé.

**Odpověď na hlavní otázku (podrobně v kapitole 11):** NHL „Model 0“ je skutečný samostatný model, který vlastní pravděpodobnosti počítá. V rozhodování o tipech ale **převažuje trh**: 85 % váhy dostává trh a u OU je model ukotvený na trh. Vlastní predikční schopnost proti trhu **z dostupných dat nelze rozhodnout**.

---

## 2. Architektura

### 2.1 Identita

| Otázka | Zjištění | Doklad |
|---|---|---|
| Kde je „Model 0“ definován | Nikde. Jde o čítač v souhrnu skenu. | `grep -rniE "model ?0"` ve `src/` a `docs/` nic nenajde. Čítač: `pipeline.py:28,349` |
| Co je skutečný NHL model | xG/Fenwick model + GSAx brankářů + Monte Carlo | `src/tipsbot/models/nhl.py` |
| Kde se inicializuje | `ModelProvider.__init__` pro `Sport.NHL` | `pipeline.py:120–126` |
| Vstupní ratingy | `NHLRatings` (síla týmů k danému dni) | `src/tipsbot/nhl_data.py` |
| Samostatný model, nebo fallback? | Samostatný model. Fallback „jen trh“ nastává, když chybí model (`p_model = p_market`, `pipeline.build_candidates`, `pipeline.py:286`). Když chybí férová linie, nevznikne kandidát vůbec. | `pipeline.py:262–290` |
| Generuje vlastní pravděpodobnosti | Ano, pro všechny trhy (vítěz, puckline, totaly, týmové totaly, základní doba) | `models/nhl.simulate` |
| Verze | Hlavní bot `Settings.model_version = "0.1.0"`, Profil 4 `p4-1.0.0`. Žádná „0“. | `config.py:285`, tabulka `predictions.model_version` |

### 2.2 Rozdíl proti fotbalu

| | Fotbal (ligy) | NHL |
|---|---|---|
| Model | Dixon-Coles / Poisson z výsledků a xG (`models/football.py`), kalibrace 1X2 (`calibration`) | xG/Fenwick ratingy + Monte Carlo (`models/nhl.py`), **bez kalibrace** |
| Druhý názor | `secondopinion` (penaltyblog) | žádný (`second_opinion` vrací `None`, protože `params` je `None`) |
| Kontext | zranění, počasí, rozhodčí, rohy a karty | jen únava back-to-back; brankáři z podílu odchytaných zápasů nebo ručně v panelu; zranění z ESPN jen do rozboru, ne do čísel |
| Váha modelu | ligy 0,10, UNL 0,25 | 0,15 |
| Sledované profily | Základ, Profil 2, 3, 3x, rohy, karty | jen Základ a Tipy (`paper.track`, `paper.py:337`) |

### 2.3 Tok dat od vstupu k tipu

```
MoneyPuck all_teams.csv (denně) ─┐
MoneyPuck goalies.csv (denně) ───┼─► NHLRatings(as_of = dnešní UTC datum) ─► GameContext ─► expected_goals (λ_h, λ_a)
ruční brankáři z panelu ─────────┘                                                              │
                                                                                                ▼
kurzové API (cache odds_cache:NHL) ─► sharp_and_offers ─► consensus_line (Pinnacle/Betfair/Sbobet) ─► _total_anchor
                                         │                       │                                       │
                                         │                       └─► p_market (férová linie)             ▼
                                         │                                                  market_probabilities (MC 5 000)
                                         ▼                                                               │
                                     offers (ostatní kanceláře) ──────────────► build_candidates ◄────── p_model
                                                                                      │
                                                         Hlavní bot: evaluate → assess_value (logit směs 0,15 / 0,85) → Tip
                                                         Profil 4: profil4.nhl.candidates (A, B, C = 0,15·A + 0,85·B, D) → decide
                                                         Sledování: paper.track (Základ: čistý model proti kurzům)
```

**Klíčová vazba:** bez ostré kanceláře `consensus_line` vyhodí `DevigError`, `build_candidates` nic nevrátí a celý zápas v hlavním botu odpadne (`pipeline.py:272–275`). Profil 4 má jinou referenci trhu (kap. 8) a zápas hodnotí i bez Pinnacle.

---

## 3. Matematický popis

Všechny konstanty jsou v `config.NHLModelConfig` (`src/tipsbot/config.py:207–228`). Kód, který by je fitoval z dat, v repozitáři **není**. Jsou to tedy pevné, ručně zvolené hodnoty a jejich původ je **neznámý**, kromě vah modelu (kap. 3.8).

### 3.1 Síla týmu – `nhl_data._rating`, `NHLRatings.__init__`
- **Vstupy:** zápasy týmu s `situation == "all"` (všechny herní situace dohromady), sloupce `iceTime`, `xGoalsFor/Against`, `unblockedShotAttemptsFor/Against`.
- **Vzorec:** `xgf60 = Σ xGoalsFor / (Σ iceTime / 3600)`; obdobně `xga60`, `ff60`, `fa60` (Fenwick = neblokované střely).
- **Ligový průměr:** `league_xg60 = Σ xGoalsFor / hodiny` za aktuální sezónu. Když má aktuální sezóna méně než 200 řádků, počítá se z aktuální a předchozí.
- **Data:** naměřená (MoneyPuck). Jen zápasy s `date < as_of`.
- **Forma:** žádné časové vážení. Celá sezóna má stejnou váhu, takže **aktuální forma se nemodeluje**.

### 3.2 Studený start – `models/nhl.cold_start_rating`
- Když má tým v aktuální sezóně méně než `cold_start_games = 8` zápasů: `x = 0,60·x_minulá + 0,40·x_aktuální`.
- **Konstanty:** pevné (8 zápasů, 0,60).
- 26. 11. mají týmy ~23 zápasů, takže se nepoužívá.

### 3.3 Útok a obrana se střelbou – `_offence`, `_defence`
- `off = (1 − w)·xgf60 + w·ff60·(league_xg60 / league_ff60)`, kde `w = shot_volume_weight = 0,20` (pevná konstanta).
- `def` obdobně s `xga60` a `fa60`.

### 3.4 Očekávané góly – `expected_goals`
- `λ_h = off_h · def_a / league_xg60 · home_factor`, kde `home_factor = 1,035`;
- `λ_a = off_a · def_h / league_xg60 · away_factor`, kde `away_factor = 0,965`;
- back-to-back s cestou přes časová pásma: `λ ·= 1 − 0,05` (`b2b_xg_penalty`). Cesta se určuje podle `names.NHL_UTC_OFFSET`;
- brankář soupeře: `λ_h −= gsax60(brankář hostí)`, kde `gsax60 = GSAx / (střely + 1 500) · 28` (`goalie_regression_shots = 1500`, `shots_against_per_60 = 28`);
- spodní mez `λ ≥ 0,8` (`min_expected_goals`).
- **Výhoda domácího ledu:** pevná konstanta ±3,5 %. Neměří se.
- **Přesilovky a oslabení:** nemodelují se zvlášť. Jsou jen implicitně v `situation = "all"`.

### 3.5 Brankáři – `nhl_data.team_goalies`
- GSAx = `xGoals − goals` brankáře za sezónu, vážené podílem odchytaných zápasů (`games_played / Σ`).
- **Startující brankář není předem znám.** Bez ručního zadání v panelu se použije vážený průměr týmu.
- Ruční zadání (`ledger.goalie_overrides`) platí, dokud se nezruší.

### 3.6 Simulace – `models/nhl.simulate` (Monte Carlo, `mc_iterations = 5000`)
- **Bivariátní Poisson:** `shared ~ Poi(c)`, `h = Poi(λ_h − c) + shared`, `a = Poi(λ_a − c) + shared`, kde `c = min(0,15; 0,5·min(λ))` (`regulation_covariance`, pevná konstanta). Tím vzniká kladná korelace skóre.
- **Prodloužení:** intenzita `(λ_h + λ_a)/60 · 1,9` (`ot_scoring_multiplier`).
  - `P(gól v OT) = 1 − exp(−intenzita · 5)`;
  - domácí vyhraje OT s `0,5 + (λ_h/(λ_h + λ_a) − 0,5)·0,6` (`ot_strength_shrink`);
  - nájezdy 50/50.
- **Trhy:** `MONEYLINE`, `PUCKLINE_1.5(R)`, `OU_3.5…8.5` (včetně OT a nájezdů), `OU60_*` (60 min), `TTH/TTA` (týmové), `REG:1/X/2`, `OT`.
- **Seed:** `crc32(match_id)`, tedy deterministický pro zápas.

### 3.7 Ukotvení na trh – `models/nhl.market_probabilities` + `pipeline._total_anchor`
- `_total_anchor` vybere z půlbodových linií `OU_4.5–7.5` tu, jejíž férová pravděpodobnost overu (z **ostrých** kanceláří) je nejblíž 50 %.
- `market_probabilities` pak bisekcí (14 kroků, `k ∈ [0,6; 1,6]`) vynásobí `λ_h` i `λ_a` stejným `k` tak, aby model na této lince **přesně trefil tržní pravděpodobnost**.
- **Důsledek:** celková úroveň gólů je převzatá z trhu. Model určuje jen poměr sil (kdo vyhraje) a tvar rozdělení.
  - Pro OU na kotevní lince je `p_model ≈ p_market` z konstrukce;
  - na ostatních linkách se liší jen tvarem Poissona.
- Bez ostré kanceláře ukotvení chybí (`anchored = 0`) a totaly jsou čistě modelové. Kód je sám označuje jako „nejsou spolehlivé“ (`models/nhl.py`, komentář u `anchored`).
- Příznak `anchored` se do historie **neukládá**.

### 3.8 Směs model + trh a rozhodnutí
- **Hlavní bot** (`devig.blend_probability`, logit prostor):
  - `logit(p) = 0,15·logit(p_model) + 0,85·logit(p_market)`;
  - váha z `ValueConfig.league_model_weight["NHL"] = 0,15` (`config.py:121`).
- **Zdůvodnění váhy** (komentář `config.py:118–119`, commit `6d5db4d` z 16. 11. 2026):
  - 10 664 zápasů 2012–2021;
  - log loss modelu bez brankářů 0,6776, otevírací linie 0,6701, zavírací 0,6677;
  - nejlepší směs s otevírací linií 0,1–0,2, se zavírací do 0,1.
- **Filtry hlavního bota** (`devig.assess_value`):
  - EV ≥ 2,5 % (MAJOR), časný tip 5 %;
  - kurz 1,15–4,20;
  - `p ≥ 0,45`;
  - `|p_model − p_market| ≤ 0,08`;
  - **EV musí být kladné i vůči samotnému trhu.**
- **Profil 4** (`profil4/nhl.candidates`):
  - A = model;
  - B = trh (Pinnacle bez marže, jinak medián ≥ 2 kanceláří);
  - C = `0,15·A + 0,85·B` – **lineárně**, ne v logitu;
  - D = `min(B, C)`.
  - Rozhodnutí `decision.decide`: EV(C) ≥ 3 %, EV(D) ≥ 0, `|A − B| ≤ 0,10`, robustnost ≥ 60, kvalita dat ≥ 0,6, integrita kurzu.

### 3.9 Korelace mezi trhy
- V rámci jedné simulace jsou trhy konzistentní (stejné simulované skóre).
- Mezi zápasy se korelace neřeší. Nohy kombinací berou zvlášť `profil4/correlation.py` a `kombi.py`, ty nejsou předmětem auditu.

---

## 4. Datové zdroje

| Zdroj | Původ | Poslední aktualizace (ověřeno) | Obnova | Chybějící hodnoty | Historie | Jen data před zápasem? |
|---|---|---|---|---|---|---|
| Týmové zápasy | MoneyPuck `all_teams.csv` (`nhl_data.download_nhl`) | soubor 26. 11. 07:01, poslední zápas **24. 11.** | denně s ranní analýzou (`pipeline` „Stáhne aktuální data“, `pipeline.py:648–655`) | tým bez dat → `context` vrátí `None` → bez modelu | soubor obsahuje i starší sezóny (ověřeno 2022–2026), model bere `min_season = rok − 2` | ano: `date < as_of`. **Zpoždění 1–2 dny**: zápasy z 25. 11. v souboru chybí |
| Brankáři | MoneyPuck `goalies.csv` aktuální sezóny | 26. 11. 07:01 | denně | tým bez brankáře → `Goalie()` (GSAx 0) | jen aktuální souhrn, **ne po dnech** | pro živé predikce ano. Pro zpětné přehrání by šlo o únik (souhrn zahrnuje budoucí zápasy). `backtest.nhl_walk_forward` proto brankáře nepoužívá |
| Ruční brankáři | panel, `state.nhl_goalie_overrides` | — | ručně | — | ne | ano |
| Kurzy | The Odds API, region `eu`, `h2h` + `totals` (spreads z `parse_events`) | cache 26. 11. 06:00:37 | průchod automatu ~6 h, `cache_seconds = 600` | bez Pinnacle/Betfair/Sbobet není férová linie (hlavní bot) | jen poslední cache (`state.odds_cache:NHL`) + kurzy Profilu 4 (`odds_quotes`, od 21. 11.) | ano (stav v okamžiku stažení) |
| Výsledky | NHL API (`nhl_settle`, `api-web.nhle.com`) | průběžně (vyhodnocení po 15 min) | — | nevyhodnocené zůstávají `NULL` | ano | n/a (jen vyhodnocení) |
| Rozpis zápasů a id | NHL API `schedule` (`profil4/nhl.fetch_schedule`) | při skenu Profilu 4 | — | zápas mimo rozpis se přeskočí | — | ano |
| Zranění | ESPN (`context.fetch_nhl_injuries`) | při skenu | — | — | ne | jen do textového rozboru a pokrytí dat, **ne do pravděpodobností** |
| Rozbor týmů | oficiální NHL API (`nhl_api`) | při skenu | — | — | ne | jen textový rozbor |

**Odvozené hodnoty** (nejsou to zdroje): `xgf60…`, `league_xg60`, `gsax60`, `λ`, férová linie, ukotvení, směs C/D.

---

## 5. Predikční pipeline v provozu

1. Automat v `run_once` → `scan_league("NHL")`:
   - kurzy bere z cache, dokud je platná (`pipeline.py:300–312`);
   - jinak je stáhne (2 kredity).
2. `ModelProvider` načte MoneyPuck a brankáře (lokální soubory) a pro každý zápas spočítá `probabilities`.
3. `build_candidates`: jen trhy s férovou linií z ostrých kanceláří. Bez ní zápas odpadne (aktuální stav).
4. `modelcheck.log_predictions` uloží MONEYLINE model + trh (`state.model_check:NHL`), ale **jen pro zápasy s modelem a férovou linií**. U zápasu se přepisuje poslední sken před začátkem.
5. Hlavní bot `evaluate`: logit směs, filtry, tip.
6. Sledování `paper.track`:
   - profil Základ bere u NHL **čistý model** (`p = model[...]`, bez trhu) proti kurzům (`paper._best`, `paper.py:294`);
   - EV ≥ 5 %.
7. Profil 4 `profil4/nhl.run`:
   - z téže cache;
   - modely A–D;
   - zápis všech kandidátů do `predictions` (všechny trhy, ne jen schválené) a vybraných do `tips`.
8. Profil 5: jen tržní konsenzus, bez modelu týmů.

---

## 6. Historické výsledky a jejich omezení

**Obecná omezení:**
- období je jen **21.–26. 11. 2026**, hlavní bot NHL tipy 11.–12. 11.;
- vzorky jsou malé;
- všechno je pre-match: predikce před začátkem ověřena, kurz nikdy novější než predikce;
- zavírací kurzy se jako vstup predikce nepoužívají. Pracuje se jen s nimi pro CLV.

### 6.1 Hlavní bot – `state.model_check:NHL` (MONEYLINE domácí, poslední sken před začátkem)

| Metrika | Hodnota |
|---|---|
| Záznamů / vyhodnocených | 40 / 37 zápasů (21.–26. 11.) |
| Záznamy s `p_model == p_market` (skrytý fallback) | 0 |
| Predikce po začátku | 0 |
| \|model − trh\| | průměr 0,043, medián 0,036, max 0,122 |

| Pravděpodobnost | n | Log loss | Brier |
|---|---|---|---|
| Model | 37 | 0,6668 | 0,2360 |
| Trh (férová linie Pinnacle) | 37 | 0,6775 | 0,2420 |
| Směs 0,15 (produkce) | 37 | 0,6752 | 0,2407 |
| Směs 0,50 | 37 | 0,6707 | 0,2383 |

- Rozdíl log loss model − trh = **−0,0107**, 95% bootstrap interval po zápasech **(−0,043; +0,024)**, t ≈ −0,59. **Neprůkazné.**
- Směs 0,15 − trh = −0,0023 (−0,007; +0,003). Neprůkazné.
- Základní míra: domácí vyhráli 20 z 37.
- **Kalibrace** (po 0,2): modelový koš 0,4–0,6 má 22 zápasů, předpověď 0,534 a skutečnost 0,50. Ostatní koše mají 1–10 zápasů, takže se kalibrace hodnotit nedá.
- **Potřebná velikost vzorku** (směrodatná odchylka rozdílu na zápas 0,110): rozdíl 0,01 odhalí ~470 zápasů, 0,005 ~1 900 zápasů. To je zhruba **0,4, resp. 1,5 sezóny NHL**.

### 6.2 Profil 4 – tabulka `predictions` (všechny analyzované zápasy a trhy, ne jen schválené)
- 1 590 řádků, 56 zápasů, vyhodnoceno 41.
- Na zápas a linii se bere jedna strana (domácí / over), aby se páry nepočítaly dvakrát.

| Trh | Párů | A (model) LL / Brier | B (trh) LL / Brier | C (směs) | D | LL A − B (95% CI po zápasech) |
|---|---|---|---|---|---|---|
| MONEYLINE | 41 | 0,6725 / 0,2390 | 0,6790 / 0,2428 | 0,6773 | 0,6799 | −0,007 (−0,040; +0,030) |
| PUCKLINE | 45 | 0,6072 / 0,2091 | 0,6271 / 0,2178 | 0,6234 | 0,6258 | −0,017 (−0,047; +0,016) |
| OU | 57 | 0,6890 / 0,2480 | 0,6799 / 0,2435 | 0,6810 | 0,6822 | **+0,007** (−0,011; +0,027) |
| Vše | 143 | 0,6585 / 0,2331 | 0,6630 / 0,2352 | 0,6618 | 0,6638 | −0,004 (−0,019; +0,015) |

**Omezení:**
1. **Zastaralý trh B.** Kurzy v okamžiku predikce byly staré medián **826 min** (max 1 208). Všech 1 590 řádků má `odds_status = ZASTARALÝ`, protože Profil 4 bere poslední 6h cache hlavního bota. Trh B je proto slabší měřítko než skutečný trh před začátkem a **srovnání A − B je zkreslené ve prospěch modelu**.
2. U OU je A z velké části kopie trhu: 41 % řádků OU má `|A − B| < 0,01` (u MONEYLINE 11 %).
3. 54 řádků nemá trh B (`market_covered = 0`).
4. Ukládá se jen poslední predikce před začátkem (`history.record_predictions`, `ON CONFLICT … DO UPDATE`). Doba před začátkem: medián 244 min, rozsah 1–2 196 min.

### 6.3 Profil 4 – NHL tipy (`tips`, 102 záznamů, 21.–28. 11.)

| Rozhodnutí | Vyhodnoceno | Výhry | Win rate (Wilson 95%) | Prům. p(C) | Zisk (j.) | ROI/j. (bootstrap 95%) | CLV (n, průměr) |
|---|---|---|---|---|---|---|---|
| APPROVED BET | 23 (z 30) | 11 | 0,478 (0,29–0,67) | 0,493 | +1,64 | +0,071 (−0,33; +0,54) | 14, +7,2 % |
| BEST AVAILABLE (WATCH) | 35 (z 46) | 21 | 0,600 (0,44–0,74) | 0,511 | +9,47 | +0,271 (−0,08; +0,61) | 16, +8,2 % |
| BEST AVAILABLE (NO BET) | 18 (z 22) | 13 | 0,722 | 0,562 | +5,53 | +0,307 | 0 |
| NO BET | 4 | 4 | — | 0,597 | +2,81 | — | 0 |

**Definice:**
- **zisk** = `mathcore.unit_profit`: 1 jednotka; výhra = kurz − 1, prohra = −1, vrácení 0;
- **ROI (yield)** = zisk / počet vyhodnocených sázek po 1 j.;
- **CLV** = `kurz / férový zavírací kurz − 1`. Férový zavírací kurz je kurz stejné kanceláře bez marže, nebo kurz zahraniční kanceláře (`history._clv`, `_closing`). Počítá se jen proti skutečně zavíracímu kurzu.

**Struktura:**
- schválené tipy jsou z 29 z 30 **OU** (zbytek 1 MONEYLINE);
- kanceláře onexbet 11, unibet_nl 9, sport888 6, marathonbet 3;
- `|A − B|` u schválených: medián 0,004, v 67 % pod 0,01. **O schválení rozhodl trh a cena kanceláře, ne model.**

**Kvalita kurzů:**
- kurzy schválených tipů byly čerstvé (medián stáří 1 min, max 4);
- u WATCH medián 372 min.

**Závěr:** ROI i CLV mají intervaly přes nulu a vzorek je 23 sázek. Nejde z nich nic vyvodit a k modelu se navíc téměř nevztahují.

### 6.4 Hlavní bot – NHL tipy (`tips`)
- 8 tipů z 11.–12. 11., všechny `OU_6.5 over`.
- Všechny `cancelled`: 4× „nevsazeno“, 4× „nabídka zmizela“. **Žádný vyhodnocený.**
- `p_model` a `p_market` se liší o 0,000–0,042, u 5 z 8 o méně než 0,01. Ukotvení se tu projevilo stejně: EV 6,7–14,3 % plyne z kurzu české kanceláře proti férové linii.

### 6.5 Sledované výběry (`state.paper_picks`, NHL)
- **Základ** (čistý model, EV ≥ 5 %):
  - 29 výběrů, 21 vyhodnocených, 14 výher, +3,56 j., ROI +17 %;
  - slibované EV průměr +9,3 %, průměrný kurz 1,81;
  - trhy MONEYLINE 12, PUCKLINE 9, OU 8.
  - Výběr podle největšího rozdílu model proti kurzu. Na 21 sázkách **bez průkaznosti**.
- **Tipy:** 5 výběrů (zrcadlo tipů hlavního bota), 2 výhry, −0,61 j.

### 6.6 Profil 5 (bez modelu týmů, jen pro úplnost)
- NHL 41 kandidátů, z toho 36 zamítnutých kontrolou kurzu.
- Vyhodnocení kandidáti: 1 výhra, 2 prohry (−0,96 j.). Zamítnutí: 7 výher, 12 proher (−3,71 j.).
- Model 0 se ho netýká.

### 6.7 Co nelze spočítat a proč
- **Kalibrace po koších:** 37–41 zápasů, koše mají 1–22 bodů.
- **Výsledky podle období:** data jsou jen z 6 dnů.
- **Srovnání se zavírací linií:** zadání ho jako vstup zakazuje. Pro výkonnost by šlo jen u tipů s CLV (14–16).
- **Zpětný test 2012–2021:** zdrojová data s kurzy v repozitáři ani v `data/` nejsou a `backtest.nhl_compare` srovnává jen s četnostmi výsledků, ne s kurzy (`backtest.py:354–355`). Čísla 0,6776 / 0,6701 / 0,6677 proto **nelze ověřit**.
- **Ukotvené vs. neukotvené predikce:** příznak `anchored` se neukládá.

---

## 7. Srovnání s trhem (nezávislost)

| Otázka | Odpověď | Doklad |
|---|---|---|
| Používá model kurzy přímo jako vstup pravděpodobnosti? | **Ano, u úrovně gólů.** Tržní total ukotvuje λ (`k` z bisekce). Síla týmů z trhu není. | `market_probabilities`, `_total_anchor` |
| Přebírá, nebo upravuje? | Vítěz a puckline: vlastní výpočet (poměr λ), celková úroveň gólů z trhu. OU na kotevní lince: v podstatě převzetí. | 6.2 (OU \|A − B\| < 0,01 ve 41 %) |
| Existuje vrstva nezávislá na trhu? | Ano: ratingy, brankáři, únava, simulace. Bez ukotvení je model zcela nezávislý (pak ale totaly nejsou spolehlivé). | `nhl_data`, `models/nhl` |
| Jak se liší od trhu bez marže? | MONEYLINE průměr 0,043 (max 0,12–0,13); OU medián 0,011 | 6.1, 6.2 |
| Měřitelná informační výhoda? | **Neprokázaná.** Bodově model lepší u MONEYLINE/PUCKLINE a horší u OU, všechny intervaly přes nulu. Starý test (neověřitelný) říká, že je horší než trh. | 6.1, 6.2, 3.8 |
| Dopad na tipy | Do tipů jde 15 %. U OU je navíc ukotvený, takže **tipy jsou v praxi tržní** (cena kanceláře proti konsenzu). | 6.3, 6.4 |

Zavírací kurzy se do predikce nikde nepoužívají. Přečtou se až po zápase pro CLV (`history._closing`). Ověřeno: žádný kurz v `predictions` ani v `tips` není novější než predikce.

---

## 8. Rizika a možné zkreslení

| # | Riziko | Zjištění | Doklad |
|---|---|---|---|
| R1 | Únik dat (statistiky po zápase) | Nenalezen pro živé predikce: `date < as_of` a soubor má zpoždění 1–2 dny. Brankáři jsou souhrn sezóny, takže pro **zpětné** přehrání by šlo o únik; walk-forward je proto nepoužívá. | `NHLRatings.__init__`, `backtest.nhl_walk_forward` |
| R2 | Zavírací kurzy v predikci | Ne. | kap. 7 |
| R3 | Chybná časová razítka | Predikce < začátek 1 590/1 590; `odds_time ≤ predicted_at` u všech. **Trh B je ale v době predikce starý ~14 h** (kap. 6.2), takže srovnání s trhem zvýhodňuje model. | `predictions` |
| R4 | Malé vzorky | 37–41 zápasů, 23 schválených sázek. Na závěr potřeba ~470–1 900 zápasů. | 6.1 |
| R5 | Selection bias | `predictions` obsahuje **všechny** analyzované zápasy a trhy, ne jen schválené – to je v pořádku. `model_check` jen zápasy s Pinnacle (vyřazuje zápasy > 36 h dopředu, ty se ale přepíší pozdějším skenem). Profil Základ vybírá největší rozdíl model proti kurzu (adverse selection). | 6.2, 6.5 |
| R6 | Chybějící výsledky | Ve vzorku Profilu 4 vyhodnoceno 41 z 56 zápasů; zbytek zatím nedohrán (do 28. 11.). | `predictions.result IS NULL` |
| R7 | Přiřazení zápasů | `nhl_abbr` a `match_game` (± 6 h). Diagnostika: 0 nespárovaných týmů. Chyby nenalezeny. | `ModelProvider.unmatched` |
| R8 | Nekalibrace | Kalibrace NHL **neexistuje** (fotbal má `calibration`). Ověřit nejde (malý vzorek). | `pipeline.py:178–183` |
| R9 | Fallback vydávaný za model | Hlavní bot: bez modelu je `p_model = p_market` (`build_candidates`). V `model_check` takové záznamy nejsou (0). V Profilu 4 platí bez modelu C = B. Do OU navíc proniká tržní kotva pod jménem „model“. | `pipeline.py:286`, `profil4/nhl.py:224` |
| R10 | Závislost na Pinnacle | Betfair a Sbobet v NHL datech nejsou nikdy (0 z 56 zápasů), takže férová linie hlavního bota = Pinnacle, nebo nic. | `odds_quotes` |
| R11 | Pevné konstanty | Domácí led ±3,5 %, střelba 0,20, kovariance 0,15, OT 1,9 / 0,6, regrese 1 500 střel – původ neznámý, bez fitovacího kódu. | `NHLModelConfig` |

---

## 9. Nálezy a závažnost

**P0 – žádný.** Nenašel jsem únik dat, chybné vyhodnocení ani skryté poškození historie.

**P1**
- **F1 – „Model 0“ není model, ale čítač, a hlavní bot NHL bez Pinnacle mlčí.**
  - `with_model` počítá jen zápasy s férovou linií. Pinnacle je v NHL kurzech jen < 36 h před začátkem.
  - Ranní cache 26. 11. tak dala 0/15, přestože model běží. Hlavní bot pak u NHL nevytvoří ani jednoho kandidáta.
  - Doklad: `pipeline.py:272–275, 349`; tabulka v příloze A.4.
- **F2 – Tipy NHL jsou v praxi tržní, ne modelové.**
  - U OU ukotvení dělá z modelu kopii trhu. Schválené tipy Profilu 4 (29/30 OU) i všechny tipy hlavního bota (8/8 OU) mají `|A − B|` typicky < 0,01.
  - Hodnota vzniká z ceny jedné kanceláře proti konsenzu (line shopping). Označení „model“ to zakrývá.
  - Doklad: `market_probabilities`; kap. 6.3, 6.4.
- **F3 – Vlastní predikční schopnost není doložená.**
  - Živé vzorky jsou malé a neprůkazné.
  - Jediný velký test je jen komentář s neověřitelnými čísly (a i ten říká, že model je horší než trh).
  - Doklad: kap. 6.1, 6.2, 6.7.

**P2**
- **F4 – Srovnání model × trh v Profilu 4 používá zastaralý trh** (~14 h). Výsledky A − B jsou zkreslené ve prospěch modelu (`predictions.odds_time`, `odds_status`).
- **F5 – Nekonzistentní směs a reference trhu:**
  - hlavní bot: logit směs, trh jen z ostrých kanceláří;
  - Profil 4: lineární směs, trh Pinnacle nebo medián ≥ 2 měkkých kanceláří.
  - Pro stejný výběr tak vznikají jiná C i jiné rozhodnutí. Metriky obou proto nejsou přímo srovnatelné (`devig.blend_probability` × `profil4/nhl.candidates`, `market_b`).
- **F6 – NHL nemá kalibraci ani modelování formy** (celá sezóna má stejnou váhu) a startující brankář není znám bez ručního zadání.
- **F7 – Bez Pinnacle jsou totaly neukotvené** (podle kódu nespolehlivé) a příznak `anchored` se neukládá. V historii proto ukotvené a neukotvené predikce nejde rozlišit.
- **F8 – Pevné konstanty bez doloženého původu** (R11) a zpoždění MoneyPuck 1–2 dny.

**P3**
- **F9** – `predictions` drží jen poslední predikci před začátkem. Vývoj pravděpodobnosti se ztrácí (souvisí s Phase 3.4 / A).
- **F10** – Žádné vyhodnocené NHL tipy hlavního bota. Všech 8 bylo zrušeno 11.–12. 11.
- **F11** – Profil Základ používá u NHL čistý model bez trhu, jinak než hlavní bot i Profil 4. Jeho výsledky o modelu něco říkají, ale s výběrovým zkreslením.

---

## 10. Doporučení dalšího postupu (návrh, nic neimplementovat bez schválení)

Pořadí je zvolené tak, aby se nejdřív sbírala data. Žádný krok nevyžaduje nové placené API ani víc kreditů.

1. **Nejdřív měřit, nerozhodovat** – nic se nemění v provozu:
   - nechat běžet sběr `predictions` a `model_check` do zhruba **≥ 470 vyhodnocených zápasů** (NHL hraje průměrně ~7 zápasů denně, tedy cca konec ledna 2027), teprve pak rozhodnout o modelu;
   - průběžné hodnocení jen ze zápasů, kde byl trh B čerstvý.
2. **Opravit měřítko** (návrh změny, ke schválení):
   - k predikci ukládat příznak `anchored` a čas, z jaké doby byl trh B;
   - srovnání A − B počítat proti trhu z téhož okamžiku nebo z bodu 75/30/10 min (už sledované kurzy Profilu 4 – bez dalších kreditů).
3. **Oddělit „model“ od „trhu“ ve výkaznictví:**
   - u OU označovat tip jako tržní (cena proti konsenzu), ne modelový;
   - ve výkazu odlišit MONEYLINE/PUCKLINE (model něco přidává) od OU (model ≈ trh).
4. **Zopakovatelný zpětný test:** doložit zdroj historických kurzů NHL 2012–2021 (soubor, licence), nebo test z konfigurace označit jako neověřený. Bez dat žádná rekalibrace.
5. **Teprve pak volba** (podle bodu 1–4):
   - (a) ponechat model jako kontrolu s malou váhou;
   - (b) kalibrovat (Platt / isotonic na `predictions`);
   - (c) doplnit formu (časové vážení), startující brankáře a přesilovky;
   - (d) navrhnout samostatný NHL model.
   - Kritérium: průkazně nižší log loss proti čerstvému trhu u MONEYLINE/PUCKLINE.
6. **Provozní nález F1** (mimo model): odkdy hlavní bot NHL hodnotí, závisí na tom, kdy Pinnacle vypíše linii. Jestli to řešit (pořadí aktualizace, interpretace čítače v panelu), je samostatné rozhodnutí a souvisí s F6 z Phase 3.4. **Žádné extra stahování kvůli tomu.**

---

## 11. Odpověď

**Je NHL „Model 0“ skutečný samostatný predikční model s doložitelnou vlastní predikční schopností, nebo převážně tržní / fallback mechanismus?**

- **Model 0 jako takový neexistuje.** Je to čítač „s modelem 0“ v logu, který ukazuje, že u zápasů chyběla férová linie Pinnacle (zápasy > 36 h dopředu). Fallback to není.
- **Skutečný NHL model existuje a je samostatný:**
  - xG/Fenwick ratingy z MoneyPuck, GSAx brankářů, únava a Monte Carlo včetně prodloužení;
  - počítá vlastní pravděpodobnosti (26. 11.: 15/15 zápasů).
- **V rozhodování převažuje trh.** Tip tvoří z 85 % trh a u počtu gólů je model ukotvený na tržní total, takže tam trh v podstatě kopíruje. Schválené NHL tipy (téměř všechny OU) jsou fakticky tržní.
- **Doložitelná vlastní predikční schopnost: z dostupných dat ji nelze rozhodnout.**
  - Na 37–41 živých zápasech je model bodově o něco lepší u vítěze a puckline a horší u OU, ale všechny 95% intervaly zahrnují nulu.
  - Srovnání s Profilem 4 je navíc zkreslené zastaralým trhem.
  - Jediný velký historický test je neověřitelný a říká, že samotný model je slabší než trh.

---

## Příloha A – zopakování výpočtů (jen čtení)

**A.1 Hlavní bot:**
```sql
SELECT value FROM state WHERE key = 'model_check:NHL';
```
- JSON `{match_id: {t, ko, p: {home: [p_model, p_market], away: […]}, res}}`;
- log loss a Brier na straně „home“;
- bootstrap po zápasech: 4 000 opakování, seed 7.

**A.2 Profil 4 – predikce:**
```sql
SELECT fixture_id, market, selection, model, p, p_market, market_covered, outcome, predicted_at, kickoff, odds_time
  FROM predictions WHERE league = 'NHL';
```
- jedna strana na trh: `home`, `home-1.5`, `home+1.5`, `over`;
- rozdíl A − B průměrovaný po zápasech, bootstrap jako A.1.

**A.3 Profil 4 – tipy:**
```sql
SELECT decision, result, profit, clv, probs, odds_time, predicted_at, market, bookmaker
  FROM tips WHERE league = 'NHL';
```
- ROI = Σ profit / n;
- Wilson 95% pro win rate;
- bootstrap ROI 4 000 opakování.

**A.4 Pinnacle podle předstihu** (`profil4.db`, NHL id > 2 000 000 000):
```sql
SELECT fixture_id, MIN(fetched_at), MAX(kickoff), MAX(bookmaker = 'pinnacle')
  FROM odds_quotes WHERE fixture_id > 2000000000
 GROUP BY fixture_id, substr(fetched_at, 1, 13);
```

| Předstih | Stažení s Pinnacle |
|---|---|
| < 12 h | 18/24 |
| 12–24 h | 49/50 |
| 24–36 h | 3/6 |
| > 36 h | 0/12 |

Betfair 0/56 zápasů.

**A.5 Aktuální cache** (kontejner `tipsbot`, `mode=ro`, síť v procesu zablokovaná):
- `state.odds_cache:NHL` z 26. 11. 06:00:37, 15 zápasů, 16 kanceláří, žádná ostrá;
- `ModelProvider("NHL")`: 32 ratingů, 32 brankářů, 0 nespárovaných, model pro 15/15 zápasů;
- `build_candidates` → `has_line = False` u všech.

**A.6 Data MoneyPuck:**
- `all_teams.csv` (26. 11. 07:01): sezóna 2026 obsahuje 770 řádků, zápasy 29. 9.–24. 11.;
- `goalies.csv` (26. 11. 07:01).
