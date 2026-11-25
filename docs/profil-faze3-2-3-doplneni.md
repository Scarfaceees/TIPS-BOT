# Fáze 3.2.3 – doplnění auditu: report × Excel, snapshot, návrhy k rozhodnutí

Stav k 24. 11. 2026, větev `dev`. Produkční logika, strategie, váhy, EV, prahy, body 75/30/10 a historická data
zůstaly beze změny. Žádné API dotazy ani kredity navíc. Nic nebylo nasazeno do Dockeru.

Všechny aktuální počty v tomto dokumentu jsou z jednoho snapshotu: **S20261124-195233-0cc626** (data k 24. 11. 2026
19:52 UTC). Ze stejného snapshotu je i Excel `tipsbot-vysledky-S20261124-195233-0cc626.xlsx` a výstup CLI
`tipsbot profile measure --snapshot S20261124-195233-0cc626`.

## 1. Proč se lišil report a Excel

Nejde o chybu výpočtu ani o jiné filtry. Mezi okamžikem, ze kterého je report, a okamžikem exportu Excelu proběhl
automatický sken. **Chybou reportu bylo, že neuvedl čas dat** a srovnával čísla ze dvou různých okamžiků.

| Čas (UTC) | Co se stalo | MI výběrů / se záznamem / OVĚŘENO / porovnatelných | P5 odmítnutých / skupin / důvodů |
|---|---|---|---|
| 18:11 | kopie databáze pro audit, ze které je report | 194 / 66 / 2 / 26 | 11 / 8 / 22 |
| 18:34 | záloha `backups/*.20261124-faze323.bak` (ověřeno, stejná čísla) | 194 / 66 / 2 / 26 | 11 / 8 / 22 |
| ~18:38 | CLI v kontejneru pro report (stejná čísla) | 194 / 66 / 2 / 26 | – |
| 18:44 | automat – Profil 5 odmítl novou nabídku | | 12 / 9 / 23 |
| 18:53:22 | automat – 24 nových výběrů | 218 / 90 / 24 / 34 | |
| 18:53 | export Excelu z panelu (po skenu) | 218 / 90 / 24 / 34 | 12 / 9 / 23 |
| 19:52 | snapshot S20261124-195233-0cc626 (žádný další sken) | 218 / 90 / 24 / 34 | 12 / 9 / 23 |

Přírůstky z automatických skenů:
- **Výběry (18:53:22):** všech 24 jsou nové zápasy, žádný starší výběr se nezměnil ani nezmizel.
  - podle profilu: Základ 10, Profil 2 10, Profil 3 2, Profil 3x 2;
  - podle soutěže: Konferenční liga 12, Evropská liga 11, Liga mistrů 1.
  - Všech 24 má záznam kontroly. 22 z nich je OVĚŘENO, protože měly čerstvé kurzy z kurzového API; 2 jsou NEOVĚŘENO.
- **Porovnatelné nabídky:** +8 (stejná nabídka u více profilů).
- **Profil 5 (18:44):** +1 odmítnutá nabídka. SK Brann – AGF, 1X2 „2“, unibet_nl @ 3,45, kurz o 13 % nad mediánem
  11 kanceláří. Jeden důvod, nová skupina.

Výpočet na kopii z 18:11 dává přesně čísla reportu, výpočet na snapshotu z 19:52 přesně čísla Excelu.
Filtry i rozsah profilů jsou v obou výstupech stejné.

## 2. Snapshot dat (implementováno na dev, nenasazeno)

Excel, panel a CLI mohou číst stejný snapshot, takže je sken mezi výstupy nerozhodí.
- **Co to je:** konzistentní kopie obou databází (panel + Profil 4) do `data/snapshots/<ID>/` přes SQLite backup.
  Zdroj se otevírá jen pro čtení. Když se databáze během kopírování změní (`PRAGMA data_version`), kopie se
  udělá znovu.
- **ID:** `S<čas UTC>-<6 znaků SHA-256 obsahu>`. Stejná data mají stejný otisk.
- **Ovládání:**
  - Panel: Export → „📸 Vytvořit snapshot“, u každého snapshotu odkaz na Excel a Audit měření.
  - CLI: `tipsbot profile snapshot create|list`, `tipsbot profile measure --snapshot ID`.
