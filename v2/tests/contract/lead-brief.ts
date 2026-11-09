/**
 * Reads the setter text the Lead Engine v2 puts in `sales_strategy`
 * (Facuz272/automatizacionMB, v2/lead_engine/models.py `crm_payload` + `brief()`,
 * competitor_intel.py `hook_line`):
 *
 *   <hook, 1–2 lines>[ Zona: <where> dicen que <weakness> → argumento: <argument>.]
 *   Ficha: <vertical> · <fact> · … · Plan B: <title> <name> [phone] · oficina <hours>
 *          · otros tel.: <label> <number>; <label> <number> · est. US$ 4.5k–7.2k/año · score 72/100
 *
 * "Zona:" is appended to the hook on the same line; "Ficha:" is its own line. Older leads
 * have neither, so they come back as a plain hook. Pure and total: it never throws.
 */

export type BriefPhone = { label: string; number: string };
export type LeadBrief = {
  /** The hook without the Zona/Ficha metadata. */
  hook: string;
  zona: { text: string; finding: string; argument: string | null } | null;
  ficha: {
    raw: string;
    vertical: string | null;
    estimate: string | null;
    score: number | null;
    planB: string | null;
    /** Phone at the end of the Plan B fact, when the engine had one. */
    planBPhone: string | null;
    officeHours: string | null;
    otherPhones: BriefPhone[];
    floors: number | null;
    units: number | null;
    yearBuilt: number | null;
    /** "<who> administra N condominios en la zona (incl. …)" */
    portfolio: { text: string; count: number } | null;
    /** Every other fact, in the engine's order, for the quick-read chips. */
    facts: string[];
  } | null;
};

const VERTICALS = ["Gran Comunidad", "Shopping/Plaza Comercial", "Edificio Comercial", "Clínica/Hospital"];
const PHONE_AT_END = /^(.*?)[\s:=-]*(\+?\(?\d[\d\s().-]{6,}\d)$/;

function splitPhone(text: string): BriefPhone | null {
  const m = PHONE_AT_END.exec(text.trim());
  if (!m) return null;
  const digits = m[2]!.replace(/\D/g, "");
  if (digits.length < 10 || digits.length > 15) return null;
  return { label: m[1]!.trim().replace(/[:\s]+$/, "") || "otro", number: m[2]!.trim() };
}

function parseZona(text: string) {
  const body = text.replace(/^Zona:\s*/, "").trim();
  const [finding, argument] = body.split(/\s*→\s*argumento:\s*/);
  const strip = (s: string | undefined) => (s ?? "").trim().replace(/\.$/, "").trim();
  return { text: body, finding: strip(finding), argument: argument === undefined ? null : strip(argument) || null };
}

function parseFicha(line: string): NonNullable<LeadBrief["ficha"]> {
  const raw = line.replace(/^Ficha:\s*/, "").trim();
  // " · " separates facts; the "administradora: x (MX ok) · mail@x" fact carries its email after one.
  const parts: string[] = [];
  for (const p of raw.split(" · ").map((x) => x.trim()).filter(Boolean)) {
    if (/^[^\s@]+@[^\s@]+$/.test(p) && parts.length && parts[parts.length - 1]!.startsWith("administradora:")) parts[parts.length - 1] += ` · ${p}`;
    else parts.push(p);
  }
  const out: NonNullable<LeadBrief["ficha"]> = {
    raw, vertical: null, estimate: null, score: null, planB: null, planBPhone: null, officeHours: null, otherPhones: [],
    floors: null, units: null, yearBuilt: null, portfolio: null, facts: [],
  };
  for (const p of parts) {
    let m: RegExpExecArray | null;
    if (!out.vertical && VERTICALS.includes(p)) out.vertical = p;
    else if (/^est\. US\$/.test(p)) out.estimate = p;
    else if ((m = /^score (\d{1,3})\/100$/.exec(p))) out.score = Number(m[1]);
    else if (/^Plan B:/.test(p)) {
      const text = p.replace(/^Plan B:\s*/, "");
      const phone = splitPhone(text);
      out.planB = phone ? phone.label : text;
      out.planBPhone = phone?.number ?? null;
    }
    else if (/^oficina /.test(p)) out.officeHours = p.replace(/^oficina\s+/, "");
    else if (/^otros tel\.:/.test(p)) {
      for (const item of p.replace(/^otros tel\.:\s*/, "").split(";")) {
        const phone = splitPhone(item);
        if (phone) out.otherPhones.push(phone);
      }
    } else {
      if ((m = /(\d+) pisos\b/.exec(p))) out.floors = Number(m[1]);
      if ((m = /(\d+) unidades\b/.exec(p))) out.units = Number(m[1]);
      if ((m = /^construido (\d{4})$/.exec(p))) out.yearBuilt = Number(m[1]);
      if ((m = /administra (\d+) condominios/.exec(p))) out.portfolio = { text: p, count: Number(m[1]) };
      out.facts.push(p);
    }
  }
  return out;
}

export function parseLeadBrief(text: string | null | undefined): LeadBrief {
  const lines = (text ?? "").split(/\r?\n/);
  let ficha: LeadBrief["ficha"] = null;
  let zona: LeadBrief["zona"] = null;
  const hook: string[] = [];
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) continue;
    if (!ficha && /^Ficha:\s*\S/.test(line)) { ficha = parseFicha(line); continue; }
    // "Zona:" may start its own line or follow the hook on the same line (" … Zona: …").
    const at = zona ? -1 : line.search(/(^|\s)Zona:\s+\S/);
    if (at >= 0) {
      const start = line.indexOf("Zona:", at);
      zona = parseZona(line.slice(start));
      const before = line.slice(0, start).trim();
      if (before) hook.push(before);
      continue;
    }
    hook.push(line);
  }
  return { hook: hook.join("\n"), zona, ficha };
}

/** tel: href for a Ficha phone (US numbers get +1). */
export function phoneE164(number: string): string | null {
  const d = number.replace(/\D/g, "");
  if (d.length === 10) return `+1${d}`;
  if (d.length === 11 && d.startsWith("1")) return `+${d}`;
  if (d.length >= 11 && d.length <= 15) return `+${d}`;
  return null;
}
