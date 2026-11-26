# Phase 3.5.2 – Production Readiness Audit M1 + M2 (NHL měření)

Stav k 26. 11. 2026.

- **Produkce:** image `30c74eb85cb5` = commit `e7e2a85`.
- **Audit:** `dev` = `c56e91d` (M1 `6034048`, M2 `982780a`, M3 `c56e91d`).
- **Nic se nenasadilo**, produkční DB ani kontejnery se neměnily. Produkční `profil4.db` se jen jednou četla (`mode=ro`, `query_only`). Všechny ostatní zkoušky běžely na **kopiích ověřené zálohy** `backups/predB1-20261126-1715/` a na kopiích souborů MoneyPuck.
- **Síť** byla ve všech skriptech zablokovaná a pokusy se počítaly.
- **Skripty** pro zopakování jsou v `scripts/audit_352/`, viz tabulka v příloze.

**Rozdíl kódu produkce → audit:** `git diff --stat e7e2a85 HEAD -- src` mění jen `profil4/cli.py` (+11), `profil4/nhl.py` (+9/−1), `profil4/store.py` (+37) a přidává `profil4/nhl_measure.py` a `profil4/nhl_eval.py`. Mezi `e7e2a85` a `d1d3012` se `src/` nemění.

---

## Výsledek po oblastech

| # | Oblast | Výsledek |
|---|---|---|
| 1 | Schéma produkce × M1/M2 | **PASS** |
| 2 | Migrace, idempotence, souběh, restart | **PASS** |
| 3 | `TIPSBOT_CODE_REV` = běžící commit | **FAIL** |
| 4 | Cesty skenu a vyhodnocení | **PASS** (s nálezem P1-1 o kvalitě dat) |
| 5 | Chyba měření nezmění ani nezastaví rozhodování | **PASS** |
| 6 | Vazby zápas → snímek → trh → výsledek, opravy | **PASS** |
| 7 | Výkon, růst DB, dopad na provoz | **PASS** (P2) |
| 8 | Zálohy, obnova, rollback | **PASS** |
| 9 | Žádné nové API požadavky | **PASS** |
| 10 | Postup nasazení a kontroly | **připraven** (kap. 10), platí až po opravách |
| — | Měření na skutečných datech dává srovnatelné vzorky | **FAIL** (P1-1) |

**Doporučení: NO-GO.** Nasazení by rozhodování neohrozilo (oblasti 2, 5, 8 a 9 a regrese jsou v pořádku). Měření by ale v produkci nebylo použitelné:
- všechny snímky by vyšly jako nesrovnatelné (P1-1);
- všechny záznamy by měly verzi kódu `unknown` (P1-2).

Po opravě F1–F3 a krátkém opakování kontrol (kap. 11) doporučuji **GO** k samostatnému schválení deploye.

---

## 1. Schéma – PASS

**Příkaz:** `python scripts/audit_352/pra_schema.py data/profil4.db` (produkce jen pro čtení; otisk souboru před i po stejný `fecee88f95f0f179`).

- Produkce: 15 tabulek, `journal_mode = delete`.
- M1/M2 přidá **jen** tabulky `nhl_snapshots`, `nhl_measurements`, `nhl_results`, `nhl_result_corrections` a indexy `nhl_snapshots_fixture`, `nhl_snapshots_scan` (UNIQUE), `nhl_measurements_snapshot`, `nhl_measurements_fixture`.
- Společné tabulky: **žádný rozdíl** (sloupce, typy).
- `LATER_COLUMNS` už v produkci existují, takže se nespustí žádný `ALTER TABLE`.
- Jednorázová oprava `fix_ah_away_sign_v1` už proběhla (je v `kv`) a znovu se nespustí.

## 2. Migrace, idempotence, souběh, restart – PASS

**Příkaz:** `python scripts/audit_352/pra_migrate.py backups/predB1-20261126-1715/profil4.db`

