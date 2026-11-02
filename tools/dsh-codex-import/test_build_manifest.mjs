/**
 * Tests for build_manifest.mjs.
 *
 * Run with: node --test test_build_manifest.mjs
 *
 * Every test uses temporary catalog, skill, and output files. The catalog-only
 * test explicitly points DSH_NODE_MODULES at a missing directory to prove that
 * no YAML dependency is loaded without --desktop-skills.
 */
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdir, mkdtemp, readFile, realpath, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import test from 'node:test';

const execFileAsync = promisify(execFile);
const SCRIPT = path.join(path.dirname(fileURLToPath(import.meta.url)), 'build_manifest.mjs');
const DEFAULT_DSH_APP_ROOT = '/Applications/DSH Desktop.app/Contents/Resources/app';
const DSH_NODE_MODULES = process.env.DSH_NODE_MODULES ?? path.join(process.env.DSH_APP_ROOT ?? DEFAULT_DSH_APP_ROOT, 'node_modules');

/**
 * Create a temporary workspace removed after the test.
 * @param {import('node:test').TestContext} t - test context.
 * @returns {Promise<string>} absolute temp directory.
 */
async function makeRoot(t) {
	const root = await mkdtemp(path.join(tmpdir(), 'build-manifest-'));
	t.after(async () => {
		await rm(root, { recursive: true, force: true });
	});
	return root;
}

/**
 * Write a SKILL.md at `<root>/<segments>/SKILL.md`.
 * @param {string} root - temp workspace.
 * @param {string[]} segments - directory segments below root.
 * @param {string} frontmatter - YAML frontmatter lines.
 * @returns {Promise<{ dir: string, path: string }>} skill directory and file path.
 */
async function writeSkill(root, segments, frontmatter) {
	const dir = path.join(root, ...segments);
	await mkdir(dir, { recursive: true });
	const skillPath = path.join(dir, 'SKILL.md');
	await writeFile(skillPath, `---\n${frontmatter}\n---\n\n# Body\n`, 'utf8');
	return { dir, path: skillPath };
}

/**
 * Write a Codex catalog JSON.
 * @param {string} root - temp workspace.
 * @param {Array<Record<string, unknown>>} skills - flattened catalog skills.
 * @returns {Promise<string>} catalog path.
 */
async function writeCatalog(root, skills) {
	const catalogPath = path.join(root, 'codex-catalog.json');
	await writeFile(catalogPath, JSON.stringify({ data: [{ cwd: root, skills }] }, null, 2), 'utf8');
	return catalogPath;
}

/**
 * Run build_manifest.mjs.
 * @param {string[]} args - CLI arguments.
 * @param {NodeJS.ProcessEnv} [env] - extra environment.
 * @returns {Promise<{ stdout: string, stderr: string }>} command output.
 */
async function run(args, env = {}) {
	return execFileAsync(process.execPath, [SCRIPT, ...args], {
		encoding: 'utf8',
		maxBuffer: 10 * 1024 * 1024,
		env: { ...process.env, ...env },
	});
}

test('catalog-only build ignores disabled skills, keeps repo skills native, and needs no YAML', async (t) => {
	const root = await makeRoot(t);
	const alpha = await writeSkill(root, ['codex', 'Alpha'], 'name: Alpha\ndescription: alpha skill');
	const beta = await writeSkill(root, ['codex', 'Beta'], 'name: Beta\ndescription: beta skill');
	const repo = await writeSkill(root, ['project', '.agents', 'skills', 'repo-skill'], 'name: repo-skill\ndescription: repo skill');
	const disabled = await writeSkill(root, ['codex', 'Hatch Pet'], 'name: Hatch Pet\ndescription: disabled skill');
	const catalogPath = await writeCatalog(root, [
		{ name: 'Alpha', path: alpha.path, enabled: true, scope: 'global' },
		{ name: 'Beta', path: beta.path, enabled: true, scope: 'global' },
		{ name: 'repo-skill', path: repo.path, enabled: true, scope: 'repo' },
		{ name: 'Hatch Pet', path: disabled.path, enabled: false, scope: 'global' },
	]);
	const outputPath = path.join(root, 'out', 'manifest.json');
	const { stdout, stderr } = await run(
		['--catalog', catalogPath, '--output', outputPath],
		{ DSH_NODE_MODULES: path.join(root, 'missing-node-modules'), DSH_YAML_PACKAGE_JSON: '' },
	);
	assert.equal(stderr, '');
	const summary = JSON.parse(stdout);
	assert.equal(summary.shared_global_skills, 2);
	assert.deepEqual(summary.native_project_skills, ['repo-skill']);
	assert.deepEqual(summary.disabled_skills, ['Hatch Pet']);

	const manifest = JSON.parse(await readFile(outputPath, 'utf8'));
	assert.equal(manifest.version, 1);
	assert.equal(typeof manifest.generated_at, 'string');
	assert.deepEqual(manifest.entries.map((entry) => entry.name), ['alpha', 'beta']);
	assert.deepEqual(manifest.entries.map((entry) => entry.codex_name), ['Alpha', 'Beta']);
	assert.equal(manifest.entries[0].path, await realpath(alpha.path));
	assert.equal(manifest.project_skills.length, 1);
	assert.equal(manifest.project_skills[0].loading, 'DSH native project discovery');
	assert.deepEqual(manifest.disabled.map((item) => item.name), ['Hatch Pet']);
	assert.ok(!manifest.entries.some((entry) => entry.path === manifest.project_skills[0].path));
	if (process.platform !== 'win32') {
		assert.equal((await stat(outputPath)).mode & 0o777, 0o600, 'manifest must be mode 0600');
	}
});

