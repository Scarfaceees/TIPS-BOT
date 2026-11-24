# Fáze 3.2.1 – Strategy Audit, Market Integrity & Profil 5 (Shadow Mode)

Stav k 24. 11. 2026, větev `dev`. Výchozí HEAD před fází: `b319d36` (po Fázi 3.2 `a511dc6`, oprava NHL PUSH `e9ca2fd`
a pozdějších úpravách tiketů a rozborů). Podklad: `tipsbot-vysledky-2026-11-23-2258.xlsx` (20 listů) porovnaný
s aktuální databází panelu (`data/tipsbot.db`) a Profilu 4 (`data/profil4.db`, jen čtení).

Nic z této fáze nemění modely A–D, jejich váhy, rankingové váhy, výpočty EV, settlement, minimální EV 5 %, limity
kurzů, podmínku 25 % prohry, poměr 25/75 u rohů a karet, prahy Profilu 4, risk management ani API Budget Manager.
Historické tipy se nepřepisují ani nemažou. Výsledky papírových strategií nejsou důkaz dlouhodobé výnosnosti.

---

## 1. Audit původního stavu

### 1.1 Repozitář a data
- Větev `dev`, pracovní strom čistý (jen netrackované zálohy a archivy uživatele).
- Excel: 420 záznamů, 22 kombi nohou, 13 pohybů bankrollu, 130 vyřazených duplicit/tipů po výkopu – sedí s databází
  k času exportu. Bankroll mezitím uživatel vynuloval (ostrý režim, vklad 1 000 Kč, smazané ukončené tikety) –
  „Moje tikety“ proto v novém exportu nejsou.
- Kontrolní přepočet čísel ze Souhrnu proti databázi: Základ 55 / +8,42 j. / +15,31 %, Profil 2 33 / −1,26 / −3,82 %,
  Profil 3 19 vyhodnoceno (15 vsazených + 4 vrácené) / −3,51 / −23,40 %, Kombi 7 / +4,49 / +64,18 % – shodné.
  Profil 4 APPROVED mezitím 54 vyhodnocených / +10,44 j. (další vyhodnocené zápasy po exportu).

### 1.2 Definice v Excelu (ověřeno v kódu `export.py`)
- **Vyhodnocená sázka** = výsledek ≠ čeká. **Výhra** = won + half_won, **prohra** = lost + half_lost.
- **PUSH** (vrácení vkladu) ≠ **INVALID** (neplatná sázka). Oba mimo jmenovatel ROI.
- **ROI na 1 j.** = zisk / (výhry + prohry) – je to *yield* na jednotku vkladu.
- **Nesoulad (opraveno)**: panel Sledování dělil zisk i vrácenými sázkami → u Profilu 3 ukazoval −18,5 % místo −23,4 %.
  Teď stejná definice jako Excel (`paper.stats`).
- **Nesoulad (jen popsán)**: Profil 4 v panelu (`history.bet_stats`) počítá PUSH do jmenovatele yieldu a jeho „ROI“ je
  zisk / 100 j. kapitálu. Excel počítá bez PUSH. Návrh viz kapitola 18 – vyžaduje souhlas (týká se Profilu 4).
- Rozlišení počtů: *zápasy* ≠ *unikátní výběry* ≠ *předpovědi* ≠ *oficiální tipy* ≠ *virtuální sázky* ≠ *skutečné tikety*.
  Např. 5 802 předpovědí fotbalu v Model Comparison = 50 zápasů; 2 245 výběrů rankingu = 19 zápasů.

### 1.3 Nesoulad 70 vs. 50 + 19 zápasů (Profil 4)
- Souhrn „přesnost modelů na všech předpovědích“ = 70 zápasů (A–D), trh bez marže 69 zápasů.
- Model Comparison = **jen řádky, kde mají předpověď všechny čtyři modely i trh** (`common_sample`) → 50 + 19 = 69.
- Rozdíl 1 zápas = zápas bez de-vig ceny trhu v době předpovědi (proto i „trh 69“ v Souhrnu). Není to chyba exportu,
  jen jiný vzorek. Každá tabulka svůj vzorek uvádí; nic se uměle nesjednocuje.

