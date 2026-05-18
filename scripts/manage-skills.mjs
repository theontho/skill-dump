#!/usr/bin/env node

import { createHash } from "node:crypto";
import { existsSync, lstatSync, mkdirSync, readFileSync, readlinkSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SKILLS_DIR = join(ROOT, "skills");

const PACKAGE = {
  name: "skill-dump",
  description: "a dumping ground for various skills I make",
  version: "0.1.0",
  source: "github.com/theontho/skill-dump",
  repository: "https://github.com/theontho/skill-dump",
  author: "theontho",
  license: "MIT",
};

const PLUGIN_DESCRIPTION = "Reusable agent skills for transcript summaries, web-to-Markdown conversion, and Ghostty theme tooling.";

function usage() {
  return `Usage:
  node scripts/manage-skills.mjs list
  node scripts/manage-skills.mjs sync [--check]
  node scripts/manage-skills.mjs new <skill-name> --description "When to use this skill."
`;
}

function fail(message) {
  console.error(message);
  process.exit(1);
}

function rel(path) {
  return path.startsWith(ROOT) ? path.slice(ROOT.length + 1) : path;
}

function readJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function stableJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function parseScalar(value) {
  const trimmed = value.trim();
  if (!trimmed) return "";
  if ((trimmed.startsWith('"') && trimmed.endsWith('"')) || (trimmed.startsWith("'") && trimmed.endsWith("'"))) {
    if (trimmed.startsWith('"')) return JSON.parse(trimmed);
    return trimmed.slice(1, -1).replace(/''/g, "'");
  }
  if (trimmed === "true") return true;
  if (trimmed === "false") return false;
  return trimmed;
}

function parseFrontmatter(markdown, filePath) {
  const lines = markdown.split(/\r?\n/);
  if (lines[0] !== "---") {
    fail(`${rel(filePath)} must start with YAML frontmatter`);
  }

  const end = lines.indexOf("---", 1);
  if (end === -1) {
    fail(`${rel(filePath)} is missing closing frontmatter marker`);
  }

  const result = {};
  let currentObject = null;

  for (const line of lines.slice(1, end)) {
    if (!line.trim()) continue;

    const nested = line.match(/^  ([A-Za-z0-9_-]+):\s*(.*)$/);
    if (nested && currentObject) {
      currentObject[nested[1]] = parseScalar(nested[2]);
      continue;
    }

    const top = line.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (!top) {
      fail(`${rel(filePath)} has unsupported frontmatter line: ${line}`);
    }

    const [, key, value] = top;
    if (value.trim() === "") {
      result[key] = {};
      currentObject = result[key];
    } else {
      result[key] = parseScalar(value);
      currentObject = null;
    }
  }

  return result;
}

function sha256(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function discoverSkills() {
  if (!existsSync(SKILLS_DIR)) return [];

  return readdirSync(SKILLS_DIR, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => {
      const skillDir = join(SKILLS_DIR, entry.name);
      const skillPath = join(skillDir, "SKILL.md");
      if (!existsSync(skillPath)) {
        fail(`${rel(skillDir)} is missing SKILL.md`);
      }

      const frontmatter = parseFrontmatter(readFileSync(skillPath, "utf8"), skillPath);
      const id = frontmatter.name || entry.name;
      if (id !== entry.name) {
        fail(`${rel(skillPath)} frontmatter name must match its directory (${entry.name})`);
      }
      if (!frontmatter.description) {
        fail(`${rel(skillPath)} must include a description`);
      }

      const skill = {
        id,
        name: id,
        description: frontmatter.description,
      };

      if (frontmatter.license) skill.license = frontmatter.license;
      skill.path = `skills/${entry.name}/SKILL.md`;
      skill.sha256 = sha256(skillPath);
      if (frontmatter.metadata && Object.keys(frontmatter.metadata).length > 0) {
        skill.metadata = frontmatter.metadata;
      }

      return skill;
    })
    .sort((a, b) => a.id.localeCompare(b.id));
}

function existingGeneratedAt() {
  const path = join(ROOT, ".agentskills.json");
  if (!existsSync(path)) return new Date().toISOString();
  try {
    return readJson(path).package?.generated_at || new Date().toISOString();
  } catch {
    return new Date().toISOString();
  }
}

function buildAgentSkills(skills) {
  return {
    manifest_version: 1,
    package: {
      name: PACKAGE.name,
      description: PACKAGE.description,
      version: PACKAGE.version,
      generated_at: existingGeneratedAt(),
      source: PACKAGE.source,
    },
    skills,
  };
}

function buildClaudePlugin() {
  return {
    name: PACKAGE.name,
    description: PLUGIN_DESCRIPTION,
    version: PACKAGE.version,
    author: {
      name: PACKAGE.author,
    },
    repository: PACKAGE.repository,
    license: PACKAGE.license,
    skills: "./skills/",
  };
}

function buildClaudeMarketplace() {
  return {
    name: "theontho-skill-dump",
    owner: {
      name: PACKAGE.author,
    },
    description: "Marketplace catalog for theontho/skill-dump agent skills.",
    version: PACKAGE.version,
    plugins: [
      {
        name: PACKAGE.name,
        source: "./",
        description: PLUGIN_DESCRIPTION,
        version: PACKAGE.version,
        author: {
          name: PACKAGE.author,
        },
        repository: PACKAGE.repository,
        license: PACKAGE.license,
        keywords: ["agent-skills", "claude-code", "transcripts", "markdown", "ghostty"],
        category: "productivity",
      },
    ],
  };
}

function buildCodexPlugin() {
  return {
    name: PACKAGE.name,
    version: PACKAGE.version,
    description: PLUGIN_DESCRIPTION,
    author: {
      name: PACKAGE.author,
    },
    repository: PACKAGE.repository,
    license: PACKAGE.license,
    keywords: ["agent-skills", "codex", "transcripts", "markdown", "ghostty"],
    skills: "./skills/",
    interface: {
      displayName: "Skill Dump",
      shortDescription: "Reusable skills for transcripts, Markdown conversion, and Ghostty themes.",
      longDescription: "A small collection of reusable agent skills packaged for Codex.",
      developerName: PACKAGE.author,
      category: "Productivity",
      capabilities: ["Read", "Write"],
      websiteURL: PACKAGE.repository,
    },
  };
}

function buildCodexMarketplace() {
  return {
    name: "theontho-skill-dump",
    interface: {
      displayName: "theontho/skill-dump",
    },
    plugins: [
      {
        name: PACKAGE.name,
        source: {
          source: "local",
          path: "./",
        },
        policy: {
          installation: "AVAILABLE",
          authentication: "ON_INSTALL",
        },
        category: "Productivity",
      },
    ],
  };
}

function checkOrWriteJson(path, value, check) {
  const next = stableJson(value);
  if (check) {
    if (!existsSync(path) || readFileSync(path, "utf8") !== next) {
      console.error(`${rel(path)} is stale; run npm run skills:sync`);
      return false;
    }
    return true;
  }

  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, next);
  return true;
}

function checkOrCreateSymlink(path, target, check, type = "dir") {
  if (existsSync(path) || lstatExists(path)) {
    const stat = lstatSync(path);
    if (!stat.isSymbolicLink()) {
      fail(`${rel(path)} exists and is not a symlink; refusing to replace it`);
    }

    const current = readlinkSync(path);
    if (current === target) return true;

    if (check) {
      console.error(`${rel(path)} points to ${current}; expected ${target}`);
      return false;
    }

    rmSync(path);
  }

  if (check) {
    console.error(`${rel(path)} is missing; run npm run skills:sync`);
    return false;
  }

  mkdirSync(dirname(path), { recursive: true });
  symlinkSync(target, path, type);
  return true;
}

function lstatExists(path) {
  try {
    lstatSync(path);
    return true;
  } catch {
    return false;
  }
}

function sync({ check = false } = {}) {
  const skills = discoverSkills();
  let ok = true;

  ok = checkOrWriteJson(join(ROOT, ".agentskills.json"), buildAgentSkills(skills), check) && ok;
  ok = checkOrWriteJson(join(ROOT, ".claude-plugin", "plugin.json"), buildClaudePlugin(), check) && ok;
  ok = checkOrWriteJson(join(ROOT, ".claude-plugin", "marketplace.json"), buildClaudeMarketplace(), check) && ok;
  ok = checkOrWriteJson(join(ROOT, ".codex-plugin", "plugin.json"), buildCodexPlugin(), check) && ok;
  ok = checkOrWriteJson(join(ROOT, ".agents", "plugins", "marketplace.json"), buildCodexMarketplace(), check) && ok;

  ok = checkOrCreateSymlink(join(ROOT, ".agents", "skills"), "../skills", check) && ok;
  ok = checkOrCreateSymlink(join(ROOT, ".claude", "skills"), "../skills", check) && ok;
  ok = checkOrCreateSymlink(join(ROOT, "agentskills.json"), ".agentskills.json", check, "file") && ok;

  if (!ok) process.exit(1);
  console.log(check ? "Skill metadata is up to date." : `Synced ${skills.length} skills.`);
}

function listSkills() {
  for (const skill of discoverSkills()) {
    console.log(`${skill.id}\t${skill.path}`);
  }
}

function parseOptions(args) {
  const options = {};
  const positional = [];

  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--description" || arg === "-d") {
      options.description = args[++index];
    } else if (arg === "--license") {
      options.license = args[++index];
    } else {
      positional.push(arg);
    }
  }

  return { options, positional };
}

