***REMOVED*** MCP sidecar — standalone Streamable HTTP host for the vendored
***REMOVED*** frontend-v3/src/mcp tool surface.
***REMOVED***
***REMOVED*** Build stage bundles the TypeScript sources into one ESM file with the
***REMOVED*** project lockfile's own esbuild (vite dep) — the runtime image carries
***REMOVED*** no node_modules.

FROM node:22-alpine AS build
WORKDIR /app
COPY frontend-v3/package.json frontend-v3/package-lock.json ./
RUN npm ci
COPY frontend-v3/src/mcp ./src/mcp
RUN ./node_modules/.bin/esbuild src/mcp/standalone-server.ts \
      --bundle --platform=node --format=esm --outfile=server.mjs

FROM node:22-alpine
WORKDIR /app
COPY --from=build --chown=node:node /app/server.mjs ./server.mjs
USER node
EXPOSE 8100
CMD ["node", "server.mjs"]
