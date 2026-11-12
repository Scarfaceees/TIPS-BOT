"""Webovy panel: prehled, tipy, tikety, kalkulacka hodnoty, historie, nastaveni."""

from __future__ import annotations

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

from . import analysis, combo, detail, devig, risk
from .config import LEAGUES, SETTINGS
from .storage import RESULTS, TICKET_OPEN, Ledger, Ticket, Tip, make_session_factory, utcnow

TABS = (("prehled", "Přehled"), ("kalkulacka", "Kalkulačka"), ("sazka", "Zapsat sázku"),
        ("historie", "Historie"), ("nastaveni", "Nastavení"))

PAGE = """<!doctype html>
<html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tips Bot</title>
<style>
:root{--bg:#f3f5f8;--card:#fff;--line:#dfe4ea;--text:#18212b;--muted:#64707d;--ok:#16794a;--okbg:#e4f5ec;
--bad:#b42318;--badbg:#fdeceb;--acc:#1d5fd1;--accbg:#e8f0fd;--warn:#8a5a00;--warnbg:#fff3d6}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--text)}
header{background:var(--card);border-bottom:1px solid var(--line)}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px}
.top{display:flex;align-items:center;gap:12px;padding:14px 0 10px}.top h1{font-size:20px;margin:0}
nav{display:flex;gap:4px;overflow-x:auto}nav a{padding:10px 14px;text-decoration:none;color:var(--muted);border-bottom:3px solid transparent;white-space:nowrap;font-weight:600}
nav a.on{color:var(--acc);border-color:var(--acc)}nav a:hover{color:var(--text)}
main{padding:20px 0 48px}h2{font-size:17px;margin:26px 0 10px}h2:first-child{margin-top:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:12px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:8px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat span{display:block;color:var(--muted);font-size:13px}.stat b{font-size:21px}.stat small{color:var(--muted);font-weight:400;font-size:13px}
.muted{color:var(--muted);font-size:14px}.pos{color:var(--ok)}.neg{color:var(--bad)}
.badge{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600;background:var(--accbg);color:var(--acc)}
.badge.warn{background:var(--warnbg);color:var(--warn)}.badge.bad{background:var(--badbg);color:var(--bad)}
.msg{padding:11px 14px;border-radius:8px;margin-bottom:14px;background:var(--okbg);color:var(--ok);font-weight:600}
.msg.err{background:var(--badbg);color:var(--bad)}.note{background:var(--warnbg);color:var(--warn);border-color:#f0d99a}
.tip{display:grid;grid-template-columns:1fr auto;gap:8px 20px;align-items:start}
.tip .title{font-size:16px;font-weight:700}.tip .pick{font-size:15px;margin-top:2px}
.kv{display:flex;flex-wrap:wrap;gap:4px 18px;margin-top:6px;color:var(--muted);font-size:14px}.kv b{color:var(--text)}
.big{font-size:22px;font-weight:700;text-align:right}.big small{display:block;font-size:13px;font-weight:400;color:var(--muted)}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:end;margin-top:12px}form.inline{display:inline}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
label{font-size:13px;color:var(--muted);display:flex;flex-direction:column;gap:4px;flex:1 1 130px}
input,select{background:#fff;color:var(--text);border:1px solid #c5ccd5;border-radius:7px;padding:9px 10px;font-size:15px;width:100%}
input:focus,select:focus{outline:2px solid var(--accbg);border-color:var(--acc)}
button{background:var(--acc);color:#fff;border:0;border-radius:7px;padding:10px 16px;font-size:14px;font-weight:600;cursor:pointer}
button.ghost{background:#eef1f5;color:var(--text)}button.ok{background:var(--ok)}button.bad{background:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:14px}th{color:var(--muted);font-weight:600;font-size:13px}
td,th{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left}tr:last-child td{border-bottom:0}td.num,th.num{text-align:right}
.verdict{margin-top:14px;padding:14px;border-radius:9px;background:var(--badbg)}.verdict.yes{background:var(--okbg)}
.verdict b.v{font-size:19px}.help{margin:0 0 4px;padding-left:18px;color:var(--muted);font-size:14px}
.bar{display:flex;flex-wrap:wrap;gap:12px 24px;align-items:end;justify-content:space-between;margin-bottom:6px}
.legend{font-size:13px;color:var(--muted)}.note-inline{color:var(--warn)}
.badge.tipb{background:var(--ok);color:#fff}
.match{padding:0;overflow:hidden}.mh{display:flex;flex-wrap:wrap;gap:10px 20px;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--line)}
.rec{text-align:right;padding:8px 12px;border-radius:8px}.rec small{display:block;color:var(--muted);font-size:12px}.recyes{background:var(--okbg)}.recno{background:#f3f4f6}
.tablewrap{overflow-x:auto;padding:0 8px}table.mk td,table.mk th{white-space:nowrap}table.mk td:last-child{white-space:normal;min-width:170px;font-size:13px}tr.best td{background:#f4f9ff;font-weight:600}
.det{border-top:1px solid var(--line);padding:10px 16px}.det summary{cursor:pointer;font-weight:600;color:var(--acc)}.det h4{margin:12px 0 4px}.det ul{margin:0;padding-left:18px}.det textarea{width:100%;font:13px/1.4 monospace;border:1px solid #c5ccd5;border-radius:7px;padding:8px}
.tipbox{border-top:1px solid var(--line);padding:12px 16px;background:#fbfdf9}.tipbox .row{margin-top:8px}
@media(max-width:720px){.stats{grid-template-columns:repeat(2,1fr)}.tip{grid-template-columns:1fr}.big{text-align:left}}
</style></head><body>
<header><div class="wrap">
<div class="top"><h1>Tips Bot</h1>
{% if s.test_mode %}<span class="badge warn">TESTOVACÍ REŽIM</span>{% endif %}
{% if s.paused %}<span class="badge bad">STOP-LOSS</span>{% endif %}</div>
<nav>{% for key, label in tabs %}<a href="{{ url_for('index', tab=key) }}" class="{{ 'on' if tab == key }}">{{ label }}</a>{% endfor %}</nav>
</div></header>
<main><div class="wrap">
{% if msg %}<div class="msg {{ 'err' if err else '' }}">{{ msg }}</div>{% endif %}

{% if tab == 'prehled' %}
<div class="stats">
  <div class="stat"><span>Bankroll</span><b>{{ '%.0f'|format(s.bankroll) }} {{ cur }}</b></div>
  <div class="stat"><span>Zisk / ztráta</span><b class="{{ 'pos' if s.profit_loss >= 0 else 'neg' }}">{{ '%+.0f'|format(s.profit_loss) }} {{ cur }}</b></div>
  <div class="stat"><span>Yield</span><b>{{ '%+.2f'|format(s.yield_pct) }} %</b></div>
  <div class="stat"><span>Průměrné CLV</span><b>{{ '%+.2f'|format(s.avg_clv_pct) }} %</b></div>
  <div class="stat"><span>Úspěšnost</span><b>{{ '%.1f'|format(s.win_rate_pct) }} %</b></div>
  <div class="stat"><span>Sázky</span><b>{{ '%.0f'|format(s.settled_bets) }}</b> <small>+ {{ '%.0f'|format(s.open_bets) }} otevřených</small></div>
  <div class="stat"><span>Propad od vrcholu</span><b>{{ '%.1f'|format(s.drawdown_pct) }} %</b> <small>limit 15 %</small></div>
  <div class="stat"><span>Dnes vsazeno</span><b>{{ '%.0f'|format(exposure) }}</b> <small>z {{ '%.0f'|format(exposure_cap) }} {{ cur }}</small></div>
</div>
{% if s.bankroll <= 0 %}<div class="card note"><b>Nastav bankroll</b> – bez něj bot nespočítá výši vkladu.
<form class="row" method="post" action="{{ url_for('deposit') }}">
<label>Částka ({{ cur }})<input name="amount" type="number" step="1" min="1" required></label>
<label>Režim<select name="test"><option value="1">Testovací (virtuální peníze)</option><option value="0">Ostrý</option></select></label>
<button>Uložit</button></form></div>{% endif %}
{% if s.paused %}<div class="card note">Propad dosáhl limitu, tipy jsou pozastavené do re-kalibrace.
<form class="row" method="post" action="{{ url_for('resume') }}"><button>Obnovit vydávání tipů</button></form></div>{% endif %}

<div class="bar">
{% if has_key %}<form class="row" style="margin:0" method="post" action="{{ url_for('scan') }}">
<label style="flex:0 1 260px">Soutěž<select name="league"><option value="all">Všechny soutěže</option>{% for k in scan_leagues %}<option value="{{ k }}">{{ leagues[k].name }}</option>{% endfor %}</select></label>
<label style="flex:0 0 auto;flex-direction:row;align-items:center;gap:6px"><input type="checkbox" name="force" value="1" style="width:auto"> nová data (stojí kredity)</label><button>Načíst kurzy a rozbor</button></form>
{% else %}<div class="note-inline">Živé kurzy nejsou zapnuté. Zadej API klíč v záložce <a href="{{ url_for('index', tab='nastaveni') }}">Nastavení</a>.</div>{% endif %}
<div class="legend"><span class="badge tipb">TIP</span> ověřená hodnota, vsadit &nbsp; <span class="badge tipb">CÍL</span> doporučený tip, sázet jen při kurzu od uvedené hranice &nbsp; <span class="badge">VALUE</span> hodnota je, ale tip nevznikl &nbsp; <span class="badge bad">BEZ VALUE</span> nesázet</div>
</div>

<h2>Tikety – doporučené kombinace</h2>
{% for c in combos %}<div class="card"><div class="tip"><div>
  <div class="title">{{ c.legs|length }}× kombinace <span class="badge {{ 'tipb' if loop.first else '' }}">{{ 'nejlepší' if loop.first else '' }}</span></div>
  {% for lg in c.legs %}<div class="pick">{{ loop.index }}. <b>{{ lg.row.label }}</b> – {{ lg.match }} @ {{ '%.2f'|format(lg.row.best_odds) }} <span class="muted">({{ lg.row.best_book }}, výkop {{ lg.local }})</span></div>{% endfor %}
  <div class="kv"><span>Pravděpodobnost výhry tiketu <b>{{ '%.1f'|format(c.prob * 100) }} %</b></span><span>Férový kurz <b>{{ '%.2f'|format(c.fair_odds) }}</b></span><span>EV <b>{{ '%+.1f'|format(c.ev_pct) }} %</b></span></div></div>
  <div class="big">{{ '%.2f'|format(c.odds) }}<small>celkový kurz · vklad {{ '%.0f'|format(c.stake) }} {{ cur }}</small></div></div>
  <div class="muted" style="margin-top:6px">Sázet jen, pokud kancelář dá celkový kurz alespoň <b>{{ '%.2f'|format(c.min_odds) }}</b>. Každá sázka je z jiného zápasu, takže jsou nezávislé.</div></div>
{% else %}<div class="card muted">Zatím není z čeho tiket složit – potřebuju aspoň dvě příležitosti s hodnotou z různých zápasů.</div>{% endfor %}

<h2>Zápasy ({{ views|length }})</h2>
{% for v in views %}{% set rec = v.rows[v.recommended] %}{% set tip = tips_by_match.get(v.match_id) %}
<div class="card match">
  <div class="mh"><div><div class="title">{{ v.name }}</div>
    <div class="muted">{{ leagues[v.league].name if v.league in leagues else v.league }} · výkop {{ v.local }}{% if not v.has_model %} · <span class="neg">bez modelu, jen trh</span>{% endif %}</div></div>
    {% if v.verdict == 'CÍL' %}{% set tg = v.rows[v.target] %}
    <div class="rec recyes"><small>Tip (cíl) – ověř kurz u kanceláře</small>
      <b>{{ tg.label }}</b> <span class="badge tipb">CÍL</span>
      <small>šance {{ '%.0f'|format(tg.p_model * 100) }} % · sázet od kurzu <b>{{ '%.2f'|format(tg.min_odds) }}</b> · férový {{ '%.2f'|format(tg.fair_odds) }}{% if v.target_stake %} · vklad ~{{ '%.0f'|format(v.target_stake) }} {{ cur }}{% endif %}</small></div>
    {% else %}<div class="rec {{ 'recyes' if v.verdict in ('TIP', 'VALUE') else 'recno' }}"><small>Doporučení</small>
      <b>{{ rec.label }}</b> @ {{ '%.2f'|format(rec.best_odds) }}
      <span class="badge {{ 'tipb' if v.verdict == 'TIP' else ('bad' if v.verdict.startswith('BEZ') else '') }}">{{ v.verdict }}</span>
      <small>EV {{ '%+.1f'|format(rec.ev_pct) }} % · {{ rec.best_book }}</small></div>{% endif %}</div>
  <div class="tablewrap"><table class="mk"><tr><th>Trh</th><th class="num">Model</th><th class="num">Trh (fér.)</th><th class="num">Fér. kurz</th><th class="num">Kurz (medián / česká)</th><th class="num">EV</th><th></th></tr>
  {% for r in v.rows %}<tr class="{{ 'best' if loop.index0 == v.recommended }}"><td>{{ r.label }}</td>
    <td class="num">{{ '%.1f'|format(r.p_model * 100) }} %</td>
    {% if r.status == 'model' %}<td class="num muted">–</td><td class="num">{{ '%.2f'|format(r.fair_odds) }}</td><td class="num muted" colspan="2">sázet od kurzu <b>{{ '%.2f'|format(r.min_odds) }}</b></td>
    <td><span class="muted">jen model</span></td>
    {% else %}<td class="num">{{ '%.1f'|format(r.p_market * 100) }} %</td>
    <td class="num">{{ '%.2f'|format(r.fair_odds) }}</td><td class="num"><b>{{ '%.2f'|format(r.best_odds) }}</b> <span class="muted">{{ r.best_book }}</span></td>
    <td class="num {{ 'pos' if r.ev_pct > 0 else 'neg' }}">{{ '%+.1f'|format(r.ev_pct) }} %</td>
    <td>{% if r.status == 'tip' %}<span class="badge tipb">TIP</span>{% elif r.status == 'value' %}<span class="badge">VALUE</span>{% else %}<span class="muted">{{ r.note }}</span>{% endif %}</td>{% endif %}</tr>{% endfor %}</table></div>
  <details class="det"><summary>Podrobná analýza a podklady</summary>
    {% for sec in v.sections %}<h4>{{ sec.title }}</h4><ul>{% for l in sec.lines %}<li>{{ l }}</li>{% endfor %}</ul>{% endfor %}
    {% if not v.sections %}<p class="muted">Model nemá pro tento zápas data (stáhni je: tipsbot fetch-nhl / fetch-football).</p>{% endif %}
    <h4>Absence a zranění</h4>
    {% if v.absences %}<ul>{% for l in v.absences %}<li>{{ l }}</li>{% endfor %}</ul>
    {% else %}<p class="muted">{{ 'Žádné nahlášené absence u těchto týmů.' if v.absence_checked else 'Zdroj absencí nedostupný (u fotbalu zadej API-Football klíč v Nastavení).' }} Sestavy: zveřejňují se až těsně před zápasem – model je zatím nezapočítává.</p>{% endif %}
    {% if v.weather %}<h4>Počasí při výkopu</h4><p>{{ v.weather }}</p>{% endif %}
    {% if v.news %}<h4>Poslední zprávy (titulky)</h4><ul>{% for l in v.news %}<li>{{ l }}</li>{% endfor %}</ul>{% endif %}
    <h4>Text analýzy ke zkopírování</h4><textarea readonly rows="9" onclick="this.select()">{{ v.post }}</textarea>
  </details>
  {% if tip and partners.get(v.name) %}{% set pc = partners[v.name] %}<div class="tipbox" style="background:#f4f9ff"><b>Doplnit tiket o:</b>
    {% for lg in pc.legs if lg.match != v.name %}<b>{{ lg.row.label }}</b> – {{ lg.match }} @ {{ '%.2f'|format(lg.row.best_odds) }}{% endfor %}
    <span class="muted">→ celkový kurz {{ '%.2f'|format(pc.odds) }}, šance {{ '%.1f'|format(pc.prob * 100) }} %, EV {{ '%+.1f'|format(pc.ev_pct) }} %, vklad {{ '%.0f'|format(pc.stake) }} {{ cur }}</span></div>{% endif %}
  {% if tip %}<div class="tipbox"><div class="tip"><div><b>Tip: {{ tip.market }} → {{ tip.selection }}</b>{% if tip.needs_review %} <span class="badge warn">zkontrolovat kurz</span>{% endif %}{% if tip.early %} <span class="badge">časný</span>{% endif %}
    <div class="muted">Sázet jen při kurzu alespoň <b>{{ '%.2f'|format(tip.min_odds) }}</b> · doporučený vklad <b>{{ '%.0f'|format(tip.kelly_stake) }} {{ cur }}</b> · platí do {{ tip.valid_until.strftime('%H:%M') }} UTC</div></div></div>
  <form class="row" method="post" action="{{ url_for('ticket_add') }}"><input type="hidden" name="tip_id" value="{{ tip.id }}">
    <label>Kancelář<select name="bookmaker">{% for b in books %}<option>{{ b }}</option>{% endfor %}</select></label>
    <label>Skutečný kurz<input name="odds" type="number" step="0.01" min="1.01" required></label>
    <label>Vklad ({{ cur }})<input name="stake" type="number" step="1" value="{{ '%.0f'|format(tip.kelly_stake) }}" required></label>
    <button class="ok">Vsazeno</button><button class="ghost" formaction="{{ url_for('tip_cancel', tip_id=tip.id) }}" formnovalidate>Nevsadím</button></form></div>{% endif %}
</div>
{% else %}<div class="card muted">Zatím žádné zápasy. Klikni na „Načíst kurzy a rozbor".</div>{% endfor %}
{% for t in orphan_tips %}<div class="card"><b>{{ t.match_name }}</b> – tip {{ t.market }} → {{ t.selection }} @ {{ '%.2f'|format(t.raw_odds) }}
<form class="row" method="post" action="{{ url_for('tip_cancel', tip_id=t.id) }}"><button class="ghost">Zrušit tip</button></form></div>{% endfor %}

<h2>Otevřené tikety</h2>
{% for k in open_tickets %}<div class="card"><div class="tip">
  <div><div class="title">{{ k.match_name or k.match_id }}</div><div class="pick">{{ k.market }} → <b>{{ k.selection }}</b></div>
  <div class="kv"><span>Den zápasu <b>{{ k.match_day.strftime('%d.%m.%Y') }}</b></span><span>Kancelář <b>{{ k.bookmaker }}</b></span></div></div>
  <div class="big">{{ '%.2f'|format(k.odds) }}<small>vklad {{ '%.0f'|format(k.stake) }} {{ cur }}</small></div></div>
  <form class="row" method="post" action="{{ url_for('ticket_settle', ticket_id=k.id) }}">
    <label>Férový zavírací kurz (nepovinné, pro CLV)<input name="closing" type="number" step="0.01"></label>
    <button class="ok" name="result" value="won">Výhra</button>
    <button class="bad" name="result" value="lost">Prohra</button>
    <button class="ghost" name="result" value="void">Vráceno</button>
  </form></div>
{% else %}<div class="card muted">Žádné otevřené tikety.</div>{% endfor %}

{% elif tab == 'kalkulacka' %}
<h2>Kalkulačka hodnoty</h2>
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
  <div class="kv"><span>EV <b>{{ '%+.1f'|format(c.result.ev * 100) }} %</b> (práh {{ '%.1f'|format(c.result.ev_threshold * 100) }} %)</span>
  <span>Férový kurz <b>{{ '%.2f'|format(1 / c.result.p_market) }}</b></span><span>Minimální kurz <b>{{ '%.2f'|format(c.result.min_odds) }}</b></span>
  {% if c.result.is_value %}<span>Vklad <b>{{ '%.0f'|format(c.stake) }} {{ cur }}</b>{% if c.stake_reason != 'ok' %} ({{ c.stake_reason }}){% endif %}</span>{% endif %}</div>
  {% if c.result.reasons %}<div class="muted" style="margin-top:6px">Důvod: {{ c.result.reasons|join(', ') }}</div>{% endif %}
  {% if c.result.needs_review %}<div class="muted" style="margin-top:6px">Neobvykle vysoké EV – zkontroluj, že kurzy sedí.</div>{% endif %}
</div>{% endif %}</div>

{% elif tab == 'sazka' %}
<h2>Zapsat sázku ručně</h2>
<div class="card"><div class="muted">Pro sázky, které nevznikly z tipu bota.</div>
<form method="post" action="{{ url_for('ticket_add') }}"><input type="hidden" name="tab" value="sazka"><div class="grid2" style="margin-top:12px">
  <label>Zápas<input name="match_name" placeholder="Sparta - Slavia" required></label>
  <label>Den zápasu<input name="match_day" type="date" value="{{ today }}" required></label>
  <label>Trh<input name="market" value="1X2" required></label>
  <label>Výběr<input name="selection" placeholder="1" required></label>
  <label>Kancelář<select name="bookmaker">{% for b in books %}<option>{{ b }}</option>{% endfor %}</select></label>
  <label>Kurz<input name="odds" type="number" step="0.01" min="1.01" required></label>
  <label>Vklad ({{ cur }})<input name="stake" type="number" step="1" min="1" required></label>
</div><div class="row"><button>Zapsat sázku</button></div></form></div>

{% elif tab == 'historie' %}
<h2>Vyhodnocené sázky</h2>
<div class="card"><table><tr><th>Den</th><th>Zápas</th><th>Výběr</th><th>Kancelář</th><th class="num">Kurz</th><th class="num">Vklad</th><th class="num">Zisk / ztráta</th><th class="num">CLV</th></tr>
{% for k in settled %}<tr><td>{{ k.match_day.strftime('%d.%m.%Y') }}</td><td>{{ k.match_name or k.match_id }}</td>
<td>{{ k.market }} {{ k.selection }}</td><td>{{ k.bookmaker }}</td><td class="num">{{ '%.2f'|format(k.odds) }}</td><td class="num">{{ '%.0f'|format(k.stake) }}</td>
<td class="num {{ 'pos' if k.profit_loss >= 0 else 'neg' }}">{{ '%+.0f'|format(k.profit_loss) }}</td>
<td class="num">{{ '%+.1f %%'|format(k.clv_pct) if k.clv_pct is not none else '–' }}</td></tr>
{% else %}<tr><td colspan="8" class="muted">Zatím žádné vyhodnocené sázky.</td></tr>{% endfor %}</table></div>

{% elif tab == 'nastaveni' %}
<h2>Bankroll</h2>
<div class="card">Aktuální stav: <b>{{ '%.0f'|format(s.bankroll) }} {{ cur }}</b> · režim: <b>{{ 'testovací' if s.test_mode else 'ostrý' }}</b>
<form class="row" method="post" action="{{ url_for('deposit') }}"><input type="hidden" name="tab" value="nastaveni">
<label>Přidat vklad ({{ cur }})<input name="amount" type="number" step="1" min="1" required></label>
<label>Režim<select name="test"><option value="1" {{ 'selected' if s.test_mode }}>Testovací (virtuální peníze)</option><option value="0" {{ 'selected' if not s.test_mode }}>Ostrý</option></select></label>
<button>Uložit</button></form></div>
<h2>Informace o zápasech (zdarma)</h2>
<div class="card">Klíč API-Football (zranění a absence fotbalistů, města stadionů pro počasí; zdarma 100 požadavků/den, registrace na api-football.com): <b>{{ 'uložen' if has_football_key else 'chybí' }}</b>
<form class="row" method="post" action="{{ url_for('set_football_key') }}"><label>Klíč<input name="key" type="password" autocomplete="off" required></label><button>Uložit klíč</button></form></div>
<h2>Živé kurzy</h2>
<div class="card">API klíč z the-odds-api.com: <b>{{ 'uložen' if has_key else 'chybí' }}</b>
<form class="row" method="post" action="{{ url_for('set_key') }}">
<label>API klíč<input name="key" type="password" autocomplete="off" required></label><button>Uložit klíč</button></form></div>
{% endif %}
</div></main></body></html>"""


