# Tips Bot

Value-betting poradce pro NHL a evropský fotbal. Počítá férové kurzy, hledá hodnotové
sázky u českých kanceláří, doporučuje výši vkladu a vede evidenci. Sázku nepodává –
tu podáš ručně a tiket zapíšeš do panelu.


## Hybrid Premium redesign

Projekt obsahuje redesign webového panelu ve stylu Hybrid Premium. Přehled změn je v `REDESIGN-HYBRID-PREMIUM.md` a navazující kompletní zadání pro Claude Code v `CLAUDE_CODE_REDESIGN_HANDOFF.md`.

## Instalace (Python 3.10+)

    python -m venv .venv
    .venv\Scripts\activate          (Windows)   |   source .venv/bin/activate   (Linux/macOS)
    pip install -e ".[dev]"
    pip install -e ".[second]"     (volitelne: druhy model penaltyblog jako kontrola fotbalu)
    pytest                          (ověření, že vše funguje)

## Spuštění panelu

    tipsbot web

Panel běží na http://127.0.0.1:8000. Při prvním otevření nastav bankroll
(testovací režim = virtuální peníze).

## Backtest a kalibrace fotbalu

    tipsbot fetch-football --league ENG1
    tipsbot backtest --league ENG1
    tipsbot backtest --league ENG1 --xi 0.002 0.0038 0.006     (ladění časového útlumu)
    tipsbot verify                                              (zpetny test vsech modelu, vysledek je v panelu v zalozce Overeni modelu)
    tipsbot calibrate --league all                              (uloží korekci, jen když zlepší nevídaná data)

Ligy: ENG1, ESP1, GER1, ITA1, FRA1, POR1. Backtest porovnává model s uzavírací linií Pinnacle
(po devigu) a se směsí modelu a trhu. Kritérium: Brier pod 0,20, nebo směs není horší než trh;
dále lepší než naivní model a Sharpe nad 1,2.

## Další příkazy

    tipsbot status                   přehled bankrollu a výsledků
    tipsbot tips                     otevřené tipy
    tipsbot ticket-add --book tipsport --odds 2.10 --stake 100 --match-id x --market 1X2 --selection 1 --day 2026-11-14
    tipsbot ticket-settle 1 won --closing 2.02
    tipsbot resume                   obnoví tipy po stop-lossu

Data a databáze se ukládají do složky `data/`.

## Živé kurzy a tipy

1. Zaregistruj se na the-odds-api.com (bezplatný tarif, 500 kreditů měsíčně) a zkopíruj API klíč.
2. Ulož ho v panelu v záložce Nastavení, nebo příkazem:

       tipsbot set-key tvuj_klic

3. Stáhni data pro modely (jednou, pak občas obnovit):

       tipsbot fetch-football --league ENG1      (a další ligy)
       tipsbot fetch-nhl

4. Načti kurzy a tipy: tlačítko „Načíst kurzy a tipy" v panelu, nebo

       tipsbot scan --league ENG1
       tipsbot scan                               (všechny soutěže)

Sken jedné soutěže stojí 2 kredity (u NHL 3 kvůli puckline). Kurzy se 10 minut berou z paměti
(kredity se nespotřebují); zaškrtnutím „nová data" v panelu se stáhnou znovu.
Soutěž bez zápasu v příštích 24 hodinách se nestahuje.

## Profil 2 – rozbor zápasu z vlastních kurzů

Záložka **Profil 2** v panelu (nebo příkaz `tipsbot profil2`) rozebere jeden zápas z kurzů, které opíšeš
z Tipsportu, Fortuny a dalších. Postup vychází z rozboru Wales – Portugalsko (Liga národů, 11/2026):

1. **Tři scénáře gólů:** A = model (tvůj odhad λ, model bota pro zvolenou soutěž, nebo průměr obou;
   volitelně násobky z kontextu – absence, motivace), B = kompromis 50 % model / 50 % trh,
   C = trh (λ dopočtené z kurzů po odstranění marže, jen pro kontrolu).
