# Profil 4 – Fáze 3.2: Predictive Validation & Settlement Integrity

Stav k 22. 11. 2026. Výchozí stav: větev `dev`, HEAD `e97c729`. Navazuje na `5a0fbbc` a dva menší commity
automatu: tikety AKU a vyhodnocení fotbalu každých 15 minut.

**Fáze jen měří, nic neoptimalizuje.** Beze změny zůstaly:

- modely A–D a jejich váhy;
- Opportunity Ranking a jeho váhy;
- EV a férové kurzy;
- settlement (`mathcore.settle`, `nhl_settle.grade`);
- prahy APPROVED / WATCH / NO BET, minimální kurzy a limity rizika;
- API Budget Manager.

Nic se nesází, žádný historický záznam se nemaže ani nepřepisuje. Nové výpočty čtou jen uložená data a nedělají
žádné požadavky na API.

## 1. Audit původního stavu

| Oblast | Stav před 3.2 | Zjištění |
|---|---|---|
| Kalibrace A–D proti trhu | funguje (`intelligence.model_table`) | Společný vzorek a trh bez marže jsou správně, fotbal a NHL zvlášť. Chybělo: ECE, chronologické rozdělení trénink / validace / test, časová osa a diagnostika favorit / outsider. |
| Ranking Validation (3.1) | funguje částečně | **Míchal fotbal a NHL do jedněch pásem.** Chyběly výhry, prohry, push a půl výsledky, průměrný kurz, p modelu a p trhu, EV, analýza přidané hodnoty nad EV a korekce na víc srovnání. Sloupec „ROI“ byl ve skutečnosti zisk / vklad bez vrácených sázek, kdežto Úspěšnost modelů počítá ROI = zisk / 100 j. Definice sjednocuje 3.2, čísla 3.1 se kvůli zpětné kompatibilitě nemění. |
| CLV | funguje (`history._closing`) | Druhy kurzu (závěrečný, zahraniční závěrečný, poslední pozorovaný, zastaralý, chybějící, změna linie) se ukládají správně a CLV se počítá jen ze závěrečného. Chybělo dělení podle trhu, kanceláře, rankingu, rozhodnutí a modelu, medián, podíl kladných a vývoj v čase. |
| ROI / yield / bankroll | funguje (`history.bet_stats`, `export.stats`) | Deduplikace výběrů, vyřazení tipů po výkopu a oddělení jednotek od Kč fungují. Chyběl přehled podle modelu, ligy, trhu a rankingu a dělení skutečných tiketů na AKU / z tipu / ručně zadané. |
| Settlement – dohledatelnost | **chyběla** | U vyhodnocení se ukládal jen výsledek, skóre a zisk, ne původní data z API, zdroj, metoda a verze. U NHL se neukládá skóre po 60 minutách, takže trhy na 60 minut nejdou zpětně ověřit. |
| Nezávislá kontrola settlementu | jen fotbal a jen v Analýze chyb | `integrity.outcome` je vlastní implementace (3.1). Pro NHL nezávislý výpočet neexistoval a záznam neshod také ne. |
| Statistická nejistota | částečně | Intervaly z bootstrapu po zápasech fungují, ale model_table je ukazoval i pro 14 zápasů NHL, což vypadá falešně přesně. Ve 3.2 se pod 30 zápasů intervaly nezobrazují. |
| Data pro out-of-sample | omezená | Ukládá se čas předpovědi, verze modelu, hash konfigurace, kurz s časem a seznam zdrojů, ale **ne snímek vstupních dat**. Historické vstupy proto nelze plně zrekonstruovat a předpovědi přepočítat. |

### Nalezená chyba – jen zdokumentována, NEOPRAVENA (settlement bez souhlasu neměním)

**NHL: vrácení vkladu se ukládá jako „neplatná sázka“.**

- **Příčina:** `nhl_settle.grade` vrací u součtu přesně na celé lince (např. over/under 6,0 při 3:3) hodnotu `void`.
  V hlavním botu `void` znamená vrácení, Profil 4 ale `void` chápe jako neplatnou sázku a vrácení značí `push`.
- **Dopad:** zisk je stejný (0). Takový tip se ale nepočítá do obratu ani do počtu sázek (yield nepatrně vyšší)
  a v Excelu je jako „neplatné“ místo „vráceno“.
- **Výskyt:** v živých datech zatím **0 případů**. Linky NHL tipů jsou dosud jen půlové.
- **Zachycení:** nový nezávislý výpočet to hlásí jako INTEGRITY MISMATCH s vysvětlením (test
  `test_known_finding_nhl_push_stored_as_void_is_flagged`).
