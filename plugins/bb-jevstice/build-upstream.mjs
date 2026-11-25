import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const root = dirname(fileURLToPath(import.meta.url));
const source = join(root, "node_modules", "jevstice", "src");

function replaceOnce(text, original, replacement) {
  if (text.split(original).length !== 2) {
    throw new Error("Jevstice changed at a BB adapter boundary; review the upstream pin");
  }
  return text.replace(original, () => replacement);
}

const entryDir = await mkdtemp(join(tmpdir(), "bb-jevstice-build-"));
try {
  const entry = join(entryDir, "entry.ts");
  await writeFile(entry, [
    `export { createJevController } from ${JSON.stringify(join(source, "controller.ts"))};`,
    `export { createTypeSafeJudge } from ${JSON.stringify(join(source, "judge.ts"))};`,
    `export { DEFAULT_CONFIG } from ${JSON.stringify(join(source, "config.ts"))};`,
    `export { createToolCatalog } from ${JSON.stringify(join(source, "guidance.ts"))};`,
  ].join("\n"));
  await build({
    entryPoints: [entry],
    outfile: join(root, "upstream.mjs"),
    platform: "node",
    format: "esm",
    target: "node22",
    bundle: true,
    banner: { js: 'import { createRequire } from "node:module";const require = createRequire(import.meta.url);' },
    plugins: [{
      name: "jevstice-bb-boundaries",
      setup(builder) {
        builder.onLoad({ filter: /[\\/]jevstice[\\/]src[\\/]controller\.ts$/ }, async ({ path }) => {
          let contents = await readFile(path, "utf8");
          const skill = await readFile(join(source, "jev-skill.md"), "utf8");
          contents = replaceOnce(contents,
            'readFileSync(new URL("./jev-skill.md", import.meta.url), "utf8").trim()',
            JSON.stringify(skill.trim()));
          contents = replaceOnce(contents, "interface ControllerDeps {",
            "interface ControllerDeps {\n readSessionArtifact?: (ctx: unknown, name: string, maxBytes?: number) => Promise<string | null>;");
          contents = replaceOnce(contents, "const manager = sessionManagerOf(ctx);",
            "if (deps.readSessionArtifact !== undefined) return deps.readSessionArtifact(ctx, name, maxBytes);\n const manager = sessionManagerOf(ctx);");
          return { contents, loader: "ts", resolveDir: source };
        });
      },
    }],
  });
} finally {
  await rm(entryDir, { recursive: true, force: true });
}
