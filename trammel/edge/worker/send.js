// Customers sending files from the edge portal (FR-PRT-7): stored in R2 and
// passed to Trammel, which takes them in for review (checking each file's
// kind itself) and publishes how they got on.

import { inbox } from "./db.js";
import { html } from "./html.js";

function safeName(name) {
  const base = String(name || "file").split(/[\\/]/).pop().slice(0, 200);
  return base.replace(/[^A-Za-z0-9._ -]/g, "_") || "file";
}

/** The "send us files" form and what they've sent, for a page. */
export function sendSection(site, sent, jobId = null) {
  const up = site.uploads;
  const list = sent.length
    ? html`<ul class="plain portal-sent">${sent.map(
        (s) => html`<li><strong>${s.data.name}</strong> <span class="muted small">${s.data.at || ""}${s.data.at ? " · " : ""}${s.data.status}</span>${s.data.reason ? html`<br><span class="muted small">${s.data.reason}</span>` : ""}</li>`,
      )}</ul>`
    : "";
  if (!up || !up.enabled) {
    return sent.length ? html`<section><h2>Files you've sent</h2>${list}</section>` : "";
  }
  return html`<section class="card" aria-labelledby="send-heading">
  <h2 id="send-heading">Send us files</h2>
  <p class="muted small">Drawings, sketches or reference photos: ${up.kinds_label}, up to ${up.limit_mb} MB each, ${up.per_send} at a time. We'll look at them before they go with your ${jobId ? "job" : "account"}.</p>
  <form method="post" action="/portal/send/" enctype="multipart/form-data" class="stack">
    ${jobId ? html`<input type="hidden" name="job" value="${jobId}">` : ""}
    <div class="field"><label for="send-files">Files</label>
      <input type="file" id="send-files" name="files" multiple required${up.accept ? html` accept="${up.accept}"` : ""}></div>
    <div class="field"><label for="send-note">A note about them (optional)</label>
      <textarea id="send-note" name="note" rows="2" maxlength="2000"></textarea></div>
    <div class="actions"><button type="submit">Send</button></div>
  </form>
  ${sent.length ? html`<h3>Files you've sent</h3>${list}` : ""}
</section>`;
}

/** Store what was sent and pass it on. Returns a problem (a notice's key,
 * html.js), or "". */
export async function takeFiles(env, site, visitor, form, ip, jobOk) {
  const up = site.uploads;
  if (!up || !up.enabled) return "send-off";
  const job = String(form.get("job") || "") || null;
  if (job && !(await jobOk(job))) return "send-off";
  const files = form.getAll("files").filter((f) => f && typeof f === "object" && f.size > 0);
  if (!files.length) return "send-none";
  if (files.length > up.per_send) return "send-too-many";
  const limit = up.limit_mb * 1024 * 1024;
  if (files.some((f) => f.size > limit)) return "send-too-big";
  const note = String(form.get("note") || "").trim().slice(0, 2000);
  for (const file of files) {
    const id = crypto.randomUUID();
    const name = safeName(file.name);
    const key = `upload/${id}/${name}`;
    await env.FILES.put(key, await file.arrayBuffer(), {
      httpMetadata: { contentType: file.type || "application/octet-stream" },
    });
    await env.DB.prepare(
      "INSERT OR REPLACE INTO records (type, id, customer, job, sort, data) VALUES ('upload', ?, ?, ?, ?, ?)",
    )
      .bind(id, visitor.customerId, job, new Date().toISOString(), JSON.stringify({ name, status: "Sending to us…" }))
      .run();
    await inbox(
      env.DB,
      "upload",
      { id, key, name, size: file.size, type: file.type || "", note, job, sent_by: visitor.name },
      { ip, customer: visitor.customerId },
    );
  }
  return "";
}