- **Migrace kopie produkce:** 0,51 s. Data i schéma 15 původních tabulek (270 162 řádků) jsou podle SHA-1 všech řádků **shodná**.
- **Idempotence:** dva další starty nic nezmění.
- **Souběh:** 8 **procesů** současně otevře `Store` nad kopií produkce před migrací, 10 kol → **0 selhání**, vždy právě 4 nové tabulky a data beze změny.
- **Zamčená DB** (cizí zápis drží zámek): migrace počká a dokončí se po uvolnění. Ověřeno pro zámek 5 s (5,5 s) i 30 s (30,3 s). Při zámku delším než 60 s (`BUSY_TIMEOUT_S`) by otevření `Store` selhalo. Tak se `Store` chová i dnes, tabulky se pak vytvoří při dalším otevření.
- **Restart kontejnerů:** každý start a každé otevření `Store` spouští `CREATE TABLE IF NOT EXISTS`, které je po první migraci bez účinku (idempotence výše).
- **Známá chyba mimo rozsah:** souběh na **úplně nové** DB (`duplicate column name` v `LATER_COLUMNS`) se produkce netýká, protože sloupce už má. Podle zadání neopraveno.

## 3. `TIPSBOT_CODE_REV` – FAIL (P1-2)

**Příkaz:** `docker exec tipsbot sh -c 'printenv TIPSBOT_CODE_REV || echo "(nenastaveno)"'` → `(nenastaveno)`.

- Image neobsahuje `.git` (`.dockerignore`) a `Dockerfile` ani `docker-compose.yml` proměnnou nenastavují.
- Všechny snímky by proto měly `code_rev = unknown` a M3 by celý vzorek vykazoval jako „verze kódu neznámá“.

## 4. Cesty skenu a vyhodnocení – PASS (nález P1-1)

**Volání** (`grep` v `src/`):

| Kde | Co | Měření |
|---|---|---|
| `auto.run_once` → `profil4.nhl.run_and_store` → `nhl.run` | sken NHL Profilu 4, jediná cesta | `measure_scan` po `analyse` + `history.record` |
| `profil4.closing.tick` (vlákno automatu) → `nhl.settle` | vyhodnocení každých ~15 min | `measure_results` |
| `web` `/profil4/settle` (panel) → `nhl.settle` | ruční vyhodnocení | `measure_results` |

- `nhl.run` neběží uvnitř `store.batch()`. `history.record` má vlastní dávku, která je před měřením uzavřená. `record` měření má vlastní dávku, takže snímek a řádky se zapíšou buď celé, nebo vůbec (test `test_failed_record_leaves_nothing_half_written`).
- Výjimku z `run_and_store` by navíc zachytil automat (`auto.py:139`).

**P1-1 – skutečná data:**
- **Příkaz:** `python scripts/audit_352/pra_perf.py backups/predB1-20261126-1715 data/nhl` – skutečná cache NHL (15 zápasů) a skutečné soubory MoneyPuck.
- **Výsledek:** **0 z 15 snímků srovnatelných**, důvod „kurz … má čas poskytovatele po čase stažení – kurzy nejsou z jednoho okamžiku“.
- **Příčina (ověřeno):**
  - `odds_cache:NHL.t` = 06:00:37 je `now` **začátku průchodu automatu** (`auto.run_once` → `scan_league(now=now)`), ne čas stažení;
  - kurzy NHL se stáhly až po ostatních ligách. Všech **469 z 469** trhů má `last_update` po 06:00:37, nejnovější 06:11:34 (o 656 s později).
- Kontrola v M1 je správná. Jako čas stažení ale dostává začátek průchodu.

**P2-1 – chyba v M1:** v `nhl_measure.build` cyklus sběru časů kanceláří po prvním takovém kurzu skončí (`break`). Časy ostatních kanceláří včetně Pinnacle pak chybí. Ve variantě s Pinnacle to dalo 50× „S3: neznámý čas poskytovatele ostré linie“.

## 5. Izolace chyb – PASS

**Testy** (`tests/test_nhl_measure_m2.py`, izolované DB):
- selhání zápisu (`database is locked`), výpočtu (`ZeroDivisionError`) i ukládání výsledků → varování v logu, rozhodování shodné;
- s měřením i bez něj na třech scénářích: tipy, predikce, EV, robustnost i pořadí jsou shodné;
- globální generátor náhodných čísel se nemění.

