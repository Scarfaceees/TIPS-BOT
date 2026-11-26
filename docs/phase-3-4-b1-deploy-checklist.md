# Phase 3.4 / B1 – předprodukční kontrola a postup nasazení

Stav k 26. 11. 2026. Nasazení **neproběhlo** a čeká na výslovné potvrzení.

- **Image před nasazením:**
  - `tipsbot-local:latest` = `tipsbot-local:8e3033d` = `sha256:ace23bdde10216f4fbdb88291135f464c863ce5dedbb07fc7becee38436712a8`;
  - oba kontejnery na něm běží od 25. 11. 13:38 UTC a neměly restart (ověřeno 26. 11. večer).
- **Očekávaný nový image:**
  - `tipsbot-local:latest` + `tipsbot-local:<commit z dev>`, sestavený z `dev` HEAD v okamžiku nasazení;
  - jeho ID vznikne až při buildu (předem sestavovat nebudeme);
  - po nasazení musí mít oba kontejnery **stejné** nové ID různé od `ace23bdde102`.
- Nasazuje se kód z `dev` (B1 = 4c7d37a + oprava před nasazením). Mezi 8e3033d a `dev` se ve `src/` mění jen B1.

---

## 0. Incident při přípravě (nutné rozhodnutí)

Při lokálním spuštění celé testovací sady zapsal jeden test do **produkční** `data/tipsbot.db`.

**Příčina:**
- `config.database_url` má výchozí hodnotu `sqlite:///data/tipsbot.db`, což je cesta relativní k pracovnímu adresáři.
- `tests/conftest.py` přesměrovával jen `TIPSBOT_DATA_DIR`, databázi hlavního bota ne.
- Test `closing.tick` v `test_profil4_calibration.py` proto otevřel produkční DB a vzal z ní skutečný API klíč.
- Dřívější běhy v kontejneru bez složky `data` se to netýkalo.

**Následky, ověřeno jen čtením:**
- V produkční `tipsbot.db` vznikla tabulka `odds_api_calls`, protože testy už běžely s novým kódem.
- Tabulka obsahuje 1 řádek: 26. 11. 14:23:40 UTC, `/sports/soccer_epl/events`, `fixture_id` 777 (testovací), HTTP 200, `x-requests-last` = 0.
- Šlo o **skutečný požadavek** na seznam zápasů, který je zdarma. Kredity zůstaly 239/261 a stav `odds_api_quota` se nezměnil, protože druhý, placený požadavek se neposlal.
- Jiné zápisy cesta testu nedělá: `set_odds_api_quota(None, None)` nic neuloží. Starý kód tabulku ignoruje (ověřeno, viz 2.3).

**Oprava (commit na `dev`):**
- `conftest.py` nastavuje `TIPSBOT_DATABASE_URL` do dočasné složky.
- Nová pojistka `_no_paid_api` zastaví v testech každý skutečný požadavek na the-odds-api.com a api-sports.io.
- Pojistka `_no_network` (doplněna 26. 11. večer) zablokuje v testech **jakékoli** síťové spojení mimo tento počítač, tedy i bezplatná API. Když to testovaný kód zkusí, test na konci selže, i kdyby kód chybu spojení sám zachytil.
- Dva nové testy hlídají izolaci DB a placená API.
- Pojistka `_no_network` hned odhalila další dosavadní problém: 3 testy v `test_live.py` (`test_league_without_matches_is_skipped_to_save_credits`, `test_odds_cache_saves_credits_and_force_refetches`, `test_cache_only_rescan_never_spends_credits`) při živém skenu skutečně stahovaly titulky z Google News (`news.google.com`, bezplatné). Opraveno napodobenou odpovědí v `test_live.py` (fixture `_no_news`).
- Po opravě se při testech nezměnil čas úpravy ani velikost produkční `tipsbot.db`.

