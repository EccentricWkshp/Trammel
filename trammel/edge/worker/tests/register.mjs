// Run the Worker's modules under Node: "cloudflare:sockets" resolves to a stub
// whose connect() the tests supply (globalThis.__connect).
import { registerHooks } from "node:module";

registerHooks({
  resolve(specifier, context, next) {
    if (specifier === "cloudflare:sockets") {
      return { url: new URL("./sockets-stub.mjs", import.meta.url).href, shortCircuit: true };
    }
    return next(specifier, context);
  },
});
