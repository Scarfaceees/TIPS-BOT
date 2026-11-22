# Profil 4 – Fáze 3: Intelligence & Calibration

Stav k 22. 11. 2026. Výchozí commit před Fází 3: `3819ba6` (větev `dev`).

## 1. Audit původního stavu

| Oblast | Co už existovalo (soubor) | Co chybělo |
|---|---|---|
| Matematický engine | modely A–D, Poisson/Dixon-Coles, Monte Carlo, EV, férové kurzy, asijské a čtvrtinové linie (`models.py`, `mathcore.py`, `montecarlo.py`) | – (beze změny) |
| Rozhodování | APPROVED / WATCH / NO BET, BEST AVAILABLE, kategorie A/B/C (`decision.py`) | – (beze změny) |
| Kontrola integrity | nezávislý přepočet EV, spárování protistrany ze stejné aktualizace, cena trhu, potvrzení jinou kanceláří (`integrity.py`) | souhrnný přehled „proč je kurz podezřelý“ (medián, odchylka) |
| Stress test | optimistický (A), základní (C), konzervativní (D), tržní (B), −3 p.b., kurz −5 %, sestava, horší data (`stress.py`) | citlivost EV na posun šance |
| Robustnost | Tip Robustness Score 0–100 (`stress.robustness`) | – (zachováno) |
| Kvalita dat | váhový průměr dostupnosti zdrojů 0–1 (`coverage.py`), používá ho rozhodování | samostatné skóre 0–100 s vysvětlením (kurzy, kanceláře, protistrana, konzistence) |
| Ukládání předpovědí | `predictions` (A–D, trh, kurz, kancelář, čas zdroje, stav, verze, otisk), jen před výkopem, vyhodnocené se nemění (`history.record_predictions`) | – (ověřeno testy) |
| Kalibrace | Brier, log loss, 95% intervaly z bootstrapu po zápasech, rozdíl proti trhu, křivka po decilech (`history.calibration`) | srovnání modelů na **stejném** vzorku (každý model měl vlastní řádky), slučování malých pásem, stav „dobře/špatně kalibrovaný“, vývoj v čase, profily soutěží, oddělení NHL |
| Historie a ROI | deduplikace (`history.unique_bets`), yield, CLV jen z ověřených závěrečných kurzů | vyřazení tipů uložených po výkopu (viz chyba níže) |
| Backtest | walk-forward modelu A (`history.backtest`) | out-of-sample návrh ladění nad uloženými předpověďmi |
| AI analytik | pravidlový analytik + volitelný `LLMBackend` (vypnutý, placený), fakta se zdrojem a stavem (`analyst.py`) | – (beze změny, viz bod 8) |
| Automat | `closing.tick` – vyhodnocení každou hodinu, přepočet úspěšnosti | přepočet Intelligence & Calibration (bez API) |
| Panel / export | záložka Profil 4, Export výsledků (7 listů) | sekce Intelligence & Calibration, 5 nových listů |

### Nalezená chyba (opravena s regresním testem)

Rozbor zápasu spuštěný **po výkopu** (např. tlačítko Detail u odehraného zápasu) ukládal tipy. Předpovědi pro kalibraci
tuto ochranu měly, tipy ne. V živé databázi bylo 6 takových tipů (SV Elversberg – SC Freiburg a Bayer Leverkusen –
SC Paderborn, rozbor 21:36 UTC na zápas ve 14:30 UTC) a započítaly se do ROI.

- `history.record` teď u rozboru po výkopu tipy neukládá.
- `history.unique_bets` vyřadí tipy uložené po výkopu z výsledků (ROI, export, panel). Data se nemažou ani nemění.
- Testy: `test_analysis_after_kickoff_records_no_tips`, `test_tip_saved_after_kickoff_is_excluded_from_results`.

Druhý nález kontroly (5 tipů Sigma Olomouc – Slavia s „kurzem novějším než předpověď“) **není únik informací**: jako
čas předpovědi se ukládá začátek skenu, kurz stažený během dlouhého skenu je pořád před výkopem. Kontrola byla
nahrazena kontrolou „kurz z doby po výkopu“.