- **Označení ve výstupech:** každý výstup uvádí zdroj, buď „Snapshot ID (data k …)“, nebo „Živá databáze bez
  snapshotu – další sken může čísla změnit“. Excel ze snapshotu se jmenuje podle ID.
- **Úložiště:** snapshot vzniká jen na vyžádání, nic se automaticky nemaže. Jeden má zhruba 130 MB.

**K rozhodnutí:** jak dlouho snapshoty držet (návrh: posledních 5, starší mazat jen tlačítkem v panelu).

## 3. Aktuální počty (snapshot S20261124-195233-0cc626)

**Market Integrity**

| Ukazatel | Počet |
|---|---:|
| Výběrů | 218 |
| Záznam kontroly má | 90 (41 %) |
| Bez záznamu | 128 |
| OVĚŘENO | 24 (11 %) |
| NEOVĚŘENO | 66 |
| PODEZŘELÉ | 0 |
| Porovnatelných nabídek | 34 |
| Neshody | 0 |

**Profil 5**

| Ukazatel | Počet |
|---|---:|
| Unikátní kandidáti | 1 |
| Unikátní odmítnuté nabídky | 12 |
| Odmítnuté nabídky po skupinách kanceláří | 9 |
| Jednotlivých důvodů | 23 |
| Nabídek s více důvody | 9 |
| Bez reference (jen poslední sken každé soutěže) | 1 699 z 2 821 posouzených |

**CLV:** beze změny (žádné nové vyhodnocení). 289 − 66 − 6 − 1 = 216 vyhodnocených; CLV Performance 216 + 60 = 276.

**Profil 3**
- Výběrů s měřením nezávislosti: 19 (17 celých/čtvrtinových linií + 2 nové).
- Jen P3 po spuštění stínu 3x: 1 zápas; jen 3x: 1 zápas.

**Oprava diagnostiky (jen prezentace).** Dva nové výběry Profilu 3 mají kurz „zahraničí (medián)“: Ajax – Thun OU_2.5
a Salzburg – Ararat 1X2. Audit u nich ukazoval referenci kanceláří jako OK. Reference ale vzniká z týchž kanceláří,
jejichž medián je hodnocený kurz. Audit je teď značí jako **CIRCULAR_REFERENCE** a EV proti kancelářím
nevykazuje (test pokrývá i tento případ).

**Názorný příklad kruhovosti:** Salzburg – Ararat, 1X2 „1“ @ 1,17.
- Profil 3 (1X2 je ve fitu gólů): EV −2,0 %.
- Bez trhu 1X2: EV −37,6 %.
- Bez 1X2 zbývají hlavně gólové trhy, které nerozliší domácí a hosty. Nezávislý odhad tu není věrohodný ani v
  jedné verzi, proto návrh níže vyžaduje aspoň jeden trh, který rozděluje síly týmů.

## 4. Návrh: skutečně nezávislý Profil 3x (neimplementováno)

**Postup:**
1. **M** = skupiny kanceláří, které tvořily medián hodnoceného kurzu Profilu 3.
   - API-Football: kanceláře s úplným trhem v okamžiku výběru (u všech 53 výběrů souhlasí počet z popisku
     „N kanc.“ i medián kurzu).
   - Kurzové API: všechny kanceláře mediánu.
2. **Kandidáti reference:** kanceláře z API-Football (uložené `odds_quotes`) a z kurzového API (cache) mimo M.
   - Bez burz.
   - Kurzy staré nejvýš 6 h a stažené nejpozději v okamžiku výběru.
   - Skupiny se slučují podle aliasů (viz bod 5).
3. **Trh do fitu gólů** jen tehdy, když ho má úplný aspoň 3 skupiny mimo M. Postup: de-vig každé kanceláře →
   medián ve skupině → medián skupin.
4. **Podmínky výpočtu:**
   - aspoň 2 takové trhy;
   - celkem aspoň 3 nezávislé skupiny;
   - aspoň 1 trh, který rozděluje domácí a hosty (1X2, AH, TTH/TTA).

   Jinak se výsledek označí **INSUFFICIENT_BOOKMAKERS**: kandidát nevznikne a EV se nepočítá. Nikdy se jako náhrada
   nepoužije původní reference ani EV Profilu 3.
5. **Beze změny:** scénáře, váhy a pravidla výběru Profilu 3x. K výběru se uloží vynechané skupiny, použité skupiny,
   trhy a čas kurzů. Profil zůstává ve stínovém režimu.