def create_app(database_url: str | None = None) -> Flask:
    app = Flask(__name__)
    factory = make_session_factory(database_url)
    valid_tabs = {k for k, _ in TABS}

    def render(session, msg: str = "", err: bool = False, calc: dict | None = None, tab: str = "prehled"):
        led = Ledger(session)
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
                    views.append(v)
        views.sort(key=lambda v: v.kickoff)
        local = {v.name: v.local for v in views}
        bank = led.bankroll()
        expo = led.open_exposure(today)
        combos = combo.best_combos(views, bank, expo)
        for c in combos:
            for lg in c.legs:
                object.__setattr__(lg, "local", local.get(lg.match, ""))
        partners = {v.name: combo.partner_for(v.name, views, bank, expo) for v in views if v.name in {t.match_name for t in open_tips}}
        for pc in partners.values():
            for lg in (pc.legs if pc else ()):
                object.__setattr__(lg, "local", local.get(lg.match, ""))
        return render_template_string(
            PAGE,
            s=type("S", (), s),
            cur=SETTINGS.currency,
            tabs=TABS,
            tab=tab if tab in valid_tabs else "prehled",
            msg=msg,
            err=err,
            views=views,
            combos=combos,
            partners=partners,
            tips_by_match=tips_by_match,
            orphan_tips=[t for t in open_tips if t.match_id not in {v.match_id for v in views}],
            open_tickets=list(session.scalars(select(Ticket).where(Ticket.status == TICKET_OPEN).order_by(Ticket.match_day))),
            settled=list(
                session.scalars(
                    select(Ticket).where(Ticket.status != TICKET_OPEN).order_by(Ticket.settled_at.desc()).limit(200)
                )
            ),
            books=SETTINGS.bookmakers.betting_books,
            leagues=LEAGUES,
            exposure=led.open_exposure(today),
            exposure_cap=s["bankroll"] * SETTINGS.risk.max_daily_exposure_pct,
            today=today.isoformat(),
            has_key=bool(led.api_key()),
            has_football_key=bool(led.api_football_key()),
            scan_leagues=list(SETTINGS.odds_api.sport_keys),
            c=type("C", (), calc) if calc else None,
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
            ticket = led.add_ticket(
                bookmaker=f["bookmaker"],
                odds=float(f["odds"]),
                stake=float(f["stake"]),
                tip_id=tip_id,
                match_id=None if tip_id else f"manual:{day}:{name}",
                market=f.get("market") or None,
                selection=f.get("selection") or None,
                match_day=day,
                match_name=name,
            )
            return f"Tiket #{ticket.id} zapsán."

        return action(fn, "")

    @app.post("/tickets/<int:ticket_id>/settle")
    def ticket_settle(ticket_id: int):
        def fn(led: Ledger, _):
            result = request.form["result"]
            if result not in RESULTS:
                raise ValueError("Neznámý výsledek.")
            closing = request.form.get("closing")
            t = led.settle_ticket(ticket_id, result, float(closing) if closing else None)
            return f"Tiket #{t.id} vyhodnocen: {t.profit_loss:+.0f} {SETTINGS.currency}."

        return action(fn, "")

    @app.post("/scan")
    def scan():
        def fn(led: Ledger, _):
            from .pipeline import scan_all

            league = request.form.get("league", "all")
            reports = scan_all(led, None if league == "all" else [league], force=request.form.get("force") == "1")
            return " · ".join(r.summary().replace("\n  - ", " – ") for r in reports)

        return action(fn, "")

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