- **Navržená oprava (čeká na souhlas):** v `profil4/nhl.settle` převést `void` z `nhl_settle.grade` na `push`, a to
  jen pro Profil 4. Hlavní bot má vlastní konvenci a nemění se.

## 2. Architektura nových částí

- `profil4/performance.py` – Predictive Performance (jen čtení):
  - `ranking_performance`: pásma po sportech, přidaná hodnota nad EV, rozdíl ≥ 60 / < 60;
  - `model_comparison`: A–D a trh, ECE, chronologické rozdělení, časová osa a únik informací, favorit / outsider,
    rozdíl model – trh, kurzová pásma, ligy a trhy;
  - `clv_performance`, `finance`, `alerts`, `calibration_drift`, `sufficiency`;
  - `store_performance`: kv `web_p4_perf`, denní historický audit a týdenní snímek `archive` (`p4_perf_week`).
- `profil4/settleaudit.py` – Settlement Integrity Monitor:
  - `football_check` = `integrity.outcome` + vlastní převod na výsledek + vlastní výpočet zisku;
  - `nhl_check` = vlastní pravidla NHL;
  - `record` při vyhodnocení, `audit_history` (historický audit), `summary`.
- Napojení beze změny výsledku: `history.settle_tip(..., evidence=None)` po zápisu výsledku zavolá
  `settleaudit.safe_record`. Chyba auditu vyhodnocení nikdy nezastaví (test). `history.settle` předává surová data
  z API-Football a u zrušených zápasů ukládá i stav. `profil4/nhl.settle` předává surová data z NHL API včetně skóre
  po 60 minutách a způsobu konce.
- Monitoring: `closing.tick` → `refresh_performance` po každém vyhodnocení, `perf_daily` jednou denně i ve dnech bez
  zápasů. Upozornění se jen zobrazují a nic nemění.

## 3. Definice metrik

| Metrika | Definice |
|---|---|
| Nezávislý zápas | jednotka statistiky; všechny výběry jednoho zápasu tvoří jeden shluk |
| Výběr | (zápas, trh, výběr); poslední předzápasový záznam |
| Obrat | počet vyhodnocených sázek po 1 j. (výhra, půl výhra, vrácení, půl prohra, prohra); neplatné sázky (void) se nepočítají |
| Čistý zisk | výhra = kurz − 1; půl výhra = (kurz − 1) / 2; vrácení = 0; půl prohra = −0,5; prohra = −1 |
| Yield | čistý zisk / obrat |
| ROI | čistý zisk / výchozí virtuální bankroll 100 j. (stejně jako Úspěšnost modelů); u skutečných tiketů zisk Kč / vklady Kč |
| Win rate | (výhry + půl výhry) / sázky bez vrácení |
| Brier | průměr (p − y)², y = 1 výhra / půl výhra, 0 prohra / půl prohra; vrácení se nehodnotí |
| Log loss | −průměr [y·ln p + (1 − y)·ln(1 − p)] |
| ECE | Σ (n_pásma / N)·\|odhad − skutečnost\| přes kalibrační pásma (sloučená pod 15 zápasů); jen od 30 zápasů |
| Tržní pravděpodobnost | de-vig z doby předpovědi stávající logikou (`p_market`, multiplikativní) |
| CLV | kurz / férový závěrečný kurz − 1 (bez marže, pokud má trh obě strany), jinak kurz / závěrečný kurz − 1; jen skutečný závěrečný kurz |
| Kombinovaný tiket | Kč; vrácená noha se vyjme z kurzu, prohraná noha znamená prohraný tiket (hlavní databáze) |

## 4. Statistická metodika

- **Bootstrap po zápasech** (`history.cluster_ci`, `performance._boot`): převzorkovávají se celé zápasy, takže korelace
  výběrů jednoho utkání zůstává. Test ověřuje, že desetinásobné zopakování výběrů téhož zápasu interval nezúží.
- Pod 30 zápasy se interval nezobrazuje. O průkaznosti se rozhoduje až od 100 nezávislých zápasů.
- **Úrovně dat:** pod 100 MÁLO DAT, 100–199 PŘEDBĚŽNÁ, 200–499 ROZŠÍŘENÁ, od 500 SILNĚJŠÍ ZÁKLAD. U každé úrovně je
  textové zdůvodnění: zápasy, výběry, období a omezení. Samotná úroveň neznamená významnost.
- **Více srovnání:** yield pásem má navíc interval s Bonferroniho korekcí na 4 pásma (98,75 %). Závěr o pásmu se
  bere z korigovaného intervalu. Nejlepší historické pásmo se nevydává za nejlepší strategii.
