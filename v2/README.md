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
│   ├── __main__.py        CLI: run | import | pdf | route | dbpr-sync | dbpr-load | cam-load
│   ├── config.py          env + ciudades y consultas por estrategia (onsite/pain/portfolio)
│   ├── normalize.py       extract_domain, to_e164, phone_key, address_problem, emails (puro)
│   ├── models.py          Lead + brief() «Ficha» + crm_payload() (contrato exacto del endpoint)
│   ├── collectors/
│   │   ├── google_places.py  Places API (New): coordenadas + reseñas con fecha
│   │   ├── osm.py            Overpass (gratis): edificios con número de calle + búsqueda por dirección
│   │   ├── dbpr_seed.py      semilla desde el padrón DBPR (sin Google Places)
│   │   ├── appraiser_seed.py semilla desde el catastro: plazas, edificios comerciales, hospitales
│   │   └── seed_csv.py       listas públicas / directorios / exports
│   ├── web_extract.py     mailto/tel con etiqueta, JSON-LD, __NEXT_DATA__/estado hidratado, personas, «managed by»
│   ├── render.py          Playwright opcional para SPAs (RENDER_JS=auto)
│   ├── search.py          SerpAPI / Google CSE / Brave → administradora, CAM, email (solo resultados que nombran el edificio)
│   ├── dbpr.py            padrón oficial de condominios FL: descarga, cruce por dirección/nombre, rutas «C/O», cluster
│   ├── cam.py             padrón de licencias CAM (personas) y CAB (firmas) del DBPR: verificación de nombres
│   ├── names.py           normalización de firmas y personas (abreviaturas, tipeos, variantes de espaciado)
│   ├── municipal_signals.py  open data municipal: recertificación pendiente, estructura insegura, código, permisos
│   ├── valuation.py       ticket anual estimado de limpieza exterior (rango USD)
│   ├── appraiser.py       catastro Miami-Dade (Property Appraiser) + ventana Milestone / recertificación
│   ├── lodging.py         padrón DBPR de alojamiento (hoteles, moteles, apart-hoteles): habitaciones, operador, teléfono
│   ├── npi.py             NPI Registry (CMS): clínicas/hospitales con Authorized Official
│   ├── verticals.py       segmentos de negocio y vocabulario comercial por segmento
│   ├── roles.py           roles de decisión (PM, facilities, asistente, junta) y horario de oficina
│   ├── mgmt_domains.py    dominios corporativos verificados de administradoras
│   ├── pdf_generator.py   informe técnico preliminar de preventa (PDF de 2 páginas, EN/ES, QR) por lead
│   ├── pdf_common.py      estilos, formatos EN/ES, QR vectorial y pie compartidos por los PDF
│   ├── pdf_i18n.py        textos EN/ES con terminología de Florida (SB 4-D, 40-Year Recertification)
│   ├── portfolios.py      ranking de administradoras por portafolio + señales en vivo
│   ├── portfolio_pdf.py   dossier ejecutivo de 2 páginas por firma (Preferred Vendor Program)
│   ├── phone_resolver.py  cascada gratuita de teléfonos (DBPR Lodging, central de la firma)
│   ├── tenant.py          perfiles de empresa white-label (tenants/<slug>.json)
│   ├── portfolio_consolidation.py  un teléfono central, varios edificios: se cotizan en la misma llamada
│   ├── outreach_pack.py   email + WhatsApp/SMS en inglés y guion telefónico en español por lote
│   ├── daily.py           rutina diaria: Pool del CRM → mix de verticales → PDFs + outreach → CRM → Slack
│   ├── inspector_sheet.py hoja de campo y cotización interna (español) para los inspectores
│   ├── broward.py         catastro de Broward (tax roll del condado): lookup y semilla del corredor este
│   ├── overpass.py        Overpass (OSM) con mirrors, endpoint preferido y corte por corrida
│   ├── preflight.py       diagnóstico OK / PENDIENTE / ERROR de claves, perfil, padrones y red
│   ├── ads_export.py      audiencias B2B (Meta / Google / LinkedIn) en CSV
│   ├── commercial_mgmt.py operadores de plazas / edificios comerciales (catálogo verificado en data/)
│   ├── batch_report.py    panel HTML autocontenido del lote (out/<lote>/index.html)
│   ├── competitor_intel.py  debilidades de la competencia en reseñas de edificios vecinos
│   ├── route_optimizer.py rutas de inspección por zonas e inspector
│   ├── geocode.py         geocodificación gratuita (US Census) para leads sin coordenadas
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
python -m lead_engine dbpr-sync              # condominios + licencias CAM + alojamiento (hrlodge1/2)
python -m lead_engine dbpr-sync --groups MD,PB,CE
```

Cruza cada lead por número + calle normalizada + ZIP (o por nombre dentro del ZIP) y lee la
línea «C/O» del registro: administradora («C/O CASTLE MANAGEMENT LLC»), oficina en el edificio
(«C/O MANAGEMENT OFFICE»), directivo de la junta («C/O PETER BATTY, PRESIDENT») o agente
registrado (no se usa como contacto). Con los datos de nov-2026 interpreta el 99 % de esas líneas.

### PDF de preventa

```bash
python -m lead_engine run --seed dbpr --strategy onsite --limit 20 --dry-run --pdf   # PDFs del lote
python -m lead_engine pdf out/<lote>/leads.jsonl --top 10                          # o después, de un lote ya generado
```

Un PDF de 2 páginas por lead en `out/<lote>/pdf/<edificio>.pdf`, listo para mandar por mail o
WhatsApp. Página 1: portada con los datos oficiales (catastro de Miami-Dade y DBPR: año y antigüedad,
pisos, unidades, ft², lote, uso de suelo, valor tasado, asociación, administración, folio). Página
2: diagnóstico técnico (Milestone/recertificación, permisos y casos municipales, reseñas con
evidencia, salitre, antecedentes de la zona, superficies), alcance del servicio softwash según el
segmento, cuadro con el rango del contrato anual por componente y próximo paso. El contenido de cada
página se ajusta para que el informe sea siempre de 2 páginas. Lleva la marca de `COMPANY_*` (solo
lo que esté definido) y la aclaración de que es una estimación preliminar, no una inspección
estructural ni una cotización vinculante.

**Idioma** (`--pdf-lang en|es`, `pdf --lang`, default `PDF_LANG=en`): la versión en inglés usa la
terminología del mercado de Florida — *SB 4-D Milestone Structural Inspection (F.S. 553.899)*,
*40-Year Recertification*, *Softwash Biological Treatment*, *surface preparation before elastomeric
coating* — y formatos de EE. UU. ($1,234 · mm/dd/yyyy). Las señales y reseñas se reescriben desde
los datos estructurados, así el PDF nunca mezcla idiomas. COI y W-9 solo aparecen si están en
`COMPANY_DOCUMENTS` ("available upon request"; nunca "adjunto").

**QR**: en el pie de la página 2, un QR vectorial (`reportlab` QrCodeWidget, sin dependencias) que
apunta a `COMPANY_BOOKING_URL`, o al sitio web, o a `tel:`; sin ninguno de los tres no se imprime.

### Dossiers de portafolio (`portfolios`)

```bash
python -m lead_engine portfolios --top 15 --pdf            # ranking + dossier por firma (inglés)
python -m lead_engine portfolios --top 30 --offline        # solo el padrón, sin consultas en vivo
```

Cuenta **solo los edificios dentro de la zona de servicio**: la unión de `target_cities` del perfil
(o `TARGET_CITIES*` del `.env`), con los alias por código postal (Brickell → 33129-33131, Sunny
Isles → 33160…). Edificios, unidades, valor del portafolio y dossier quedan acotados a esa zona.
Agrupa el padrón DBPR de `CLUSTER_COUNTIES` por firma (licencia CAB cuando el padrón de CAM la
conoce; si no, el nombre normalizado; y une variantes que el catálogo de dominios identifica como
la misma empresa, p. ej. «Castle Group» + «Castle Management LLC»). Por firma: edificios, unidades
y contrato anual combinado estimado; ordena por el extremo alto. Para el top consulta en vivo los
`--depth` (10) edificios más grandes **en Miami-Dade** (donde hay catastro y capas municipales):
cuántos están en ventana Milestone/recertificación y cuántos tienen registros municipales abiertos.
Deja `out/portfolios/<fecha>/ranking.csv`, `ranking.json` (con los edificios) y, con `--pdf`, un
dossier ejecutivo de 2 páginas por firma: KPIs del portafolio, propiedades más urgentes, propuesta
de *Preferred Vendor Program* con las escalas del perfil (`pvp_tiers`), piloto sugerido, próximos
pasos y QR. Corrida de nov-2026 con el perfil `mb_softwash` (solo su zona): FirstService
Residential 37 edificios / 5.728 unidades (82 / 16.380 en todo el padrón); Castle Management 6
edificios (sin Bonaventure ni el oeste de Broward), 2 de 5 analizados en ventana Milestone. Seacrest y Campbell, casi todo Palm Beach, salen
del top 15.

**Corredor este de Broward.** En el perfil, `zip_restrictions` limita Fort Lauderdale, Hollywood y
Hallandale Beach a sus códigos postales costeros/este (Fort Lauderdale: 33301, 33304-33306, 33308,
33309, 33311, 33312, 33315, 33316, 33334; Hollywood: 33019-33021; Hallandale: 33009) y
`exclude_zips` saca siempre el oeste lejano (33325-33327, 33330-33332: Bonaventure, Weston). Vale
para el ranking de portafolios, la semilla DBPR y la semilla del catastro.

**Permisos y 40/50 años en Fort Lauderdale.** La capa pública de permisos de la ciudad
(gis.fortlauderdale.gov, ~0,5 s, 200 mil permisos; datos hasta mar-2026) se consulta con 5 s de tope:
una solicitud abierta del *Building Safety Inspection Program* (BSIP, la recertificación 40/50 años de
Broward) es una señal de recertificación pendiente (peso 6, como la de Miami) y los permisos de techo,
ventanas/puertas, shutters y estructurales del edificio (no de una unidad, sin carpas ni carteles) son
obra exterior reciente. Hollywood, Hallandale Beach y el condado no publican una capa equivalente
(búsqueda en ArcGIS Online y en sus servidores, nov-2026).

**Catastro en vivo.** Miami-Dade: Property Appraiser + capas municipales. Este de Broward: tax roll
del Broward County Property Appraiser (capa pública de Broward County GIS con todos los folios,
unidades de condominio incluidas; ~0,5 s por consulta en nov-2026): año de construcción, unidades
y pisos por números de unidad, así que la ventana Milestone también se calcula ahí. Cada consulta
tiene 5 s de tope y el primer error o demora la desactiva por el resto de la corrida. La capa
estatal FDOR se descartó (~55 s por consulta).

### Un teléfono central, varios edificios (`portfolio_consolidation`)

Con `ALLOW_MGMT_FIRM_PHONE=true`, varios edificios de una misma administradora pueden resolver al
mismo teléfono central, y el CRM acepta cada teléfono una sola vez. Antes de enriquecer, a todos
los que comparten esa central se les hace un chequeo oficial liviano (catastro, Milestone,
registros municipales, ticket estimado); el más fuerte (señales urgentes, luego ticket, luego
unidades) se queda con el teléfono y los demás se listan en él: en el `sales_strategy` de la
setter («Teléfono = central de X: al llamar, cotizar también en el mismo llamado [Edificio 2 – 240
un., permiso de obra exterior] y […]»), en una tabla del PDF y en el outreach pack. Siguen en
`needs_phone.csv` con `consolidated_into` y `firm_phone`. La central de la firma es siempre el
último recurso: antes se buscan Places, DBPR Lodging, OSM y el sitio propio del edificio.

```bash
python -m lead_engine enrich-phones out/<lote>/needs_phone.csv --dry-run --pdf
```

Reintenta los pendientes con lo que haya configurado ahora (`GOOGLE_PLACES_API_KEY`, búsqueda
externa…), con su fuente e id originales, y arma el lote completo (`crm_payload.json`, PDFs,
outreach pack) listo para `--commit` o `import`.

### Outreach Pack

Cada lote deja `out/<lote>/outreach_pack.md` y `.json`: por edificio, email de primer contacto y
WhatsApp/SMS (≤320 caracteres) en inglés comercial de EE. UU., y el guion telefónico en español
para la setter (apertura, motivo con el dato real, oferta, PDF, edificios hermanos, dos objeciones
y cierre). El gancho es el dato verificable más fuerte: ventana Milestone (año de construcción),
recertificación o caso abierto, permiso de obra exterior, reseña, y después ft²/unidades. Sin
nombre de contacto dice "Hi there,"; agenda, teléfono, email y documentos solo si están en el perfil.

### Sin OpenStreetMap: mirrors e inferencia catastral

Overpass se consulta en orden: `OVERPASS_URL`, después `OVERPASS_MIRRORS` (por defecto
overpass.kumi.systems y lz4.overpass-api.de). Un reset, timeout, 429 o 5xx pasa al siguiente al
instante (6 s para conectar, 15 s de lectura en los mirrors); el que respondió se usa primero el
resto de la corrida, y si todos fallan dos veces seguidas Overpass se apaga para esa corrida.

Cuando OSM no da perfil, las superficies se infieren de datos oficiales y quedan marcadas como
estimación (nunca como amenity observada):

- plazas y comerciales: si el lote catastral es ≥ 1,3× la huella, estacionamiento/hardscape =
  50 % de (lote − huella), «estimado por lote catastral»;
- condominios y hoteles con ≥ 6 pisos, ≥ 75 unidades o en la costa: garaje (unidades × 250 ft²) y
  pool deck / áreas recreativas, «est. por escala catastral — validar en sitio».

Entran en el ticket estimado (`basis.inferred`), el PDF del Property Manager los muestra como «est.,
to be confirmed on site», y la hoja de campo siempre tiene esas filas, aunque el registro venga de
un lote viejo o de un export del CRM.

### Hoja de campo para inspectores (`inspector_sheet`)

Con `--pdf` (y en `pdf` y `route`) se genera, además del brief en inglés para el Property Manager,
una hoja interna en español por edificio (`out/<lote>/campo/<edificio>_campo.pdf`; en `route`, un
PDF por inspector con sus visitas en orden: `ruta_<fecha>_<inspector>_campo.pdf`):

- superficies (ft²) con la misma geometría de `valuation.py`: fachada, aceras/entradas, garaje,
  pool deck, dumpster pads, canchas, estacionamiento del lote; las no detectadas se marcan para
  confirmar en sitio;
- operación: horas de cuadrilla por superficie, jornadas, galones de mezcla, hipoclorito al 12,5 %
  y surfactante, acceso por cuerdas desde 8 pisos;
- tres opciones para cerrar en el lugar: 1) Entrada/Horizontal (con precio piso), 2) Completa /
  Milestone, 3) Contrato Anual Preferente (opción 2 + mantenimientos semestrales de alto tránsito
  con descuento; nunca más barato que la opción 2), con equivalente mensual.

Rendimientos, mezclas, cuadrilla, precio piso y descuento son valores de referencia
(`inspector_sheet.FIELD_DEFAULTS`) que el perfil ajusta con `field_rates`.

### Rutina diaria (`daily`)

```bash
python -m lead_engine daily --target-pool 40            # dry-run contra el CRM
python -m lead_engine daily --target-pool 40 --commit   # importa
```

1. Consulta `GET /api/public/health` del CRM, que devuelve `pool_available` y `pool_target`: el lote
   es el objetivo menos los disponibles (objetivo = `--target-pool`, si no el `pool_target` del CRM,
   si no 40). Con una versión del CRM que solo informe `pool_low`, el lote es el objetivo completo
   cuando el Pool está bajo y `--fallback-limit` (10) cuando no. `--limit` fuerza el tamaño.
2. Corre con el perfil `mb_softwash` (o `--tenant`/`TENANT`), salteando lo ya trabajado. Si el
   perfil tiene `daily_mix`, reparte el lote entre verticales (MB Softwash: 35 % condominios
   onsite, 25 % condominios con administradora, 25 % plazas, 10 % edificios comerciales, 5 %
   clínicas NPI); cada porción corre por separado, lo que falte se completa con la primera, y el
   lote final se une, se deduplica por teléfono y se ordena por `lead_score` y ticket antes de los
   PDFs, las hojas de campo, el outreach pack y el CRM. `--seed` o `--no-mix` vuelven a una sola
   fuente.
3. `--commit` importa los aceptados; si no, dry-run.
4. Con `SLACK_WEBHOOK_URL`, resumen: leads inyectados, valor anual del lote, top 3 con su señal y la
   carpeta de PDFs. También queda en `out/<lote>/daily_summary.json`.

### Perfiles de empresa (`--tenant`)

```bash
python -m lead_engine --tenant mb_softwash run --seed dbpr --limit 20 --pdf
```

`tenants/<slug>.json` define marca (nombre, teléfono, web, agenda, documentos), color e idioma del
PDF, ciudades objetivo por estrategia, `min_units`, tarifas de `valuation.py`, escalas del PVP,
teléfonos de administradoras propios e inspectores. Claves desconocidas o tarifas mal formadas
abortan la corrida. Ejemplo: `tenants/mb_softwash.json`.

### Memoria entre corridas

`data/state.db` → tabla `seen_leads`: cada edificio trabajado, por dirección normalizada (la misma
torre que llega por DBPR, catastro o Places es una sola) y por id de origen, con estado
(`processed`, `imported`, `in_crm`, `needs_phone`, `rejected`; nunca baja de estado). La corrida
siguiente los salta, así `--seed dbpr --limit 20` trae los 20 siguientes del ranking.
`--ignore-seen` los vuelve a incluir; `--retry-needs-phone` reintenta solo los que quedaron sin
teléfono. El `quality_report.json` cuenta `skipped_seen_in_earlier_runs`.

### Inteligencia competitiva de la zona

Con las reseñas de todos los edificios vistos en la corrida (gratis) y, si `COMPETITOR_NEARBY=true`,
una búsqueda Places Nearby por lead, `competitor_intel.py` busca frases sobre empresas de limpieza
exterior con una debilidad: el moho vuelve a las semanas, la alta presión dañó pintura o selladores,
rayas, incumplimientos, daños a plantas o filtraciones, sobreprecios (inglés y español). El gancho
suma una línea de cierre sin nombrar a ningún competidor (los nombres quedan en la evidencia):

```
Zona: reseñas de 2 edificio(s) vecino(s) dicen que el moho y las manchas vuelven a las pocas semanas
después del lavado a presión → argumento: el softwash trata el moho con producto a baja presión en vez
de solo arrastrarlo, por eso dura más.
```

### Gancho de zona y Plan B en todos los leads

El gancho de zona (`Zona: … → argumento: …`) tiene tres niveles, en orden:

1. reseñas de edificios vecinos que muestran una debilidad de una empresa de limpieza (sin nombrarla);
2. reseñas de vecinos que describen problemas físicos o de mantenimiento exterior (moho, algas,
   suciedad, manchas, fachada, pool deck, pasillos, garaje, mantenimiento, obras, 40 años /
   inspección): nombra hasta dos de esos edificios y la calle;
3. si nadie se queja de nada: proximidad, con hasta dos edificios vecinos reales que Google Places
   devolvió en el mismo radio («Estuvimos relevando propiedades sobre Collins Ave cerca de A y B»).
   Nunca nombra al propio edificio. Requiere `competitor_nearby` (activo en `mb_softwash`; una
   búsqueda Nearby de Places por lead).

El Plan B usa primero un segundo contacto con nombre del sitio del edificio. Si no hay sitio o es un
portal cerrado (BuildingLink), se arma uno táctico: el teléfono publicado de la administradora / CAM
(o del operador comercial) cuando es distinto del teléfono del lead, o, si no, «pedir en
recepción/conserjería por el Chief Engineer / Director of Facilities / In-House Maintenance» (en
plazas y comerciales, el Property / Facilities Manager). Los dos formatos los lee la tarjeta del CRM
(`tests/test_crm_brief_contract.py`).

### Rutas de inspección

```bash
python -m lead_engine route export_crm.csv --date 2026-11-10 --inspectors "Tomás,Mateo" --start "25.7617,-80.1918"
python -m lead_engine route out/<lote>/leads.jsonl --inspectors "Tomás,Mateo"
```

Entrada: `leads.jsonl` de un lote o un export del CRM (CSV/JSON con `company_name`, `address` y,
opcionales, `inspection_at`, `inspector`, `phone`, `lat`/`lng`; las direcciones sin coordenadas se
geocodifican gratis con el Census). Cada visita recibe su zona (Brickell/Downtown, Miami Beach,
Sunny Isles/Aventura, Coral Gables/Grove, Doral, Fort Lauderdale…). Las visitas sin inspector se
reparten por zonas enteras al inspector con citas más cercanas ese día. Las citas agendadas mantienen
su hora (y se avisa si el traslado no llega); las visitas libres se insertan donde menos kilómetros
suman sin atrasar ninguna cita, prefiriendo terminar antes el día (los huecos de la mañana se
llenan). Salida: `ruta_<día>.md` (para WhatsApp, con links de Google Maps), `.csv` y `.json`.

Prueba real con 8 edificios (2 citas): 81 km en total, cada inspector en su zona y la mañana de Mateo
ocupada antes de su cita de las 14:00; antes del ajuste
de reparto por citas eran 132 km cruzando Miami.

### Segmentos de negocio

Cada lead tiene un `vertical` — `community` (Gran Comunidad), `plaza` (Shopping/Plaza Comercial),
`commercial` (Edificio Comercial: oficinas, hoteles) o `clinic` (Clínica/Hospital) — tomado del uso
de suelo oficial del catastro, la fuente (DBPR → comunidad, NPI → clínica), la licencia de alojamiento,
los tipos de Google Places o el nombre. Cambia el gancho, el prompt de Claude y la ficha:

| segmento | a quién | superficies | ángulo |
| --- | --- | --- | --- |
| Gran Comunidad | Property Manager (LCAM), junta | fachada, balcones, garaje, áreas comunes | salitre, moho, recertificación / Milestone, asamblea |
| Shopping/Plaza | PM del centro comercial | aceras, frentes, parking (aceite/grasa), dumpster pads | tráfico peatonal, trabajo nocturno sin cerrar locales |
| Edificio Comercial | PM o Chief Engineer | fachada y vidrios, entrada, garaje, plaza de acceso | imagen corporativa, fuera de horario |
| Clínica/Hospital | administrador/a o Facilities | entradas de pacientes, rampas, aceras, parking | imagen sanitaria, caídas, normativa |

**El CRM hoy solo acepta `segment` = `Dolor Urgente` | `Gran Comunidad`** (`src/lib/lead-import.ts`
en `main`). Por eso `segment` sigue siendo el de dolor y el segmento de negocio va en la ficha
(«Ficha: Clínica/Hospital · …»). Cuando el CRM agregue los cuatro nombres, `CRM_SEGMENT_MODE=vertical`
los manda como `segment`.

### Catastro oficial y Milestone Inspection

`appraiser.py` consulta la capa pública `PaGISView` del Property Appraiser de Miami-Dade (verificada en
vivo, sin clave) por número + calle + ZIP. Un condominio son N folios de unidad: se agregan (unidades,
año más frecuente, ft² y valor tasado sumados, pisos leídos de los números de unidad). Otra propiedad
es un folio con pisos, ft² construidos, lote, año, propietario, dirección postal (con «C/O» de la
administradora) y valor tasado. El dato oficial pisa la estimación de OSM cuando existe. Cobertura:
Miami-Dade (Broward no expone una capa equivalente accesible).

Ventana Milestone: condominios/co-ops de 3+ pisos tienen inspección estructural a los 30 años (25 a
<3 millas de la costa, FS 553.899 / SB 4-D) y cada 10 después; además el condado recertifica a los
30/40/50… Si la edad está a ±1 año de uno de esos hitos, se genera una señal de dolor de máxima
prioridad. Para plazas, oficinas, hoteles y hospitales solo cuenta la recertificación del condado.

```bash
python -m lead_engine run --seed pa --vertical plaza --limit 20 --dry-run        # Dolphin Mall, CocoWalk…
python -m lead_engine run --seed pa --vertical commercial --limit 20 --dry-run   # torres de oficinas, hoteles
python -m lead_engine run --seed pa --vertical clinic --limit 20 --dry-run       # hospitales
```

### Semilla catastral en el este de Broward

`--seed pa --vertical plaza|commercial|clinic` (y las porciones `pa` del mix de `daily`) siembra
Miami-Dade desde su Property Appraiser y, para las ciudades del corredor que estén en la zona
(Hallandale, Hollywood, Fort Lauderdale), el tax roll de Broward con los mismos códigos de uso del
DOR (plazas 11-16, comerciales 17-19 y hoteles 39, hospitales 73/85), un registro por folio, con
propietario, «%» (administradora), año, ft² construidos y lote. Los dos condados se unen en un solo
ranking por superficie. Nov-2026: 209 plazas de 20.000+ ft² en el corredor en ~1 s (Galleria Mall,
Sheridan Plaza, Village at Gulfstream Park, Beach Place…). Sin Google Places, OSM ni búsqueda
externa no hay teléfono para casi ninguna: quedan en `needs_phone.csv` para `enrich-phones`.

### Operadores de plazas comerciales (`commercial_mgmt`)

Las plazas están a nombre de sociedades de un solo inmueble («SFA ATLANTIS ASSOC LP»), así que el
operador se reconoce en el propio catastro: nombre del propietario, «%»/C/O y, sobre todo, la
dirección postal del propietario, porque todo el portafolio de un operador recibe la boleta en la
misma oficina (500 N Broadway, Jericho → Kimco; 17100 Collins Ave → RK Centers; 7978 Cooper Creek
Blvd → Benderson). `lead_engine/data/commercial_operators.json` se armó y verificó en vivo el
9-nov-2026 sobre las 1.753 plazas y edificios comerciales de la zona: 16 operadores cubren 107
inmuebles; dominio y teléfono leídos del sitio oficial de cada uno con el mismo rastreador del motor
(los sitios que responden 403 a clientes automáticos —Kimco, Hines— no se forzaron y quedan sin
teléfono). Solo se marcan los números de oficinas de Florida (`phone_scope: florida`: RK Centers,
Dacra, Regency Boca Raton, Benderson, Stiles, Aventura Mall —esta es la línea de concierge—): 54
inmuebles. Las sedes fuera del estado (Thor, Federal Realty, Procacci, Continental) solo nombran a
la administradora.

Con `ALLOW_MGMT_FIRM_PHONE=true` la cascada usa ese teléfono como «central administradora» (último
recurso, como en condominios) y `portfolio_consolidation` deja un lead por operador con las demás
plazas listadas para cotizar en el mismo llamado; el ticket de cada hermana sale de la superficie
construida y del lote.

### Hoteles (DBPR Lodging) y clínicas (NPI Registry)

`dbpr-sync` también carga `hrlodge1.csv` / `hrlodge2.csv` (~44.000 licencias de alojamiento): para
hoteles, moteles y apart-hoteles da habitaciones, licenciatario, administrador y el teléfono del
front desk. Las licencias de grupos de alquiler dentro de condominios se cuentan, pero nunca se usan
como teléfono del edificio.

`--seed npi` busca clínicas y hospitales en las ciudades objetivo en el NPI Registry federal
(taxonomías Hospital, Clinic/Center, Ambulatory Surgical, Urgent Care), salteando consultorios dentro
de suites y priorizando cargos de facilities/administración sobre facturación. Deja el Authorized
Official como contacto con su teléfono directo. Las clínicas que llegan por otra fuente se cruzan
con NPI por ZIP y nombre.

### Contactos múltiples, plan B y horario

El extractor web captura todas las personas con cargo — PM/LCAM, **Chief Engineer, Director of
Facilities, Maintenance Supervisor, Assistant Property Manager**, junta — y el horario de oficina
(texto o JSON-LD `openingHours`). El principal es el PM; el plan B, facilities o el asistente:
`Plan B: Chief Engineer Carlos Ruiz · oficina L–V 9–17`.

### Dominio de la administradora

Si el lead tiene administradora pero no email directo, `mgmt_domains.py` resuelve su dominio
corporativo (catálogo curado: FirstService, Castle, KW, AKAM, Campbell, RealManage, Associa,
Continental, Miami Management, Seacrest, Leland, Sentry, Vesta; ampliable con `MGMT_DOMAINS_FILE`;
o el sitio oficial vía búsqueda API), verifica MX y, si su página de contacto publica un buzón de
ese dominio, lo agrega: `administradora: castlegroup.com (MX ok) · info@castlegroup.com`. Nunca se
inventan direcciones personales.

### Modo semilla DBPR (`--seed dbpr`)

```bash
python -m lead_engine run --seed dbpr --strategy onsite --limit 20 --dry-run
python -m lead_engine run --seed dbpr --strategy portfolio --min-units 80 --limit 30 --dry-run
```

Descubre edificios desde el padrón oficial (no desde Google): filtra por `TARGET_CITIES` (ciudad
postal del DBPR o alias de barrio: «Brickell Miami» → 33129/33130/33131, «Sunny Isles Beach» → 33160…),
`MIN_UNITS` (50) y tipo de administración — `onsite` cuando el «C/O» es la oficina del edificio o
la dirección del propio edificio, `portfolio` cuando es una administradora externa — y ordena
primero los de `PRIORITY_UNITS`+ (80). Es la fuente por defecto si no hay `GOOGLE_PLACES_API_KEY`.

Para cada edificio busca el teléfono, en este orden: Google Places **por nombre + dirección**
(una consulta puntual por edificio, si hay clave), el padrón DBPR de alojamiento (licencia de
hotel/apartamentos del edificio → *front desk*; licencia CNDO solo si el titular es la propia
asociación, nunca la de un propietario), etiquetas de OpenStreetMap en esa dirección, el sitio
oficial vía búsqueda API + rastreo y, solo con `ALLOW_MGMT_FIRM_PHONE=true`, la central de la
administradora (perfil del tenant o su sitio oficial, prefiriendo números del sur de Florida),
etiquetada «central administradora». Como el CRM acepta cada teléfono una sola vez, la central
entra en un único lead por firma; los demás van a `needs_phone.csv`. Los que pasan todos los filtros pero no tienen
teléfono quedan en `out/<lote>/needs_phone.csv` (edificio, dirección, unidades, asociación,
administradora) para buscarlos a mano.

Con el padrón de nov-2026 y los filtros por defecto: 442 edificios `onsite` (366 con 80+ unidades)
y 322 `portfolio` en las ciudades objetivo.

### Licencias CAM y efecto multi-propiedad

`dbpr-sync` también descarga `lic38cam.csv` (19.575 CAM personas, 2.070 firmas CAB). Cada
contacto y administradora se verifica contra ese padrón: «LCAM activa CAM12345» / «firma CAM
licenciada CAB2893» en la ficha (`≈` cuando el cruce es por variante de escritura). Un contacto
con licencia activa pasa a cargo «LCAM».

Las rutas «C/O» del padrón de condominios se agrupan por firma (por su licencia CAB, así
«First Service Residential» y «Firstservice Residential» son la misma) o por persona. Si la
administradora del lead maneja otros condominios en `CLUSTER_COUNTIES`, la ficha y el gancho lo
dicen y proponen una inspección combinada con los 2 edificios hermanos más cercanos (mismo ZIP,
misma ciudad, luego más grandes):

```
Edificio administrado por First Service Residential: pedir por el/la CAM asignado/a al edificio y ofrecer
inspección gratis de fachada expuesta al salitre. Proponer inspección combinada con Sunset Harbour North y
300 Collins (misma administradora, 81 condominios en la zona).
```

### Señales municipales (open data)

| capa (ArcGIS REST, pública) | qué detecta | peso |
| --- | --- | --- |
| City of Miami · 40_Year_Recertification | recertificación 40 años **pendiente** | 6 |
| Miami-Dade · Open_Building_Violations | caso abierto de **estructura insegura** / violación de edificación | 5 / 3 |
| Miami-Dade · CodeComplianceViolation_Open_View | caso de código abierto por mantenimiento | 4 |
| Miami-Dade · miamidade_permit_data | permiso reciente (24 meses) de pintura, impermeabilización, restauración, fachada, techo, pool deck | 2,5 |

Se consultan por número + calle; el de recertificación además exige ciudad postal Miami y
cercanía a las coordenadas. Una señal municipal pesa más que cualquier reseña y encabeza el
gancho («Registro municipal: …»). Si una capa no responde, se apaga por el resto de la corrida
sin cortar nada. Miami Beach, Broward y Fort Lauderdale no exponen capas accesibles desde
nuestra red (nov-2026): esos leads simplemente no tienen señal municipal.

### Ticket anual estimado

`valuation.py` estima el rango anual del contrato (fachada por perímetro×altura, ×1,5 con
salitre y ×1,3 con acceso por cuerdas desde 8 pisos; caminos; garaje; pool deck; canchas;
garita) con pisos/huella de OSM o, si faltan, derivados de las unidades del DBPR. Va a la ficha
(`est. US$ 36k–65k/año`) y desempata el `lead_score`: entre leads igual de calificados, la
setter llama primero al contrato más grande. Las tarifas están en una tabla al inicio del
módulo para ajustarlas con las primeras cotizaciones reales.

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
costo estimado de APIs (total y por lead, precios ajustables con `PRICE_<API>`), más % con señal municipal, % contacto con LCAM activa, % firma CAB licenciada, % con cluster multi-propiedad y la suma del ticket anual estimado del lote.

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

## Panel del lote (`index.html`)

Cada lote con PDFs (`run --pdf`, `daily`, `enrich-phones --pdf`) deja `out/<lote>/index.html`, una
página autocontenida (sin CDN ni red) con las métricas del lote y una tabla que se filtra y ordena:
score, ticket anual, vertical, señales, teléfono, edificios consolidados, enlaces al PDF en inglés y
a la hoja de campo, y botones para copiar el email y el WhatsApp del outreach pack. `portfolios
--pdf` deja el suyo con el ranking y el dossier de cada administradora. Los enlaces son relativos:
la carpeta se puede comprimir y compartir tal cual.

## Piloto automático: qué garantiza

- **Nada pagado se pierde.** Un edificio queda "consumido" en la memoria (`data/state.db`) solo cuando
  el CRM confirma el lote de una corrida con `--commit`. Las corridas de prueba (sin `--commit`, o
  dry-run al CRM) no consumen nada. Si el CRM falla después de sus reintentos, el lote queda en la
  cola `pending_batches` (con `crm_payload.json` y `leads.jsonl` en disco) y `daily` lo reenvía al
  arrancar; mientras el CRM siga fallando, no arma un lote nuevo. `import <lote>/crm_payload.json
  --commit` hace lo mismo a mano.
- **Una corrida a la vez.** `data/engine.lock` (flock, se libera solo aunque el proceso muera):
  una segunda corrida sale con código 3 y el mensaje de quién la tiene.
- **Fallas externas acotadas.** Claude: 60 s por llamada y 2 reintentos (`ANTHROPIC_TIMEOUT_S`);
  si no responde, el lead sigue con reglas. Places: una búsqueda que falla se saltea; tres seguidas
  cortan la recolección con lo encontrado. Overpass, catastros y capas municipales se apagan solos.
- **Renderizado JS** (`RENDER_JS=auto`, por defecto): si el paquete de Playwright busca otra versión
  de Chromium que la instalada, usa cualquier Chromium presente en la máquina.
- **Emails verificados de verdad.** Si el DNS del sistema no responde MX (pasa en algunos entornos),
  la verificación va por DNS-over-HTTPS; con `EMAIL_REQUIRE_MX=true` (por defecto) se descartan los
  dominios sin MX. `preflight` muestra qué resolvedor se usa, el lock y la cola pendiente.
- La carpeta `data/` (memoria, padrones, lock, caché) tiene que vivir en un disco persistente.

## Pre-vuelo (`preflight`)

```bash
python -m lead_engine --tenant mb_softwash preflight            # con red
python -m lead_engine --tenant mb_softwash preflight --offline  # solo configuración
```

Tabla OK / PENDIENTE / ERROR de: claves (`GOOGLE_PLACES_API_KEY`, `ANTHROPIC_API_KEY`,
`CRM_WEBHOOK_SECRET`, búsqueda externa, `SLACK_WEBHOOK_URL`), perfil (agenda, documentos, teléfono,
email, web), padrones locales (`data/dbpr.db`: condominios, CAM, alojamiento) y conectividad en
paralelo con timeouts cortos (catastros de Miami-Dade y Broward, recertificación de Miami, permisos,
Census, NPI, Overpass y sus mirrors). Con las claves cargadas hace un ping liviano a Google Places
(Text Search pidiendo solo el id), Anthropic (lista de modelos, sin tokens) y `/api/public/health`
del CRM (estado y Pool). No escribe nada; sale con código 1 solo si algo configurado falla.

## Audiencias para Ads (`export-ads`)

```bash
python -m lead_engine export-ads out/<lote>/leads.jsonl   # → out/<lote>/ads_audience.csv
python -m lead_engine export-ads                          # padrones de la zona → out/ads/ads_audience.csv
python -m lead_engine export-ads --no-include-npi         # sin consultar el NPI Registry
```

Columnas `email, phone, fn, ln, ct, st, zip, country, company, value`, normalizadas como pide Meta
antes de hashear (minúsculas, nombres sin signos, ciudad sin espacios, estado y país de 2 letras,
ZIP de 5 dígitos, teléfono E.164); Meta, Google y LinkedIn hashean el archivo en su propia carga.
`value` es el punto medio del ticket anual estimado. Solo registros dentro de la zona de servicio
del perfil; se descartan los que no tienen ningún identificador y se unen duplicados (mismo email,
teléfono o nombre + ZIP). Desde un lote salen el contacto principal (email verificado y teléfono) y
los demás contactos con nombre. Desde los padrones locales de la zona se suman:

- condominios: la persona registrada en el «C/O» con el ZIP del edificio (solo nombre + ubicación);
- DBPR Lodging: hoteles, moteles, edificios de apartamentos y apart-hoteles con licencia activa y
  teléfono, y las licencias de condominio a nombre de la propia asociación (nunca las de un
  propietario particular, ni viviendas `DWEL` o B&B);
- NPI Registry (`--include-npi`, por defecto): clínicas y hospitales de las ciudades y ZIPs de la
  zona con el *authorized official* (nombre, apellido y teléfono directo);
- una fila por administradora / operador comercial con su teléfono publicado y `company_website`
  (columna extra para LinkedIn). El conmutador de la firma no se copia en las filas de personas.

Se deduplica por email, teléfono, nombre + ZIP y empresa + dirección. Nov-2026, sin Google Places:
4.423 filas, 4.355 con teléfono E.164 (NPI 2.121, Lodging 2.224, firmas y operadores 12, contactos
de condominio 66).

## Contrato con la tarjeta del CRM

`sales_strategy` va en dos líneas, como lo lee `src/lib/lead-brief.ts` del CRM: el gancho (con
«Zona: …» al final de la misma línea) y debajo «Ficha: …». La Ficha nunca se corta a mitad de un
dato: tiene prioridad dentro de los 2000 caracteres y se recorta el gancho (conservando entera la
frase de Zona). `tests/test_crm_brief_contract.py` corre el parser del CRM (copia en
`tests/contract/lead-brief.ts`, con Node) sobre salidas reales del motor —Zona, Plan B, otros
tel., consolidación de central de administradora, plazas— y verifica que vuelva cada dato. Si el
CRM está clonado al lado, el test también avisa cuando su `lead-brief.ts` cambió.

Hasta nov-2026 el motor mandaba todo en una sola línea; el parser del CRM también acepta ese formato
(«… Ficha: …» después de un espacio), así que los leads viejos muestran la Ficha sin backfill.

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
