export function connect(address, options) {
  if (!globalThis.__connect) throw new Error("No mail server in this test.");
  return globalThis.__connect(address, options);
}
