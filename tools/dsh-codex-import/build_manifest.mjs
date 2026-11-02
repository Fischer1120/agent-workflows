#!/usr/bin/env node
/**
 * Build a DSH shared-skill manifest from an explicit Codex `skills/list`
 * catalog and, optionally, one or more confirmed-active Desktop plugin skill
 * roots. Nothing is discovered automatically: every input path is named on the
 * command line, and no plugin cache version is selected for the caller.
 *
 * Catalog entries that Codex reports as enabled and not `repo`-scoped become
 * shared entries. `repo`-scoped skills stay with DSH's native project
 * discovery, and disabled skills are recorded but not shared.
 *
 * Desktop plugin skills need the YAML parser shipped with DSH Desktop; the
 * parser is loaded lazily only when a `--desktop-skills` root is supplied.
 * Set `DSH_NODE_MODULES` or `DSH_APP_ROOT` when DSH Desktop lives somewhere
 * other than the default macOS application path.
 *
 * Usage (from the repository root):
 *   node tools/dsh-codex-import/build_manifest.mjs --catalog PATH --output PATH \
 *     [--desktop-skills [NAMESPACE=]SKILL_ROOT]...
 *
 * The output manifest contains absolute local paths and is written with mode
 * 0600. Keep it out of version control.
 */
