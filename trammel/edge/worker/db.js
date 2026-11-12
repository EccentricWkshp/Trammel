// The edge portal's D1 database (SPEC §12, 0.9.x): what Trammel published for
// customers to see, the inbox of what they did, and sign-in state. The schema is
// created and upgraded by the Worker itself, the first time an isolate needs it.

export const SCHEMA_VERSION = 1;

const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)`,
  // Published records: the customer they belong to, their job, a sort key, and
  // display-ready data as JSON (Trammel formats every date and amount).
  `CREATE TABLE IF NOT EXISTS records (
     type TEXT NOT NULL, id TEXT NOT NULL, customer TEXT, job TEXT, sort TEXT,
     data TEXT NOT NULL, PRIMARY KEY (type, id))`,
  `CREATE INDEX IF NOT EXISTS records_customer ON records (customer, type, sort)`,
  `CREATE INDEX IF NOT EXISTS records_job ON records (job, type, sort)`,
  // What customers did, for Trammel to pull and apply, in order.
  `CREATE TABLE IF NOT EXISTS inbox (
     seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, at TEXT NOT NULL,
     ip TEXT, customer TEXT, data TEXT NOT NULL)`,
  // Signed-in browsers: only a hash of the cookie's token is kept. A session
  // without a customer is an address that has signed in and must choose one.
  `CREATE TABLE IF NOT EXISTS sessions (
     id TEXT PRIMARY KEY, customer TEXT, email TEXT, name TEXT, given_by TEXT,
     choices TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)`,
  // Emailed sign-in codes, kept as keyed hashes.
  `CREATE TABLE IF NOT EXISTS codes (
     id TEXT PRIMARY KEY, email TEXT NOT NULL, code_hash TEXT NOT NULL,
     created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT,
     tries INTEGER NOT NULL DEFAULT 0)`,
  `CREATE INDEX IF NOT EXISTS codes_email ON codes (email)`,
  // Sign-in requests and tries, for the rate limits.
  `CREATE TABLE IF NOT EXISTS attempts (
     at TEXT NOT NULL, kind TEXT NOT NULL, email TEXT, ip_key TEXT,
     ok INTEGER NOT NULL DEFAULT 0)`,
  `CREATE INDEX IF NOT EXISTS attempts_at ON attempts (at)`,
  // Codes from staff used here, so one can't sign in twice before Trammel hears.
  `CREATE TABLE IF NOT EXISTS used_access_codes (hash TEXT PRIMARY KEY, at TEXT NOT NULL)`,
];

let ready = false;

/** Create or upgrade the schema, once per isolate. */
export async function ensureSchema(db) {
  if (ready) return;
  let version = 0;
  try {
    const row = await db.prepare("SELECT value FROM meta WHERE key = 'schema'").first();
    version = row ? Number(row.value) : 0;
  } catch {
    version = 0; // no meta table yet
  }
  if (version < SCHEMA_VERSION) {
    await db.batch(SCHEMA.map((sql) => db.prepare(sql)));
    await db
      .prepare("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)")
      .bind(String(SCHEMA_VERSION))
      .run();
  }
  ready = true;
}

/** For tests: forget that this isolate has checked the schema. */
export function _resetSchemaFlag() {
  ready = false;
}

export function now() {
  return new Date().toISOString();
}

export function later(ms) {
  return new Date(Date.now() + ms).toISOString();
}

export function earlier(ms) {
  return new Date(Date.now() - ms).toISOString();
}

/** One published record, its data parsed; null if missing. */
export async function record(db, type, id) {
  const row = await db
    .prepare("SELECT type, id, customer, job, sort, data FROM records WHERE type = ? AND id = ?")
    .bind(type, id)
    .first();
  return row ? { ...row, data: JSON.parse(row.data) } : null;
}

/** A customer's records of one type (or a job's, with `job`), newest first. */
export async function records(db, type, { customer = null, job = null } = {}) {
  let sql = "SELECT type, id, customer, job, sort, data FROM records WHERE type = ?";
  const args = [type];
  if (customer !== null) {
    sql += " AND customer = ?";
    args.push(customer);
  }
  if (job !== null) {
    sql += " AND job = ?";
    args.push(job);
  }
  sql += " ORDER BY sort DESC";
  const { results } = await db.prepare(sql).bind(...args).all();
  return (results || []).map((row) => ({ ...row, data: JSON.parse(row.data) }));
}

/** Add to the inbox for Trammel. */
export async function inbox(db, kind, data, { ip = null, customer = null } = {}) {
  await db
    .prepare("INSERT INTO inbox (kind, at, ip, customer, data) VALUES (?, ?, ?, ?, ?)")
    .bind(kind, now(), ip, customer, JSON.stringify(data))
    .run();
}
