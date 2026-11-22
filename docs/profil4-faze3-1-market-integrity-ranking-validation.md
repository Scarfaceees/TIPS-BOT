# Profil 4 – Fáze 3.1: Market Integrity & Ranking Validation

Stav k 22. 11. 2026. Výchozí commit `1cd3225` (Fáze 3), větev `dev`.

Modely A–D, jejich váhy, Poisson, Monte Carlo, EV, férové kurzy, settlement a prahy APPROVED / WATCH / NO BET
se **nezměnily**. Jediná změna rozhodování: u NHL přibyla nezávislá kontrola výpočtu. Při neshodě výpočtů dostane
kandidát NO BET. Jiné stavy neovlivňuje.

## 1. Audit původního stavu

| Oblast | Stav před Fází 3.1 | Zjištění |
|---|---|---|
| Mapování AH z API-Football | funguje | `odds._ah` neotáčí znaménko u „Away“. Ověřeno znovu všemi typy linií: celé, půlové, čtvrtinové, nulová, kladné, záporné, `"-0.5,-1"`. |
| Settlement | funguje | Produkční `mathcore.settle` a nezávislý `integrity.outcome` souhlasí s ručně psanou referenční tabulkou (46 případů). |
| Nezávislý přepočet EV (fotbal) | funguje | `integrity.validate` přepočítá EV, férový kurz a pravděpodobnost z matice skóre vlastní implementací. Při neshodě dá NO BET. |
| Nezávislý přepočet (NHL) | **chyběl** | EV se počítalo jedním vzorcem bez kontroly. Doplněno `integrity.binary_check`. |
| Spárování protistrany, marže, cena trhu | funguje | Kontroluje se jen stejná kancelář a stejný čas aktualizace. |
| Quality of Data v DB | částečně | Hodnoty v `tips` jsou v DB správně (stáří v minutách je číslo, časy ISO, stavy v `data_sources`). **Žádný ze 213 tipů ale neměl `quality`/`rank`/`intel`.** |
| Kvalita dat v Excelu | chybné zobrazení | Počet kanceláří a skóre byly prázdné, protože u starších tipů nejsou uložené. Prázdná buňka nebyla odlišená od nuly. Časy byly text, ne datum. Výsledkem byl dojem posunutých sloupců. |
| Opportunity Ranking | **nefungoval v praxi** | Viz níže. |

### Proč byl list Opportunity Ranking prázdný (přesná příčina)

1. Ranking se počítal správně: NHL rozbor ve 02:10 ho u všech 14 kandidátů měl. Ukládal se ale **jen do tabulky
   `tips`** a jen u tipů, které se ukládaly **poprvé**. `history.record` přeskočí tip, jehož kancelář, kurz
   a rozhodnutí už v databázi jsou. U nadcházejících zápasů s tipy uloženými před nasazením Fáze 3 se proto ranking
   nikdy nezapsal.
2. Fotbalový sken od nasazení neproběhl (ranní sken běží v 9:00). Poslední fotbalové tipy jsou z 21. 11. 22:49, tedy
   z doby před Fází 3.
3. Export i panel četly ranking jen z `tips.intel`, takže se do nich nedostali kandidáti, kteří se neuložili jako tip.

Migrace ani podmínky exportu chybu nezpůsobovaly. Ranking se ztrácel v návrhu ukládání.

## 2. Opravy a nové funkce

- **Tabulka `rankings`** (nová, nedestruktivní `CREATE TABLE IF NOT EXISTS`). `history.record_rankings` ukládá
  při každém rozboru (fotbal i NHL) ranking **všech** předzápasových kandidátů. Ke každému ukládá čas výpočtu,
  verzi `rank-v1 (22.11.2026)`, kurz, EV, konzervativní EV, robustnost, kvalitu dat, rozhodnutí, verdikt integrity,
  příznak `valid` a detail (důvody pořadí, kvalita dat, kurzová inteligence a audit).
  - Nový řádek vzniká jen při změně kanceláře, kurzu, rankingu, kvality dat nebo rozhodnutí. Tím se zajistí
    deduplikace a aktualizace po změně kurzu nebo dat.
  - Po výkopu se nic neukládá a historické řádky se nemění.
- **Aktuální ranking** (`opportunities_ranked`) bere poslední výpočet každého výběru, jen pokud zápas ještě nezačal
  a výběr je platný (aktuální kurz, nezávislá kontrola bez tvrdé chyby). Rozhodnutí se přebírá beze změny.
  Ranking NO BET nikdy nezmění na APPROVED.