- **Přidaná hodnota rankingu:**
  - Spearmanova korelace s EV, konzervativním EV, robustností, kvalitou dat, tržní pravděpodobností a rozhodnutím;
    při |ρ| > 0,9 se hlásí „kopíruje“.
  - Stratifikovaný test: rozdíl yieldu nad a pod mediánem rankingu uvnitř tercilů konzervativního EV.
  - Reziduální test: y − p_trh nad a pod mediánem rankingu.
  - Oba testy mají interval z bootstrapu po zápasech.
- Fotbal a NHL se vždy počítají zvlášť a trhy se dělí podle typu.

## 5. Ochrana před únikem informací

- Předpověď, tip a ranking se berou jen tehdy, když vznikly **před výkopem**. Ostatní se vyřadí a spočítají.
- Kurz vstupu musí být z doby před výkopem. Kontrola `odds_after_kickoff` při nule hlásí „OK“, jinak se zobrazí
  upozornění.
- Výsledek musí být až po výkopu (`settled_before_kickoff`).
- Závěrečné kurzy se používají jen k CLV, nikdy jako vstup.
- Chronologické rozdělení podle výkopu zápasu je 60 / 20 / 20 %. Zápas nikdy nepatří do dvou částí a test je vždy
  nejnovější.
- **Out-of-sample:** modely se neučí z uložených předpovědí, takže každá uložená předpověď je mimo vzorek modelu.
  Rozdělení slouží k posouzení stability a k případnému ručnímu ladění, které by se směl dívat jen na trénovací část.
- Snímek vstupních dat se neukládá, proto historické vstupy nelze zrekonstruovat. Nevytváří se žádný „přepočítaný“
  out-of-sample výsledek.

## 6. Settlement Integrity – metodika

- **Záznam při vyhodnocení** (`settlement_audit`, jeden řádek na tip, `INSERT OR IGNORE`, takže se nikdy nepřepíše):
  - zápas, tip, sport, trh, výběr, linie, kurz;
  - surová data výsledku (JSON);
  - normalizované skóre;
  - čas získání, zdroj, produkční metoda a verze;
  - výsledek, zisk, jednotky, čas vyhodnocení;
  - nezávislý výsledek a zisk, verze kontroly, stav (SHODA / INTEGRITY MISMATCH / NEOVĚŘITELNÉ) a důvod.
- **Nezávislý výpočet** nevolá `mathcore.settle`, `mathcore.unit_profit` ani `nhl_settle.grade`:
  - Fotbal: rozklad vkladu na sousední linie u čtvrtinových linií (`integrity.outcome`), vlastní převod na
    výsledek a vlastní zisk.
  - NHL: vítěz z konečného skóre (včetně vítězného gólu nájezdů), 60 minut ze skóre po 3. třetině, prodloužení
    podle způsobu konce zápasu, handicapy a součtové linie včetně čtvrtin.
  - Vlastní referenční tabulky: 15 případů fotbalu a 14 případů NHL.
- **Neshoda:** stav INTEGRITY MISMATCH s důvodem. Produkční výsledek zůstává a nevydává se za ověřený. Panel, CLI
  a automat hlásí chybu.
- **Historický audit** (`audit_history`, tlačítko v panelu, `--audit-history`, automat jednou denně) znovu ověří
  všechny uložené tipy a předpovědi z uloženého skóre. U nových tipů použije uložená surová data.
  - Tipy ani předpovědi nemění, zapíše jen souhrn do `settlement_audit_runs`.
  - Neověřitelné jsou: neplatné sázky bez skóre a trhy NHL na 60 minut bez uloženého skóre po 60 minutách.

## 7. Změněné soubory

- nové: `src/tipsbot/profil4/performance.py`, `src/tipsbot/profil4/settleaudit.py`,
  `tests/test_profil4_phase32.py`, tento dokument
- `src/tipsbot/profil4/store.py` – tabulky `settlement_audit`, `settlement_audit_runs`
- `src/tipsbot/profil4/history.py` – `settle_tip(evidence=)` a auditní záznam (výsledek beze změny);
  `cluster_ci(level=)` (výchozí 95 % jako dřív)
- `src/tipsbot/profil4/nhl.py` – předání surových dat NHL API do auditu
- `src/tipsbot/profil4/closing.py` – `refresh_performance`, `perf_daily`
- `src/tipsbot/profil4/cli.py` – sekce `performance`, `ranking-performance`, `model-comparison`, `clv`,
  `settlement-integrity`, přepínač `--audit-history`
