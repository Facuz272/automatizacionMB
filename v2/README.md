# Lead Engine v2 (Python)

> Vive en `v2/` de `Facuz272/automatizacionMB`, separado de la v1 (raíz del repo: el
> pipeline de emails en frío con `run_pipeline.py`, que sigue igual). La v2 no importa nada
> de la v1 y tiene su propio `requirements.txt`, `.env` y base local (`v2/data/state.db`).
> Su destino es el CRM `Facuz272/mbguiaclientes` (endpoint `/api/public/leads-import`,
> contrato en `docs/IMPORTAR_LEADS.md` de ese repo).

Busca condominios, HOAs y administradoras en el sur de Florida, descarta los que no
se pueden llamar o visitar, completa decisor/email/administración, precalifica con
Claude y envía el lote a `POST /api/public/leads-import` del CRM.

```
collect ─► gate ─► precheck CRM ─► enrich (por lead) ───────────────────────────► rank ─► output ─► (send)
Places     E.164   dry_run: quita   web (+Playwright/JSON hidratado) → teléfonos    lead_score  out/<lote>/   dry_run o
OSM        + calle teléfonos que    etiquetados → DBPR → búsqueda API → Apollo →    desc.       *.json(l)     --commit
CSV                ya están         OSM (pisos/m²/amenities/costa) → dolor →                    quality_report
                                    email MX → Claude (con guardas) → reglas → score
```

## Estructura

```
v2/
├── pyproject.toml / requirements.txt / .env.example
├── lead_engine/
│   ├── __main__.py        CLI: run | import | dbpr-sync | dbpr-load
│   ├── config.py          env + ciudades y consultas por estrategia (onsite/pain/portfolio)
│   ├── normalize.py       extract_domain, to_e164, phone_key, address_problem, emails (puro)
│   ├── models.py          Lead + brief() «Ficha» + crm_payload() (contrato exacto del endpoint)
│   ├── collectors/
│   │   ├── google_places.py  Places API (New): coordenadas + reseñas con fecha
│   │   ├── osm.py            Overpass (gratis): edificios con número de calle
│   │   └── seed_csv.py       listas públicas / directorios / exports
│   ├── web_extract.py     mailto/tel con etiqueta, JSON-LD, __NEXT_DATA__/estado hidratado, personas, «managed by»
│   ├── render.py          Playwright opcional para SPAs (RENDER_JS=auto)
│   ├── search.py          SerpAPI / Google CSE / Brave → administradora, CAM, email (solo resultados que nombran el edificio)
│   ├── dbpr.py            padrón oficial de condominios FL: descarga, carga y cruce por dirección / nombre
│   ├── geo.py             perfil físico OSM: pisos, huella m², techo, amenities lavables, distancia a la costa
│   ├── pain.py            detector de dolor ponderado EN/ES con cita y fecha
│   ├── email_verify.py    DNS MX/A con caché y sonda de resolvedor
│   ├── scoring.py         lead_score 0–100 y orden del lote
│   ├── quality.py         quality_report.json + resumen en consola
│   ├── costs.py           contadores de uso por API y costo estimado
│   ├── apollo.py          decisor por dominio (títulos de PM; revelar email es opcional)
│   ├── qualify.py         gate de contactabilidad, segment y gancho por reglas con superficies y evidencia
│   ├── ai_enrich.py       Claude con salida estructurada + guardas anti-inventos (personas, emails, superficies)
│   ├── crm_importer.py    cliente del endpoint: lotes ≤200, dry-run, reintentos, reporte
│   ├── state.py           SQLite local: no volver a pagar por lugares ya procesados
│   └── pipeline.py        orquestación
└── tests/                 pytest sin red (fixtures reales del DBPR)
```

## Radiografía de cada lead

| bloque | de dónde sale | qué deja |
| --- | --- | --- |
| **Físico** | OSM alrededor de las coordenadas de Places + texto del sitio + DBPR | `building`: pisos, huella m²/ft² (edificio y complejo), techo, `amenities` (parking_garage, pool_deck, tennis_court, sidewalks, gatehouse con su fuente), costero (distancia a la costa), unidades, `large` |
| **Decisor y administración** | sitio (HTML, JSON-LD, `__NEXT_DATA__`, render JS) → padrón DBPR («C/O …») → búsqueda API → Apollo → Claude | `contact_name/title`, `management_company`, `management_type`, `association_name`, `public_records.dbpr`, `search_hits` |
| **Teléfonos** | `tel:`, texto, JSON-LD `contactPoint`, estado hidratado | `phones[]` con etiqueta (management, hoa, front_desk, leasing, maintenance); el principal es el de administración; fax nunca |
| **Dolor** | reseñas de Google (con fecha) + avisos del sitio | `pain[]` con categoría, cita exacta y fecha; `pain_score` 0–10 (≥3 = verificado) |
| **Email** | mailto/texto/JSON/búsqueda/Apollo | el mejor cuyo dominio existe; `email_status` mx / a_only / unchecked (nxdomain y sin correo se descartan) |
| **Prioridad** | `scoring.py` | `lead_score` 0–100 + `score_breakdown`; el lote se envía ordenado de mayor a menor |

