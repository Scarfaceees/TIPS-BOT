# Fáze 3.2.2 – Safety Guards, Settlement Consistency & Data Quality

Stav 24. 11. 2026, větev `dev`, výchozí HEAD `d82676d` (Fáze 3.2.1), pracovní strom čistý. Databáze před fází:
233 výběrů Sledování (170 vyhodnocených), 279 vyhodnocených tipů Profilu 4, žádné otevřené tikety, bankroll 1 000 Kč
(ostrý režim), 0 kandidátů Profilu 5 (kurzy v cache byly při skenech 1 115 min staré → reference zastaralá, správně
žádný kandidát; viz kap. 7).

Neměnily se: modely A–D a jejich váhy, výpočty pravděpodobností a EV, prahy (EV 5 %, 25 % prohry, 60 % výhry, 25/75,
prahy Profilu 4), rankingové váhy, API Budget Manager, settlement. Historické výsledky se nepřepisují.

## 1. Model A – pojistka proti extrémním predikcím

### Příčina 99,998 % (Bodø/Glimt – LASK Linz, LM, 24. 11. 2026)
Rekonstrukce z cache API-Football (`fixtures league=2, season 2025+2026`, 515 zápasů, 443 odehraných před zápasem):
- λ domácích = exp(μ 0,021 + domácí výhoda Bodø 0,281 + útok Bodø 0,931 − obrana LASK (−1,364)) = **13,43 gólu**,
  λ hostů 0,18. Výsledek 1X2: 99,998 % / 0,002 % / 0,0001 %.
- Model A se u LM učí **jen ze zápasů soutěže** (vč. kvalifikace). LASK měl v datech 6 zápasů (Celtic 2×, AEK, Liverpool
  3:6, Sporting, Slovan) a s ostatními týmy je propojen jen přes ně. Jeho obrana (−1,36) vyšla mezi nejhoršími 2 %
  souboru; Bodø (22 zápasů, výhry nad Interem, City, Atléticem) má útok v horních procentech.
- Parametry týmů s málo zápasy se nestahují k průměru a λ = exp(útok − obrana) pak extrapoluje exponenciálně.
  Minimální podmínka modelu (≥ 5 zápasů týmu) byla splněna (6). Model C = 50 % A → 81 %.
- Jde o ojedinělý případ: ze 140 uložených předpovědí Modelu A je jediný (dalších max 0,945).

### Pojistka (`profil4/guards.py`)
- Pro celý zápas se porovná 1X2 Modelu A s trhem (Model B). Spustí se, když rozdíl log-šancí na některém výsledku
  ≥ **3,0** (poměr šancí 20×), nebo – bez ceny trhu – Model A dává výsledku ≥ **99 %**.
- Zdůvodnění prahů z dat (56 zápasů s A i trhem): medián rozdílu 0,31, 95. percentil 1,15, maximum mimo Bodø 1,92
  (7×), Bodø 12,03 (168 000×). Mimo Bodø Model A nejvýš 0,945, trh nejvýš 0,83 – 0,99 se u trhu nevyskytuje.
  Na 140 historických předpovědích by pojistka zachytila **jen Bodø** (ověřeno).
- Při spuštění: všechny výběry zápasu tvrdá výhrada „MODEL A – EXTRÉMNÍ PREDIKCE: …“ → NO BET (nesmí být APPROVED ani
  WATCH). Pravděpodobnosti, EV, ranking se nemění. Auditní stopa: celý záznam pojistky (p A, p trhu, rozdíl, λ, prahy,
  důvody) v `tips.intel.model_guard` a `rankings.detail.model_guard`, důvod v rozhodnutí.
- Měkká hranice `max_logit_gap` 0,5 (brání schválení jednoho výběru) zůstává beze změny.
- Nové parametry `DecisionConfig.model_a_guard_logit = 3.0`, `model_a_guard_p = 0.99` → mění se otisk konfigurace
  (`config_hash`) nově ukládaných predikcí; staré záznamy se nemění.

## 2. Globální Market Integrity – tvrdé odmítnutí
- Tvrdé odmítnutí = stav **PODEZŘELÉ** společné kontroly (`marketcheck`, prahy Profilu 4): kurz > 10 % nad mediánem
  ≥ 3 nezávislých kanceláří, > 10 % nad férovou cenou bez potvrzení, nebo kurzy kanceláře netvoří jeden trh.
  NEOVĚŘENO (málo dat) tvrdým odmítnutím není – jen se tak označí.
- **Sledování (Základ, Profil 2, 3, 3x, Rohy, Karty):** výběr zůstane jako diagnostický záznam se štítkem
  „BEZPEČNOSTNĚ ODMÍTNUTO – kontrola kurzu“, ale nepočítá se do výsledků profilu (`paper.counted`); v Excelu je ve
  skupině „… – bezpečnostně odmítnuto“. Rozhoduje kontrola před výkopem, ne výsledek.