**Znovu ověřeno 26. 11. večer (jen čtení):**
- tabulka má právě 1 řádek;
- `id` 1, `ts` 2026-11-26 14:23:40, `endpoint` `/sports/soccer_epl/events`, `purpose` `foreign_events`, `league` ENG1, `fixture_id` 777, `params` `{"dateFormat": "iso"}`, HTTP 200, `remaining` 239, `used` 261, `cost` 0 (`header`);
- `odds_api_quota` beze změny (t = 06:00:37, 239/261).

**Přesný SQL příkaz (zatím NESPUŠTĚNO).** Spouští se jen po záloze z kroku 4 a jen při volbě (a). Podmínka se trefí jen do tohoto řádku; když smaže jiný počet než 1, vše se vrátí:

```sql
BEGIN IMMEDIATE;
DELETE FROM odds_api_calls
 WHERE id = 1 AND fixture_id = 777 AND purpose = 'foreign_events'
   AND endpoint = '/sports/soccer_epl/events' AND ts = '2026-11-26 14:23:40.000000';
SELECT changes();  -- musí být 1, jinak ROLLBACK
COMMIT;
```

Provedení v kontejneru (Python, kontrola počtu a návrat při odchylce):

```
docker exec -i tipsbot python -c "import sqlite3; c=sqlite3.connect('/app/data/tipsbot.db', timeout=60); c.isolation_level=None; c.execute('BEGIN IMMEDIATE'); n=c.execute(\"DELETE FROM odds_api_calls WHERE id=1 AND fixture_id=777 AND purpose='foreign_events' AND endpoint='/sports/soccer_epl/events' AND ts='2026-11-26 14:23:40.000000'\").rowcount; (c.execute('COMMIT') if n==1 else c.execute('ROLLBACK')); print('smazano', n if n==1 else 0)"
```

**Záznam o incidentu zůstává v této dokumentaci i po smazání řádku.**

**Rozhodnutí pro vás – testovací řádek `id = 1` v `odds_api_calls`:**
- **(a) smazat** při nasazení po záloze (doporučeno, jinak zkresluje evidenci zápasem 777);
- **(b) ponechat** jako záznam o skutečném požadavku.

Tabulka sama zůstane v obou případech; nasazení by ji vytvořilo stejně.

---

## 1. Zálohy obou databází

**Postup:**
- SQLite Backup API uvnitř kontejneru, zdroj otevřený v režimu `mode=ro`. Skript `scripts/b1_preflight.py backup` ověří, že zápis do zdroje selže.
- Kopie se uloží do `/tmp` v kontejneru, tedy ne do `data/`, a pomocí `docker cp` se přenese do `backups/`.

**Zkouška nanečisto 26. 11. (do pracovní složky mimo projekt, pak smazáno):**

| Kontrola | tipsbot.db | profil4.db |
|---|---|---|
| Velikost kopie | 27 930 624 B | 197 029 888 B |
| `integrity_check` kopie | ok | ok |
| Obnova (kopie → nová DB) | ok | ok |
| Obnova shodná (tabulky, počty, SHA-1 všech řádků) | ano (9 tabulek, 1 072 řádků) | ano (15 tabulek, 260 913 řádků) |
| Původní DB nedotčené | zdroj jen pro čtení | zdroj jen pro čtení |

---

## 2. Migrace

1. **Bezpečnost a nedotčená data.** Na kopii DB z doby před B1 (`backups/tipsbot.db.20261125-predMetaV2.bak`) proběhlo:
   - nový kód přidal jen tabulku `odds_api_calls`;
   - všech 8 existujících tabulek má stejné schéma, počty i SHA-1 všech řádků;
   - druhý start nic nezměnil (idempotence).
   - `profil4.db` se B1 netýká.
2. **Souběžný start obou kontejnerů.**
   - Zjištěno 26. 11.: dva procesy startující současně selhaly v 6 z 30 pokusů na chybě `table odds_api_calls already exists`. SQLAlchemy totiž nejdřív zjistí, co chybí, a pak tabulku vytváří.
   - Opraveno: `make_session_factory` při chybě „already exists“ zopakuje `create_all`, který tabulku už najde.
   - Po opravě 0 selhání ze 40 pokusů.
   - Postup nasazení navíc spouští kontejnery postupně.