**Skutečná data** (`pra_perf.py`): rozhodnutí i kandidáti s měřením a bez něj jsou shodní pro skutečnou cache i pro variantu s Pinnacle.

**Regrese proti produkční verzi `e7e2a85`** (worktree, stejná data):
- `scripts/audit_352/m1_golden.py`: 4 syntetické scénáře včetně vyhodnocení, 179 216 B JSON, **bitově shodné**;
- `scripts/audit_352/pra_realgolden.py`: skutečná cache a MoneyPuck nad kopií produkční `profil4.db`. 108 kandidátů Profilu 4, 422 predikcí, 15 tipů a 50 kandidátů hlavního bota (varianta s Pinnacle) jsou **bitově shodné**.

**Log:** varování měření jdou bez nastavení logování přes výchozí výstup Pythonu na stderr, takže jsou v `docker logs` (ověřeno lokálně). Zobrazí se jen text bez jména loggeru.

**Zbývající riziko (P2-4):** když je DB zamčená, zápis měření čeká až 60 s na každý zápas a zdrží zbytek skenu. Rozhodnutí zápasu je v tu chvíli už uložené. Stejné čekání má dnes i `history.record`.

## 6. Vazby a opravy výsledků – PASS

- **Testy M2:** vazba snímek → řádky → výsledek, žádné duplicity při opakovaném skenu, výsledek jen pro správný zápas (id NHL API + týmy + začátek ± 6 h), konzistence REG/OT/SO, konflikt nic nepřepíše, výslovná oprava s důvodem.
- **Výkaz M3** (`nhl_eval.integrity`) počítá řádky bez snímku, řádky s jiným zápasem, výsledky bez snímku a duplicity (testy M3).
- **P2-5:** opravu výsledku lze provést jen voláním `nhl_measure.correct_result` v kontejneru (Python). Panel ani CLI na to nemají.

## 7. Výkon a růst DB – PASS (P2-2)

Kopie produkční `profil4.db` (199 MB), skutečná data, 20 skenů s měřením proti 20 bez něj:

| Měřítko | Hodnota |
|---|---|
| Růst DB způsobený měřením | **≈ 5,5 KB na snímek** (1 604 KiB / 300 snímků) |
| Odhad za sezónu | 1 312 zápasů × ~8 snímků ≈ **55 MiB** (≈ +27 % dnešní `profil4.db`) |
| Čas skenu 15 zápasů | +0,2 až +1,3 s (medián 5,72 proti 4,45 s; jednotlivé běhy kolísají) |
| `record_results` nad 12 300 snímky | 0,016 s |

- Největší sloupce: `exclusion` (~241 B na řádek trhu), `ref_p4_books` a `mkt_p4_books` (seznamy kanceláří).
- Počet snímků na zápas v provozu (nový snímek jen při novém stažení kurzů) je **NEOVĚŘENÝ**, odhad 8.
- Dřívější hrubý údaj 20 KB na snímek zahrnoval i existující zápisy skenu (kurzy, predikce), proto se nepoužívá.

## 8. Zálohy, obnova, rollback – PASS

- **Zálohy a obnova:** `scripts/b1_preflight.py backup/verify` funguje pro obě DB. Dnes 17:15 `VERIFY OK`, `profil4.db` 15 tabulek / 270 162 řádků, obnova shodná.
- **Rollback na `e7e2a85`** (`scripts/audit_352/pra_rollback.py`, worktree `e7e2a85`):
  - nový kód nejdřív zmigroval kopii a zapsal měření i výsledek;
  - starý kód nad ní nastartoval `Store`, proskenoval 15 zápasů a vyhodnotil 15 zpráv **bez chyby**;
  - tabulky měření zůstaly **beze změny** (počty i max id).
- Nové tabulky po rollbacku zůstanou a starý kód je ignoruje. Obnova `profil4.db` ze zálohy je odstraní.

## 9. API požadavky – PASS

