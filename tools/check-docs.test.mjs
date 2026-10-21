import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { lint } from "markdownlint-cli2/markdownlint/promise";
import closedFences from "./closed-fences.cjs";
import { collectPublicMarkdown, runChecks } from "./check-docs.mjs";

const runnerUrl = new URL("./check-docs.mjs", import.meta.url).href;
const validDiagram = "```mermaid\nflowchart LR\n    A --> B\n```\n";
const malformedDiagram = "```mermaid\nflowchart LR\n    A -->\n```\n";

function fixture(t, files = {}) {
  const root = mkdtempSync(join(tmpdir(), "uplus docs [fixture] "));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const [name, text] of Object.entries(files)) {
    const path = join(root, name);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, text);
  }
  return root;
}

function check(root) {
  const originals = new Map(collectPublicMarkdown(root).map((file) => [file, readFileSync(join(root, file))]));
  const result = spawnSync(process.execPath, [
    "--input-type=module", "-e",
    `import { checkDocumentation } from ${JSON.stringify(runnerUrl)}; process.exitCode = checkDocumentation(process.argv[1]);`,
    root,
  ], { cwd: root, encoding: "utf8", shell: false });
  assert.ifError(result.error);
  assert.equal(result.signal, null);
  for (const [file, original] of originals) {
    assert.deepEqual(readFileSync(join(root, file)), original, `${file} must not be modified`);
  }
  return { status: result.status, output: result.stdout + result.stderr };
}

test("selection is sorted, public-only, recursive under docs, and includes future release notes", (t) => {
  const root = fixture(t, {
    "README.md": "",
    "CHANGELOG.md": "",
    "CONTRIBUTING.md": "",
    "SECURITY.md": "",
    ".github/PULL_REQUEST_TEMPLATE.md": "",
    ".github/ISSUE_TEMPLATE/work_item.md": "",
    ".github/backlog/private-plan.md": "private",
    "AGENTS.md": "private",
    ".hidden.md": "private",
    "docs/z.md": "",
    "docs/a space/guide [1].md": "",
    "docs/AGENTS.md": "private",
    "docs/not-markdown.txt": "",
    "other/README.md": "private",
    ".local/report.md": "private",
    ".agents/rules.md": "private",
    "raw/capture.md": "private",
  });
  for (const directory of [
    ".local", ".agents", ".git", ".venv", "node_modules", "venv", "env", "deps",
    "vendor", "artifacts", "build", "dist", "generated", "coverage", "site", "tmp", "raw", "secrets", "decompiled",
  ]) {
    mkdirSync(join(root, "docs", directory), { recursive: true });
    writeFileSync(join(root, "docs", directory, "excluded.md"), "private");
  }
  assert.deepEqual(collectPublicMarkdown(root), [
    ".github/ISSUE_TEMPLATE/work_item.md", ".github/PULL_REQUEST_TEMPLATE.md",
    "CHANGELOG.md", "CONTRIBUTING.md", "README.md", "SECURITY.md",
    "docs/a space/guide [1].md", "docs/z.md",
  ]);
});

test("directory symlinks and Windows junctions are never traversed", (t) => {
  const root = fixture(t, { "README.md": "", "docs/public.md": "", ".local/private.md": "" });
  const linkType = process.platform === "win32" ? "junction" : "dir";
  symlinkSync(join(root, ".local"), join(root, "docs", "private-link"), linkType);
  symlinkSync(root, join(root, "docs", "loop"), linkType);
  assert.deepEqual(collectPublicMarkdown(root), ["README.md", "docs/public.md"]);
});

test("a linked docs directory is not a public source directory", (t) => {
  const root = fixture(t, { "README.md": "", ".local/private.md": "" });
  symlinkSync(join(root, ".local"), join(root, "docs"), process.platform === "win32" ? "junction" : "dir");
  assert.deepEqual(collectPublicMarkdown(root), ["README.md"]);
});

test("linked GitHub template directories cannot import private records", (t) => {
  const root = fixture(t, { "README.md": "", ".local/private.md": "" });
  mkdirSync(join(root, ".github"));
  symlinkSync(join(root, ".local"), join(root, ".github/ISSUE_TEMPLATE"),
    process.platform === "win32" ? "junction" : "dir");
  assert.deepEqual(collectPublicMarkdown(root), ["README.md"]);
});