**Na kolika výběrech by nezávislý výpočet byl možný** (uložená data, přísná varianta: bez Betfairu, každý trh aspoň
3 skupiny mimo M):

| Výběry | Celkem | Možné | INSUFFICIENT_BOOKMAKERS | Bez dat API-Football |
|---|---:|---:|---:|---:|
| Profil 3 – historické a současné z mediánu API-Football (19 vyhodnocených + 17 otevřených) | 36 | **33** | 3 | 0 |
| Profil 3 – nové z mediánu kurzového API (18:53) | 2 | 0 | – | 2 |
| Profil 3x | 19 | 17 | 0 | 2 |

- U typického výběru (medián 1xbet + marathonbet) zbývá mimo M 7 skupin: bet365, betano, betfair, betvictor,
  pinnacle, sbo, williamhill.
- **Neověřeno:**
  - podmínka „aspoň 1 trh dělící domácí a hosty“ zatím v propočtu není;
  - výsledné EV ani výběry nového 3x nejsou spočítané;
  - pro zápasy Evropské a Konferenční ligy API-Football uložené kurzy nemá.

## 5. Návrhy pro Profil 5 a společnou kontrolu kurzů (neimplementováno)

1. **Sloučení skupin podle shody cen** (kurzové API, uložené kurzy; kritérium ≥ 90 % shodných cen):

   | Dvojice | Shodná cena | Porovnání | Stav |
   |---|---:|---:|---|
   | sport888 – williamhill | 95 % | 293 | nový nález, navrhuji sloučit |
   | betsson – nordicbet | 92 % | 193 | navrhuji sloučit |
   | unibet_nl – unibet_se | 85 % | 117 | už sloučené |

   Dopad:
   - Reference Profilu 5 klesnou o 1–2 skupiny (17 → 15, 11 → 9, 8 → 7). Žádný kandidát ani odmítnutí nezmění stav.
   - Odmítnuté nabídky po skupinách: 9 → 8.
   - Odmítnutí betsson i nordicbet (Real Madrid – PSV) měla dnes v referenci sesterskou kancelář.
   - **Market Integrity:** 4 výběry dnes OVĚŘENO se 3 skupinami by klesly na 2 skupiny a staly se NEOVĚŘENO
     (Plzeň – Benfica OU_2.5: Základ a Profil 2; Ajax – Thun OU_2.5: Profil 3 a 3x).
   - Pro Profil 3x je potřeba alias i mezi zdroji: 1xbet (API-Football) = onexbet (kurzové API).
2. **Tvrdé odmítnutí „nad férovou cenou a nepotvrzen“ jen při ≥ 3 nezávislých skupinách.**
   Dopad: 0 případů. Všech 12 odmítnutí mělo 8–17 skupin a žádný výběr nemá EV nad 10 % při méně než 3 skupinách.
3. **Zastaralé kurzy bez platné reference nesmí vytvořit kandidáta.** Kód to už splňuje:
   - kanceláře s kurzem starším než 6 h se do reference nepočítají;
   - kandidát potřebuje kontrolu OVĚŘENO včetně čerstvosti (jediný kandidát má kurz starý 1 min).

   Návrh: výslovný regresní test. Dopad 0 případů.
4. **Stav „bez reference“ oddělit od odmítnutí kvůli ceně.** Dnes se nabídky bez reference neukládají, je jen
   počitadlo posledního skenu (1 699 z 2 821; Liga mistrů a NHL v posledním skenu 100 %, kvůli staré cache).

   Návrh: ukládat je jednou na nabídku jako stav BEZ REFERENCE s příčinou (zastaralá cache / málo kanceláří),
   odděleně od REJECTED. Do rozhodování to nezasahuje.

## 6. Seznam navržených změn k rozhodnutí