- `nhl_measure.py` ani `nhl_eval.py` neimportují `requests` ani `socket` (`grep`).
- **Skutečný sken a vyhodnocení s měřením** (`pra_perf.py`, `pra_realgolden.py`): **0 síťových pokusů** (zablokovaný a počítaný `socket.connect` i `getaddrinfo`).
- Vyhodnocení s měřením i bez něj volá stažení výsledků pro stejné dny a stejněkrát (test `test_settle_measurement_adds_no_requests`).
- Celé sady testů M1, M2 a M3 prošly vždy 2× s pojistkami `_no_network` a `_no_paid_api`.

---

## Problémy

| ID | Závažnost | Problém | Dopad |
|---|---|---|---|
| P1-1 | **P1** | `odds_fetched_at` = začátek průchodu automatu, ne čas stažení NHL | všechny skutečné snímky nesrovnatelné, měření bezcenné; rozhodování neohrožuje |
| P1-2 | **P1** | `TIPSBOT_CODE_REV` se nenastavuje | všechny záznamy `unknown`, M3 je vykáže jako neznámou verzi |
| P2-1 | P2 | `break` v M1 ukončí sběr časů kanceláří | chybějící čas ostré linie (S3) |
| P2-2 | P2 | růst ≈ 55 MiB za sezónu | větší zálohy a snímky DB; dlouhé texty v `exclusion` |
| P2-3 | P2 | S5 (MoneyPuck ≤ 3 dny) je na hraně: soubor z 26. 11. končí 24. 11. a zápasy po UTC půlnoci jsou o den dál | časné snímky vyřazeny; dopad na poslední snímek před zápasem NEOVĚŘEN |
| P2-4 | P2 | zamčená DB → čekání až 60 s na zápas | zdržení skenu, rozhodnutí už uložená |
| P2-5 | P2 | oprava výsledku jen přes Python v kontejneru | provozní pohodlí |
| P3 | P3 | souběh migrace na úplně nové DB (existující chyba) | produkce se netýká |

**P0: žádný.**

## Nutné opravy před nasazením (ke schválení – NEIMPLEMENTOVÁNO)

- **F1 (P1-1) – skutečný čas stažení z evidence B1:**
  - `nhl.run` předá `nhl_measure` čas skutečného požadavku na kurzy NHL z `odds_api_calls` (`purpose = refresh_league`, `league = NHL`, první `ts ≥ odds_cache.t` a ≤ t + 3 h, přes `ledger.odds_api_calls(since)`). Jde jen o čtení, rozhodování se nemění;
  - snímek uloží oba časy: `odds_fetched_at` = skutečný požadavek a nový sloupec `odds_cache_t` = začátek průchodu;
  - když evidence chybí (cache z doby před B1, selhaná evidence), snímek se označí „přesný čas stažení neznámý“ jako **nesrovnatelný**. Žádná náhradní hodnota.
  - Testy: skutečný průběh (cache t 06:00:37, kurzy do 06:11:34, evidence 06:11:40) → srovnatelné; bez evidence → nesrovnatelné.
- **F2 (P1-2) – verze kódu v image:**
  - `Dockerfile`: `ARG TIPSBOT_CODE_REV=unknown` + `ENV TIPSBOT_CODE_REV=$TIPSBOT_CODE_REV`;
  - `docker-compose.yml`: `build.args.TIPSBOT_CODE_REV: ${TIPSBOT_CODE_REV:-unknown}` u obou služeb;
  - nasazení: build jen z čistého stromu (`git status --porcelain -- src Dockerfile docker-compose.yml pyproject.toml` prázdné) s `TIPSBOT_CODE_REV=$(git rev-parse --short HEAD)`;
  - kontrola po nasazení: `docker exec tipsbot printenv TIPSBOT_CODE_REV` = HEAD v obou kontejnerech.
