"""Self-contained HTML audit panel per batch: out/<batch>/index.html (no network, no external files).

Written when a batch closes with PDFs (`run --pdf`, `daily`, `enrich-phones --pdf`) and for
`portfolios --pdf`. Batch panel: metrics (accepted, rejected, without phone, annual value, PDFs,
consolidated buildings), and a sortable / filterable table with each accepted building — score,
annual ticket, vertical and signal chips, phone, links to its English PDF (pdf/) and Spanish field
sheet (campo/), and buttons that copy the email / WhatsApp text of the outreach pack. Everything is
embedded as JSON in the page and rendered by a few lines of vanilla JS; links are relative so the
folder can be zipped or shared as is.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Iterable

from .models import PHONE_LABEL
from .pdf_common import safe_filename

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#1f2933;--muted:#61707e;--rule:#d5dde5;--brand:#0e5a8a;--chip:#eef3f7;
--good:#1d7a46;--warn:#9a6700;--bad:#b42318}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#101418;--card:#171c21;--ink:#e6edf3;
--muted:#9aa7b4;--rule:#2b333b;--brand:#5aa9e6;--chip:#222a31;--good:#4ac27e;--warn:#e0b24a;--bad:#f0807a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif}main{max-width:1200px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:20px;margin:0 0 2px}.sub{color:var(--muted);margin:0 0 18px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px}
.kpi{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:10px 12px}
.kpi b{display:block;font-size:20px}.kpi span{color:var(--muted);font-size:12px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}input[type=search]{flex:1;min-width:200px;padding:8px 10px;
border:1px solid var(--rule);border-radius:6px;background:var(--card);color:var(--ink)}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--rule);border-radius:8px}
table{border-collapse:collapse;width:100%;min-width:860px}th,td{padding:8px 10px;border-bottom:1px solid var(--rule);
text-align:left;vertical-align:top}th{font-size:12px;color:var(--muted);cursor:pointer;user-select:none;white-space:nowrap}
th[data-k]:after{content:" ↕";opacity:.4}tr:last-child td{border-bottom:0}.name{font-weight:600}.addr{color:var(--muted);font-size:12px}
.chip{display:inline-block;background:var(--chip);border-radius:999px;padding:1px 8px;margin:2px 3px 0 0;font-size:12px;white-space:nowrap}
.s-hi{color:var(--good);font-weight:600}.s-mid{color:var(--warn);font-weight:600}.s-lo{color:var(--bad);font-weight:600}
a{color:var(--brand)}button{border:1px solid var(--rule);background:var(--card);color:var(--ink);border-radius:6px;
padding:3px 8px;margin:2px 4px 2px 0;font-size:12px;cursor:pointer}button:hover{border-color:var(--brand)}
.links a{margin-right:8px;white-space:nowrap}.empty{padding:24px;color:var(--muted)}
"""

