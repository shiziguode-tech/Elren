#!/usr/bin/env node
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import * as z from "zod/v4";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";

const workspace = path.resolve(
  process.env.ELREN_WORKSPACE || process.env.DEEPDESK_WORKSPACE || process.cwd(),
);
const outputs = path.join(workspace, "outputs");
await fs.mkdir(outputs, { recursive: true });
const outputsReal = await fs.realpath(outputs);

function artifactPath(relativePath) {
  if (!relativePath || path.isAbsolute(relativePath)) throw new Error("A relative artifact path is required");
  const normalized = relativePath.replaceAll("\\", "/").replace(/^\.\//, "");
  const withoutOutputPrefix = normalized.replace(/^outputs\//i, "");
  if (!withoutOutputPrefix || withoutOutputPrefix === ".") throw new Error("An artifact filename is required");
  const resolved = path.resolve(outputs, withoutOutputPrefix);
  if (resolved !== outputs && !resolved.startsWith(outputs + path.sep)) throw new Error("Artifact path escapes outputs/");
  return resolved;
}

function assertRealArtifactPath(resolved) {
  if (resolved !== outputsReal && !resolved.startsWith(outputsReal + path.sep)) {
    throw new Error("Artifact resolves outside outputs/");
  }
  return resolved;
}

async function walk(directory, prefix = "") {
  const entries = await fs.readdir(directory, { withFileTypes: true });
  const items = [];
  for (const entry of entries.slice(0, 500)) {
    const relative = path.join(prefix, entry.name);
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) items.push(...await walk(absolute, relative));
    else if (entry.isFile()) {
      const stat = await fs.stat(absolute);
      items.push({ path: relative.replaceAll("\\", "/"), size: stat.size, modified: stat.mtime.toISOString() });
    }
  }
  return items;
}

const server = new McpServer({ name: "elren-workspace", version: "1.0.0" });

server.registerTool("runtime_info", {
  description: "Return non-secret Elren MCP runtime information",
  inputSchema: {},
}, async () => ({
  content: [{ type: "text", text: JSON.stringify({ platform: os.platform(), arch: os.arch(), workspace, outputs, pid: process.pid }) }],
}));

server.registerTool("list_artifacts", {
  description: "List files in the Elren outputs directory",
  inputSchema: {},
}, async () => ({
  content: [{ type: "text", text: JSON.stringify({ artifacts: await walk(outputs) }) }],
}));

server.registerTool("read_artifact", {
  description: "Read a UTF-8 text artifact from outputs/. A leading outputs/ prefix is accepted and normalized.",
  inputSchema: { path: z.string().min(1).max(500) },
}, async ({ path: relativePath }) => {
  const target = assertRealArtifactPath(await fs.realpath(artifactPath(relativePath)));
  const stat = await fs.stat(target);
  if (stat.size > 200_000) throw new Error("Artifact is larger than 200 KB");
  return { content: [{ type: "text", text: await fs.readFile(target, "utf8") }] };
});

server.registerTool("write_artifact", {
  description: "Create or overwrite a UTF-8 text artifact inside outputs/. A leading outputs/ prefix is accepted and normalized.",
  inputSchema: {
    path: z.string().min(1).max(500),
    content: z.string().max(200_000),
  },
}, async ({ path: relativePath, content }) => {
  const target = artifactPath(relativePath);
  await fs.mkdir(path.dirname(target), { recursive: true });
  const parent = assertRealArtifactPath(await fs.realpath(path.dirname(target)));
  const destination = path.join(parent, path.basename(target));
  const temporary = path.join(
    parent,
    `.${path.basename(target)}.elren-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}.tmp`,
  );
  let handle;
  try {
    // Write a brand-new sibling and replace the directory entry. Writing the
    // destination directly follows an outputs/ symlink or hardlink and can
    // overwrite AGENTS.md outside outputs. Atomic replacement never writes
    // through the old inode/link.
    handle = await fs.open(temporary, "wx", 0o600);
    await handle.writeFile(content, "utf8");
    await handle.sync();
    await handle.close();
    handle = undefined;
    try {
      const existing = await fs.lstat(destination);
      if (existing.isDirectory()) throw new Error("Artifact destination is a directory");
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    // Revalidate immediately before replacement. rename replaces the old
    // directory entry without following its inode; never pre-delete it, since
    // a failed rename must leave the previous artifact intact.
    assertRealArtifactPath(await fs.realpath(parent));
    await fs.rename(temporary, destination);
  } finally {
    if (handle) await handle.close().catch(() => {});
    await fs.rm(temporary, { force: true }).catch(() => {});
  }
  const normalizedPath = path.relative(outputs, target).replaceAll("\\", "/");
  return { content: [{ type: "text", text: JSON.stringify({ path: normalizedPath, bytes: Buffer.byteLength(content) }) }] };
});

const transport = new StdioServerTransport();
await server.connect(transport);
console.error("elren-workspace MCP server ready");