- **Oficiální tipy bota:** tip s podezřelým kurzem se nevydá (ani do Přehledu, ani do Tipy bota); zapíše se záznam.
- **Profil 5:** odmítal už dřív; podezřelé nabídky se nově zapisují i do společné stopy.
- Profil 4 má vlastní (přísnější) integritu – beze změny.
- Nová tabulka `integrity_rejections`: profil, zápas, trh, výběr, kancelář, **původní kurz, referenční (férový) kurz,
  medián ostatních, stáří kurzu, počet nezávislých skupin, důvod**, celý výsledek kontroly. Jen přidávání.
- Kombi nohy se kontrolou neprocházejí (jsou dnes hypotetické, viz kap. 5).

## 3. Profil 2 – asijské vypořádání
- Podmínka „výhra ≥ 60 %“ (role „Kurz 1,5+ s vysokou šancí“) dřív počítala **půl výhru jako celou výhru**.
- Nově `win_share` = plná výhra + 0,5 × půl výhra (půl výhra = polovina vkladu vyhraje, polovina se vrátí). Push ani
  půl prohra se nepočítají. Práh 60 % beze změny; `p_win` a z něj `p_model` ve Sledování beze změny.
- Testy pro plnou výhru, půl výhru, push, půl prohru a plnou prohru (AH −0,25 / +0,25 / 0,0).
- Starší uložené rozbory pole nemají → použije se dosavadní hodnota.

## 4. Profil 3 – paralelní nekruhové měření
- Nový stínový profil **„Profil 3 – nekruhový (SHADOW)“** (`profil3x`): stejná pravidla jako Profil 3, ale góly každé
  nabídky se fitují **bez jejího trhu** (`marketsim.analyse_lobo`; produkční `analyse` beze změny); nabídka, u které nezbydou ≥ 2 trhy, se
  nehodnotí (NEOVĚŘENO). Rozbory Profilu 3 nepřepisuje.
- Produkční Profil 3 beze změny (test: stejný výběr s i bez stínu); měření `p3_lobo` u produkčních výběrů pokračuje.
- Srovnání jen na stejných zápasech (`strategy.paired`): panel (Profil 3 / 3x), Excel „Profile 3 LOBO“, CLI audit.
- Poznatek: celé linie (OU 2,0, 4,0) se do fitu nikdy neberou → u nich kruhovost nevzniká a stín vybere totéž.
  Dosavadních 9 měření `p3_lobo` dává průměrný rozdíl EV 0,0 p. b. – zatím bez důkazu, že kruhovost výběr mění.

## 5. Kombi tikety
- Tři oddělené skupiny: **hypotetické / neověřené**, **s ověřenými kurzy**, **skutečně vsazené** (Moje tikety, AKU,
  v Kč – nikdy se nesčítají s jednotkami).
- Do ověřené výkonnosti jen tiket, kde **každá** noha má doložený kurz kanceláře (`verified_odds`), který byl dostupný.
  Cílový minimální kurz není nikdy ověřený (ani když existuje vyšší nabídka). Starší nohy bez dokladu ceny = „neověřený
  (kurz bez dokladu, starší záznam)“ – dřív se necílová noha brala jako ověřená.
- Stav: 11 započítaných tiketů, plně ověřených 0; skutečně vsazených AKU tiketů 0 (po vynulování 24. 11.).

## 6. ROI a yield – jedna definice (`metrics.py`)
**Sázka s vkladem** = výhra, půl výhra, prohra nebo půl prohra; každá = 1 jednotka obratu.
**Zisk:** výhra = kurz − 1; půl výhra = (kurz − 1) / 2; půl prohra = −0,5; prohra = −1.
**Vrácení (PUSH)** a **neplatná sázka (INVALID)**: zisk 0, nejsou v obratu ani v počtu sázek, vykazují se zvlášť a
nejsou totéž. **YIELD = ROI na 1 j. = zisk / počet sázek s vkladem.** Skutečné tikety: ROI = zisk Kč / vsazené Kč.
**Win rate** = (výhry + půl výhry) / sázky s vkladem. Profil 4 navíc **zhodnocení kapitálu** = zisk / 100 j. – není to ROI.

Audit před změnou:
| Výstup | Dřív | Teď |
|---|---|---|
| Panel Sledování (`paper.stats`) | bez push (opraveno ve 3.2.1) | `metrics.is_staked` |
| Excel (`export.stats`) | bez push i neplatných | beze změny (už odpovídalo) |
| Profil 4 panel/CLI (`history.bet_stats`) | push **v** obratu; „ROI“ = zisk / 100 j. | bez push; ROI = yield; `capital_return` |
| Profil 4 Predictive Performance (`performance._bet_stats`) | push v obratu i v intervalu yieldu | bez push |
| Ranking Validation (`intelligence`) | bez push | beze změny |
| Profil 5 (`profil5.perf`) | bez neplatných | `metrics.is_staked` |
| CLI `profile compare` | z Excelu | beze změny |
Testy: stejné výsledky → stejný yield v `paper.stats`, `export.stats`, `history.bet_stats`, `performance._bet_stats`
a `metrics.unit_yield`; panel a Excel ukazují stejné ROI (test vykreslí panel a porovná s řádkem Souhrnu).
Uložený přepočet výkonnosti Profilu 4 v panelu se přepočítá při nejbližším vyhodnocení; do té doby panel ukazuje
starší uložené hodnoty (a starou definici).

