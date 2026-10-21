import { spawnSync } from "node:child_process";
import { lstatSync, readFileSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const repositoryRoot = fileURLToPath(new URL("../", import.meta.url));
const configPath = join(repositoryRoot, ".markdownlint-cli2.jsonc");
const excludedDirectories = new Set([
  "artifacts", "build", "coverage", "decompiled", "deps", "dist", "env",
  "generated", "node_modules", "raw", "secrets", "site", "tmp", "vendor", "venv",
]);

// Enumerate root Markdown, docs, and public GitHub templates, never the whole tree.
// Dirent/lstat checks do not follow file symlinks, directory symlinks or junctions.
export function collectPublicMarkdown(root) {
  const files = [];
  function visit(directory, prefix, recursive) {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const name = entry.name;
      const lowerName = name.toLowerCase();
      if (name.startsWith(".") || lowerName === "agents.md" || entry.isSymbolicLink()) {
        continue;
      }
      const relative = prefix ? `${prefix}/${name}` : name;
      if (entry.isFile() && lowerName.endsWith(".md")) {
        files.push(relative);
      } else if (recursive && entry.isDirectory() && !excludedDirectories.has(lowerName)) {
        visit(join(directory, name), relative, true);
      }
    }
  }
  visit(root, "", false);
  const docs = join(root, "docs");
  let docsStat;
  try {
    docsStat = lstatSync(docs);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  if (docsStat?.isDirectory() && !docsStat.isSymbolicLink()) {
    visit(docs, "docs", true);
  }
  const github = join(root, ".github");
  let githubStat;
  try {
    githubStat = lstatSync(github);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  if (githubStat?.isDirectory() && !githubStat.isSymbolicLink()) {
    visit(github, ".github", false);
    const templates = join(github, "ISSUE_TEMPLATE");
    let templatesStat;
    try {
      templatesStat = lstatSync(templates);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    if (templatesStat?.isDirectory() && !templatesStat.isSymbolicLink()) {
      visit(templates, ".github/ISSUE_TEMPLATE", true);
    }
  }
  return files.sort();
}

function executable(packageName, command) {
  let manifestPath;
  try {
    manifestPath = require.resolve(`${packageName}/package.json`);
  } catch (error) {
    if (error.code !== "ERR_PACKAGE_PATH_NOT_EXPORTED") throw error;
    // markdownlint-cli2 exports its main module, but not its package manifest.
    manifestPath = join(dirname(require.resolve(packageName)), "package.json");
  }
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const bin = typeof manifest.bin === "string" ? manifest.bin : manifest.bin?.[command];
  if (!bin) throw new Error(`${packageName} does not declare executable ${command}`);
  return resolve(dirname(manifestPath), bin);
}

function mermaidInput(path) {
  // The Mermaid CLI's raw-line extractor does not understand blockquote/list
  // containers. Use the Markdown parser already pinned by markdownlint-cli2
  // to extract only real Mermaid fences, not examples inside other code blocks.
  const MarkdownIt = createRequire(require.resolve("markdownlint-cli2"))("markdown-it");
  const text = readFileSync(path, "utf8").replace(/\r\n?/g, "\n");
  const lines = text.split("\n").map(() => "");
  for (const token of new MarkdownIt({ html: true }).parse(text, {})) {
    if (token.type !== "fence" || token.info.trim().split(/\s+/)[0] !== "mermaid") continue;
    const body = token.content.replace(/\n$/, "");
    const bodyLines = body ? body.split("\n") : [];
    const start = token.map[0];
    lines[start] = `${token.markup}mermaid`;
    for (let index = 0; index < bodyLines.length; index++) {
      lines[start + 1 + index] = bodyLines[index];
    }
    // Do not repair unclosed fences: the strict checker must see them too.
    if (token.map[1] - start === bodyLines.length + 2) {
      lines[start + bodyLines.length + 1] = token.markup;
    }
  }
  return lines.join("\n");
}

// Prepare each check inside its own try block: even a missing package or a
// failed process must not prevent the other check from producing diagnostics.
export function runChecks(checks, cwd) {
  let failed = false;
  for (const { name, prepare } of checks) {
    console.log(`\n[${name}]`);
    try {
      const { args, input, source } = prepare();
      const result = spawnSync(process.execPath, args, {
        cwd,
        input,
        stdio: input === undefined ? "inherit" : "pipe",
        encoding: "utf8",
        shell: false,
      });
      if (input !== undefined) {
        process.stdout.write((result.stdout ?? "").replaceAll("<stdin>", source));
        process.stderr.write((result.stderr ?? "").replaceAll("<stdin>", source));
      }
      if (result.error) throw result.error;
      if (result.status !== 0) {
        failed = true;
        console.error(`${name} failed (${result.signal ? `signal ${result.signal}` : `exit ${result.status}`}).`);
      } else {
        console.log(`${name} passed.`);
      }
    } catch (error) {
      failed = true;
      console.error(`${name} could not run: ${error.message}`);
    }
  }
  return failed ? 1 : 0;
}

export function checkDocumentation(root = repositoryRoot) {
  const files = collectPublicMarkdown(root);
  if (files.length === 0) {
    // Neither CLI may fall back to its own default discovery (or stdin).
    console.error("No public Markdown files found.");
    return 1;
  }
  console.log(`Public Markdown (${files.length}):\n${files.join("\n")}`);
  return runChecks([
    {
      name: "Markdown",
      prepare: () => ({
        args: [
          executable("markdownlint-cli2", "markdownlint-cli2"),
          "--config", configPath, "--no-globs", "--",
          ...files.map((file) => `:./${file}`),
        ],
      }),
    },
    ...files.map((file) => ({
      name: `Mermaid: ${file}`,
      prepare: () => ({
        args: [executable("@mermaid-lint/cli", "mermaid-lint"), "--strict", "-"],
        input: mermaidInput(join(root, file)),
        source: file,
      }),
    })),
  ], root);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    process.exitCode = checkDocumentation();
  } catch (error) {
    console.error(`Documentation file selection failed: ${error.message}`);
    process.exitCode = 1;
  }
}
