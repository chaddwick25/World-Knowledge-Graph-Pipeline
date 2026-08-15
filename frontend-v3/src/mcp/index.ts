import path from "node:path";
import { fileURLToPath } from "node:url";
import { type Plugin } from "vite";
import { createChannel } from "./channel/server";
import { createMcpServer } from "./mcp-server";
import { createMcpMiddleware } from "./vite-middleware";
import type { ClientFunctions } from "./client-functions";
import { createMcpHandlers } from "./mcp-handlers";

export interface VueMcpOptions {
  /**
   * Glob or regex pattern matching the app entry file where the client
   * runtime will be injected.
   *
   * @default /\/src\/main\.\w+$/
   */
  appendTo?: string | RegExp;
}

const _dirname = path.dirname(fileURLToPath(import.meta.url));

export default function VueMcpPlugin(options: VueMcpOptions = {}): Plugin {
  const appendTo = options.appendTo
    ? typeof options.appendTo === "string"
      ? new RegExp(options.appendTo.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
      : options.appendTo
    : /\/src\/main\.\w+$/;

  return {
    name: "vite-plugin-vue-mcp",
    apply: "serve",

    transform(code, id) {
      const normalizedId = path.normalize(id).replace(/\\/g, "/");
      if (appendTo.test(normalizedId)) {
        // Use .ts extension — we vendor the TypeScript source directly
        // (no build step). Vite's dev server resolves .ts imports natively.
        const clientRuntimePath = path.resolve(_dirname, "client-runtime.ts");
        const rel = path
          .relative(path.dirname(id), clientRuntimePath)
          .replace(/\\/g, "/");
        return `import '${rel.startsWith(".") ? rel : "./" + rel}';\n${code}`;
      }
    },

    configureServer(server) {
      const callClient = createChannel<ClientFunctions>(server.ws, "vue-mcp");
      const mcpHandlers = createMcpHandlers(callClient);

      const middleware = createMcpMiddleware(() =>
        createMcpServer(mcpHandlers),
      );
      server.middlewares.use(middleware);
      console.log("[MCP] Streamable HTTP endpoint mounted at /__mcp");
    },
  };
}
