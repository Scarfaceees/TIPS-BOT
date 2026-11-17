# Tips Bot

Value-betting poradce pro NHL a evropský fotbal. Počítá férové kurzy, hledá hodnotové
sázky u českých kanceláří, doporučuje výši vkladu a vede evidenci. Sázku nepodává –
tu podáš ručně a tiket zapíšeš do panelu.

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
