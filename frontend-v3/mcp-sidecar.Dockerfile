# MCP sidecar — standalone Streamable HTTP host for the vendored
# frontend-v3/src/mcp tool surface (deploy/plans/MCP_SIDECAR_PLAN.md).
#
# Build stage bundles the TypeScript sources into one ESM file with the
# project lockfile's own esbuild (vite dep) — the runtime image carries
# no node_modules.

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