function createSkill(args) {
  const { options, positional } = parseOptions(args);
  const name = positional[0];

  if (!name || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(name)) {
    fail(`Skill name must be kebab-case, max 64 chars.\n\n${usage()}`);
  }
  if (!options.description) {
    fail(`New skills require --description so agents know when to use them.\n\n${usage()}`);
  }

  const dir = join(SKILLS_DIR, name);
  const skillPath = join(dir, "SKILL.md");
  if (existsSync(dir)) {
    fail(`${rel(dir)} already exists`);
  }

  mkdirSync(dir, { recursive: true });
  const license = options.license || PACKAGE.license;
  const frontmatter = [
    "---",
    `name: ${name}`,
    `description: ${JSON.stringify(options.description)}`,
    `license: ${license}`,
    "metadata:",
    `  author: ${PACKAGE.author}`,
    '  version: "1.0.0"',
    "---",
    "",
    `# ${name}`,
    "",
    "Describe the workflow, inputs, outputs, and any scripts or references this skill should use.",
    "",
  ].join("\n");

  writeFileSync(skillPath, frontmatter);
  sync();
  console.log(`Created ${rel(skillPath)}`);
}

const [command, ...args] = process.argv.slice(2);

if (command === "list") {
  listSkills();
} else if (command === "sync") {
  sync({ check: args.includes("--check") });
} else if (command === "new") {
  createSkill(args);
} else {
  fail(usage());
}