2. **Nejistota odhadu:** λ se násobí náhodným faktorem (gama rozdělení, ±25 %), takže vysoké výsledky
   nejsou podceněné jako u čistého Poissona.
3. **Přesný výpočet každé sázky** na matici výsledků: výhra / vrácení / prohra a EV ve všech scénářích.
   Celé a čtvrtinové linie (Under 4,0, Over 2,25, handicap −1,0) se počítají s vrácením vkladu.
4. **Doporučení** jen ze sázek s kladným EV v A i B: *nejrozumnější sázka* (prohra max. 25 %, i nízký kurz),
   *kurz 1,5+ s vysokou šancí* (výhra aspoň 60 %), *nejvyšší hodnota*; k tomu *vyhnout se* a minimální kurz.
5. **Korelace:** když doporučené sázky stojí na stejném průběhu zápasu, bot upozorní, ať je nedáváš na AKO.
6. **Sure bety** mezi kancelářemi a hranice kurzu, od které by sure bet vznikl.
7. **Simulace 1000 zápasů:** nejčastější výsledky, rozložení gólů a výsledek každé sázky po 100 Kč.

Proti férové linii týchž kurzů (scénář C) vychází EV vždy zhruba minus marže – kladná hodnota vzniká
jen tam, kde se model liší od trhu, nebo u lepšího kurzu jiné kanceláře. **Profil 2 není ověřený
zpětným testem**: tlačítkem „Sledovat doporučené sázky“ je pošli do Sledování (zdroj `profil2`)
a po stovce vyhodnocených sázek uvidíš, jestli skutečně vydělávají.

Zápis kurzů (řádek `kancelář | trh | kurzy`, kancelář lze vynechat):

    OU_3.5 | 2.34 1.62            celkem gólů: víc, méně (OU_4.0 = celá linie s vrácením)
    1X2 | 6.90 4.80 1.45          DC (1X 12 X2), DNB (domácí hosté), BTTS (ano ne)
    TTA_1.5 | 1.38 2.78           góly hostů (TTH_ = domácích): víc, méně
    AH_-1.0 | 2.10 1.70           handicap z pohledu domácích: domácí, hosté
    U4.5 | 1.25                   jedna strana linie (O2.5, H_U1.5, A_O0.5) nebo TRH:výběr (AH_1.0:away)
    2&U3.5 | 2.41                 kombinace s jedním kurzem (X2&U3.5, 2&BTTS, 2&H_U0.5, G1-3, G3, S0:1)
    fortuna | OU_3.5 | 2.30 1.66  jiná kancelář (pro sure bety a nejlepší kurz)

Příkazová řádka:

    tipsbot profil2 --match "Wales - Portugalsko" --odds kurzy.txt --lam 0.83 1.95 --day 2026-11-17 --save --track
    tipsbot profil2 --match "Wales - Portugalsko" --odds kurzy.txt --league UNL      (λ z modelu bota)

Ukázkové kurzy z rozboru jsou v `tests/fixtures/profil2_wales_portugal.txt`. Nastavení (nejistota, prahy,
váha kompromisu) je v `profile2.Profile2Config`.

## Co bot ukáže u každého zápasu

- Tabulka 10 a více trhů: pravděpodobnost modelu, férová pravděpodobnost trhu, férový kurz, kurz a EV.
- Doporučení: **TIP** = ověřená hodnota; **CÍL** = doporučený tip s kurzem, od kterého se vyplatí sázet
  (kurz si ověříš u kanceláře); **BEZ VALUE** = nic rozumného.
- Podrobná analýza: síla týmů, forma, vzájemné zápasy, únava, brankáři (NHL), absence, počasí, novinky.
- Tikety: kombinace max. 3 příležitostí (standardně 2) z různých zápasů, řazené podle růstu bankrollu.