3. **Kompatibilita rollbacku.** Kód 8e3033d nad migrovanou DB nastartuje, přečte stav kreditů a nic nezmění.
4. **Produkční DB už tabulku má** (bod 0), takže migrace při nasazení nic nevytvoří.

---

## 3. Evidence

1. **Chyba evidence je v logu.**
   - `oddsapi._emit` ji zachytí a zapíše varování `evidence pozadavku kurzoveho API selhala: …` (bez klíče).
   - Test `test_evidence_failure_is_logged_and_quota_still_saved`.
2. **Žádné další požadavky.**
   - Evidence jen zapisuje.
   - Testy počítají HTTP volání a skutečný síťový požadavek v nich test shodí.
   - Každé místo volání pošle stejný počet požadavků jako dřív.
3. **Stav kreditů se uloží i při selhání evidence.**
   - `log_odds_api_call` nově nic neukládá okamžitě. Řádek přidá do transakce volajícího a chyba v něm nenaruší ukládání stavu kreditů (test výše).
   - Pořadí požadavků v jedné transakci se drží kvůli výpočtu rozdílu `used` (test `test_two_requests_in_one_transaction_delta_uses_pending_row`).
4. **Zbývající riziko:** když by selhalo samotné uložení celé transakce (např. zamčená DB), neuloží se ani stav kreditů. To platilo i před B1.

---

## 4. Postup nasazení (po vašem „ano“)

V Git Bash vždy s `MSYS_NO_PATHCONV=1`. Bez něj se cesty `/tmp/...` u `docker exec/cp` přepíšou; viz pozn. 2 níže.

1. **Okno nasazení:**
   - mimo průchody automatu (00, 06, 12, 18 UTC ± 20 min);
   - ne 90 min před výkopem zápasu se schváleným tipem (měření zavíracího kurzu 75/30/10 min);
   - kontrola: stav `auto:running` v `tipsbot.db` (jen čtení) a `docker logs --since 10m tipsbot-auto`.
2. **Výchozí stav:** `docker inspect -f '{{.Image}} {{.State.StartedAt}} {{.RestartCount}}' tipsbot tipsbot-auto` a `git rev-parse HEAD` (musí odpovídat schválenému commitu na `dev`).
3. **Rollback image:** `docker tag tipsbot-local:latest tipsbot-local:rollback-8e3033d` (= `ace23bdde102`).
4. **Zálohy:**
   - `docker exec -i tipsbot python - backup /tmp/predB1 < scripts/b1_preflight.py`;
   - `docker cp` do `backups/predB1-YYYYMMDD-HHMM/`;
   - `docker exec tipsbot rm -rf /tmp/predB1`;
   - `python scripts/b1_preflight.py verify backups/predB1-…` musí vypsat `VERIFY OK`. Jinak **stop**.
5. **(Jen při volbě 0a)** `DELETE FROM odds_api_calls WHERE id=1 AND fixture_id=777 AND purpose='foreign_events'` v `tipsbot.db`. Kontrola: smazán přesně 1 řádek.
6. **Sestavení (bez výpadku, staré kontejnery běží dál):** `docker compose build` a pak `docker tag tipsbot-local:latest tipsbot-local:<commit>`.
7. **Restart postupně:**
   1. `docker compose up -d --no-build tipsbot` a počkat na HTTP 200 z `http://127.0.0.1:8000/`;
   2. `docker compose up -d --no-build tipsbot-auto` a počkat na první řádek logu automatu.
8. **Očekávaný dopad:**
   - panel nedostupný přibližně 5–15 s;
   - automat se restartuje a pokračuje svým rozvrhem (rozpracovaný průchod by se přerušil, proto okno v kroku 1);
   - žádné API požadavky navíc. Přestavba image API nevolá; start automatu volá API jen podle normálního rozvrhu a jeho pojistek.

