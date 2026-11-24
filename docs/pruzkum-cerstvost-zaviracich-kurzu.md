# Průzkum: čerstvější závěrečné kurzy bez dalších API dotazů

Technický průzkum a návrh, **bez změny kódu**. Body 75/30/10, max. 3 dotazy na zápas, pravidla skutečných
závěrečných kurzů (fáze 2.1) i oddělení zastaralých kurzů od CLV zůstávají beze změny.

**Data:** kopie `profil4.db` (jen čtení). Období 20. 11. – 24. 11. 2026, tedy 4 dny.
- 81 zápasů s pozorováním závěrečných kurzů: 55 fotbal a 26 NHL.
- 243 pozorování.
- 121 646 uložených kurzů, z toho 75 fotbalových zápasů z API-Football.

Vzorek je malý. UCL má 18 zápasů, ostatní ligy po 8–10.

## 1. Aktualizuje poskytovatel už stažené kurzy (cache)?

- **Naše cache se v bodech před výkopem nepoužívá.** Všech 315 dotazů `odds?fixture=` v `api_log` bylo
  `cached=0`, protože `force=True` obchází cache.
- **Pole `update` jednoznačně určuje verzi dat.**
  - Při stejném `update` se kurz nezměnil v žádném z 117 693 výběrů.
  - Když se `update` změní, změní se kurz u 71 % výběrů: 24 703 z 34 781.
- **Opakované stažení mezi body vrátilo stejná data v 93 ze 110 případů (85 %).**
  - Z 165 fotbalových dotazů v bodech jen 17 přineslo novou verzi.
  - Ta přišla 13× mezi body 75 a 30 min a 4× mezi body 30 a 10 min.
- **Žádná aktualizace u poskytovatele nepřišla méně než 45 min před výkopem.**
  - Nejmenší hodnoty: 45, 45, 45, 45, 47 a 59 min.
  - Méně než 180 min před výkopem bylo jen 27 ze 406 verzí.

**Závěr:** API-Football obnovuje předzápasové kurzy po dávkách. Těsně před výkopem je prakticky neobnovuje. Další
stažení ani posun bodu na 5 min tedy nemůže přinést kurz, který poskytovatel ještě nevydal.

## 2. Dá se lépe využít časování a pořadí tří dotazů?

- **Aktualizace chodí v dávkách**, tedy u mnoha zápasů najednou (až 18 zápasů v 10 minutách).
  - Hodiny UTC s nejvíce aktualizacemi: 3–5, 8–9, 12–13, 16–17 a 20–21. Cyklus je zhruba 4 h.
  - Medián intervalu mezi dvěma verzemi téhož zápasu je 395 min (p10 141, p90 797).
  - Kód uvádí podle dokumentace poskytovatele „zhruba po 3 h“. Naměřeno je spíš 4–8 h.
- **Výkopy:** u večerních zápasů (19–20 h UTC) je poslední dávka před výkopem typicky ze 16–17 h UTC.
  Všechny tři body (75/30/10) pak vrátí stejnou verzi.
- **Stáří verze v okamžiku pozorování (fotbal, medián):** bod 75 → 194 min, bod 30 → 178 min, bod 10 → 184 min.
  Do 60 min to bylo 5, 7 a 5 případů z 54. Do 20 min 0, 1 a 0.
- **Pořadí dotazů na čerstvost nemá vliv:** jedna verze platí pro celý zápas a dávka se nedá vyvolat dřív.

**Závěr:** žádné přeskupení ani přečasování tří dotazů v okně posledních 75 min nedá u API-Football fotbalu
skutečný závěrečný kurz (≤ 20 min). Dosažitelné maximum je „poslední dávka před výkopem“ a tu už dnešní body
zachytí.

Jediný měřitelný přínos jiného časování by byla **úspora** dotazů, ne čerstvost: 85 % opakovaných stažení
vrací stejná data.

## 3. Rozdíly podle soutěží, kanceláří a sportů

> **Oprava (24. 11. 2026):** první verze této tabulky zahrnovala i zápasy, které ještě nezačaly, a zápasy bez
> stažení v bodech před výkopem (Liga mistrů hraje 24.–25. 11.). Hodnota „Liga mistrů 1 119 min“ proto byla
> **chybná** a čísla ostatních lig nepřesná. Níže jsou opravená čísla z diagnostiky Odds Freshness: jen odehrané
> zápasy sledované v bodech 75/30/10, stáří poslední aktualizace u poskytovatele v okamžiku výkopu.