## Doplňkové zdroje (zdarma)

- Klíč z api-football.com (100 požadavků denně) v Nastavení: zranění a absence fotbalistů, město pro počasí.
- NHL zranění (ESPN), počasí (Open-Meteo) a titulky zpráv (Google News) klíč nepotřebují.
- `tipsbot fetch-nhl` stáhne i brankáře (GSAx z MoneyPuck).

Zranění, počasí a zprávy se zatím jen zobrazují, do pravděpodobností nezasahují.

Dokud nejsou automaticky dostupné kurzy českých kanceláří, berou se nabídky od
ostatních evropských kanceláří. Tip pak čti jako cíl: u české kanceláře vsaď jen
tehdy, když nabízí aspoň „minimální kurz" uvedený u tipu.

Bez stažených dat pro model bot vyhodnocuje sázky jen proti férové linii trhu.

## Profil 4 – Hybridní expert

Samostatný analytik fotbalu nad **API-Football** (jediný placený sportovní zdroj). Sám projde nadcházející zápasy,
levně vyřadí nezajímavé, vybrané rozebere do hloubky a řekne, jestli sázka dává smysl – a proč. Ostatní profily
(Základ, Profil 2, Profil 3, Rohy a karty) běží beze změny; Profil 4 má vlastní databázi `data/profil4.db`
a vypíná se proměnnou `TIPSBOT_PROFIL4=0`.

**Filozofie:** matematika rozhoduje o číslech, AI rozumí kontextu, rozhodovací engine určuje, zda sázet.
Kontextový analytik nikdy nemění pravděpodobnosti, EV, férové kurzy ani simulace.

### Nastavení

    set TIPSBOT_API_FOOTBALL_KEY=tvuj_klic        (Windows; Linux: export ...)

Nebo stačí klíč uložený v panelu (Nastavení → API-Football). Klíč se nikdy neukládá do kódu ani do logů.
Kvóta: skutečný denní limit se čte z hlaviček API (`x-ratelimit-requests-*`, minutový `X-RateLimit-*`);
bez nich platí 100/den. Nastavení (prahy, váhy, TTL cache, limity) je v `src/tipsbot/profil4/config.py`.

### Příkazy

    tipsbot profil4                       stav: klíč, kvóta, soutěže, watchlist, historie
    tipsbot profil4 daily                 DAILY SCAN – dnešní zápasy (quick scan → hloubková analýza nejlepších)
    tipsbot profil4 upcoming --hours 48   UPCOMING SCAN
    tipsbot profil4 analyze 1234567       DEEP SCAN jednoho zápasu (id z API-Football, je ve výpisu skenu)
    tipsbot profil4 odds --fixture 1234567 --file tipsport.txt     ruční kurzy (řádky jako v Profilu 2: trh | kurzy)
    tipsbot profil4 odds --csv kurzy.csv   |   --json kurzy.json   import (fixture_id, bookmaker, market, selection, odds[, time])
    tipsbot profil4 watchlist add 1234567 --market 1X2 --selection 1 --min-odds 2.10 --lineups
    tipsbot profil4 watchlist check       upozornění jen se skutečným důvodem (kurz, EV, sestavy, změna rozhodnutí)
    tipsbot profil4 settle                vyhodnocení skončených zápasů (90 min, půlvýhry, vrácení, CLV)
    tipsbot profil4 evaluate              Brier, log loss, ROI, yield, úspěšnost, CLV po modelech/trzích/soutěžích/…
    tipsbot profil4 intel                 Intelligence & Calibration: A–D a trh na společném vzorku, leakage, ranking (bez API)
    tipsbot profil4 backtest --league 39 --season 2025      walk-forward modelu A bez data leakage
    tipsbot profil4 competitions --disable 848 --priority 345 1 --add 207 --name "Super League"
    tipsbot profil4 auto --every-hours 6  plánované skenování (vynechá sken, když kvóta dochází)