**Rollback:**
- `docker tag tipsbot-local:rollback-8e3033d tipsbot-local:latest`, potom totéž postupné `up -d --no-build`.
- Tabulka `odds_api_calls` zůstane a starý kód ji ignoruje (ověřeno).
- Obnova DB ze zálohy jen při poškození dat: zastavit oba kontejnery, vrátit soubory ze zálohy, spustit postupně.

---

## 5. Kontrola po nasazení (jen čtení, žádný testovací API požadavek)

1. Oba kontejnery: `.Image` = ID nového image (stejné u obou), `RestartCount` = 0, stav `running`.
2. Nový kód v obou: `docker exec <kontejner> python -c "import tipsbot.oddsapi as o; print(hasattr(o, 'request'))"` vrátí `True`.
3. Tabulka existuje: dotaz na `sqlite_master` (`mode=ro`). Počet řádků je 0 (volba a) nebo 1 (volba b).
4. Logy obou kontejnerů bez chyb a bez varování evidence.
5. **První přirozené volání** (nejbližší průchod automatu v 00/06/12/18 UTC nebo stažení výsledků):
   - nový řádek: účel, liga, HTTP 200;
   - `cost_kind = header`, pokud API hlavičku `x-requests-last` posílá;
   - `remaining + used = 500`;
   - součet `cost` za průchod = změna `used`;
   - stav `odds_api_quota` se dál aktualizuje jako dřív.
6. Pokud něco nesedí: nic neopravovat, zapsat diagnostiku a případně rollback.

---

## 5a. Test snapshotu – nález 26. 11.

**Selhání v běhu celé sady:**
- `test_snapshot_during_concurrent_writes_is_one_consistent_state_of_both_databases` (1× z 821).
- Traceback toho běhu se **neuložil**, protože výstup byl zkrácený. Proto nelze s jistotou říct, která podmínka tehdy selhala.

**Reprodukce a příčina:**
- Bez zátěže test prošel 25 z 25 běhů.
- Pod umělou zátěží (4 procesy zapisující na disk a 6 zatěžujících CPU) selhal 1 z 15 běhů na podmínce `w.max_wait < 0.5`: zapisovač čekal 1,0 s, tedy celý svůj busy timeout, a zámek získal až na konci.
- Snapshot drží zámek jen po dobu kroku Backup API. Délka kroku ale závisí na zátěži disku a CPU, takže pevná mez 0,5 s není zaručitelná. Jde o **chybu testu** (měření času na sdíleném stroji), ne o regresi.
- `snapshot.py` ani `test_snapshot.py` se od 8e3033d nezměnily a B1 je nepoužívá.

**Oprava (jen `dev`, jen testy):**
- Timeout zapisovače v testu je 10 s (automat má 60 s) a mez proti dlouhému blokování 5 s.
- Nový deterministický test `test_source_is_not_locked_between_backup_steps` po každém kroku kopírování zapíše do obou zdrojů s **timeoutem 0**. Projde jen tehdy, když snapshot mezi kroky nedrží zámek.
- Kontrola testu samotného: `test_probe_detects_a_lock_held_across_steps` dokazuje, že by zámek držený přes kroky odhalil.

**Zbývající citlivost:**
- Pod stejnou umělou zátěží skončily 3 z 20 opakování `SnapshotBusy`: zdroj se měnil celých 60 s a snapshot se vzdal.
- To je navržené chování snapshotu (raději nic než smíšená kopie), ne chyba dat. Bez umělé zátěže se neobjevilo.

---

## 6. Mimo rozsah B1

- Strategie NHL ani přepínání Modelu 0 se nemění. Je to samostatné téma.
- Rozhodovací engine, EV, prahy, profily, rezerva 50 kreditů a cache-only zůstávají beze změny.

**Poznámky:**
1. Testy se spouštějí lokálně z `.venv`. Po opravě `conftest.py` už nesahají na produkční DB.
2. Zkouška zálohy nanečisto kvůli přepisu cest v Git Bash nejdřív zapsala kopie do zapisovatelné vrstvy kontejneru (`/app/C:/…`), ne do `data/`. Zkopírovaly se ven a v kontejneru se hned smazaly.
