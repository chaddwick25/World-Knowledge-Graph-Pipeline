// MCP smoke test — exercises the WorldKG tools exactly like Goose would.
// Usage: node scripts/mcp-smoke-test.mjs [port]
import { Client } from "@modelcontextprotocol/sdk/client/index.js";

const port = process.argv[2] || "5174";
const url = `http://localhost:${port}/__mcp`;

const client = new Client({ name: "worldkg-mcp-smoke", version: "0.1.0" });

async function call(name, args) {
  const res = await client.callTool({ name, arguments: args });
  if (res.isError) {
    console.log(`\n❌ ${name}: ${res.content?.[0]?.text || "tool error"}`);
    return null;
  }
  const text = res.content?.[0]?.text;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

const { StreamableHTTPClientTransport } = await import(
  "@modelcontextprotocol/sdk/client/streamableHttp.js"
);

try {
  await client.connect(new StreamableHTTPClientTransport(new URL(url)));

  const tools = await client.listTools();
  console.log("Registered tools (%d):", tools.tools.length);
  for (const t of tools.tools) console.log(`  - ${t.name}`);

  // 1. structuredSearch — Belize cafes
  const s = await call("structuredSearch", {
    countryCode: "Belize",
    queryTags: { amenity: "cafe" },
    topK: 2,
  });
  if (s) {
    console.log(
      `\n✅ structuredSearch: ${s.count} results; top: ${s.results?.[0]?.tags?.name} @ (${s.results?.[0]?.geom?.lat?.toFixed(3)}, ${s.results?.[0]?.geom?.lon?.toFixed(3)})`
    );
  }

  // 2. nameSearch — cross-script (Korean → Latin)
  const n = await call("nameSearch", {
    countryCode: "KR",
    naturalQuery: "파리바게뜨",
    topK: 2,
  });
  if (n) {
    const first = n.results?.[0];
    console.log(
      `✅ nameSearch: ${n.count} results; top: ${first?.tags?.name || first?.name} @ (${first?.geom?.lat?.toFixed(3)}, ${first?.geom?.lon?.toFixed(3)})`
    );
  }

  // 3. templateQuery — MapQA end-to-end
  const t = await call("templateQuery", {
    query: "How far is the nearest cafe in Belize City?",
  });
  if (t) {
    console.log(
      `✅ templateQuery: template=${t.parsed?.template}; answer="${t.result?.answer}"; results=${(t.result?.results || []).length}`
    );
  }
} catch (err) {
  console.error("MCP smoke test failed:", err.message);
  process.exitCode = 1;
} finally {
  await client.close().catch(() => {});
}