test("file symlinks cannot import private Markdown", (t) => {
  const root = fixture(t, { "README.md": "", "docs/public.md": "", ".local/private.md": "" });
  try {
    symlinkSync(join(root, ".local", "private.md"), join(root, "docs", "public-looking.md"), "file");
  } catch (error) {
    if (process.platform !== "win32" || !["EPERM", "EACCES"].includes(error.code)) throw error;
    t.skip("Windows file symlinks require Developer Mode or symbolic-link privilege; junction coverage remains mandatory.");
    return;
  }
  assert.deepEqual(collectPublicMarkdown(root), ["README.md", "docs/public.md"]);
});

test("empty public selection fails without default CLI discovery", (t) => {
  const root = fixture(t, { ".local/private.md": "# Private\n" });
  const result = check(root);
  assert.equal(result.status, 1);
  assert.match(result.output, /No public Markdown files found/);
  assert.doesNotMatch(result.output, /\[Markdown\]|\[Mermaid:/);
});

for (const [label, eol] of [["LF", "\n"], ["CRLF", "\r\n"]]) {
  test(`real linters accept ${label}, spaces, glob metacharacters and nested Mermaid without rewriting`, (t) => {
    const root = fixture(t, {
      "README.md": (`# Readme\n\n${validDiagram}`).replaceAll("\n", eol),
      "CHANGELOG.md": (`# Changelog\n\n${validDiagram}`).replaceAll("\n", eol),
      "docs/deep space/guide [1].md": (
        "# Nested\n\n- Example:\n\n  > ```mermaid\n  > flowchart LR\n  >     A --> B\n  > ```\n"
      ).replaceAll("\n", eol),
      "AGENTS.md": malformedDiagram,
      ".local/private.md": malformedDiagram,
      "docs/artifacts/excluded.md": malformedDiagram,
    });
    const result = check(root);
    assert.equal(result.status, 0, result.output);
    assert.match(result.output, /Markdown passed/);
    for (const file of collectPublicMarkdown(root)) {
      assert.ok(result.output.includes(`Mermaid: ${file} passed.`), result.output);
      assert.ok(result.output.includes(`scanning ${file}`), result.output);
    }
    assert.equal((result.output.match(/checked 1 diagram in 1 file/g) ?? []).length, 3, result.output);
    assert.doesNotMatch(result.output, /<stdin>|private\.md|excluded\.md|AGENTS\.md/);
  });
}

test("real Markdown violations fail but Mermaid still runs", (t) => {
  const root = fixture(t, { "README.md": `# Readme\n\n### Skipped heading\n\n${validDiagram}` });
  const result = check(root);
  assert.equal(result.status, 1, result.output);
  assert.match(result.output, /MD001/);
  assert.match(result.output, /Markdown failed/);
  assert.match(result.output, /Mermaid: README\.md passed/);
});

test("real Mermaid syntax failures propagate after Markdown succeeds", (t) => {
  const root = fixture(t, { "README.md": `# Readme\n\n${malformedDiagram}` });
  const result = check(root);
  assert.equal(result.status, 1, result.output);
  assert.match(result.output, /Markdown passed/);
  assert.match(result.output, /Mermaid: README\.md failed/);
  assert.match(result.output, /README\.md:\d+:\d+:.*parse error/);
});

test("strict Mermaid warnings are failures, not silent successes", (t) => {
  const root = fixture(t, { "README.md": "# Readme\n\n```mermaid\nflowchart LR\n    A --> B\n    A --> B\n```\n" });
  const result = check(root);
  assert.equal(result.status, 1, result.output);
  assert.match(result.output, /no-duplicate-edges/);
  assert.match(result.output, /Mermaid: README\.md failed/);
});

test("both failures are reported, including nested Mermaid with original file provenance", (t) => {
  const root = fixture(t, {
    "docs/nested [diagram].md": "# Nested\n\n### Skipped heading\n\n- Example:\n\n  > ```mermaid\n  > flowchart LR\n  >     A -->\n  > ```\n",
  });
  const result = check(root);
  assert.equal(result.status, 1, result.output);
  assert.match(result.output, /MD001/);
  assert.match(result.output, /Markdown failed/);
  assert.match(result.output, /Mermaid: docs\/nested \[diagram\]\.md failed/);
  assert.match(result.output, /docs\/nested \[diagram\]\.md:(?:8|9|10):\d+:.*parse error/);
  assert.doesNotMatch(result.output, /<stdin>/);
});

test("Mermaid-looking examples inside a non-Mermaid fence are not linted as diagrams", (t) => {
  const root = fixture(t, {
    "README.md": "# Example\n\n````text\n```mermaid\nthis is deliberately not a diagram\n```\n````\n",
  });
  const result = check(root);
  assert.equal(result.status, 0, result.output);
  assert.match(result.output, /checked 0 diagrams in 1 file/);
});

test("unclosed non-Mermaid fences fail the canonical check", (t) => {
  const root = fixture(t, { "README.md": "# Readme\n\n> ```text\n> missing close\n" });
  const result = check(root);
  assert.equal(result.status, 1, result.output);
  assert.match(result.output, /closed-fences/);
  assert.match(result.output, /Mermaid: README\.md passed/);
});

const closedCases = [
  "```text\nbody\n```\n",
  "~~~text\nbody\n~~~~\n",
  "````text\n```\n````\n",
  "- Item\n\n  ```text\n  body\n  ```\n",
  "> ```text\n> body\n> ```\n",
  "- Item\n\n  > 1. Item\n  >\n  >    ```text\n  >    body\n  >    ```\n",
  "```text\nbody\n```",
  "    ```text\n    indented code, not a fence\n",
];
const unclosedCases = [
  "```text\nbody\n",
  "```text\nbody\n~~~\n",
  "````text\nbody\n```\n",
  "```text\nbody\n``` trailing text\n",
  "- Item\n\n  ```text\n  body\n",
  "> ```text\n> body\n",
  "> ```text\n> body\n\n```\n",
  "- Item\n\n  > ```text\n  > body\n\n  ```\n",
];

for (const [label, cases, expectedClosed] of [
  ["closed", closedCases, true], ["unclosed", unclosedCases, false],
]) {
  test(`fence rule recognizes ${label} containers, markers and lengths with LF and CRLF`, async () => {
    for (const text of cases) {
      for (const eol of ["\n", "\r\n"]) {
        const results = await lint({
          strings: { fixture: text.replaceAll("\n", eol) },
          config: { default: false, "closed-fences": true },
          customRules: [closedFences],
        });
        assert.equal(results.fixture.length === 0, expectedClosed, JSON.stringify(text));
        if (!expectedClosed) assert.ok(results.fixture.every((error) => error.ruleNames.includes("closed-fences")));
      }
    }
  });
}

test("process failure in either position never masks or prevents the other check", (t) => {
  const root = fixture(t);
  for (const codes of [[0, 0], [2, 0], [0, 3], [2, 3]]) {
    const names = codes.map((code, index) => `${codes.join("-")}-${index}.txt`);
    const checks = codes.map((code, index) => ({
      name: `Process ${index}`,
      prepare: () => ({ args: ["-e", `require('node:fs').writeFileSync(${JSON.stringify(names[index])}, 'ran'); process.exit(${code});`] }),
    }));
    assert.equal(runChecks(checks, root), codes.some(Boolean) ? 1 : 0);
    for (const name of names) assert.equal(readFileSync(join(root, name), "utf8"), "ran");
  }
});

test("executable preparation errors do not prevent subsequent processes", (t) => {
  const root = fixture(t);
  const result = runChecks([
    { name: "Missing dependency", prepare: () => { throw new Error("missing executable"); } },
    { name: "Following check", prepare: () => ({ args: ["-e", "require('node:fs').writeFileSync('continued.txt', 'ran')"] }) },
  ], root);
  assert.equal(result, 1);
  assert.equal(readFileSync(join(root, "continued.txt"), "utf8"), "ran");
});