| # | Změna | Ovlivní výběr sázek? | Dopad dnes |
|---|---|---|---|
| A | Nezávislý Profil 3x (bod 4) | jen stínový 3x | 3x by měl výpočet u 17 z 19 výběrů; P3 33 z 36 |
| B | Aliasy skupin sport888/williamhill, betsson/nordicbet (+ 1xbet/onexbet pro 3x) | ano – MI stavy, P5 reference | 4 výběry OVĚŘENO → NEOVĚŘENO; P5 beze změny stavu |
| C | Odmítnutí podle férové ceny jen při ≥ 3 skupinách | ano – tvrdé odmítnutí | 0 případů |
| D | Test: zastaralý kurz bez reference nevytvoří kandidáta | ne | 0 |
| E | Profil 5: stav BEZ REFERENCE ukládat zvlášť | ne (jen evidence) | ~1 700 nabídek / sken |
| F | Retence snapshotů (posledních 5) | ne | – |
| G | Nasazení snapshotu a opravy diagnostiky do Dockeru | ne | – |

Podle tvého pokynu nepovoluji nic, co by ovlivnilo výběr sázek (B, C). Doporučuji nejdřív G a D, potom A a E ve
stínovém režimu. B a C až po tvém souhlasu.

## 7. Ověření snapshotu po neúspěšném nasazení (25. 11. 2026)

Nasazení c9da248 bylo vráceno na 2e04cbd. Kopie `profil4.db` v jednom kroku držela zámek pro čtení
(`journal_mode=delete`) a automat hlásil `database is locked`.

**Implementace:** SQLite Online Backup API (`sqlite3.Connection.backup` = `sqlite3_backup_init/step/finish`), ne
kopírování souboru.
- **Kopírování po krocích:** po 256 stránkách. Zámek pro čtení se drží jen po dobu kroku; zápis jiného spojení kopii
  restartuje.
- **Konzistence obou databází:** `PRAGMA data_version` obou zdrojů se kontroluje před kopírováním a po něm. Při
  změně se obě kopírují znovu.
- **Pokusy a limit:** pokusy se opakují až do limitu 180 s, pak `SnapshotBusy`.
- **Kopie nejdřív lokálně:** do lokální dočasné složky (v Dockeru `/tmp`), teprve potom se hotová kopie přesune do
  `data/snapshots` a ověří se její SHA-256.
- **Proč lokálně:** automat zapisuje do `profil4.db` každou minutu (`auto_tick_at`, změřeno 10 min: interval
  ~63 s). Kopie přímo do připojené složky trvá ~67 s, takže by se nikdy nedokončila.

**Testy** (`tests/test_snapshot.py`, 10): souběžné zápisy s invarianty (součet účtů, log bez mezer, rozdíl
pořadových čísel obou databází 0 nebo 1), shoda obsahu se zdrojem, vynucený zápis mezi kopiemi (2. pokus),
kontrolní test bez `data_version` (smíchaná kopie se odhalí), přesná logická kopie a nezměněný zdroj, timeout,
nepřetržité zápisy (Busy v limitu), přerušení, cizí `.tmp` složka po tvrdém zabití, opakování ve stejné sekundě,
úklid pracovní složky a poškozený přesun.

**Docker zátěžový test** (`scripts/snapshot_loadtest.py`, samostatný image `tipsbot-loadtest`, kopie produkčních
databází ze zálohy 25. 11., 175 MB + 45 MB):

| Varianta | Snapshotů | Busy | Doba | Konzistentní | Produkční tabulky = zdroj | Chyby zámku automatu | Max. čekání automatu |
|---|---:|---:|---|---:|---:|---:|---:|
| stará verze, zápis každé 3 s | 0 | 2 | 180 s | – | – | 0 | 0,79 s |
| stará verze, zápis každých 90 s | 0 | 2 | 357 s (limit 300) | – | – | 0 | 1,31 s |
| **nová verze, zápis každých 60 s** | **8** | **0** | **~59 s (2 pokusy)** | **8/8** | **8/8** | **0** | **3,43 s** |
| nová verze, nepřetržité zápisy, limit 45 s | 0 | 2 | 45,1 s | – | – | 0 | 0,62 s |

## 8. Ostré nasazení 11c37a7 – průběh kopírování snapshotu S20261125-102857-6aeeaa

**Oprava reportu z 25. 11.:** v reportu stálo „kopírování skončilo v 10:29:21“. To bylo chybně.
`copied_until` v `meta.json` je čas konce **celého** snapshotu, tedy včetně otisku, přesunu do `data/snapshots`
a jeho ověření. Kopírování přes SQLite Backup API skončilo dříve, nejpozději v 10:29:15,39. Zápis automatu
s časem 10:29:17 proto přišel až po skončení kopírování a závěr reportu platí, jen byl špatně doložen.