## 2. Nové funkce

Vše v `src/tipsbot/profil4/intelligence.py`. Nic z toho nemění pravděpodobnosti, EV, settlement ani rozhodnutí.

### Kalibrace A–D (z uložených předpovědí, bez API)
- `clean_predictions`: jen předpovědi před výkopem, poslední před zápasem pro každý (zápas, trh, výběr, model).
  Opakované skeny nejsou nezávislé vzorky.
- `common_sample`: jen řádky, kde mají předpověď **všechny** modely A–D i trh bez marže.
- `model_table`: Brier, log loss, 95% intervaly ze shlukového bootstrapu po zápasech (seed 7, 2 000 opakování),
  rozdíl log loss proti trhu na stejných řádcích, kalibrační pásma a stav kalibrace.
- Pásma 0–10 %, …, 90–100 %; pásmo s méně než 15 zápasy se sloučí se sousedním. Interval skutečnosti je Wilsonův
  z počtu **zápasů**.
- Stav kalibrace: rozdíl skutečnost − odhad se shlukovým intervalem → *málo dat* / *neprůkazné* / *průkazně dobře
  kalibrovaný* (celý interval v ±3 p.b.) / *průkazně špatně kalibrovaný (přeceňuje / podceňuje)*.
- **Průkaznost až od 100 zápasů** (`IntelligenceConfig.min_matches_verdict`). Na živých datech (31 zápasů,
  3 556 výběrů) by se jinak všechny modely jevily jako „průkazně špatně kalibrované“. Výsledky jednoho zápasu jsou
  silně korelované, takže 31 zápasů neodpovídá 3 556 nezávislým pozorováním.
- Profily soutěží, typů trhu a kurzových pásem: model C a trh na společném vzorku, kalibrace, stabilita (log loss
  1. a 2. poloviny chronologicky), ROI/yield/CLV tipů. Důvěra: ≥ 100 zápasů dostatečný vzorek, 30–99 omezený,
  pod 30 nízká.
- Vývoj v čase (Brier a log loss po měsících pro A–D a trh).
- Fotbal a NHL (`sport_of`) se počítají vždy zvlášť.

### Kvalita dat 0–100 (`quality_of_data`)
Složky (body z 100, experimentální váhy v `IntelligenceConfig`):

| Složka | Body |
|---|---|
| aktuálnost kurzu | 20 |
| zpoždění stažení | 10 |
| protistrana ze stejné aktualizace | 15 |
| počet kanceláří | 15 |
| konzistence kurzů | 10 |
| statistiky | 15 |
| sestavy | 5 |
| absence | 5 |
| úplnost | 5 |

Každá složka má text vysvětlení. Chybějící data se uvádějí jako *nejistota*, ne jako negativní sportovní informace.
Skóre je oddělené od Tip Robustness Score i od šance na výhru. Původní `coverage.quality()`, které používá
rozhodování, zůstává beze změny.

### Kurzová inteligence (`odds_intel`)
Nejlepší cena, medián aktuálních kurzů, férový tržní kurz, odchylka od mediánu, počet kanceláří, stáří a čas stažení.
Kontroluje se:
- extrémní kurz (> 10 % nad mediánem ≥ 3 kanceláří);
- kurz > 10 % nad férovou cenou trhu;
- zastaralý kurz;
- chyba mapování nebo výpočtu (tvrdé chyby integrity);
- neúplný trh nebo protistrana z jiné aktualizace;
- rozdílná pravidla vyhodnocení;
- podezřele vysoké EV;
- nesoulad modelu a trhu;
- kurz nepotvrzený jinou kanceláří.

Že extrémní kurz nedostane APPROVED, zajišťuje **už existující** decision engine (`_integrity`), což ověřují testy.

### EV analýza (`ev_breakdown`)
- EV modelů A, B, C a D při dostupném kurzu (tentýž `mathcore.evaluate`), konzervativní EV, EV proti trhu a minimální
  kurz.
- Rozpad hodnoty:
  - **z ceny** = kurz nad férovou cenou trhu;
  - **z modelu** = co model přidává navíc nad trh.