test('desktop skill roots use the plugin namespace or an explicit NAMESPACE= prefix', async (t) => {
	const root = await makeRoot(t);
	const chart = await writeSkill(
		root,
		['plugins', 'data-analytics', '1.0.0', 'skills', 'chart'],
		'name: chart\ndescription: charts',
	);
	const skillsRoot = path.join(root, 'plugins', 'data-analytics', '1.0.0', 'skills');
	const catalogPath = await writeCatalog(root, []);

	const defaultOutput = path.join(root, 'default.json');
	await run(
		['--catalog', catalogPath, '--output', defaultOutput, '--desktop-skills', skillsRoot],
		{ DSH_NODE_MODULES },
	);
	const defaultManifest = JSON.parse(await readFile(defaultOutput, 'utf8'));
	assert.deepEqual(defaultManifest.entries, [
		{
			name: 'data-analytics-chart',
			codex_name: 'data-analytics:chart',
			path: await realpath(chart.path),
		},
	]);

	const namedOutput = path.join(root, 'named.json');
	await run(
		['--catalog', catalogPath, '--output', namedOutput, '--desktop-skills', `custom=${skillsRoot}`],
		{ DSH_NODE_MODULES },
	);
	const namedManifest = JSON.parse(await readFile(namedOutput, 'utf8'));
	assert.equal(namedManifest.entries[0].name, 'custom-chart');
	assert.equal(namedManifest.entries[0].codex_name, 'custom:chart');
});

test('desktop skill roots without a configured DSH parser fail with actionable guidance', async (t) => {
	const root = await makeRoot(t);
	await writeSkill(root, ['skills', 'chart'], 'name: chart\ndescription: charts');
	const skillsRoot = path.join(root, 'skills');
	const catalogPath = await writeCatalog(root, []);
	await assert.rejects(
		run(
			['--catalog', catalogPath, '--output', path.join(root, 'manifest.json'), '--desktop-skills', skillsRoot],
			{ DSH_NODE_MODULES: path.join(root, 'missing-node-modules'), DSH_YAML_PACKAGE_JSON: '' },
		),
		(error) => {
			assert.notEqual(error.code, 0);
			assert.match(error.stderr, /DSH_NODE_MODULES|DSH_APP_ROOT/);
			return true;
		},
	);
});

test('missing catalog and conflicting aliases fail with clear errors', async (t) => {
	const root = await makeRoot(t);
	await assert.rejects(
		run(['--catalog', path.join(root, 'absent.json'), '--output', path.join(root, 'manifest.json')]),
		(error) => {
			assert.notEqual(error.code, 0);
			assert.match(error.stderr, /cannot read catalog/);
			return true;
		},
	);

	const first = await writeSkill(root, ['codex', 'first'], 'name: a:b\ndescription: first');
	const second = await writeSkill(root, ['codex', 'second'], 'name: a-b\ndescription: second');
	const catalogPath = await writeCatalog(root, [
		{ name: 'a:b', path: first.path, enabled: true, scope: 'global' },
		{ name: 'a-b', path: second.path, enabled: true, scope: 'global' },
	]);
	await assert.rejects(
		run(['--catalog', catalogPath, '--output', path.join(root, 'manifest.json')]),
		(error) => {
			assert.notEqual(error.code, 0);
			assert.match(error.stderr, /conflicting DSH skill name/);
			return true;
		},
	);

	await assert.rejects(run(['--bogus']), (error) => {
		assert.notEqual(error.code, 0);
		assert.match(error.stderr, /unknown option/);
		return true;
	});
});

test('help documents explicit inputs and environment overrides', async () => {
	const { stdout } = await run(['--help']);
	assert.match(stdout, /--catalog PATH/);
	assert.match(stdout, /--desktop-skills/);
	assert.match(stdout, /DSH_NODE_MODULES/);
	assert.match(stdout, /mode 0600/);
});