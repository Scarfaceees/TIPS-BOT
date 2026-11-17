"""Webovy panel: prehled, tipy, tikety, kalkulacka hodnoty, historie, nastaveni."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

try:
    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo("Europe/Prague")
except Exception:  # chybi tzdata
    _TZ = None


def _local(dt: datetime) -> str:
    """UTC -> cesky cas (kdyz chybi casova databaze, zustane UTC)."""
    if _TZ is None:
        return dt.strftime("%d.%m. %H:%M UTC")
    return dt.replace(tzinfo=timezone.utc).astimezone(_TZ).strftime("%d.%m. %H:%M")


from flask import Flask, render_template_string, request, url_for
from sqlalchemy import select

from . import analysis, auto, backtest, combo, czechodds, detail, devig, metrics, paper, profile2, risk, secondopinion
from .config import LEAGUES, SETTINGS
from .storage import RESULTS, TICKET_OPEN, Ledger, Ticket, Tip, make_session_factory, utcnow

TABS = (("prehled", "Přehled"), ("tikety", "Moje tikety"), ("sledovani", "Sledování modelu"),
        ("historie", "Historie"), ("overeni", "Ověření modelu"), ("profil2", "Profil 2"), ("kalkulacka", "Kalkulačka"), ("nastaveni", "Nastavení"))
TAB_ALIASES = {"sazka": "tikety"}  # stare odkazy

PAGE = """<!doctype html>
<html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tips Bot</title>
<style>
:root{--bg:#f3f5f8;--card:#fff;--line:#dfe4ea;--text:#18212b;--muted:#64707d;--ok:#16794a;--okbg:#e4f5ec;
--bad:#b42318;--badbg:#fdeceb;--acc:#1d5fd1;--accbg:#e8f0fd;--warn:#8a5a00;--warnbg:#fff3d6}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}
header{background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.wrap{max-width:1640px;margin:0 auto;padding:0 16px}
.top{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;padding:12px 0 6px}.top h1{font-size:20px;margin:0}
.chips{margin-left:auto;display:flex;gap:14px;font-size:14px;color:var(--muted)}.chips b{color:var(--text)}
nav{display:flex;gap:2px;overflow-x:auto}nav a{padding:9px 12px;text-decoration:none;color:var(--muted);border-bottom:3px solid transparent;white-space:nowrap;font-weight:600}
nav a.on{color:var(--acc);border-color:var(--acc)}nav a:hover{color:var(--text)}
nav a .cnt{display:inline-block;min-width:18px;padding:0 5px;margin-left:4px;border-radius:99px;background:var(--bad);color:#fff;font-size:12px;text-align:center}
main{padding:18px 0 48px}h2{font-size:17px;margin:24px 0 10px;display:flex;align-items:center;gap:8px}h2:first-child{margin-top:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:10px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.stat span{display:block;color:var(--muted);font-size:13px}.stat b{font-size:20px}.stat small{display:block;color:var(--muted);font-size:13px}
.muted{color:var(--muted);font-size:14px}.pos{color:var(--ok)}.neg{color:var(--bad)}
.badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600;background:var(--accbg);color:var(--acc)}
.badge.warn{background:var(--warnbg);color:var(--warn)}.badge.bad{background:var(--badbg);color:var(--bad)}.badge.tipb{background:var(--ok);color:#fff}
.msg{padding:11px 14px;border-radius:8px;margin-bottom:14px;background:var(--okbg);color:var(--ok);font-weight:600}
.msg.err{background:var(--badbg);color:var(--bad)}.note{background:var(--warnbg);color:var(--warn);border-color:#f0d99a}
.note a{color:var(--warn);font-weight:700}
.tip{display:grid;grid-template-columns:1fr auto;gap:8px 20px;align-items:start}
.title{font-size:16px;font-weight:700}.pick{font-size:15px;margin-top:2px}
.kv{display:flex;flex-wrap:wrap;gap:4px 18px;margin-top:6px;color:var(--muted);font-size:14px}.kv b{color:var(--text)}
.big{font-size:22px;font-weight:700;text-align:right}.big small{display:block;font-size:13px;font-weight:400;color:var(--muted)}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin-top:12px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
label{font-size:13px;color:var(--muted);display:flex;flex-direction:column;gap:4px;flex:1 1 130px}
input,select,textarea{background:#fff;color:var(--text);border:1px solid #c5ccd5;border-radius:7px;padding:8px 10px;font-size:15px;width:100%}
input:focus,select:focus{outline:2px solid var(--accbg);border-color:var(--acc)}
button{background:var(--acc);color:#fff;border:0;border-radius:7px;padding:9px 15px;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap}
button.ghost{background:#eef1f5;color:var(--text)}button.ok{background:var(--ok)}button.bad{background:var(--bad)}button.sm{padding:5px 10px;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}th{color:var(--muted);font-weight:600;font-size:13px}
td,th{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}tr:last-child td{border-bottom:0}td.num,th.num{text-align:right}
.tablewrap{overflow-x:auto}
.verdict{margin-top:14px;padding:14px;border-radius:9px;background:var(--badbg)}.verdict.yes{background:var(--okbg)}
.verdict b.v{font-size:19px}.help{margin:0 0 4px;padding-left:18px;color:var(--muted);font-size:14px}
.bar{display:flex;flex-wrap:wrap;gap:10px 24px;align-items:end;justify-content:space-between}
.legend{font-size:13px;color:var(--muted)}.note-inline{color:var(--warn)}
details>summary{cursor:pointer;list-style:none}details>summary::-webkit-details-marker{display:none}
details.fold>summary{font-weight:600;color:var(--acc);padding:4px 0}details.fold>summary::before{content:"▸ "}details.fold[open]>summary::before{content:"▾ "}
.filters{display:flex;gap:6px;flex-wrap:wrap}.filters button{background:#eef1f5;color:var(--text)}.filters button.on{background:var(--acc);color:#fff}
.day{font-size:13px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin:16px 0 6px}
.match{padding:0;overflow:hidden;margin-bottom:8px}
.ms{display:grid;grid-template-columns:52px 1fr auto 18px;gap:12px;align-items:center;padding:11px 14px}
.ms:hover{background:#f8fafc}.ms .time{font-weight:700;font-size:15px}.ms .nm{font-weight:700}.ms .nm small{display:block;font-weight:400;color:var(--muted);font-size:13px}
.ms .rc{text-align:right;font-size:14px}.ms .rc small{display:block;color:var(--muted);font-size:12px}
.ms .arr{color:var(--muted);transition:transform .15s}.match[open] .arr{transform:rotate(90deg)}
.match[open]>.ms{border-bottom:1px solid var(--line)}
.match.k-tip{border-left:4px solid var(--ok)}.match.k-value{border-left:4px solid var(--acc)}
.mbody{padding:4px 14px 12px}table.mk td,table.mk th{white-space:nowrap}table.mk td,table.mk th{padding:7px 6px}table.mk td:first-child{white-space:normal;min-width:140px}table.mk td:last-child{font-size:13px}tr.best td{background:#f4f9ff;font-weight:600}
.det h4{margin:12px 0 4px}.det ul{margin:0;padding-left:18px}.det textarea{font:13px/1.4 monospace}
.tipbox{margin-top:10px;padding:12px;border-radius:8px;background:#f3faf5;border:1px solid #cfe9d9}.tipbox.alt{background:#f4f9ff;border-color:#d6e4fb}
.scorebox{display:flex;flex-wrap:wrap;gap:10px;align-items:end}.scorebox label{flex:2 1 240px}.scorebox label.sc{flex:0 0 76px}.scorebox .colon{font-size:22px;font-weight:700;padding-bottom:6px}
.profbar{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;margin-bottom:14px}
.prof{display:block;text-decoration:none;color:var(--text);background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:10px;padding:10px 14px}
.prof:hover{border-color:var(--acc)}.prof.on{border-left-color:var(--acc);background:var(--accbg)}.prof.off{opacity:.75}
.prof small{display:block;color:var(--muted);font-size:13px;margin:2px 0 4px}.prof .pst{font-size:13px;color:var(--muted)}
.card.sec-acc{border-top:3px solid var(--acc)}.card.sec-ok{border-top:3px solid var(--ok)}.card.sec-warn{border-top:3px solid #e0a800}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}.tile{border-radius:8px;padding:8px 10px;background:#f6f8fb}.tile span{display:block;font-size:12px;color:var(--muted)}.tile b{font-size:18px}.tile.good{background:var(--okbg)}.tile.good b{color:var(--ok)}.tile.bad{background:var(--badbg)}.tile.bad b{color:var(--bad)}
table.mkt td,table.mkt th{padding:5px 6px}.res{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:700}.res.won{background:var(--okbg);color:var(--ok)}.res.lost{background:var(--badbg);color:var(--bad)}.res.void{background:#eef1f5;color:var(--muted)}
.edge{display:inline-block;padding:1px 8px;border-radius:99px;font-weight:700;background:var(--okbg);color:var(--ok)}.edge.hi{background:var(--warnbg);color:var(--warn)}
tr.r-won td:first-child{box-shadow:inset 3px 0 var(--ok)}tr.r-lost td:first-child{box-shadow:inset 3px 0 var(--bad)}
.czgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px;margin-top:10px}
.czgrid fieldset{border:1px solid var(--line);border-radius:8px;padding:6px 10px 10px;margin:0;display:flex;gap:8px}
.czgrid legend{font-size:13px;font-weight:600;padding:0 4px}.czgrid label{flex:1 1 0;min-width:0}.czgrid input{padding:6px 8px}
.betrow{display:flex;gap:6px;align-items:center;margin:0}.betrow input{width:78px;padding:5px 7px}.betrow .muted{min-width:56px}
table.bets td{vertical-align:middle}
.cols{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:18px;align-items:start}
.cols>aside{grid-column:2;grid-row:1;min-width:0}.cols>.colmain{grid-column:1;grid-row:1;min-width:0}
.cols>aside .stats{grid-template-columns:repeat(2,1fr)}.cols>aside h2{margin-top:16px}.cols>aside>h2:first-child,.colmain>h2:first-child{margin-top:0}
.mgrid{display:grid;grid-template-columns:minmax(0,1fr) 350px;gap:14px;align-items:start;margin-top:8px}
.czbox{border:1px solid var(--line);border-radius:8px;padding:8px 12px;background:#fafbfd}.czbox .czgrid{grid-template-columns:1fr;gap:6px}.czbox fieldset{align-items:end;padding:2px 10px 8px}
.boxes{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px;margin-top:8px;align-items:start}
.box{border:1px solid var(--line);border-radius:8px;padding:8px 12px;background:#fff}.box h4{margin:0 0 4px}.box ul{margin:0;padding-left:18px}.box p{margin:0}
.box table td,.box table th{padding:5px 6px}.box tr.win td{font-weight:700;background:#f3faf5}
.sec2{font-size:12px;color:var(--muted)}.sec2.gap{color:var(--warn);font-weight:700}
.setgrid{columns:2 460px;column-gap:20px}.setgrid>section{break-inside:avoid;margin-bottom:6px}.setgrid>section>h2{margin-top:0}
@media(max-width:1250px){.cols{grid-template-columns:1fr}.cols>aside,.cols>.colmain{grid-column:1;grid-row:auto}.cols>aside .stats{grid-template-columns:repeat(4,1fr)}}
.mgrid.one{grid-template-columns:1fr}.mgrid.one .czbox{display:none}
@media(max-width:1000px){.mgrid{grid-template-columns:1fr}}
@media(max-width:720px){.cols>aside .stats{grid-template-columns:repeat(2,1fr)}.stats{grid-template-columns:repeat(2,1fr)}.tip{grid-template-columns:1fr}.big{text-align:left}
.ms{grid-template-columns:44px 1fr 14px}.ms .rc{grid-column:2;text-align:left}.chips{margin-left:0;width:100%}}
</style></head><body>

{% macro mrow(r, v) %}<tr class="{{ 'best' if r is sameas v.rows[v.recommended] or (v.target is not none and r is sameas v.rows[v.target]) }}">
<td>{{ r.label }}</td><td class="num">{{ '%.0f'|format(r.p_final * 100) }} %</td>
<td class="num">{% if r.p_second is not none %}{% set gap = (r.p_second - r.p_model)|abs >= second_gap %}<span class="sec2 {{ 'gap' if gap }}" title="náš model {{ '%.0f'|format(r.p_model * 100) }} %, druhý model {{ '%.0f'|format(r.p_second * 100) }} %{{ ' – modely se rozcházejí, opatrně' if gap }}">{{ '%.0f'|format(r.p_second * 100) }} %{{ ' ⚠' if gap }}</span>{% else %}<span class="muted">–</span>{% endif %}</td>
<td class="num">{{ '%.2f'|format(r.fair_odds) }}</td>
<td class="num">{% if r.status == 'model' %}<b>{{ '%.2f'|format(r.min_odds) }}</b>{% else %}<span class="muted">–</span>{% endif %}</td>
<td class="num">{% if r.status != 'model' %}{{ '%.2f'|format(r.best_odds) }} <span class="muted">{{ r.best_book }}</span>{% else %}<span class="muted">–</span>{% endif %}</td>
<td class="num">{% if r.cz %}<b>{{ '%.2f'|format(r.cz[0]) }}</b> <span class="muted">{{ r.cz[1] }}</span>{% else %}<span class="muted">nezadáno</span>{% endif %}</td>
<td class="num {{ 'pos' if r.cz_ev is not none and r.cz_ev > 0 else 'neg' if r.cz_ev is not none else '' }}">{{ '%+.1f %%'|format(r.cz_ev * 100) if r.cz_ev is not none else ('%+.1f %%'|format(r.ev_pct) if r.status != 'model' else '–') }}</td>
<td>{% if r.cz_ok %}<span class="badge tipb">VSADIT</span>{% elif r.cz_suspect %}<span class="badge warn" title="čekaný zisk nad 15 % – zkontroluj, že kurz patří k tomuto trhu">ověř kurz</span>{% elif r.status == 'tip' %}<span class="badge tipb">TIP</span>{% elif r.status == 'value' %}<span class="badge">VÝHODNÉ</span>{% elif r.cz %}<span class="badge bad">nízký kurz</span>{% endif %}</td></tr>{% endmacro %}

{% macro score_form(back, kind) %}{% set rows = pending|selectattr(kind)|list %}
{% if rows %}<div class="card sec-acc"><b>Zadat výsledek zápasu</b>
<div class="muted">Vyber skóre po 90 minutách (domácí : hosté). Bot podle něj vyhodnotí všechny otevřené tikety i sledované výběry toho zápasu.</div>
<form class="scorebox" style="margin-top:10px" method="post" action="{{ url_for('score') }}"><input type="hidden" name="tab" value="{{ back }}">
<label>Zápas<select name="match">{% for p in rows %}<option value="{{ p.key }}">{{ p.day_label }} · {{ p.name }} ({{ p.what }})</option>{% endfor %}</select></label>
<label class="sc">Domácí<select name="home">{% for g in range(11) %}<option>{{ g }}</option>{% endfor %}</select></label><span class="colon">:</span>
<label class="sc">Hosté<select name="away">{% for g in range(11) %}<option>{{ g }}</option>{% endfor %}</select></label>
<button class="ok">Vyhodnotit</button></form></div>{% endif %}
{% endmacro %}

<header><div class="wrap">
<div class="top"><h1>Tips Bot</h1>
{% if s.test_mode %}<span class="badge warn">TESTOVACÍ REŽIM</span>{% endif %}
{% if s.paused %}<span class="badge bad">STOP-LOSS</span>{% endif %}
<div class="chips"><span>Bankroll <b>{{ '%.0f'|format(s.bankroll) }} {{ cur }}</b></span>
<span>Zisk <b class="{{ 'pos' if s.profit_loss >= 0 else 'neg' }}">{{ '%+.0f'|format(s.profit_loss) }} {{ cur }}</b></span></div></div>
<nav>{% for key, label in tabs %}<a href="{{ url_for('index', tab=key) }}" class="{{ 'on' if tab == key }}">{{ label }}{% set n = (pending|selectattr('tikety')|list|length) if key == 'tikety' else (pending|selectattr('vybery')|list|length) if key == 'sledovani' else 0 %}{% if n %}<span class="cnt" title="zápasy čekající na výsledek">{{ n }}</span>{% endif %}</a>{% endfor %}</nav>
</div></header>
<main><div class="wrap">
{% if msg %}<div class="msg {{ 'err' if err else '' }}">{{ msg }}</div>{% endif %}

{% if tab == 'prehled' %}
<div class="card sec-acc autobar"><b>Automat</b> {% if auto_st %}<span class="muted">naposledy {{ auto_st.t[:16].replace('T', ' ') }} UTC – sám načítá kurzy na 48 h dopředu, všechny profily vybírají sázky do Sledování, odehrané zápasy vyhodnocuje, jednou týdně ověří model (každých 6 h)</span>
<details class="fold"><summary>Co udělal ({{ auto_st.notes|length }})</summary><div class="muted">{% for n in auto_st.notes %}<div>{{ n }}</div>{% endfor %}</div></details>{% else %}<span class="muted">zatím neproběhl (spouští se v Dockeru jako služba tipsbot-auto)</span>{% endif %}
<form class="row" method="post" action="{{ url_for('auto_run') }}" style="margin-top:6px">{% if auto_req %}<span class="badge warn">spustí se do minuty</span>{% else %}<button class="ghost sm">Spustit automat teď</button>{% endif %}</form></div>
<div class="cols"><aside>
<div class="stats">
  <div class="stat"><span>Bankroll</span><b>{{ '%.0f'|format(s.bankroll) }} {{ cur }}</b><small>dnes vsazeno {{ '%.0f'|format(exposure) }} z {{ '%.0f'|format(exposure_cap) }}</small></div>
  <div class="stat"><span>Zisk / ztráta</span><b class="{{ 'pos' if s.profit_loss >= 0 else 'neg' }}">{{ '%+.0f'|format(s.profit_loss) }} {{ cur }}</b><small>výnos {{ '%+.2f'|format(s.yield_pct) }} % z vsazeného</small></div>
  <div class="stat"><span>Sázky</span><b>{{ '%.0f'|format(s.settled_bets) }}</b><small>úspěšnost {{ '%.1f'|format(s.win_rate_pct) }} % · {{ '%.0f'|format(s.open_bets) }} otevřených</small></div>
  <div class="stat"><span title="o kolik lepší kurz jsi vzal než ten těsně před zápasem; dlouhodobě v plusu = sázíš chytře">Lepší než závěrečný kurz</span><b>{{ '%+.2f'|format(s.avg_clv_pct) }} %</b><small>propad {{ '%.1f'|format(s.drawdown_pct) }} % (limit 15 %)</small></div>
</div>
{% if s.bankroll <= 0 %}<div class="card note"><b>Nastav bankroll</b> – bez něj bot nespočítá výši vkladu.
<form class="row" method="post" action="{{ url_for('deposit') }}">
<label>Částka ({{ cur }})<input name="amount" type="number" step="1" min="1" required></label>
<label>Režim<select name="test"><option value="1">Testovací (virtuální peníze)</option><option value="0">Ostrý</option></select></label>
<button>Uložit</button></form></div>{% endif %}
{% if s.paused %}<div class="card note">Propad dosáhl limitu, tipy jsou pozastavené do re-kalibrace.
<form class="row" method="post" action="{{ url_for('resume') }}"><button>Obnovit vydávání tipů</button></form></div>{% endif %}
{% set nt = pending|selectattr('tikety')|list|length %}{% set nv = pending|selectattr('vybery')|list|length %}
{% if nt or nv %}<div class="card note">Na výsledek čeká:
{% if nt %}<a href="{{ url_for('index', tab='tikety') }}">{{ nt }} zápasů s tvými tikety →</a>{% endif %}
{% if nv %}<a href="{{ url_for('index', tab='sledovani') }}">{{ nv }} zápasů se sledovanými výběry modelu →</a>{% endif %}</div>{% endif %}

<div class="card"><div class="bar">
{% if has_key %}<form class="row" style="margin:0;flex:1" method="post" action="{{ url_for('scan') }}">
<label style="flex:0 1 260px">Soutěž<select name="league"><option value="all">Všechny soutěže</option>{% for k in scan_leagues %}<option value="{{ k }}">{{ leagues[k].name }}</option>{% endfor %}</select></label>
<label style="flex:0 0 auto;flex-direction:row;align-items:center;gap:6px"><input type="checkbox" name="force" value="1" style="width:auto"> nová data (stojí kredity)</label><button>Načíst kurzy a rozbor</button></form>
{% else %}<div class="note-inline">Živé kurzy nejsou zapnuté. Zadej API klíč v záložce <a href="{{ url_for('index', tab='nastaveni') }}">Nastavení</a>.</div>{% endif %}
</div>
<details class="fold" style="margin-top:8px"><summary>Co znamenají štítky a pojmy</summary><div class="legend"><span class="badge tipb">TIP</span> ověřená hodnota, vsadit &nbsp; <span class="badge tipb">CÍL</span> sázet jen při kurzu od uvedené hranice &nbsp; <span class="badge">VÝHODNÉ</span> kurz je výhodný, ale tip nevznikl &nbsp; <span class="badge bad">NEVÝHODNÉ</span> nesázet</div><div class="legend" style="margin-top:6px"><b>Šance</b> – jak pravděpodobné to podle modelu je · <b>Kurz bez marže</b> – kurz, který by odpovídal skutečné šanci (sázkovka si k němu bere marži) · <b>Sázet od</b> – nejnižší kurz, při kterém se sázka ještě vyplatí · <b>Čekaný zisk</b> – kolik průměrně vyděláš na 100 Kč vsazených, když model sedí (+10 % = +10 Kč) · <b>Lepší než závěrečný kurz</b> – jestli jsi vzal vyšší kurz, než jaký byl těsně před zápasem (dlouhodobě v plusu = sázíš chytře)</div></details></div>

<h2>Doporučený tiket <span class="muted" style="font-weight:400">(ze zahraničních kurzů)</span></h2>
{% for c in combos[:1] %}<div class="card"><div class="tip"><div>
  <div class="title">{{ c.legs|length }}× kombinace <span class="badge tipb">nejlepší</span></div>
  {% for lg in c.legs %}<div class="pick">{{ loop.index }}. <b>{{ lg.row.label }}</b> – {{ lg.match }} @ {{ '%.2f'|format(lg.row.best_odds) }} <span class="muted">({{ lg.row.best_book }}, výkop {{ lg.local }})</span></div>{% endfor %}
  <div class="kv"><span>Šance tiketu <b>{{ '%.1f'|format(c.prob * 100) }} %</b></span><span title="kurz, který odpovídá skutečné šanci – bez marže, kterou si bere sázková kancelář">Kurz bez marže <b>{{ '%.2f'|format(c.fair_odds) }}</b></span><span title="kolik průměrně vyděláš na každých 100 Kč vsazených, pokud odhad modelu sedí (+10 % = +10 Kč na 100 Kč)">Čekaný zisk <b>{{ '%+.1f'|format(c.ev_pct) }} %</b></span></div></div>
  <div class="big">{{ '%.2f'|format(c.odds) }}<small>celkový kurz · vklad {{ '%.0f'|format(c.stake) }} {{ cur }}</small></div></div>
  <div class="muted" style="margin-top:6px">Sázet jen, pokud kancelář dá celkový kurz alespoň <b>{{ '%.2f'|format(c.min_odds) }}</b>. Každá sázka je z jiného zápasu.</div></div>
{% else %}<div class="card muted">Zatím není z čeho tiket složit – potřebuju aspoň dvě příležitosti s hodnotou z různých zápasů.</div>{% endfor %}
{% if combos|length > 1 %}<details class="fold card"><summary>Další kombinace ({{ combos|length - 1 }})</summary>
<div class="tablewrap"><table><tr><th>Sázky</th><th class="num">Kurz</th><th class="num">Šance</th><th class="num" title="kolik průměrně vyděláš na každých 100 Kč vsazených, pokud odhad modelu sedí (+10 % = +10 Kč na 100 Kč)">Čekaný zisk</th><th class="num">Min. kurz</th><th class="num">Vklad</th></tr>
{% for c in combos[1:] %}<tr><td>{% for lg in c.legs %}<div><b>{{ lg.row.label }}</b> – {{ lg.match }} @ {{ '%.2f'|format(lg.row.best_odds) }} <span class="muted">{{ lg.local }}</span></div>{% endfor %}</td>
<td class="num"><b>{{ '%.2f'|format(c.odds) }}</b></td><td class="num">{{ '%.1f'|format(c.prob * 100) }} %</td><td class="num">{{ '%+.1f'|format(c.ev_pct) }} %</td><td class="num">{{ '%.2f'|format(c.min_odds) }}</td><td class="num">{{ '%.0f'|format(c.stake) }}</td></tr>{% endfor %}
</table></div></details>{% endif %}
</aside><div class="colmain">
<h2>Co vsadit</h2>
{% if bets %}<div class="card tablewrap" style="padding:6px 10px"><table class="bets"><tr><th>Výkop</th><th>Zápas</th><th>Sázka</th><th class="num">Šance</th><th class="num" title="kolik průměrně vyděláš na každých 100 Kč vsazených, pokud odhad modelu sedí (+10 % = +10 Kč na 100 Kč)">Čekaný zisk</th><th>Kurz · kancelář</th><th>Vklad</th><th></th></tr>
{% for b in bets %}{% set v = b.view %}{% set r = b.row %}<tr>
<td>{{ v.local }}</td><td><b>{{ v.name }}</b>{% if r.status == 'model' %}<div class="muted">jen model, bez férové linie trhu – ve zpětném testu model na gólech trh neporazil, sázej opatrně</div>{% endif %}</td><td><b>{{ r.label }}</b></td>
<td class="num">{{ '%.0f'|format(r.p_final * 100) }} %</td><td class="num pos">{{ '%+.1f'|format(r.cz_ev * 100) }} %</td>
<td colspan="3"><form class="betrow" method="post" action="{{ url_for('ticket_add') }}">
<input type="hidden" name="match_name" value="{{ v.name }}"><input type="hidden" name="match_day" value="{{ b.day }}">
<input type="hidden" name="market" value="{{ r.market }}"><input type="hidden" name="selection" value="{{ r.selection }}"><input type="hidden" name="bookmaker" value="{{ r.cz[1] }}">
<input name="odds" type="number" step="0.01" min="1.01" value="{{ '%.2f'|format(r.cz[0]) }}" title="kurz, za který jsi vsadil" required><span class="muted">{{ r.cz[1] }}</span>
<input name="stake" type="number" step="10" min="1" value="{{ '%.0f'|format(b.stake) if b.stake else '' }}" placeholder="vklad" required><button class="ok sm">Vsazeno</button></form></td></tr>{% endfor %}</table>
<div class="muted" style="margin:6px 4px">Sázej jen za uvedený kurz nebo vyšší. Klikem na „Vsazeno“ se sázka zapíše do Moje tikety.</div></div>
{% else %}<div class="card muted">Na zadaných českých kurzech teď nic nemá hodnotu. Zadej kurzy u zápasů (rozbal zápas → Zadat kurzy z české kanceláře).</div>{% endif %}
{% if check %}<details class="fold card"><summary>Ověř kurz ({{ check|length }}) – čekaný zisk nad 15 %, nejspíš chyba v kurzu nebo modelu</summary>
<table>{% for b in check %}<tr><td>{{ b.view.local }}</td><td>{{ b.view.name }}</td><td>{{ b.row.label }}</td><td class="num">{{ '%.2f'|format(b.row.cz[0]) }}</td><td class="num">{{ '%+.1f'|format(b.row.cz_ev * 100) }} %</td></tr>{% endfor %}</table>
<div class="muted">Model na trzích s góly zatím nemá ověřenou přesnost (Sledování modelu). Takhle velký čekaný zisk proti kanceláři bývá spíš chyba než příležitost.</div></details>{% endif %}

<h2>Zápasy ({{ views|length }})</h2>
<div class="filters" id="flt" style="margin-bottom:6px">
{% for key, name in (('all', 'Vše'), ('cz', 'Vsadit podle českých kurzů'), ('tip', 'Tipy'), ('cil', 'Cíle – ověř kurz'), ('value', 'Výhodné'), ('none', 'Nevýhodné')) %}
{% set n = views|length if key == 'all' else tag_counts.get(key, 0) %}
{% if n %}<button type="button" data-f="{{ key }}" class="sm {{ 'on' if key == 'all' }}">{{ name }} ({{ n }})</button>{% endif %}{% endfor %}</div>
{% for v in views %}{% set rec = v.rows[v.recommended] %}{% set tip = tips_by_match.get(v.match_id) %}
{% if loop.changed(v.local[:6]) %}<div class="day" data-day>{{ v.local[:6] }}</div>{% endif %}
<details class="card match k-{{ 'tip' if 'tip' in v.tags or 'cz' in v.tags or 'cil' in v.tags else 'value' if 'value' in v.tags else 'none' }}" data-tags="{{ v.tags|join(' ') }}" {{ 'open' if tip }}>
  <summary class="ms"><span class="time">{{ v.local[7:] }}</span>
    <span class="nm">{{ v.name }}<small>{{ leagues[v.league].name if v.league in leagues else v.league }}{% if not v.has_model %} · <span class="neg">bez modelu, jen trh</span>{% endif %}{% if v.cz_count %} · české kurzy: {{ v.cz_count }}{% endif %}</small></span>
    {% if v.cz_best %}<span class="rc"><b>{{ v.cz_best.label }}</b> @ {{ '%.2f'|format(v.cz_best.cz[0]) }} <span class="badge tipb">VSADIT</span><small>{{ v.cz_best.cz[1] }} · čekaný zisk {{ '%+.1f'|format(v.cz_best.cz_ev * 100) }} %</small></span>
    {% elif v.verdict == 'CÍL' %}{% set tg = v.rows[v.target] %}<span class="rc"><b>{{ tg.label }}</b> <span class="badge tipb">CÍL</span><small>sázet od kurzu {{ '%.2f'|format(tg.min_odds) }} · šance {{ '%.0f'|format(tg.p_model * 100) }} %</small></span>
    {% else %}<span class="rc"><b>{{ rec.label }}</b> @ {{ '%.2f'|format(rec.best_odds) }} <span class="badge {{ 'tipb' if v.verdict == 'TIP' else ('bad' if v.verdict.startswith('NEVÝ') else '') }}">{{ v.verdict }}</span><small>čekaný zisk {{ '%+.1f'|format(rec.ev_pct) }} % · {{ rec.best_book }}</small></span>{% endif %}
    <span class="arr">›</span></summary>
  <div class="mbody">
  {% if tip %}<div class="tipbox"><b>Tip: {{ tip.market }} → {{ tip.selection }}</b>{% if tip.needs_review %} <span class="badge warn">zkontrolovat kurz</span>{% endif %}{% if tip.early %} <span class="badge">časný</span>{% endif %}
    <div class="muted">Sázet jen při kurzu alespoň <b>{{ '%.2f'|format(tip.min_odds) }}</b> · doporučený vklad <b>{{ '%.0f'|format(tip.kelly_stake) }} {{ cur }}</b> · platí do {{ tip.valid_until.strftime('%H:%M') }} UTC</div>
  <form class="row" method="post" action="{{ url_for('ticket_add') }}"><input type="hidden" name="tip_id" value="{{ tip.id }}">
    <label>Kancelář<select name="bookmaker">{% for b in books %}<option>{{ b }}</option>{% endfor %}</select></label>
    <label>Skutečný kurz<input name="odds" type="number" step="0.01" min="1.01" required></label>
    <label>Vklad ({{ cur }})<input name="stake" type="number" step="1" value="{{ '%.0f'|format(tip.kelly_stake) }}" required></label>
    <button class="ok">Vsazeno</button><button class="ghost" formaction="{{ url_for('tip_cancel', tip_id=tip.id) }}" formnovalidate>Nevsadím</button></form></div>
  {% if partners.get(v.name) %}{% set pc = partners[v.name] %}<div class="tipbox alt"><b>Doplnit tiket o:</b>
    {% for lg in pc.legs if lg.match != v.name %}<b>{{ lg.row.label }}</b> – {{ lg.match }} @ {{ '%.2f'|format(lg.row.best_odds) }}{% endfor %}
    <span class="muted">→ celkový kurz {{ '%.2f'|format(pc.odds) }}, šance {{ '%.1f'|format(pc.prob * 100) }} %, čekaný zisk {{ '%+.1f'|format(pc.ev_pct) }} %, vklad {{ '%.0f'|format(pc.stake) }} {{ cur }}</span></div>{% endif %}{% endif %}

  <div class="mgrid{{ ' one' if v.league in leagues and leagues[v.league].sport.value == 'nhl' }}"><div><div class="tablewrap"><table class="mk"><tr><th>Trh</th><th class="num">Šance</th><th class="num" title="nezávislý druhý model (penaltyblog) jako kontrola">2. model</th><th class="num" title="kurz, který odpovídá skutečné šanci – bez marže, kterou si bere sázková kancelář">Bez marže</th><th class="num">Sázet od</th><th class="num">Zahraničí</th><th class="num">Česká kancelář</th><th class="num" title="kolik průměrně vyděláš na každých 100 Kč vsazených, pokud odhad modelu sedí (+10 % = +10 Kč na 100 Kč)">Čekaný zisk</th><th></th></tr>
  {% for r in v.main_rows %}{{ mrow(r, v) }}{% endfor %}</table></div>
  {% if v.more_rows %}<details class="fold"><summary>Další trhy podle modelu ({{ v.more_rows|length }})</summary>
  <div class="tablewrap"><table class="mk"><tr><th>Trh</th><th class="num">Šance</th><th class="num" title="nezávislý druhý model (penaltyblog) jako kontrola">2. model</th><th class="num" title="kurz, který odpovídá skutečné šanci – bez marže, kterou si bere sázková kancelář">Bez marže</th><th class="num">Sázet od</th><th class="num">Zahraničí</th><th class="num">Česká kancelář</th><th class="num" title="kolik průměrně vyděláš na každých 100 Kč vsazených, pokud odhad modelu sedí (+10 % = +10 Kč na 100 Kč)">Čekaný zisk</th><th></th></tr>
  {% for r in v.more_rows %}{{ mrow(r, v) }}{% endfor %}</table></div></details>{% endif %}

  </div><div class="czbox"><b>Kurzy z české kanceláře</b>
  <form method="post" action="{{ url_for('czech_odds_match') }}" class="czf" data-prefill='{{ v.cz_books|tojson }}'>
  <input type="hidden" name="match" value="{{ v.name }}"><input type="hidden" name="league" value="{{ v.league }}">
  <div class="row" style="margin-top:4px"><label style="flex:0 1 200px">Kancelář<select name="book">{% for b in czech_books %}<option value="{{ b }}">{{ b }}</option>{% endfor %}</select></label>
  <span class="muted" style="flex:1 1 160px">Vyplň jen trhy, které chceš (trh vždy celý). Přepočet nestojí kredity.</span></div>
  <div class="czgrid">{% for m, sels, names in form_markets %}<fieldset><legend>{{ market_names[m] }}</legend>
    {% for s in sels %}<label>{{ names[loop.index0] }}<input name="{{ m }}:{{ s }}" inputmode="decimal" autocomplete="off"></label>{% endfor %}</fieldset>{% endfor %}</div>
  <div class="row"><button class="ok">Uložit kurzy a přepočítat</button></div></form></div></div>

  <details class="fold det"><summary>Podrobná analýza a podklady</summary>
    <div class="boxes">{% for sec in v.sections %}<div class="box"><h4>{{ sec.title }}</h4><ul>{% for l in sec.lines %}<li>{{ l }}</li>{% endfor %}</ul></div>{% endfor %}
    {% if not v.sections %}<div class="box"><p class="muted">Model nemá pro tento zápas data (Nastavení → Aktualizovat data).</p></div>{% endif %}
    <div class="box"><h4>Absence a zranění</h4>
    {% if v.absences %}<ul>{% for l in v.absences %}<li>{{ l }}</li>{% endfor %}</ul>
    {% else %}<p class="muted">{{ 'Žádné nahlášené absence u těchto týmů.' if v.absence_checked else 'Zdroj absencí nedostupný (u fotbalu zadej API-Football klíč v Nastavení).' }} Sestavy se zveřejňují až těsně před zápasem – model je zatím nezapočítává.</p>{% endif %}</div>
    {% if v.weather %}<div class="box"><h4>Počasí při výkopu</h4><p>{{ v.weather }}</p></div>{% endif %}
    {% if v.news %}<div class="box"><h4>Poslední zprávy (titulky)</h4><ul>{% for l in v.news %}<li>{{ l }}</li>{% endfor %}</ul></div>{% endif %}</div>
    <h4>Text analýzy ke zkopírování</h4><textarea readonly rows="9" onclick="this.select()">{{ v.post }}</textarea>
  </details></div>
</details>
{% else %}<div class="card muted">Zatím žádné zápasy. Klikni na „Načíst kurzy a rozbor".</div>{% endfor %}
{% for t in orphan_tips %}<div class="card"><b>{{ t.match_name }}</b> – tip {{ t.market }} → {{ t.selection }} @ {{ '%.2f'|format(t.raw_odds) }}
<form class="row" method="post" action="{{ url_for('tip_cancel', tip_id=t.id) }}"><button class="ghost">Zrušit tip</button></form></div>{% endfor %}
</div></div>
<script>
(function(){var f=document.getElementById('flt');
 if(f)f.addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;var k=b.dataset.f;
  f.querySelectorAll('button').forEach(function(x){x.classList.toggle('on',x===b)});
  document.querySelectorAll('details.match').forEach(function(m){
   m.style.display=(k=='all'||m.dataset.tags.split(' ').indexOf(k)>=0)?'':'none'});
  document.querySelectorAll('[data-day]').forEach(function(h){var n=h.nextElementSibling,vis=false;
   while(n&&!n.hasAttribute('data-day')){if(n.matches('details.match')&&n.style.display!='none')vis=true;n=n.nextElementSibling}
   h.style.display=vis?'':'none'});});
 document.querySelectorAll('form.czf').forEach(function(form){var data=JSON.parse(form.dataset.prefill||'{}'),sel=form.elements.book;
  var books=Object.keys(data);if(books.length)sel.value=books[0];
  function fill(){var d=data[sel.value]||{};form.querySelectorAll('.czgrid input').forEach(function(i){i.value=d[i.name]||''})}
  sel.addEventListener('change',fill);fill();});
})();
</script>

{% elif tab == 'tikety' %}
<div class="cols"><aside>
{{ score_form('tikety', 'tikety') }}
<h2>Zapsat sázku</h2>
<div class="card"><div class="muted">Pro sázky mimo tipy bota. Když zvolíš trh ze seznamu, bot ji po zadání skóre vyhodnotí sám.</div>
<form method="post" action="{{ url_for('ticket_add') }}"><input type="hidden" name="tab" value="tikety"><div class="grid2" style="margin-top:12px">
  <label>Zápas (Domácí - Hosté)<input name="match_name" placeholder="Kypr - Černá Hora" required></label>
  <label>Den zápasu<input name="match_day" type="date" value="{{ today }}" required></label>
  <label>Trh<select name="market"><option value="1X2">Výsledek (1 / X / 2)</option><option value="OU">Počet gólů (over / under)</option><option value="BTTS">Oba dají gól (ano / ne)</option><option value="DC">Dvojitá šance (1X / 12 / X2)</option><option value="DNB">Bez remízy (home / away)</option><option value="JINY">Jiný trh (vyhodnotíš ručně)</option></select></label>
  <label>Hranice gólů (jen u počtu gólů)<input name="line" placeholder="2.5"></label>
  <label>Výběr<input name="selection" placeholder="1 · over · ano · 1X" required></label>
  <label>Kancelář<select name="bookmaker">{% for b in books %}<option>{{ b }}</option>{% endfor %}</select></label>
  <label>Kurz<input name="odds" type="number" step="0.01" min="1.01" required></label>
  <label>Vklad ({{ cur }})<input name="stake" type="number" step="1" min="1" required></label>
</div><div class="row"><button>Zapsat sázku</button></div></form></div>
</aside><div class="colmain">
<h2>Otevřené tikety ({{ open_tickets|length }})</h2>
<div class="card tablewrap"><table><tr><th>Den</th><th>Zápas</th><th>Sázka</th><th>Kancelář</th><th class="num">Kurz</th><th class="num">Vklad</th><th></th></tr>
{% for k in open_tickets %}<tr><td>{{ k.match_day.strftime('%d.%m.') }}</td><td>{{ k.match_name or k.match_id }}</td><td><b>{{ k.market }} {{ k.selection }}</b></td><td>{{ k.bookmaker }}</td>
<td class="num">{{ '%.2f'|format(k.odds) }}</td><td class="num">{{ '%.0f'|format(k.stake) }}</td>
<td><details class="fold"><summary>ručně</summary><form method="post" action="{{ url_for('ticket_settle', ticket_id=k.id) }}"><input type="hidden" name="tab" value="tikety">
<label>Kurz bez marže těsně před zápasem (nepovinné)<input name="closing" type="number" step="0.01"></label>
<div class="row" style="margin-top:6px"><button class="ok sm" name="result" value="won">Výhra</button><button class="bad sm" name="result" value="lost">Prohra</button><button class="ghost sm" name="result" value="void">Vráceno</button></div></form></details></td></tr>
{% else %}<tr><td colspan="7" class="muted">Žádné otevřené tikety.</td></tr>{% endfor %}</table></div>
<form method="post" action="{{ url_for('settle_all') }}"><input type="hidden" name="tab" value="tikety"><button class="ghost">Stáhnout výsledky a vyhodnotit automaticky</button></form>
</div></div>

{% elif tab == 'sledovani' %}
<div class="profbar">{% for pr in profiles %}{% set st = prof_stats[pr.key] %}
<a class="prof{{ ' on' if pr.key == prof.key }}{{ ' off' if not pr.active }}" href="{{ url_for('index', tab='sledovani', profile=pr.key) }}">
<b>{{ pr.name }}</b>{% if not pr.active %} <span class="badge warn">nenastaven</span>{% endif %}<small>{{ pr.about }}</small>
<span class="pst">{% if st.settled %}{{ st.settled }} vyhodnoceno · zisk <b class="{{ 'pos' if st.profit >= 0 else 'neg' }}">{{ '%+.2f'|format(st.profit) }} j.</b> · ROI <b class="{{ 'pos' if st.roi >= 0 else 'neg' }}">{{ '%+.1f'|format(st.roi * 100) }} %</b>{% else %}zatím nic vyhodnoceno{% endif %}{% if st.open %} · {{ st.open }} čeká{% endif %}</span></a>{% endfor %}</div>
<div class="cols"><aside>
{{ score_form('sledovani', 'vybery') }}
<div class="card sec-ok"><b>Jak model vydělává</b> <span class="muted">({{ prof.name }}: {{ prof.about }}; sázka 1 jednotky bez peněz)</span>
{% if pst.settled %}<div class="tiles">
<div class="tile"><span>Vyhodnoceno</span><b>{{ pst.settled }}</b></div>
<div class="tile"><span>Úspěšnost</span><b>{{ '%.0f'|format(pst.hit * 100) }} %</b></div>
<div class="tile"><span>Průměrný kurz</span><b>{{ '%.2f'|format(pst.avg_odds) }}</b></div>
<div class="tile {{ 'good' if pst.profit >= 0 else 'bad' }}"><span>Zisk</span><b>{{ '%+.2f'|format(pst.profit) }} j.</b></div>
<div class="tile {{ 'good' if pst.roi >= 0 else 'bad' }}"><span>ROI</span><b>{{ '%+.1f'|format(pst.roi * 100) }} %</b></div>
<div class="tile"><span>Model čekal</span><b>{{ '%+.1f %%'|format(pst.avg_ev * 100) if pst.avg_ev is not none else '–' }}</b></div></div>
{% if prec.n %}<div class="muted">Největší propad {{ '%.2f'|format(prec.max_drawdown) }} j. · nejdelší série proher {{ prec.longest_losing }}{% if prec.current_losing %} (teď {{ prec.current_losing }}){% endif %}</div>{% endif %}
{% if pst.settled < 30 %}<div class="muted note-inline">Zatím jen {{ pst.settled }} výběrů, na závěry je to málo (potřeba aspoň 30 až 100).</div>{% endif %}
{% if pst.by_market %}<table class="mkt" style="margin-top:8px"><tr><th>Trh</th><th class="num">Výběrů</th><th class="num">Zisk (j.)</th><th class="num">ROI</th></tr>
{% for m, v in pst.by_market.items() %}<tr><td>{{ m }}</td><td class="num">{{ v[0] }}</td><td class="num {{ 'pos' if v[1] >= 0 else 'neg' }}">{{ '%+.2f'|format(v[1]) }}</td><td class="num"><span class="res {{ 'won' if v[2] >= 0 else 'lost' }}">{{ '%+.1f'|format(v[2] * 100) }} %</span></td></tr>{% endfor %}</table>{% endif %}
{% else %}<div class="muted" style="margin-top:6px">Zatím nic vyhodnoceno ({{ pst.open }} čeká na výsledek).</div>{% endif %}
<form class="row" method="post" action="{{ url_for('settle_all') }}"><input type="hidden" name="tab" value="sledovani"><input type="hidden" name="profile" value="{{ prof.key }}"><button class="ghost">Stáhnout výsledky a vyhodnotit automaticky</button></form></div>
<details class="fold card"><summary>Přidat vlastní výběr do sledování</summary>
<form method="post" action="{{ url_for('paper_add') }}"><input type="hidden" name="tab" value="sledovani"><input type="hidden" name="profile" value="{{ prof.key }}"><div class="grid2" style="margin-top:8px">
  <label>Zápas<input name="name" placeholder="Sparta - Slavia" required></label>
  <label>Den zápasu<input name="day" type="date" value="{{ today }}" required></label>
  <label>Trh (1X2, OU_2.5, BTTS)<input name="market" value="OU_2.5" required></label>
  <label>Výběr (1/X/2, over/under, yes/no)<input name="selection" placeholder="under" required></label>
  <label>Kancelář<select name="book">{% for b in books %}<option>{{ b }}</option>{% endfor %}</select></label>
  <label>Kurz<input name="odds" type="number" step="0.01" min="1.01" required></label>
</div><div class="row"><button>Přidat do sledování</button></div></form></details>
</aside><div class="colmain">
<h2>Čeká na výsledek ({{ paper_open|length }})</h2>
<div class="card sec-warn tablewrap"><table><tr><th>Zápas</th><th>Výběr</th><th class="num">Kurz</th><th class="num">Model</th><th class="num" title="kolik průměrně vyděláš na každých 100 Kč vsazených, pokud odhad modelu sedí (+10 % = +10 Kč na 100 Kč)">Čekaný zisk</th><th></th></tr>
{% for p in paper_open %}<tr><td>{{ p.name }}<div class="muted">{{ p.day }} · {{ p.source }}</div>{% for f in p.flags %}<div><span class="badge warn">{{ f }}</span></div>{% endfor %}{% if p.detail %}<details class="fold"><summary>rozbor</summary><div class="muted">{{ p.detail }}</div></details>{% endif %}</td><td><b>{{ p.market }} {{ p.selection }}</b><div class="muted">{{ p.book }}</div></td><td class="num">{{ '%.2f'|format(p.odds) }}</td>
<td class="num">{{ '%.0f %%'|format(p.p_model * 100) if p.p_model else '–' }}</td><td class="num">{% if p.ev is not none %}<span class="edge {{ 'hi' if p.ev > 0.15 }}">{{ '%+.1f %%'|format(p.ev * 100) }}</span>{% else %}–{% endif %}</td>
<td><details class="fold"><summary>ručně</summary><form method="post" action="{{ url_for('paper_result', pick_id=p.id) }}" class="row" style="margin:4px 0"><input type="hidden" name="tab" value="sledovani"><input type="hidden" name="profile" value="{{ prof.key }}">
<select name="result"><option value="won">vyhráno</option><option value="lost">prohráno</option><option value="void">vráceno</option></select><button class="sm">Zapsat</button></form>
<form method="post" action="{{ url_for('paper_delete', pick_id=p.id) }}" style="margin:0"><input type="hidden" name="tab" value="sledovani"><input type="hidden" name="profile" value="{{ prof.key }}"><button class="ghost sm">Smazat</button></form></details></td></tr>
{% else %}<tr><td colspan="6" class="muted">Nic nesledováno. Vlož české kurzy (Nastavení) a spusť sken.</td></tr>{% endfor %}</table></div>
<details class="fold card" {{ 'open' if paper_done }}><summary>Vyhodnoceno ({{ paper_done|length }})</summary>
<div class="tablewrap"><table><tr><th>Zápas</th><th>Výběr</th><th class="num">Kurz</th><th>Skóre</th><th>Výsledek</th><th class="num">Zisk (j.)</th></tr>
{% for p in paper_done %}<tr class="r-{{ p.result }}"><td>{{ p.name }}<div class="muted">{{ p.day }}</div></td><td>{{ p.market }} {{ p.selection }}</td><td class="num">{{ '%.2f'|format(p.odds) }}</td><td>{{ p.score or '–' }}</td><td><span class="res {{ p.result }}">{{ {'won': 'výhra', 'lost': 'prohra', 'void': 'vráceno'}.get(p.result, p.result) }}</span></td>
<td class="num {{ 'pos' if p.profit >= 0 else 'neg' }}">{{ '%+.2f'|format(p.profit) }}</td></tr>
{% else %}<tr><td colspan="6" class="muted">Zatím nic.</td></tr>{% endfor %}</table></div></details>
</div></div>

{% elif tab == 'historie' %}
<h2>Vyhodnocené sázky</h2>
{% if rec.n %}<div class="card"><b>Výsledky ({{ rec.n }} sázek):</b> zisk {{ '%+.0f'|format(rec.profit) }} {{ cur }}{% if rec.yield_pct is not none %} · výnos {{ '%+.1f'|format(rec.yield_pct) }} %{% endif %} · největší propad {{ '%.0f'|format(rec.max_drawdown) }} {{ cur }} · nejdelší série proher {{ rec.longest_losing }}{% if rec.current_losing %} (teď {{ rec.current_losing }}){% endif %}
{% if rec.n < 30 %}<div class="muted">Při {{ rec.n }} sázkách jsou čísla jen orientační (potřeba desítky až stovky).</div>{% endif %}</div>{% endif %}
<div class="card tablewrap"><table><tr><th>Den</th><th>Zápas</th><th>Výběr</th><th>Kancelář</th><th class="num">Kurz</th><th class="num">Vklad</th><th>Výsledek</th><th class="num">Zisk / ztráta</th><th class="num" title="o kolik lepší kurz jsi vzal než ten těsně před zápasem; dlouhodobě v plusu = sázíš chytře">Proti závěr. kurzu</th></tr>
{% for k in settled %}<tr><td>{{ k.match_day.strftime('%d.%m.%Y') }}</td><td>{{ k.match_name or k.match_id }}</td>
<td>{{ k.market }} {{ k.selection }}</td><td>{{ k.bookmaker }}</td><td class="num">{{ '%.2f'|format(k.odds) }}</td><td class="num">{{ '%.0f'|format(k.stake) }}</td>
<td>{{ {'won': 'výhra', 'lost': 'prohra', 'void': 'vráceno', 'half_won': 'půl výhra', 'half_lost': 'půl prohra'}.get(k.status, k.status) }}</td>
<td class="num {{ 'pos' if k.profit_loss >= 0 else 'neg' }}">{{ '%+.0f'|format(k.profit_loss) }}</td>
<td class="num">{{ '%+.1f %%'|format(k.clv_pct) if k.clv_pct is not none else '–' }}</td></tr>
{% else %}<tr><td colspan="9" class="muted">Zatím žádné vyhodnocené sázky.</td></tr>{% endfor %}</table></div>

{% elif tab == 'overeni' %}
<div class="card"><b>Zpětný test modelů</b>
<div class="muted">Bot projde poslední tři sezóny zápas po zápase: model se vždy učí jen z dřívějších výsledků a jeho odhad se porovná se skutečností,
se zavíracím kurzem Pinnacle a s druhým nezávislým modelem. Čím nižší číslo (log-loss), tím přesnější odhad. Rozdíl pod 0,005 je v šumu.</div>
{% if bt_running %}<div class="note-inline" style="margin-top:8px">Test právě běží na pozadí (spuštěn {{ bt_running }}). Trvá zhruba 25 minut, pak obnov stránku.</div>
{% else %}<form class="row" method="post" action="{{ url_for('backtest_start') }}"><button>Spustit zpětný test (cca 25 minut)</button></form>{% endif %}</div>
{% if bt %}<div class="muted" style="margin:10px 0 2px">Poslední výpočet: {{ bt.when }} · {{ bt.seasons }} sezóny</div>
<div class="boxes" style="grid-template-columns:repeat(auto-fill,minmax(430px,1fr))">{% for g in bt.groups %}<div class="box"><h4>{{ g.name }}</h4><div class="muted">{{ g.note }}</div>
<table>{% for r in g.rows %}{% if loop.changed(r.market) %}<tr><th>{{ market_names.get(r.market, r.market) }} <span class="muted">({{ r.n }} zápasů)</span></th><th class="num">Log-loss</th><th class="num">Brier</th></tr>{% endif %}
<tr class="{{ 'win' if r.best }}"><td>{{ r.source }}</td><td class="num">{{ '%.4f'|format(r.logloss) }}</td><td class="num">{{ '%.4f'|format(r.brier) }}</td></tr>{% endfor %}</table></div>{% endfor %}</div>
{% else %}<div class="card muted">Zatím nebyl spuštěn.</div>{% endif %}

{% elif tab == 'profil2' %}
<h2>Profil 2 – rozbor zápasu z tvých kurzů</h2>
<div class="card">
<div class="muted">Postup z rozboru Wales – Portugalsko: tři scénáře gólů (A model, B kompromis model/trh, C trh), nejistota odhadu,
přesný výpočet každé sázky včetně vrácení vkladu (celé linie), simulace 1000 zápasů, kontrola korelace a sure betů.
Doporučí jen sázky s kladnou hodnotou ve scénáři A i B. <b>Profil 2 není ověřený zpětným testem</b> – doporučené sázky si dej do sledování a po čase uvidíš, jestli vydělávají.</div>
<form method="post" action="{{ url_for('profil2_run') }}"><input type="hidden" name="tab" value="profil2">
<div class="grid2" style="margin-top:12px">
  <label>Zápas (Domácí - Hosté)<input name="match" placeholder="Wales - Portugalsko" value="{{ p2f.match }}" required></label>
  <label>Datum zápasu<input name="day" type="date" value="{{ p2f.day or today }}"></label>
  <label>Soutěž (doplní model bota)<select name="league"><option value="">– bez modelu bota –</option>{% for k, lg in leagues.items() if lg.sport.value == 'football' %}<option value="{{ k }}" {{ 'selected' if p2f.league == k }}>{{ lg.name }}</option>{% endfor %}</select></label>
  <label>Kancelář (výchozí pro řádky bez ní)<select name="book">{% for b in czech_books %}<option {{ 'selected' if p2f.book == b }}>{{ b }}</option>{% endfor %}</select></label>
  <label>Můj odhad gólů – domácí (λ)<input name="lam_h" type="number" step="0.01" min="0.05" placeholder="0.83" value="{{ p2f.lam_h }}"></label>
  <label>Můj odhad gólů – hosté (λ)<input name="lam_a" type="number" step="0.01" min="0.05" placeholder="1.95" value="{{ p2f.lam_a }}"></label>
  <label>Kontext – góly domácích ×<input name="ctx_h" type="number" step="0.01" min="0.5" max="1.5" value="{{ p2f.ctx_h or '1.00' }}"></label>
  <label>Kontext – góly hostů ×<input name="ctx_a" type="number" step="0.01" min="0.5" max="1.5" value="{{ p2f.ctx_a or '1.00' }}"></label>
</div>
<label style="margin-top:10px">Kurzy (řádek: kancelář | trh | kurzy; kancelář můžeš vynechat)
<textarea name="odds" rows="9" placeholder="OU_3.5 | 2.34 1.62        (víc, méně)
OU_4.0 | 3.30 1.33        (celá linie: při 4 gólech vrácení)
1X2 | 6.90 4.80 1.45
BTTS | 1.65 2.11          (ano, ne)
TTA_1.5 | 1.38 2.78       (góly hostů: víc, méně)
U4.5 | 1.25               (jen jedna strana)
AH_1.0:away | 1.65        (hosté -1,0)
2&U3.5 | 2.41             (kombinace)
G1-3 | 1.73               (1 až 3 góly)
fortuna | OU_3.5 | 2.30 1.66">{{ p2f.odds }}</textarea></label>
<label style="flex-direction:row;align-items:center;gap:8px;margin-top:8px"><input type="checkbox" name="use_czech" value="1" style="width:auto" {{ 'checked' if p2f.use_czech != '0' }}> přidat i kurzy uložené u zápasu / v Nastavení</label>
<div class="row"><button>Spočítat Profil 2</button></div></form>
<details class="fold" style="margin-top:8px"><summary>Zápis trhů</summary><ul class="help">
<li><b>1X2</b> (1 X 2), <b>DC</b> (1X 12 X2), <b>DNB</b> (domácí hosté), <b>BTTS</b> (ano ne)</li>
<li><b>OU_2.5</b>, <b>OU_4.0</b>, <b>OU_2.25</b> – celkem gólů (víc méně); celé a čtvrtinové linie se počítají s vrácením</li>
<li><b>TTH_1.5</b> / <b>TTA_1.5</b> – góly domácích / hostů (víc méně); <b>AH_-1.0</b> – handicap z pohledu domácích (domácí hosté)</li>
<li>Jedna strana: <b>U4.5</b>, <b>O2.5</b>, <b>H_U1.5</b> (domácí méně než 1,5), <b>A_O0.5</b>, nebo obecně <b>TRH:výběr</b> (např. AH_1.0:away)</li>
<li>Kombinace s jedním kurzem: <b>2&amp;U3.5</b>, <b>X2&amp;U3.5</b>, <b>2&amp;BTTS</b>, <b>2&amp;H_U0.5</b> (výhra hostů s čistým kontem), <b>G1-3</b>, <b>G3</b>, <b>S0:1</b></li>
</ul></details>
</div>

{% if p2st.settled or p2st.open %}<h2>Sledování Profilu 2</h2>
<div class="stats"><div class="stat"><span>Vyhodnoceno</span><b>{{ p2st.settled }}</b><small>čeká {{ p2st.open }}</small></div>
<div class="stat"><span>Zisk (jednotky)</span><b class="{{ 'pos' if p2st.profit >= 0 else 'neg' }}">{{ '%+.2f'|format(p2st.profit) }}</b></div>
<div class="stat"><span>ROI</span><b>{{ '%+.1f %%'|format(p2st.roi * 100) if p2st.roi is not none else '–' }}</b></div>
<div class="stat"><span>Úspěšnost</span><b>{{ '%.0f %%'|format(p2st.hit * 100) if p2st.hit is not none else '–' }}</b><small>{% if p2st.avg_ev is not none %}čekané EV {{ '%+.1f %%'|format(p2st.avg_ev * 100) }}{% endif %}</small></div></div>
<div class="muted">Do 100+ vyhodnocených sázek jsou výsledky hlavně náhoda. Výběry a výsledky najdeš v záložce Sledování modelu.</div>{% endif %}

{% for a in p2 %}{% set first = loop.first %}
<details class="card match" {{ 'open' if first }}>
<summary class="ms" style="grid-template-columns:1fr auto 18px"><span class="nm">{{ a.match }}<small>{{ a.day }}{{ ' · ' + leagues[a.league].name if a.league in leagues }} · spočteno {{ a.created.replace('T', ' ') }} UTC</small></span>
<span class="rc">{% if a.picks %}<b>{{ a.picks[0].offer.label }} @ {{ '%.2f'|format(a.picks[0].offer.odds) }}</b><small>{{ a.picks[0].title }}</small>{% else %}<span class="badge bad">nesázet</span>{% endif %}</span><span class="arr">▸</span></summary>
<div class="mbody">
<table class="mk" style="margin-top:6px"><tr><th>Scénář</th><th class="num">Góly domácí</th><th class="num">Góly hosté</th><th class="num">Celkem</th><th>Zdroj</th></tr>
{% for sc in a.scenarios %}<tr><td><b>{{ sc.name }}</b>{% if not sc.decides %} <span class="muted">(kontrola)</span>{% endif %}</td><td class="num">{{ '%.2f'|format(sc.lam_h) }}</td><td class="num">{{ '%.2f'|format(sc.lam_a) }}</td><td class="num">{{ '%.2f'|format(sc.lam_h + sc.lam_a) }}</td><td class="muted">{{ sc.source }}</td></tr>{% endfor %}</table>

{% for pk in a.picks %}{% set o = pk.offer %}<div class="tipbox {{ 'alt' if not loop.first }}">
<div class="tip"><div><div class="muted">{{ pk.title }}</div><div class="title">{{ o.label }} <span class="muted">({{ o.book }})</span></div>
<div class="kv"><span>Výhra <b>{{ '%.0f'|format(o.worst_win * 100) }} %</b></span>{% if o.worst_push > 0.005 %}<span>Vrácení <b>{{ '%.0f'|format(o.worst_push * 100) }} %</b></span>{% endif %}<span>Prohra <b>{{ '%.0f'|format(o.worst_loss * 100) }} %</b></span>
<span>EV {% for e in o.ev %}<b class="{{ 'pos' if e > 0 else 'neg' }}">{{ '%+.1f'|format(e * 100) }} %</b>{{ ' / ' if not loop.last }}{% endfor %}</span>
<span>Min. kurz <b>{{ '%.2f'|format(o.min_odds) }}</b></span><span>Simulace 1000× po 100 {{ cur }} <b class="{{ 'pos' if o.sim_profit > 0 else 'neg' }}">{{ '%+.0f'|format(o.sim_profit * 100) }} %</b></span></div></div>
<div class="big">{{ '%.2f'|format(o.odds) }}<small>{% if pk.stake %}vklad {{ '%.0f'|format(pk.stake) }} {{ cur }}{% else %}vklad: {{ pk.stake_note }}{% endif %}</small></div></div></div>{% endfor %}
{% for n in a.notes %}<div class="card note" style="margin-top:8px">{{ n }}</div>{% endfor %}
{% if a.correlation %}<div class="muted" style="margin-top:6px">Společně: {% for c in a.correlation %}{{ c.a }} + {{ c.b }} – obě vyjdou {{ '%.0f'|format(c.both_win * 100) }} %, obě padnou {{ '%.0f'|format(c.both_lose * 100) }} %{{ '; ' if not loop.last }}{% endfor %}.</div>{% endif %}
<div class="row" style="margin-top:8px">
<form method="post" action="{{ url_for('profil2_track') }}"><input type="hidden" name="tab" value="profil2"><input type="hidden" name="match" value="{{ a.match }}"><input type="hidden" name="day" value="{{ a.day }}"><button class="ok sm" {{ 'disabled' if not a.picks }}>Sledovat doporučené sázky</button></form>
<form method="post" action="{{ url_for('profil2_delete') }}"><input type="hidden" name="tab" value="profil2"><input type="hidden" name="match" value="{{ a.match }}"><input type="hidden" name="day" value="{{ a.day }}"><button class="ghost sm">Smazat rozbor</button></form></div>

<div class="boxes">
<div class="box"><h4>Simulace {{ a.sim.n }} zápasů (scénář {{ a.scenarios[0].name.split(' ')[0] }})</h4>
<p>Domácí <b>{{ '%.1f'|format(a.sim.home) }} %</b> · remíza <b>{{ '%.1f'|format(a.sim.draw) }} %</b> · hosté <b>{{ '%.1f'|format(a.sim.away) }} %</b> · průměr <b>{{ '%.2f'|format(a.sim.avg_goals) }}</b> gólu</p>
<table><tr><th>Výsledek</th><th class="num">%</th><th>Výsledek</th><th class="num">%</th></tr>
{% for i in range(0, a.sim.top|length, 2) %}<tr><td>{{ a.sim.top[i].score }}</td><td class="num">{{ '%.1f'|format(a.sim.top[i].pct) }}</td>{% if i + 1 < a.sim.top|length %}<td>{{ a.sim.top[i+1].score }}</td><td class="num">{{ '%.1f'|format(a.sim.top[i+1].pct) }}</td>{% endif %}</tr>{% endfor %}</table>
<p class="muted">Počet gólů: {% for g, v in a.sim.goals.items() %}{{ g }}: {{ '%.1f'|format(v) }} %{{ ' · ' if not loop.last }}{% endfor %}</p></div>
<div class="box"><h4>Sure bety</h4>{% set sure = a.surebets|selectattr('sure')|list %}
{% if sure %}{% for sb in sure %}<p><span class="badge tipb">SURE BET</span> {% for sel in sb.labels %}{{ sel }} {{ '%.2f'|format(sb.odds[loop.index0]) }} ({{ sb.books[loop.index0] }}, {{ sb.split[loop.index0] }} % vkladu){{ ', ' if not loop.last }}{% endfor %} – výnos <b>{{ '%+.1f'|format(sb.yield * 100) }} %</b></p>{% endfor %}
{% else %}<p class="muted">Žádný. Sure bet vznikne, když jiná kancelář nabídne aspoň:</p>
<table>{% for sb in a.surebets if sb.needed %}<tr>{% for i in (0, 1) %}<td>{{ sb.labels[1 - i] }} ≥ <b>{{ '%.2f'|format(sb.needed[i]) }}</b> <span class="muted">(proti {{ '%.2f'|format(sb.odds[i]) }})</span></td>{% endfor %}</tr>{% endfor %}</table>{% endif %}</div>
{% if a.avoid %}<div class="box"><h4>Vyhnout se</h4><ul>{% for o in a.avoid %}<li>{{ o.label }} @ {{ '%.2f'|format(o.odds) }} <span class="neg">{{ '%+.0f'|format(o.worst_ev * 100) }} %</span></li>{% endfor %}</ul></div>{% endif %}
</div>
<details class="fold" style="margin-top:8px"><summary>Všechny nabídky ({{ a.offers|length }})</summary><div class="tablewrap">
<table class="mk"><tr><th>Sázka</th><th class="num">Kurz</th>{% for sc in a.scenarios %}<th class="num">EV {{ sc.name.split(' ')[0] }}</th>{% endfor %}<th class="num">Výhra / vrácení / prohra</th><th class="num">Min. kurz</th><th class="num">Sim.</th><th></th></tr>
{% for o in a.offers %}<tr><td>{{ o.label }} <span class="muted">{{ o.book }}</span></td><td class="num">{{ '%.2f'|format(o.odds) }}</td>
{% for e in o.ev %}<td class="num {{ 'pos' if e > 0 else 'neg' }}">{{ '%+.1f'|format(e * 100) }} %</td>{% endfor %}
<td class="num">{{ '%.0f'|format(o.worst_win * 100) }} / {{ '%.0f'|format(o.worst_push * 100) }} / {{ '%.0f'|format(o.worst_loss * 100) }} %</td><td class="num">{{ '%.2f'|format(o.min_odds) }}</td>
<td class="num {{ 'pos' if o.sim_profit > 0 else 'neg' }}">{{ '%+.0f'|format(o.sim_profit * 100) }} %</td>
<td>{% if o.verdict == 'robust' %}<span class="badge tipb">robustní</span>{% elif o.verdict == 'borderline' %}<span class="badge warn">jen model</span>{% elif o.verdict == 'avoid' %}<span class="badge bad">vyhnout se</span>{% elif o.verdict == 'suspicious' %}<span class="badge warn">ověř kurz</span>{% endif %}</td></tr>{% endfor %}</table></div></details>
<details class="fold"><summary>Text pro kopírování</summary><textarea rows="12" readonly style="font:13px/1.4 monospace">{{ p2text[loop.index0] }}</textarea></details>
</div></details>
{% else %}<div class="card muted">Zatím žádný rozbor. Vyplň zápas a kurzy výše.</div>{% endfor %}

{% elif tab == 'kalkulacka' %}
<h2>Rychlá value kalkulačka</h2>
<div class="card"><div class="muted">Zadej kurz a svůj odhad pravděpodobnosti (nejdřív odhad, až potom se dívej na kurz). Počítá se přímo v prohlížeči.</div>
<div class="grid2" style="margin-top:12px">
  <label>Formát kurzu<select id="vf"><option value="dec">Decimální (2.50)</option><option value="frac">Zlomkový (3/2)</option><option value="us">Americký (+150 / -200)</option></select></label>
  <label>Kurz<input id="vo" value="1.48"></label>
  <label>Můj odhad pravděpodobnosti (%)<input id="vp" type="number" step="0.1" min="0.1" max="99.9" value="71"></label>
</div><div id="vout" class="kv" style="margin-top:12px"></div></div>
<script>
(function(){
 var K={{ kelly_fraction }}, CAP={{ stake_cap }}, BANK={{ '%.0f'|format(s.bankroll) }}, CUR="{{ cur }}";
 function dec(){var f=document.getElementById('vf').value,v=document.getElementById('vo').value.trim().replace(',','.');
  if(f=='dec')return parseFloat(v);
  if(f=='frac'){var a=v.split('/');return a.length==2?1+parseFloat(a[0])/parseFloat(a[1]):NaN;}
  var n=parseFloat(v);return n>0?1+n/100:1+100/Math.abs(n);}
 function frac(d){var b=d-1,best=[1,1,9];for(var q=1;q<=40;q++){var pn=Math.round(b*q),e=Math.abs(pn/q-b);if(e<best[2]-1e-9)best=[pn,q,e];if(e<1e-9)break;}
  var g=function(a,c){return c?g(c,a%c):a},k=g(best[0],best[1]);return (best[0]/k)+'/'+(best[1]/k);}
 function run(){var d=dec(),p=parseFloat(document.getElementById('vp').value)/100,o=document.getElementById('vout');
  if(!(d>1)||!(p>0&&p<1)){o.innerHTML='<span>Zadej platný kurz a pravděpodobnost.</span>';return;}
  var ev=p*d-1,imp=1/d,b=d-1,kelly=(b*p-(1-p))/b,stake=Math.max(0,Math.min(kelly*K,CAP))*BANK;
  var cls=ev>0?'pos':'neg',h=function(l,v){return '<span>'+l+' <b>'+v+'</b></span>'};
  o.innerHTML=h('Čekaný zisk','<span class="'+cls+'">'+(ev>=0?'+':'')+(ev*100).toFixed(2)+' %</span>')+
   h('Šance podle kurzu',(imp*100).toFixed(2)+' %')+h('Kurz bez marže podle tvého odhadu',(1/p).toFixed(2))+
   h('Decimální / zlomkový',d.toFixed(2)+' / '+frac(d))+h('Výhra / 100 '+CUR,(b*100).toFixed(2))+h('Výplata / 100 '+CUR,(d*100).toFixed(2))+
   h('Plný Kelly',(Math.max(kelly,0)*100).toFixed(1)+' %')+h('Vklad bota (1/8 Kelly, strop 2 %)',ev>0?stake.toFixed(0)+' '+CUR:'nesázet')+
   (ev>0.15?'<div class="muted">Čekaný zisk nad 15 % je podezřelý, odhad pravděpodobnosti raději ověř.</div>':'')+
   '<div class="muted">Odhad 55 % může být ve skutečnosti 51–59 %. Sázej jen, když čekaný zisk přesahuje nejistotu odhadu i marži.</div>';}
 ['vf','vo','vp'].forEach(function(i){document.getElementById(i).addEventListener('input',run);});run();
})();
</script>

<h2>Kalkulačka hodnoty z ostrých kurzů</h2>
<div class="card">
<ol class="help"><li>Opiš kurzy ostré kanceláře (např. Pinnacle) na všechny výsledky trhu, oddělené mezerou.</li>
<li>Napiš, kolikátý výsledek v pořadí chceš sázet (1 = první).</li><li>Zadej kurz, který na něj nabízí tvoje česká kancelář.</li></ol>
<form method="post" action="{{ url_for('calc') }}"><div class="grid2" style="margin-top:12px">
  <label>Ostré kurzy<input name="sharp" placeholder="2.10 3.40 3.60" value="{{ c.sharp if c else '' }}" required></label>
  <label>Můj výběr (pořadí)<input name="pick" type="number" min="1" max="6" value="{{ c.pick if c else 1 }}" required></label>
  <label>Kurz české kanceláře<input name="odds" type="number" step="0.01" placeholder="2.25" value="{{ c.odds if c else '' }}" required></label>
  <label>Soutěž<select name="league">{% for k, lg in leagues.items() %}<option value="{{ k }}" {{ 'selected' if c and c.league == k }}>{{ lg.name }}</option>{% endfor %}</select></label>
  <label>Pravděpodobnost modelu v % (nepovinné)<input name="p_model" type="number" step="0.1" value="{{ c.p_model_in if c else '' }}"></label>
</div><div class="row"><button>Spočítat</button></div></form>
{% if c and c.result %}{% set yes = c.result.is_value and c.stake > 0 %}<div class="verdict {{ 'yes' if yes }}">
  <b class="v {{ 'pos' if yes else 'neg' }}">{{ 'SÁZET' if yes else 'NESÁZET' }}</b>
  <div class="kv"><span title="kolik průměrně vyděláš na každých 100 Kč vsazených, pokud odhad modelu sedí (+10 % = +10 Kč na 100 Kč)">Čekaný zisk <b>{{ '%+.1f'|format(c.result.ev * 100) }} %</b> (práh {{ '%.1f'|format(c.result.ev_threshold * 100) }} %)</span>
  <span title="kurz, který odpovídá skutečné šanci – bez marže, kterou si bere sázková kancelář">Kurz bez marže <b>{{ '%.2f'|format(1 / c.result.p_market) }}</b></span><span>Minimální kurz <b>{{ '%.2f'|format(c.result.min_odds) }}</b></span>
  {% if c.result.is_value %}<span>Vklad <b>{{ '%.0f'|format(c.stake) }} {{ cur }}</b>{% if c.stake_reason != 'ok' %} ({{ c.stake_reason }}){% endif %}</span>{% endif %}</div>
  {% if c.result.reasons %}<div class="muted" style="margin-top:6px">Důvod: {{ c.result.reasons|join(', ') }}</div>{% endif %}
  {% if c.result.needs_review %}<div class="muted" style="margin-top:6px">Neobvykle vysoký čekaný zisk – zkontroluj, že kurzy sedí.</div>{% endif %}
</div>{% endif %}</div>

{% elif tab == 'nastaveni' %}
<div class="setgrid">
<section><h2>Data pro model</h2>
<div class="card">Stáhne nejnovější výsledky lig, pohárů, reprezentací a NHL (zdarma, trvá zhruba minutu). Dělej to jednou denně před načtením kurzů.
<form class="row" method="post" action="{{ url_for('refresh_data') }}"><button>Aktualizovat data</button></form></div></section>
<section><h2>České kurzy</h2>
<div class="card">Kurzy se nejpohodlněji zadávají přímo u zápasu: <b>Přehled → rozbal zápas → Zadat kurzy z české kanceláře</b>. Platí 48 hodin.
{% if czech_entries %}<details class="fold" style="margin-top:8px"><summary>Uložené kurzy ({{ czech_entries|length }})</summary><div class="tablewrap"><table><tr><th>Kancelář</th><th>Zápas</th><th>Trh</th><th>Kurzy</th></tr>
{% for e in czech_entries %}<tr><td>{{ e.book }}</td><td>{{ e.home }} - {{ e.away }}</td><td>{{ market_names.get(e.market, e.market) }}</td><td>{{ e.odds|join(' · ') }}</td></tr>{% endfor %}</table></div>
<form method="post" action="{{ url_for('clear_czech_odds') }}"><button class="bad sm">Smazat všechny</button></form></details>{% endif %}
<details class="fold" style="margin-top:6px"><summary>Hromadné vložení textem</summary>
<div class="muted">Jeden řádek na trh: <code>kancelář | Domácí - Hosté | trh | kurzy</code>, např. <code>tipsport | Německo - Itálie | 1X2 | 2.10 3.30 3.40</code>.</div>
<form method="post" action="{{ url_for('set_czech_odds') }}"><textarea name="text" rows="5" required style="margin-top:8px"></textarea><div class="row" style="margin-top:8px"><button>Uložit kurzy</button></div></form></details></div></section>
<section><h2>Bankroll</h2>
<div class="card">Aktuální stav: <b>{{ '%.0f'|format(s.bankroll) }} {{ cur }}</b> · režim: <b>{{ 'testovací' if s.test_mode else 'ostrý' }}</b>
<form class="row" method="post" action="{{ url_for('deposit') }}"><input type="hidden" name="tab" value="nastaveni">
<label>Přidat vklad ({{ cur }})<input name="amount" type="number" step="1" min="1" required></label>
<label>Režim<select name="test"><option value="1" {{ 'selected' if s.test_mode }}>Testovací (virtuální peníze)</option><option value="0" {{ 'selected' if not s.test_mode }}>Ostrý</option></select></label>
<button>Uložit</button></form></div></section>
<section><h2>API klíče</h2>
<details class="fold card"><summary>Živé kurzy (the-odds-api.com): {{ 'uložen' if has_key else 'chybí' }}</summary>
<form class="row" method="post" action="{{ url_for('set_key') }}">
<label>API klíč<input name="key" type="password" autocomplete="off" required></label><button>Uložit klíč</button></form></details>
<details class="fold card"><summary>API-Football (zranění, absence, stadiony): {{ 'uložen' if has_football_key else 'chybí' }}</summary>
<div class="muted">Zdarma 100 požadavků/den, registrace na api-football.com.</div>
<form class="row" method="post" action="{{ url_for('set_football_key') }}"><label>Klíč<input name="key" type="password" autocomplete="off" required></label><button>Uložit klíč</button></form></details></section>
</div>
{% endif %}
</div></main></body></html>"""


MARKET_NAMES = {"1X2": "Výsledek", "DC": "Dvojitá šance", "DNB": "Sázka bez remízy", "OU_1.5": "Góly 1.5",
                "OU_2.5": "Góly 2.5", "OU_3.5": "Góly 3.5", "OU_4.5": "Góly 4.5",
                "OU_2.0": "Góly 2.0 (při 2 vráceno)", "OU_4.0": "Góly 4.0 (při 4 vráceno)", "TTH_1.5": "Góly domácích 1.5",
                "TTA_1.5": "Góly hostů 1.5", "BTTS": "Oba týmy dají gól", "TTH_0.5": "Domácí dají gól",
                "TTA_0.5": "Hosté dají gól", "MONEYLINE": "Vítěz zápasu (vč. prodloužení)",
                "OU_5.5": "Góly 5.5", "OU_6.5": "Góly 6.5"}


def _decorate(v, czech_entries) -> None:
    """Doplni k zapasu ceske kurzy (EV, zda vsadit), hlavni/dalsi trhy a stitky pro filtr."""
    league = LEAGUES.get(v.league)
    thr = SETTINGS.value.ev_threshold(league.tier) if league else 0.03
    best = czechodds.for_match(czech_entries, v.name)
    for r in v.rows:
        r.cz = best.get((r.market, r.selection))
        r.cz_ev = r.p_final * r.cz[0] - 1.0 if r.cz else None
        good = bool(r.cz) and (r.cz[0] >= r.min_odds if r.status == "model" else r.cz_ev >= thr)
        r.cz_suspect = good and r.cz_ev > 0.15  # tak velka hrana byva spis chybne opsany kurz
        r.cz_ok = good and not r.cz_suspect
    key = {id(v.rows[v.recommended])} | ({id(v.rows[v.target])} if v.target is not None else set())
    v.main_rows = [r for r in v.rows if r.status != "model" or r.cz or id(r) in key]
    v.more_rows = [r for r in v.rows if r not in v.main_rows]
    ok = [r for r in v.rows if r.cz_ok]
    v.cz_best = max(ok, key=lambda r: r.cz_ev) if ok else None
    v.cz_count = sum(1 for r in v.rows if r.cz)
    v.cz_books = {}
    for b in sorted({e.book for e in czech_entries}):
        vals = czechodds.for_match([e for e in czech_entries if e.book == b], v.name)
        if vals:
            v.cz_books[b] = {f"{m}:{sel}": f"{o:.2f}" for (m, sel), (o, _) in vals.items()}
    v.tags = (["cz"] if ok else []) + (["tip"] if v.verdict == "TIP" or v.has_tip else [])         + (["cil"] if v.verdict == "CÍL" else []) + (["value"] if v.verdict.startswith("VÝHODNÉ") else [])
    if not v.tags:
        v.tags = ["none"]


def _to_bet(views, led, bank: float, expo: float) -> tuple[list[dict], list[dict]]:
    """Souhrn napric zapasy: sazky s hodnotou na zadanych ceskych kurzech (s vkladem) a podezrele kurzy k overeni."""
    bets, check = [], []
    for v in views:
        k = datetime.fromisoformat(v.kickoff)
        day = (k.replace(tzinfo=timezone.utc).astimezone(_TZ) if _TZ else k).date()
        for r in v.rows:
            if not (r.cz_ok or r.cz_suspect):
                continue
            item = {"view": v, "row": r, "day": day.isoformat(), "stake": 0.0}
            if r.cz_suspect:
                check.append(item)
                continue
            p = r.p_final * (0.97 if r.status == "model" else 1.0)  # u trhu jen z modelu opatrneji
            st = risk.size_stake(p, r.cz[0], bank, expo, paused=led.paused())
            stake = st.stake if st.allowed else 0.0
            if r.status == "model":  # neovereny trh: polovicni vklad, zaokrouhleny na desitky
                stake = max(round(stake / 2 / 10) * 10, SETTINGS.risk.min_stake) if stake else 0.0
            item["stake"] = stake
            bets.append(item)
    bets.sort(key=lambda b: b["view"].kickoff)
    return bets, check


def _manual_bet(market: str, line: str, selection: str) -> tuple[str, str]:
    """Trh z formulare -> tvar pro automaticke vyhodnoceni; nesrozumitelny vyber u znameho trhu je chyba."""
    from .settlement import grade, normalize_bet

    market = market.strip().upper()
    if market == "JINY":
        return "JINY", selection.strip()
    if market == "OU":
        if not line.strip():
            raise ValueError("U počtu gólů vyplň hranici (např. 2.5).")
        market = f"OU_{float(line.replace(',', '.')):g}"
    market, selection = normalize_bet(market, selection)
    if grade(market, selection, 0, 0) is None:
        raise ValueError(f"Výběru '{selection}' u trhu {market} nerozumím (např. 1/X/2, over/under, ano/ne, 1X).")
    return market, selection


def _pending(open_tickets, open_picks) -> list[dict]:
    """Zapasy, u kterych neco ceka na vysledek (tikety i sledovane vybery), jeden radek na zapas."""
    out: dict[tuple, dict] = {}
    items = [(t.match_name, t.match_day, "tikety") for t in open_tickets if t.match_name and t.match_day]
    items += [(p.name, date.fromisoformat(p.day), "vybery") for p in open_picks]
    for name, day, what in items:
        if " - " not in name or day > date.today():  # budouci zapas jeste nema vysledek
            continue
        key = (*(czechodds._en(x.strip()) for x in name.split(" - ", 1)), day)
        row = out.setdefault(key, {"key": f"{day.isoformat()}|{name}", "name": name, "day": day,
                                   "day_label": day.strftime("%d.%m."), "tikety": 0, "vybery": 0})
        row[what] += 1
    rows = sorted(out.values(), key=lambda r: r["day"])
    for r in rows:
        r["what"] = ", ".join(f"{r[w]}× {label}" for w, label in (("tikety", "tiket"), ("vybery", "sledovaný výběr")) if r[w])
    return rows


def create_app(database_url: str | None = None) -> Flask:
    app = Flask(__name__)
    factory = make_session_factory(database_url)
    valid_tabs = {k for k, _ in TABS}

    def render(session, msg: str = "", err: bool = False, calc: dict | None = None, tab: str = "prehled",
               p2form: dict | None = None):
        led = Ledger(session)
        all_picks = paper.load(led)
        prof = paper.profile(request.values.get("profile"))
        picks = [p for p in all_picks if p.profile == prof.key]
        p2 = profile2.load(led)
        done_picks = sorted((p for p in picks if p.result is not None), key=lambda p: p.kickoff)
        settled = list(session.scalars(select(Ticket).where(Ticket.status != TICKET_OPEN).order_by(Ticket.settled_at.desc()).limit(200)))
        s = led.summary()
        s["test_mode"] = led.test_mode()
        today = date.today()
        open_tips = led.open_tips()
        tips_by_match = {t.match_id: t for t in open_tips}
        views = []
        now = utcnow()
        for lg in SETTINGS.odds_api.sport_keys:
            for v in analysis.loads(led.analysis(lg)):
                k = datetime.fromisoformat(v.kickoff)
                if k > now:
                    v.local = _local(k)
                    rec = v.rows[v.recommended]
                    v.target_stake = 0.0
                    if v.target is not None:
                        tg = v.rows[v.target]
                        st = risk.size_stake(tg.p_model * 0.97, tg.min_odds, led.bankroll(), led.open_exposure(today), paused=led.paused())
                        v.target_stake = st.stake if st.allowed else 0.0
                    ctx_lines = v.absences + ([f"Počasí: {v.weather}"] if v.weather else [])
                    v.post = detail.post_text(v.name, v.local, LEAGUES[v.league].name if v.league in LEAGUES else v.league,
                                              rec.label, rec.best_odds, rec.ev_pct, rec.p_final, rec.p_market, v.sections, ctx_lines)
                    v.has_tip = v.match_id in tips_by_match
                    views.append(v)
        views.sort(key=lambda v: v.kickoff)
        czech_entries = czechodds.load(led, now)
        for v in views:
            _decorate(v, czech_entries)
        local = {v.name: v.local for v in views}
        bank = led.bankroll()
        expo = led.open_exposure(today)
        bets, check = _to_bet(views, led, bank, expo)
        # kombinace, jejiz noha ma zadany cesky kurz pod hranici, by byla klamava
        combos = [c for c in combo.best_combos(views, bank, expo)
                  if not any(lg.row.status == "model" and getattr(lg.row, "cz", None) and lg.row.cz[0] < lg.row.min_odds
                             for lg in c.legs)]
        for c in combos:
            for lg in c.legs:
                object.__setattr__(lg, "local", local.get(lg.match, ""))
        partners = {v.name: combo.partner_for(v.name, views, bank, expo) for v in views if v.name in {t.match_name for t in open_tips}}
        for pc in partners.values():
            for lg in (pc.legs if pc else ()):
                object.__setattr__(lg, "local", local.get(lg.match, ""))
        open_tickets = list(session.scalars(select(Ticket).where(Ticket.status == TICKET_OPEN).order_by(Ticket.match_day)))
        tab = TAB_ALIASES.get(tab, tab)
        return render_template_string(
            PAGE,
            s=type("S", (), s),
            cur=SETTINGS.currency,
            tabs=TABS,
            tab=tab if tab in valid_tabs else "prehled",
            pending=_pending(open_tickets, [p for p in picks if p.result is None]),
            msg=msg,
            err=err,
            views=views,
            combos=combos,
            partners=partners,
            tips_by_match=tips_by_match,
            orphan_tips=[t for t in open_tips if t.match_id not in {v.match_id for v in views}],
            open_tickets=open_tickets,
            settled=settled,
            books=SETTINGS.bookmakers.betting_books,
            leagues=LEAGUES,
            exposure=led.open_exposure(today),
            exposure_cap=s["bankroll"] * SETTINGS.risk.max_daily_exposure_pct,
            today=today.isoformat(),
            czech_entries=czech_entries,
            second_gap=secondopinion.GAP,
            bt=json.loads(led._get(backtest.STATE_KEY, "") or "null"),
            bt_running=led._get(backtest.RUNNING_KEY, ""),
            czech_books=sorted(czechodds._BOOKS, key=lambda b: (b != "tipsport", b)),
            form_markets=czechodds.FORM_MARKETS,
            market_names=MARKET_NAMES,
            bets=bets,
            check=check,
            tag_counts={t: sum(t in v.tags for v in views) for t in ("cz", "tip", "cil", "value", "none")},
            kelly_fraction=SETTINGS.risk.kelly_fraction,
            stake_cap=SETTINGS.risk.max_stake_pct,
            pst=paper.stats(picks),
            prof=prof,
            profiles=paper.PROFILES,
            prof_stats={pr.key: paper.stats([p for p in all_picks if p.profile == pr.key]) for pr in paper.PROFILES},
            rec=metrics.track_record([t.profit_loss for t in reversed(settled)], [t.stake for t in reversed(settled)]),
            prec=metrics.track_record([p.profit for p in done_picks], [1.0] * len(done_picks)),
            paper_open=[p for p in picks if p.result is None],
            paper_done=[p for p in picks if p.result is not None][::-1],
            has_key=bool(led.api_key()),
            auto_st=auto.status(led),
            auto_req=bool(led._get(auto.REQUEST_KEY, "")),
            has_football_key=bool(led.api_football_key()),
            scan_leagues=list(SETTINGS.odds_api.sport_keys),
            c=type("C", (), calc) if calc else None,
            p2=p2,
            p2text=[profile2.format_text(a, SETTINGS.currency) for a in p2],
            p2f=type("F", (), {k: "" for k in ("match", "day", "league", "book", "lam_h", "lam_a", "ctx_h", "ctx_a", "odds", "use_czech")} | (p2form or {})),
            p2st=type("P", (), paper.stats([p for p in all_picks if p.profile == "profil2"])),
        )

    def action(fn, ok_msg: str, tab: str = "prehled"):
        tab = request.form.get("tab", tab)
        with factory() as session:
            try:
                result = fn(Ledger(session), session)
                session.commit()
                return render(session, result or ok_msg, tab=tab)
            except (ValueError, KeyError) as e:
                session.rollback()
                return render(session, f"Chyba: {e}", err=True, tab=tab), 400

    @app.get("/")
    def index():
        with factory() as session:
            return render(session, tab=request.args.get("tab", "prehled"))

    @app.post("/deposit")
    def deposit():
        def fn(led: Ledger, _):
            led.deposit(float(request.form["amount"]))
            led.set_test_mode(request.form.get("test", "1") == "1")

        return action(fn, "Bankroll uložen.")

    @app.post("/key")
    def set_key():
        def fn(led: Ledger, _):
            key = request.form["key"].strip()
            if not key:
                raise ValueError("Klíč je prázdný.")
            led.set_api_key(key)

        return action(fn, "API klíč uložen.", tab="nastaveni")

    @app.post("/football-key")
    def set_football_key():
        def fn(led: Ledger, _):
            led.set_api_football_key(request.form["key"])

        return action(fn, "Klíč API-Football uložen.", tab="nastaveni")

    @app.post("/czech-odds")
    def set_czech_odds():
        def fn(led: Ledger, _):
            now = utcnow()
            entries, errors = czechodds.parse_text(request.form["text"], now)
            if errors and not entries:
                raise ValueError("; ".join(errors))
            czechodds.add(led, entries, now)
            return f"Uloženo kurzů: {len(entries)}." + (f" Přeskočeno: {'; '.join(errors)}" if errors else "")

        return action(fn, "", tab="nastaveni")

    @app.post("/czech-odds/match")
    def czech_odds_match():
        def fn(led: Ledger, _):
            from .pipeline import scan_all

            f = request.form
            now = utcnow()
            entries, errors = czechodds.from_form(f["book"], f["match"], f, now)
            if not entries:
                raise ValueError("; ".join(errors) or "Nevyplnil jsi žádný kurz.")
            czechodds.add(led, entries, now)
            scan_all(led, [f["league"]], cache_only=True)
            return (f"Uloženo {len(entries)} trhů ({f['book']}, {f['match']}), zápas přepočítán."
                    + (f" Přeskočeno: {'; '.join(errors)}" if errors else ""))

        return action(fn, "")

    @app.post("/backtest")
    def backtest_start():
        def fn(led: Ledger, _):
            import subprocess
            import sys

            if led._get(backtest.RUNNING_KEY, ""):
                return "Zpětný test už běží."
            led._set(backtest.RUNNING_KEY, datetime.now().strftime("%d.%m. %H:%M"))
            cmd = [sys.executable, "-c", "import sys; from tipsbot.cli import main; sys.exit(main())", "verify"]
            if database_url:
                cmd[3:3] = ["--db", database_url]
            subprocess.Popen(cmd)  # noqa: S603 - vlastni prikaz bota, bezi na pozadi
            return "Zpětný test spuštěn na pozadí. Za zhruba 25 minut obnov stránku."

        return action(fn, "", tab="overeni")

    @app.post("/auto/run")
    def auto_run():
        return action(lambda led, _: auto.request_run(led),
                      "Automat se spustí do minuty (načte kurzy, profily vyberou sázky, vyhodnotí zápasy).")

    @app.post("/refresh-data")
    def refresh_data():
        def fn(led: Ledger, _):
            from .pipeline import refresh_data as run_refresh

            return " · ".join(run_refresh(led))

        return action(fn, "", tab="nastaveni")

    @app.post("/czech-odds/clear")
    def clear_czech_odds():
        return action(lambda led, _: czechodds.save(led, []), "České kurzy smazány.", tab="nastaveni")

    @app.post("/paper")
    def paper_add():
        def fn(led: Ledger, _):
            f = request.form
            p = paper.add_manual(led, league=f.get("league", ""), name=f["name"].strip(), day=f["day"], market=f["market"],
                                 selection=f["selection"], odds=float(f["odds"]), book=f["book"], now=utcnow(),
                                 profile_key=f.get("profile"))
            return f"Přidáno do sledování: {p.name} {p.market} {p.selection} @ {p.odds:.2f}."

        return action(fn, "", tab="sledovani")

    @app.post("/paper/<pick_id>/result")
    def paper_result(pick_id: str):
        def fn(led: Ledger, _):
            result = request.form["result"]
            if result not in ("won", "lost", "void"):
                raise ValueError("Neznámý výsledek.")
            paper.set_result(led, pick_id, result)

        return action(fn, "Výsledek zapsán.", tab="sledovani")

    @app.post("/paper/<pick_id>/delete")
    def paper_delete(pick_id: str):
        return action(lambda led, _: paper.remove(led, pick_id), "Smazáno.", tab="sledovani")

    @app.post("/settle")
    def settle_all():
        def fn(led: Ledger, _):
            from .settlement import settle_everything

            return " · ".join(settle_everything(led))

        return action(fn, "", tab="sledovani")

    @app.post("/score")
    def score():
        def fn(led: Ledger, _):
            from .settlement import parse_score, settle_with_score

            day, _, name = request.form["match"].partition("|")
            f = request.form
            hg, ag = parse_score(f"{f['home']}:{f['away']}" if "home" in f else f.get("score", ""))
            return " · ".join(settle_with_score(led, name, date.fromisoformat(day), hg, ag))

        return action(fn, "", tab="tikety")

    @app.post("/resume")
    def resume():
        return action(lambda led, _: led.resume(), "Vydávání tipů obnoveno.")

    @app.post("/tips/<int:tip_id>/cancel")
    def tip_cancel(tip_id: int):
        def fn(led: Ledger, session):
            tip = session.get(Tip, tip_id)
            if tip is None:
                raise ValueError("Tip neexistuje.")
            led.cancel_tip(tip, "nevsazeno")

        return action(fn, "Tip zrušen.")

    @app.post("/tickets")
    def ticket_add():
        def fn(led: Ledger, _):
            f = request.form
            tip_id = int(f["tip_id"]) if f.get("tip_id") else None
            name = f.get("match_name", "").strip()
            day = date.fromisoformat(f["match_day"]) if f.get("match_day") else None
            market, selection = f.get("market") or None, f.get("selection") or None
            if tip_id is None and market:
                market, selection = _manual_bet(market, f.get("line", ""), selection or "")
            ticket = led.add_ticket(
                bookmaker=f["bookmaker"],
                odds=float(f["odds"]),
                stake=float(f["stake"]),
                tip_id=tip_id,
                match_id=None if tip_id else f"manual:{day}:{name}",
                market=market,
                selection=selection,
                match_day=day,
                match_name=name,
            )
            return f"Tiket #{ticket.id} zapsán: {ticket.match_name} {ticket.market} {ticket.selection} @ {ticket.odds:.2f}."

        return action(fn, "", tab="tikety")

    @app.post("/tickets/<int:ticket_id>/settle")
    def ticket_settle(ticket_id: int):
        def fn(led: Ledger, _):
            result = request.form["result"]
            if result not in RESULTS:
                raise ValueError("Neznámý výsledek.")
            closing = request.form.get("closing")
            t = led.settle_ticket(ticket_id, result, float(closing) if closing else None)
            return f"Tiket #{t.id} vyhodnocen: {t.profit_loss:+.0f} {SETTINGS.currency}."

        return action(fn, "", tab="tikety")

    @app.post("/scan")
    def scan():
        def fn(led: Ledger, _):
            from .pipeline import scan_all

            league = request.form.get("league", "all")
            reports = scan_all(led, None if league == "all" else [league], force=request.form.get("force") == "1")
            return " · ".join(r.summary().replace("\n  - ", " – ") for r in reports)

        return action(fn, "")

    @app.post("/profil2")
    def profil2_run():
        f = request.form
        form = {k: f.get(k, "") for k in ("match", "day", "league", "book", "lam_h", "lam_a", "ctx_h", "ctx_a", "odds")}
        form["use_czech"] = f.get("use_czech", "0")
        with factory() as session:
            led = Ledger(session)
            try:
                match = " - ".join(p.strip() for p in f["match"].replace("–", "-").split(" - ", 1))
                if " - " not in match:
                    raise ValueError("Zápas zapiš jako 'Domácí - Hosté'.")
                entries, errors = profile2.parse_odds(f.get("odds", ""), f.get("book") or "tipsport")
                if f.get("use_czech") == "1":
                    entries += profile2.from_czech(czechodds.load(led, utcnow()), match)
                if errors and not entries:
                    raise ValueError("; ".join(errors))
                manual = None
                if f.get("lam_h") and f.get("lam_a"):
                    manual = (float(f["lam_h"].replace(",", ".")), float(f["lam_a"].replace(",", ".")))
                home, away = match.split(" - ", 1)
                lams, source = profile2.combine_lambdas(manual, f.get("league", ""), home, away, date.today())
                ctx = (float((f.get("ctx_h") or "1").replace(",", ".")), float((f.get("ctx_a") or "1").replace(",", ".")))
                a = profile2.analyse(match, entries, lams, source, f.get("league", ""), f.get("day", ""), ctx,
                                     led.bankroll(), led.open_exposure(date.today()), utcnow())
                profile2.save(led, a)
                session.commit()
                msg = f"Profil 2: {match} spočten ({len(a.offers)} nabídek)." + (f" Přeskočeno: {'; '.join(errors)}" if errors else "")
                return render(session, msg, tab="profil2", p2form=form)
            except (ValueError, KeyError) as e:
                session.rollback()
                return render(session, f"Chyba: {e}", err=True, tab="profil2", p2form=form), 400

    @app.post("/profil2/track")
    def profil2_track():
        def fn(led: Ledger, _):
            a = next((x for x in profile2.load(led) if x.match == request.form["match"] and x.day == request.form["day"]), None)
            if a is None:
                raise ValueError("Rozbor nenalezen.")
            n = profile2.track_picks(led, a, utcnow())
            return f"Do sledování přidáno: {n}." if n else "Nic nového ke sledování (už sledováno, nebo trh nejde vyhodnotit)."

        return action(fn, "", tab="profil2")

    @app.post("/profil2/delete")
    def profil2_delete():
        return action(lambda led, _: profile2.remove(led, request.form["match"], request.form["day"]), "Rozbor smazán.",
                      tab="profil2")

    @app.post("/calc")
    def calc():
        f = request.form
        data = {k: f.get(k, "") for k in ("sharp", "pick", "odds", "league")}
        data.update(p_model_in=f.get("p_model", ""), result=None, stake=0.0, stake_reason="")
        with factory() as session:
            try:
                sharp = [float(x) for x in f["sharp"].replace(",", ".").replace(";", " ").split()]
                pick = int(f["pick"]) - 1
                if not 0 <= pick < len(sharp):
                    raise ValueError("Výběr je mimo počet zadaných kurzů.")
                league = LEAGUES[f["league"]]
                p_market = devig.fair_line(sharp).probs[pick]
                p_model = float(f["p_model"]) / 100.0 if f.get("p_model") else p_market
                odds = float(f["odds"])
                thr = SETTINGS.value.ev_threshold(league.tier)
                res = devig.assess_value(p_model, p_market, odds, thr)
                led = Ledger(session)
                st = risk.size_stake(res.prob, odds, led.bankroll(), led.open_exposure(date.today()), paused=led.paused())
                data.update(result=res, stake=st.stake, stake_reason=st.reason)
                return render(session, calc=data, tab="kalkulacka")
            except (ValueError, KeyError) as e:
                return render(session, f"Chyba: {e}", err=True, calc=data, tab="kalkulacka"), 400

    return app


def run(database_url: str | None = None, host: str = "127.0.0.1", port: int = 8000) -> None:
    create_app(database_url).run(host=host, port=port)