V panelu je záložka **Profil 4 – Expert** (sken, analýza podle id, ruční kurzy z Tipsportu, vyhodnocení).

### Architektura (`src/tipsbot/profil4/`)

- **Data:** `apiclient` (centrální klient: cache podle endpointu a parametrů, deduplikace, retry s exponenciálním
  backoffem, timeouty, rozpoznání chyb v těle odpovědi), `budget` (denní a minutová kvóta, priority HIGH/MEDIUM/LOW
  s rezervou 2/15/40 %, strop 12 požadavků na zápas a 60 na sken), `provider` (normalizované zápasy, kurzy, absence,
  sestavy, tabulka, predikce, H2H), `coverage` (Coverage Report a kvalita dat 0–100 %), `odds` (kurzy s časem, zdrojem
  a stavem AKTUÁLNÍ / ZASTARALÝ / HISTORICKÝ / ORIENTAČNÍ).
- **Matematika:** `mathcore` (EV, férový kurz, settlement – sdílený výpočet linií s Profilem 2), `models` (A–D),
  `montecarlo`, `stress` (stress test, robustnost), `correlation`, `surebet`.
- **AI analytik:** `analyst` (fakta se zdrojem, časem, stavem ověření a jistotou; ochrana proti prompt injection).
- **Rozhodování a hledání:** `decision`, `analysis` (hloubková analýza), `hunter` (quick/deep, daily/upcoming),
  `watchlist`, `history` (evidence, vyhodnocení, backtest), `report` (český report).

### Modely

- **A – vlastní:** Dixon-Coles z bota (útok/obrana, domácí výhoda po týmech, exponenciální útlum starších zápasů,
  korekce nízkých skóre, síla soupeřů je součástí odhadu) na výsledcích z API-Football (aktuální + minulá sezóna).
  Používá jen zápasy odehrané před výkopem.
- **B – tržní:** kurzy bez marže (výchozí p_i = (1/kurz_i) / Σ 1/kurz; volitelně Shin, Power), přednost má Pinnacle,
  jinak medián kanceláří. Uchovají se původní kurzy, hrubé i očištěné pravděpodobnosti. Z 1X2 a gólových linií se
  dopočtou očekávané góly.
- **C – kombinovaný:** směs matic w·A + (1−w)·B. Výchozí w = 0,5; když je soutěž ve zpětném testu bota (top ligy),
  použije se váha z něj (u lig 0,10 – zpětný test ukázal, že vyšší váha modelu zhoršuje odhad). Bez jednoho zdroje je
  C označen jako omezený a sázka nemůže být schválená.
- **D – konzervativní:** nejhorší EV z {C s nejistotou parametrů, A s nejistotou, B}. Nejistota očekávaných gólů je
  gama směs (±20 % při dobrých datech, ±33 % při slabých). Odděleně se ukazuje náhodnost výsledku, simulační chyba,
  nejistota parametrů, kvalita dat a rozdíl modelů.
- **Monte Carlo:** 50 000 simulací (nastavitelné), pevný seed, simulace zahrnuje i nejistotu gólů; výstup má
  simulační chybu a 95% interval. Víc simulací nezvyšuje přesnost modelu.

### EV, férový kurz, linie

EV = p × kurz − 1, férový kurz = 1/p. U linií s vrácením (celé linie, DNB, čtvrtinové asijské linie) je výnos
lineární v kurzu (a·kurz + b), proto férový kurz = (1 − b)/a a EV se počítá z celé skórové matice včetně
půlvýher a půlproher – ne binárním vzorcem.

### Robustnost a rozhodnutí

Robustnost 0–100 = shoda modelu a trhu (20) + stabilita EV ve scénářích (25) + kvalita dat (15) + aktuálnost kurzu
(10) + jistota sestav (10) + citlivost na parametry (10) + nejhorší scénář (10). **Není to pravděpodobnost výhry.**

