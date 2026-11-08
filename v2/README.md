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
collect ──► gate ──► precheck CRM ──► enrich ──► output ──► (send)
Places      E.164    dry_run: quita    web +       out/<lote>/  dry_run o
OSM         + calle  teléfonos que     Apollo +    *.json(l)    --commit
CSV         (web     ya están, antes   reglas +
            rellena) de gastar IA      Claude
```

## Estructura

```
v2/
├── pyproject.toml / requirements.txt / .env.example
├── lead_engine/
│   ├── __main__.py        CLI: run | import
│   ├── config.py          env + ciudades y consultas por estrategia (onsite/pain/portfolio)
│   ├── normalize.py       extract_domain, to_e164, phone_key, address_problem, emails (puro)
│   ├── models.py          Lead + crm_payload() (contrato exacto del endpoint)
│   ├── collectors/
│   │   ├── google_places.py  Places API (New) Text Search, reseñas solo en `pain`
│   │   ├── osm.py            Overpass (gratis): edificios con número de calle
│   │   └── seed_csv.py       listas públicas / directorios / exports (DBPR, CAI, Apollo…)
│   ├── web_extract.py     mailto:, tel:, JSON-LD, «Nombre, Property Manager», «managed by X»
│   ├── apollo.py          decisor por dominio (títulos de PM; revelar email es opcional)
│   ├── qualify.py         gate de contactabilidad, señales de dolor, segment y gancho por reglas
│   ├── ai_enrich.py       Claude con salida estructurada + filtro anti-inventos
│   ├── crm_importer.py    cliente del endpoint: lotes ≤200, dry-run, reintentos, reporte
│   ├── state.py           SQLite local: no volver a pagar por lugares ya procesados
│   └── pipeline.py        orquestación y resumen de cobertura
└── tests/                 pytest (sin red)
```

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
| `sales_strategy` | **solo** el gancho de 1–2 líneas para la setter |
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
pip install -e '.[dev]'
cp .env.example .env    # completar GOOGLE_PLACES_API_KEY, ANTHROPIC_API_KEY, CRM_WEBHOOK_SECRET (Apollo opcional)
pytest -q
python -m lead_engine run --strategy onsite --limit 20 --dry-run
```

Deja en `out/<lote>/`:

| archivo | contenido |
| --- | --- |
| `crm_payload.json` | exactamente lo que se enviaría al CRM |
| `crm_response_dry.json` | respuesta real del CRM en dry-run (`would_insert`, `duplicates`, `rejected`) |
| `leads.jsonl` | registro completo por lead con evidencia |
| `rejected.jsonl` | descartados y motivo |
| `summary.json` | cobertura de dirección, decisor, email, administración, IA |

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

- Places (New) Text Search: ~1–8 páginas de 20 resultados (Enterprise SKU por los campos
  de teléfono/web; `pain` suma reseñas). Se corta al llegar a 20 leads válidos.
- Claude Sonnet 5.5: una llamada por lead aceptado (`ANTHROPIC_EFFORT=low`), ~5–8k tokens de
  entrada y <1k de salida ≈ US$0,02 por lead, ~US$0,40 por lote de 20.
- Apollo: 0 créditos salvo `APOLLO_REVEAL_EMAILS=true`.
