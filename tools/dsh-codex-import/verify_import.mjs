#!/usr/bin/env node
/**
 * Verify that shared Codex skills load through DSH with their original bodies
 * and resource directories, and (when installed) that the global instruction
 * link is loaded in full.
 *
 * The script checks only what the given manifest actually declares:
 * `entries` are loaded through the shared provider, `project_skills` are
 * expected from DSH's native project discovery, and `disabled` paths must not
 * appear among imported skills. It does not require any specific project skill
 * such as `project-corrections`.
 *
 * DSH packages come from the running DSH Desktop installation. Set
 * `DSH_NODE_MODULES` or `DSH_APP_ROOT` when it is not at the default macOS
 * application path. No package is installed.
 *
 * Usage (from the repository root):
 *   node tools/dsh-codex-import/verify_import.mjs --cwd PATH --manifest PATH
 *   node tools/dsh-codex-import/verify_import.mjs --cwd PATH --installed [DIR]
 */
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { lstat, mkdir, readFile, realpath, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_DSH_APP_ROOT = '/Applications/DSH Desktop.app/Contents/Resources/app';
const USAGE = `Usage: node tools/dsh-codex-import/verify_import.mjs [options]

Options:
  --cwd PATH            Project directory for native project skill discovery (default: current directory)
  --manifest PATH       Manifest to verify; provider.mjs comes from the manifest directory
                        when present, otherwise from this script's directory
  --installed [DIR]     Verify an installed copy (default: <dsh-home>/codex-shared-skills)
  --dsh-home PATH       DSH home used by --installed (default: $DSH_HOME or ~/.dsh)
  -h, --help            Show this help

Environment:
  DSH_NODE_MODULES, DSH_APP_ROOT  Installed DSH Desktop dependency root
  DSH_YAML_PACKAGE_JSON           Exact path to dsh-skill-filesystem/package.json

Writes a report under .verification/ next to this script.`;

/**
 * Resolve the DSH `node_modules` directory from the environment.
 * @returns the node_modules root, or the macOS default when unset.
 */
function dshNodeModulesRoot() {
	return process.env.DSH_NODE_MODULES ?? path.join(process.env.DSH_APP_ROOT ?? DEFAULT_DSH_APP_ROOT, 'node_modules');
}

/**
 * Read the `name` field from a SKILL.md frontmatter block.
 * @param raw - full SKILL.md text.
 * @returns the unquoted name, or undefined when absent.
 */
function frontmatterName(raw) {
	const frontmatter = raw.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
	if (!frontmatter) return undefined;
	const name = frontmatter[1].match(/^name:\s*(.+?)\s*$/m);
	if (!name) return undefined;
	return name[1].replace(/^["']|["']$/g, '').trim();
}

/**
 * Render an arbitrary thrown value safely.
 * @param error - thrown value.
 * @returns a string message.
 */
function errorMessage(error) {
	return error instanceof Error ? error.message : String(error);
}

/**
 * Parse the command line.
 * @param argv - arguments after the script name.
 * @returns parsed options.
 */
function parseArgs(argv) {
	const options = {
		cwd: process.cwd(),
		dshHome: process.env.DSH_HOME ? path.resolve(process.env.DSH_HOME) : path.join(os.homedir(), '.dsh'),
		installed: false,
		installedRoot: undefined,
		manifestPath: undefined,
		help: false,
	};
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
			case '--cwd':
				options.cwd = takeValue();
				break;
			case '--manifest':
				options.manifestPath = takeValue();
				break;
			case '--dsh-home':
				options.dshHome = path.resolve(takeValue());
				break;
			case '--installed':
				options.installed = true;
				if (inline !== undefined) options.installedRoot = inline;
				else if (argv[index + 1] !== undefined && !argv[index + 1].startsWith('-')) {
					index += 1;
					options.installedRoot = argv[index];
				}
				break;
			default:
				throw new Error(`unknown option ${raw}; run with --help for usage`);
		}
	}
	if (options.installed && options.manifestPath !== undefined) {
		throw new Error('--installed and --manifest are mutually exclusive');
	}
	return options;
}

/**
 * Run the verification.
 */
async function main() {
	const options = parseArgs(process.argv.slice(2));
	if (options.help) {
		console.log(USAGE);
		return;
	}
	const nodeModules = dshNodeModulesRoot();
	const dshSkillPackageJson = path.join(nodeModules, '@deepseek-ai/dsh-skill/package.json');
	if (!existsSync(dshSkillPackageJson)) {
		throw new Error(
			`DSH packages not found (${dshSkillPackageJson}); set DSH_NODE_MODULES or DSH_APP_ROOT to the installed DSH Desktop resources`,
		);
	}
	const cwd = path.resolve(options.cwd);
	if (!existsSync(cwd)) throw new Error(`--cwd does not exist: ${cwd}`);
	const pluginRoot = options.manifestPath !== undefined
		? path.dirname(path.resolve(options.manifestPath))
		: options.installed
			? path.resolve(options.installedRoot ?? path.join(options.dshHome, 'codex-shared-skills'))
			: SCRIPT_DIR;
	const manifestPath = options.manifestPath !== undefined
		? path.resolve(options.manifestPath)
		: path.join(pluginRoot, 'manifest.json');
	const providerInPluginRoot = path.join(pluginRoot, 'provider.mjs');
	const providerPath = options.installed || existsSync(providerInPluginRoot)
		? providerInPluginRoot
		: path.join(SCRIPT_DIR, 'provider.mjs');
	for (const file of [manifestPath, providerPath]) {
		if (!existsSync(file)) {
			throw new Error(`${file} not found; generate the manifest or pass --manifest/--installed`);
		}
	}

	const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));
	if (typeof manifest !== 'object' || manifest === null || !Array.isArray(manifest.entries)) {
		throw new Error(`manifest ${manifestPath} must be a JSON object with an "entries" array`);
	}
	const projectSkills = Array.isArray(manifest.project_skills) ? manifest.project_skills : [];
	const disabled = Array.isArray(manifest.disabled) ? manifest.disabled : [];

	const requireFromDsh = createRequire(dshSkillPackageJson);
	const { Context } = requireFromDsh('@deepseek-ai/cordis');
	const { SkillRegistry } = await import(
		pathToFileURL(path.join(nodeModules, '@deepseek-ai/dsh-skill/lib/index.js')).href
	);
	const filesystem = await import(
		pathToFileURL(path.join(nodeModules, '@deepseek-ai/dsh-skill-filesystem/lib/index.js')).href
	);
	const shared = await import(pathToFileURL(providerPath).href);
	const ctx = new Context();
	const fibers = [];
	try {
		const registry = ctx.plugin(SkillRegistry);
		fibers.push(registry);
		await registry;
		const native = ctx.plugin(filesystem, { watch: false });
		fibers.push(native);
		await native;
		const imported = ctx.plugin(shared, { manifestPath });
		fibers.push(imported);
		await imported;
		const skills = await ctx.skills.list({ cwd });
		// Inspect the providers independently as the registry hides overridden candidates.
		const control = { invalidate() {}, signal: new AbortController().signal };
		const sharedProvider = new shared.CodexSkillProvider(ctx, control, { manifestPath });
		const nativeProvider = new filesystem.FileSystemSkillProvider(ctx, control, { watch: false });
		fibers.push(sharedProvider, nativeProvider);
		const sharedCandidates = await sharedProvider.list({ cwd });
		const projectByName = new Map();
		for (const item of projectSkills) {
			assert.equal(typeof item?.path, 'string', 'manifest project_skills entry is missing a path');
			const text = await readFile(item.path, 'utf8');
			const name = frontmatterName(text) ?? item.name;
			assert.equal(typeof name, 'string', `cannot determine the DSH name of project skill ${item.path}`);
			assert(!projectByName.has(name), `Duplicate declared native project skill: ${name}`);
			projectByName.set(name, { path: item.path, text });
		}
		const loaded = [];
		const shadowed = [];
		const disabledPaths = new Set();
		for (const item of disabled) {
			if (typeof item?.path !== 'string') continue;
			disabledPaths.add(await realpath(item.path).catch(() => path.resolve(item.path)));
		}
		const disabledNames = new Set(disabled.map((item) => item?.name).filter((name) => typeof name === 'string'));

		async function checkedNative(name, declared, actual) {
			assert(actual, `Native project skill ${name} was not discovered in ${cwd}`);
			const expectedPath = await realpath(declared.path);
			assert.equal(await realpath(actual.path), expectedPath, `Unexpected native project skill path: ${name}`);
			assert.equal(actual.provider, 'filesystem', `Unexpected native project skill provider: ${name}`);
			assert(['project-agents', 'project-dsh'].includes(actual.source), `Unexpected native project skill source: ${name}`);
			const expected = await nativeProvider.get({ locator: { path: declared.path, directory: path.dirname(declared.path) }, source: actual.source }, {});
			assert(expected, `Cannot load declared native project skill: ${name}`);
			assert.equal(expected.name, name, `Native project skill name mismatch: ${name}`);
			assert.equal(await realpath(expected.path), expectedPath, `Native project source mismatch: ${name}`);
			assert.equal(await realpath(actual.resourceBase.path), path.dirname(expectedPath), `Native resource base mismatch: ${name}`);
			// Compare byte-for-byte with the direct native-provider output; never trim either side.
			assert(actual.content === expected.content, `Native project body mismatch: ${name}`);
			return expected;
		}

		for (const item of manifest.entries) {
			assert.equal(typeof item?.name, 'string', 'manifest entry is missing a DSH name');
			assert.equal(typeof item?.path, 'string', `manifest entry ${item.name} is missing a source path`);
			const candidate = sharedCandidates.find((skill) => skill.name === item.name);
			assert(candidate, `Missing imported skill ${item.name}`);
			const sourceDefinition = await sharedProvider.get(candidate, { cwd });
			assert(sourceDefinition, `Cannot load shared source ${item.name}`);
			const text = await readFile(item.path, 'utf8');
			const body = text.replace(/^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, '');
			assert(sourceDefinition.content === body, `Body mismatch: ${item.name}`);
			const sourcePath = await realpath(item.path);
			assert.equal(await realpath(sourceDefinition.path), sourcePath, `Shared source path mismatch: ${item.name}`);
			assert.equal(sourceDefinition.provider, 'codex-shared-skills', `Shared provider mismatch: ${item.name}`);
			assert.equal(await realpath(sourceDefinition.resourceBase.path), path.dirname(sourcePath), `Resource base mismatch: ${item.name}`);
			assert(!disabledPaths.has(sourcePath), `Disabled Codex skill was imported: ${item.name}`);
			assert(item.codex_name === undefined || !disabledNames.has(item.codex_name), `Disabled Codex skill name was imported: ${item.name}`);
			assert(skills.find((skill) => skill.name === item.name), `Missing imported skill ${item.name}`);
			const definition = await ctx.skills.get(item.name, { cwd });
			assert(definition, `Cannot load skill ${item.name}`);
			const activePath = await realpath(definition.path);
			const sharedBodyHash = createHash('sha256').update(body).digest('hex');
			if (activePath === sourcePath) {
				assert.equal(definition.provider, 'codex-shared-skills', `Unexpected shared skill provider: ${item.name}`);
				assert(definition.content === body, `Body mismatch: ${item.name}`);
				assert.equal(await realpath(definition.resourceBase.path), path.dirname(sourcePath), `Resource base mismatch: ${item.name}`);
			} else {
				const declared = projectByName.get(item.name);
				assert(declared, `Undeclared skill shadows shared skill: ${item.name}`);
				const expected = await checkedNative(item.name, declared, definition);
				shadowed.push({ name: item.name, shared_path: item.path, active_path: definition.path,
					shared_body_sha256: sharedBodyHash,
					active_body_sha256: createHash('sha256').update(expected.content).digest('hex'),
					active_source_sha256: createHash('sha256').update(declared.text).digest('hex') });
			}
			loaded.push({ name: item.name, codex_name: item.codex_name, path: sourceDefinition.path, body_sha256: sharedBodyHash });
		}

		const nativeProjectSkills = [];
		for (const [name, declared] of projectByName) {
			const definition = await ctx.skills.get(name, { cwd });
			await checkedNative(name, declared, definition);
			nativeProjectSkills.push({ name, path: definition.path });
		}

		const report = {
			installed: options.installed,
			cwd,
			manifest: manifestPath,
			shared_skills_loaded: loaded.length,
			shared_skills_effective: loaded.length - shadowed.length,
			native_project_skills_checked: nativeProjectSkills.length,
			disabled_skills_checked: disabled.length,
			original_bodies_and_resource_paths_verified: true,
			disabled_skills_not_imported: true,
			native_project_skills: nativeProjectSkills,
			shadowed_shared_skills: shadowed,
			skills: loaded,
		};

		if (options.installed) {
			const agents = path.join(options.dshHome, 'AGENTS.md');
			if (!existsSync(agents)) throw new Error(`installed global instruction link is missing: ${agents}`);
			const info = await lstat(agents);
			assert(info.isSymbolicLink(), `${agents} must be a symlink`);
			const instructions = await import(
				pathToFileURL(path.join(nodeModules, '@deepseek-ai/dsh-agent-instructions/lib/index.js')).href
			);
			const baseline = await instructions.loadBaselineInstructions({ cwd, dshHome: options.dshHome, maxBytes: 65536 });
			const globalSource = (await readFile(agents, 'utf8')).trim();
			assert(baseline.text.includes(globalSource), 'Global instruction source was not loaded in full');
			report.global_instructions_loaded_in_full = true;
			const projectAgents = path.join(cwd, 'AGENTS.md');
			if (existsSync(projectAgents)) {
				const projectSource = (await readFile(projectAgents, 'utf8')).trim();
				assert(baseline.text.includes(projectSource), 'Project instruction source was not loaded in full');
				report.project_instructions_loaded_in_full = true;
			} else {
				report.project_instructions_loaded_in_full = null;
			}
		}

		await mkdir(path.join(SCRIPT_DIR, '.verification'), { recursive: true });
		const reportPath = path.join(
			SCRIPT_DIR,
			'.verification',
			`${options.installed ? 'installed' : 'staged'}-verification.json`,
		);
		await writeFile(reportPath, JSON.stringify(report, null, 2) + '\n');
		console.log(JSON.stringify({ ...report, skills: undefined }, null, 2));
	} finally {
		for (const fiber of fibers.reverse()) await fiber.dispose();
	}
}

main().catch((error) => {
	console.error(`verify_import: ${errorMessage(error)}`);
	process.exitCode = 1;
});