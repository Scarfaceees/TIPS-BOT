# Fáze 3.2.3 – audit měření: Profil 3x, Profil 5, rozsah CLV, Market Integrity

Stav k 24. 11. 2026. Výchozí stav: větev `dev`, HEAD 550c2db. Data jsou z kopie databáze, která se jen četla.
Produkční výběry, pravděpodobnosti, EV, váhy, prahy, body 75/30/10, pravidla CLV ani historické záznamy se nezměnily.
Žádný API dotaz ani kredit navíc. Profil 5 a Profil 3x zůstávají ve stínovém režimu.

## 1. Profil 3x – proč je EV stejné jako u Profilu 3

**Příčina (ověřeno na všech 17 výběrech):** všechny výběry Profilu 3 jsou celé nebo čtvrtinové linie (TTH_2.0,
TTA_1.0, OU_5.0, OU_6.0, OU_2.25…). Takové trhy `marketsim._fit_rows` do odhadu gólů nebere ani v produkci, protože
mají vrácení vkladu. Hodnocený kurz tedy odhad gólů neovlivňuje nikdy a jeho vynechání (LOBO = vynech jeden trh)
nic nemění. Odhad gólů je v obou případech ze stejných trhů (u ukázky Galatasaray – Aston Villa 20 trhů, λ 1,576 : μ 1,834),
takže EV je totožné.
Ze všech 36 výběrů Profilu 3 nebyl ve fitu ani jeden.

- **Je Profil 3x nekruhový?** Ano v úzkém smyslu: hodnocený kurz se do jeho reference nedostane (test
  `test_evaluated_market_price_does_not_move_lobo_reference_but_moves_production`). U současných výběrů to ale
  NEDOKAZUJE nezávislost od kanceláří. Odhad gólů vzniká ze sousedních linií (TTH_1.5, TTH_2.5…) týchž 2–3
  kanceláří z API-Football, jejichž medián tvoří i hodnocený kurz. Je to informačně provázané, i když ne kruhové.
- **Proč je „EV proti kancelářím“ prázdné:** reference kanceláří (`marketcheck`) se počítá z kurzového API. To nemá
  týmové góly ani celé či čtvrtinové linie, ty jsou jen v API-Football. Všech 17 výběrů má proto stav
  MISSING_MARKET. Hodnocený kurz je navíc medián 2–3 kanceláří, ne kurz jedné kanceláře, takže není koho vynechat.
  Minimum 3 nezávislých skupin by stejně nesplnil.
- **„Jen P3 19, jen P3x 0“:** těch 19 zápasů mělo výkop dřív, než stín 3x začal vybírat (24. 11. 17:04 UTC).
  Po spuštění je 0 zápasů, které by měl jen jeden z profilů.

Nový výstup (list Profile 3 LOBO, panel Audit měření, CLI `tipsbot profile measure`) ukazuje u každého výběru:
- EV produkce, EV bez hodnoceného trhu a EV výběru 3x;
- EV proti kancelářím (jen při platné referenci) a počet nezávislých skupin;
- vynechanou kancelář nebo skupinu;
- dva stavy: OK / INSUFFICIENT_BOOKMAKERS / MISSING_MARKET / INVALID_LINE / CIRCULAR_REFERENCE / CALCULATION_ERROR;
- důvod. Nedostupný výpočet je prázdný, nikdy EV 0.

## 2. Profil 5 – 11 odmítnutých nabídek, 22 důvodů

Je to správně, důvody se překrývají. Každá odmítnutá nabídka je uložena jednou (id = zápas + trh + výběr +
kancelář) a může mít víc důvodů.

| Důvod | Nabídek | Důvodů | Z toho i s jiným důvodem |
|---|---:|---:|---:|
| cenový extrém (nad mediánem) | 11 | 11 | 9 |
| kurz mimo pásmo | 9 | 9 | 9 |
| cenový extrém (nad férovou cenou, nepotvrzen) | 2 | 2 | 2 |
| **Celkem** | **11** | **22** | **9** |

- Duplicity a překryvy:
  - unikátní kandidát 1;
  - duplicitní řádky 0;
  - překryv kandidát × odmítnutí 0;
  - kontrola `profile 5 validate` hlásí 0 problémů.
- Kandidát (gtbets 1X2 „1“ @ 1,19) prošel kontrolou kurzu se stavem OVĚŘENO. Reference je z 20 kanceláří bez gtbets
  a bez dvou burz.
- Po skupinách kanceláří je odmítnutých nabídek 8, ne 11: unibet_se a unibet_nl mají stejnou cenu (3 dvojice).
- Nabídky bez dostatečné reference se po nabídkách neukládají. Je jen počitadlo posledního skenu: 1 781 z 1 840.
  V posledním skenu (17:04) byly kurzy v cache 665 min staré, nad limitem 360 min. Reference proto správně nevznikla.
- Nabídky bez platného trhu: 0.

## 3. CLV – 289 proti 276 (přesné odvození)

Obě čísla počítají jen Profil 4 (všechna rozhodnutí). Rozdíl byl chyba v mé diagnostice Odds Freshness (commit
550c2db): počítala surové řádky tabulky `tips` včetně opakovaně uložených tipů.