- **Audit integrity kandidáta** (`intelligence.integrity_audit`) obsahuje vše podle bodu 4.4 zadání. Verdikty:
  - OVĚŘENO
  - ČÁSTEČNĚ OVĚŘENO – trh nelze plně ověřit (chybí protistrana nebo cena trhu; nelze APPROVED)
  - PODEZŘELÝ KURZ
  - NO BET – MATEMATICKÁ INTEGRITA NEOVĚŘENA
  - NEOVĚŘENO – nezávislá kontrola neproběhla

  Tvrdé chyby nezávislé kontroly mají v důvodech NO BET předponu „MATEMATICKÁ INTEGRITA NEOVĚŘENA“.
- **NHL:** `integrity.binary_check` znovu spočítá EV = p·kurz − 1, férový kurz 1/p a minimální kurz z p a kurzu.
  Neshoda vede k NO BET.
- **Quality of Data** zůstává jedno skóre 0–100 se stejnými váhami. Nově má tři dílčí skóre (kvalita kurzu,
  sportovní data, úplnost zdrojů) a explicitní `details`: stáří, čas u zdroje, čas stažení, zdroj, kancelář, počet
  kanceláří, protistrana (True / False / None = nelze spárovat), statistiky, sestavy, absence, dostupné a chybějící
  zdroje. Hodnota None znamená „neznámo“, nikoli nulu.
- **Ranking Validation** (`ranking_validation`) pracuje s posledním předzápasovým rankingem každého výběru,
  výsledkem z `predictions` a ROI na 1 jednotku ze skutečného kurzu rankingu.
  - Pro pásma 0–39 / 40–59 / 60–79 / 80–100 počítá: počet zápasů, počet výběrů, ROI s intervalem 95 % (shlukový
    bootstrap po zápasech), Brier, Log Loss, CLV (jen ověřené), průměrné konzervativní EV, kvalitu dat a podíl
    APPROVED.
  - Rozdíl ROI mezi rankingem ≥ 60 a < 60 má vlastní bootstrap interval. Navíc se data dělí chronologicky na starší
    a novější polovinu zápasů.
  - Závěr se vyvozuje až od 100 zápasů, do té doby platí „MÁLO DAT – RANKING ZATÍM NEOVĚŘEN“.
  - **Ochrana proti přeučení:** váhy rankingu se neladí. Validuje se jen jedna verze algoritmu, a to na rankinzích
    spočtených až po jejím zavedení, takže jde vždy o data mimo vzorek.
- **Portfolio Risk** (`portfolio` a list Excelu):
  - Koncentrace podle zápasu, soutěže, sportu, kurzového pásma a rozhodnutí.
  - Korelované výběry stejného zápasu a **protichůdné výběry** stejného trhu, například „Atalanta – Inter 1X2:1
    i 1X2:2“ v živých datech.
  - V Excelu odděleně: oficiální tipy, sledované výběry, skutečné tikety (Kč), kombi, virtuální výsledek
    a skutečný výsledek. Jednotky a koruny se nikdy nesčítají.
- **Analýza chyb:** `classify_error` přiřadí kategorii jen podle doložených dat. Nezávisle přepočítá settlement
  ze skóre, takže pozná chybu settlementu. Dále rozpozná chybu mapování, chybu kurzu, zastaralý kurz, protistranu,
  pravidla trhu, nedostatek dat, optimistický nebo konzervativní odhad a nízkou robustnost. Ostatní prohry označí
  jako „běžná náhodná prohra“. Registr `KNOWN_ISSUES` uvádí opravené chyby: co se stalo, vstup, očekávanou hodnotu,
  opravu a test.
- **Panel** (Profil 4 → Intelligence & Calibration):
  - nové sekce Market Integrity, Ranking Validation a Portfolio & riziko jako karty a rozbalovací detaily;
  - u rankingu dílčí kvalita, integrita, čas výpočtu a verze;
  - u chyb kategorie a registr známých chyb.
- **CLI:** `tipsbot profil4 intel [integrity|ranking|calibration|data-quality|portfolio|errors] [--out report.json]`.
  Je jen analytické, zapisuje pouze přepočet do kv a archivu, historii nemění.
- **Excel:** všech 12 listů zůstalo, přibyly listy Market Integrity, Ranking Validation a Portfolio Risk. List
  Kvalita dat čte hodnoty podle názvů polí, časy ukládá jako datum a obsahuje sloupec „Původ hodnot“. Opportunity
  Ranking čte z `rankings` a navíc ukazuje historii posledního rankingu všech výběrů. Analýza chyb obsahuje
  kategorie a registr.

## 3. Změněné soubory

- `src/tipsbot/profil4/store.py`: tabulka `rankings` a index
- `src/tipsbot/profil4/history.py`: `record_rankings`, volané z `record`
- `src/tipsbot/profil4/intelligence.py`:
  - skupiny a detaily kvality dat, `integrity_audit`
  - `opportunities_ranked`, `ranking_validation`, `integrity_summary`, `portfolio`
  - `classify_error`, `independent_settle`, `KNOWN_ISSUES`, `load_rankings`
  - CLI text po sekcích