### lead_score (0–100)

| componente | pts | regla |
| --- | --- | --- |
| Decisor | 25 | nombre y apellido + LCAM/CAM/Property/Community/General/Building Manager (15 con otro cargo, 8 solo nombre) |
| Onsite o dolor | 25 | administración en el edificio confirmada o `pain_score ≥ 3` (10 si la estrategia es onsite sin confirmar) |
| Email MX | 15 | dominio con registros MX |
| Exposición/tamaño | 20 | costero (salitre) o grande: ≥8 pisos, ≥2.000 m² o ≥100 unidades (10 si mediano) |
| Tel. de administración | 15 | teléfono principal etiquetado Management Office / Property Manager (8 si HOA) |

El CRM agrega los leads importados al final de la cola en el orden recibido y el reparto de
las 6 AM los toma por posición: por eso `crm_payload.json` sale ordenado por `lead_score`.

### Qué ve la setter (`sales_strategy`)

```
Reseña (hace 2 meses) menciona moho/hongos: «Garage has oil stains and black mold in the hallways».
Pedir por Ana Diaz (Property Manager) y ofrecer inspección gratis de garaje/parking multinivel,
deck de piscina, canchas de tenis y fachada de 32 pisos expuesta al salitre.
Ficha: 32 pisos · ~2.002 m² de huella (~21.549 ft²) · 299 unidades · techo hormigón plano · garaje/parking
multinivel, deck de piscina, canchas de tenis, caminos/aceras, garita de acceso · frente costero (salitre) ·
tel. principal = administración · otros tel.: front desk +13058661301 · score 100/100
```

El CRM no tiene columnas para pisos, m², amenities ni score; la línea «Ficha» los lleva a la
tarjeta (se apaga con `CRM_INCLUDE_BRIEF=false`). El registro completo queda en `leads.jsonl`.

### Padrón oficial DBPR

```bash
python -m lead_engine dbpr-sync              # descarga Condo_MD.csv + condo_PB.csv (~13.000 condominios)
python -m lead_engine dbpr-sync --groups MD,PB,CE
```

Cruza cada lead por número + calle normalizada + ZIP (o por nombre dentro del ZIP) y lee la
línea «C/O» del registro: administradora («C/O CASTLE MANAGEMENT LLC»), oficina en el edificio
(«C/O MANAGEMENT OFFICE»), directivo de la junta («C/O PETER BATTY, PRESIDENT») o agente
registrado (no se usa como contacto). Con los datos de nov-2026 interpreta el 99 % de esas líneas.

### Búsqueda externa

Solo APIs oficiales (`SERPAPI_KEY`, `GOOGLE_CSE_KEY`+`GOOGLE_CSE_CX` o `BRAVE_SEARCH_KEY`); no se
hace scraping de páginas de resultados de buscadores. Se usa cuando el lead no tiene web o
cuando web + DBPR no dieron decisor y administradora. Solo se atribuye lo que aparece en un
resultado que nombra al edificio.

### Auditoría por lote

Cada `run` imprime y guarda `out/<lote>/quality_report.json`: % con decisor (y con cargo), %
con administradora identificada, % email verificado MX, % con señales de dolor (y verificado),
% con tel. de administración, % con perfil físico, % costero o grande, % cruce DBPR, % con IA,
`lead_score` promedio/mediana/bandas, categorías de dolor más frecuentes, motivos de rechazo y
costo estimado de APIs (total y por lead, precios ajustables con `PRICE_<API>`).

## Reglas que garantiza

- **Nada pasa sin teléfono E.164** (`+1XXXXXXXXXX`, NANP válido, sin rangos 555-01XX)
  **y dirección física** (número de calle, sin `@`, no P.O. Box, en `TARGET_STATE`).
  Si Google no trae teléfono o la dirección es solo «Miami, FL», se intenta completar
  desde el sitio (`tel:` y JSON-LD `PostalAddress`) antes de descartar.
- **El email nunca es obligatorio**: se guarda el mejor (personal del dominio > rol del
  dominio > otro); `noreply@`, placeholders e imágenes se descartan.
- **Leads sin web se conservan** (la v1 los tiraba): el canal es el teléfono.
- **Claude no inventa**: un nombre o administradora solo se acepta si aparece en la
  evidencia enviada, un email solo si está entre los encontrados; si el modelo inventa
  algo, se descarta también su gancho y queda el de reglas. Sin `ANTHROPIC_API_KEY`
  todo funciona igual con reglas.