- `src/tipsbot/web.py` – sekce Predictive Performance (A–E), tlačítko Audit settlementu, `/profil4/settlement-audit`
- `src/tipsbot/export.py` – 5 nových listů a `finance_rows` (Kč a jednotky ve zvláštních sloupcích); export čte
  databázi Profilu 4 jen pro čtení
- `tests/test_export.py` – rozšířený seznam listů (15 → 20)

## 8. Databáze a migrace

- Nové tabulky `settlement_audit` a `settlement_audit_runs` (`CREATE TABLE IF NOT EXISTS`). Jde čistě o přidání,
  migrace je opakovatelná a existující tabulky ani data se nemění. Testy ověřují starou databázi bez tabulek
  i opakované otevření.
- Před nasazením se obě databáze zálohovaly přes sqlite backup API do `backups/` (viz závěrečná zpráva).
- Tipy vyhodnocené před nasazením nemají záznam z doby vyhodnocení. Nic se zpětně nedoplňuje ani nevymýšlí,
  ověřují se jen historickým auditem.

## 9. CLI, panel, Excel

- `tipsbot profil4 intel performance | ranking-performance | model-comparison | clv | settlement-integrity
  [--audit-history] [--out report.json]`
- Panel → Profil 4 → **Predictive Performance**: A. Model Comparison, B. Opportunity Ranking Performance, C. CLV
  Monitor, D. Settlement Integrity, E. Data Sufficiency, finance a metodika. Vše jsou karty a rozbalovací části bez
  tabulek; každý stav má textové zdůvodnění.
- Excel: původních 15 listů zůstává. Nové listy jsou Predictive Performance, Ranking Performance, Model Comparison,
  CLV Performance a Settlement Integrity. Chybějící hodnota je prázdná buňka s vysvětlením, nikdy 0.

## 10. Známá omezení

- Dat je zatím velmi málo. Fotbal má společný vzorek A–D a trhu 33 zápasů, NHL 14. Vyhodnocený ranking existuje jen
  pro 2 fotbalové zápasy a 0 NHL. Všechny závěry jsou **MÁLO DAT**.
- Platný závěrečný kurz má jen 18 ze 122 vyhodnocených tipů (15 %). 100 tipů má zastaralý kurz, protože API-Football
  kurz před výkopem neobnovilo. CLV je zatím jen orientační.
- Snímek vstupních dat se neukládá, viz bod 5.
- Settlement hlavního bota (Profily 1–3 a tikety) Settlement Integrity Monitor nepokrývá, jde jen o Profil 4.
  Hlavní bot byl ručně ověřen proti externím zdrojům 22. 11. 2026 (231 sázek, 0 chyb).

## 11. Ověřeno na živých datech a ověřeno simulací

**Na živých datech** (Docker, 22. 11. 2026 16:09 UTC):

- **Migrace:** přidané tabulky na živé databázi, 271 tipů beze změny. Zálohy:
  `backups/profil4.db.20261122-faze32.bak` a `backups/tipsbot.db.20261122-faze32.bak` (integrity check ok).
- **Historický audit settlementu:** zkontrolováno 17 079 vyhodnocení, z toho 17 077 shoda, **0 neshod**
  a 2 neověřitelné (neplatné sázky bez skóre).
  - tipy fotbal 127, tipy NHL 38;
  - předpovědi fotbal 16 524, předpovědi NHL 390.
- **Model Comparison:**
  - Fotbal: 34 zápasů / 3 909 výběrů, log loss C 0,4589, trh 0,4636. Rozdíl −0,0046 je MÁLO DAT a neprůkazný.
  - NHL: 14 zápasů / 96 výběrů, MÁLO DAT.
- **Ranking Performance:** fotbal 3 vyhodnocené zápasy (353 výběrů), NHL 0, vše MÁLO DAT.
- **CLV:** platný závěrečný kurz má 21 ze 125 vyhodnocených tipů (17 %). Fotbal 14 tipů, průměr +1,7 %; NHL 7 tipů,
  průměr +7,9 %. Vše MÁLO DAT.
- **Ostatní kontroly:**
  - CLI `performance --out`, `model-comparison` a `settlement-integrity --audit-history` v kontejneru: OK.
  - Panel na šířce 375 px: žádný vodorovný posun (scrollWidth 375), sekce A–E jako karty, konzole bez chyb.
  - Excel stažený z panelu (`/export/xlsx`) má 20 listů a list Settlement Integrity přepočítává audit při exportu.
- **Jen simulací (testy):** záznam auditu při skutečném vyhodnocení (první ostré vyhodnocení proběhne po nasazení),
  neshody settlementu, upozornění na prudkou změnu CLV a zhoršení kalibrace a pásma rankingu s více než 100 zápasy.