## 7. CLV a plánování skenů
- Data (78 zápasů, `odds_observations`): stáří poslední aktualizace API-Football v bodě 75 min medián 238 min (čerstvé
  ≤ 60 min jen 6 %), 30 min 246 min (9 %), 10 min 180 min (23 %). Zastaralé závěrečné kurzy způsobuje poskytovatel,
  ne náš rozvrh – čerstvost ale roste směrem k výkopu.
- Bod **75 min nikdy nemůže dát závěrečný kurz** (platí jen pozorování ≤ 20 min před výkopem). Posun na 5 min
  (body 30 / 10 / 5, stále **3 požadavky na zápas**) byl implementován a otestován, ale **vrácen**: na „posledním
  bodu“ závisí logika NHL kurzů a ověření zahraniční kanceláří (kurzové API) a okno stahování; změna by vyžadovala
  přepsat 7 původních testů a přínos je zatím jen odhad z rostoucí čerstvosti. **Doporučení (čeká na souhlas):**
  body 30 / 10 / 5 se stejným počtem požadavků; bod 10 min jako pojistka. Body zůstávají 75 / 30 / 10.
- Pravidla true closing z fáze 2.1 beze změny (≤ 20 min pozorování, ≤ 60 min aktualizace u poskytovatele).
  Zastaralý kurz zůstává bez CLV (ne CLV 0) – test.
- Kurzové API (Profil 5, společná kontrola): stahuje se 1× denně kvůli kreditům; posun by vyžadoval víc požadavků
  nebo opožděnou ranní analýzu – **neměněno**. Důsledek: kontrola kurzu je u výběrů ze starých kurzů NEOVĚŘENO
  a Profil 5 nemá referenci (správně nic nevytváří).

## 8. Opportunity Ranking – diagnostika (`rankdiag.py`)
Váhy beze změny. Rozklad na kvalitu dat, ranking, EV a výsledek + „zbytek rankingu“ (ranking minus část vysvětlená
kvalitou dat). Závěr o přidané hodnotě až od **100 nezávislých zápasů**. Živá data (24. 11.): 2 474 výběrů,
21 zápasů – **MÁLO DAT**. Spearman: ranking × kvalita 0,66, × konz. EV 0,70, × EV 0,58, × zisk 0,11; kvalita
vysvětluje 45 % rozptylu rankingu. Excel „Ranking Diagnostics“, panel (Srovnání strategií), CLI `profile ranking`.

## 9. Rohy a karty
Profily zůstávají oddělené, metodiky „jen model“ a „25/75“ zvlášť. Metodika s méně než 30 zápasy se **nevyhodnocuje**:
panel ukáže „nevyhodnocuje se (n záp., min. 30)“ místo ROI, Excel u řádků metodik nechá yield prázdný. Nová metodika
má 1 tip.

## 10. Testy a nasazení
- `tests/test_phase322.py` – 21 testů (pojistka A, tvrdé odmítnutí Sledování i tipů, 5 výsledků AH + podíl výher,
  stín Profilu 3, přísné kombi + vsazené tikety, jednotný yield, panel = Excel, body CLV beze změny + zastaralý ≠ CLV,
  ranking ≥ 100 zápasů, rohy/karty, export).
- Upravená očekávání původních testů (změna chování na výslovný požadavek, kontroly zůstaly stejně přísné):
  `test_profil4_phase32::test_roi_yield_push_half_and_duplicates` a `…::test_nhl_push_saved_as_push…` (push mimo obrat),
  `test_marketsim::test_profile3_tracks_without_bot_model` (ve stejném zápase vybírá i stín 3x – test bere výběr
  Profilu 3), `test_export`/`test_profil4_phase32`
  (29 listů), `test_phase321` (noha bez dokladu ceny není ověřená).
- Nové tabulky: `integrity_rejections`. Záloha před nasazením: `backups/tipsbot.db.20261124-faze322.bak`.

## Co zůstává jen ve stínovém režimu
Profil 5 – Market Value, Profil 3 – nekruhový (3x), Rohy, Karty (papírové sledování bez vkladu), diagnostika rankingu.

## Co nebylo možné ověřit
- Pojistka Modelu A na živém novém zápase (ověřena na rekonstrukci Bodø a na 140 historických předpovědích).
- Zda by bod 5 min zvýšil podíl čerstvých závěrečných kurzů – nezměněno, jen doporučeno.
- Tvrdé odmítnutí na živých datech (zatím 0 případů; kurzy v cache byly staré → NEOVĚŘENO, ne PODEZŘELÉ).
- Výkonnost stínu Profilu 3 a Profilu 5 – zatím bez vyhodnocených výběrů. Žádný test není důkaz predikční výhody.
