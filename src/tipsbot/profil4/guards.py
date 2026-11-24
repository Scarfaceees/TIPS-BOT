"""Faze 3.2.2 – bezpecnostni pojistka Modelu A proti extremnim predikcim.

Puvod: Bodo/Glimt – LASK Linz (LM, 24.11.2026). Model A (Dixon-Coles jen ze zapasu souteze) dal domacim
λ = exp(μ 0,02 + domaci vyhoda 0,28 + utok Bodo 0,93 − obrana LASK −1,36) = 13,4 golu a vyhru 99,998 %
(trh 63 %). LASK mel v datech souteze jen 6 zapasu a slabe propojeni s ostatnimi tymy; parametry tymu
s malo zapasy se k prumeru nestahuji, exp() pak extrapoluje. Model C (50 % A) z toho udelal 81 %.

Pojistka NEMENI pravdepodobnosti ani EV, jen zabrani schvaleni: kdyz se spusti, vsechny vybery zapasu dostanou
tvrdou vyhradu (NO BET) s duvodem a zaznamem v auditni stope (tips.intel, rankings.detail).

Podminky (zduvodneni z dat 56 zapasu s Modelem A i trhem na 1X2, 24.11.2026):
1. rozdil Modelu A a trhu v log-sancich na nekterem vysledku 1X2 >= `model_a_guard_logit` (3,0 = pomer sanci 20×).
   Bezne rozdily: median 0,31, 95. percentil 1,15, maximum mimo Bodo 1,92 (7×); Bodo 12,03 (168 000×).
   Hranice 3,0 je nad vsemi beznymi pripady s rezervou a hluboko pod patologickym.
2. bez ceny trhu: Model A dava nekteremu vysledku 1X2 >= `model_a_guard_p` (0,99). Mimo Bodo bylo nejvys 0,945
   (Bayern – Koln), ferova cena trhu nejvys 0,83; 0,99 (kurz 1,01) se v nasich datech u trhu nevyskytuje.
Mekka hranice `max_logit_gap` (0,5 – jen brani schvaleni jednoho vyberu) zustava beze zmeny.
"""

from __future__ import annotations

import math

from . import mathcore

GUARD = "MODEL A – EXTRÉMNÍ PREDIKCE"


def _logit(p: float) -> float:
    p = min(max(p, 1e-9), 1 - 1e-9)
    return math.log(p / (1 - p))


def model_a_guard(ms, cfg) -> dict:
    """Kontrola Modelu A pro cely zapas. `cfg` = DecisionConfig. Vraci zaznam pro auditni stopu."""
    out = {"triggered": False, "reasons": [], "threshold_logit": cfg.model_a_guard_logit,
           "threshold_p": cfg.model_a_guard_p}
    if ms is None or not ms.a.available:
        return out
    pa = [mathcore.evaluate("1X2", s, ms.a.matrix).p_win for s in ("1", "X", "2")]
    out.update(p_a=[round(x, 6) for x in pa], lam_a=[round(ms.a.lam_home or 0, 3), round(ms.a.lam_away or 0, 3)])
    if ms.b.available:
        pb = [mathcore.evaluate("1X2", s, ms.b.matrix).p_win for s in ("1", "X", "2")]
        gaps = [abs(_logit(a) - _logit(b)) for a, b in zip(pa, pb)]
        g = max(gaps)
        i = gaps.index(g)
        out.update(p_market=[round(x, 6) for x in pb], max_logit_gap=round(g, 3))
        if g >= cfg.model_a_guard_logit:
            out["reasons"].append(f"Model A {pa[i]:.1%} proti trhu {pb[i]:.1%} na {('1', 'X', '2')[i]} "
                                  f"(rozdíl log-šancí {g:.1f}, poměr šancí {math.exp(min(g, 50)):.0f}×, "
                                  f"hranice {cfg.model_a_guard_logit:.1f})")
    elif max(pa) >= cfg.model_a_guard_p:
        out["reasons"].append(f"Model A dává {max(pa):.2%} bez ceny trhu ke srovnání (hranice {cfg.model_a_guard_p:.0%})")
    if out["reasons"]:
        out["triggered"] = True
        out["reasons"].append(f"očekávané góly Modelu A {out['lam_a'][0]:.2f} : {out['lam_a'][1]:.2f}")
    return out