- Příznak, že cena trhu není podložená aktuálním kurzem. Takový tip APPROVED nedostane už v původním enginu
  (`market_covered`), ověřeno testem.
- Citlivost EV na posun šance o −5 až +5 p.b. (při posunu 0 se rovná EV modelu C, ověřeno testem).
- Scénáře stress testu (optimistický, základní, konzervativní, tržní a další) zůstávají v `stress.py`. Simulační chyba
  Monte Carlo (interval z 50 000 simulací) je oddělená od nejistoty modelu (D, horší data) a od kurzového rizika
  (kurz −5 %).

### Opportunity Ranking 0–100 (`rank`)
| Složka | Body |
|---|---|
| konzervativní EV (−5 % = 0 b., +10 % = plně) | 25 |
| kvalita dat | 20 |
| robustnost | 15 |
| shoda modelu a trhu | 15 |
| aktuálnost kurzu | 10 |
| potvrzení kurzu | 5 |
| rizika | 5 |
| historická kalibrace typu trhu | 5 |

- Váhy jsou **experimentální** a zatím nejsou ověřené na historii. Mění se jen ručně v `IntelligenceConfig`.
- Ranking není pravděpodobnost výhry a nemění rozhodnutí.
- Kategorie A/B/C, BEST AVAILABLE a oddělení od APPROVED zůstávají v `decision.py` beze změny.

### Analýza chyb (`diagnose`)
Vychází jen z uložených dat:
- zastaralý kurz;
- kvalita dat pod 60;
- podezřelý kurz;
- rozchod s trhem;
- záporné CLV;
- neplatná sázka.

U prohry bez doložené příčiny se výslovně uvede, že příčina není v datech doložena.

### Kontrola úniku informací (`leakage_audit`)
Kontroluje:
- předpovědi a tipy uložené po výkopu;
- kurz tipu z doby po výkopu;
- předpovědi změněné po vyhodnocení;
- duplicitní předpovědi stejné verze.

### Návrh ladění (`tuning_report`)
Chronologické rozdělení: prvních 60 % zápasů trénink, zbytek test. Váha `w` ve směsi `w·C + (1−w)·trh` se hledá jen
na tréninku, na testu se porovná s C (shlukový interval).

Výstup obsahuje:
- důvod;
- počty zápasů;
- výsledek před a po změně;
- interval;
- rizika;
- doporučení.

**Nikdy se nenasazuje automaticky.** Pod 60 společnými zápasy návrh nevzniká.

## 3. Změněné soubory

**Nový kód a testy**
- `src/tipsbot/profil4/intelligence.py` – nový modul.
- `tests/test_profil4_phase3.py` – nové testy.

**Napojení a ukládání**
- `src/tipsbot/profil4/config.py`: `IntelligenceConfig`, pole `intel`. Z otisku konfigurace je vyňaté, otisk modelu
  se nemění.
- `src/tipsbot/profil4/decision.py`: pole `Candidate.intel` (jen data).
- `src/tipsbot/profil4/analysis.py` a `nhl.py`: `intelligence.annotate` až po rozhodnutí.
- `src/tipsbot/profil4/history.py`:
  - tipy se neukládají z rozboru po výkopu;
  - `unique_bets` vyřadí tipy uložené po výkopu;
  - ukládají se `quality`, `rank` a `intel`.
- `src/tipsbot/profil4/store.py`: nové sloupce v `LATER_COLUMNS`.
- `src/tipsbot/profil4/closing.py`: automat po vyhodnocení přepočítá `web_p4_intel`.

**Výstupy**
- `src/tipsbot/profil4/view.py`: kvalita dat, kurz, EV a ranking na kartě tipu.
- `src/tipsbot/profil4/cli.py`: `tipsbot profil4 intel`.
- `src/tipsbot/web.py`: sekce Intelligence & Calibration, tlačítko Přepočítat, SVG grafy, mobilní karty, karta tipu.
- `src/tipsbot/export.py`: listy Kalibrace modelů, EV analýza, Kvalita dat, Analýza chyb a Opportunity Ranking.

**Upravené testy**
- `tests/test_export.py`: 12 listů; testovací tip uložený před výkopem.