- **Duplicados**: por `phone_key` (últimos 10 dígitos, igual que el CRM) dentro del lote,
  contra el ledger local y contra el CRM (precheck en dry-run antes de enriquecer).

## Qué recibe el CRM

Cada lead va con sus columnas nativas (migración `0045_lead_import_trace` del CRM):

| campo | origen |
| --- | --- |
| `company_name`, `phone` (E.164), `address`, `website_url`, `maps_url`, `google_rating`, `total_reviews` | colector + web |
| `contact_name`, `contact_title` | sitio web → Apollo → Claude (solo si figura en la evidencia) |
| `email` | mailto / texto / JSON-LD / Apollo (opcional) |
| `management_company` | «managed by …», OSM `operator`, Claude; si es propia: «Administración en el edificio» |
| `segment`, `strategy_kind`, `properties_count` | reglas + Claude |
| `sales_strategy` | gancho de 1–2 líneas con evidencia + línea «Ficha» (ver arriba) |
| `source` | `lead-engine-v2:<colector>` |
| `import_batch_id` | id del lote (`le2-AAAAMMDD-HHMM-<estrategia>-xxxx`); `imported_at` lo pone el CRM |

La tarjeta del lead muestra «Cargo · Administra» debajo del contacto. Para ver un lote
en el CRM: `SELECT position, company_name FROM leads WHERE import_batch_id = '…'`.
El registro completo (evidencia, páginas visitadas, señales de dolor, confianza de la
IA) queda en `out/<lote>/leads.jsonl`.

## Zona y modelo (todo por `.env`)

- `TARGET_STATE=FL`: estado exigido en la dirección (`''` lo desactiva).
- `TARGET_CITIES=Miami Beach,Aventura` para todas las estrategias, o
  `TARGET_CITIES_ONSITE` / `_PAIN` / `_PORTFOLIO` por estrategia. Vacío = listas de `config.py`.
- `OSM_BBOX=south,west,north,east` para OpenStreetMap.
- `ANTHROPIC_MODEL=claude-sonnet-5-5` (default, por costo) con `ANTHROPIC_EFFORT=low`.

## Primera corrida: 20 leads `onsite` en dry-run

```bash
cd v2
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'          # + '.[dev,browser]' y `playwright install chromium` para sitios SPA
cp .env.example .env    # completar GOOGLE_PLACES_API_KEY, ANTHROPIC_API_KEY, CRM_WEBHOOK_SECRET (Apollo opcional)
pytest -q
python -m lead_engine dbpr-sync
python -m lead_engine run --strategy onsite --limit 20 --dry-run
```

Deja en `out/<lote>/`:

| archivo | contenido |
| --- | --- |
| `quality_report.json` | auditoría de calidad y costo del lote |
| `crm_payload.json` | exactamente lo que se enviaría al CRM, ordenado por `lead_score` |
| `crm_response_dry.json` | respuesta real del CRM en dry-run (`would_insert`, `duplicates`, `rejected`) |
| `leads.jsonl` | registro completo por lead con evidencia |
| `rejected.jsonl` | descartados y motivo |

Inyectar ese mismo lote a producción, después de revisarlo:

```bash
python -m lead_engine import out/<lote>/crm_payload.json --commit
```

Otras variantes:

```bash
python -m lead_engine run --strategy portfolio --limit 30 --dry-run
python -m lead_engine run --strategy pain --limit 20 --dry-run          # solo con reseñas de suciedad/moho/etc.
python -m lead_engine run --source csv --seed condos_dbpr.csv --strategy onsite --limit 50 --dry-run
python -m lead_engine run --strategy onsite --limit 20 --no-ai          # solo reglas
```

## Costos por corrida de 20

- Places (New) Text Search: ~1–8 páginas de 20 resultados con reseñas (Enterprise + Atmosphere,
  ~US$0,04 por página). Se corta al llegar a 20 leads válidos.
- Búsqueda externa: como mucho 1 consulta por lead con huecos (SerpAPI ~US$0,015; CSE/Brave ~US$0,005).
- OpenStreetMap, DBPR, DNS y Playwright: gratis.
- Claude Sonnet 5.5: una llamada por lead aceptado (`ANTHROPIC_EFFORT=low`), ~5–8k tokens de
  entrada y <1k de salida ≈ US$0,02 por lead, ~US$0,40 por lote de 20.
- Apollo: 0 créditos salvo `APOLLO_REVEAL_EMAILS=true`.
- El costo real estimado de cada corrida queda en `quality_report.json` → `cost_estimate_usd`.
