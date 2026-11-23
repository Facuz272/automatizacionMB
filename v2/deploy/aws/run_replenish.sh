#!/bin/sh
# Lo llama lead-engine-replenish.service. REPLENISH_COMMIT=true en .env escribe en el CRM; si no, dry run.
set -eu
cd /opt/lead-engine/v2
if [ "${REPLENISH_COMMIT:-false}" = "true" ]; then
  exec /opt/lead-engine/venv/bin/python -m lead_engine auto-replenish --commit
fi
exec /opt/lead-engine/venv/bin/python -m lead_engine auto-replenish