**Dokumentace**
- `README.md`
- tento dokument

## 4. Databáze a migrace

- `tips` dostává sloupce `quality REAL`, `rank REAL` a `intel TEXT` (JSON s kvalitou dat, kurzem, EV a rankingem).
  Přidávají se kompatibilně přes `ALTER TABLE ADD COLUMN` (`Store._migrate`). Nic se nemaže a starší řádky mají
  hodnotu NULL.
- Nový klíč `kv.web_p4_intel` s posledním přepočtem pro panel. Archivuje se v `archive`.
- Žádná destruktivní migrace. Historická data se nepřepisují.

## 5. Ověření

**Testy:** 23 nových testů v `test_profil4_phase3.py` a upravené testy exportu.
- Ověřují Brier, log loss, Wilsonův interval, slučování pásem, bootstrap po zápasech (seed), společný vzorek,
  leakage, neměnnost vyhodnocených předpovědí a regresi tipů po výkopu.
- Dále kvalitu dat, podezřelý kurz, hodnotu bez ceny trhu, ranking, BEST AVAILABLE, oddělení NHL a návrh ladění.
- A také Excel (s databází Profilu 4 i bez ní) a panel (sekce, tlačítko, mobilní karty).
- Celá sada: viz bod 7.

**Simulace a záznamy:** testy pracují se syntetickými předpověďmi s pevným seedem, se zaznamenanou odpovědí
API-Football (`tests/fixtures/apifootball_odds_20261120.json`) a s mockovaným API.

**Na skutečných datech:** přepočet na kopii živé `profil4.db` z 22. 11. 2026 proběhl.

| Ukazatel | Výsledek |
|---|---|
| předpovědi | 14 280 |
| tipy | 213 |
| společný vzorek – fotbal | 31 zápasů |
| společný vzorek – NHL | 2 zápasy |
| doba výpočtu | ~1 s |
| nalezená chyba | 6 tipů uložených po výkopu |

Export a panel byly ověřeny proti běžícímu Dockeru.

**Neověřeno:**
- Ranking a kvalita dat na nových živých analýzách: ukládají se od nasazení, ověří je první sken automatu.
- Zda experimentální váhy rankingu odpovídají budoucímu výsledku. Chybí na to data.
- Kalibrační závěry: všechny jsou zatím „málo dat“ (pod 100 zápasy).

Nová vrstva nevolá žádné API, kvótu API-Football tedy nezatěžuje.

## 6. Limity

- Průkazné závěry o kalibraci a o srovnání s trhem vyžadují ≥ 100 společných zápasů. Návrh ladění vyžaduje ≥ 60
  zápasů a vždy ruční schválení.
- NHL má zatím 2 společné zápasy. Žádné změny modelu NHL se nedělají.
- Kvalita dat a ranking jsou experimentální váhy, nikoli statisticky odhadnuté.
- Analýza chyb nezná kontext zápasu (absence a podobně) nad rámec uložených dat a nic si nedomýšlí.
- AI analytik (`analyst.LLMBackend`) zůstává vypnutý. Placená služba vyžaduje souhlas uživatele. Pravidlový analytik
  nemění pravděpodobnosti, fakta mají zdroj, čas a stav ověření.

## 7. Výsledek testů a nasazení

- Celá sada: **485 testů prošlo** (z toho 24 nových pro Fázi 3), žádný původní test nebyl odstraněn. Dva upravené
  testy exportu odpovídají novému chování: 12 listů a testovací tip uložený před výkopem.
- Docker: obraz přestavěn. Migrace živé `profil4.db` přidala sloupce `quality`, `rank` a `intel`, všech 213 tipů
  zůstalo. Před nasazením byla vytvořena záloha `backups/*.20261122-0259.bak` (mimo `data`, necommituje se).
- CLI: `docker exec tipsbot tipsbot profil4 intel` vypsal souhrn (fotbal 31 a NHL 2 společné zápasy, kontrola úniku
  informací).
- Panel: sekce Intelligence & Calibration se vykreslí. Na šířce 375 px nevzniká vodorovné posouvání.
- Export: XLSX (12 listů), CSV i PDF stažené z běžícího panelu.