- `src/tipsbot/profil4/integrity.py`: `binary_check`
- `src/tipsbot/profil4/nhl.py`: nezávislá kontrola NHL kandidátů
- `src/tipsbot/profil4/decision.py`: předpona „MATEMATICKÁ INTEGRITA NEOVĚŘENA“ u tvrdých chyb kontroly
- `src/tipsbot/profil4/cli.py`: sekce a `--out`
- `src/tipsbot/export.py`: Kvalita dat, Opportunity Ranking, Analýza chyb, 3 nové listy, metodika
- `src/tipsbot/web.py`: nové sekce panelu
- `tests/test_profil4_phase31.py` (nový), `tests/test_export.py` (seznam listů)
- `README.md`, tento dokument

## 4. Databáze a migrace

- Nová tabulka `rankings` a index `rankings_key`. Jde o čistě přidání, existující tabulky ani data se nemění.
- Před nasazením se zálohovaly obě databáze přes sqlite backup API do `backups/profil4.db.20261122-0308.bak`
  a `backups/tipsbot.db.20261122-0308.bak`.
- Žádné historické tipy, tikety, ručně zadané kurzy ani výsledky se nemazaly ani nepřepisovaly.

## 5. Matematické kontroly

- Referenční tabulka settlementu je psaná ručně podle pravidel kanceláří. Pokrývá 1X2, DNB, BTTS, AH 0, celé,
  půlové a čtvrtinové linie pro obě strany a OU/TT včetně čtvrtin. Produkční i nezávislá implementace s ní
  souhlasí ve všech 46 případech.
- EV čtvrtinové linie se ručně rozdělí na dvě poloviny vkladu (−0,25 = půl na 0, půl na −0,5). Výsledek souhlasí
  s `mathcore.evaluate` i s `integrity.recompute`. Binární zjednodušení by dalo jiné EV a v kódu se nepoužívá.
- Férový kurz linie s vrácením je (1 − b) / a, ne 1/p. Ověřeno.

## 6. Testy

- Nový soubor `tests/test_profil4_phase31.py`: 138 testů včetně parametrizací. Pokrývá:
  - referenční settlement a mapování AH;
  - čtvrtinové EV a nezávislou kontrolu NHL;
  - NO BET při neshodě, verdikty integrity a podezřelé kurzy;
  - všech 64 kombinací dostupnosti dat;
  - NULL ≠ 0 a mapování sloupců Excelu;
  - ukládání, deduplikaci a aktualizaci rankingu;
  - pouze předzápasové rankingy a to, že ranking nemění rozhodnutí;
  - validaci pásem: málo dat, funkční ranking, únik informací a oddělení verzí;
  - export, portfolio (korelace, protichůdné výběry, jednotky vs. Kč), kategorie chyb, panel a CLI.
- Celá sada: výsledek viz závěr. Žádný původní test nebyl odstraněn, jen `test_export.py` má rozšířený seznam listů.

## 7. Ověřeno na skutečných datech

- Nezávislý přepočet settlementu ze skóre na živé DB Profilu 4: **109 vyhodnocených fotbalových tipů a 15 032
  předpovědí, 0 neshod**. 11 tipů NHL se tímto fotbalovým přepočtem neověřuje.
- Portfolio na živé DB: 56 čekajících tipů na 22 zápasech. Více zápasů má několik korelovaných tipů, Atalanta – Inter
  má protichůdné výběry 1X2.
- Analýza chyb na živé DB: 59× běžná náhodná prohra, 30× výhra, 4× vrácení, 3× zastaralý kurz, 1× chyba mapování
  (známý AH tip zneplatněný 20. 11.), 1× vysoká nejistota modelu. Chyba settlementu se nenašla.
- Migrace, Docker, CLI, panel a Excel na živém nasazení: viz závěrečná zpráva.

## 8. Ověřeno simulací

- Ranking a validace pásem: syntetické rankingy s pevným seedem (funkční i nefunkční ranking, únik informací,
  jiná verze).
- Ukládání rankingu: záznam odpovědí API-Football (`tests/fixtures`) přes `hunter.analyze_fixture`.

## 9. Co zatím není ověřeno

- **Opportunity Ranking na živých fotbalových datech:** tabulka `rankings` se začne plnit při příštím rozboru
  (ranní sken automatu, Detail zápasu nebo NHL sken). Do té doby jsou listy Ranking a Market Integrity prázdné.
- **Účinnost rankingu:** zatím 0 vyhodnocených rankingů, stav „MÁLO DAT – RANKING ZATÍM NEOVĚŘEN“. Závěr bude možný
  až od 100 nezávislých zápasů.
- Kalibrace A–D: fotbal 31 a NHL 2 společné zápasy. Váhy se nemění.
- Rychlý sken (`--quick`) nepočítá modely, proto ranking nemá. Ranking vzniká jen při hloubkovém rozboru.
