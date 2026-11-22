"""Webovy panel: prehled, tipy, tikety, kalkulacka hodnoty, historie, nastaveni."""

from __future__ import annotations

import json
import os
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


from flask import (Flask, flash, get_flashed_messages, jsonify, redirect, render_template_string, request, send_file,
                   url_for)
from sqlalchemy import select

from . import analysis, auto, backtest, combo, czechodds, detail, devig, metrics, paper, profile2, risk, secondopinion
from .config import LEAGUES, SETTINGS
from .storage import RESULTS, TICKET_OPEN, Ledger, Ticket, Tip, make_session_factory, utcnow

# (klic, nazev, ikona); prvni ctyri jsou na mobilu ve spodni liste, ostatni pod "Více"
TABS = (("prehled", "Přehled", "⚽", "Přehled"), ("tikety", "Moje tikety", "🎫", "Tikety"),
        ("sledovani", "Sledování", "📈", "Sledování"), ("overeni", "Ověření modelu", "🔬", "Ověření"),
        ("profil2", "Ruční rozbor", "✍️", "Rozbor"), ("profil4", "Profil 4 – Expert", "🧠", "Expert"), ("kalkulacka", "Kalkulačka", "🧮", "Kalkulačka"),
        ("export", "Export výsledků", "📤", "Export"), ("nastaveni", "Nastavení", "⚙️", "Nastavení"))
MAIN_TABS = 4
P2_FORM_KEY = "profil2_form"  # posledni vyplneny formular rucniho rozboru (aby po prepoctu zustal vyplneny)
TAB_ALIASES = {"sazka": "tikety", "historie": "tikety"}  # stare odkazy (historie je soucasti tiketu)

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
.topnav{display:flex;gap:2px;overflow-x:auto}.topnav a{padding:9px 12px;text-decoration:none;color:var(--muted);border-bottom:3px solid transparent;white-space:nowrap;font-weight:600}
.topnav a.on{color:var(--acc);border-color:var(--acc)}.topnav a:hover{color:var(--text)}
.topnav a .cnt{display:inline-block;min-width:18px;padding:0 5px;margin-left:4px;border-radius:99px;background:var(--bad);color:#fff;font-size:12px;text-align:center}
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
.mgrid.one{grid-template-columns:minmax(0,1fr)}.mgrid.one .czbox{display:none}
/* polozky mrizky se nesmi roztahnout podle siroke tabulky (jinak karta tabulku orizne a nejde posouvat) */
.mgrid>*,.cols>*{min-width:0}.tablewrap{max-width:100%;min-width:0}
@media(max-width:1000px){.mgrid{grid-template-columns:minmax(0,1fr)}}
@media(max-width:720px){.cols>aside .stats{grid-template-columns:repeat(2,1fr)}.stats{grid-template-columns:repeat(2,1fr)}.tip{grid-template-columns:1fr}.big{text-align:left}
.ms{grid-template-columns:44px 1fr 14px}.ms .rc{grid-column:2;text-align:left}.chips{margin-left:0;width:100%}}
/* rozbalovani a stav automatu */
.arow{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.autobar details{margin-top:4px}.autobar .notes{margin-top:6px;max-height:260px;overflow:auto}
.bottomnav{display:none}
.boxes{grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr))}.czgrid{grid-template-columns:repeat(auto-fill,minmax(min(250px,100%),1fr))}
.profbar{grid-template-columns:repeat(auto-fit,minmax(min(240px,100%),1fr))}
/* telefon: spodni lista jako v aplikaci, kompaktni karty, tabulky jako karty nebo s posuvem */
@media(max-width:720px){
body{font-size:15px;overflow-x:hidden;padding-bottom:calc(68px + env(safe-area-inset-bottom))}
.wrap{padding:0 12px}main{padding:10px 0 20px}h2{font-size:16px;margin:18px 0 8px}
header{position:static}.topnav{display:none}.top{padding:10px 0 8px;gap:4px 10px}.top h1{font-size:18px}
.top .badge{font-size:11px;padding:1px 7px}.chips{margin-left:auto;width:auto;gap:10px;font-size:13px}
.bottomnav{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:20;background:var(--card);border-top:1px solid var(--line);
 padding:4px 4px calc(4px + env(safe-area-inset-bottom));box-shadow:0 -2px 10px rgba(0,0,0,.06)}
.bottomnav>a,.bottomnav summary{flex:1;display:flex;flex-direction:column;align-items:center;gap:1px;padding:5px 2px;text-decoration:none;
 color:var(--muted);font-size:11px;font-weight:600;border-radius:10px;position:relative;list-style:none;cursor:pointer}
.bottomnav i{font-style:normal;font-size:20px;line-height:1.2}.bottomnav .on{color:var(--acc);background:var(--accbg)}
.bottomnav .cnt{position:absolute;top:0;right:calc(50% - 22px);min-width:16px;padding:0 4px;border-radius:99px;background:var(--bad);color:#fff;font-size:10px;line-height:16px}
.bottomnav details{flex:1;display:flex}.bottomnav details>summary{width:100%}
.bottomnav .sheet{position:fixed;left:8px;right:8px;bottom:calc(70px + env(safe-area-inset-bottom));background:var(--card);border:1px solid var(--line);
 border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.18);padding:6px;display:grid;gap:2px}
.bottomnav .sheet a{padding:13px 14px;border-radius:10px;text-decoration:none;color:var(--text);font-weight:600;font-size:15px}
input,select,textarea{font-size:16px}button{padding:11px 16px;font-size:15px}button.sm{padding:8px 12px;font-size:14px}
.card{padding:12px;border-radius:12px}.mbody{padding:4px 10px 12px}
.stats,.cols>aside .stats{grid-template-columns:repeat(2,1fr);gap:8px}.stat{padding:8px 10px}.stat b{font-size:17px}.stat span,.stat small{font-size:12px}
.tiles{grid-template-columns:repeat(3,minmax(0,1fr))}.tile b{font-size:15px}.big{font-size:20px}
.filters{overflow-x:auto;flex-wrap:nowrap;padding-bottom:4px;margin:0 -12px;padding-left:12px}.filters button{flex:0 0 auto}
.profbar{display:flex;overflow-x:auto;gap:8px;margin:0 -12px 12px;padding:0 12px 4px;scroll-snap-type:x mandatory}
.prof{flex:0 0 auto;max-width:72vw;scroll-snap-align:start;padding:8px 12px}.prof small{display:none}.prof .pst{display:block;font-size:12px}
.setgrid{columns:1}label{flex-basis:100%}.czgrid label,.scorebox label.sc{flex-basis:0}.scorebox label.sc{flex:1 1 70px}
.ms{grid-template-columns:44px 1fr 14px;gap:8px;padding:10px 12px}.ms .rc{font-size:13px}
main table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch;font-size:13px;max-width:100%}td,th{padding:6px}
main table:not(.bets) tr>:first-child{position:sticky;left:0;z-index:1;background:var(--card);box-shadow:1px 0 0 var(--line)}
main tr.best>:first-child{background:#f4f9ff}
main button{max-width:100%;white-space:normal}
table.mk td:first-child{min-width:110px}table.mk tr>:nth-child(3),table.mk tr>:nth-child(4){display:none}
table.bets{display:block}table.bets tr:first-child{display:none}
table.bets tr{display:grid;grid-template-columns:1fr 1fr;gap:2px 10px;padding:10px 2px;border-bottom:1px solid var(--line)}
table.bets td{border:0;padding:0;text-align:left}table.bets td:nth-child(1){grid-column:1/-1;font-size:12px;color:var(--muted)}
table.bets td:nth-child(2),table.bets td:nth-child(3),table.bets td:nth-child(6){grid-column:1/-1}
table.bets td:nth-child(4)::before{content:"Šance ";color:var(--muted)}table.bets td:nth-child(5)::before{content:"Čekaný zisk ";color:var(--muted)}
.betrow{flex-wrap:wrap;margin-top:6px}.betrow input{flex:1 1 80px}
.msg{position:sticky;top:6px;z-index:10;box-shadow:0 2px 10px rgba(0,0,0,.08)}
table.cardt{display:block;font-size:14px}table.cardt tr:first-child{display:none}
table.cardt tr{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 14px;padding:10px 4px;border-bottom:1px solid var(--line)}
table.cardt td{border:0;padding:0;text-align:left}table.cardt td:first-child,table.cardt td:nth-child(2){flex:1 0 100%}
table.cardt td[data-l]::before{content:attr(data-l) " ";color:var(--muted);font-weight:400}
table.cardt td:last-child:has(details){flex:1 0 100%}table.cardt .badge{border-radius:6px;font-size:11px}
}