| Soutěž (API-Football) | Medián (min) | Průměr | Min | Max | Zápasů |
|---|---:|---:|---:|---:|---:|
| Serie A | 146 | 157 | 83 | 267 | 10 |
| La Liga | 174 | 187 | 59 | 414 | 10 |
| Ligue 1 | 178 | 194 | 105 | 316 | 8 |
| Bundesliga | 193 | 169 | 45 | 283 | 9 |
| Czech Liga | 236 | 203 | 47 | 287 | 8 |
| Premier League | 313 | 273 | 119 | 358 | 10 |

Liga mistrů zatím nemá žádný odehraný zápas sledovaný v bodech před výkopem; ukáže ji až diagnostika.

- **Kanceláře:** API-Football má jen jedno `update` pro celý zápas (v 508 z 508 stažení byl jeden čas).
  Stáří jednotlivých kanceláří tedy v API-Football **rozlišit nelze**. Všech 8 hlavních kanceláří (Pinnacle,
  bet365, Betfair, Marathonbet, William Hill, SBO, Betano a 1xBet) je ve všech 75 zápasech, ale se stejným časem.
- **NHL** bere kurzy z kurzového API (The Odds API), které má `last_update` u každé kanceláře zvlášť.
  - V bodu 10 min je medián stáří 0 min a do 60 min bylo 14 z 26 pozorování.
  - Body 75 a 30 u NHL čtou uloženou cache hlavního bota (bez požadavku). Stahuje se jen v bodu 10 u schváleného
    tipu, ostatní zápasy mají kurz z posledního stažení hlavního bota.
- **Ověření fotbalu zahraniční kanceláří** běží přes stejné kurzové API, jen u schválených tipů a jen v bodu 10 min.
  - Medián stáří poslední aktualizace je **10 min** před výkopem; do 20 min bylo 308 z 308 kurzů (kancelář × zápas).
  - Je to jediný dnešní zdroj skutečných závěrečných kurzů fotbalu, ale stojí kredity kurzového API.

## 4. Lze rozlišit čas stažení od skutečného času aktualizace?

**Ano, už se to dělá.**
- `odds_quotes.fetched_at` je čas stažení a `odds_quotes.source_time` je `update` poskytovatele.
- `odds_observations.observed_at` a `latest_update` platí pro každý bod.
- `history._closing` se řídí časem poskytovatele, ne časem stažení. Zastaralý kurz zůstává bez CLV.

Omezení: u API-Football je čas jen za celý zápas, ne za jednotlivou kancelář. U kurzového API je za kancelář.

## Doporučení (nic z toho zatím není nasazeno)

1. **Body 75/30/10 nechat beze změny.** Data ukazují, že posun na 5 min by nepřinesl nic: poslední aktualizace
   nikdy nepřišla dřív než 45 min před výkopem.
2. **Měřit dál a zobrazovat** (jen uložená data, 0 dotazů):
   - podíl bodů, které přinesly novou verzi;
   - stáří poslední verze podle soutěže;
   - časy dávek poskytovatele.

   Jako diagnostická karta v panelu nebo list v Excelu. Vyhodnotit po alespoň 4 týdnech nebo 200 zápasech.
3. **U API-Football nehledat čerstvost v pořadí dotazů.** Případnou změnu časování zvažovat jen jako úsporu dotazů.
   Bod 30 min přinesl novou verzi jen ve 4 z 55 zápasů.
   - Bez souhlasu by se ale nic nemělo vynechávat. Méně pozorování by snížilo kvalitu auditu.
4. **Skutečné závěrečné kurzy fotbalu jdou dnes jen z kurzového API** (ověření zahraniční kanceláří).
   - Rozšíření na další zápasy by **nezvýšilo počet dotazů na API-Football**, ale spotřebovalo by kredity
     kurzového API: (počet trhů tipu) × 1 kredit za zápas.
   - Kurzové API je už zapojené (NHL, Profil 5), takže to není nová placená služba. Je to ale vyšší spotřeba
     kreditů a vyžaduje výslovný souhlas.
5. **Liga mistrů:** opraveno – původní tvrzení o 18 h starých kurzech vycházelo z nezačatých zápasů a neplatí.
6. Případný testovací návrh 30/10/5 má smysl jen tehdy, pokud měření z bodu 2 ukáže aktualizace v posledních
   45 min. Zatím žádná nebyla.

## Co se ověřit nepodařilo

- Vzorek pokrývá 4 dny a 55 fotbalových zápasů. Dávkování a časy se mohou lišit o víkendu nebo mimo evropské
  poháry.
- Dokumentaci poskytovatele k četnosti aktualizací jsem znovu neověřoval. Údaj „zhruba 3 h“ je převzatý z
  komentáře v kódu.
- Nevím, zda poskytovatel kurzy těsně před výkopem obnovuje u jiných soutěží nebo v jiných tarifech.
