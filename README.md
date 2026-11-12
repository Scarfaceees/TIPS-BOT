# Tips Bot

Value-betting poradce pro NHL a evropský fotbal. Počítá férové kurzy, hledá hodnotové
sázky u českých kanceláří, doporučuje výši vkladu a vede evidenci. Sázku nepodává –
tu podáš ručně a tiket zapíšeš do panelu.

## Instalace (Python 3.10+)

    python -m venv .venv
    .venv\Scripts\activate          (Windows)   |   source .venv/bin/activate   (Linux/macOS)
    pip install -e ".[dev]"
    pytest                          (ověření, že vše funguje)

## Spuštění panelu

    tipsbot web

Panel běží na http://127.0.0.1:8000. Při prvním otevření nastav bankroll
(testovací režim = virtuální peníze).

## Backtest a kalibrace fotbalu

    tipsbot fetch-football --league ENG1
    tipsbot backtest --league ENG1
    tipsbot backtest --league ENG1 --xi 0.002 0.0038 0.006     (ladění časového útlumu)
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
