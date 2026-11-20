# Audit Profilu 4 – EV, kurzy a mapování trhů (20. 11. 2026)

Prověřeny byly čtyři podezřelé tipy ze skenu ve 14:17–15:23 UTC. Rozbor stojí na surových odpovědích
API-Football z cache Profilu 4 a na přepočtu modelů z uložených kurzů. Data byla jen čtena, kopie ve scratchpadu.

## Shrnutí příčin

| Tip | Hlášeno | Příčina | Po opravě |
|---|---|---|---|
| Leverkusen – Paderborn, hosté +1,5 @ 13.00 (Betfair) | EV +495 % | **Chybné mapování AH.** Kurz 13.00 je „Away +1.5“ = hosté **−1,5** (Paderborn vyhraje o 2+). Kód ho uložil jako hosté +1,5. | Kurz 13.00 patří k hosté −1,5: EV záporné, NO BET. Hosté +1,5 mají správně kurz 2.22 (Pinnacle). |
| Elversberg – Freiburg, hosté +1,75 @ 4.25 (Bet365) | EV +264 % | Stejná chyba mapování AH. Navíc to byl zastaralý kurz (6+ h). | Kurz 4.25 je hosté −1,75. |
| Schalke – Stuttgart, remíza @ 4.32 (1xBet) | p 28,3 %, EV +22 % | **Tržní model B neseděl na vlastní kurzy.** Gólové linie byly starší než 6 h, a tak B stál jen na 1X2. Silný „prior“ na součet gólů převzatý **z modelu A** posunul remízu trhu z 23,2 % na 28,1 %. Model C (90 % B) pak „viděl“ hodnotu. | B dává remízu 23,2 % (= Pinnacle bez marže). EV klesá zhruba na +4 % a kurz je jen 0,6 % nad cenou trhu. |
| Real Madrid – Celta, remíza @ 9.00 (Betfair) | p 13,8 %, EV +24 % | Stejná vada modelu B (remíza 13,2 % místo 12,0 %). Navíc osamocený kurz: další nejvyšší je 8.20. | Kurz je ~8 % nad trhem a model A s trhem se rozcházejí (poměr šancí 1,7×), takže tip nedostane APPROVED. |

Další nálezy:

- **Real – Celta, pod 2,5 @ 3.20, EV +55 %:** gólové linie byly 10 h staré, takže je B nepoužil. Pravděpodobnost gólů
  modelu B (48 %) byla jen dopočet z 1X2 a celkového počtu gólů z modelu A, ne cena trhu (Pinnacle ~31 %). Přesto
  se zobrazovala jako „trh“.
- **Bayern – Köln, hosté přes 1,0 gólu @ 4.15 (1xBet), APPROVED:** samotné mapování sedí. Kurz je ale o ~30 % nad
  férovou cenou trhu (~3.1) a nikdo jiný ho nenabízí, nejbližší je Marathonbet 3.16. Jde o chybný nebo osamocený
  kurz, ne o hodnotu.
- **Vzorce EV a férového kurzu jsou správně.** EV = p·kurz − 1 a férový kurz = 1/p, u čtvrtinových a celých linií
  (1 − b)/a. Pravděpodobnost je vždy v rozsahu 0–1. Chyby nebyly ve výpočtu, ale v datech, která do něj vstupovala.

## Důkaz chyby mapování AH

API-Football uvádí u **obou** stran číslo handicapu **domácích**:

- Pinnacle: `Home -1.5 = 1.70` / `Away -1.5 = 2.22`. Je to jeden trh s marží 3,9 %.
- Bet365: `Away +0.5 = 8.00` se rovná kurzu na výhru hostů v 1X2 (8.00).
- Při starém výkladu by `Away -1.75 = 1.95` znamenalo, že Paderborn vyhraje o 2+ s kurzem 1.95, což je nesmysl.

## Opravy

1. `profil4/odds.py`: „Away X“ se mapuje na `AH_X:away` bez otočení znaménka. Nejednoznačné duplicitní výběry
   (stejný výběr se dvěma různými kurzy) se vynechají.
2. `profil4/models.py`: model B závisí jen na kurzech, ne na modelu A. 1X2 reprodukuje přesně. Když chybí aktuální
   gólová linie, říká, že gólové trhy jsou jen dopočet.
3. `profil4/integrity.py`, nová kontrolní vrstva před APPROVED BET:
   - nezávislý přepočet výsledku sázky skóre po skóre, včetně půlvýher, vrácení a půlproher. Musí se shodovat EV,
     férový kurz i pravděpodobnost, jinak NO BET;
   - spárování kurzu s protistranou téže kanceláře. Součet pod 99 % znamená záměnu strany, znaménka nebo linie
     (NO BET). Chybějící protistrana nebo marže nad 25 % znamená WATCH;
   - cena trhu (bez marže) pro přesně tentýž výběr. Pokud pro daný typ trhu nejsou aktuální kurzy, nelze tip
     schválit;
   - extrémní hodnota (kurz o víc než 10 % nad trhem) potřebuje potvrzení od jiné kanceláře, jejíž kurz je nižší
     nejvýš o 5 %. Jinak WATCH s konkrétním důvodem;
   - rozchod modelu A a trhu měřený i poměrem šancí (nad 1,65×), ne jen v procentních bodech. Hlídá to hlavně
     outsidery.
4. `profil4/store.py`: jednorázová oprava dat.
   - Kurzy „Away“ AH z API se smazaly a z cache API se při dalším čtení načtou správně.
   - Tipy, které na nich stály, dostaly výsledek `void`, takže se nezapočítávají do výsledků (na ostré databázi
     2 tipy).
   - Ruční kurzy zůstaly beze změny.

## Ověření

- Testy `tests/test_profil4_integrity.py` (36) používají záznam skutečné odpovědi API
  (`tests/fixtures/apifootball_odds_20261120.json`, zkrácený, hodnoty beze změny). Nejde o živé ověření.
- Před opravou testy mapování AH, absurdního EV a fitu modelu B padají, po opravě procházejí.
- Celá sada: 347 testů prošlo.
- Po nasazení proběhl jeden ostrý sken (20. 11. ~16:55 UTC). Celkový verdikt je WATCH a žádný tip nedostal APPROVED.
  - Leverkusen: nejlepší možnost je hosté +0,75 @ 3.73 (Pinnacle) s EV +3,8 %. Neschváleno, protože konzervativní
    EV je záporné a model A se rozchází s trhem.
  - Real – Celta pod 2,5: WATCH, protože kurz je zastaralý a cena trhu není ověřitelná.

## Co zůstává (neopraveno, jen zjištěno)

- **Kurzy jiných kanceláří nejsou kurzy Tipsportu.** Betfair, 1xBet a další se zobrazují pod svým jménem. Pro
  Tipsport platí řádek „Min. kurz“; kurz z Tipsportu je potřeba zadat ručně.
- **Betfair v API může být burza.** Pak je kurz před odečtením provize (~5 % ze zisku) a skutečný kurz je nižší.
  Z API to nelze rozlišit.
- **Poissonův model B** nesedí přesně na krajní gólové linie (např. přes 7,5) – odchylky 2–5 p.b. Cena trhu pro
  kontrolu se proto bere přímo z kurzů bez marže, kde existují.
- **Detailní kurzy z API** (`odds?fixture=`) bývají staré i 6–12 h, i když 1X2 za ligu je čerstvé. Takové gólové
  linie se do trhu nepoužijí a tipy na ně nedostanou APPROVED.
- **Váhy modelů se neměnily.** Nebyly laděné na těchto zápasech: model A má v ligách 10 % podle zpětného testu
  hlavního bota.