**Co snapshot zaznamenává a co ne:** `meta.json` ukládá `created`, `copied_until` a `attempts`. Časy začátku a
konce Backup API pro jednotlivé databáze ani hodnoty `PRAGMA data_version` se nezapisují do metadat ani do
logu, takže je zpětně doložit nejde. Průběh níže je odvozen z metadat, časů souborů snapshotu, logu panelu
a obsahu kopií.

**Pořadí kroků** (`snapshot.create`):
1. čas `created`;
2. otevření obou zdrojů jen pro čtení a `data_version` obou;
3. Backup API `tipsbot.db`, pak `profil4.db` do `/tmp` kontejneru;
4. znovu `data_version` obou a zavření spojení;
5. SHA-256 lokálních kopií;
6. kopie `tipsbot.db`, pak `profil4.db` do `data/snapshots/.tmp-*`;
7. kontrola SHA-256, čas `copied_until`, `meta.json`, přejmenování na ID.

**Časová osa (UTC):**

| Čas | Událost | Zdroj |
|---|---|---|
| 10:28:13 | poslední zápis automatu před snapshotem | `auto_tick_at` v kopii `profil4.db` |
| 10:28:57 | start snapshotu (krok 1) | `created` |
| 10:28:57 – nejpozději 10:29:15,39 | kroky 2–5: Backup API obou databází, `data_version`, otisk | odvozeno |
| do 10:29:15,39 | `tipsbot.db` zapsaná do `data/snapshots` (krok 6) | čas změny souboru |
| 10:29:15,39 – 10:29:18,79 | `profil4.db` zapsaná do `data/snapshots` (krok 6) | čas změny souboru |
| 10:29:17 | další zápis automatu, jen do `profil4.db` (`auto_tick_at`) | sledování v kontejneru |
| 10:29:21,37 | `meta.json`, odpověď panelu 302 (krok 7) | čas souboru, log panelu |

`auto_tick_at` je čas začátku ticku a commit přijde až po něm, tedy po 10:29:17. Oba kontejnery běží ve
stejném virtuálním stroji Docker Desktop se společnými hodinami.

**Odpovědi auditu:**

1. **Časy Backup API:** pro jednotlivé databáze nejsou zaznamenány. Obě kopie proběhly mezi 10:28:57 a
   10:29:15,39 (nejvýš ~18 s), nejdřív `tipsbot.db`, pak `profil4.db`.
2. **`data_version`:** absolutní hodnoty nejsou zaznamenány. Ani nemají samostatný význam, protože jde o
   čítač daného spojení. Zaznamenaný výsledek porovnání je `attempts = 1`: hodnoty před kopírováním a po něm
   byly u obou databází shodné.
3. **Restarty:**
   - Opakování celého pokusu: 0 (`attempts = 1`).
   - Vnitřní restart Backup API: 0. Nastal by jen při commitu jiného spojení do zdroje během kopírování. Takový
     commit by změnil `data_version` a vynutil 2. pokus.
   - Log restartu proto neexistuje, protože žádný restart nebyl.
4. **Zápis z 10:29:17:** není v **žádné** kopii.
   - Kopie `profil4.db` obsahuje `auto_tick_at = 10:28:13`.
   - `tipsbot.db` tento údaj neobsahuje a nejnovější časový údaj v její kopii je z 06:08:50.
   - Žádný časový údaj v kopiích není novější než 10:28:57 (kromě budoucích `expires_at` cache).
5. **Změna jedné databáze:** vede k novému kopírování **obou** databází. Smyčka v `create` vždy kopíruje obě;
   ověřuje to test `test_write_between_the_two_copies_forces_a_new_attempt`. U tohoto snapshotu k tomu
   nedošlo.
6. **Konzistence:** mezi dvěma čteními `data_version` (10:28:57 až nejpozději 10:29:15,39) nebyl do žádné
   z databází proveden commit. Obě kopie proto odpovídají jednomu okamžiku a žádná část nepochází z jiného
   okamžiku. Potvrzují to i obsah kopií (bod 4) a `integrity_check ok` obou kopií.

**Možné doplnění (není implementováno):** zapisovat do `meta.json` časy Backup API každé databáze, hodnoty
`data_version` a počet pokusů s důvodem. Další audit by pak šel doložit přímo z metadat.