### 1.4 Skutečné problémy (doložené)
| # | Problém | Důkaz | Stav |
|---|---------|-------|------|
| P1 | **Kombi tikety jsou 100 % hypotetické**: všech 69 tiketů / 138 nohou má jen cílový (minimální) kurz „cíl – ověř u kanceláře“; žádná noha nemá ověřenou nabídku | DB `paper_picks` | Odděleno ve statistice (ROI hypotetické vs. ověřené), konstrukce beze změny |
| P2 | **Profil 3 je kruhový**: góly se fitují ze stejných mediánových kurzů, které se pak hodnotí; scénář „trh“ (váha 50 %) dává EV ≈ −marže | `marketsim.fit_lambdas` + hodnocení týchž `entries`; 19 z 28 výběrů z mediánu jen 2 kanceláří API-Football | Paralelní LOBO měření; strategie beze změny |
| P3 | **Rohy a karty: 13 ze 14 tipů je ze staré metodiky** (jen model). Metodika 25/75 platí od commitu `25366af` (21. 11. 2026 22:09 UTC); pod ní je **1 tip** | `created` < 22:09 UTC, `detail` bez „% model (“ | Metodiky vykazovány zvlášť |
| P4 | Rohy a karty sdílely jeden výběr na zápas (karta s vyšším EV vytlačila roh) | `_best` s `props=True` | Rozděleno na dva profily |
| P5 | Panel a Excel měly jiný jmenovatel ROI (vrácené sázky) | viz 1.2 | Sjednoceno |
| P6 | **Model A Profilu 4 – degenerovaný odhad u Bodø/Glimt – LASK** (LM): výhra domácích 99,998 % (trh 63 %), 92 % na 7+ gólů domácích; C = ½A + ½B → 81 % | `forecasts` | Jen popsáno (modely A–D se nemění) – návrh v kap. 18 |
| P7 | Opportunity Ranking převážně kopíruje Quality of Data: korelace rank × kvalita 0,70, rank × EV 0,10; kvalita sama vysvětluje 50 % rozptylu, s konz. EV a robustností 73 % | 5 497 řádků `rankings` | Jen popsáno (váhy se nemění) |
| P8 | CLV: z 279 vyhodnocených tipů P4 má 204 (73 %) „zastaralý kurz“, ověřený závěrečný jen 54 (19 %) | `tips.closing_kind` | Návrh načasování (kap. 18) |

### 1.5 Potenciální problémy (podezření, nedoloženo)
- **Základ přeceňuje outsidery**: průměrné EV podle modelu roste s kurzem (1,5–2: +10 %, 2,5–3,5: +21 %, 3,5–5,5: +28 %);
  26 z 55 vyhodnocených výběrů má EV > 15 %. Skutečná úspěšnost 2,5–3,5 je 33 % proti čekaným 42 %. Vzorek je malý.
- Profil 2: `p_win` počítá **půl výhru jako celou výhru** (podmínka „výhra ≥ 60 %“); `p_loss` počítá půl prohru napůl.
  Asymetrie u asijských/čtvrtinových linií. Nově se ukládá všech 5 výsledků zvlášť (kap. 8).
- Ranking Validation NHL: v `rankings` je 12 zápasů NHL (431 řádků), ale ve validaci zatím žádný vyhodnocený – ověřit,
  jestli se rankingy NHL po zápase párují s výsledkem (nezkoumáno do hloubky).
- Profil 4 Model D: over + under ≠ 1 (u 1 597 z 2 321 dvojic) – **záměr**, D je dolní odhad každého výběru zvlášť
  (min přes konzervativní matice); Brier D je proto mírně zkreslený a jeho Ø odhad (0,46) je pod skutečností (0,49).

### 1.6 Správně fungující části (ověřeno)
- Settlement Integrity: 25 717 kontrol, 0 neshod; PUSH a INVALID oddělené; verze a auditní stopa zachovány.
- Market Integrity Profilu 4 u Bodø/Glimt – LASK zablokovala všech 20 extrémních nabídek správně jako NO BET (viz 2.3).
- Duplicity: jeden výběr jednoho zápasu v profilu = jedna sázka; tipy po výkopu vyřazeny (130).
- Kombi: každá sázka se ve statistice počítá jednou (`kombi.overlaps`).
- Bootstrap po zápasech (Profil 4 `cluster_ci`) – teď použit pro všechny profily.

---

## 2. Přehled profilů

| Profil | Hypotéza | Pravděpodobnost | Cena | Výběr |
|--------|----------|-----------------|------|-------|
| Základ | model bota najde hodnotu | model bota | medián zahraničních kanceláří (nebo ruční české kurzy) | max EV ≥ 5 % na zápas |
| Profil 2 | výběr robustní vůči chybě modelu | λ modelu, kompromis 50/50, trh (kontrola), gama nejistota | nejlepší kurz zadaný | EV > 0 v A i B; nejdřív prohra ≤ 25 %, pak 1,5+ s výhrou ≥ 60 %, pak hodnota |
| Profil 3 | „bezpečné“ nízké kurzy přes simulaci trhu | λ fitované z kurzů (kruhové), 4 scénáře | medián kanceláří | kurz 1,15–1,75, prohra ≤ 25 %, nejhorší scénář ≥ −15 % |
| Tipy bota | oficiální tipy | model + Pinnacle | kurz tipu | práh EV bota |
| Kombi | součin hodnotových nohou | součin p | součin kurzů (dnes jen cílové kurzy) | Přehled |
| Rohy | rohy jinak než trh | 25 % model + 75 % trh | kurzy kanceláří API-Football | EV ≥ 5 % |
| Karty | karty (rozhodčí) jinak než trh | 25 % model + 75 % trh | totéž | EV ≥ 5 % |
| Profil 4 | modely A–D + integrita + ranking | A–D | API-Football / kurzové API | APPROVED / WATCH / NO BET |
| **Profil 5** | **cena kanceláře nad nezávislým konsenzem** | **trh bez hodnocené kanceláře** | **nabídka kanceláře** | **shadow, ≥ 3 nezávislé, integrita** |

## 3. Metodika každého profilu
Podrobně v `paper.PROFILES` (texty v panelu), `strategy.METHODS` (list Strategy Comparison) a v modulech
`profile2.py`, `marketsim.py`, `paper.py`, `kombi.py`, `profil4/`, `profil5.py`.

## 4. Překryvy a mezery
- **Profil 3 ≈ trh**: kruhový tržní scénář → profil fakticky měří „model bota (25 %) + stres ±12 %“ nad cenou trhu.
  Nalézá sázky kolem férové ceny (pravidlo dovoluje i vážené EV do −3 %), takže dlouhodobě se čeká ≈ −marže.
- **Profil 2 a Základ** používají tentýž model bota (λ). Profil 2 přidává nejistotu a trh jako brzdu – jiná výběrová
  pravidla nad stejnou informací, ne nová informace.
- **Kombi** staví na nohách Přehledu (tytéž hodnoty jako tipy/value) – nejde o novou hypotézu, jen jiný typ sázky;
  navíc s hypotetickými cenami.
- **Rohy/karty** a **Profil 5** jsou jediné profily s jinou informací než model gólů bota.
- **Profil 5** je první profil, který vůbec nepotřebuje model týmů → nezávislý benchmark.

## 5. Opravené problémy
- Jednotný jmenovatel ROI/yield v panelu (P5).
- Rohy a karty jako samostatné strategie, výběr jednoho trhu nevytlačuje druhý (P4); historické záznamy beze změny,
  zobrazují se podle trhu (`paper.group_key`).
- Kombi: hypotetické a ověřené výsledky už se nesčítají; nové nohy evidují ceny (P1).
- Profil 5: závěrečná cena až z pozdějšího skenu než zjištění kandidáta (jinak by CLV = vlastní EV) – nalezeno testem.

## 6. Společná Market Integrity (`marketcheck.py`, verze `mi-1`)
- Jedna funkce `check()` pro každou nabídku; prahy převzaté z Profilu 4 (`MIConfig.from_p4`): stáří ≤ 360 min,
  odchylka od mediánu > 10 % = extrém, > 10 % nad férovou cenou bez potvrzení = extrém, potvrzení = jiná kancelář
  max. o 5 % níž, marže −1 % až +25 %.
- Oddělené vlastnosti: **platnost** (trh, linie, strana), **čerstvost** (čas změny u poskytovatele), **protistrana**,
  **dostupnost** (u mediánu: kolik kanceláří nabízí aspoň tento kurz), **potvrzení**, **důvěryhodnost reference**
  (leave-one-bookmaker-out, ≥ 3 nezávislé skupiny), **časová srovnatelnost** (kurz vs. reference ≤ 180 min).
- Stavy: OVĚŘENO / ČÁSTEČNĚ OVĚŘENO / NEOVĚŘENO / PODEZŘELÉ. Chybějící data = NEOVĚŘENO, nikdy „v pořádku“.
- Profily Základ, 2, 3, Rohy, Karty ukládají výsledek k výběru (`Pick.integrity`). **Kontrola nemění výběr, EV,
  pravděpodobnosti ani prahy.** Starší výběry nemají kontrolu (nic se zpětně nedopočítává).
- Stejná nabídka = stejný verdikt ve všech profilech (jedna deterministická funkce; `strategy.integrity_consistency`
  hledá neshody – v testech i na živých datech 0).
- Kontrola **nemůže** zatím zabránit, aby Základ vybral nabídku, kterou Profil 4 zamítne – to by byla změna výběru
  (návrh v kap. 18). Stav kontroly je ale u výběru vidět a ve statistice počítán.

### 2.3 Bodø/Glimt – LASK Linz (list Market Integrity)
- Kurzy samotné nejsou chybné: bet365 29 proti mediánu 20,75 na „domácí 6,5+ gólu“ je běžný rozptyl dlouhých kurzů,
  jen 2–3 kanceláře, stáří 175 min (pod limitem 360).
- **Příčina extrémního EV je model A** (výhra domácích 99,998 %, 92 % na 7+ gólů), který přes C (50 % A) dává
  „nesoulad modelu (47 %) a trhu (5 %)“. Jediný takový případ ze 145 předpovědí 1X2 (A > 97 %).
- Detekce je správná (PODEZŘELÝ KURZ → NO BET). Ostatní profily: Základ/2/3 tento zápas neviděly (UCL zápas s modelem
  bota bez extrému); Profil 5 model nepoužívá, takže by tento případ nevznikl vůbec. Neplatí to obecně pro všechny kurzy.

## 7. Rozdělení Rohů a Karet
- Profily `rohy` (trhy `ROHY_`) a `karty` (`KARTY_`), každý vlastní výběr na zápas; metodika 25/75 beze změny.
- Výběry původního profilu „Rohy a karty“ se nepřepisují – patří do statistiky podle trhu.
- Metodika se vykazuje zvlášť: „jen model“ (do 21. 11. 2026 22:09 UTC) × „25/75“ (od té doby; u nových výběrů uložena
  v `meta.method`). Stará metodika se do nové nepočítá.
- Každá strategie má vlastní: výběry, nezávislé zápasy, výhry/prohry, yield s intervalem, Brier, Log Loss, CLV
  (dnes žádné ověřené), stav kontroly kurzů a spolehlivost (Excel: Corners/Cards Performance, panel, CLI).
- Stav: Karty 12 (11 jen model / 1 25/75), Rohy 2 (jen model) – **MÁLO DAT**, nelze nic usuzovat. Oba stínově.
- Karty – kontrolní body (stav kódu): model týmů + rozhodčí (`referees.py`); trh KARTY_x = počet karet (žluté + červené
  dle football-data.co.uk, druhá žlutá podle zdroje); booking points v systému nejsou; statistiky jen předzápasové.
  Rohy – týmové průměry doma/venku z `teamreport`; sestavy ani styl hry model rohů nezná.

## 8. Úpravy Profilů 2 a 3
**Profil 2** (`profile2.scenario_data`, ukládá se do `Pick.meta.p2` u nových výběrů):
λ, EV a bod zvratu každého scénáře, **5 výsledků zvlášť** (výhra, půl výhra, vrácení, půl prohra, prohra) se správným
rozdělením čtvrtinových linií, citlivost šance na výhru na chybu λ (5.–95. kvantil gama(k)), počet simulací a seed,
role (proč doporučen). Detail v panelu: „Profil 2 – scénáře“ v rozboru výběru.
- Ověřeno: EV ze 5 výsledků = EV enginu (test). Simulace (1 000, seed z názvu zápasu) **neovlivňuje výběr ani EV**.
- Podmínka „prohra ≤ 25 %“ měří pravděpodobnost ztráty vkladu v horším scénáři (půl prohra napůl), ne riziko propadu
  ani velikost ztráty. Alternativa (návrh): očekávaná ztráta = Σ p·ztráta, nebo dolní kvantil výnosu.

**Profil 3** (`Pick.meta.p3` + `meta.p3_lobo`): λ, chyba fitu, EV/výhra/vrácení/prohra každého scénáře, váhy,
seed, a **LOBO experiment**: góly fitované bez trhu hodnoceného výběru → nezávislé EV (`ev_lobo`) vs. vlastní (`ev_own`).
Bez aspoň 2 jiných trhů = NEOVĚŘENO. Strategie beze změny. List „Profile 3 LOBO“, CLI `profile audit`.
- Simulační stabilita (`marketsim.sim_stability`): u λ 1,45:1,15 je směrodatná odchylka podílu nejčastějšího skóre
  při 1 000 simulacích ~1 p. b., při 10 000 ~0,3 p. b. Šance a EV se počítají přesně ze skórové matice, proto
  **počet simulací nemění výběr ani pořadí** (test se dvěma seedy). Doporučení: počet neměnit; jde jen o zobrazení.

## 9. Ověřování kombi kurzů
- Nové nohy: `target_odds`, `verified_odds`, `verified_book`, `verified_at`, `available`, `price_source`, `first_odds`.
- Třídy tiketu: **ověřené ceny**, **ověřené – kurz se změnil**, **nelze sestavit za uvedený kurz**, **hypotetický**.
- Starší nohy: cílová = hypotetická; necílová = kurz kanceláře z kurzového API v době návrhu.
- Statistika zvlášť: hypotetické ROI, ROI z ověřených tiketů, počet plně ověřených, počet s neověřenou nohou,
  unikátní nohy a zápasy (tikety sdílejí zápasy – nejsou nezávislé). Souhrn Excelu: „Kombi tikety – hypotetické
  (cílový kurz)“ 11 tiketů, ověřených 0. **Hypotetické ROI +64 % není výkonnost dosažitelná u kanceláře.**
- Konstrukce tiketů beze změny.

## 10. Profil 4 – přehledy
- `history.evaluate` má nově skupiny **„sport“** a **„sport · rozhodnutí“** (panel Profil 4, první dvě rozbalovací
  tabulky); Strategy Comparison má APPROVED/WATCH/NO BET celkem i zvlášť fotbal a NHL.
- Živá data (24. 11.): APPROVED fotbal 42 vyhodn. yield +11,7 % [−59; +84], NHL 12 vyhodn. +49,8 % [−5; +104];
  WATCH fotbal −25,5 % [−49; 0]; NO BET NHL +68 % (7 zápasů). **Vše MÁLO DAT nebo neprůkazné** – nelze tvrdit,
  že jedna kategorie je lepší.
- Modely A–D, ranking, kalibrace, Model Comparison beze změny. Ranking: viz P7 (kopíruje kvalitu dat); NHL zatím bez
  vyhodnocené validace.

## 11. Architektura Profilu 5 (`profil5.py`, algoritmus `p5-1.0`)
1. Ze **stejných, už stažených kurzů** kurzového API (cache skenu, žádný další dotaz) se pro každou nabídku
   (zápas, trh 1X2 / OU / moneyline / puckline, výběr, kancelář) spočítá reference.
2. Kandidát: EV proti trhu ≥ 3 % (`min_edge`, experimentální výchozí hodnota, neladěná), kurz v pásmu nastavení
   (1,15–4,20), reference ≥ 3 nezávislé skupiny, **Market Integrity OVĚŘENO** (čerstvost, protistrana, extrémy,
   potvrzení, časová srovnatelnost). Nevyhovující nabídky nad prahem se zapíšou jako odmítnuté s důvody.
3. Na zápas nejvýš jeden kandidát (nejvyšší EV z těch, které prošly). Vstupy se po zapsání nemění; další skeny dopisují
   jen závěrečnou cenu (konsenzus bez hodnocené kanceláře) → CLV, po zápase výsledek.
4. Tabulka `p5_candidates`: ID, čas, verze, snapshot trhu (všechny kanceláře, časy), zahrnuté/vynechané kanceláře
   s důvodem, p_ref, férový kurz, kurz kanceláře, EV, odchylka, integrita, line shopping, CLV, výsledek, zisk.
5. Vyhodnocení: fotbal stejnými daty jako Sledování (`settle_bet`), NHL přes NHL API (`nhl_settle.grade`).

## 12. Leave-One-Bookmaker-Out
- Hodnocená kancelář **i celá její skupina** (`unibet_se`/`unibet_nl`… – stejný provozovatel, stejné ceny) se vynechá.
- Vynechají se také burzy, neúplné trhy (chybí protistrana), kurzy starší 360 min a trhy s marží mimo −1 % až 25 %.
- Každá zbylá kancelář: de-vig mocninnou metodou (`devig_power`); skupina = medián; reference = medián skupin
  (korelované zdroje tak neváží víc).
- Férový kurz = 1 / p_ref; EV = kurz × p_ref − 1; odchylka = kurz / medián ostatních − 1.
- Stejný trh = stejný klíč trhu kurzového API (1X2, OU_x s přesnou linií, PUCKLINE ±1,5), stejná strana = stejný výběr.
- < 3 nezávislé skupiny → žádný důvěryhodný odhad (kandidát nevznikne). Hranice se automaticky nesnižuje.
- `tipsbot profile 5 validate` z uloženého snapshotu znovu ověří, že hodnocená kancelář ani její skupina v referenci
  nejsou a že p_ref i EV jdou spočítat znovu.

## 13. Shadow Mode
Profil 5 nevydává tipy, nemění bankroll, Portfolio Risk, rozhodnutí APPROVED/WATCH/NO BET ani jiné profily, nepoužívá
modely A–D ani model týmů. Chyba Profilu 5 nesmí shodit sken ani vyhodnocení (try/except). Ověřeno testem
(bankroll, Sledování a tipy beze změny). Market Value a Line Shopping se měří zvlášť (kap. 16).

## 14. Databázové migrace
- Nová tabulka `p5_candidates` (vzniká sama přes `create_all`, nedestruktivní). Stávající tabulky beze změny.
- Nová pole v JSON záznamech Sledování (`integrity`, `meta`; nohy kombi `target_odds`, `verified_*`, `available`,
  `price_source`, `first_odds`) – starší záznamy je nemají a čtou se s výchozí hodnotou.
- Profil 4: žádná změna schématu.
- Záloha před nasazením: `backups/tipsbot.db.20261124-faze321.bak`.

## 15. Nové CLI příkazy (jen čtení)
```
tipsbot profile audit
tipsbot profile market-integrity
tipsbot profile compare
tipsbot profile 5 shadow
tipsbot profile 5 report
tipsbot profile 5 validate
```

## 16. Nové listy Excelu (20 původních zachováno)
Strategy Comparison · Market Integrity – All Profiles · Profile 5 Market Value · Profile 5 Line Shopping ·
Corners Performance · Cards Performance · Combo Odds Verification · Profile 3 LOBO. Každý list má na řádku 5 metodiku
a rozsah dat; prázdná hodnota zůstává prázdná (test). Souhrn: Rohy a Karty zvlášť, kombi jako „hypotetické“.

## 17. Testy
`tests/test_phase321.py` – 19 testů: společná integrita (stejná nabídka = stejný verdikt, neshoda se ukáže), LOBO
vynechá kancelář i skupinu, de-vig, zastaralé kurzy, chybějící protistrana, málo kanceláří = NEOVĚŘENO, jiná linie
= jiný trh, dostupnost u mediánu, záznam integrity bez změny výběru; Profil 2 – 5 výsledků, EV scénářů, čtvrtinová
linie, citlivost; Profil 3 – LOBO bez hodnoceného trhu, NEOVĚŘENO, nezávislost na seedu, stabilita simulace; rohy/karty
– oddělení, metodika podle času/detailu, nezávislý výběr; kombi – hypotetické vs. ověřené, změna kurzu, unikátní nohy;
PUSH ≠ INVALID a mimo jmenovatel; Profil 4 podle sportu; Profil 5 – kandidát, shadow, duplicity, CLV až z dalšího skenu,
málo kanceláří, extrém, vyhodnocení, Line Shopping zvlášť; export – nové listy, staré zachované, prázdné ≠ 0; panel.
Původní testy beze změny.

## 18. Známá omezení a změny vyžadující souhlas
Omezení:
- Profil 5 zatím nemá výsledky (kandidáti vznikají až novými skeny). Kurzové API se stahuje po ~6 h → závěrečná cena je
  většinou jen „poslední pozorovaný kurz“ (CLV se nevydává za ověřené). Profil 5 pracuje jen s trhy kurzového API
  (1X2, góly, NHL moneyline/puckline); trhy API-Football (Profil 4) nepoužívá – jiné mapování trhů by vyžadovalo
  vlastní ověření.
- Společná integrita existuje jen u nových výběrů; Profil 2/3 často hodnotí mediánový kurz → dostupnost = počet
  kanceláří s aspoň tímto kurzem, ne konkrétní nabídka.
- Párování zápasů Profil 5 × Profil 4 je přibližné (datum + začátky názvů týmů).

Návrhy (neprovedeno, čeká na souhlas):
1. Model A: pojistka proti degenerovanému odhadu (např. strop λ nebo vyloučení A, když se od trhu liší o > X log-sancí,
   nebo když týmy nemají společné soupeře) – Bodø/Glimt.
2. Profil 3: tržní scénář fitovat bez hodnoceného trhu (LOBO) místo kruhového fitu – až po vyhodnocení měření.
3. Profil 2: počítat půl výhru jako ½ výhry v podmínce 60 % a zvážit metriku očekávané ztráty místo „prohra ≤ 25 %“.
4. Sledování: výběr se stavem PODEZŘELÉ neukládat / nepočítat (dnes jen označen) – změna výběrové logiky.
5. Kombi: nepoužívat cílové nohy nebo vytvářet tikety jen z ověřených nabídek – změna konstrukce.
6. Profil 4: yield bez PUSH v panelu (sjednocení s Excelem).
7. CLV: přesunout existující sken kurzového API zápasů s výběry na ~T−15 min místo pevného 6h cyklu (stejný počet
   požadavků, víc skutečných závěrečných kurzů).
8. Ranking: přehodnotit váhu kvality dat (P7) – až s daty (≥ 100 zápasů).

## 19. Ověřeno na živých datech
- Export na kopii živé DB (24. 11.): 28 listů, čísla Souhrnu shodná s původním Excelem (kap. 1.1).
- Srovnání strategií na živé DB (kopie) – tabulka v kap. 10.
- Profil 5 přehraný na uložených kurzech kurzového API (10 soutěží, 2 099 posouzených nabídek): 116 bez důvěryhodné
  reference, 17 nad prahem EV, 14 odmítnuto (12× cenový extrém nad mediánem, 2× kurz mimo pásmo), **3 kandidáti**
  (Arsenal – Dortmund 1 @ 1,46 onexbet, Leipzig – Lens 1 @ 1,46 gtbets, Seattle – Anaheim over 6,5 @ 1,97 tipico_de;
  EV proti trhu +3,1 až +3,2 %, 12–17 nezávislých skupin). Validace: 0 problémů. (Na kopii; živá evidence začíná
  prvním skenem po nasazení.)
- Panel (Docker) a mobilní zobrazení 375 px – viz závěrečný report.

## 20. Ověřeno pouze simulací / syntetickými daty
- LOBO a de-vig na syntetických trzích, Profil 2/3 scénáře a stabilita simulace, vyhodnocení Profilu 5, oddělení
  kombi cen, PUSH/INVALID, export (testy). Predikční přínos žádného profilu **není** prokázán – všechny vzorky jsou pod
  hranicí průkaznosti.