JS = """
const D=JSON.parse(document.getElementById('data').textContent);const tb=document.querySelector('tbody');
let key='score',dir=-1;const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const usd=n=>n?'$'+Number(n).toLocaleString('en-US'):'—';
function cls(s){return s>=75?'s-hi':s>=50?'s-mid':'s-lo'}
function render(){const q=document.getElementById('q').value.toLowerCase();
const rows=D.rows.filter(r=>!q||JSON.stringify(r).toLowerCase().includes(q)).sort((a,b)=>{const x=a[key],y=b[key];
return (typeof x==='number'||typeof y==='number')?((x||0)-(y||0))*dir:String(x||'').localeCompare(String(y||''))*dir});
tb.innerHTML=rows.length?rows.map(r=>`<tr><td>${r.rank}</td><td><div class=name>${esc(r.name)}</div><div class=addr>${esc(r.address)}</div>
${r.siblings?`<span class=chip>+${r.siblings} edificios de la misma firma</span>`:''}</td>
<td><span class="${cls(r.score)}">${r.score}</span></td><td>${usd(r.low)} – ${usd(r.high)}</td><td><span class=chip>${esc(r.vertical)}</span></td>
<td>${(r.signals||[]).map(s=>`<span class=chip>${esc(s)}</span>`).join('')||'—'}</td>
<td>${r.phone?`<a href="tel:${esc(r.phone)}">${esc(r.phone)}</a>`:'—'}<div class=addr>${esc(r.phone_label||'')}</div></td>
<td class=links>${r.pdf?`<a href="${esc(r.pdf)}">PDF (EN)</a>`:''}${r.campo?`<a href="${esc(r.campo)}">Hoja de campo</a>`:''}<br>
${r.email?`<button data-copy="email" data-i="${r.i}">Copiar email</button>`:''}${r.sms?`<button data-copy="sms" data-i="${r.i}">Copiar WhatsApp</button>`:''}</td></tr>`).join('')
:`<tr><td colspan=8 class=empty>Sin resultados</td></tr>`}
document.getElementById('q').addEventListener('input',render);
document.querySelectorAll('th[data-k]').forEach(th=>th.addEventListener('click',()=>{const k=th.dataset.k;dir=key===k?-dir:-1;key=k;render()}));
tb.addEventListener('click',e=>{const b=e.target.closest('button[data-copy]');if(!b)return;const r=D.rows[b.dataset.i];
const t=b.dataset.copy==='email'?`Subject: ${r.email.subject}\\n\\n${r.email.body}`:r.sms;
const done=()=>{const o=b.textContent;b.textContent='Copiado ✓';setTimeout(()=>b.textContent=o,1400)};
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t).then(done)}else{const a=document.createElement('textarea');
a.value=t;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove();done()}});render();
"""


def file_names(records: list[dict[str, Any]]) -> dict[int, str]:
    """index in `records` → file stem, with the same order and de-duplication as pdf_generator.generate_batch."""
    order = sorted(range(len(records)), key=lambda i: -(records[i].get("lead_score") or 0))
    used: set[str] = set()
    out = {}
    for i in order:
        name = safe_filename(records[i].get("company_name") or "lead")
        cand, n = name, 2
        while cand in used:
            cand, n = f"{name}_{n}", n + 1
        used.add(cand)
        out[i] = cand
    return out


def _signals(r: dict[str, Any]) -> list[str]:
    seen, out = set(), []
    for s in (r.get("municipal") or []) + (r.get("pain") or []):
        lab = s.get("label")
        if lab and lab not in seen:
            seen.add(lab)
            out.append(lab)
    return out[:4]


def _page(title: str, sub: str, kpis: list[tuple[str, str]], headers: list[tuple[str, str | None]], data: dict) -> str:
    k = "".join(f'<div class="kpi"><b>{html.escape(v)}</b><span>{html.escape(t)}</span></div>' for t, v in kpis)
    th = "".join(f'<th{f" data-k={chr(34)}{key}{chr(34)}" if key else ""}>{html.escape(h)}</th>' for h, key in headers)
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (f'<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>{CSS}</style></head>'
            f'<body><main><h1>{html.escape(title)}</h1><p class="sub">{html.escape(sub)}</p><div class="kpis">{k}</div>'
            f'<div class="bar"><input id="q" type="search" placeholder="Filtrar por nombre, ciudad, señal, vertical…"></div>'
            f'<div class="wrap"><table><thead><tr>{th}</tr></thead><tbody></tbody></table></div></main>'
            f'<script type="application/json" id="data">{blob}</script><script>{JS}</script></body></html>')