```
Vyhodnocených záznamů v DB 289 − 66 opakovaně uložených − 6 uložených po výkopu − 1 neplatná = 216 vyhodnocených sázek
CLV Performance 276 = 216 vyhodnocených + 60 zatím nevyhodnocených
```

**Oprava:** obě tabulky teď berou tipy z jedné funkce `history.clv_scope` (unique_bets + bez neplatných). Odds
Freshness ukazuje jen vyhodnocené (216), CLV Performance i nevyhodnocené (řádek „zatím nevyhodnoceno“). Na obou
listech i v panelu je tabulka rozsahu: profily, sport, stav vypořádání, období, vyloučené tipy a důvody.

Kontrola shody po druzích závěrečného kurzu:

| Druh kurzu | Odds Freshness (fotbal + hokej) | CLV Performance |
|---|---|---:|
| zastaralý | 157 + 11 | 168 |
| ověřitelný | 10 + 12 + 9 | 31 (= 10 + 21) |
| jen poslední pozorovaný | 0 + 5 | 5 |
| změna trhu / linie | 1 + 0 | 1 |
| chybí | 1 + 10 | 11 |

Opravená CLV coverage (dříve nadsazená duplicitami):

| Tipy | Sport | Dříve | Nyní |
|---|---|---|---|
| Všechny | fotbal | 15 % (34/232) | 12 % (22/181) |
| Všechny | NHL | 35 % (20/57) | 26 % (9/35) |
| APPROVED | fotbal | 34 % | 31 % (13/42) |
| APPROVED | NHL | 60 % | 60 % (9/15) |

- Týdny u fotbalu: 10 % (týden od 16. 11.) → 44 % (od 23. 11., jen 9 tipů).
- Zastaralý ani chybějící kurz se nikde nepočítá jako CLV 0.

## 4. Market Integrity – pokrytí

| Ukazatel | Počet |
|---|---:|
| Výběrů celkem | 194 |
| Záznam kontroly má | 66 (34 %) |
| Bez záznamu (starší výběry) | 128 |
| OVĚŘENO | 2 (1 % všech výběrů) |
| NEOVĚŘENO (kontrola proběhla, data nestačí) | 64 |
| PODEZŘELÉ | 0 |
| Porovnatelných nabídek (stejná nabídka u ≥ 2 profilů) | 26 |
| Skutečné neshody | 0 |

„0 neshod“ platí jen pro 26 porovnatelných nabídek, ne pro 194 výběrů. Excel, panel i CLI to teď uvádějí výslovně.

## 5. Nálezy v produkční logice – NEOPRAVENO, čeká na souhlas

1. **Profil 3x u současných výběrů nic neměří.** Leave-one-market-out je u celých a čtvrtinových linií prázdná
   operace.
   - Návrh (stínový, bez API): při odhadu gólů pro Profil 3x vynechat kanceláře, ze kterých je hodnocený medián.
     Data jsou po kancelářích uložená v `odds_quotes`.
   - Riziko: u 2–3 kanceláří nezbude dost dat a výsledek bude většinou INSUFFICIENT_BOOKMAKERS.
2. **Tvrdé odmítnutí „nad férovou cenou a nepotvrzen“ nekontroluje počet skupin reference** (`marketcheck.check`).
   Odchylka od mediánu vyžaduje aspoň 3 skupiny, tahle podmínka ne. Výběr tak může být PODEZŘELÝ i podle slabé
   reference z 1–2 kanceláří.
   - Dopad dnes: 0 případů (všech 11 odmítnutí mělo 8 nebo 17 skupin).
   - Návrh: přidat `ref["n"] >= min_ref_books`.
3. **Skupiny kanceláří podle předpony názvu nespojí betsson a nordicbet.** Je to stejný provozovatel, oba měli
   kurz 21,0. Počet „nezávislých“ skupin se tím nadhodnocuje.
   - Návrh: ruční tabulka aliasů provozovatelů.
4. **Profil 5 nemá referenci, když je cache kurzů starší než 6 h** (sken 17:04). Chování je správné. Jen upozorňuji,
   že takový sken nic neměří.

## Co se změnilo v kódu (jen diagnostika a prezentace)

- `profil4/history.py`: `clv_scope` – jeden rozsah tipů pro obě CLV tabulky.
- `profil4/performance.py`: CLV Performance používá `clv_scope` (počty beze změny) a vrací rozsah.
- `freshness.py`: CLV coverage se stejným rozsahem (oprava duplicit).
- `marketsim.py`: pomocná funkce `in_fit` vyčleněná z `_fit_rows`, chování beze změny.
- `strategy.py`: `p3_audit`, `integrity_coverage`.
- `profil5.py`: `counts` – unikátní nabídky odděleně od jednotlivých důvodů.
- `export.py`: tabulky auditu v existujících listech (počet, názvy a pořadí 30 listů zachované), `fcell`.
- `web.py`: Sledování → 🔎 Audit měření; Čerstvost kurzů ukazuje rozsah CLV; Srovnání strategií „0 z N
  porovnatelných“.
- `profilecli.py`: `tipsbot profile measure`; `market-integrity` a `5 report` s jasnými počty.
