// Runs the CRM's own parser (vendored copy of mbguiaclientes src/lib/lead-brief.ts) over a JSON
// array of sales_strategy strings read from stdin; prints the parsed briefs as JSON.
import { parseLeadBrief } from "./lead-brief.ts";

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => (input += c));
process.stdin.on("end", () => {
  const texts = JSON.parse(input);
  process.stdout.write(JSON.stringify(texts.map((t) => parseLeadBrief(t))));
});