import { existsSync } from 'node:fs';
import { chmod, mkdir, readFile, readdir, realpath, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_OUTPUT = path.join(SCRIPT_DIR, 'manifest.json');
const DEFAULT_DSH_APP_ROOT = '/Applications/DSH Desktop.app/Contents/Resources/app';
const SKILL_NAME_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

/**
 * Return whether a value is a plain JSON object.
 * @param value - candidate value.
 * @returns whether it is a non-array object.
 */
function isRecord(value) {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * Resolve the DSH `node_modules` directory from the environment.
 * @returns the node_modules root, or the macOS default when unset.
 */
function dshNodeModulesRoot() {
	return process.env.DSH_NODE_MODULES ?? path.join(process.env.DSH_APP_ROOT ?? DEFAULT_DSH_APP_ROOT, 'node_modules');
}

/**
 * Load the `yaml` parser shipped with DSH Desktop without installing a package.
 * @returns the parser's `parse` function.
 */
function loadYamlParser() {
	const packageJson = process.env.DSH_YAML_PACKAGE_JSON
		?? path.join(dshNodeModulesRoot(), '@deepseek-ai/dsh-skill-filesystem/package.json');
	if (!existsSync(packageJson)) {
		throw new Error(
			`desktop skills need the YAML parser shipped with DSH; set DSH_NODE_MODULES or DSH_APP_ROOT (checked ${packageJson})`,
		);
	}
	const requireFromDsh = createRequire(packageJson);
	try {
		return requireFromDsh('yaml').parse;
	} catch (error) {
		throw new Error(`cannot load the YAML parser from ${packageJson}: ${error instanceof Error ? error.message : String(error)}`);
	}
}

/**
 * Derive the DSH skill name from a Codex name, mirroring the previous alias
 * rules: `plugin:skill` becomes `plugin-skill` unless the suffix already
 * carries the prefix, and a plain name is lower-cased and hyphenated.
 * @param codexName - Codex catalog or plugin skill name.
 * @returns the proposed DSH name.
 */
function aliasFor(codexName) {
	const [prefix, ...rest] = codexName.toLowerCase().split(':');
	const suffix = rest.join('-');
	const proposed = !suffix || suffix === prefix ? prefix : suffix.startsWith(prefix + '-') ? suffix : prefix + '-' + suffix;
	return proposed.replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}

/**
 * Derive the namespace for a desktop skill root. A `<plugin>/<version>/skills`
 * layout uses the plugin directory, a `<plugin>/skills` layout uses the plugin
 * directory, and anything else uses its own basename.
 * @param skillRoot - absolute desktop skill root.
 * @returns the namespace string.
 */
function defaultNamespace(skillRoot) {
	const base = path.basename(skillRoot);
	if (base === 'skills') {
		const parent = path.basename(path.dirname(skillRoot));
		const parentIsVersion = /^v?\d+(?:[._-]\d+)*$/i.test(parent);
		const candidate = parentIsVersion
			? path.basename(path.dirname(path.dirname(skillRoot)))
			: parent;
		if (candidate.length > 0) return candidate;
	}
	return base;
}

/**
 * Parse one `--desktop-skills` value, supporting an optional `NAMESPACE=` prefix.
 * @param spec - raw command-line value.
 * @returns `{ namespace, path }` with a possibly undefined namespace.
 */
function parseDesktopSpec(spec) {
	const separator = spec.indexOf('=');
	if (separator > 0 && separator < spec.length - 1 && /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(spec.slice(0, separator))) {
		return { namespace: spec.slice(0, separator), path: spec.slice(separator + 1) };
	}
	return { namespace: undefined, path: spec };
}

/**
 * Parse the command line.
 * @param argv - arguments after the script name.
 * @returns parsed options.
 */
function parseArgs(argv) {
	const options = { catalogPath: undefined, outputPath: DEFAULT_OUTPUT, desktopSkills: [], help: false };
	for (let index = 0; index < argv.length; index += 1) {
		const raw = argv[index];
		if (raw === '--help' || raw === '-h') {
			options.help = true;
			continue;
		}
		let key = raw;
		let inline;
		const equals = raw.indexOf('=');
		if (raw.startsWith('--') && equals > 2) {
			key = raw.slice(0, equals);
			inline = raw.slice(equals + 1);
		}
		const takeValue = () => {
			if (inline !== undefined) return inline;
			index += 1;
			if (index >= argv.length) throw new Error(`${key} requires a value`);
			return argv[index];
		};
		switch (key) {
			case '--catalog':
				options.catalogPath = takeValue();
				break;
			case '--output':
				options.outputPath = takeValue();
				break;
			case '--desktop-skills':
				options.desktopSkills.push(parseDesktopSpec(takeValue()));
				break;
			default:
				throw new Error(`unknown option ${raw}; run with --help for usage`);
		}
	}
	return options;
}

/**
 * Validate and flatten the catalog's `data[].skills` arrays.
 * @param catalog - parsed catalog JSON.
 * @param catalogPath - catalog path for error messages.
 * @returns flattened skill records.
 */
function collectCatalogSkills(catalog, catalogPath) {
	if (!isRecord(catalog) || !Array.isArray(catalog.data)) {
		throw new Error(`catalog ${catalogPath} must be a JSON object with a "data" array`);
	}
	const skills = [];
	catalog.data.forEach((entry, entryIndex) => {
		if (!isRecord(entry) || !Array.isArray(entry.skills)) {
			throw new Error(`catalog ${catalogPath} data[${entryIndex}] must be an object with a "skills" array`);
		}
		entry.skills.forEach((item, skillIndex) => {
			const label = `data[${entryIndex}].skills[${skillIndex}]`;
			if (!isRecord(item)) throw new Error(`catalog ${catalogPath} ${label} must be an object`);
			if (typeof item.name !== 'string' || item.name.trim().length === 0) {
				throw new Error(`catalog ${catalogPath} ${label}.name must be a non-empty string`);
			}
			if (typeof item.path !== 'string' || !path.isAbsolute(item.path)) {
				throw new Error(`catalog ${catalogPath} ${label}.path must be an absolute path`);
			}
			if (typeof item.enabled !== 'boolean') {
				throw new Error(`catalog ${catalogPath} ${label}.enabled must be a boolean`);
			}
			if (typeof item.scope !== 'string' || item.scope.length === 0) {
				throw new Error(`catalog ${catalogPath} ${label}.scope must be a non-empty string`);
			}
			skills.push({ name: item.name, path: item.path, enabled: item.enabled, scope: item.scope });
		});
	});
	return skills;
}

/**
 * Read one desktop skill root into catalog-shaped records.
 * @param skillRoot - directory containing skill subdirectories.
 * @param namespace - namespace prefixed to each skill name.
 * @param parseYaml - DSH-provided YAML parser.
 * @param warnings - sink for skipped entries.
 * @returns catalog-shaped skill records.
 */
async function readDesktopSkillRoot(skillRoot, namespace, parseYaml, warnings) {
	let entries;
	try {
		entries = await readdir(skillRoot, { withFileTypes: true });
	} catch (error) {
		if (error.code === 'ENOENT') throw new Error(`desktop skill root does not exist: ${skillRoot}`);
		throw error;
	}
	const skills = [];
	for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
		if (!entry.isDirectory()) continue;
		const skillPath = path.join(skillRoot, entry.name, 'SKILL.md');
		let text;
		try {
			text = await readFile(skillPath, 'utf8');
		} catch (error) {
			if (error.code === 'ENOENT') continue;
			throw error;
		}
		const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
		let meta;
		try {
			meta = match ? parseYaml(match[1]) : undefined;
		} catch (error) {
			warnings.push(`${skillPath} skipped: invalid YAML frontmatter: ${error instanceof Error ? error.message : String(error)}`);
			continue;
		}
		if (!isRecord(meta) || typeof meta.name !== 'string' || meta.name.trim().length === 0
			|| typeof meta.description !== 'string' || meta.description.trim().length === 0) {
			warnings.push(`${skillPath} skipped: frontmatter requires a non-empty name and description`);
			continue;
		}
		skills.push({ name: `${namespace}:${meta.name.trim()}`, path: skillPath, enabled: true, scope: 'desktop-plugin' });
	}
	return skills;
}

/**
 * Build the manifest object and write it to disk.
 * @param options - explicit inputs.
 * @param options.catalogPath - Codex catalog JSON path.
 * @param options.desktopSkills - desktop skill roots (`{ path, namespace? }`).
 * @param options.outputPath - manifest output path.
 * @param options.generatedAt - timestamp recorded in the manifest.
 * @returns `{ manifest, warnings }`.
 */
export async function buildManifest({
	catalogPath,
	desktopSkills = [],
	outputPath = DEFAULT_OUTPUT,
	generatedAt = new Date(),
} = {}) {
	if (typeof catalogPath !== 'string' || catalogPath.length === 0) throw new Error('--catalog is required');
	let catalog;
	try {
		catalog = JSON.parse(await readFile(catalogPath, 'utf8'));
	} catch (error) {
		throw new Error(`cannot read catalog ${catalogPath}: ${error instanceof Error ? error.message : String(error)}`);
	}
	const catalogSkills = collectCatalogSkills(catalog, catalogPath);
	const origins = [...catalogSkills];
	const warnings = [];
	if (desktopSkills.length > 0) {
		const parseYaml = loadYamlParser();
		for (const source of desktopSkills) {
			const root = path.resolve(source.path);
			const namespace = source.namespace ?? defaultNamespace(root);
			if (namespace.length === 0) throw new Error(`cannot derive a namespace for desktop skill root ${root}; pass NAMESPACE=PATH`);
			origins.push(...await readDesktopSkillRoot(root, namespace, parseYaml, warnings));
		}
	}
	const sources = origins.filter((item) => item.enabled && item.scope !== 'repo');
	sources.sort((left, right) => Number(left.name.includes(':')) - Number(right.name.includes(':'))
		|| (left.name < right.name ? -1 : left.name > right.name ? 1 : 0));

	const seenPaths = new Set();
	const seenNames = new Set();
	const entries = [];
	for (const item of sources) {
		let file;
		try {
			file = await realpath(item.path);
		} catch (error) {
			if (error.code === 'ENOENT') throw new Error(`enabled Codex skill "${item.name}" points to a missing file: ${item.path}`);
			throw error;
		}
		if (seenPaths.has(file)) continue;
		let name = aliasFor(item.name);
		if (seenNames.has(name)) name = item.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
		if (!SKILL_NAME_PATTERN.test(name)) {
			throw new Error(`cannot derive a DSH skill name from Codex skill "${item.name}"; adjust its name or the desktop namespace`);
		}
		if (seenNames.has(name)) {
			throw new Error(`conflicting DSH skill name "${name}" for Codex skill "${item.name}"; rename one source or adjust the namespace`);
		}
		seenPaths.add(file);
		seenNames.add(name);
		entries.push({ name, codex_name: item.name, path: file });
	}

	const manifest = {
		version: 1,
		generated_at: generatedAt.toISOString(),
		project_skills: catalogSkills
			.filter((item) => item.enabled && item.scope === 'repo')
			.map((item) => ({ name: item.name, path: item.path, loading: 'DSH native project discovery' })),
		disabled: catalogSkills.filter((item) => !item.enabled).map((item) => ({ name: item.name, path: item.path })),
		entries,
	};
	await mkdir(path.dirname(outputPath), { recursive: true });
	await writeFile(outputPath, JSON.stringify(manifest, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 });
	await chmod(outputPath, 0o600);
	return { manifest, warnings };
}

/**
 * Run the command-line entry point.
 * @param argv - arguments after the script name.
 */
export async function main(argv = process.argv.slice(2)) {
	const options = parseArgs(argv);
	if (options.help) {
		console.log(`Usage: node tools/dsh-codex-import/build_manifest.mjs --catalog PATH [options]

Build a DSH shared-skill manifest from an explicit Codex skills/list catalog.

Options:
  --catalog PATH                Codex catalog JSON written by read_codex_catalog.py (required)
  --output PATH                 Manifest output path (default: ${DEFAULT_OUTPUT})
  --desktop-skills [NAME=]PATH  Skill root of one confirmed-active Desktop plugin version;
                                repeatable. The default namespace is the plugin directory name
                                for a <plugin>/<version>/skills layout, else the root's basename.
  -h, --help                    Show this help.

Environment:
  DSH_NODE_MODULES, DSH_APP_ROOT  Installed DSH Desktop dependency root. Desktop skill roots
                                  need its bundled YAML parser; catalog-only builds do not.
  DSH_YAML_PACKAGE_JSON           Exact path to dsh-skill-filesystem/package.json.

The manifest contains absolute local paths, is written with mode 0600, and must stay
out of version control.`);
		return;
	}
	if (!options.catalogPath) throw new Error('--catalog is required; run with --help for usage');
	const { manifest, warnings } = await buildManifest({
		catalogPath: options.catalogPath,
		desktopSkills: options.desktopSkills,
		outputPath: options.outputPath,
	});
	for (const warning of warnings) console.warn(`build_manifest: ${warning}`);
	console.log(JSON.stringify({
		output: path.resolve(options.outputPath),
		shared_global_skills: manifest.entries.length,
		native_project_skills: manifest.project_skills.map((item) => item.name),
		disabled_skills: manifest.disabled.map((item) => item.name),
		desktop_skill_roots: options.desktopSkills.length,
	}, null, 2));
}

if (process.argv[1] !== undefined && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	main().catch((error) => {
		console.error(`build_manifest: ${error instanceof Error ? error.message : String(error)}`);
		process.exitCode = 1;
	});
}