def build(out_dir: str | Path, records: Iterable[dict[str, Any]], *, quality: dict[str, Any] | None = None,
          outreach: list[dict[str, Any]] | None = None) -> Path:
    out = Path(out_dir)
    recs = list(records)
    q = quality or {}
    names = file_names(recs)
    packs = {p.get("company_name"): p for p in outreach or []}
    rows = []
    for i, r in enumerate(recs):
        d = r.get("estimated_deal") or {}
        stem = names[i]
        pack = packs.get(r.get("company_name")) or {}
        primary = next((p for p in r.get("phones") or [] if p.get("number") == r.get("phone")), {})
        rows.append({
            "i": i, "rank": i + 1, "name": r.get("company_name"), "address": r.get("address"),
            "score": r.get("lead_score") or 0, "low": d.get("low") or 0, "high": d.get("high") or 0,
            "vertical": r.get("vertical") or "", "signals": _signals(r), "phone": r.get("phone"),
            "phone_label": PHONE_LABEL.get(primary.get("label"), primary.get("label")),
            "siblings": len(r.get("portfolio_siblings") or []),
            "pdf": f"pdf/{stem}.pdf" if (out / "pdf" / f"{stem}.pdf").exists() else None,
            "campo": f"campo/{stem}_campo.pdf" if (out / "campo" / f"{stem}_campo.pdf").exists() else None,
            "email": pack.get("email_en"), "sms": pack.get("sms_en"),
        })
    low = sum(r["low"] for r in rows)
    high = sum(r["high"] for r in rows)
    needs = len((out / "needs_phone.csv").read_text(encoding="utf-8").splitlines()) - 1 if (out / "needs_phone.csv").exists() else 0
    kpis = [("Aceptados", str(len(rows))), ("Rechazados", str(q.get("rejected", "—"))),
            ("Sin teléfono propio (needs_phone.csv)", str(needs)),
            ("Valor anual estimado", f"${low:,} – ${high:,}" if high else "—"), ("PDF", str(q.get("pdfs", "—"))),
            ("Consolidados por firma", str(q.get("consolidated_siblings", 0)))]
    if q.get("mix"):
        kpis.append(("Mix", " · ".join(f"{k} {v['accepted']}/{v['quota']}" for k, v in q["mix"].items())))
    if q.get("crm"):
        c = q["crm"]
        kpis.append(("CRM", f"{'insertados ' + str(c.get('inserted')) if not c.get('dry_run') else 'dry-run'} · "
                            f"duplicados {len(c.get('duplicates') or [])}"))
    headers = [("#", "rank"), ("Edificio", "name"), ("Score", "score"), ("Ticket anual", "high"), ("Vertical", "vertical"),
               ("Señales", None), ("Teléfono", None), ("Archivos y mensajes", None)]
    path = out / "index.html"
    path.write_text(_page(f"Lote {out.name}", f"Estrategia: {q.get('strategy', '—')} · generado por MB Lead Engine v2",
                          kpis, headers, {"rows": rows}), encoding="utf-8")
    return path


def build_portfolios(out_dir: str | Path, firms: list[dict[str, Any]]) -> Path:
    """index.html for `portfolios --pdf`: ranking with KPIs and a link to each firm's dossier."""
    out = Path(out_dir)
    rows = []
    for i, f in enumerate(firms):
        stem = safe_filename(f["firm"])
        urgent = [b for b in f.get("urgent") or [] if b.get("signals")]
        rows.append({"i": i, "rank": i + 1, "name": f["firm"], "address": ", ".join(f.get("cities") or []),
                     "score": f["buildings"], "low": f.get("total_estimated_low") or 0,
                     "high": f.get("total_estimated_usd") or 0, "vertical": f"lic. {f.get('license') or '—'}",
                     "signals": [f"Milestone {f.get('milestone_window', 0)}", f"registros {f.get('municipal_records', 0)}"]
                     + [b["name"] for b in urgent[:2]],
                     "phone": None, "phone_label": f"{f['units']:,} unidades", "siblings": 0,
                     "pdf": f"pdf/{stem}.pdf" if (out / "pdf" / f"{stem}.pdf").exists() else None,
                     "campo": None, "email": None, "sms": None})
    kpis = [("Administradoras", str(len(rows))), ("Edificios", f"{sum(f['buildings'] for f in firms):,}"),
            ("Unidades", f"{sum(f['units'] for f in firms):,}"),
            ("Valor anual combinado (alto)", f"${sum(f.get('total_estimated_usd') or 0 for f in firms):,}")]
    headers = [("#", "rank"), ("Administradora", "name"), ("Edificios", "score"), ("Valor anual", "high"),
               ("Licencia", "vertical"), ("Señales", None), ("Unidades", None), ("Dossier", None)]
    path = out / "index.html"
    path.write_text(_page(f"Portafolios {out.name}", "Ranking de administradoras en la zona de servicio", kpis, headers,
                          {"rows": rows}), encoding="utf-8")
    return path