- **F3 (P2-1):** zrušit `break`, sbírat časy všech kanceláří a hlášení o kurzu po čase stažení zapsat jednou se seznamem. Test: časy Pinnacle se zapíšou i při jiném chybném kurzu.
- **Doporučené, ne nutné:**
  - **F4 (P2-2):** `exclusion` ukládat jako krátké kódy (`S2`, `S3`, `MIX`…) s číselníkem v kódu. Odhad −40 % objemu;
  - P2-3 sledovat po nasazení v M3 (`vyřazeno`: „data MoneyPuck končí“).

## 10. Postup nasazení (až po F1–F3 a samostatném schválení)

1. **Okno a výchozí stav:** stejné jako B1 (mimo 00/06/12/18 UTC ± 20 min, žádný výkop do 90 min, `auto:running` prázdné). Dále `docker inspect` obou kontejnerů a `git rev-parse HEAD` musí odpovídat schválenému commitu na `dev`.
2. **Rollback image:** `docker tag tipsbot-local:latest tipsbot-local:rollback-e7e2a85` (= `30c74eb85cb5`).
3. **Zálohy:**
   - `docker exec -i tipsbot python - backup /tmp/predM2 < scripts/b1_preflight.py`;
   - `docker cp` do `backups/predM2-…`, pak `docker exec tipsbot rm -rf /tmp/predM2`;
   - `python scripts/b1_preflight.py verify backups/predM2-…` → `VERIFY OK`, jinak **stop**.
4. **Build** z čistého stromu s `TIPSBOT_CODE_REV` (F2). Potom `docker tag tipsbot-local:latest tipsbot-local:<commit>`.
5. **Postupný restart:** `docker compose up -d --no-build tipsbot` (HTTP 200), potom `tipsbot-auto`. Migraci provede první otevření `Store` (≈ 0,5 s).
6. **Kontroly hned po nasazení:**
   - oba kontejnery mají stejné nové image ID, `RestartCount 0`;
   - `printenv TIPSBOT_CODE_REV` = commit;
   - v `profil4.db` (`mode=ro`) jsou 4 nové tabulky a otisky původních tabulek odpovídají záloze (kromě běžných zápisů);
   - v logu nejsou „měření NHL selhalo“ ani „ukládání výsledků selhalo“.
7. **Po prvním NHL skenu:**
   - nové snímky odpovídají počtu analyzovaných zápasů;
   - `code_rev` = commit;
   - `odds_fetched_at` = čas evidence B1;
   - srovnatelnost podle přítomnosti Pinnacle;
   - tipy a predikce Profilu 4 vznikají jako dřív (počty proti předchozím průchodům).
8. **Po prvním vyhodnocení:**
   - `nhl_results` pro dohrané měřené zápasy;
   - `nhl_result_corrections` prázdné;
   - `nhl-measure-report` proběhne s nulovou integritou chyb.
9. **Rollback:** `docker tag tipsbot-local:rollback-e7e2a85 tipsbot-local:latest` a postupné `up -d --no-build`. Tabulky měření zůstanou (ověřeno v oblasti 8), obnova DB jen při poškození.

## 11. Opakování kontrol po opravách

- testy F1–F3;
- `pra_perf.py` musí na skutečné cache s evidencí B1 dát srovnatelné snímky ve variantě s Pinnacle;
- `pra_realgolden.py` bitově shodný s `e7e2a85`;
- celá sada 2×;
- teprve potom žádost o schválení nasazení.

---

## Příloha – skripty (`scripts/audit_352/`, jen kopie DB, síť zablokovaná)

| Skript | Oblast |
|---|---|
| `pra_schema.py` | 1 – schéma produkce (jen čtení) proti M1/M2 |
| `pra_migrate.py` | 2 – migrace, idempotence, souběh procesů, zámek |
| `pra_perf.py` | 4, 5, 7, 9 – skutečný sken s měřením a bez něj, růst, síť |
| `pra_rollback.py` | 8 – starý kód nad zmigrovanou DB (spouští se s `PYTHONPATH` na worktree `e7e2a85`) |
| `pra_realgolden.py` | 5 – regrese na skutečných datech (spouští se nad `e7e2a85` i nad HEAD) |
| `m1_golden.py` | 5 – regrese na syntetických scénářích (potřebuje `tests/test_nhl_measure*.py`) |
