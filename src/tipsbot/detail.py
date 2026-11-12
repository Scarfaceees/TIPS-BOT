"""Podrobny rozbor zapasu: z ceho model vychazi (sila tymu, forma, unava, vzajemne zapasy)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from .models import football
from .models.nhl import goalie_gsax60

Section = dict  # {"title": str, "lines": [str]}


def _res(gf: int, ga: int) -> str:
    return "V" if gf > ga else "P" if gf < ga else "R"


def nhl_sections(ratings, home: str, away: str, day: date, probs: dict[str, float]) -> list[Section]:
    ctx = ratings.context(home, away, day)
    out: list[Section] = []
    if ctx is None:
        return out
    lh, la = probs.get("lambda_home", 0.0), probs.get("lambda_away", 0.0)
    out.append({"title": "Očekávané skóre (model)", "lines": [
        f"Očekávané góly: {home} {lh:.2f} – {la:.2f} {away} (celkem {lh + la:.2f}, úroveň gólů sladěna s trhem)",
        f"Pravděpodobnost výhry v základní hrací době: {home} {probs['REG:1'] * 100:.0f} %, "
        f"prodloužení {probs['REG:X'] * 100:.0f} %, {away} {probs['REG:2'] * 100:.0f} %",
        f"Celkově vč. prodloužení: {home} {probs['MONEYLINE:home'] * 100:.1f} %, {away} {probs['MONEYLINE:away'] * 100:.1f} %",
    ]})
    # sila tymu: xG rozdil na 60 minut a poradi v lize
    table = sorted(ratings.ratings.items(), key=lambda kv: kv[1].xgf60 - kv[1].xga60, reverse=True)
    rank = {t: i + 1 for i, (t, _) in enumerate(table)}
    lines = []
    for t in (home, away):
        r = ratings.ratings[t]
        lines.append(
            f"{t}: xG pro {r.xgf60:.2f}/60, proti {r.xga60:.2f}/60 (liga {ctx.league_xg60:.2f}), "
            f"rozdíl {r.xgf60 - r.xga60:+.2f} → {rank[t]}. z {len(table)} týmů, zápasů {r.games}"
        )
    out.append({"title": "Síla týmů (očekávané góly, všechny situace)", "lines": lines})
    if ctx.home_goalie.shots > 0 or ctx.away_goalie.shots > 0:
        out.append({"title": "Brankáři (průměr týmu podle odchytaných zápasů; starter se potvrzuje až v den zápasu)", "lines": [
            f"{t}: GSAx {g.gsax:+.1f} na {g.shots:.0f} střelách → {goalie_gsax60(g):+.2f} gólu/60 min (po stažení k nule)"
            for t, g in ((home, ctx.home_goalie), (away, ctx.away_goalie))]})
    # forma + unava
    form, rest = [], []
    for t in (home, away):
        g = ratings.past[ratings.past["team"] == t].sort_values("date").tail(5)
        if g.empty:
            continue
        seq = " ".join(_res(int(a), int(b)) for a, b in zip(g["goalsFor"], g["goalsAgainst"]))
        form.append(
            f"{t}: {seq} (góly {int(g['goalsFor'].sum())}:{int(g['goalsAgainst'].sum())}, "
            f"xG {g['xGoalsFor'].sum():.1f}:{g['xGoalsAgainst'].sum():.1f})"
        )
        last = g["date"].max()
        rest.append(f"{t}: {(day - last).days} dní od posledního zápasu")
    if form:
        out.append({"title": "Forma (posledních 5 zápasů, V/P)", "lines": form})
    flags = []
    if ctx.home_b2b_travel:
        flags.append(f"{home}: druhý zápas ve dvou dnech + cesta přes časové pásmo (−5 % gólů)")
    if ctx.away_b2b_travel:
        flags.append(f"{away}: druhý zápas ve dvou dnech + cesta přes časové pásmo (−5 % gólů)")
    out.append({"title": "Odpočinek a cestování", "lines": rest + (flags or ["Žádný tým nehraje back-to-back s cestou."])})
    return out


def football_sections(params, df: pd.DataFrame, home: str, away: str, probs: dict[str, float]) -> list[Section]:
    lam, mu = params.expected_goals(home, away)
    m = football.match_matrix(params, home, away)
    scores = sorted(((m[i, j], i, j) for i in range(m.shape[0]) for j in range(m.shape[1])), reverse=True)[:3]
    out: list[Section] = [{"title": "Očekávané skóre (model)", "lines": [
        f"Očekávané góly: {home} {lam:.2f} – {mu:.2f} {away}",
        f"1X2: {home} {probs['1X2:1'] * 100:.1f} %, remíza {probs['1X2:X'] * 100:.1f} %, {away} {probs['1X2:2'] * 100:.1f} %",
        "Nejpravděpodobnější výsledky: " + ", ".join(f"{i}:{j} ({p * 100:.1f} %)" for p, i, j in scores),
        f"Over 2.5 gólů: {probs['OU_2.5:over'] * 100:.1f} %",
    ]}]
    rk = sorted(params.teams, key=lambda t: params.attack[t] + params.defence[t], reverse=True)
    lines = []
    for t in (home, away):
        lines.append(
            f"{t}: útok {params.attack[t]:+.2f}, obrana {params.defence[t]:+.2f} → {rk.index(t) + 1}. z {len(rk)} týmů"
            + (f", domácí výhoda {params.home_adv[t]:+.2f}" if t == home else "")
        )
    out.append({"title": "Síla týmů (Dixon-Coles, váhy podle stáří zápasů)", "lines": lines})
    form = []
    for t in (home, away):
        g = df[(df["home"] == t) | (df["away"] == t)].sort_values("date").tail(5)
        if g.empty:
            continue
        seq, gf, ga = [], 0, 0
        for _, r in g.iterrows():
            a, b = (r["home_goals"], r["away_goals"]) if r["home"] == t else (r["away_goals"], r["home_goals"])
            seq.append(_res(int(a), int(b)))
            gf, ga = gf + int(a), ga + int(b)
        form.append(f"{t}: {' '.join(seq)} (góly {gf}:{ga})")
    if form:
        out.append({"title": "Forma (posledních 5 zápasů, V/R/P)", "lines": form})
    h2h = df[((df["home"] == home) & (df["away"] == away)) | ((df["home"] == away) & (df["away"] == home))].tail(5)
    if not h2h.empty:
        out.append({"title": "Poslední vzájemné zápasy", "lines": [
            f"{r['date']:%d.%m.%Y} {r['home']} {int(r['home_goals'])}:{int(r['away_goals'])} {r['away']}" for _, r in h2h.iterrows()
        ]})
    return out


def post_text(name: str, kickoff_local: str, league: str, rec_label: str, odds: float, ev_pct: float,
              p_model: float, p_market: float, sections: list[Section], context_lines: list[str]) -> str:
    """Hotovy text analyzy, kterou lze vlozit do komunity kancelare."""
    body = [f"{name} ({league}, {kickoff_local})", ""]
    for s in sections[:4]:
        body.append(s["title"] + ":")
        body += [f"• {line}" for line in s["lines"]]
        body.append("")
    if context_lines:
        body.append("Sestavy a absence:")
        body += [f"• {line}" for line in context_lines]
        body.append("")
    body.append(f"Tip: {rec_label} @ {odds:.2f}. Model dává {p_model * 100:.1f} %, trh {p_market * 100:.1f} % (férový kurz {1 / p_market:.2f}), "
                f"očekávaná hodnota {ev_pct:+.1f} %.")
    return "\n".join(body)