/* trhy: tabulka na pocitaci, karty s filtrem na mobilu (do 768 px) */
.mcards,.mfilt{display:none}
/* tabulka trhu ma celou sirku zapasu (ceske kurzy jsou nepovinne, formular je pod ni) – bez vodorovneho posunu */
.mgrid{grid-template-columns:minmax(0,1fr)!important}table.mk th{white-space:normal;vertical-align:bottom}
table.mk td{white-space:normal!important}table.mk td .muted{font-size:12px}table.mk td,table.mk th{padding:7px 5px}
table.p4mk td,table.p4mk th{white-space:normal}
/* rozhoduje skutecna sirka seznamu trhu: kdyz se tabulka nevejde (napr. tablet s postrannim panelem), jsou karty */
.mlist{container-type:inline-size}
@container (max-width:520px){.mtable{display:none!important}.mcards{display:block!important}.mfilt{display:flex!important}}
@media(max-width:900px){main .grid2:has(table){grid-template-columns:minmax(0,1fr)}}
/* dalsi kombinace v uzkem pravem panelu: radky jako kompaktni bloky na vsech sirkach */
table.combot,table.combot tbody{display:block}table.combot tr:first-child{display:none}
table.combot tr{display:flex;flex-wrap:wrap;gap:2px 12px;padding:8px 2px;border-bottom:1px solid var(--line)}
table.combot td{border:0;padding:0;text-align:left;position:static!important;box-shadow:none!important}table.combot td:first-child{flex:1 0 100%}
table.combot td[data-l]::before{content:attr(data-l) " ";color:var(--muted)}
.mcard{border:1px solid var(--line);border-radius:12px;background:var(--card);margin:8px 0;box-shadow:var(--shadow)}
.mcard>summary{list-style:none;cursor:pointer;padding:12px 14px;display:block}.mcard>summary::-webkit-details-marker{display:none}
.mcard.rec{border:2px solid var(--acc);background:linear-gradient(180deg,var(--accbg),var(--card) 70%)}
.mcard .eyebrow{display:block;margin-bottom:4px;color:var(--acc)}
.mc-head{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}.mc-head b{font-size:15px;line-height:1.3;min-width:0;overflow-wrap:anywhere}.mc-head .badge{flex:0 0 auto}
.mc-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 12px;margin-top:10px}
.mc-grid>div{min-width:0}.mc-grid span{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.mc-grid b{display:block;font-size:17px}.mc-grid small{display:block;color:var(--muted);font-size:12px;overflow-wrap:anywhere}
.mc-more{display:inline-block;margin-top:10px;font-size:13px;font-weight:650;color:var(--acc)}.mc-more::after{content:" ▾"}
.mcard[open] .mc-more::after{content:" ▴"}
.mc-det{padding:0 14px 14px;border-top:1px solid var(--line)}.mc-sub{margin:12px 0 6px;font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.mc-models{display:flex;flex-wrap:wrap;gap:6px}.mc-models span{background:#f1f6fd;border-radius:8px;padding:5px 9px;font-size:13px}
.mc-books{list-style:none;margin:0;padding:0}.mc-books li{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-bottom:1px solid var(--line);font-size:14px}
.mc-books li span{min-width:0;overflow-wrap:anywhere}.mc-books small{color:var(--muted)}
td .mc-det{border:0;padding:6px 0 0;min-width:280px;white-space:normal}
.warnt{color:var(--warn)}.p4h{margin:18px 0 6px}
.mfilt{gap:6px;flex-wrap:wrap;margin:2px 0 8px}.chip{background:#edf3fb;color:#29415f;border:1px solid var(--line);border-radius:999px;padding:7px 12px;font-size:14px;box-shadow:none;min-height:36px}
.chip small{opacity:.7;margin-left:2px}.chip.on{background:var(--acc);color:#fff;border-color:var(--acc)}.chip:disabled{opacity:.4;cursor:default;transform:none}
.mc-more-list>summary{padding:10px 2px;font-weight:650}
@media(max-width:768px){
.mtable{display:none}.mcards{display:block}.mfilt{display:flex}
main table.mkt,main table.mkt tbody{display:table;width:100%;table-layout:fixed}main table.mkt tr{display:table-row}
main table.mkt td,main table.mkt th{white-space:normal;overflow-wrap:anywhere;position:static;box-shadow:none}
.p4nowrap td,.p4nowrap th{white-space:normal}.p4nowrap td.num{white-space:normal}
}

/* TIPS BOT — HYBRID PREMIUM DESIGN SYSTEM */
:root{color-scheme:light;--bg:#edf3fb;--card:#fff;--line:#dce6f3;--text:#14243d;--muted:#667892;--ok:#09865d;--okbg:#e4f7ef;--bad:#d44b59;--badbg:#fff0f1;--acc:#1769dc;--accbg:#e8f1ff;--warn:#a56a08;--warnbg:#fff5df;--navy:#071a32;--navy2:#0c2b50;--shadow:0 8px 24px rgba(19,52,91,.07)}
html{background:var(--bg)}body{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;background:linear-gradient(135deg,#f4f8fd 0%,#eaf1fa 100%);color:var(--text);font-size:14px;min-height:100vh}
.app-shell{min-height:100vh}.sidebar{position:fixed;z-index:30;inset:0 auto 0 0;width:228px;padding:22px 14px 18px;background:linear-gradient(180deg,#071a32 0%,#0a2545 60%,#0b3158 100%);color:#d8e8ff;display:flex;flex-direction:column;box-shadow:8px 0 30px rgba(8,28,54,.12)}
.brand{display:flex;align-items:center;gap:11px;padding:4px 8px 25px}.brandmark{width:48px;height:48px;border-radius:15px;display:grid;place-items:center;background:linear-gradient(145deg,#1478e8,#0a2b51);font-size:28px;box-shadow:0 8px 24px #0066d955}.brandname{font-size:20px;font-weight:850;letter-spacing:-.6px;color:#fff;line-height:1.1}.brandname small{display:block;font-size:10px;letter-spacing:.2px;color:#68c9ff;font-weight:600;margin-top:4px}
.side-label{font-size:10px;letter-spacing:1.5px;color:#7592b5;font-weight:800;padding:8px 12px 9px;text-transform:uppercase}.side-nav{display:grid;gap:5px}.side-nav a{display:flex;align-items:center;gap:12px;padding:11px 12px;border-radius:11px;text-decoration:none;color:#b9cce4;font-weight:650;transition:.18s}.side-nav a .navico{width:22px;text-align:center;font-size:17px}.side-nav a:hover{background:#ffffff12;color:#fff}.side-nav a.on{background:linear-gradient(100deg,#1769d8,#104b93);color:#fff;box-shadow:0 7px 18px #0065d633}.side-nav .cnt{margin-left:auto}.side-bottom{margin-top:auto;display:grid;gap:12px}.system-card{padding:13px;border:1px solid #24476b;border-radius:13px;background:#ffffff09}.system-card b{display:block;color:#fff;font-size:12px}.system-card small{color:#9db4d0;font-size:11px}.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#27d59a;box-shadow:0 0 0 4px #27d59a22;margin-right:7px}
.app-main{margin-left:228px;min-width:0}.topbar{height:76px;position:sticky;top:0;z-index:20;background:rgba(7,26,50,.96);backdrop-filter:blur(16px);border:0;box-shadow:0 8px 25px rgba(4,24,48,.12);color:#fff}.topbar .wrap{max-width:none;padding:0 30px;height:100%;display:flex;align-items:center}.topbar .top{width:100%;padding:0;display:flex;align-items:center;gap:16px}.topbar .top h1{font-size:20px;color:#fff;font-weight:750;letter-spacing:-.4px}.topbar .top .badge{margin-left:4px}.topbar .chips{margin-left:auto;display:flex;align-items:center;gap:20px;color:#9eb4ce;font-size:12px}.topbar .chips b{color:#fff;font-size:13px}.topbar .topnav{display:none}.top-meta{display:flex;align-items:center;gap:12px;margin-left:auto}.top-status{display:flex;align-items:center;gap:9px;color:#b8cce4;font-size:12px}.top-status strong{display:block;color:#fff;font-size:13px}.status-orb{width:32px;height:32px;display:grid;place-items:center;border-radius:50%;background:#0e9b72;color:white;font-size:16px;box-shadow:0 0 0 5px #0e9b7222}.top-profile{display:flex;align-items:center;gap:9px;padding:7px 11px;border:1px solid #244362;border-radius:12px;color:#dbe9fb;font-size:12px}.top-profile .avatar{width:30px;height:30px;border-radius:50%;background:#1b5b9f;display:grid;place-items:center;font-size:16px}
.wrap{max-width:1700px;padding:0 26px}main{padding:24px 0 54px}main>.wrap{max-width:none}h2{font-size:18px;font-weight:800;letter-spacing:-.3px;margin:26px 0 12px;color:#132b4a}h2:first-child{margin-top:0}.card,.stat,.prof{border:1px solid #dce6f2;border-radius:15px;background:#fff;box-shadow:var(--shadow)}.card{padding:17px 19px;margin-bottom:13px}.stat{padding:16px 17px;min-height:105px;position:relative;overflow:hidden}.stat:after{content:"";position:absolute;right:-20px;bottom:-34px;width:100px;height:100px;border-radius:50%;background:linear-gradient(135deg,#eaf3ff,#f7fbff);z-index:0}.stat>*{position:relative;z-index:1}.stat span{font-size:12px;font-weight:650;color:#6d7f98}.stat b{font-size:25px;letter-spacing:-.6px;color:#142c4a}.stat small{font-size:11px;color:#8292a8;margin-top:3px}.stats{gap:12px;margin-bottom:16px}.cols{grid-template-columns:minmax(0,1fr) 370px;gap:20px}.cols>aside .stats{grid-template-columns:repeat(2,1fr)}
button{background:linear-gradient(135deg,#1975e8,#1057b8);border-radius:9px;padding:10px 16px;box-shadow:0 4px 12px #1769dc22;transition:transform .15s,box-shadow .15s}button:hover{transform:translateY(-1px);box-shadow:0 7px 18px #1769dc35}button.ghost{background:#edf3fb;color:#29415f;box-shadow:none}button.ok{background:linear-gradient(135deg,#0eaa78,#087b58)}button.bad{background:#d84b5a}input,select,textarea{background:#f9fbfe;border:1px solid #d5e0ee;border-radius:9px;padding:10px 12px;color:#18304e}input:focus,select:focus,textarea:focus{outline:3px solid #1769dc22;border-color:#4388e7}label{font-size:12px;font-weight:650;color:#647894}.badge{border-radius:7px;background:#e9f1ff;color:#1b62c1;padding:4px 9px;font-size:11px}.badge.tipb{background:#dcf7eb;color:#08794f}.badge.warn{background:#fff3d8;color:#94600a}.badge.bad{background:#ffe9eb;color:#bd3344}.msg{border-radius:11px;box-shadow:var(--shadow)}.note{background:#fff8e8;border-color:#f1dfb9;color:#865a0a}.tipbox{border-radius:12px}.tipbox.alt{border-radius:12px}.verdict{border-radius:12px}.tile{border-radius:11px;background:#f1f6fd}.tile.good{background:#e4f7ef}.tile.bad{background:#fff0f1}.prof{border-radius:12px}.prof.on{background:#edf4ff}.match{border-radius:12px}.ms{padding:14px 17px}.ms:hover{background:#f5f9ff}.mbody{padding:8px 17px 16px}.box{border-radius:12px;background:#f9fbfe;border-color:#e2eaf4}.czbox{background:#f7faff;border-radius:12px;border-color:#e0eaf6}.day{color:#6f86a3}.tablewrap{border-radius:10px}table{font-size:13px}td,th{border-color:#e8eef6}th{color:#70829b}tr.best td{background:#eff6ff}
.autobar{background:linear-gradient(110deg,#092344,#0e3763);color:#e9f4ff;border:1px solid #174a7a;box-shadow:0 12px 28px #092b5030}.autobar .muted,.autobar .fold>summary{color:#aec6e2}.autobar .badge{background:#dff8ec;color:#08794f}.autobar .fold{margin-top:8px}.autobar .notes{color:#d5e5f7}.arow b{font-size:16px}.autobar button.ghost{background:#1c4d7d;color:white}
.bottomnav{display:none}.side-nav a .cnt{background:#e64e60}
@media(max-width:1100px){.sidebar{width:205px}.app-main{margin-left:205px}.wrap{padding:0 18px}.cols{grid-template-columns:1fr}.cols>aside,.cols>.colmain{grid-column:1;grid-row:auto}.cols>aside .stats{grid-template-columns:repeat(4,1fr)}}
@media(max-width:720px){body{padding-bottom:calc(72px + env(safe-area-inset-bottom));background:#edf3fb}.sidebar{display:none}.app-main{margin-left:0}.topbar{height:62px;position:sticky}.topbar .wrap{padding:0 14px}.topbar .top{gap:8px}.topbar .top h1{font-size:17px}.topbar .chips{display:none}.top-status{font-size:10px}.top-status strong{font-size:11px}.status-orb{width:28px;height:28px}.top-profile{padding:4px 7px}.top-profile .avatar{width:27px;height:27px}.wrap{padding:0 12px}main{padding:14px 0 24px}.card{padding:13px;border-radius:13px}.stats,.cols>aside .stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.stat{min-height:88px;padding:11px}.stat b{font-size:20px}.cols{gap:12px}.cols>aside .stats{grid-template-columns:repeat(2,minmax(0,1fr))}.bottomnav{display:flex;position:fixed;left:8px;right:8px;bottom:calc(7px + env(safe-area-inset-bottom));z-index:40;background:rgba(7,26,50,.97);border:1px solid #244668;border-radius:17px;padding:5px 5px;box-shadow:0 8px 28px #071a3255;backdrop-filter:blur(14px)}.bottomnav>a,.bottomnav summary{color:#a9bfd9;font-size:10px;padding:5px 2px}.bottomnav i{font-size:19px}.bottomnav .on{color:#fff;background:#155cae}.bottomnav .sheet{bottom:calc(82px + env(safe-area-inset-bottom));background:#0b2441;border-color:#284a70}.bottomnav .sheet a{color:#e2efff}.bottomnav .sheet a.on{background:#155cae;color:#fff}.msg{position:relative;top:auto}.ms{padding:11px 12px}.mbody{padding:6px 10px 12px}.mgrid{grid-template-columns:1fr}.topbar .top .badge{display:none}}
@media(min-width:721px) and (max-width:1100px){.sidebar{width:76px;padding:18px 9px}.brand{justify-content:center;padding:4px 0 22px}.brandname,.side-label,.side-nav a span:not(.navico),.side-bottom{display:none}.side-nav a{justify-content:center;padding:12px 6px}.side-nav a .navico{font-size:20px}.app-main{margin-left:76px}}


/* HYBRID PREMIUM — dashboard composition and component polish */
.dashboard-kpis{margin:0 0 18px}
.dashboard-kpis .stats{grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:0}
.dashboard-layout{grid-template-columns:minmax(0,1fr) 332px;gap:18px;align-items:start}
.dashboard-layout>.dashboard-rail{grid-column:2;grid-row:1;min-width:0}
.dashboard-layout>.dashboard-main-column{grid-column:1;grid-row:1;min-width:0}
.dashboard-rail .card{box-shadow:0 8px 22px rgba(19,52,91,.055)}
.dashboard-rail h2{font-size:16px}
.dashboard-main-column>h2{margin-top:22px}
.dashboard-main-column>h2:first-of-type{margin-top:0}
.section-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin:0 0 12px}
.section-heading h2{margin:3px 0 0;font-size:19px}
.eyebrow{display:block;color:#4380c8;font-size:10px;letter-spacing:1.5px;font-weight:850}
.text-link{color:#1769dc;text-decoration:none;font-size:12px;font-weight:750;white-space:nowrap}
.opportunity-panel{margin:2px 0 20px}
.opportunity-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:11px}
.opportunity-card{min-width:0;padding:15px;border:1px solid #d9e6f5;border-radius:15px;background:linear-gradient(145deg,#fff 0%,#f6faff 100%);box-shadow:0 8px 22px rgba(19,52,91,.06)}
.opportunity-top{display:flex;align-items:center;justify-content:space-between;gap:7px;margin-bottom:12px}
.market-icon{width:29px;height:29px;display:grid;place-items:center;border-radius:9px;background:#e6f1ff;color:#1769dc;font-size:19px}
.opportunity-match{font-size:14px;line-height:1.35;font-weight:800;color:#172e4d;min-height:38px}
.opportunity-pick{margin-top:4px;color:#1769dc;font-size:13px;font-weight:750}
.opportunity-metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin-top:13px}
.opportunity-metrics>div{padding:9px 7px;border-radius:9px;background:#edf4ff;min-width:0}
.opportunity-metrics small{display:block;color:#7185a0;font-size:10px;line-height:1.25}
.opportunity-metrics b{display:block;margin-top:4px;color:#183453;font-size:17px;white-space:nowrap}
.opportunity-metrics .metric-positive{background:#e3f8ee}
.opportunity-metrics .metric-positive b{color:#078457}
.opportunity-foot{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:12px;padding-top:10px;border-top:1px solid #e4edf7;color:#7185a0;font-size:11px}
.opportunity-foot a{color:#1769dc;text-decoration:none;font-weight:750}
.welcome-panel{position:relative;display:flex;align-items:center;justify-content:space-between;gap:22px;min-height:235px;margin:2px 0 20px;padding:27px 30px;overflow:hidden;border:1px solid #1b4e83;border-radius:18px;background:radial-gradient(circle at 82% 45%,#1a5b9a 0,#123d6a 27%,transparent 54%),linear-gradient(120deg,#071d38 0%,#0c2e54 65%,#123f70 100%);box-shadow:0 14px 30px rgba(7,35,68,.16);color:#fff}
.welcome-copy{position:relative;z-index:2;max-width:690px}
.welcome-copy .eyebrow{color:#7bcaff}
.welcome-copy h2{max-width:650px;margin:9px 0 10px;color:#fff;font-size:clamp(21px,2.3vw,29px);line-height:1.16;letter-spacing:-.7px}
.welcome-copy p{max-width:650px;margin:0;color:#c3d7ee;font-size:13px;line-height:1.7}
.welcome-actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:19px}
.welcome-actions form{margin:0}
.welcome-actions button,.primary-link{display:inline-flex;align-items:center;gap:9px;padding:11px 16px;border-radius:9px;background:linear-gradient(135deg,#2b8bff,#1260ce);color:#fff;text-decoration:none;font-size:13px;font-weight:800;box-shadow:0 7px 18px #0066d955}
.secondary-link{padding:10px 13px;border:1px solid #527ba6;border-radius:9px;color:#d6eaff;text-decoration:none;font-size:12px;font-weight:700}
.welcome-visual{position:relative;flex:0 0 240px;height:190px;display:grid;place-items:center}
.bot-core{position:relative;z-index:2;width:94px;height:94px;display:grid;place-items:center;border:1px solid #67c9ff;border-radius:28px;background:linear-gradient(145deg,#2a91e8,#092a4e);box-shadow:0 0 0 12px #54bfff12,0 0 45px #39aaff77;font-size:52px}
.orbit{position:absolute;left:50%;top:50%;border:1px solid #71caff55;border-radius:50%;transform:translate(-50%,-50%) rotate(-24deg)}
.orbit-one{width:205px;height:92px}.orbit-two{width:165px;height:155px;transform:translate(-50%,-50%) rotate(42deg)}
.orbit-dot{position:absolute;width:10px;height:10px;border-radius:50%;background:#62d7ff;box-shadow:0 0 16px #62d7ff}
.dot-one{top:25px;right:30px}.dot-two{bottom:22px;left:27px;background:#44e0b1;box-shadow:0 0 16px #44e0b1}
.visual-chip{position:absolute;z-index:3;display:grid;gap:1px;padding:7px 10px;border:1px solid #477eae;border-radius:9px;background:#0a2949eF;box-shadow:0 8px 18px #06182e55}
.visual-chip span{color:#80b8e9;font-size:9px;font-weight:800;letter-spacing:1px}.visual-chip b{color:#fff;font-size:11px}
.chip-one{top:9px;right:-4px}.chip-two{bottom:7px;left:-5px}
.count-pill{display:inline-flex;min-width:24px;height:24px;align-items:center;justify-content:center;padding:0 7px;border-radius:99px;background:#e7f0ff;color:#1769dc;font-size:12px}
.empty-list-card{display:flex;align-items:center;gap:16px;padding:21px;border:1px dashed #c4d7ec;border-radius:14px;background:linear-gradient(120deg,#fff,#f5f9ff)}
.empty-icon{flex:0 0 46px;width:46px;height:46px;display:grid;place-items:center;border-radius:14px;background:#e8f2ff;color:#2875d2;font-size:24px}
.empty-list-card h3{margin:0 0 4px;color:#1b3556;font-size:15px}
.empty-list-card p{margin:0;color:#71839b;font-size:12px;line-height:1.6}
.empty-list-card .empty-action{margin-left:auto;white-space:nowrap}
.empty-action a{display:inline-flex;padding:9px 12px;border-radius:8px;background:#1769dc;color:white;text-decoration:none;font-size:12px;font-weight:750}
.dashboard-rail .note{box-shadow:none}
.dashboard-rail .row{align-items:flex-end}
.dashboard-rail .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
.dashboard-rail .stat b{font-size:21px}
.dashboard-main-column .tablewrap{border:1px solid #dce6f3;border-radius:13px;background:#fff;box-shadow:var(--shadow)}
.dashboard-main-column .filters button{border-radius:8px}
.dashboard-main-column .match{box-shadow:0 5px 16px rgba(19,52,91,.045)}
.dashboard-main-column .match[open]{box-shadow:0 9px 22px rgba(19,52,91,.08)}
.dashboard-main-column .czbox{background:#f7faff}
.dashboard-main-column .det{margin-top:12px}
/* Shared visual language for every workspace tab */
.app-main main h2{scroll-margin-top:90px}
.app-main main .card{transition:border-color .16s,box-shadow .16s}
.app-main main .card:hover{border-color:#c9d9ed}
.app-main main details.card>summary{padding:2px 0}
.app-main main details.card[open]>summary{margin-bottom:12px}
.app-main main form.row{gap:12px}
.app-main main input[type=checkbox]{accent-color:#1769dc}
.app-main main textarea{line-height:1.55}
.app-main main .verdict{border:1px solid #f1d7da}
.app-main main .verdict.yes{border-color:#ccebdc}
.app-main main .profbar .prof{box-shadow:0 5px 15px rgba(19,52,91,.04)}
.app-main main .profbar .prof.on{border-color:#a9c9f2;background:linear-gradient(120deg,#edf5ff,#fff)}
.app-main main .box{box-shadow:0 3px 12px rgba(19,52,91,.035)}
.app-main main table tr:hover td{background-color:#f8fbff}
.app-main main table tr.best:hover td{background-color:#eaf3ff}
@media(max-width:1250px){
 .dashboard-layout{grid-template-columns:minmax(0,1fr) 310px}
 .opportunity-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
 .opportunity-card:nth-child(3){display:none}
}
@media(max-width:900px){
 .dashboard-layout{grid-template-columns:1fr}
 .dashboard-layout>.dashboard-rail,.dashboard-layout>.dashboard-main-column{grid-column:1;grid-row:auto}
 .dashboard-kpis .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
 .dashboard-layout>.dashboard-rail{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
 .dashboard-layout>.dashboard-rail>.stats{grid-column:1/-1}
 .dashboard-layout>.dashboard-rail>h2{grid-column:1/-1}
}
@media(max-width:720px){
 .dashboard-kpis{margin-bottom:12px}
 .dashboard-kpis .stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
 .dashboard-layout{gap:12px}
 .dashboard-layout>.dashboard-rail{display:block}
 .opportunity-grid{grid-template-columns:1fr}
 .opportunity-card:nth-child(3){display:block}
 .welcome-panel{min-height:0;padding:21px 18px;border-radius:15px}
 .welcome-visual{display:none}
 .welcome-copy h2{font-size:22px}
 .welcome-copy p{font-size:12px}
 .welcome-actions{align-items:stretch}
 .welcome-actions form,.welcome-actions form button,.primary-link{width:100%;justify-content:center}
 .secondary-link{text-align:center}
 .empty-list-card{align-items:flex-start;padding:15px}
 .empty-list-card .empty-action{margin-left:0}
 .empty-list-card{flex-wrap:wrap}
 .section-heading h2{font-size:17px}
 .opportunity-metrics b{font-size:16px}
}


.no-value-panel{display:flex;align-items:center;gap:15px;margin:2px 0 20px;padding:18px 20px;border:1px solid #d9e6f4;border-radius:14px;background:linear-gradient(110deg,#fff,#f5f9ff);box-shadow:0 7px 20px rgba(19,52,91,.045)}
.no-value-icon{flex:0 0 42px;width:42px;height:42px;display:grid;place-items:center;border-radius:13px;background:#eaf2ff;color:#2875d2;font-size:25px}
.no-value-panel h2{margin:4px 0;color:#173352;font-size:16px}
.no-value-panel p{margin:0;color:#71839b;font-size:12px;line-height:1.55}
.no-value-panel>a{margin-left:auto;white-space:nowrap;color:#1769dc;font-size:12px;font-weight:800;text-decoration:none}
@media(max-width:720px){.no-value-panel{align-items:flex-start;flex-wrap:wrap;padding:15px}.no-value-panel>a{margin-left:57px}}


/* Profil 4, zahlavi stranek, pristupnost */
:focus-visible{outline:3px solid #1769dc;outline-offset:2px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.page-head{margin:0 0 18px}.page-head h2{margin:3px 0 4px;font-size:22px}.page-head p{margin:0;color:#6c7f98;font-size:13px;max-width:900px;line-height:1.55}
.p4stats{grid-template-columns:repeat(4,minmax(0,1fr))}
.quota{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,260px),1fr));gap:6px 18px}.quota>b{grid-column:1/-1}.qrow{min-width:0}.qrow span{display:block;font-size:12px;color:var(--muted)}.qrow b{font-size:18px;margin-right:8px}.qrow small{color:var(--muted)}
.meter{display:block;height:6px;border-radius:9px;background:#e6eef8;margin-top:7px;overflow:hidden}.meter>i{display:block;height:100%;background:linear-gradient(90deg,#1975e8,#0eaa78)}
.p4actions{display:flex;flex-wrap:wrap;gap:8px}.p4actions form{margin:0}
.p4verdict{border-radius:14px;padding:16px 18px;margin-bottom:13px;border:1px solid #f3c9cf;background:#fff4f5}.p4verdict b{display:block;font-size:22px;color:#b83242}.p4verdict p{margin:4px 0 0;color:#5f6f86;font-size:13px}
.p4verdict.yes{background:#e8f8f0;border-color:#b9e8d2}.p4verdict.yes b{color:#08794f}.p4verdict.watch{background:#fff8e8;border-color:#f1dfb9}.p4verdict.watch b{color:#94600a}
.p4cats{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,520px),1fr));gap:0 13px}.p4catnote{display:flex;flex-wrap:wrap;gap:6px 14px;margin:-4px 0 14px;font-size:12px;color:#6c7f98}
.p4tip{background:#fff;border:1px solid #dce6f2;border-radius:14px;padding:14px 16px;margin-bottom:13px;box-shadow:var(--shadow)}.p4tip.ok{border-color:#9fdcc0}
.p4tip-head{display:flex;justify-content:space-between;gap:12px}.p4odds{text-align:right}.p4odds b{display:block;font-size:24px;color:#1769dc}.p4odds small{color:#6c7f98}
.p4metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin:10px 0}.p4metrics>div{background:#f4f8fd;border-radius:10px;padding:8px 10px;min-width:0}.p4metrics span,.p4metrics small{display:block;font-size:11px;color:#6c7f98}.p4metrics b{font-size:16px}
.p4models{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}.p4models span{background:#eef4fc;border-radius:7px;padding:3px 8px;font-size:12px}
.p4risk{margin-top:6px;color:#94600a;background:#fff8e8;border-radius:8px;padding:6px 9px;font-size:12px}
.p4nowrap td.num{white-space:nowrap}.p4list{margin:6px 0;padding-left:18px;font-size:13px;line-height:1.55}.p4pre{white-space:pre-wrap;font:12px/1.45 ui-monospace,monospace;overflow-x:auto;margin:8px 0 0}
@media(max-width:1100px){.p4metrics{grid-template-columns:repeat(3,minmax(0,1fr))}}
.p4three{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:8px 0}.p4three>div{background:#f4f8fd;border-radius:10px;padding:8px 10px;min-width:0}.p4three span,.p4three small{display:block;font-size:11px;color:#6c7f98}.p4three b{font-size:16px}
.p4kv{font-size:13px;margin:4px 0;overflow-wrap:anywhere}.p4mcards{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px;margin:8px 0}.p4mcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px;min-width:0}
.p4mhead{display:flex;justify-content:space-between;gap:8px;align-items:baseline}.p4mhead small{color:var(--muted)}.p4opp{display:grid;gap:8px}.p4oppc{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:8px 12px}
.p4oppc summary{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;cursor:pointer;list-style:none}.p4rank{display:inline-flex;width:40px;height:40px;border-radius:50%;background:var(--accbg);color:var(--acc);font-weight:800;align-items:center;justify-content:center}
.p4svg{width:100%;max-width:420px;height:auto;display:block;margin:6px 0}
@media(max-width:640px){.p4three{grid-template-columns:repeat(2,minmax(0,1fr))}.p4oppc summary{grid-template-columns:auto 1fr}.p4oppc summary .badge{grid-column:1/-1;justify-self:start}}

@media(max-width:720px){.p4stats{grid-template-columns:repeat(2,minmax(0,1fr))}.p4metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.p4actions form{flex:1 1 calc(50% - 8px)}.p4actions button{width:100%}.page-head h2{font-size:19px}}
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

{% macro mbadge(r) %}{% if r.cz_ok %}<span class="badge tipb">VSADIT</span>{% elif r.cz_suspect %}<span class="badge warn" title="čekaný zisk nad 15 % – zkontroluj, že kurz patří k tomuto trhu">ověř kurz</span>{% elif r.status == 'tip' %}<span class="badge tipb">TIP</span>{% elif r.status == 'value' %}<span class="badge">VÝHODNÉ</span>{% elif r.cz %}<span class="badge bad">nízký kurz</span>{% endif %}{% endmacro %}

{% macro mfilter(cats) %}<div class="mfilt" role="group" aria-label="Filtr trhů">{% for k, nm in market_cats %}{% set n = cats|length if k == 'all' else cats|select('equalto', k)|list|length %}<button type="button" class="chip{{ ' on' if k == 'all' }}" data-cat="{{ k }}" aria-pressed="{{ 'true' if k == 'all' else 'false' }}" {{ 'disabled' if not n }}>{{ nm }} <small>{{ n }}</small></button>{% endfor %}</div>{% endmacro %}

{% macro mcard(r, v, rec=False) %}{% set priced = r.status != 'model' %}<details class="mcard{{ ' rec' if rec }}" data-cat="{{ r.market|mcat }}">
<summary>{% if rec %}<span class="eyebrow">Doporučený trh</span>{% endif %}<div class="mc-head"><b>{{ r.label }}</b>{{ mbadge(r) }}</div>
<div class="mc-grid"><div><span>Šance</span><b>{{ '%.0f'|format(r.p_final * 100) }} %</b></div>
<div><span>Sázet od</span>{% if not priced %}<b>{{ '%.2f'|format(r.min_odds) }}</b>{% else %}<b class="muted">–</b><small>kurz je v nabídce</small>{% endif %}</div>
<div><span>Kurz</span>{% if r.cz %}<b>{{ '%.2f'|format(r.cz[0]) }}</b><small>{{ r.cz[1] }}</small>{% elif priced %}<b>{{ '%.2f'|format(r.best_odds) }}</b><small>{{ r.best_book }}</small>{% else %}<b class="muted">–</b>{% endif %}</div>
<div><span>Stav kurzu</span>{% if r.cz %}<b class="pos">česká kanc.</b>{% elif priced %}<b class="pos">v nabídce</b><small>zahraničí</small>{% else %}<b class="warnt">zatím není</b><small>jen cílový kurz</small>{% endif %}</div></div>
<span class="mc-more">Detail</span></summary>
<div class="mc-det"><div class="mc-sub">Modely</div><div class="mc-models">
<span><b>Model bota</b> {{ '%.1f'|format(r.p_model * 100) }} %</span>{% if r.p_second is not none %}<span><b>2. model</b> {{ '%.1f'|format(r.p_second * 100) }} %</span>{% endif %}
{% if priced %}<span><b>Trh bez marže</b> {{ '%.1f'|format(r.p_market * 100) }} %</span>{% endif %}<span><b>Výsledná</b> {{ '%.1f'|format(r.p_final * 100) }} %</span></div>
<div class="mc-grid"><div><span>Férový kurz</span><b>{{ '%.2f'|format(r.fair_odds) }}</b></div>
<div><span>Čekaný zisk</span>{% if r.cz_ev is not none %}<b class="{{ 'pos' if r.cz_ev > 0 else 'neg' }}">{{ '%+.1f %%'|format(r.cz_ev * 100) }}</b><small>s českým kurzem</small>{% elif priced %}<b class="{{ 'pos' if r.ev_pct > 0 else 'neg' }}">{{ '%+.1f %%'|format(r.ev_pct) }}</b>{% else %}<b class="muted">–</b>{% endif %}</div>
<div><span>Min. kurz</span><b>{{ '%.2f'|format(r.min_odds) if not priced else '–' }}</b></div></div>
{% if r.offers %}<div class="mc-sub">Kurzy kanceláří</div><ul class="mc-books">{% for b, o in r.offers %}<li><span>{{ b }}</span><b>{{ '%.2f'|format(o) }}</b></li>{% endfor %}</ul>{% elif priced %}<div class="muted">Nejlepší kurz: {{ '%.2f'|format(r.best_odds) }} ({{ r.best_book }})</div>{% endif %}
{% set gap = r.p_second is not none and (r.p_second - r.p_model)|abs >= second_gap %}
{% if gap or r.cz_suspect or not priced %}<div class="p4risk">Rizika: {% if gap %}modely se rozcházejí ({{ '%.0f'|format(r.p_model * 100) }} % vs {{ '%.0f'|format(r.p_second * 100) }} %); {% endif %}{% if r.cz_suspect %}čekaný zisk nad 15 % – ověř, že kurz patří k tomuto trhu; {% endif %}{% if not priced %}šance jen z modelu, trh ji nepotvrdil{% endif %}</div>{% endif %}
{% if r.note %}<div class="muted">Proč: {{ r.note }}</div>{% endif %}</div></details>{% endmacro %}

{% macro p4det(c) %}<div class="mc-sub">Modely A–D</div><div class="mc-models">{% for k, val in c.models.items() %}<span><b>{{ k }}</b> {{ val }}</span>{% endfor %}{% if c.market %}<span title="{{ c.market.src }}"><b>Trh</b> {{ c.market.p }}</span>{% endif %}</div>
<div class="mc-grid"><div><span>Férový kurz</span><b>{{ c.fair_c }}</b><small>konzerv. {{ c.fair_d }}</small></div><div><span>EV</span><b class="{{ 'pos' if c.ev_pos else 'neg' }}">{{ c.ev_c }}</b><small>konzerv. {{ c.ev_d }}</small></div>
<div><span>Min. kurz</span><b>{{ c.min_odds }}</b></div><div><span>Robustnost</span><b>{{ c.robustness }}/100</b></div></div>
{% if c.offers %}<div class="mc-sub">Kurzy kanceláří</div><ul class="mc-books">{% for o in c.offers %}<li><span>{{ o.book }}<small> · {{ o.status|lower }} · {{ o.time }}</small></span><b>{{ o.odds }}</b></li>{% endfor %}</ul>{% endif %}
{% if c.risks %}<div class="p4risk">Rizika: {{ c.risks|join('; ') }}</div>{% endif %}
{% if c.reasons %}<div class="muted">Důvody rozhodnutí: {{ c.reasons|join('; ') }}</div>{% endif %}
<div class="muted">{{ c.source }} · kurz z {{ c.quote_time }} (stáří {{ c.quote_age }})</div>{% endmacro %}

{% macro p4badge(c) %}<span class="badge {{ 'tipb' if c.approved else 'warn' if c.decision == 'WATCH' else 'bad' }}">{{ c.decision }}</span>{% endmacro %}

{% macro p4card(c, rec=False) %}<details class="mcard{{ ' rec' if rec }}" data-cat="{{ c.market_code|mcat }}">
<summary>{% if rec %}<span class="eyebrow">Doporučený trh</span>{% endif %}<div class="mc-head"><b>{{ c.label }}</b>{{ p4badge(c) }}</div>
<div class="mc-grid"><div><span>Šance (C)</span><b>{{ c.p_win }}</b></div><div><span>Min. kurz</span><b>{{ c.min_odds }}</b></div>
<div><span>Kurz</span><b>{{ c.odds }}</b><small>{{ c.book }}</small></div><div><span>Stav kurzu</span><b class="{{ 'pos' if c.quote_status == 'AKTUÁLNÍ' else 'warnt' }}">{{ c.quote_status|lower }}</b><small>EV {{ c.ev_c }}</small></div></div>
<span class="mc-more">Detail</span></summary><div class="mc-det">{{ p4det(c) }}</div></details>{% endmacro %}

{% macro p4markets(ms, rec_key='') %}{% if ms %}{% set rec = (ms|selectattr('key', 'equalto', rec_key)|first) if rec_key else none %}
<div class="mlist" data-mlist>{{ mfilter(ms|map(attribute='market_code')|map('mcat')|list) }}
<div class="mtable tablewrap"><table class="mkt p4mk"><tr><th>Trh</th><th class="num">Šance (C)</th><th class="num">Min. kurz</th><th class="num">Kurz</th><th>Stav kurzu</th><th class="num">EV</th><th>Rozhodnutí</th><th></th></tr>
{% for c in ms %}<tr data-cat="{{ c.market_code|mcat }}" class="{{ 'best' if c.key == rec_key }}"><td>{{ c.label }}</td><td class="num">{{ c.p_win }}</td><td class="num">{{ c.min_odds }}</td><td class="num"><b>{{ c.odds }}</b> <span class="muted">{{ c.book }}</span></td>
<td>{{ c.quote_status|lower }}</td><td class="num {{ 'pos' if c.ev_pos else 'neg' }}">{{ c.ev_c }}</td><td>{{ p4badge(c) }}</td><td><details class="fold"><summary>Detail</summary><div class="mc-det">{{ p4det(c) }}</div></details></td></tr>{% endfor %}</table></div>
<div class="mcards">{% if rec %}{{ p4card(rec, True) }}{% endif %}{% for c in ms if c.key != rec_key %}{{ p4card(c) }}{% endfor %}</div>
<div class="mempty muted" hidden>V této kategorii nejsou žádné trhy.</div></div>{% endif %}{% endmacro %}

{% macro page_head(eyebrow, title, desc='') %}<header class="page-head"><span class="eyebrow">{{ eyebrow }}</span><h2>{{ title }}</h2>{% if desc %}<p>{{ desc }}</p>{% endif %}</header>{% endmacro %}

{% macro score_form(back, kind) %}{% set rows = pending|selectattr(kind)|list %}
{% if rows %}<div class="card sec-acc"><b>Zadat výsledek zápasu</b>
<div class="muted">Vyber skóre po 90 minutách (domácí : hosté). Bot podle něj vyhodnotí všechny otevřené tikety i sledované výběry toho zápasu.</div>
<form class="scorebox" style="margin-top:10px" method="post" action="{{ url_for('score') }}"><input type="hidden" name="tab" value="{{ back }}">
<label>Zápas<select name="match">{% for p in rows %}<option value="{{ p.key }}">{{ p.day_label }} · {{ p.name }} ({{ p.what }})</option>{% endfor %}</select></label>
<label class="sc">Domácí<select name="home">{% for g in range(11) %}<option>{{ g }}</option>{% endfor %}</select></label><span class="colon">:</span>
<label class="sc">Hosté<select name="away">{% for g in range(11) %}<option>{{ g }}</option>{% endfor %}</select></label>
<button class="ok">Vyhodnotit</button></form></div>{% endif %}
{% endmacro %}

<div class="app-shell">
<aside class="sidebar">
  <div class="brand"><div class="brandmark">🤖</div><div class="brandname">TIPS BOT<small>SMART BETTING ANALYTICS</small></div></div>
  <div class="side-label">Pracovní prostor</div>
  {% macro cnt(key) %}{% set n = (pending|selectattr('tikety')|list|length) if key == 'tikety' else (pending|selectattr('vybery')|list|length) if key == 'sledovani' else 0 %}{% if n %}<span class="cnt" title="zápasy čekající na výsledek">{{ n }}</span>{% endif %}{% endmacro %}
  <nav class="side-nav">{% for key, label, icon, short in tabs %}<a href="{{ url_for('index', tab=key) }}" class="{{ 'on' if tab == key }}"><span class="navico">{{ icon }}</span><span>{{ label }}</span>{{ cnt(key) }}</a>{% endfor %}</nav>
  <div class="side-bottom"><div class="system-card"><b><span class="live-dot"></span>Systém aktivní</b><small>{% if auto_running %}Automat právě pracuje{% elif auto_req %}Automat čeká na spuštění{% elif auto_st %}Poslední běh {{ auto_st.local }}{% else %}Připraven k analýze{% endif %}</small></div><div class="system-card"><b>Bankroll · {{ '%.0f'|format(s.bankroll) }} {{ cur }}</b><small class="{{ 'pos' if s.profit_loss >= 0 else 'neg' }}">Výsledek {{ '%+.0f'|format(s.profit_loss) }} {{ cur }}</small></div></div>
</aside>
<div class="app-main">
<header class="topbar"><div class="wrap"><div class="top"><h1>{% for key, label, icon, short in tabs %}{% if key == tab %}{{ label }}{% endif %}{% endfor %}</h1>
{% if s.test_mode %}<span class="badge warn">TESTOVACÍ REŽIM</span>{% endif %}{% if s.paused %}<span class="badge bad">STOP-LOSS</span>{% endif %}
<div class="top-meta"><div class="top-status"><span class="status-orb">✓</span><span><strong>{% if auto_running %}Automat běží…{% elif auto_req %}Automat se spustí{% else %}Systém připraven{% endif %}</strong><span>{% if auto_running %}Hledám další příležitosti{% elif auto_st %}Poslední kontrola {{ auto_st.local }}{% else %}Čekám na analýzu{% endif %}</span></span></div></div>
<div class="chips"><span>Bankroll <b>{{ '%.0f'|format(s.bankroll) }} {{ cur }}</b></span><span>Zisk <b class="{{ 'pos' if s.profit_loss >= 0 else 'neg' }}">{{ '%+.0f'|format(s.profit_loss) }} {{ cur }}</b></span></div></div></div></header>
<nav class="bottomnav">{% for key, label, icon, short in tabs[:main_tabs] %}<a href="{{ url_for('index', tab=key) }}" class="{{ 'on' if tab == key }}"><i>{{ icon }}</i><span>{{ short }}</span>{{ cnt(key) }}</a>{% endfor %}<details class="more"><summary class="{{ 'on' if tab in tabs[main_tabs:]|map('first')|list }}"><i>☰</i><span>Více</span></summary><div class="sheet">{% for key, label, icon, short in tabs[main_tabs:] %}<a href="{{ url_for('index', tab=key) }}" class="{{ 'on' if tab == key }}">{{ icon }} {{ label }}</a>{% endfor %}</div></details></nav>
<main><div class="wrap">
<datalist id="matchlist">{% for n in match_names %}<option value="{{ n }}">{% endfor %}</datalist>
<datalist id="stakes">{% for x in (50, 100, 200, 300, 500, 1000) %}<option value="{{ x }}">{% endfor %}</datalist>
<datalist id="amounts">{% for x in (1000, 2000, 5000, 10000, 20000) %}<option value="{{ x }}">{% endfor %}</datalist>
{% if msg %}<div class="msg {{ 'err' if err else '' }}">{{ msg }}</div>{% endif %}

{% if tab == 'prehled' %}
<div class="card sec-acc autobar"><div class="arow"><b>🤖 Automat</b>
<span id="autost" class="badge {{ 'warn' if auto_req or auto_running else '' }}" data-poll="{{ '1' if auto_req or auto_running }}" data-t="{{ auto_st.t if auto_st else '' }}">{% if auto_running %}pracuje…{% elif auto_req %}spustí se do minuty{% elif auto_st %}naposledy {{ auto_st.local }}{% else %}zatím neproběhl{% endif %}</span>
{% if not (auto_req or auto_running) %}<form method="post" action="{{ url_for('auto_run') }}" style="margin:0 0 0 auto"><button class="ghost sm">Spustit teď</button></form>{% endif %}</div>
<details class="fold"><summary>Co dělá{% if auto_st %} a co udělal ({{ auto_st.notes|length }}){% endif %}</summary>
<div class="muted">Každých 6 hodin sám načte kurzy na 48 h dopředu, všechny profily vyberou sázky do Sledování, odehrané zápasy vyhodnotí; jednou týdně ověří model. Běží v Dockeru jako služba tipsbot-auto.</div>
{% if auto_st %}<div class="muted notes">{% for n in auto_st.notes %}<div>{{ n }}</div>{% endfor %}</div>{% endif %}</details></div>
{% if quota %}<div class="card quota"><b>📶 Spotřeba API</b>
{% if quota.af %}<div class="qrow"><span>API-Football dnes ({{ quota.af.day }}, UTC)</span><b>{{ quota.af.account_used }} / {{ quota.af.limit }}</b><small>zbývá {{ quota.af.remaining }} · z toho Profil 4 {{ quota.af.used }}</small><i class="meter" aria-hidden="true"><i style="width:{{ (100 * quota.af.account_used / quota.af.limit)|round|int if quota.af.limit else 0 }}%"></i></i></div>{% endif %}
{% if quota.odds.remaining is not none %}<div class="qrow"><span>Kurzové API (The Odds API, měsíčně)</span><b>zbývá {{ quota.odds.remaining }}</b><small>{% if quota.odds.used is not none %}použito {{ quota.odds.used }} z {{ quota.odds.used + quota.odds.remaining }}{% endif %}{% if quota.odds.today is not none %} · dnes {{ quota.odds.today }}{% endif %} · stav z {{ quota.odds.t[:16]|replace('T', ' ') }} UTC</small>{% if quota.odds.used is not none and quota.odds.used + quota.odds.remaining %}<i class="meter" aria-hidden="true"><i style="width:{{ (100 * quota.odds.used / (quota.odds.used + quota.odds.remaining))|round|int }}%"></i></i>{% endif %}</div>{% endif %}
</div>{% endif %}
<div class="dashboard-kpis"><div class="stats">
  <div class="stat"><span>Bankroll</span><b>{{ '%.0f'|format(s.bankroll) }} {{ cur }}</b><small>dnes vsazeno {{ '%.0f'|format(exposure) }} z {{ '%.0f'|format(exposure_cap) }}</small></div>
  <div class="stat"><span>Zisk / ztráta</span><b class="{{ 'pos' if s.profit_loss >= 0 else 'neg' }}">{{ '%+.0f'|format(s.profit_loss) }} {{ cur }}</b><small>výnos {{ '%+.2f'|format(s.yield_pct) }} % z vsazeného</small></div>
  <div class="stat"><span>Sázky</span><b>{{ '%.0f'|format(s.settled_bets) }}</b><small>úspěšnost {{ '%.1f'|format(s.win_rate_pct) }} % · {{ '%.0f'|format(s.open_bets) }} otevřených</small></div>
  <div class="stat"><span title="o kolik lepší kurz jsi vzal než ten těsně před zápasem; dlouhodobě v plusu = sázíš chytře">Lepší než závěrečný kurz</span><b>{{ '%+.2f'|format(s.avg_clv_pct) }} %</b><small>propad {{ '%.1f'|format(s.drawdown_pct) }} % (limit 15 %)</small></div>
</div>
</div>

<div class="cols dashboard-layout"><aside class="dashboard-rail">
{% if s.bankroll <= 0 %}<div class="card note"><b>Nastav bankroll</b> – bez něj bot nespočítá výši vkladu.
<form class="row" method="post" action="{{ url_for('deposit') }}">
<label>Částka ({{ cur }})<input name="amount" type="number" step="1" min="1" list="amounts" required></label>
<label>Režim<select name="test"><option value="1">Testovací (virtuální peníze)</option><option value="0">Ostrý</option></select></label>
<button>Uložit</button></form></div>{% endif %}
{% if s.paused %}<div class="card note">Propad dosáhl limitu, tipy jsou pozastavené do re-kalibrace.
<form class="row" method="post" action="{{ url_for('resume') }}"><button>Obnovit vydávání tipů</button></form></div>{% endif %}
{% set nt = pending|selectattr('tikety')|list|length %}{% set nv = pending|selectattr('vybery')|list|length %}
{% if nt or nv %}<div class="card note">Na výsledek čeká:
{% if nt %}<a href="{{ url_for('index', tab='tikety') }}">{{ nt }} zápasů s tvými tikety →</a>{% endif %}
{% if nv %}<a href="{{ url_for('index', tab='sledovani') }}">{{ nv }} zápasů se sledovanými výběry modelu →</a>{% endif %}</div>{% endif %}

<details class="card fold" id="manual-scan"><summary>Ručně načíst kurzy · co znamenají štítky</summary><div class="bar">
{% if has_key %}<form class="row" style="margin:0;flex:1" method="post" action="{{ url_for('scan') }}">
<label style="flex:0 1 260px">Soutěž<select name="league"><option value="all">Všechny soutěže</option>{% for k in scan_leagues %}<option value="{{ k }}">{{ leagues[k].name }}</option>{% endfor %}</select></label>
<label style="flex:0 0 auto;flex-direction:row;align-items:center;gap:6px"><input type="checkbox" name="force" value="1" style="width:auto"> nová data (stojí kredity)</label><button>Načíst kurzy a rozbor</button></form>
{% else %}<div class="note-inline">Živé kurzy nejsou zapnuté. Zadej API klíč v záložce <a href="{{ url_for('index', tab='nastaveni') }}">Nastavení</a>.</div>{% endif %}
</div>
<div style="margin-top:10px"><div class="legend"><span class="badge tipb">TIP</span> ověřená hodnota, vsadit &nbsp; <span class="badge tipb">CÍL</span> sázet jen při kurzu od uvedené hranice &nbsp; <span class="badge">VÝHODNÉ</span> kurz je výhodný, ale tip nevznikl &nbsp; <span class="badge bad">NEVÝHODNÉ</span> nesázet</div><div class="legend" style="margin-top:6px"><b>Šance</b> – jak pravděpodobné to podle modelu je · <b>Kurz bez marže</b> – kurz, který by odpovídal skutečné šanci (sázkovka si k němu bere marži) · <b>Sázet od</b> – nejnižší kurz, při kterém se sázka ještě vyplatí · <b>Čekaný zisk</b> – kolik průměrně vyděláš na 100 Kč vsazených, když model sedí (+10 % = +10 Kč) · <b>Lepší než závěrečný kurz</b> – jestli jsi vzal vyšší kurz, než jaký byl těsně před zápasem (dlouhodobě v plusu = sázíš chytře)</div></div></details>

<h2>Doporučený tiket <span class="muted" style="font-weight:400">(ze zahraničních kurzů)</span></h2>
{% for c in combos[:1] %}<div class="card"><div class="tip"><div>
  <div class="title">{{ c.legs|length }}× kombinace <span class="badge tipb">nejlepší</span></div>
  {% for lg in c.legs %}<div class="pick">{{ loop.index }}. <b>{{ lg.row.label }}</b> – {{ lg.match }} @ {{ '%.2f'|format(lg.row.best_odds) }} <span class="muted">({{ lg.row.best_book }}, výkop {{ lg.local }})</span></div>{% endfor %}
  <div class="kv"><span>Šance tiketu <b>{{ '%.1f'|format(c.prob * 100) }} %</b></span><span title="kurz, který odpovídá skutečné šanci – bez marže, kterou si bere sázková kancelář">Kurz bez marže <b>{{ '%.2f'|format(c.fair_odds) }}</b></span><span title="kolik průměrně vyděláš na každých 100 Kč vsazených, pokud odhad modelu sedí (+10 % = +10 Kč na 100 Kč)">Čekaný zisk <b>{{ '%+.1f'|format(c.ev_pct) }} %</b></span></div></div>
  <div class="big">{{ '%.2f'|format(c.odds) }}<small>celkový kurz · vklad {{ '%.0f'|format(c.stake) }} {{ cur }}</small></div></div>
  <div class="muted" style="margin-top:6px">Sázet jen, pokud kancelář dá celkový kurz alespoň <b>{{ '%.2f'|format(c.min_odds) }}</b>. Každá sázka je z jiného zápasu.</div></div>
{% else %}<div class="card muted">Zatím není z čeho tiket složit – potřebuju aspoň dvě příležitosti s hodnotou z různých zápasů.</div>{% endfor %}
{% if combos|length > 1 %}<details class="fold card"><summary>Další kombinace ({{ combos|length - 1 }})</summary>
<div class="tablewrap"><table class="combot"><tr><th>Sázky</th><th class="num">Kurz</th><th class="num">Šance</th><th class="num" title="kolik průměrně vyděláš na každých 100 Kč vsazených, pokud odhad modelu sedí (+10 % = +10 Kč na 100 Kč)">Čekaný zisk</th><th class="num">Min. kurz</th><th class="num">Vklad</th></tr>
{% for c in combos[1:] %}<tr><td>{% for lg in c.legs %}<div><b>{{ lg.row.label }}</b> – {{ lg.match }} @ {{ '%.2f'|format(lg.row.best_odds) }} <span class="muted">{{ lg.local }}</span></div>{% endfor %}</td>
<td class="num" data-l="kurz"><b>{{ '%.2f'|format(c.odds) }}</b></td><td class="num" data-l="šance">{{ '%.1f'|format(c.prob * 100) }} %</td><td class="num" data-l="zisk">{{ '%+.1f'|format(c.ev_pct) }} %</td><td class="num" data-l="min. kurz">{{ '%.2f'|format(c.min_odds) }}</td><td class="num" data-l="vklad">{{ '%.0f'|format(c.stake) }}</td></tr>{% endfor %}
</table></div></details>{% endif %}
</aside><div class="colmain dashboard-main-column">
{% if bets %}
<section class="opportunity-panel">
  <div class="section-heading"><div><span class="eyebrow">TIPS BOT · VALUE RADAR</span><h2>Nejlepší příležitosti</h2></div><a class="text-link" href="#match-list">Všechny zápasy →</a></div>
  <div class="opportunity-grid">
  {% for b in bets[:3] %}{% set v = b.view %}{% set r = b.row %}
    <article class="opportunity-card">
      <div class="opportunity-top"><span class="market-icon">✦</span><span class="badge tipb">Hodnota nalezena</span></div>
      <div class="opportunity-match">{{ v.name }}</div><div class="opportunity-pick">{{ r.label }}</div>
      <div class="opportunity-metrics">
        <div><small>Modelová šance</small><b>{{ '%.1f'|format(r.p_final * 100) }} %</b></div>
        <div><small>Český kurz</small><b>{{ '%.2f'|format(r.cz[0]) }}</b></div>
        <div class="metric-positive"><small>Očekávaná hodnota</small><b>{{ '%+.1f'|format(r.cz_ev * 100) }} %</b></div>
      </div>
      <div class="opportunity-foot"><span>{{ r.cz[1] }}</span><a href="#match-list">Detail zápasu ↓</a></div>
    </article>
  {% endfor %}
  </div>
</section>
{% elif views %}
<section class="no-value-panel">
  <div class="no-value-icon">⌕</div>
  <div><span class="eyebrow">ANALÝZA DOKONČENA</span><h2>Žádná sázka teď nesplňuje podmínky pro doporučení.</h2>
  <p>To je platný výsledek. Projdi níže zápasy, doplň aktuální kurzy české kanceláře a sleduj minimální kurz. Bot nebude označovat nevýhodnou sázku jako tip.</p></div>
  <a href="#match-list">Prohlédnout zápasy ↓</a>
</section>
{% else %}
<section class="welcome-panel">
  <div class="welcome-copy">
    <span class="eyebrow">TIPS BOT · READY TO ANALYZE</span>
    <h2>Začni analýzou nadcházejících zápasů.</h2>
    <p>Jakmile načteš zápasy a kurzy, zobrazí se zde nejlepší příležitosti, modelové pravděpodobnosti a přehled rizik. Pokud žádná sázka nemá hodnotu, bot to řekne otevřeně.</p>
    <div class="welcome-actions">
      {% if has_key %}<form method="post" action="{{ url_for('scan') }}"><input type="hidden" name="league" value="all"><button>Načíst kurzy a rozbor →</button></form>
      {% else %}<a class="primary-link" href="{{ url_for('index', tab='nastaveni') }}">Nastavit API a začít →</a>{% endif %}
      <a class="secondary-link" href="{{ url_for('index', tab='profil4') }}">Otevřít Profil 4 – Expert</a>
    </div>
  </div>
  <div class="welcome-visual" aria-hidden="true"><div class="orbit orbit-one"></div><div class="orbit orbit-two"></div><div class="bot-core">🤖</div><div class="orbit-dot dot-one"></div><div class="orbit-dot dot-two"></div><div class="visual-chip chip-one"><span>EV</span><b>Value radar</b></div><div class="visual-chip chip-two"><span>MODEL</span><b>A · B · C · D</b></div></div>
</section>
{% endif %}
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

<h2 id="match-list">Zápasy <span class="count-pill">{{ views|length }}</span></h2>
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
    <label>Kancelář<select name="bookmaker">{% for b in books %}<option {{ 'selected' if b == 'tipsport' }}>{{ b }}</option>{% endfor %}</select></label>
    <label>Skutečný kurz<input name="odds" type="number" step="0.01" min="1.01" required></label>
    <label>Vklad ({{ cur }})<input name="stake" type="number" step="1" value="{{ '%.0f'|format(tip.kelly_stake) }}" required></label>
    <button class="ok">Vsazeno</button><button class="ghost" formaction="{{ url_for('tip_cancel', tip_id=tip.id) }}" formnovalidate>Nevsadím</button></form></div>
  {% if partners.get(v.name) %}{% set pc = partners[v.name] %}<div class="tipbox alt"><b>Doplnit tiket o:</b>
    {% for lg in pc.legs if lg.match != v.name %}<b>{{ lg.row.label }}</b> – {{ lg.match }} @ {{ '%.2f'|format(lg.row.best_odds) }}{% endfor %}
    <span class="muted">→ celkový kurz {{ '%.2f'|format(pc.odds) }}, šance {{ '%.1f'|format(pc.prob * 100) }} %, čekaný zisk {{ '%+.1f'|format(pc.ev_pct) }} %, vklad {{ '%.0f'|format(pc.stake) }} {{ cur }}</span></div>{% endif %}{% endif %}

  <div class="mgrid{{ ' one' if v.league in leagues and leagues[v.league].sport.value == 'nhl' }}"><div class="mlist" data-mlist>{{ mfilter(v.rows|map(attribute='market')|map('mcat')|list) }}<div class="mtable tablewrap"><table class="mk"><tr><th>Trh</th><th class="num">Šance</th><th class="num" title="nezávislý druhý model (penaltyblog) jako kontrola">2. model</th><th class="num" title="kurz, který odpovídá skutečné šanci – bez marže, kterou si bere sázková kancelář">Bez marže</th><th class="num">Sázet od</th><th class="num">Zahraničí</th><th class="num">Česká kancelář</th><th class="num" title="kolik průměrně vyděláš na každých 100 Kč vsazených, pokud odhad modelu sedí (+10 % = +10 Kč na 100 Kč)">Čekaný zisk</th><th></th></tr>
  {% for r in v.main_rows %}{{ mrow(r, v) }}{% endfor %}</table></div>
  {% if v.more_rows %}<details class="fold mtable"><summary>Další trhy podle modelu ({{ v.more_rows|length }})</summary>
  <div class="tablewrap"><table class="mk"><tr><th>Trh</th><th class="num">Šance</th><th class="num" title="nezávislý druhý model (penaltyblog) jako kontrola">2. model</th><th class="num" title="kurz, který odpovídá skutečné šanci – bez marže, kterou si bere sázková kancelář">Bez marže</th><th class="num">Sázet od</th><th class="num">Zahraničí</th><th class="num">Česká kancelář</th><th class="num" title="kolik průměrně vyděláš na každých 100 Kč vsazených, pokud odhad modelu sedí (+10 % = +10 Kč na 100 Kč)">Čekaný zisk</th><th></th></tr>
  {% for r in v.more_rows %}{{ mrow(r, v) }}{% endfor %}</table></div></details>{% endif %}
  {% set recr = v.rows[v.recommended] if v.recommended is not none else (v.rows[v.target] if v.target is not none else none) %}
  <div class="mcards">{% if recr %}{{ mcard(recr, v, True) }}{% endif %}{% for r in v.main_rows if r is not sameas recr %}{{ mcard(r, v) }}{% endfor %}
  {% if v.more_rows %}<details class="fold mc-more-list"><summary>Další trhy podle modelu ({{ v.more_rows|length }})</summary>{% for r in v.more_rows if r is not sameas recr %}{{ mcard(r, v) }}{% endfor %}</details>{% endif %}</div>
  <div class="mempty muted" hidden>V této kategorii nejsou žádné trhy.</div>

  </div><div class="czbox"><b>Kurzy z české kanceláře</b>
  <form method="post" action="{{ url_for('czech_odds_match') }}" class="czf" data-prefill='{{ v.cz_books|tojson }}'>
  <input type="hidden" name="match" value="{{ v.name }}"><input type="hidden" name="league" value="{{ v.league }}">
  <div class="row" style="margin-top:4px"><label style="flex:0 1 200px">Kancelář<select name="book">{% for b in czech_books %}<option value="{{ b }}">{{ b }}</option>{% endfor %}</select></label>
  <span class="muted" style="flex:1 1 160px">Vyplň jen trhy, které chceš (trh vždy celý). Přepočet nestojí kredity.</span></div>
  <div class="czgrid">{% for m, sels, names in (nhl_form_markets if v.league == 'NHL' else form_markets) %}<fieldset><legend>{{ market_names[m] }}</legend>
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
{% else %}<section class="empty-list-card">
  <div class="empty-icon">⚽</div>
  <div><h3>Zatím tu nejsou žádné zápasy</h3><p>Načti nadcházející zápasy a jejich kurzy. Potom se zde zobrazí analýzy, modelové pravděpodobnosti, férové kurzy a nalezené příležitosti.</p></div>
  <div class="empty-action">{% if has_key %}<a href="#manual-scan" class="js-open-scan">Načíst zápasy</a>{% else %}<a href="{{ url_for('index', tab='nastaveni') }}">Nastavit API</a>{% endif %}</div>
</section>{% endfor %}
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
 var scanLink=document.querySelector('.js-open-scan');if(scanLink)scanLink.addEventListener('click',function(e){var panel=document.getElementById('manual-scan');if(panel){panel.open=true;panel.scrollIntoView({behavior:'smooth',block:'center'});}});
 document.querySelectorAll('form.czf').forEach(function(form){var data=JSON.parse(form.dataset.prefill||'{}'),sel=form.elements.book;
  var books=Object.keys(data);if(books.length)sel.value=books[0];
  function fill(){var d=data[sel.value]||{};form.querySelectorAll('.czgrid input').forEach(function(i){i.value=d[i.name]||''})}
  sel.addEventListener('change',fill);fill();});
})();
</script>

{% elif tab == 'tikety' %}
{{ page_head('Peníze', 'Moje tikety', 'Skutečně vsazené sázky: otevřené čekají na výsledek, ukončené tvoří tvoje výsledky (zisk, výnos, proti závěrečnému kurzu).') }}
<div class="cols"><aside>
{{ score_form('tikety', 'tikety') }}
<h2>Zapsat sázku</h2>
<div class="card"><div class="muted">Pro sázky mimo tipy bota. Když zvolíš trh ze seznamu, bot ji po zadání skóre vyhodnotí sám.</div>
<form method="post" action="{{ url_for('ticket_add') }}"><input type="hidden" name="tab" value="tikety"><div class="grid2" style="margin-top:12px">
  <label>Zápas (Domácí - Hosté)<input name="match_name" list="matchlist" placeholder="vyber nebo napiš" autocomplete="off" required></label>
  <label>Den zápasu<input name="match_day" type="date" value="{{ today }}" required></label>
  <label>Trh<select name="market"><option value="1X2">Výsledek (1 / X / 2)</option><option value="OU">Počet gólů (over / under)</option><option value="BTTS">Oba dají gól (ano / ne)</option><option value="DC">Dvojitá šance (1X / 12 / X2)</option><option value="DNB">Bez remízy (home / away)</option><optgroup label="NHL"><option value="REG">NHL – výsledek po 60 min (1 / X / 2)</option><option value="MONEYLINE">NHL – vítěz vč. prodloužení a nájezdů</option><option value="NHL_OU">NHL – počet gólů vč. prodloužení</option><option value="NHL_OU60">NHL – počet gólů za 60 min</option><option value="PUCKLINE">NHL – handicap ±1,5 vč. prodloužení</option></optgroup><option value="JINY">Jiný trh (vyhodnotíš ručně)</option></select></label>
  <label>Hranice gólů (jen u počtu gólů)<select name="line">{% for x in ('0.5', '1.5', '2.5', '3.5', '4.5', '5.5', '6.5', '2.0', '3.0', '4.0') %}<option {{ 'selected' if x == '2.5' }}>{{ x }}</option>{% endfor %}</select></label>
  <label>Výběr<select name="selection" data-for="market" required></select><input name="selection" class="seltext" placeholder="popis sázky" disabled hidden></label>
  <label>Kancelář<select name="bookmaker">{% for b in books %}<option {{ 'selected' if b == 'tipsport' }}>{{ b }}</option>{% endfor %}</select></label>
  <label>Kurz<input name="odds" type="number" step="0.01" min="1.01" required></label>
  <label>Vklad ({{ cur }})<input name="stake" type="number" step="1" min="1" list="stakes" required></label>
</div><div class="row"><button>Zapsat sázku</button></div></form></div>
</aside><div class="colmain">
<h2>Otevřené tikety ({{ open_tickets|length }})</h2>
<div class="card tablewrap"><table class="cardt"><tr><th>Den</th><th>Zápas</th><th>Sázka</th><th>Kancelář</th><th class="num">Kurz</th><th class="num">Vklad</th><th></th></tr>
{% for k in open_tickets %}<tr><td class="muted">{{ k.match_day.strftime('%d.%m.') }}</td><td><b>{{ k.match_name or k.match_id }}</b></td><td><b>{{ k.market|bet(k.selection, k.match_name) }}</b></td><td>{{ k.bookmaker }}</td>
<td class="num" data-l="kurz">{{ '%.2f'|format(k.odds) }}</td><td class="num" data-l="vklad">{{ '%.0f'|format(k.stake) }}</td>
<td><details class="fold"><summary>ručně</summary><form method="post" action="{{ url_for('ticket_settle', ticket_id=k.id) }}"><input type="hidden" name="tab" value="tikety">
<label>Kurz bez marže těsně před zápasem (nepovinné)<input name="closing" type="number" step="0.01"></label>
<div class="row" style="margin-top:6px"><button class="ok sm" name="result" value="won">Výhra</button><button class="bad sm" name="result" value="lost">Prohra</button><button class="ghost sm" name="result" value="void">Vráceno</button></div></form></details></td></tr>
{% else %}<tr><td colspan="7" class="muted">Žádné otevřené tikety.</td></tr>{% endfor %}</table></div>
<form method="post" action="{{ url_for('settle_all') }}"><input type="hidden" name="tab" value="tikety"><button class="ghost">Stáhnout výsledky a vyhodnotit automaticky</button></form>
<h2 id="ukoncene">Ukončené tikety ({{ settled|length }})</h2>
{% if rec.n %}<div class="card"><div class="tiles" style="margin-top:0">
<div class="tile"><span>Vyhodnoceno</span><b>{{ rec.n }} sázek</b></div>
<div class="tile {{ 'good' if rec.profit >= 0 else 'bad' }}"><span>Zisk</span><b>{{ '%+.0f'|format(rec.profit) }} {{ cur }}</b></div>
<div class="tile {{ 'good' if (rec.yield_pct or 0) >= 0 else 'bad' }}"><span>Výnos</span><b>{{ '%+.1f %%'|format(rec.yield_pct) if rec.yield_pct is not none else '–' }}</b></div></div>
<div class="muted">Výsledky ({{ rec.n }} sázek): největší propad {{ '%.0f'|format(rec.max_drawdown) }} {{ cur }} · nejdelší série proher {{ rec.longest_losing }}{% if rec.current_losing %} (teď {{ rec.current_losing }}){% endif %}</div>
{% if rec.n < 30 %}<div class="muted note-inline">Při {{ rec.n }} sázkách jsou čísla jen orientační (potřeba desítky až stovky).</div>{% endif %}</div>{% endif %}
<div class="card tablewrap"><table class="cardt"><tr><th>Den</th><th>Zápas</th><th>Výběr</th><th>Kancelář</th><th class="num">Kurz</th><th class="num">Vklad</th><th>Výsledek</th><th class="num">Zisk / ztráta</th><th class="num" title="o kolik lepší kurz jsi vzal než ten těsně před zápasem; dlouhodobě v plusu = sázíš chytře">Proti závěr. kurzu</th></tr>
{% for k in settled %}<tr class="r-{{ k.status }}"><td class="muted">{{ k.match_day.strftime('%d.%m.%Y') }}</td><td><b>{{ k.match_name or k.match_id }}</b></td>
<td>{{ k.market|bet(k.selection, k.match_name) }}</td><td>{{ k.bookmaker }}</td><td class="num" data-l="kurz">{{ '%.2f'|format(k.odds) }}</td><td class="num" data-l="vklad">{{ '%.0f'|format(k.stake) }}</td>
<td>{{ {'won': 'výhra', 'lost': 'prohra', 'void': 'vráceno', 'half_won': 'půl výhra', 'half_lost': 'půl prohra'}.get(k.status, k.status) }}</td>
<td class="num {{ 'pos' if k.profit_loss >= 0 else 'neg' }}">{{ '%+.0f'|format(k.profit_loss) }}</td>
<td class="num">{{ '%+.1f %%'|format(k.clv_pct) if k.clv_pct is not none else '–' }}</td></tr>
{% else %}<tr><td colspan="9" class="muted">Zatím žádné vyhodnocené sázky.</td></tr>{% endfor %}</table></div>
</div></div>


{% elif tab == 'sledovani' %}
{{ page_head('Bez peněz', 'Sledování modelu', 'Každý profil si sám vybírá sázky (1 jednotka, bez peněz) a bot je po zápase vyhodnotí. Tak se ukáže, který přístup by vydělával.') }}
<div style="margin:-4px 0 10px"><a class="secondary-link" href="{{ url_for('index', tab='export') }}">📤 Export výsledků (Excel, CSV, PDF)</a></div>
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
  <label>Zápas<input name="name" list="matchlist" placeholder="vyber nebo napiš" autocomplete="off" required></label>
  <label>Den zápasu<input name="day" type="date" value="{{ today }}" required></label>
  <label>Trh<select name="market">{% for m, nm in market_names.items() if not m.startswith(('OU_2.0', 'OU_4.0', 'OU60_', 'MONEYLINE')) %}<option value="{{ m }}" {{ 'selected' if m == 'OU_2.5' }}>{{ nm }}</option>{% endfor %}</select></label>
  <label>Výběr<select name="selection" data-for="market" required></select></label>
  <label>Kancelář<select name="book">{% for b in books %}<option {{ 'selected' if b == 'tipsport' }}>{{ b }}</option>{% endfor %}</select></label>
  <label>Kurz<input name="odds" type="number" step="0.01" min="1.01" required></label>
</div><div class="row"><button>Přidat do sledování</button></div></form></details>
</aside><div class="colmain">
<h2>Čeká na výsledek ({{ paper_open|length }})</h2>
<div class="card sec-warn tablewrap"><table class="cardt"><tr><th>Zápas</th><th>Výběr</th><th class="num">Kurz</th><th class="num">Model</th><th class="num" title="kolik průměrně vyděláš na každých 100 Kč vsazených, pokud odhad modelu sedí (+10 % = +10 Kč na 100 Kč)">Čekaný zisk</th><th></th></tr>
{% macro pname(p) %}{% if p.legs %}{% for x in p.legs %}<div><b>{{ x.label }}</b> – {{ x.name }} @ {{ '%.2f'|format(x.odds) }}{% if x.result %} <span class="res {{ 'won' if x.result in ('won', 'half_won') else 'lost' if x.result in ('lost', 'half_lost') else '' }}">{{ {'won': 'výhra', 'lost': 'prohra', 'void': 'vráceno', 'half_won': 'půl výhra', 'half_lost': 'půl prohra'}.get(x.result, x.result) }}</span> <span class="muted">{{ x.score }}</span>{% endif %}</div>{% endfor %}{% else %}{{ p.name }}{% endif %}{% endmacro %}
{% for p in paper_open %}<tr><td>{{ pname(p) }}<div class="muted">{{ p.day }} · {{ p.source }}</div>{% for f in p.flags %}<div><span class="badge warn">{{ f }}</span></div>{% endfor %}{% if p.detail %}<details class="fold"><summary>rozbor</summary><div class="muted">{{ p.detail }}</div></details>{% endif %}</td><td><b>{{ p.market|bet(p.selection, p.name) }}</b><div class="muted">{{ p.book }}</div></td><td class="num" data-l="kurz">{{ '%.2f'|format(p.odds) }}</td>
<td class="num" data-l="šance">{{ '%.0f %%'|format(p.p_model * 100) if p.p_model else '–' }}</td><td class="num">{% if p.ev is not none %}<span class="edge {{ 'hi' if p.ev > 0.15 }}">{{ '%+.1f %%'|format(p.ev * 100) }}</span>{% else %}–{% endif %}</td>
<td><details class="fold"><summary>ručně</summary><form method="post" action="{{ url_for('paper_result', pick_id=p.id) }}" class="row" style="margin:4px 0"><input type="hidden" name="tab" value="sledovani"><input type="hidden" name="profile" value="{{ prof.key }}">
<select name="result"><option value="won">vyhráno</option><option value="lost">prohráno</option><option value="void">vráceno</option></select><button class="sm">Zapsat</button></form>
<form method="post" action="{{ url_for('paper_delete', pick_id=p.id) }}" style="margin:0"><input type="hidden" name="tab" value="sledovani"><input type="hidden" name="profile" value="{{ prof.key }}"><button class="ghost sm">Smazat</button></form></details></td></tr>
{% else %}<tr><td colspan="6" class="muted">{% if prof.method == 'tips' %}Žádný otevřený tip. Sem se samo zapíše každé oficiální doporučení bota (TIP / VSADIT z Přehledu), i když ho nevsadíš nebo ho bot později zruší – tak je vidět, jak by tipy dopadly.
{% if last_tip %}Bot zatím vydal {{ last_tip.n }} tipů, poslední {{ last_tip.when }} ({{ last_tip.name }}). Tip vznikne jen u sázky, která i po smíchání modelu s Pinnacle splní práh čekaného zisku – to bývá vzácné, prázdná záložka tedy neznamená chybu.{% else %}Bot zatím nevydal žádný tip.{% endif %}
{% else %}Teď nic nečeká. Profil vybírá z kurzů při každém skenu automatu; když u zápasu nic nesplní jeho pravidla, nic nepřidá.{% endif %}</td></tr>{% endfor %}</table></div>
{% if prof.method == 'sim' %}<h2>Rozbory zápasů ({{ sim_rows|length }})</h2>
<div class="card"><div class="muted">Profil 3 rozebere při každém skenu každý zápas – i když vhodnou sázku nenajde. Góly se fitují z kurzů kanceláří (kurzové API + medián kanceláří z API-Football), pak simulace a 4 scénáře. Rozbor odehraného zápasu zmizí po 2 dnech.</div>
{% for a in sim_rows %}<details class="fold"><summary><b>{{ a.name }}</b> <span class="muted">{{ a.kickoff[:16]|replace('T', ' ') }} UTC · {{ leagues[a.league].name if a.league in leagues else a.league }}</span><br>
{% if a.verdict.startswith('sázka') %}<span class="badge tipb">{{ a.verdict }}</span>{% else %}<span class="badge">{{ a.verdict }}</span>{% endif %}</summary>
<div class="muted">{% if a.lam is defined %}Trh čeká {{ '%.2f'|format(a.lam) }} : {{ '%.2f'|format(a.mu) }} gólu (z {{ a.fit_markets }} trhů, odchylka fitu {{ '%.1f'|format(a.fit_err * 100) }} %).
Nejčastější výsledky simulace: {% for sc, p in a.top %}{{ sc }} {{ '%.0f'|format(p * 100) }} %{{ ', ' if not loop.last }}{% endfor %}.{% endif %}
Trhy: {{ a.markets|join(', ') }}. V pásmu kurzů posouzeno {{ a.checked }} sázek.
{% if a.near %}<br>Nejblíž byla {{ a.near.market|bet(a.near.selection, a.name) }} @ {{ '%.2f'|format(a.near.odds) }} – neprošla: {{ a.near.why|join('; ') }}.{% endif %}
<br><span class="muted">rozbor {{ a.t[:16]|replace('T', ' ') }} UTC</span></div></details>
{% else %}<div class="muted">Zatím žádný rozbor – objeví se po nejbližším skenu.</div>{% endfor %}</div>
{% endif %}
<details class="fold card" {{ 'open' if paper_done }}><summary>Vyhodnoceno ({{ paper_done|length }})</summary>
<div class="tablewrap"><table class="cardt"><tr><th>Zápas</th><th>Výběr</th><th class="num">Kurz</th><th>Skóre</th><th>Výsledek</th><th class="num">Zisk (j.)</th></tr>
{% for p in paper_done %}<tr class="r-{{ p.result }}"><td>{{ pname(p) }}<div class="muted">{{ p.day }}</div></td><td>{{ p.market|bet(p.selection, p.name) }}</td><td class="num" data-l="kurz">{{ '%.2f'|format(p.odds) }}</td><td data-l="skóre">{{ p.score or '–' }}</td><td><span class="res {{ p.result }}">{{ {'won': 'výhra', 'lost': 'prohra', 'void': 'vráceno'}.get(p.result, p.result) }}</span></td>
<td class="num {{ 'pos' if p.profit >= 0 else 'neg' }}">{{ '%+.2f'|format(p.profit) }}</td></tr>
{% else %}<tr><td colspan="6" class="muted">Zatím nic.</td></tr>{% endfor %}</table></div></details>
</div></div>

{% elif tab == 'overeni' %}
{{ page_head('Kontrola kvality', 'Ověření modelu') }}
{% if mq and mq.leagues %}<div class="card"><h3>Model vs. trh v ostrém provozu</h3>
<div class="muted">U každého zápasu se před výkopem uloží šance modelu a trhu na hlavní trh (fotbal 1X2, NHL vítěz vč. prodloužení); po zápase se porovná, kdo byl blíž výsledku (log-loss, nižší = lepší). Rozdíl proti samotnému trhu má 95% interval z bootstrapu po zápasech – záporný rozdíl = model pomáhá. Průkaznost až od {{ mq_min }} zápasů. Váhy modelu se samy nemění, je to podklad pro rozhodnutí. Přepočet jednou denně (naposledy {{ mq.t[:16]|replace('T', ' ') }} UTC).</div>
{% for lg, q in mq.leagues.items() %}<details class="fold" {{ 'open' if q.n >= mq_min }}><summary><b>{{ leagues[lg].name if lg in leagues else lg }}</b> – vyhodnoceno {{ q.n }} z {{ q.stored }} zápasů · {{ q.verdict }}</summary>
{% if q.rows %}<div class="tablewrap"><table class="mkt"><tr><th>Váha modelu</th><th class="num">Log-loss</th><th class="num">Proti trhu</th><th class="num">95% interval</th></tr>
{% for r in q.rows %}<tr class="{{ 'best' if r.current }}"><td>{{ 'jen trh' if r.w == 0 else 'jen model' if r.w == 1 else '%.0f %%'|format(r.w * 100) }}{% if r.current %} <span class="badge">nastaveno</span>{% endif %}{% if r.w == q.best %} <span class="badge tipb">nejnižší</span>{% endif %}</td>
<td class="num">{{ '%.4f'|format(r.ll) }}</td><td class="num">{{ '%+.4f'|format(r.diff) if r.w else '–' }}</td><td class="num">{{ ('%+.4f až %+.4f'|format(r.lo, r.hi)) if r.lo == r.lo else '–' }}</td></tr>{% endfor %}</table></div>{% endif %}</details>{% endfor %}</div>
{% endif %}
<div class="card sec-acc"><div class="muted">Dva pohledy: <b>skutečné výběry ve Sledování</b> (reálné kurzy, zatím malý vzorek) a <b>zpětný test</b> za poslední tři sezóny
(model se vždy učí jen z dřívějších zápasů – tisíce zápasů, ale u většiny trhů bez historických kurzů). Trh je pro sázení důvěryhodný, až když obojí ukazuje stejným směrem.</div>
<div class="arow" style="margin-top:8px">{% if bt_running %}<span class="badge warn">zpětný test běží (od {{ bt_running }}), trvá asi 30 minut</span>
{% else %}<form method="post" action="{{ url_for('backtest_start') }}" style="margin:0"><input type="hidden" name="tab" value="overeni"><button class="sm">Spustit zpětný test znovu</button></form>{% endif %}
{% if bt %}<span class="muted">poslední výpočet {{ bt.when }} · automat ho opakuje jednou týdně</span>{% endif %}</div></div>

<h2>Skutečné výběry ve Sledování podle trhů <span class="muted" style="font-weight:400">(všechny profily, 1 jednotka na výběr)</span></h2>
<div class="card tablewrap">{% if track_markets %}<table class="mkt"><tr><th>Trh</th><th class="num">Výběrů</th><th class="num">Zisk (j.)</th><th class="num">ROI</th><th>Hodnocení</th></tr>
{% for r in track_markets %}<tr><td><b>{{ r.name }}</b></td><td class="num">{{ r.n }}</td><td class="num {{ 'pos' if r.profit >= 0 else 'neg' }}">{{ '%+.2f'|format(r.profit) }}</td>
<td class="num"><span class="res {{ 'won' if r.roi >= 0 else 'lost' }}">{{ '%+.1f'|format(r.roi * 100) }} %</span></td>
<td>{% if r.n < 30 %}<span class="badge warn">málo dat ({{ r.n }} z 30+)</span>{% elif r.roi > 0 %}<span class="badge tipb">vydělává</span>{% else %}<span class="badge bad">prodělává</span>{% endif %}</td></tr>{% endfor %}</table>
<div class="muted" style="margin-top:6px">Pod 30 výběrů je výsledek hlavně náhoda: +100 % ROI z 5 výběrů jsou 3 trefy za kurz kolem 3, což se stane i bez jakékoli výhody.</div>
{% else %}<span class="muted">Zatím nic vyhodnoceno.</span>{% endif %}</div>

{% if btv %}
<h2>Přesnost modelu podle trhů – {{ btv.summary_name }}</h2>
<div class="card tablewrap"><table class="mkt"><tr><th>Trh</th><th class="num">Zápasů</th><th class="num" title="o kolik je odhad přesnější než prostá četnost výsledků (log-loss); kladné = model pomáhá">Proti odhadu bez modelu</th><th class="num" title="srovnání se zavíracím kurzem Pinnacle (nejpřesnější trh); kladné = model přesnější než trh">Proti trhu</th><th>Hodnocení</th></tr>
{% for m in btv.summary %}<tr><td><b>{{ market_names.get(m.market, m.market) }}</b></td><td class="num">{{ m.n }}</td>
<td class="num {{ 'pos' if m.skill > 0 else 'neg' }}">{{ '%+.1f'|format(m.skill * 100) }} %</td>
<td class="num">{% if m.vs_market is not none %}<span class="{{ 'pos' if m.vs_market >= 0 else 'neg' }}">{{ '%+.1f'|format(m.vs_market * 100) }} %</span>{% else %}<span class="muted">kurzy nejsou</span>{% endif %}</td>
<td><span class="badge {{ m.cls }}">{{ m.verdict }}</span></td></tr>{% endfor %}</table>
<div class="muted" style="margin-top:6px">Procenta = o kolik je odhad přesnější (log-loss). Historické kurzy jsou jen pro výsledek zápasu a počet gólů 2,5; u ostatních trhů jde srovnat jen s odhadem bez modelu. U lig se celková úroveň gólů (v testu i při sázení) sladí s linií Pinnacle, takže u počtu gólů 2,5 je model shodný s trhem; model přidává rozdělení gólů mezi týmy a na ostatní linie.
To říká, že model zápasy „čte“, ne že porazí kancelář – proto se tyto trhy ověřují ve Sledování na skutečných kurzech.</div></div>

<h2>Podrobně podle soutěží</h2>
{% for g in btv.groups %}<details class="card fold"><summary>{{ g.name }} <span class="muted" style="font-weight:400">· {{ g.markets|length }} trhů</span></summary>
<div class="muted">{{ g.note }}. Čísla = log-loss (nižší je lepší), zvýrazněný je nejlepší zdroj kromě odhadu bez modelu.</div>
<div class="tablewrap"><table class="mkt"><tr><th>Trh</th><th class="num">Zápasů</th>{% for src in g.sources %}<th class="num">{{ src_names.get(src, src) }}</th>{% endfor %}</tr>
{% for m in g.markets %}<tr><td>{{ market_names.get(m.market, m.market) }}</td><td class="num">{{ m.n }}</td>
{% for src in g.sources %}{% set r = m.src.get(src) %}<td class="num">{% if r %}<span class="{{ 'edge' if r.best }}">{{ '%.4f'|format(r.logloss) }}</span>{% else %}<span class="muted">–</span>{% endif %}</td>{% endfor %}</tr>{% endfor %}</table></div></details>{% endfor %}
{% else %}<div class="card muted">Zpětný test zatím nebyl spuštěn.</div>{% endif %}

{% elif tab == 'profil2' %}
{{ page_head('Profil 2', 'Ruční rozbor zápasu', 'Scénáře model / kompromis / trh, nejistota, simulace a sure bety z kurzů, které si opíšeš.') }}
<div class="card note">K čemu to je: <b>rozbor libovolného zápasu ze zahraničních kurzů, které bot stáhne sám</b> (nebo z kurzů, které si zapíšeš) – i zápasu, který bot v nabídce nemá
(jiná liga, pohár, speciální sázky jako „výhra hostů a méně než 3,5 gólu“). Automatické tipy Profilu 2 najdeš v záložce Sledování, tady je jen ruční nástroj.</div>
<div class="card">
<div class="muted">Postup z rozboru Wales – Portugalsko: tři scénáře gólů (A model, B kompromis model/trh, C trh), nejistota odhadu,
přesný výpočet každé sázky včetně vrácení vkladu (celé linie), simulace 1000 zápasů, kontrola korelace a sure betů.
Doporučí jen sázky s kladnou hodnotou ve scénáři A i B. <b>Profil 2 není ověřený zpětným testem</b> – doporučené sázky si dej do sledování a po čase uvidíš, jestli vydělávají.</div>
<form method="post" action="{{ url_for('profil2_run') }}"><input type="hidden" name="tab" value="profil2">
<div class="grid2" style="margin-top:12px">
  <label>Zápas (Domácí - Hosté)<input name="match" list="matchlist" placeholder="vyber nebo napiš" autocomplete="off" value="{{ p2f.match }}" required></label>
  <label>Datum zápasu<input name="day" type="date" value="{{ p2f.day or today }}"></label>
  <label>Soutěž (doplní model bota)<select name="league"><option value="">– bez modelu bota –</option>{% for k, lg in leagues.items() if lg.sport.value == 'football' %}<option value="{{ k }}" {{ 'selected' if p2f.league == k }}>{{ lg.name }}</option>{% endfor %}</select></label>
  <label>Kancelář (výchozí pro řádky bez ní)<select name="book">{% for b in czech_books %}<option {{ 'selected' if p2f.book == b }}>{{ b }}</option>{% endfor %}</select></label>
  <label>Můj odhad gólů – domácí (λ)<input name="lam_h" type="number" step="0.01" min="0.05" placeholder="0.83" value="{{ p2f.lam_h }}"></label>
  <label>Můj odhad gólů – hosté (λ)<input name="lam_a" type="number" step="0.01" min="0.05" placeholder="1.95" value="{{ p2f.lam_a }}"></label>
  {% for k, nm in (('ctx_h', 'domácích'), ('ctx_a', 'hostů')) %}{% set cur_v = '%.2f'|format((p2f[k] or '1')|replace(',', '.')|float) %}
  <label>Kontext – góly {{ nm }}<select name="{{ k }}">{% for x in range(80, 121, 5) %}{% set v = '%.2f'|format(x / 100) %}<option value="{{ v }}" {{ 'selected' if v == cur_v }}>×{{ v }}{{ ' (beze změny)' if x == 100 else ' (oslabení, absence)' if x < 100 else ' (posílení)' }}</option>{% endfor %}</select></label>{% endfor %}
</div>
<label style="margin-top:10px">Vlastní kurzy – nepovinné (řádek: kancelář | trh | kurzy; kancelář můžeš vynechat)
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
<label style="flex-direction:row;align-items:center;gap:8px;margin-top:4px"><input type="checkbox" name="use_foreign" value="1" style="width:auto" {{ 'checked' if p2f.use_foreign != '0' }}> doplnit zahraniční kurzy zápasu (medián evropských kanceláří – nic se neopisuje)</label>
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

{% elif tab == 'profil4' %}
{{ page_head('Hloubková analýza', 'Profil 4 – Hybridní expert', 'Bot projde nadcházející zápasy z API-Football, levně vyřadí nezajímavé a vybrané rozebere do hloubky: vlastní model A, trh B, kombinace C, konzervativní D, Monte Carlo, stress test a robustnost. Kategorie ani robustnost nejsou zárukou výhry.') }}
{% macro p4tip(c, title='') %}<div class="p4tip {{ 'ok' if c.approved else '' }}">
<div class="p4tip-head"><div>{% if title %}<span class="eyebrow">{{ title }}</span>{% endif %}<b>{{ c.label }}</b> <span class="muted">{{ c.key }}</span><div class="muted">{{ c.match }}</div></div>
<div class="p4odds"><b>{{ c.odds }}</b><small>{{ c.book }}</small></div></div>
<div class="p4metrics"><div><span>Šance (C)</span><b>{{ c.p_win }}</b>{% if c.p_push %}<small>vrácení {{ c.p_push }}</small>{% endif %}</div><div><span>Férový kurz</span><b>{{ c.fair_c }}</b><small>konzerv. {{ c.fair_d }}</small></div>
<div><span>EV</span><b class="{{ 'pos' if c.ev_pos else 'neg' }}">{{ c.ev_c }}</b><small>konzerv. {{ c.ev_d }}</small></div><div><span>Min. kurz</span><b>{{ c.min_odds }}</b></div>
<div><span>Robustnost</span><b>{{ c.robustness }}/100</b><i class="meter" aria-hidden="true"><i style="width:{{ c.robustness }}%"></i></i></div></div>
<div class="p4models">{% for k, v in c.models.items() %}<span title="model {{ k }}"><b>{{ k }}</b> {{ v }}</span>{% endfor %}{% if c.market %}<span title="férová pravděpodobnost trhu pro přesně tento výběr ({{ c.market.src }})"><b>Trh</b> {{ c.market.p }} · kurz vs trh {{ c.market.ev }}</span>{% endif %}</div>
<div class="arow"><span class="badge {{ 'tipb' if c.approved else 'warn' if c.decision == 'WATCH' else 'bad' }}">{{ c.decision }}</span>
<span class="muted">{{ c.source }} · {{ c.book }} · {{ c.key }} · <b>{{ c.quote_status }}</b> v čase analýzy · kurz u zdroje z {{ c.quote_time }} (stáří {{ c.quote_age }}){% if c.quote_fetched %} · staženo {{ c.quote_fetched }}{% endif %}{% if c.note %} · {{ c.note }}{% endif %}</span></div>
{% if c.reasons %}<div class="muted">Proč: {{ c.reasons|join('; ') }}</div>{% endif %}
{% if c.risks %}<div class="p4risk">Rizika: {{ c.risks|join('; ') }}</div>{% endif %}
{% if c.stress %}<details class="fold"><summary>Stress test a robustnost</summary><div class="tablewrap"><table class="mkt"><tr><th>Scénář</th><th class="num">EV</th></tr>
{% for n, ev, ok in c.stress %}<tr><td>{{ n }}</td><td class="num {{ 'pos' if ok else 'neg' }}">{{ ev }}</td></tr>{% endfor %}</table></div>
<div class="muted">Robustnost: {{ c.robust_parts|join(', ') }}</div></details>{% endif %}
{% if c.intel %}{% set i = c.intel %}<div class="p4three"><div><span>Šance na výhru</span><b>{{ c.p_win }}</b><small>odhad modelu C</small></div>
<div><span>Robustnost odhadu</span><b>{{ c.robustness }}/100</b><small>stabilita odhadu</small></div>
<div><span>Kvalita dat</span><b>{{ '%.0f'|format(i.quality) }}/100</b><small>vstupní informace</small></div>
<div><span>Opportunity Ranking</span><b>{{ '%.0f'|format(i.rank) }}/100</b><small>pořadí zajímavosti</small></div></div>
{% if i.flags %}<div class="{{ 'p4risk' if i.suspicious else 'muted' }}">Kurz: {{ i.flags|join('; ') }}</div>{% endif %}
<details class="fold"><summary>Kvalita dat, kurz a EV podle modelů</summary>
<div class="muted">Žádný z ukazatelů není zárukou zisku. Šance = odhad výhry; robustnost = jak stabilní je odhad; kvalita dat = jak dobré jsou vstupy; ranking = jen pořadí mezi příležitostmi.</div>
<div class="p4kv"><b>Kurz proti trhu</b> nejlepší {{ c.odds }} ({{ c.book }}) · medián {{ i.median }} ({{ i.books }} kanc.) · férový tržní {{ i.fair_market }} · odchylka od mediánu {{ i.dev }}</div>
<div class="p4kv"><b>EV při dostupném kurzu</b> {% for k, v in i.ev_models.items() %}{{ k }} {{ v }}{{ ' · ' if not loop.last }}{% endfor %} · konzervativní {{ c.ev_d }} · proti trhu {{ i.ev_market }} · min. kurz {{ c.min_odds }}</div>
<div class="p4kv"><b>Odkud hodnota</b> z ceny (kurz nad férovou cenou trhu) {{ i.value_price }} · z modelu (navíc nad trh) {{ i.value_model }}{% if not i.market_backed %} · <span class="neg">cena trhu neověřena – hodnota stojí jen na modelu</span>{% endif %}</div>
<div class="p4kv"><b>Citlivost EV na šanci</b> {% for d, v, ok in i.sensitivity %}<span class="{{ 'pos' if ok else 'neg' }}">{{ d }}: {{ v }}</span>{{ ' · ' if not loop.last }}{% endfor %}</div>
<div class="p4kv"><b>Kvalita dat</b> {{ i.quality_parts|join('; ') }}{% if i.uncertain %} · <i>nejistota: {{ i.uncertain|join(', ') }}</i>{% endif %}</div>
<div class="p4kv"><b>Ranking</b> {{ i.rank_parts|join('; ') }} <i>(váhy experimentální)</i></div></details>{% endif %}
</div>{% endmacro %}
{% macro p4verdict(v) %}<div class="p4verdict {{ 'yes' if v.verdict == 'APPROVED BET' else 'watch' if v.verdict == 'WATCH' else 'no' }}"><span class="eyebrow">Verdikt</span><b>{{ v.verdict or 'NO BET' }}</b><p>{{ v.explanation }}</p></div>
{% if v.best_available %}{{ p4tip(v.best_available, 'BEST AVAILABLE – není schválená sázka') }}{% endif %}
<div class="p4cats">{% for cat in v.categories if cat.tip and not cat.same_as_a %}{{ p4tip(cat.tip, cat.name) }}{% endfor %}</div>
<div class="p4catnote">{% for cat in v.categories if not cat.tip or cat.same_as_a %}<span><b>{{ cat.name }}</b>: {{ 'stejný tip jako A' if cat.same_as_a else 'nic nesplňuje podmínky' }}</span>{% endfor %}</div>{% endmacro %}

<div class="stats p4stats">
<div class="stat"><span>Klíč API-Football</span><b class="{{ 'pos' if p4.key else 'neg' }}">{{ 'nastaven' if p4.key else 'chybí' }}</b><small>{% if not p4.key %}<a href="{{ url_for('index', tab='nastaveni') }}">uložit v Nastavení</a>{% else %}API-Football v3{% endif %}</small></div>
{% if p4.budget %}<div class="stat"><span>Kvóta dnes</span><b>{{ p4.budget.used }} / {{ p4.budget.limit }}</b><small>využito požadavků</small><i class="meter" aria-hidden="true"><i style="width:{{ (100 * p4.budget.used / p4.budget.limit)|round|int if p4.budget.limit else 0 }}%"></i></i></div>
<div class="stat"><span>Zbývá</span><b>{{ p4.budget.remaining }}</b><small>jeden sken spotřebuje nejvýš {{ p4.run_limit }}</small></div>{% endif %}
<div class="stat"><span>Poslední výsledek</span><b>{{ p4.view.verdict if p4.view and p4.view.verdict else '–' }}</b><small>{{ (p4.view.mode ~ ' · ' ~ p4.view.when) if p4.view and p4.view.kind == 'scan' else (p4.view.name if p4.view else 'zatím nic nespuštěno') }}</small></div>
</div>
{% if p4.error %}<div class="card note">{{ p4.status }}</div>{% endif %}

<div class="card"><div class="section-heading"><div><span class="eyebrow">Sken</span><h2>Spustit</h2></div></div>
<div class="p4actions">
<form method="post" action="{{ url_for('profil4_scan') }}"><input type="hidden" name="mode" value="daily"><button class="ok">Dnešní sken</button></form>
<form method="post" action="{{ url_for('profil4_scan') }}"><input type="hidden" name="mode" value="upcoming"><button>Nadcházející 48 h</button></form>
<form method="post" action="{{ url_for('profil4_settle') }}"><button class="ghost">Vyhodnotit skončené</button></form>
<form method="post" action="{{ url_for('profil4_eval') }}"><button class="ghost">Úspěšnost modelů</button></form></div>
<div class="muted" style="margin-top:8px">Sken trvá desítky sekund až pár minut. <b>APPROVED BET</b> = splňuje všechna kritéria, <b>WATCH</b> = zajímavé, ale něco chybí, <b>NO BET</b> = bez hodnoty, <b>BEST AVAILABLE</b> = nejlepší nalezená možnost s důvodem, proč není schválená.</div></div>

<div class="grid2 p4forms">
<div class="card"><form method="post" action="{{ url_for('profil4_analyze') }}">
<label for="p4fid">Hloubková analýza jednoho zápasu – id z API-Football</label>
<div class="row" style="gap:8px"><input id="p4fid" name="fixture_id" inputmode="numeric" pattern="[0-9]+" required list="p4fixtures" placeholder="např. 1208021" style="max-width:220px"><button>Analyzovat</button></div>
<datalist id="p4fixtures">{% if p4.view and p4.view.kind == 'scan' %}{% for q in p4.view.quick %}<option value="{{ q.fixture_id }}">{{ q.name }}</option>{% endfor %}{% endif %}</datalist>
<div class="muted">Id najdeš ve výsledcích skenu níže, nebo ho vybereš ze seznamu.</div></form></div>
<div class="card"><form method="post" action="{{ url_for('profil4_odds') }}">
<label for="p4oid">Kurzy z Tipsportu (ručně)</label>
<input id="p4oid" name="fixture_id" inputmode="numeric" pattern="[0-9]+" required list="p4fixtures" placeholder="id zápasu" style="max-width:220px;margin-bottom:6px">
<label for="p4odds" class="sr">Řádky s kurzy</label><textarea id="p4odds" name="odds" rows="4" placeholder="1X2 | 2.10 3.40 3.60&#10;OU_2.5 | 1.90 1.95&#10;fortuna | BTTS | 1.80 2.00"></textarea>
<div class="muted">Řádek = <code>trh | kurzy</code> nebo <code>kancelář | trh | kurzy</code>. Ručně opsaný kurz platí 2 hodiny.</div>
<button style="margin-top:6px">Uložit kurzy</button></form></div></div>

{% set v = p4.detail or p4.view %}
{% if v and v.kind == 'scan' %}
<div class="section-heading"><div><span class="eyebrow">{{ v.mode }} · {{ v.when }}</span><h2>Výsledek skenu</h2></div><span class="muted">{{ v.fixtures }} zápasů · {{ v.deep }} rozebráno do hloubky · API tento sken {{ v.budget.run_used }}/{{ v.budget.run_limit }}</span></div>
{{ p4verdict(v) }}
<h2>Rozebrané zápasy ({{ v.matches|length }})</h2>
<div class="card tablewrap"><table class="cardt"><tr><th>Výkop</th><th>Zápas</th><th>Verdikt</th><th>Nejlepší možnost</th><th class="num">Data</th><th></th></tr>
{% for m in v.matches %}<tr><td class="muted">{{ m.kickoff }}</td><td><b>{{ m.name }}</b><div class="muted">{{ m.league }}</div></td>
<td><span class="badge {{ 'tipb' if m.verdict == 'APPROVED BET' else 'warn' if m.verdict == 'WATCH' else 'bad' }}">{{ m.verdict }}</span></td>
<td>{% if m.best %}{{ m.best.label }} @ <b>{{ m.best.odds }}</b> <span class="muted">EV {{ m.best.ev_c }} · robustnost {{ m.best.robustness }}</span>{% else %}<span class="muted">–</span>{% endif %}</td>
<td class="num" data-l="data">{{ m.quality }}</td>
<td>{% if m.fixture_id|string in p4.cached %}<form method="get" action="{{ url_for('index') }}" style="margin:0"><input type="hidden" name="tab" value="profil4"><input type="hidden" name="p4fid" value="{{ m.fixture_id }}"><button class="ghost sm">Detail</button></form>
{% else %}<form method="post" action="{{ url_for('profil4_analyze') }}" style="margin:0"><input type="hidden" name="fixture_id" value="{{ m.fixture_id }}"><button class="ghost sm">Detail</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan="6" class="muted">Do hloubkové analýzy nepostoupil žádný zápas.</td></tr>{% endfor %}</table></div>
<details class="fold card"><summary>Rychlý průchod – všechny zápasy ({{ v.quick|length }})</summary><div class="tablewrap"><table class="cardt"><tr><th>Výkop</th><th>Zápas</th><th>Stav</th><th>Důvod</th></tr>
{% for q in v.quick %}<tr><td class="muted">{{ q.kickoff }}</td><td><b>{{ q.name }}</b><div class="muted">{{ q.league }} · id {{ q.fixture_id }}</div></td><td>{% if q.candidate %}<span class="badge">hloubková</span>{% else %}<span class="badge bad">vyřazen</span>{% endif %}</td><td class="muted">{{ q.reason }}</td></tr>{% endfor %}</table></div></details>
{% if v.notes %}<div class="card note"><b>Poznámky</b><ul>{% for n in v.notes %}<li>{{ n }}</li>{% endfor %}</ul></div>{% endif %}
{% elif v and v.kind == 'analysis' %}
<div class="row p4back" style="gap:8px;align-items:center"><a class="secondary-link" href="{{ url_for('index', tab='profil4') }}">← Zpět na přehled{{ ' skenu' if p4.view }}</a>
<form method="post" action="{{ url_for('profil4_analyze') }}" style="margin:0"><input type="hidden" name="fixture_id" value="{{ v.fixture_id }}"><button class="ghost sm">Analyzovat znovu</button></form>
{% if v.when %}<span class="muted">rozbor {{ v.when }}</span>{% endif %}</div>
<div class="section-heading"><div><span class="eyebrow">{{ v.league }} · {{ v.kickoff }} · {{ v.status }}</span><h2>{{ v.name }}</h2></div><span class="muted">id {{ v.fixture_id }} · kvalita dat {{ v.quality }}</span></div>
{{ p4verdict(v) }}
{% for t in v.tips if t.title == 'SCHVÁLENO' %}{{ p4tip(t.tip, t.title) }}{% endfor %}
{% if v.markets %}<h3 class="p4h">Všechny trhy ({{ v.markets|length }})</h3>{{ p4markets(v.markets, v.markets[0].key if v.markets[0].approved else (v.best_available.key if v.best_available else '')) }}{% endif %}
<div class="grid2">
<div class="card"><h3>Modely (1X2: domácí / remíza / hosté)</h3>{% if v.models %}<div class="tablewrap"><table class="mkt p4nowrap"><tr><th>Model</th><th class="num">Pravděpodobnost</th><th class="num">Férové kurzy</th></tr>
{% for m in v.models %}<tr><td><b>{{ m.name }}</b><div class="muted">{{ m.note }}</div></td>{% if m.probs %}<td class="num">{{ m.probs|join(' / ') }}</td><td class="num">{{ m.fair|join(' / ') }}</td>{% else %}<td class="muted" colspan="2">nedostupný</td>{% endif %}</tr>{% endfor %}</table></div>{% else %}<div class="muted">Modely nejsou k dispozici.</div>{% endif %}</div>
<div class="card"><h3>Monte Carlo</h3>{% if v.mc %}<div class="muted">{{ v.mc.n }} simulací · očekávané skóre {{ v.mc.expected }} · 1X2 {{ v.mc.one_x_two|join(' / ') }}</div>
<div class="muted">Nejčastější výsledky: {% for s, p in v.mc.top_scores %}{{ s }} ({{ p }}){{ ', ' if not loop.last }}{% endfor %}</div>
<div class="tablewrap"><table class="mkt"><tr><th>Trh</th><th class="num">Šance</th><th class="num">95% interval</th><th class="num">Férový kurz</th><th class="num">EV</th></tr>
{% for m in v.mc.markets %}<tr><td>{{ m.key }}</td><td class="num">{{ m.prob }}</td><td class="num">{{ m.ci }}</td><td class="num">{{ m.fair }}</td><td class="num">{{ m.ev or '–' }}</td></tr>{% endfor %}</table></div>
<div class="muted">Víc simulací zmenší jen simulační chybu, ne chybu vstupního modelu.</div>{% else %}<div class="muted">Neproběhla (chybí model).</div>{% endif %}</div></div>
<div class="grid2">
<div class="card"><h3>Kvalita dat · {{ v.quality }}</h3><ul class="p4list">{% for l in v.coverage %}<li>{{ l }}</li>{% endfor %}</ul>
<div class="muted">Chybí: {{ v.missing|join(', ') or 'nic' }}</div></div>
<div class="card"><h3>Kontext zápasu</h3>{% if v.context %}<ul class="p4list">{% for l in v.context %}<li>{{ l }}</li>{% endfor %}</ul>{% else %}<div class="muted">Bez kontextových informací.</div>{% endif %}
{% if v.narrative %}<div class="muted">Shrnutí: {{ v.narrative }}</div>{% endif %}<div class="muted">Kontext je jen informace – matematické výsledky nemění.</div></div></div>
{% if v.notes %}<div class="card note"><b>Poznámky</b><ul>{% for n in v.notes %}<li>{{ n }}</li>{% endfor %}</ul></div>{% endif %}
{% elif not p4.error %}<div class="card empty-list-card"><span class="empty-icon">🧠</span><div><b>Zatím žádný sken</b><div class="muted">Spusť „Dnešní sken“ nebo „Nadcházející 48 h“. Výsledky se tu zobrazí jako karty s verdiktem a nejlepšími možnostmi.</div></div></div>{% endif %}
{% set tp = p4.tips %}{% if tp %}<h2 id="p4tips">Tipy Profilu 4</h2>
<div class="card"><div class="muted">Každý tip Profilu 4 se po zápase vyhodnotí sám (automat zhruba 2,5 h po výkopu, nebo tlačítko „Vyhodnotit skončené“). Počítá se 1 jednotka na tip, opakovaně uložený tip jednoho zápasu jen jednou. CLV = kurz proti závěrečnému férovému kurzu.</div>
<div class="stats p4stats">{% for name, s in (('Schválené sázky', tp.approved), ('Jen WATCH (nesázet)', tp.watch)) %}
<div class="stat"><span>{{ name }}</span><b>{{ s.n }}</b><small>vyhodnoceno{% if loop.first %} · čeká {{ tp.open }}{% endif %}</small></div>
<div class="stat"><span>Zisk · úspěšnost</span><b class="{{ 'pos' if s.profit >= 0 else 'neg' }}">{{ '%+.2f'|format(s.profit) }} j.</b><small>{{ ('%.0f %%'|format(s.win_rate * 100)) if s.n else '–' }} výher · yield {{ ('%+.1f %%'|format(s.yield * 100)) if s.n else '–' }}</small></div>
<div class="stat"><span>CLV</span><b>{{ ('%+.1f %%'|format(s.avg_clv * 100)) if s.clv_n else '–' }}</b><small>{{ s.clv_n }} se závěrečným kurzem · {{ s.yield_status }}</small></div>{% endfor %}</div></div>
<div class="card tablewrap"><table class="cardt"><tr><th>Výkop</th><th>Zápas</th><th>Tip</th><th>Rozhodnutí</th><th>Výsledek</th><th class="num">Zisk (j.)</th><th class="num">CLV</th></tr>
{% for r in tp.rows %}<tr class="{{ ('r-' ~ r.result) if r.result }}"><td class="muted">{{ (r.kickoff or '')[:16]|replace('T', ' ') }}</td><td><b>{{ r.match }}</b><div class="muted">{{ r.league }}</div></td>
<td>{{ r.label }} @ <b>{{ '%.2f'|format(r.odds) }}</b> <span class="muted">{{ r.bookmaker }}</span></td>
<td><span class="badge {{ 'tipb' if r.decision == 'APPROVED BET' else 'warn' }}">{{ 'SCHVÁLENO' if r.decision == 'APPROVED BET' else 'WATCH' }}</span></td>
<td data-l="výsledek">{% if r.result %}<span class="res {{ 'won' if r.result in ('won', 'half_won') else 'lost' if r.result in ('lost', 'half_lost') else '' }}">{{ {'won': 'výhra', 'lost': 'prohra', 'void': 'vráceno', 'half_won': 'půl výhra', 'half_lost': 'půl prohra'}.get(r.result, r.result) }}</span> {{ r.score or '' }}{% else %}<span class="muted">čeká</span>{% endif %}</td>
<td class="num {{ 'pos' if (r.profit or 0) >= 0 else 'neg' }}">{{ '%+.2f'|format(r.profit) if r.profit is not none else '–' }}</td>
<td class="num">{{ '%+.1f %%'|format(r.clv * 100) if r.clv is not none else '–' }}</td></tr>
{% else %}<tr><td colspan="7" class="muted">Zatím žádný schválený ani WATCH tip.</td></tr>{% endfor %}</table></div>{% endif %}
{% set ev = p4.eval %}<h2 id="p4eval">Úspěšnost modelů</h2>
{% if ev %}<div class="card"><div class="muted">Přepočet po každém vyhodnocení (automat každou hodinu, nebo tlačítko „Úspěšnost modelů“), naposledy {{ ev.t[:16]|replace('T', ' ') }} UTC. Průkaznost podle 95% intervalu z bootstrapu po zápasech; pod 30 zápasy jen orientačně.</div>
{% set c = ev.calibration %}{% if c and c.models %}<h3>Jak přesně modely odhadují šance</h3>
<div class="muted">Všechny předpovědi před výkopem ({{ c.matches }} zápasů, {{ c.predictions }} předpovědí). Nižší Brier a log loss = přesnější. „Trh“ = kurzy bez marže; model, který trh neporazí, nepřidává hodnotu.</div>
<div class="tablewrap"><table class="mkt"><tr><th>Model</th><th class="num">Zápasů</th><th class="num">Brier</th><th class="num">Log loss</th><th class="num">Odhad / skutečnost</th><th>Proti trhu</th></tr>
{% for name, m in c.models.items() %}<tr><td><b>{{ name }}</b></td><td class="num">{{ m.matches }}</td><td class="num">{{ '%.4f'|format(m.brier) }}</td><td class="num">{{ '%.4f'|format(m.log_loss) }}</td>
<td class="num">{{ '%.1f'|format(m.mean_p * 100) }} / {{ '%.1f'|format(m.rate * 100) }} %</td><td class="muted">{% if m.vs_market %}{{ '%+.4f'|format(m.vs_market.diff) }} – {{ m.vs_market.status }}{% else %}–{% endif %}</td></tr>{% endfor %}</table></div>
<div class="muted">A = vlastní model, B = trh, C = kombinace (rozhoduje o sázkách), D = konzervativní.</div>{% endif %}
{% set a = ev.all %}<h3>Tipy celkem (1 j. na tip)</h3>
<div class="muted">{{ a.matches }} zápasů, {{ a.n }} tipů · zisk {{ '%+.2f'|format(a.profit) }} j. · úspěšnost {{ ('%.0f %%'|format(a.win_rate * 100)) if a.n else '–' }} · yield {{ ('%+.1f %%'|format(a.yield * 100)) if a.n else '–' }} · CLV {{ ('%+.1f %%'|format(a.avg_clv * 100)) if a.clv_n else '–' }} ({{ a.clv_n }}) · {{ a.yield_status }}</div>
{% for g in ('rozhodnutí', 'kategorie', 'trh', 'kurzové pásmo', 'soutěž') if ev.groups[g] %}<details class="fold"><summary>Podle: {{ g }}</summary><div class="tablewrap"><table class="mkt"><tr><th>{{ g }}</th><th class="num">Tipů</th><th class="num">Zisk (j.)</th><th class="num">Yield</th><th class="num">CLV</th></tr>
{% for k, s in ev.groups[g].items() if s.n %}<tr><td>{{ k }}</td><td class="num">{{ s.n }}</td><td class="num {{ 'pos' if s.profit >= 0 else 'neg' }}">{{ '%+.2f'|format(s.profit) }}</td><td class="num">{{ '%+.1f %%'|format(s.yield * 100) }}</td><td class="num">{{ ('%+.1f %%'|format(s.avg_clv * 100)) if s.clv_n else '–' }}</td></tr>
{% else %}<tr><td colspan="5" class="muted">zatím nic vyhodnoceno</td></tr>{% endfor %}</table></div></details>{% endfor %}</div>
{% else %}<div class="card muted">Zatím nespočítáno – klikni na „Úspěšnost modelů“ nahoře, nebo počkej na automat (po vyhodnocení zápasů).</div>{% endif %}
{% set ig = p4.intel %}<h2 id="p4intel">Intelligence &amp; Calibration</h2>
<div class="card"><div class="row" style="justify-content:space-between;gap:8px;flex-wrap:wrap"><div class="muted">Měří, jak přesné jsou šance modelů A–D proti trhu, kde systém funguje a kde ne. Počítá se jen z uložených dat (bez API) – automat po každém vyhodnocení{% if ig %}, naposledy {{ ig.t[:16]|replace('T', ' ') }} UTC{% endif %}.</div>
<form method="post" action="{{ url_for('profil4_intel') }}" style="margin:0"><button class="ghost sm">Přepočítat</button></form></div>
<details class="fold"><summary>Co znamenají ukazatele</summary><div class="muted"><b>Brier</b> – jak přesně model odhaduje pravděpodobnosti (nižší = lepší). <b>Log loss</b> – jak silně jsou trestány sebejisté chybné odhady (nižší = lepší). <b>Proti trhu</b> – rozdíl log loss modelu a trhu bez marže na stejných řádcích; záporný = model lepší. <b>Kalibrace</b> – sedí odhad 60 % s tím, že to vyjde v 60 % případů? <b>CLV</b> – zda jsme získali lepší kurz než závěrečný trh. <b>Kvalita dat</b> – kvalita vstupních informací. <b>Opportunity Ranking</b> – jak zajímavá je příležitost proti ostatním (ne šance na výhru). Intervaly 95 % jsou z bootstrapu po zápasech; pod 30 zápasy se o průkaznosti nerozhoduje.</div></details></div>
{% if ig %}
{% set opp = ig.opportunities %}<h3>Opportunity Ranking – nejlepší aktuální příležitosti</h3>
{% if opp %}<div class="p4opp">{% for o in opp[:8] %}<details class="p4oppc"><summary><span class="p4rank">{{ '%.0f'|format(o.rank or 0) }}</span><span><b>{{ o.label }}</b> @ {{ '%.2f'|format(o.odds) }} <span class="muted">{{ o.book }}</span><br><small class="muted">{{ o.match }} · {{ (o.kickoff or '')[:16]|replace('T', ' ') }}</small></span>
<span class="badge {{ 'tipb' if o.decision == 'APPROVED BET' else 'warn' if 'WATCH' in (o.decision or '') else 'bad' }}">{{ o.decision }}</span></summary>
<div class="p4kv">EV {{ '%+.1f %%'|format(o.ev * 100) if o.ev is not none else '–' }} · konzervativní {{ '%+.1f %%'|format(o.ev_conservative * 100) if o.ev_conservative is not none else '–' }} · min. kurz {{ '%.2f'|format(o.min_odds) if o.min_odds else '–' }} · kvalita dat {{ '%.0f'|format(o.quality) if o.quality is not none else '–' }}/100 · robustnost {{ '%.0f'|format(o.robustness or 0) }}/100</div>
<div class="muted">{{ o.reasons|join('; ') }}</div>{% if o.flags %}<div class="p4risk">{{ o.flags|join('; ') }}</div>{% endif %}</details>{% endfor %}</div>
<div class="muted">Ranking je pořadí zajímavosti, ne pravděpodobnost výhry ani doporučení k sázce. BEST AVAILABLE a WATCH nejsou schválené sázky.</div>
{% else %}<div class="card muted">Zatím žádné nadcházející tipy s rankingem (ranking se ukládá od Fáze 3 při každé analýze).</div>{% endif %}
{% for sport, sd in ig.sports.items() %}<h3>{{ 'Fotbal' if sport == 'fotbal' else 'NHL (samostatná kalibrace)' }}</h3>
<div class="muted">Vyhodnocené předpovědi před výkopem: {{ sd.predictions }} ({{ sd.matches_any }} zápasů). Společný vzorek všech modelů A–D a trhu: <b>{{ sd.common_matches }} zápasů</b>, {{ sd.common_rows }} výběrů – jen na něm se modely porovnávají.</div>
{% if sd.models %}<div class="p4mcards">{% for k, m in sd.models.items() %}<div class="p4mcard"><div class="p4mhead"><b>{{ 'Trh (bez marže)' if k == 'trh' else 'Model ' ~ k }}</b><small>{{ m.matches }} záp.</small></div>
<div class="p4kv">Brier <b>{{ '%.4f'|format(m.brier) }}</b>{% if m.brier_ci95 %} <small>({{ '%.3f'|format(m.brier_ci95[0]) }}–{{ '%.3f'|format(m.brier_ci95[1]) }})</small>{% endif %}</div>
<div class="p4kv">Log loss <b>{{ '%.4f'|format(m.log_loss) }}</b>{% if m.log_loss_ci95 %} <small>({{ '%.3f'|format(m.log_loss_ci95[0]) }}–{{ '%.3f'|format(m.log_loss_ci95[1]) }})</small>{% endif %}</div>
<div class="p4kv">Odhad / skutečnost {{ '%.1f'|format(m.mean_p * 100) }} / {{ '%.1f'|format(m.rate * 100) }} %</div>
<div class="p4kv">Kalibrace: <b>{{ m.calibration.status }}</b></div>
{% if m.vs_market %}<div class="p4kv">Proti trhu: {{ '%+.4f'|format(m.vs_market.diff) }}{% if m.vs_market.ci95 %} <small>({{ '%+.3f'|format(m.vs_market.ci95[0]) }} až {{ '%+.3f'|format(m.vs_market.ci95[1]) }})</small>{% endif %} – <b>{{ m.vs_market.status }}</b></div>{% endif %}
<details class="fold"><summary>Kalibrační pásma</summary>{{ calib_svg(m.bins)|safe }}
{% for b in m.bins %}<div class="p4kv">{{ b.bin }}: odhad {{ '%.0f'|format(b.predicted * 100) }} %, skutečnost {{ '%.0f'|format(b.actual * 100) }} % ({{ '%.0f'|format(b.ci95[0] * 100) }}–{{ '%.0f'|format(b.ci95[1] * 100) }} %) · {{ b.matches }} záp.{% if not b.enough %} <i>málo dat</i>{% elif b.ok %} ✓{% else %} <span class="neg">mimo interval</span>{% endif %}</div>{% endfor %}</details></div>{% endfor %}</div>
{% if sd.series|length > 1 %}<details class="fold"><summary>Vývoj v čase (log loss po měsících)</summary>{{ series_svg(sd.series)|safe }}</details>{% endif %}
{% else %}<div class="card muted">Zatím žádný společný vzorek – modely a trh se srovnají, až budou vyhodnocené zápasy, kde mají předpověď všechny.</div>{% endif %}
{% for title, key in (('Výkon podle soutěží', 'competitions'), ('Výkon podle trhů', 'markets'), ('Kurzová pásma', 'bands')) if sd[key] %}<details class="fold"><summary>{{ title }}</summary><div class="p4mcards">
{% for k, g in sd[key].items() %}<div class="p4mcard"><div class="p4mhead"><b>{{ k }}</b><small>{{ g.matches }} záp. · {{ g.confidence }}</small></div>
{% if g.brier is defined %}<div class="p4kv">Model C: Brier {{ '%.4f'|format(g.brier) }} · log loss {{ '%.4f'|format(g.log_loss) }} (trh {{ '%.4f'|format(g.market_log_loss) }})</div>
<div class="p4kv">Proti trhu: {{ g.status }} · kalibrace: {{ g.calibration }} · odhad {{ '%.0f'|format(g.mean_p * 100) }} % / skutečnost {{ '%.0f'|format(g.rate * 100) }} %</div>
{% if g.stability %}<div class="p4kv">Stabilita (log loss 1. / 2. polovina): {{ '%.3f'|format(g.stability.first) }} / {{ '%.3f'|format(g.stability.second) }}</div>{% endif %}{% endif %}
{% if g.tips is defined %}<div class="p4kv">Tipy: {{ g.tips }} · zisk {{ '%+.2f'|format(g.profit) }} j. · yield {{ '%+.1f %%'|format(g.yield_ * 100) }}{% if g.clv is not none %} · CLV {{ '%+.1f %%'|format(g.clv * 100) }} ({{ g.clv_n }}){% endif %}</div>{% endif %}
</div>{% endfor %}</div></details>{% endfor %}
{% set tr = sd.tuning %}<details class="fold"><summary>Návrh ladění (jen diagnostika)</summary><div class="p4kv"><b>{{ tr.proposal }}</b> – {{ tr.status }}</div>
<div class="muted">{{ tr.recommendation }}{% if tr.weight_c is defined %} · váha C {{ '%.2f'|format(tr.weight_c) }} · log loss mimo trénink {{ '%.4f'|format(tr.before) }} → {{ '%.4f'|format(tr.after) }}{% if tr.ci95 %} (rozdíl {{ '%+.4f'|format(tr.ci95[0]) }} až {{ '%+.4f'|format(tr.ci95[1]) }}){% endif %} · trénink {{ tr.train_matches }} / test {{ tr.test_matches }} záp.{% endif %}. Rizika: {{ tr.risks }}. Změna modelu se nikdy nenasazuje automaticky.</div></details>
{% endfor %}
<details class="fold"><summary>Analýza chyb ({{ ig.errors|length }} vyhodnocených tipů)</summary><div class="muted">Jen to, co je v datech doloženo. Jednotlivá prohra sama nedokazuje chybu modelu.</div>
{% for e in ig.errors|reverse %}{% if loop.index <= 40 %}<div class="p4kv"><b>{{ e.match }}</b> – {{ e.label }} @ {{ '%.2f'|format(e.odds) }} ({{ e.decision }}): {{ e.diagnosis|join('; ') }}</div>{% endif %}{% endfor %}</details>
<details class="fold"><summary>Kontrola úniku informací (data leakage)</summary>
{% for ch in ig.leakage.checks %}<div class="p4kv">{{ '✓' if ch.ok else '✗' }} {{ ch.name }}: <b>{{ ch.count }}</b> <span class="muted">– {{ ch.note }}</span></div>{% endfor %}
<div class="muted">Z kalibrace vyřazeno: {{ ig.dropped['po_výkopu'] }} předpovědí po výkopu, {{ ig.dropped['opakované'] }} opakovaných skenů.</div></details>
{% else %}<div class="card muted">Zatím nespočítáno – klikni na „Přepočítat“ nebo počkej na automat.</div>{% endif %}
{% if p4.report %}<details class="fold card"><summary>Poslední textový výstup (ke zkopírování)</summary><pre class="p4pre">{{ p4.report }}</pre></details>{% endif %}
<h2 id="p4nhl">NHL</h2>
{% set n = p4.nhl %}{% if n %}<div class="card"><div class="muted">Automat rozebere každých 6 hodin (a ráno) všechny zápasy NHL na 48 h ze zahraničních kurzů, které právě stáhl – kredity navíc nespotřebuje. Modely: A model bota (xG, brankáři, únava), B trh (Pinnacle / medián bez marže), C smíšený, D konzervativní. Stejná kontrolní vrstva a pravidla jako u fotbalu; vyhodnocení z NHL API.</div>
<div class="muted" style="margin-top:6px">Rozbor {{ n.when }}{% if n.odds_time %} · kurzy staženy {{ n.odds_time }}{% endif %} · zápasů {{ n.matches|length }} · schválených sázek {{ n.approved }}</div>
{% if n.notes %}<ul class="help">{% for x in n.notes %}<li>{{ x }}</li>{% endfor %}</ul>{% endif %}</div>
{% for m in n.matches %}<details class="card fold" {{ 'open' if m.approved }}><summary><b>{{ m.kickoff }}</b> {{ m.name }} – <span class="badge {{ 'tipb' if m.verdict == 'APPROVED BET' else 'warn' if m.verdict == 'WATCH' else 'bad' }}">{{ m.verdict }}</span></summary>
<div class="muted">{{ m.explanation }}</div><div class="muted">Brankáři: {{ m.goalies }} · kvalita dat {{ m.quality }} %</div>
{% for c in m.approved %}{{ p4tip(c, 'APPROVED BET') }}{% else %}{% if m.best %}{{ p4tip(m.best, 'BEST AVAILABLE – není schválená sázka') }}{% endif %}{% endfor %}
{% if m.markets %}<details class="fold"><summary>Všechny trhy ({{ m.markets|length }})</summary>{{ p4markets(m.markets, m.best.key if m.best else '') }}</details>{% endif %}</details>
{% endfor %}{% if p4.nhl_report %}<details class="fold card"><summary>Textový výstup NHL</summary><pre class="p4pre">{{ p4.nhl_report }}</pre></details>{% endif %}
{% else %}<div class="card muted">NHL zatím nebylo rozebráno – udělá to automat při dalším průchodu.</div>{% endif %}

{% elif tab == 'kalkulacka' %}
{{ page_head('Nástroje', 'Value kalkulačka', 'Spočítá čekaný zisk, férový kurz a doporučený vklad podle Kellyho pro jakoukoli sázku. Nic se neukládá.') }}
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
<form method="get" action="{{ url_for('calc') }}"><div class="grid2" style="margin-top:12px">
  <label>Ostré kurzy<input name="sharp" placeholder="2.10 3.40 3.60" value="{{ c.sharp if c else '' }}" required></label>
  <label>Můj výběr (pořadí)<select name="pick">{% for i, nm in ((1, '1. kurz (domácí / víc)'), (2, '2. kurz (remíza / míň)'), (3, '3. kurz (hosté)')) %}<option value="{{ i }}" {{ 'selected' if c and c.pick|string == i|string }}>{{ nm }}</option>{% endfor %}</select></label>
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
{{ page_head('Správa', 'Nastavení', 'Data pro model, klíče k API, české kurzy a bankroll.') }}
<div class="setgrid">
<section><h2>Data pro model</h2>
<div class="card">Stáhne nejnovější výsledky lig, pohárů, reprezentací a NHL (zdarma, trvá zhruba minutu). Dělej to jednou denně před načtením kurzů.
<form class="row" method="post" action="{{ url_for('refresh_data') }}"><button>Aktualizovat data</button></form></div></section>
<section><h2>NHL – brankáři</h2>
<div class="card">Model počítá s brankáři podle podílu odchytaných zápasů v sezóně (potvrzeného brankáře NHL API předem nedává).
Když se jednička zraní nebo přijde o místo, nastav tu, kdo teď chytá – model ho použije u všech dalších zápasů týmu, dokud nastavení nezrušíš.
{% if goalies.overrides %}<div class="tablewrap" style="margin-top:8px"><table><tr><th>Tým</th><th>Brankář</th><th></th></tr>
{% for t, nm in goalies.overrides|dictsort %}<tr><td>{{ t }}</td><td><b>{{ nm }}</b></td><td><form method="post" action="{{ url_for('nhl_goalie') }}" style="margin:0"><input type="hidden" name="tab" value="nastaveni"><input type="hidden" name="team" value="{{ t }}"><button class="ghost sm">Zrušit</button></form></td></tr>{% endfor %}</table></div>{% endif %}
{% if goalies.roster %}<form method="post" action="{{ url_for('nhl_goalie') }}" class="goalief"><input type="hidden" name="tab" value="nastaveni">
<div class="grid2" style="margin-top:8px"><label>Tým<select name="team" required>{% for t in goalies.roster|sort %}<option>{{ t }}</option>{% endfor %}</select></label>
<label>Brankář<select name="name" data-roster='{{ goalies.roster|tojson }}' required></select></label></div>
<div class="row"><button>Uložit brankáře</button></div></form>
{% else %}<div class="muted" style="margin-top:8px">Data brankářů zatím nejsou stažená (Aktualizovat data).</div>{% endif %}</div></section>
<section><h2>České kurzy (nepovinné)</h2>
<div class="card">Bot vystačí se zahraničními kurzy (medián evropských kanceláří). České kurzy zadávej, jen když chceš počítat přesně s kurzem své kanceláře. Kurzy se nejpohodlněji zadávají přímo u zápasu: <b>Přehled → rozbal zápas → Zadat kurzy z české kanceláře</b>. Platí 48 hodin.
{% if czech_entries %}<details class="fold" style="margin-top:8px"><summary>Uložené kurzy ({{ czech_entries|length }})</summary><div class="tablewrap"><table><tr><th>Kancelář</th><th>Zápas</th><th>Trh</th><th>Kurzy</th></tr>
{% for e in czech_entries %}<tr><td>{{ e.book }}</td><td>{{ e.home }} - {{ e.away }}</td><td>{{ market_names.get(e.market, e.market) }}</td><td>{{ e.odds|join(' · ') }}</td></tr>{% endfor %}</table></div>
<form method="post" action="{{ url_for('clear_czech_odds') }}"><button class="bad sm">Smazat všechny</button></form></details>{% endif %}
<details class="fold" style="margin-top:6px"><summary>Hromadné vložení textem</summary>
<div class="muted">Jeden řádek na trh: <code>kancelář | Domácí - Hosté | trh | kurzy</code>, např. <code>tipsport | Německo - Itálie | 1X2 | 2.10 3.30 3.40</code>.</div>
<form method="post" action="{{ url_for('set_czech_odds') }}"><textarea name="text" rows="5" required style="margin-top:8px"></textarea><div class="row" style="margin-top:8px"><button>Uložit kurzy</button></div></form></details></div></section>
<section><h2>Bankroll</h2>
<div class="card">Aktuální stav: <b>{{ '%.0f'|format(s.bankroll) }} {{ cur }}</b> · režim: <b>{{ 'testovací' if s.test_mode else 'ostrý' }}</b>
<form class="row" method="post" action="{{ url_for('deposit') }}"><input type="hidden" name="tab" value="nastaveni">
<label>Přidat vklad ({{ cur }})<input name="amount" type="number" step="1" min="1" list="amounts" required></label>
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
{% elif tab == 'export' %}
{{ page_head('Výsledky', 'Export výsledků', 'Aktuální výsledky všech profilů přímo z databáze – Excel, CSV nebo PDF.') }}
<div class="row" style="gap:10px;flex-wrap:wrap;margin-bottom:12px">
<a class="primary-link" href="{{ url_for('export_file', fmt='xlsx') }}">📊 Excel (XLSX)</a>
<a class="secondary-link" href="{{ url_for('export_file', fmt='csv') }}">CSV všech tipů</a>
<a class="secondary-link" href="{{ url_for('export_file', fmt='pdf') }}">PDF report</a></div>
{% if ex.error %}<div class="card neg">Export se nepodařil: {{ ex.error }}</div>{% else %}
<div class="card"><div class="muted">{% for l in ex.meta %}{{ l }}<br>{% endfor %}</div>
<div class="muted" style="margin-top:6px">Excel: souhrn s grafy, všechny tipy, kombi tikety a nohy, kurzová pásma, soutěže a sporty, vývoj bankrollu, metodika. Soubor se vytvoří při každém kliknutí znovu z aktuálních dat.</div></div>
<h2>Souhrn</h2>
{% if ex.rows %}<div class="tablewrap"><table class="mkt"><tr><th>Profil</th><th class="num">Tipů</th><th class="num">Vyhodn.</th><th class="num">Čeká</th><th class="num">V / P / vr. / nepl.</th><th class="num">Úspěšnost</th><th class="num">Zisk (j.)</th><th class="num">ROI na 1 j.</th><th class="num">Prům. kurz</th><th class="num">Brier</th><th class="num">CLV</th></tr>
{% for r in ex.rows %}<tr><td><b>{{ r[0] }}</b><div class="muted">{{ r[1] }}</div></td><td class="num">{{ r[2] }}</td><td class="num">{{ r[3] }}</td><td class="num">{{ r[4] }}</td><td class="num">{{ r[5] }} / {{ r[6] }} / {{ r[7] }} / {{ r[8] }}</td>
<td class="num">{{ ('%.0f %%'|format(r[9] * 100)) if r[9] is not none else '–' }}</td><td class="num {{ 'pos' if r[10] >= 0 else 'neg' }}">{{ '%+.2f'|format(r[10]) }}</td>
<td class="num">{{ ('%+.1f %%'|format(r[11] * 100)) if r[11] is not none else '–' }}</td><td class="num">{{ ('%.2f'|format(r[12])) if r[12] is not none else '–' }}</td>
<td class="num">{{ ('%.3f'|format(r[13])) if r[13] is not none else '–' }}</td><td class="num">{{ ('%+.1f %% (%d)'|format(r[15] * 100, r[16])) if r[15] is not none else '–' }}</td></tr>{% endfor %}</table></div>
{% else %}<div class="card muted">Zatím žádné tipy – export i tak funguje (prázdné listy s metodikou).</div>{% endif %}
{% set m = ex.money %}<div class="card"><b>Moje tikety – skutečné peníze:</b> vsazeno {{ '%.0f'|format(m.stake_sum or 0) }} {{ cur }} · výsledek {{ '%+.0f'|format(m.profit_money or 0) }} {{ cur }} · skutečné ROI {{ ('%+.1f %%'|format(m.roi_real * 100)) if m.get('roi_real') is not none else '–' }} · průměrné ROI na tiket {{ ('%+.1f %%'|format(m.roi_avg * 100)) if m.get('roi_avg') is not none else '–' }}</div>
<div class="muted">ROI na 1 j. = zisk / vsazené vyhodnocené tipy; čekající, vrácené a neplatné se nepočítají. Profily se nesčítají – stejný zápas může být ve více profilech.</div>
<h2>Archiv rozborů</h2><div class="card muted">Bot si trvale ukládá každou změnu rozborů a skenů (nic se nemaže):
{% set an = {'scan': 'skeny Přehledu', 'p3': 'rozbory Profilu 3', 'p4_scan': 'skeny Profilu 4', 'p4_nhl': 'skeny Profilu 4 NHL', 'p4_detail': 'rozbory zápasů Profilu 4', 'p4_intel': 'přepočty Intelligence & Calibration'} %}
{% for k, n in ex.archive|dictsort %}{{ an.get(k, k) }} {{ n }}{{ ' · ' if not loop.last }}{% else %}zatím nic (plní se od teď při každém skenu){% endfor %}</div>
{% endif %}
{% endif %}
</div></main>
</div></div>
<script>
(function(){
 // vyber podle trhu: nabidne jen platne moznosti (u "jiny trh" se pise text)
 function sels(m){
  if(m=='1X2'||m=='REG')return [['1','1 – domácí'],['X','X – remíza'],['2','2 – hosté']];
  if(m=='PUCKLINE')return [['home-1.5','domácí −1,5'],['away+1.5','hosté +1,5'],['home+1.5','domácí +1,5'],['away-1.5','hosté −1,5']];
  if(m=='DC')return [['1X','1X'],['12','12'],['X2','X2']];
  if(m=='DNB'||m=='MONEYLINE')return [['home','domácí'],['away','hosté']];
  if(m=='BTTS')return [['yes','ano'],['no','ne']];
  if(m=='JINY')return null;
  if(m=='TTH_0.5'||m=='TTA_0.5')return [['over','ano, dají gól'],['under','ne']];
  return [['over','víc (over)'],['under','míň (under)']];}
 document.querySelectorAll('select[data-for]').forEach(function(sel){
  var form=sel.form,src=form.elements[sel.dataset.for],txt=sel.parentNode.querySelector('.seltext');
  var line=form.elements.line;
  function upd(){var o=sels(src.value);sel.innerHTML='';
   if(line)line.closest('label').style.display=/^(NHL_)?OU(60)?$/.test(src.value)?'':'none';
   sel.hidden=sel.disabled=!o;if(txt){txt.hidden=txt.disabled=!!o;txt.required=!o}
   (o||[]).forEach(function(x){var e=document.createElement('option');e.value=x[0];e.textContent=x[1];sel.appendChild(e)});}
  src.addEventListener('change',upd);upd();});
 // trhy: filtr kategorii (karty na mobilu i radky tabulky); prazdna kategorie = zprava, nic se nevymysli
 document.querySelectorAll('[data-mlist]').forEach(function(box){
  var bar=box.querySelector('.mfilt');if(!bar)return;
  bar.addEventListener('click',function(e){var b=e.target.closest('button[data-cat]');if(!b||b.disabled)return;var k=b.dataset.cat,shown=0;
   bar.querySelectorAll('button').forEach(function(x){var on=x===b;x.classList.toggle('on',on);x.setAttribute('aria-pressed',on?'true':'false')});
   box.querySelectorAll('.mcard[data-cat],tr[data-cat]').forEach(function(c){var ok=k=='all'||c.dataset.cat==k;c.hidden=!ok;if(ok&&c.matches('.mcard'))shown++});
   box.querySelectorAll('.mc-more-list').forEach(function(d){var any=d.querySelector('.mcard:not([hidden])');d.hidden=!any;if(k!='all'&&any)d.open=true});
   var em=box.querySelector('.mempty');if(em)em.hidden=shown>0;});});
 // brankari NHL: nabidka podle vybraneho tymu
 document.querySelectorAll('select[data-roster]').forEach(function(sel){
  var r=JSON.parse(sel.dataset.roster),team=sel.form.elements.team;
  function upd(){sel.innerHTML='';(r[team.value]||[]).forEach(function(x){var e=document.createElement('option');e.value=x[0];e.textContent=x[0]+' ('+x[1]+' záp.)';sel.appendChild(e)});}
  team.addEventListener('change',upd);upd();});
 // automat: po kliknuti na "Spustit ted" sleduje stav a po dobehnuti obnovi stranku
 var a=document.getElementById('autost');
 if(a&&a.dataset.poll){var t0=a.dataset.t;setInterval(function(){
  fetch('{{ url_for('auto_state') }}').then(function(r){return r.json()}).then(function(d){
   a.textContent=d.running?'pracuje… (načítá kurzy a vybírá sázky, trvá pár minut)':d.requested?'spustí se do minuty':'hotovo';
   if(!d.running&&!d.requested&&d.t!==t0)location.reload();})},5000);}
 // menu "Vice" se zavre klepnutim mimo nej
 document.addEventListener('click',function(e){var d=document.querySelector('.bottomnav details[open]');if(d&&!d.contains(e.target))d.open=false});
 // zprava po akci po chvili zmizi
 var m=document.querySelector('.msg:not(.err)');if(m)setTimeout(function(){m.style.display='none'},8000);
})();
</script></body></html>"""


MARKET_NAMES = {"1X2": "Výsledek", "DC": "Dvojitá šance", "DNB": "Sázka bez remízy", "OU_1.5": "Góly 1.5",
                "OU_2.5": "Góly 2.5", "OU_3.5": "Góly 3.5", "OU_4.5": "Góly 4.5",
                "OU_2.0": "Góly 2.0 (při 2 vráceno)", "OU_4.0": "Góly 4.0 (při 4 vráceno)", "TTH_1.5": "Góly domácích 1.5",
                "TTA_1.5": "Góly hostů 1.5", "BTTS": "Oba týmy dají gól", "TTH_0.5": "Domácí dají gól",
                "TTA_0.5": "Hosté dají gól", "MONEYLINE": "Vítěz zápasu (vč. prodloužení)",
                "OU_5.5": "Góly 5.5", "OU_6.5": "Góly 6.5", "ROHY_8.5": "Rohy 8.5", "ROHY_9.5": "Rohy 9.5",
                "ROHY_10.5": "Rohy 10.5", "KARTY_3.5": "Karty 3.5", "KARTY_4.5": "Karty 4.5", "KARTY_5.5": "Karty 5.5",
                "OU_7.5": "Góly 7.5", "TTH_2.5": "Góly domácích 2.5", "TTA_2.5": "Góly hostů 2.5",
                **{f"OU60_{x}": f"Góly za 60 min {x}" for x in ("3.5", "4.5", "5.5", "6.5", "7.5", "8.5")}}


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
    if market in ("REG", "MONEYLINE", "NHL_OU", "NHL_OU60", "PUCKLINE"):  # NHL: vyhodnoceni z NHL API
        from .nhl_settle import grade as nhl_grade

        if market in ("NHL_OU", "NHL_OU60"):
            if not line.strip():
                raise ValueError("U počtu gólů vyplň hranici (např. 5.5).")
            market = f"{'OU60' if market == 'NHL_OU60' else 'OU'}_{float(line.replace(',', '.')):g}"
        elif market == "PUCKLINE":
            market = "PUCKLINE_1.5" if selection in ("home-1.5", "away+1.5") else "PUCKLINE_1.5R"
        selection = selection.strip()
        if nhl_grade(market, selection, {"reg": (0, 0), "final": (1, 0)}) is None:
            raise ValueError(f"Výběru '{selection}' u trhu {market} nerozumím.")
        return market, selection
    if market == "OU":
        if not line.strip():
            raise ValueError("U počtu gólů vyplň hranici (např. 2.5).")
        market = f"OU_{float(line.replace(',', '.')):g}"
    market, selection = normalize_bet(market, selection)
    if grade(market, selection, 0, 0) is None:
        raise ValueError(f"Výběru '{selection}' u trhu {market} nerozumím (např. 1/X/2, over/under, ano/ne, 1X).")
    return market, selection


SRC_NAMES = {backtest.NO_MODEL: "Bez modelu", backtest.OURS: "Náš model", backtest.SECOND: "2. model",
             backtest.BOTH: "Průměr modelů", backtest.MARKET: "Trh (Pinnacle)", backtest.BLEND: "Směs model+trh"}
_SRC_ORDER = tuple(SRC_NAMES)
LEAGUE_NOTE = "zavírací kurzy Pinnacle"  # skupiny lig ve zpetnem testu (backtest.run_all)
MARKET_ORDER = ("1X2", "OU_1.5", "OU_2.5", "OU_3.5", "OU_4.5", "BTTS", "TTH_0.5", "TTH_1.5", "TTA_0.5", "TTA_1.5")


def _verdict(skill: float, vs_market: float | None) -> tuple[str, str]:
    if vs_market is not None:
        if vs_market > 0.003:
            return "přesnější než trh", "tipb"
        return ("na úrovni trhu", "") if vs_market > -0.005 else ("horší než trh", "bad")
    if skill > 0.01:
        return "model pomáhá", "tipb"
    return ("slabý přínos", "warn") if skill > 0.002 else ("bez přínosu", "bad")


def _bt_view(bt: dict | None) -> dict | None:
    """Zpetny test pro panel: po soutezich tabulka trh x zdroj a souhrn lig (vazeny poctem zapasu)."""
    if not bt or not bt.get("groups"):
        return None
    groups = []
    for g in bt["groups"]:
        markets: dict[str, dict] = {}
        for r in g["rows"]:
            markets.setdefault(r["market"], {"market": r["market"], "n": r["n"], "src": {}})["src"][r["source"]] = r
        sources = [x for x in _SRC_ORDER if any(x in m["src"] for m in markets.values())]
        groups.append({"name": g["name"], "note": g["note"], "markets": list(markets.values()), "sources": sources})
    pool = [g for g in bt["groups"] if g["note"] == LEAGUE_NOTE] or bt["groups"][:1]
    acc: dict[str, dict] = {}
    for g in pool:
        for r in g["rows"]:
            n, tot = acc.setdefault(r["market"], {}).get(r["source"], (0, 0.0))
            acc[r["market"]][r["source"]] = (n + r["n"], tot + r["n"] * r["logloss"])
    summary = []
    for market, a in acc.items():
        ll = {src: tot / n for src, (n, tot) in a.items() if n}
        if backtest.OURS not in ll or backtest.NO_MODEL not in ll:
            continue
        skill = 1.0 - ll[backtest.OURS] / ll[backtest.NO_MODEL]
        vs = (1.0 - ll[backtest.OURS] / ll[backtest.MARKET]) if backtest.MARKET in ll else None
        verdict, cls = _verdict(skill, vs)
        summary.append({"market": market, "n": a[backtest.OURS][0], "skill": skill, "vs_market": vs, "verdict": verdict, "cls": cls})
    summary.sort(key=lambda m: (MARKET_ORDER.index(m["market"]) if m["market"] in MARKET_ORDER else 99, m["market"]))
    name = f"{len(pool)} lig dohromady" if pool[0]["note"] == LEAGUE_NOTE else pool[0]["name"]
    return {"summary": summary, "summary_name": name, "groups": groups}


def _track_markets(picks) -> list[dict]:
    """Vyhodnocene vybery ze Sledovani (vsechny profily) podle trhu, nejvic vyberu nahore."""
    by = paper.stats([p for p in picks if p.result is not None]).get("by_market", {})
    rows = [{"market": m, "name": MARKET_NAMES.get(m, m), "n": n, "profit": pr, "roi": roi} for m, (n, pr, roi) in by.items()]
    return sorted(rows, key=lambda r: -r["n"])


MARKET_CATS = (("all", "Vše"), ("goly", "Góly"), ("1x2", "1X2"), ("tymy", "Týmy"), ("karty", "Karty"),
               ("rohy", "Rohy"), ("handicap", "Handicap"))


def market_cat(market: str | None) -> str:
    """Kategorie trhu pro mobilni filtr (jen zarazeni existujicich trhu, nic se nevytvari)."""
    m = (market or "").upper()
    if m.startswith("ROHY_"):
        return "rohy"
    if m.startswith("KARTY_"):
        return "karty"
    if m.startswith(("AH_", "PUCKLINE")):
        return "handicap"
    if m.startswith(("TTH_", "TTA_", "H_", "A_")):
        return "tymy"
    if m in ("1X2", "DC", "DNB", "MONEYLINE", "REG", "OT"):
        return "1x2"
    if m.startswith(("OU", "BTTS", "G")):
        return "goly"
    return "jine"


def _bet(market: str | None, selection: str | None, match: str | None = "") -> str:
    """Citelny nazev sazky ('Under 2.5 gólů' misto 'OU_2.5 under'); neznamy trh zustane, jak je."""
    if not market:
        return selection or ""
    home, _, away = (match or "").partition(" - ")
    try:
        text = analysis.label(market, selection or "", home or "domácí", away or "hosté")
    except (KeyError, IndexError, ValueError):
        return f"{market} {selection or ''}".strip()
    return text if text != f"{market} {selection}" else f"{market} {selection or ''}".strip()


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


def _calib_svg(bins: list) -> str:
    """Kalibracni krivka (odhad vs. skutecnost) jako male SVG – funguje i na mobilu (sirka 100 %)."""
    if not bins:
        return ""
    w = h = 200
    pts = " ".join(f"{20 + b['predicted'] * 170:.1f},{180 - b['actual'] * 170:.1f}" for b in bins)
    dots = "".join(f'<circle cx="{20 + b["predicted"] * 170:.1f}" cy="{180 - b["actual"] * 170:.1f}" r="{3 + min(5, b["matches"] / 20):.1f}" '
                   f'fill="{"#16794a" if b.get("ok") else "#b42318" if b.get("ok") is False else "#94a3b8"}"/>' for b in bins)
    return (f'<svg class="p4svg" viewBox="0 0 {w} {h}" role="img" aria-label="kalibrační křivka">'
            f'<rect x="20" y="10" width="170" height="170" fill="none" stroke="#dfe4ea"/>'
            f'<line x1="20" y1="180" x2="190" y2="10" stroke="#94a3b8" stroke-dasharray="4 3"/>'
            f'<polyline points="{pts}" fill="none" stroke="#1d5fd1" stroke-width="2"/>{dots}'
            f'<text x="105" y="198" font-size="9" text-anchor="middle" fill="#64707d">odhad</text>'
            f'<text x="8" y="95" font-size="9" fill="#64707d" transform="rotate(-90 8 95)" text-anchor="middle">skutečnost</text></svg>')


def _series_svg(series: list) -> str:
    """Log loss po mesicich pro A–D a trh (spolecny vzorek)."""
    keys = [("A", "#7c3aed"), ("B", "#0891b2"), ("C", "#1d5fd1"), ("D", "#a16207"), ("trh", "#111827")]
    vals = [s[k]["log_loss"] for s in series for k, _ in keys if s.get(k) and s[k]["log_loss"] == s[k]["log_loss"]]
    if len(series) < 2 or not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(series)

    def xy(i, v):
        return f"{30 + i * 250 / (n - 1):.1f},{150 - (v - lo) / span * 130:.1f}"

    lines = "".join(f'<polyline points="{" ".join(xy(i, s[k]["log_loss"]) for i, s in enumerate(series))}" fill="none" '
                    f'stroke="{c}" stroke-width="{2.5 if k == "trh" else 1.6}"/><text x="{285}" y="{150 - (series[-1][k]["log_loss"] - lo) / span * 130:.1f}" '
                    f'font-size="9" fill="{c}">{k}</text>' for k, c in keys)
    labels = "".join(f'<text x="{30 + i * 250 / (n - 1):.1f}" y="168" font-size="8" text-anchor="middle" fill="#64707d">{s["month"]}</text>'
                     for i, s in enumerate(series))
    return (f'<svg class="p4svg" viewBox="0 0 310 175" role="img" aria-label="vývoj log loss">'
            f'<text x="2" y="20" font-size="8" fill="#64707d">{hi:.3f}</text><text x="2" y="150" font-size="8" fill="#64707d">{lo:.3f}</text>'
            f'{lines}{labels}</svg>')


def create_app(database_url: str | None = None) -> Flask:
    app = Flask(__name__)
    factory = make_session_factory(database_url)
    app.add_template_filter(_bet, "bet")
    app.add_template_filter(market_cat, "mcat")
    app.jinja_env.globals["market_cats"] = MARKET_CATS
    app.jinja_env.globals["calib_svg"] = _calib_svg
    app.jinja_env.globals["series_svg"] = _series_svg
    app.secret_key = os.urandom(24)  # jen pro zpravu po presmerovani; po restartu se nic dulezitého neztratí
    valid_tabs = {k for k, *_ in TABS}

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
        if p2form is None:
            p2form = json.loads(led._get(P2_FORM_KEY, "") or "{}")
        pending = _pending(open_tickets, [p for p in picks if p.result is None])
        auto_st = auto.status(led)
        if auto_st:
            auto_st["local"] = _local(datetime.fromisoformat(auto_st["t"]))
        return render_template_string(
            PAGE,
            s=type("S", (), s),
            cur=SETTINGS.currency,
            tabs=TABS,
            main_tabs=MAIN_TABS,
            match_names=sorted({v.name for v in views} | {p["name"] for p in pending}),
            tab=tab if tab in valid_tabs else "prehled",
            pending=pending,
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
            btv=_bt_view(json.loads(led._get(backtest.STATE_KEY, "") or "null")),
            src_names=SRC_NAMES,
            track_markets=_track_markets(all_picks),
            bt_running=led._get(backtest.RUNNING_KEY, ""),
            czech_books=sorted(czechodds._BOOKS, key=lambda b: (b != "tipsport", b)),
            form_markets=czechodds.FORM_MARKETS,
            nhl_form_markets=czechodds.NHL_FORM_MARKETS,
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
            auto_st=auto_st,
            auto_req=bool(led._get(auto.REQUEST_KEY, "")),
            auto_running=led._get(auto.RUNNING_KEY, ""),
            has_football_key=bool(led.api_football_key()),
            scan_leagues=list(SETTINGS.odds_api.sport_keys),
            c=type("C", (), calc) if calc else None,
            p2=p2,
            p2text=[profile2.format_text(a, SETTINGS.currency) for a in p2],
            p2f=type("F", (), {k: "" for k in ("match", "day", "league", "book", "lam_h", "lam_a", "ctx_h", "ctx_a", "odds", "use_czech", "use_foreign")} | (p2form or {})),
            p2st=type("P", (), paper.stats([p for p in all_picks if p.profile == "profil2"])),
            p4=_p4_view(led) if tab == "profil4" else {},
            ex=_export_view(session) if tab == "export" else None,
            quota=_quota_view(led) if tab == "prehled" else None,
            goalies=_goalie_view(led) if tab == "nastaveni" else None,
            mq=json.loads(led._get("model_quality", "") or "null") if tab == "overeni" else None,
            last_tip=_last_tip(session) if tab == "sledovani" and prof.method == "tips" else None,
            sim_rows=sorted(paper.load_analyses(led).values(), key=lambda a: a["kickoff"])
            if tab == "sledovani" and prof.method == "sim" else [],
            mq_min=30,
        )

    def _goalie_view(led) -> dict:
        from . import nhl_data

        try:
            roster = nhl_data.goalie_roster()
        except Exception:  # noqa: BLE001 - poskozeny soubor nesmi shodit panel
            roster = {}
        return {"roster": roster, "overrides": led.goalie_overrides()}

    def _quota_view(led) -> dict:
        """Spotreba API: API-Football (denni kvota, hlavicky z odpovedi plati pro cely ucet) a kurzove API (mesicni kredity)."""
        out = {"odds": {"t": "", "remaining": None, "used": None, "today": None} | led.odds_api_quota(), "af": None}
        if out["odds"]["remaining"] is None:  # stav pred prvnim stazenim po aktualizaci: posledni kredity z cache kurzu
            caches = [c for c in (led.odds_cache(lg) for lg in SETTINGS.odds_api.sport_keys) if c and c[2] is not None]
            if caches:
                t, _, left = max(caches, key=lambda c: c[0])
                out["odds"].update(t=t.isoformat(), remaining=left)
        try:
            from .profil4.cli import build

            store, prov = build(database_url)
            try:
                b = prov.client.budget.status()
                b["account_used"] = max(b["used"], b["limit"] - b["remaining"]) if b.get("limit") else b["used"]
                out["af"] = b
            finally:
                store.close()
        except Exception:  # noqa: BLE001 - metrika nesmi shodit panel
            pass
        return out

    def _last_tip(session) -> dict | None:
        from sqlalchemy import func, select

        n = session.scalar(select(func.count()).select_from(Tip)) or 0
        t = session.scalars(select(Tip).order_by(Tip.timestamp.desc()).limit(1)).first()
        return {"n": n, "when": f"{t.timestamp:%d.%m.%Y}", "name": t.match_name} if t else None

    def _p4_view(led) -> dict:
        try:
            from .profil4.cli import build

            store, prov = build(database_url)
            try:
                b = prov.client.budget.status()
                key = prov.client.key() or led.api_football_key()
                st = (f"Klíč API-Football: {'nastaven' if key else 'CHYBÍ – ulož ho v Nastavení'} · kvóta dnes "
                      f"{b['used']}/{b['limit']}, zbývá {b['remaining']}")
                from .profil4.config import P4

                from .profil4 import nhl as p4nhl

                details = _p4_details(store)
                last = json.loads(store.get("web_last_view", "") or "null")
                fid = request.args.get("p4fid", "")
                return {"status": st, "key": bool(key), "budget": b, "run_limit": P4.budget.max_per_run, "report": store.get("web_last_report", ""),
                        "view": last if last and last.get("kind") == "scan" else None,
                        "detail": details.get(fid) or _p4_archived_detail(store, fid), "cached": set(details), "tips": _p4_tips(store),
                        "eval": json.loads(store.get("web_p4_eval", "") or "null"),
                        "intel": json.loads(store.get("web_p4_intel", "") or "null"),
                        "nhl": json.loads(store.get(p4nhl.VIEW_KEY, "") or "null"), "nhl_report": store.get(p4nhl.REPORT_KEY, "")}
            finally:
                store.close()
        except Exception as e:  # noqa: BLE001 - panel nesmi spadnout kvuli Profilu 4
            return {"status": f"Profil 4 není dostupný: {e}", "error": True, "report": "", "view": None, "detail": None,
                    "cached": set(), "tips": None, "nhl": None, "nhl_report": ""}

    def _export_data(session):
        from . import export
        from .profil4.config import P4

        return export.collect(session, P4.db_path, utcnow())

    def _export_view(session) -> dict:
        from . import export

        try:
            d = _export_data(session)
            arch = Ledger(session).archive_counts()
            from .profil4.config import P4

            arch |= export.p4_archive_counts(P4.db_path)
            return {"meta": export.meta_lines(d), "head": export.SUMMARY_HEAD, "rows": export._summary_rows(d),
                    "money": export.stats([r for r in d.records if r.group == export.TICKETS]), "archive": arch}
        except Exception as e:  # noqa: BLE001 - panel nesmi spadnout kvuli exportu
            return {"error": str(e)}

    @app.get("/export/<fmt>")
    def export_file(fmt: str):
        from . import export

        kinds = {"xlsx": (export.to_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                 "csv": (export.to_csv, "text/csv"), "pdf": (export.to_pdf, "application/pdf")}
        if fmt not in kinds:
            return redirect(url_for("index", tab="export"))
        with factory() as session:  # jen cteni: nic se necommituje
            d = _export_data(session)
            fn, mime = kinds[fmt]
            body = fn(d)
            session.rollback()
        import io as _io

        name = f"tipsbot-vysledky-{d.created:%Y-%m-%d-%H%M}.{fmt}"
        return send_file(_io.BytesIO(body), mimetype=mime, as_attachment=True, download_name=name)

    P4_DETAILS = "web_p4_details"  # rozbory jednotlivych zapasu {fixture_id: karty}; prehled skenu zustava zvlast

    def _p4_details(store) -> dict:
        details = json.loads(store.get(P4_DETAILS, "") or "{}")
        last = json.loads(store.get("web_last_view", "") or "null")
        if last and last.get("kind") == "analysis":  # starsi stav: rozbor prepsal prehled
            details.setdefault(str(last.get("fixture_id")), last)
        return details

    def _p4_archived_detail(store, fid: str) -> dict | None:
        """Starsi rozbor, ktery uz vypadl z poslednich 30 (archiv se nemaze)."""
        got = store.archived("p4_detail", fid) if fid else []
        return json.loads(got[-1][2]) if got else None

    def _p4_save_detail(store, data: dict) -> None:
        details = _p4_details(store)
        details.pop(str(data.get("fixture_id")), None)
        details[str(data.get("fixture_id"))] = data
        store.archive("p4_detail", str(data.get("fixture_id")), json.dumps(data, ensure_ascii=False, default=str))
        store.set(P4_DETAILS, json.dumps(dict(list(details.items())[-30:]), ensure_ascii=False, default=str))

    def _p4_tips(store) -> dict:
        """Tipy Profilu 4 s vysledky (vyhodnocuje automat i tlacitko Vyhodnotit skoncene)."""
        from .profil4 import history

        rows = [dict(r) for r in store.query("SELECT * FROM tips ORDER BY kickoff DESC, id DESC")]
        for r in rows:  # starsi zaznamy maji popisek "±-0,0"
            r["label"] = (r["label"] or r["market"]).replace("±-0", "±0")
        bets, _ = history.unique_bets(rows)
        approved = [r for r in bets if r["decision"] == history.APPROVED]
        watch = [r for r in bets if r["decision"] != history.APPROVED and "WATCH" in (r["decision"] or "")]
        bets.sort(key=lambda r: (r["kickoff"] or "", r["id"]), reverse=True)
        return {"approved": history.bet_stats(approved), "watch": history.bet_stats(watch),
                "open": sum(r["result"] is None for r in approved),
                "rows": [r for r in bets if r["decision"] == history.APPROVED or "WATCH" in (r["decision"] or "")][:60]}

    def _p4_run(fn, view=None, save=None):
        """Spusti akci Profilu 4; `fn` vraci vysledek, `view` z nej udela karty pro panel (text zustava jako zaloha).
        `save(store, karty)` ulozi karty jinam nez jako prehled skenu (rozbor jednoho zapasu)."""
        from .profil4.cli import build
        from .profil4.store import utcnow as p4_now  # Profil 4 pocita s casem vcetne pasma (UTC)

        store, prov = build(database_url)
        try:
            now = p4_now()
            res = fn(store, prov, now)
            text = res if view is None else view[0](res, now)
            store.set("web_last_report", text)
            if view is not None:
                data = view[1](res, now)
                if save is not None:
                    save(store, data)
                else:
                    store.set("web_last_view", json.dumps(data, ensure_ascii=False, default=str))
            return text
        finally:
            store.close()

    @app.post("/profil4/scan")
    def profil4_scan():
        from .profil4 import hunter, report, view
        from .profil4.config import P4

        mode = "daily" if request.form.get("mode") == "daily" else "upcoming"
        try:
            _p4_run(lambda st, pv, now: hunter.run_scan(pv, mode, now, P4),
                    (lambda r, now: report.format_scan(r), lambda r, now: view.scan(r)))
            flash("Sken hotový – výsledek je dole.")
        except Exception as e:  # noqa: BLE001
            flash(f"Chyba skenu: {e}")
        return redirect(url_for("index", tab="profil4"))

    @app.post("/profil4/analyze")
    def profil4_analyze():
        from .profil4 import hunter, report, view
        from .profil4.config import P4

        try:
            fid = int(request.form["fixture_id"])
            _p4_run(lambda st, pv, now: hunter.analyze_fixture(pv, fid, now, P4),
                    (report.format_analysis, view.analysis), save=_p4_save_detail)
            flash(f"Analýza zápasu {fid} hotová.")
        except Exception as e:  # noqa: BLE001
            flash(f"Chyba analýzy: {e}")
            return redirect(url_for("index", tab="profil4"))
        return redirect(url_for("index", tab="profil4", p4fid=fid))

    @app.post("/profil4/odds")
    def profil4_odds():
        from .profil4 import odds as p4odds

        try:
            fid = int(request.form["fixture_id"])

            def fn(st, pv, now):
                got = pv.fixtures_by_ids([fid])
                fx = got.data[0] if got.data else None
                q, errors = p4odds.parse_manual(request.form.get("odds", ""), fid, fx.name if fx else str(fid), now,
                                                fx.kickoff if fx else None)
                n = p4odds.save(st, q)
                return f"Uloženo {n} kurzů k zápasu {fid}." + (f" Přeskočeno: {'; '.join(errors)}" if errors else "")

            flash(_p4_run(fn))
        except Exception as e:  # noqa: BLE001
            flash(f"Chyba: {e}")
        return redirect(url_for("index", tab="profil4"))

    @app.post("/profil4/settle")
    def profil4_settle():
        from .profil4 import history

        try:
            from .profil4 import closing, nhl as p4nhl

            def fn(st, pv, now):
                out = history.settle(st, pv, now) + p4nhl.settle(st, now)
                closing.store_evaluation(st, now)
                return "\n".join(out)

            msgs = _p4_run(fn)
            flash(f"Vyhodnoceno: {len(msgs.splitlines())} – výsledky jsou v přehledu Tipy Profilu 4 níže."
                  if msgs else "Nic nového k vyhodnocení – všechny skončené zápasy už výsledek mají (Tipy Profilu 4 níže).")
        except Exception as e:  # noqa: BLE001
            flash(f"Chyba: {e}")
        return redirect(url_for("index", tab="profil4", _anchor="p4tips"))

    @app.post("/profil4/intel")
    def profil4_intel():
        from .profil4 import intelligence
        from .profil4.cli import build
        from .profil4.store import utcnow as p4_now

        try:
            store, _ = build(database_url)
            try:
                intelligence.store_intel(store, p4_now())
            finally:
                store.close()
            flash("Intelligence & Calibration přepočítáno z uložených dat (bez API).")
        except Exception as e:  # noqa: BLE001
            flash(f"Chyba: {e}")
        return redirect(url_for("index", tab="profil4", _anchor="p4intel"))

    @app.post("/profil4/evaluate")
    def profil4_eval():
        from .profil4 import history, report

        from .profil4 import closing

        try:
            _p4_run(lambda st, pv, now: report.format_evaluation(closing.store_evaluation(st, now)))
            flash("Úspěšnost modelů přepočítána – je níže v sekci Úspěšnost modelů.")
        except Exception as e:  # noqa: BLE001
            flash(f"Chyba: {e}")
        return redirect(url_for("index", tab="profil4", _anchor="p4eval"))

    def done(msg: str, tab: str):
        """Po uspesnem odeslani formulare presmeruje na bezne zobrazeni (obnoveni stranky nic znovu neodesle)."""
        flash(msg)
        return redirect(url_for("index", tab=tab, profile=request.form.get("profile") or None))

    def action(fn, ok_msg: str, tab: str = "prehled"):
        tab = request.form.get("tab", tab)
        with factory() as session:
            try:
                result = fn(Ledger(session), session)
                session.commit()
            except (ValueError, KeyError) as e:
                session.rollback()  # chyba: formular zustane vyplneny
                return render(session, f"Chyba: {e}", err=True, tab=tab), 400
        return done(result or ok_msg, tab)

    @app.get("/")
    def index():
        msgs = get_flashed_messages()
        with factory() as session:
            return render(session, " · ".join(msgs), tab=request.args.get("tab", "prehled"))

    @app.get("/auto/state")
    def auto_state():
        with factory() as session:
            led = Ledger(session)
            st = auto.status(led) or {}
            return jsonify(requested=bool(led._get(auto.REQUEST_KEY, "")), running=led._get(auto.RUNNING_KEY, ""), t=st.get("t", ""))

    @app.post("/deposit")
    def deposit():
        def fn(led: Ledger, _):
            led.deposit(float(request.form["amount"]))
            led.set_test_mode(request.form.get("test", "1") == "1")

        return action(fn, "Bankroll uložen.")

    @app.post("/nhl/goalie")
    def nhl_goalie():
        def fn(led: Ledger, _):
            from . import nhl_data

            team, name = request.form["team"].strip().upper(), request.form.get("name", "").strip()
            if name and name not in {n for n, _ in nhl_data.goalie_roster().get(team, [])}:
                raise ValueError(f"Brankář {name} v datech týmu {team} není.")
            led.set_goalie_override(team, name or None)
            return f"{team}: brankář {name} uložen, platí od dalšího skenu." if name else f"{team}: zpět na brankáře podle odchytaných zápasů."

        return action(fn, "", tab="nastaveni")

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
            from .nhl_settle import settle_paper, settle_tickets
            from .settlement import settle_everything

            nhl = settle_tickets(led, utcnow()) + settle_paper(led, utcnow())
            return " · ".join(nhl + settle_everything(led))

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
        form["use_foreign"] = f.get("use_foreign", "0")
        with factory() as session:
            led = Ledger(session)
            try:
                match = " - ".join(p.strip() for p in f["match"].replace("–", "-").split(" - ", 1))
                if " - " not in match:
                    raise ValueError("Zápas zapiš jako 'Domácí - Hosté'.")
                entries, errors = profile2.parse_odds(f.get("odds", ""), f.get("book") or "tipsport")
                if f.get("use_czech") == "1":
                    entries += profile2.from_czech(czechodds.load(led, utcnow()), match)
                foreign_src = ""
                if f.get("use_foreign") == "1":
                    have = {e.market for e in entries}  # rucne zadany trh ma prednost
                    try:
                        from .profil4.cli import build

                        p4store, _ = build(database_url)
                    except Exception:  # noqa: BLE001 - bez Profilu 4 aspon kurzove API
                        p4store = None
                    try:
                        got, foreign_src = profile2.foreign_entries(led, match, utcnow(), p4store)
                    finally:
                        if p4store is not None:
                            p4store.close()
                    entries += [e for e in got if e.market not in have]
                if not entries and not errors:
                    raise ValueError("Pro zápas nejsou žádné kurzy: zahraniční kurzy zápasu bot zatím nestáhl (načti kurzy "
                                     "v Přehledu nebo spusť sken Profilu 4), nebo kurzy zapiš ručně.")
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
                led._set(P2_FORM_KEY, json.dumps(form))
                session.commit()
                msg = (f"Rozbor {match} spočten ({len(a.offers)} nabídek)." + (f" Zahraniční kurzy: {foreign_src}." if foreign_src else "")
                       + (f" Přeskočeno: {'; '.join(errors)}" if errors else ""))
                return done(msg, "profil2")
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

    @app.get("/calc")
    def calc():  # jen vypocet, nic neuklada -> GET (obnoveni stranky se nepta na opetovne odeslani)
        f = request.args
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