- **APPROVED BET:** EV(C) ≥ 3 %, EV(D) ≥ 0, robustnost ≥ 60, kvalita dat ≥ 60 %, aktuální kurz 1,20–5,00,
  model a trh se liší max. o 10 p.b., EV pod 25 % (vyšší = podezřelé).
- **WATCH:** kladné EV, ale něco chybí (sestavy, starý kurz, slabší data, podezřelé EV…).
- **NO BET:** bez prokázané hodnoty nebo tvrdé porušení (kurz mimo pásmo, výrazně záporné konzervativní EV…).
- **BEST AVAILABLE:** nejlepší nalezená možnost, když nic není schválené – s důvodem, proč není schválená.
- Kategorie **A – nejrozumnější**, **B – value tip** (schválený, kurz ≥ 1,50), **C – maximum value**. Nejsou zárukou výhry.

### Fáze 3 – Intelligence & Calibration

Nadstavba nad modely A–D, která **nemění pravděpodobnosti, EV ani rozhodnutí** – jen měří, kontroluje a vysvětluje
(podrobně `docs/profil4-faze3-intelligence-calibration.md`):

- **Kalibrace A–D na společném vzorku** – všechny modely i trh bez marže se srovnávají na stejných zápasech, trzích
  a výběrech; jen předpovědi před výkopem, poslední před zápasem. Brier, log loss, 95% intervaly z bootstrapu po
  zápasech, kalibrační pásma (malá pásma se slučují), stav průkaznosti až od 100 zápasů. Fotbal a NHL zvlášť.
- **Kvalita dat 0–100**, **kurzová inteligence** (odchylka od mediánu, férový tržní kurz, podezření), **EV po
  modelech** (A, B, C, D, proti trhu, citlivost) a **Opportunity Ranking 0–100** (pořadí zajímavosti, ne šance
  na výhru; váhy experimentální) – u každého tipu v panelu i v databázi (`tips.quality`, `tips.rank`, `tips.intel`).
- **Analýza chyb**, **kontrola úniku informací** a **diagnostický návrh ladění** (chronologický out-of-sample test;
  nic se nenasazuje automaticky).
- Panel: Profil 4 → sekce *Intelligence & Calibration* (karty pro mobil). Excel export má 5 nových listů.

### Známá omezení

- **Tipsport:** API-Football nemusí Tipsport obsahovat; „Tipsport“ z API se nebere jako český tipsport.cz
  (může jít o jinou zemskou verzi). České kurzy se zadávají ručně nebo importem; scraping se nedělá. OCR screenshotů
  zatím není (vstup přes `odds.parse_manual` je připravený).
- Kurzy z API: 1–14 dní před zápasem, obnova zhruba po 3 h, historie jen 7 dní → historický ROI z API spočítat nejde;
  ROI a CLV se měří dopředu na uložených tipech. Zavírací kurz = poslední uložený kurz před výkopem.
- Sestavy obvykle 20–40 min před výkopem; stahují se až 75 min před výkopem. Absence API obnovuje po ~4 h.
- Absence a sestavy nemění pravděpodobnosti (není ověřené pravidlo) – jen se zobrazí a promítnou do rizik a robustnosti.
- Forma a odpočinek jen ze zápasů dané soutěže; u pohárů má model A málo dat (často nedostupný).
- Jazykový model (LLM) pro vyprávění kontextu je připravený (`analyst.LLMBackend`), ale vypnutý – placenou službu
  je potřeba schválit. Výchozí analytik je pravidlový nad daty API a titulky Google News RSS (neověřené).
- Surebet se označí jako ověřený jen se známými limity kanceláří; jinak „POTENCIÁLNÍ – NEOVĚŘENO“.
- Krátké období výsledků není důkazem dlouhodobé profitability (pod 100 tipů je výsledek označen jako neprůkazný).
