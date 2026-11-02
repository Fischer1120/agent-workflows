/**
 * Tests for the Codex shared skill provider.
 *
 * Run with: node --test test_provider.mjs
 *
 * The suite uses only temporary files and read-only access to the DSH packages
 * installed with DSH Desktop (real skill-service exports plus the yaml parser).
 * The DSH dependency root defaults to the macOS application path and can be
 * overridden with DSH_APP_ROOT or DSH_NODE_MODULES. It never touches the user's
 * Codex skills, profile, or installed packages.
 */
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { chmod, lstat, mkdir, mkdtemp, readdir, readFile, realpath, rmdir, stat, unlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
import test from 'node:test';

import { CodexSkillProvider, apply, inject, name as moduleName } from './provider.mjs';

const DEFAULT_DSH_APP_ROOT = '/Applications/DSH Desktop.app/Contents/Resources/app';
const DSH_APP_ROOT = process.env.DSH_APP_ROOT ?? DEFAULT_DSH_APP_ROOT;
const DSH_NODE_MODULES = process.env.DSH_NODE_MODULES ?? join(DSH_APP_ROOT, 'node_modules');
const DSH_SKILL_INDEX = join(DSH_NODE_MODULES, '@deepseek-ai/dsh-skill/lib/index.js');
if (!existsSync(DSH_SKILL_INDEX)) {
	throw new Error(
		`test_provider: cannot find ${DSH_SKILL_INDEX}; set DSH_NODE_MODULES or DSH_APP_ROOT to the installed DSH Desktop resources`,
	);
}
const dshSkill = await import(pathToFileURL(DSH_SKILL_INDEX).href);

const WATCH_INTERVAL_MS = 25;

/**
 * Create a temporary workspace removed after the test.
 * @param {import('node:test').TestContext} t - test context.
 * @returns {Promise<string>} absolute temp directory.
 */
async function makeRoot(t) {
	const root = await mkdtemp(join(tmpdir(), 'codex-shared-skills-'));
	t.after(async () => {
		assert.equal(dirname(await realpath(root)), await realpath(tmpdir()));
		assert.ok(root.startsWith(join(tmpdir(), 'codex-shared-skills-')));
		async function cleanDirectory(directory) {
			for (const entry of await readdir(directory)) {
				const target = join(directory, entry);
				assert.ok(target.startsWith(root + sep), 'cleanup must stay in this test fixture');
				const info = await lstat(target);
				if (info.isDirectory() && !info.isSymbolicLink()) {
					await cleanDirectory(target);
					await rmdir(target);
				} else {
					assert.ok(info.isFile() || info.isSymbolicLink(), 'unexpected fixture entry');
					await unlink(target);
				}
			}
		}
		await cleanDirectory(root);
		await rmdir(root);
	});
	return root;
}

/**
 * Write a SKILL.md at `<root>/<segments>/SKILL.md`.
 * @param {string} root - temp workspace.
 * @param {string[]} segments - directory segments under root.
 * @param {{ frontmatter: string, body: string }} content - frontmatter lines and verbatim body.
 * @returns {Promise<{ dir: string, path: string }>} skill directory and SKILL.md path.
 */
async function writeSkill(root, segments, { frontmatter, body }) {
	const dir = join(root, ...segments);
	await mkdir(dir, { recursive: true });
	const path = join(dir, 'SKILL.md');
	await writeSkillFile(path, frontmatter, body);
	return { dir, path };
}

/**
 * Overwrite an existing SKILL.md.
 * @param {string} path - SKILL.md path.
 * @param {string} frontmatter - frontmatter lines.
 * @param {string} body - verbatim body (including its leading newline).
 */
async function writeSkillFile(path, frontmatter, body) {
	await writeFile(path, `---\n${frontmatter}\n---\n${body}`, 'utf8');
}

/**
 * Write the manifest.
 * @param {string} root - temp workspace.
 * @param {Array<Record<string, unknown>>} entries - enabled global Codex skills.
 * @returns {Promise<string>} manifest path.
 */
async function writeManifest(root, entries) {
	const path = join(root, 'manifest.json');
	await writeFile(path, JSON.stringify({ entries }, null, 2), 'utf8');
	return path;
}

/**
 * Create a registry-like lifecycle control.
 * @returns {{ signal: AbortSignal, invalidations: number, invalidate: () => void, abort: (reason?: unknown) => void }} control.
 */
function createControl() {
	const controller = new AbortController();
	const control = {
		signal: controller.signal,
		invalidations: 0,
		invalidate() {
			control.invalidations += 1;
		},
		abort(reason) {
			controller.abort(reason);
		},
	};
	return control;
}

/**
 * Create a plugin-context stub with an optional filesystem service.
 * @param {{ fs?: object, warnings?: string[] }} [options] - fs service and warning sink.
 * @returns {{ warnings: string[], logger: { warn: (message: string) => void }, get: (name: string) => object | undefined }} context stub.
 */
function createCtx({ fs, warnings = [] } = {}) {
	return {
		warnings,
		logger: {
			warn(message) {
				warnings.push(String(message));
			},
		},
		get(name) {
			return name === 'fs' ? fs : undefined;
		},
	};
}

/**
 * Poll until a predicate holds.
 * @param {() => boolean} predicate - condition to observe.
 * @param {{ timeoutMs?: number, intervalMs?: number }} [options] - polling bounds.
 */
async function waitFor(predicate, { timeoutMs = 3000, intervalMs = 20 } = {}) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (predicate()) return;
		await sleep(intervalMs);
	}
	assert.fail(`condition was not met within ${timeoutMs}ms`);
}

/**
 * Wait for a fixed duration.
 * @param {number} ms - milliseconds.
 */
function sleep(ms) {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

test('exposes the DSH provider contract and registers through ctx.skills', async (t) => {
	const root = await makeRoot(t);
	const skill = await writeSkill(root, ['skills', 'alpha'], {
		frontmatter: 'name: Alpha\ndescription: contract skill',
		body: '\n# Alpha\n',
	});
	const manifestPath = await writeManifest(root, [{ name: 'shared-alpha', codex_name: 'Alpha', path: skill.path }]);

	assert.equal(moduleName, 'codex-shared-skills');
	assert.deepEqual(inject, ['skills']);
	assert.equal(typeof apply, 'function');
	assert.equal(typeof CodexSkillProvider, 'function');

	const warnings = [];
	let registration;
	const ctx = {
		warnings,
		logger: { warn: (message) => warnings.push(String(message)) },
		get: () => undefined,
		skills: {
			registerProvider(create) {
				const control = createControl();
				registration = { provider: create(control), control };
				return () => control.abort(new Error('unregistered'));
			},
		},
	};
	apply(ctx, { manifestPath, watchIntervalMs: WATCH_INTERVAL_MS });

	assert.ok(registration.provider instanceof CodexSkillProvider);
	assert.equal(registration.provider.name, moduleName);
	const candidates = await registration.provider.list();
	assert.deepEqual(candidates.map((candidate) => candidate.name), ['shared-alpha']);
	await registration.provider.dispose();
});

test('shares the original body and resource directory while adapting the manifest name', async (t) => {
	const root = await makeRoot(t);
	const skillA = await writeSkill(root, ['codex-skills', 'Example'], {
		frontmatter: 'name: Example\ndescription: First shared skill',
		body: '\n# Example body\n\nUse references/guide.md.\n',
	});
	const skillB = await writeSkill(root, ['codex-skills', 'Other'], {
		frontmatter: 'name: Example\ndescription: Second shared skill',
		body: '\n# Other body\n',
	});
	await mkdir(join(skillA.dir, 'references'), { recursive: true });
	await writeFile(join(skillA.dir, 'references', 'guide.md'), 'original guide', 'utf8');

	const manifestPath = await writeManifest(root, [
		{ name: 'codex-example', codex_name: 'Example', path: skillA.path },
		{ name: 'codex-example-two', codex_name: 'Example', path: skillB.path },
	]);
	const control = createControl();
	const provider = new CodexSkillProvider(createCtx(), control, { manifestPath, watchIntervalMs: WATCH_INTERVAL_MS });

	const sourceStatBefore = await stat(skillA.path);
	const candidates = await provider.list();
	assert.deepEqual(candidates.map((candidate) => candidate.name), ['codex-example', 'codex-example-two']);
	for (const candidate of candidates) {
		assert.ok(dshSkill.isSkillName(candidate.name), `${candidate.name} must satisfy the DSH name grammar`);
		assert.equal(candidate.rank, 300);
		assert.equal(candidate.source, 'codex-shared');
		assert.equal(candidate.provider, 'codex-shared-skills');
		assert.equal(typeof candidate.description, 'string');
		assert.ok(candidate.description.length > 0);
		assert.deepEqual(candidate.invocation, { modelInvocable: true, userInvocable: true });
		assert.equal(typeof candidate.locator, 'object');
		assert.equal(candidate.locator.name, candidate.name);
	}
	const first = candidates[0];
	assert.equal(first.description, 'First shared skill');
	assert.equal(first.locator.codexName, 'Example');
	assert.equal(first.locator.path, skillA.path);

	const realSkillPath = await realpath(skillA.path);
	const definition = await provider.get(first);
	assert.equal(definition.name, 'codex-example');
	assert.equal(definition.description, 'First shared skill');
	assert.equal(definition.source, 'codex-shared');
	assert.equal(definition.provider, 'codex-shared-skills');
	assert.equal(definition.path, realSkillPath);
	assert.deepEqual(definition.resourceBase, { kind: 'directory', path: dirname(realSkillPath) });
	assert.equal(definition.content, '\n# Example body\n\nUse references/guide.md.\n', 'body must be shared verbatim');
	assert.equal(dshSkill.isModelInvocable(definition), true);
	assert.equal(dshSkill.isUserInvocable(definition), true);
	const rendered = dshSkill.renderSkillContent(definition);
	assert.ok(rendered.includes('<skill_content name="codex-example">'));
	assert.ok(rendered.includes(dirname(realSkillPath)));
	assert.ok(rendered.includes(definition.content));

	assert.equal(
		await readFile(join(definition.resourceBase.path, 'references', 'guide.md'), 'utf8'),
		'original guide',
		'resource base must stay the original skill directory',
	);
	assert.equal((await stat(skillA.path)).mtimeMs, sourceStatBefore.mtimeMs, 'source file must not be modified');

	// list() and get() re-read the original file on every call.
	await writeSkillFile(
		skillA.path,
		'name: Example\ndescription: First shared skill v2',
		'\n# Example body v2\n\nStill shares references/guide.md.\n',
	);
	const refreshed = await provider.list();
	assert.equal(refreshed[0].description, 'First shared skill v2');
	const refreshedDefinition = await provider.get(refreshed[0]);
	assert.equal(refreshedDefinition.content, '\n# Example body v2\n\nStill shares references/guide.md.\n');
	assert.deepEqual(refreshedDefinition.resourceBase, definition.resourceBase);

	// Nothing was copied next to the manifest or into the workspace.
	const tree = (await readdir(root, { recursive: true })).map((entry) => entry.split(sep).join('/')).sort();
	assert.deepEqual(tree, [
		'codex-skills',
		'codex-skills/Example',
		'codex-skills/Example/SKILL.md',
		'codex-skills/Example/references',
		'codex-skills/Example/references/guide.md',
		'codex-skills/Other',
		'codex-skills/Other/SKILL.md',
		'manifest.json',
	]);
	await provider.dispose();
});

test('skips missing or invalid sources with warnings while valid entries survive', async (t) => {
	const root = await makeRoot(t);
	const valid = await writeSkill(root, ['skills', 'valid'], {
		frontmatter: 'name: Valid\ndescription: valid skill',
		body: '\n# Valid\n',
	});
	const duplicateTarget = await writeSkill(root, ['skills', 'duplicate'], {
		frontmatter: 'name: Valid\ndescription: duplicate target',
		body: '\n# Duplicate\n',
	});
	const noFrontmatter = await writeSkill(root, ['skills', 'nofront'], {
		frontmatter: 'name: NoFront\ndescription: x',
		body: '\n# NoFront\n',
	});
	await writeFile(noFrontmatter.path, '# plain markdown without frontmatter\n', 'utf8');
	const badYaml = await writeSkill(root, ['skills', 'badyaml'], {
		frontmatter: 'name: BadYaml\ndescription: x',
		body: '\n# BadYaml\n',
	});
	await writeFile(badYaml.path, '---\nname: [unclosed\n---\n\nbroken frontmatter\n', 'utf8');
	const noDescription = await writeSkill(root, ['skills', 'nodesc'], {
		frontmatter: 'name: NoDesc',
		body: '\n# NoDesc\n',
	});
	const noName = await writeSkill(root, ['skills', 'noname'], {
		frontmatter: 'description: missing source name', body: '\n# Missing name\n',
	});
	const badInvocation = await writeSkill(root, ['skills', 'badinvocation'], {
		frontmatter: 'name: BadInvocation\ndescription: bad invocation\ndisable-model-invocation: "yes"',
		body: '\n# BadInvocation\n',
	});
	const missingPath = join(root, 'skills', 'missing', 'SKILL.md');

	const manifestPath = await writeManifest(root, [
		{ name: 'shared-valid', codex_name: 'Valid', path: valid.path },
		{ name: 'shared-missing', codex_name: 'Missing', path: missingPath },
		{ name: 'shared-nofront', codex_name: 'NoFront', path: noFrontmatter.path },
		{ name: 'shared-badyaml', codex_name: 'BadYaml', path: badYaml.path },
		{ name: 'shared-nodesc', codex_name: 'NoDesc', path: noDescription.path },
		{ name: 'shared-noname', codex_name: 'NoName', path: noName.path },
		{ name: 'shared-badinvocation', codex_name: 'BadInvocation', path: badInvocation.path },
		{ name: 'Bad Name', codex_name: 'Bad', path: valid.path },
		{ name: 'shared-relative', codex_name: 'Relative', path: 'relative/SKILL.md' },
		{ name: 'shared-valid', codex_name: 'Valid', path: duplicateTarget.path },
		{ codex_name: 'NoName', path: valid.path },
		'not-an-object',
	]);
	const warnings = [];
	const ctx = createCtx({ warnings });
	const provider = new CodexSkillProvider(ctx, createControl(), { manifestPath, watchIntervalMs: WATCH_INTERVAL_MS });

	const candidates = await provider.list();
	assert.deepEqual(candidates.map((candidate) => candidate.name), ['shared-valid']);
	assert.equal(candidates[0].locator.path, valid.path, 'the first duplicate name wins');

	const expectWarning = (fragment) => {
		assert.ok(
			warnings.some((warning) => warning.includes(fragment)),
			`expected a warning containing ${JSON.stringify(fragment)}; got:\n${warnings.join('\n')}`,
		);
	};
	expectWarning('is missing');
	expectWarning('has no YAML frontmatter mapping');
	expectWarning('invalid YAML frontmatter');
	expectWarning('frontmatter requires a non-empty description');
	expectWarning('must be a boolean');
	expectWarning('invalid DSH skill name');
	expectWarning('path must be an absolute SKILL.md path');
	expectWarning('duplicate skill name');
	expectWarning('entry is not an object');
	await provider.dispose();
});

test('treats a missing manifest as no skills but surfaces malformed manifests', async (t) => {
	const root = await makeRoot(t);
	const manifestPath = join(root, 'manifest.json');
	const missing = new CodexSkillProvider(createCtx(), createControl(), { manifestPath, watchIntervalMs: WATCH_INTERVAL_MS });
	assert.deepEqual(await missing.list(), []);
	await missing.dispose();

	await writeFile(manifestPath, '{not json', 'utf8');
	const invalidJson = new CodexSkillProvider(createCtx(), createControl(), { manifestPath, watchIntervalMs: WATCH_INTERVAL_MS });
	await assert.rejects(invalidJson.list(), /not valid JSON/);
	await invalidJson.dispose();

	await writeFile(manifestPath, JSON.stringify({ noEntries: true }), 'utf8');
	const noEntries = new CodexSkillProvider(createCtx(), createControl(), { manifestPath, watchIntervalMs: WATCH_INTERVAL_MS });
	await assert.rejects(noEntries.list(), /"entries" array/);
	await noEntries.dispose();

	await writeFile(manifestPath, JSON.stringify({ entries: {} }), 'utf8');
	const wrongEntries = new CodexSkillProvider(createCtx(), createControl(), { manifestPath, watchIntervalMs: WATCH_INTERVAL_MS });
	await assert.rejects(wrongEntries.list(), /"entries" array/);
	await wrongEntries.dispose();
});

test('reads through ctx.get("fs") and reports non-absence I/O errors instead of returning empty', async (t) => {
	const root = await makeRoot(t);
	const skillPath = join(root, 'skills', 'fs-skill', 'SKILL.md');
	const manifestPath = join(root, 'manifest.json');
	const manifestText = JSON.stringify({
		entries: [{ name: 'shared-fs', codex_name: 'FsSkill', path: skillPath }],
	});
	const skillText = '---\nname: FsSkill\ndescription: shared through ctx.fs\n---\n\n# FsSkill\n';
	const files = new Map([
		[manifestPath, manifestText],
		[skillPath, skillText],
	]);
	const calls = [];
	let mode = 'ok';
	const ioError = Object.assign(new Error('simulated disk failure'), { code: 'FS_IO_ERROR' });
	const deniedError = Object.assign(new Error('simulated permission denial'), { code: 'FS_PERMISSION_DENIED' });
	const fakeFs = {
		async resolve(path) {
			calls.push(['resolve', path]);
			if (mode === 'absent' && path !== manifestPath) throw Object.assign(new Error('not found'), { code: 'FS_NOT_FOUND' });
			if (mode === 'denied' && path === skillPath) throw deniedError;
			return { targetKey: path, displayPath: path };
		},
		async stat(target) {
			calls.push(['stat', target.targetKey]);
			const content = files.get(target.targetKey);
			if (content === undefined) return undefined;
			return { type: 'file', size: content.length, version: 'test' };
		},
		processPath(target) {
			return target.targetKey;
		},
		async readText(target) {
			calls.push(['readText', target.targetKey]);
			if (mode === 'io') throw ioError;
			return files.get(target.targetKey);
		},
	};
	const provider = new CodexSkillProvider(createCtx({ fs: fakeFs }), createControl(), {
		manifestPath,
		watchIntervalMs: WATCH_INTERVAL_MS,
	});

	const candidates = await provider.list();
	assert.deepEqual(candidates.map((candidate) => candidate.name), ['shared-fs']);
	assert.equal(candidates[0].description, 'shared through ctx.fs');
	assert.ok(calls.some(([verb, path]) => verb === 'readText' && path === manifestPath));
	assert.ok(calls.some(([verb, path]) => verb === 'readText' && path === skillPath));
	const definition = await provider.get(candidates[0]);
	assert.equal(definition.content, '\n# FsSkill\n');

	mode = 'absent';
	assert.deepEqual(await provider.list(), [], 'FS_NOT_FOUND must be treated as a missing source');
	mode = 'io';
	await assert.rejects(provider.list(), (error) => error.code === 'FS_IO_ERROR');
	mode = 'denied';
	await assert.rejects(provider.list(), (error) => error.code === 'FS_PERMISSION_DENIED');
	await provider.dispose();
});

test('honors strict frontmatter invocation controls and openai.yaml allow_implicit_invocation=false', async (t) => {
	const root = await makeRoot(t);
	const modelOff = await writeSkill(root, ['skills', 'model-off'], {
		frontmatter: 'name: ModelOff\ndescription: model off\ndisable-model-invocation: true',
		body: '\n# ModelOff\n',
	});
	const userOff = await writeSkill(root, ['skills', 'user-off'], {
		frontmatter: 'name: UserOff\ndescription: user off\nuser-invocable: false',
		body: '\n# UserOff\n',
	});
	const sidecarOff = await writeSkill(root, ['skills', 'sidecar-off'], {
		frontmatter: 'name: SidecarOff\ndescription: sidecar off',
		body: '\n# SidecarOff\n',
	});
	await mkdir(join(sidecarOff.dir, 'agents'), { recursive: true });
	const sidecarPath = join(sidecarOff.dir, 'agents', 'openai.yaml');
	await writeFile(sidecarPath, 'policy:\n  allow_implicit_invocation: false\n', 'utf8');
	const bothOff = await writeSkill(root, ['skills', 'both-off'], {
		frontmatter: 'name: BothOff\ndescription: both off\ndisable-model-invocation: true',
		body: '\n# BothOff\n',
	});
	await mkdir(join(bothOff.dir, 'agents'), { recursive: true });
	await writeFile(join(bothOff.dir, 'agents', 'openai.yaml'), 'policy:\n  allow_implicit_invocation: true\n', 'utf8');
	const frontmatterOn = await writeSkill(root, ['skills', 'frontmatter-on'], {
		frontmatter: 'name: FrontmatterOn\ndescription: frontmatter on\ndisable-model-invocation: false',
		body: '\n# FrontmatterOn\n',
	});
	await mkdir(join(frontmatterOn.dir, 'agents'), { recursive: true });
	await writeFile(join(frontmatterOn.dir, 'agents', 'openai.yaml'), 'policy:\n  allow_implicit_invocation: false\n', 'utf8');

	const manifestPath = await writeManifest(root, [
		{ name: 'shared-model-off', path: modelOff.path },
		{ name: 'shared-user-off', path: userOff.path },
		{ name: 'shared-sidecar-off', path: sidecarOff.path },
		{ name: 'shared-both-off', path: bothOff.path },
		{ name: 'shared-frontmatter-on', path: frontmatterOn.path },
	]);
	const provider = new CodexSkillProvider(createCtx(), createControl(), { manifestPath, watchIntervalMs: WATCH_INTERVAL_MS });

	const candidates = await provider.list();
	const byName = new Map(candidates.map((candidate) => [candidate.name, candidate]));
	assert.deepEqual(byName.get('shared-model-off').invocation, { modelInvocable: false, userInvocable: true });
	assert.deepEqual(byName.get('shared-user-off').invocation, { modelInvocable: true, userInvocable: false });
	assert.deepEqual(byName.get('shared-sidecar-off').invocation, { modelInvocable: false, userInvocable: true });
	assert.deepEqual(
		byName.get('shared-both-off').invocation,
		{ modelInvocable: false, userInvocable: true },
		'allow_implicit_invocation=true must not re-enable a frontmatter-disabled skill',
	);
	assert.deepEqual(
		byName.get('shared-frontmatter-on').invocation,
		{ modelInvocable: false, userInvocable: true },
		'allow_implicit_invocation=false must still disable model invocation',
	);

	for (const candidate of candidates) {
		const definition = await provider.get(candidate);
		assert.deepEqual(definition.invocation, candidate.invocation);
	}
	const sidecarDefinition = await provider.get(byName.get('shared-sidecar-off'));
	assert.ok(!sidecarDefinition.content.includes('allow_implicit_invocation'), 'sidecar content must not be copied into the body');

	await writeFile(sidecarPath, 'policy:\n  allow_implicit_invocation: true\n', 'utf8');
	let refreshed = await provider.list();
	assert.deepEqual(
		refreshed.find((candidate) => candidate.name === 'shared-sidecar-off').invocation,
		{ modelInvocable: true, userInvocable: true },
	);
	await unlink(sidecarPath);
	refreshed = await provider.list();
	assert.deepEqual(
		refreshed.find((candidate) => candidate.name === 'shared-sidecar-off').invocation,
		{ modelInvocable: true, userInvocable: true },
	);

	const warnings = [];
	const warningCtx = createCtx({ warnings });
	const invalidSidecar = await provider.list();
	assert.ok(invalidSidecar.length > 0);
	await writeFile(sidecarPath, 'policy: [\n', 'utf8');
	const invalidProvider = new CodexSkillProvider(warningCtx, createControl(), {
		manifestPath,
		watchIntervalMs: WATCH_INTERVAL_MS,
	});
	const invalidCandidates = await invalidProvider.list();
	assert.deepEqual(
		invalidCandidates.find((candidate) => candidate.name === 'shared-sidecar-off').invocation,
		{ modelInvocable: false, userInvocable: true },
		'a malformed sidecar must fail closed for model invocation',
	);
	assert.ok(warnings.some((warning) => warning.includes('model invocation is disabled')));
	await invalidProvider.dispose();
	await provider.dispose();
});

test('a non-text or non-file sidecar cannot enable implicit invocation', async (t) => {
	const root = await makeRoot(t);
	const skillPath = join(root, 'skill', 'SKILL.md');
	const sidecarPath = join(root, 'skill', 'agents', 'openai.yaml');
	const manifestPath = join(root, 'manifest.json');
	const files = new Map([
		[manifestPath, JSON.stringify({ entries: [{ name: 'shared-policy', path: skillPath }] })],
		[skillPath, '---\nname: policy\ndescription: policy test\n---\nbody'],
		[sidecarPath, 'policy:\n  allow_implicit_invocation: false\n'],
	]);
	let sidecarType = 'file';
	let unreadable = false;
	const fakeFs = {
		async resolve(file) { return { targetKey: file }; },
		async stat(target) { return files.has(target.targetKey) ? { type: target.targetKey === sidecarPath ? sidecarType : 'file' } : undefined; },
		processPath(target) { return target.targetKey; },
		async readText(target) {
			if (unreadable && target.targetKey === sidecarPath) throw Object.assign(new Error('non-text policy'), { code: 'FS_NOT_TEXT' });
			return files.get(target.targetKey);
		},
	};
	const provider = new CodexSkillProvider(createCtx({ fs: fakeFs }), createControl(), { manifestPath });
	t.after(() => provider.dispose());
	const [candidate] = await provider.list();
	assert.equal(candidate.invocation.modelInvocable, false);
	unreadable = true;
	await assert.rejects(provider.list(), { code: 'FS_NOT_TEXT' });
	await assert.rejects(provider.get(candidate), { code: 'FS_NOT_TEXT' });
	unreadable = false;
	sidecarType = 'directory';
	await assert.rejects(provider.list(), /expected a regular text file/);
});

test('reports non-absence read failures from the Node fallback', async (t) => {
	if (typeof process.getuid === 'function' && process.getuid() === 0) {
		t.skip('permission-based failure is not observable as root');
		return;
	}
	const root = await makeRoot(t);
	const lockedDir = join(root, 'skills', 'locked');
	await mkdir(lockedDir, { recursive: true });
	await writeFile(join(lockedDir, 'SKILL.md'), '---\nname: Locked\ndescription: locked\n---\n\n# Locked\n', 'utf8');
	const manifestPath = await writeManifest(root, [
		{ name: 'shared-locked', codex_name: 'Locked', path: join(lockedDir, 'SKILL.md') },
	]);
	await chmod(lockedDir, 0o000);
	const provider = new CodexSkillProvider(createCtx(), createControl(), { manifestPath, watchIntervalMs: WATCH_INTERVAL_MS });
	try {
		await assert.rejects(provider.list(), (error) => error.code === 'EACCES' || error.code === 'EPERM');
	} finally {
		await chmod(lockedDir, 0o755);
	}
	await provider.dispose();
});

test('invalidates on manifest, source, and sidecar changes, refreshes watches, and stops on dispose/abort', async (t) => {
	const root = await makeRoot(t);
	const skillA = await writeSkill(root, ['skills', 'alpha'], {
		frontmatter: 'name: Alpha\ndescription: alpha v1',
		body: '\n# Alpha v1\n',
	});
	const entryA = { name: 'shared-alpha', codex_name: 'Alpha', path: skillA.path };
	const manifestPath = await writeManifest(root, [entryA]);
	const control = createControl();
	const provider = new CodexSkillProvider(createCtx(), control, { manifestPath, watchIntervalMs: WATCH_INTERVAL_MS });

	await provider.list();
	await sleep(150);
	assert.equal(control.invalidations, 0, 'absent sidecars must not invalidate on initial watch registration');
	await provider.list();
	await sleep(100);
	assert.equal(control.invalidations, 0, 'repeated list() must not invalidate by itself');

	// Source SKILL.md edits invalidate.
	await writeSkillFile(skillA.path, 'name: Alpha\ndescription: alpha v2', '\n# Alpha v2\n');
	await waitFor(() => control.invalidations >= 1);

	// Creating agents/openai.yaml invalidates even though it did not exist at list time.
	let observed = control.invalidations;
	await mkdir(join(skillA.dir, 'agents'), { recursive: true });
	await writeFile(join(skillA.dir, 'agents', 'openai.yaml'), 'policy:\n  allow_implicit_invocation: false\n', 'utf8');
	await waitFor(() => control.invalidations > observed);
	const afterSidecar = control.invalidations;
	assert.equal((await provider.list())[0].invocation.modelInvocable, false);

	// A manifest edit registers the newly enabled source.
	const skillB = await writeSkill(root, ['skills', 'beta'], {
		frontmatter: 'name: Beta\ndescription: beta v1',
		body: '\n# Beta v1\n',
	});
	const entryB = { name: 'shared-beta', codex_name: 'Beta', path: skillB.path };
	await writeManifest(root, [entryA, entryB]);
	await waitFor(() => control.invalidations > afterSidecar);
	observed = control.invalidations;
	const two = await provider.list();
	assert.deepEqual(two.map((candidate) => candidate.name), ['shared-alpha', 'shared-beta']);
	await writeSkillFile(skillB.path, 'name: Beta\ndescription: beta v2', '\n# Beta v2\n');
	await waitFor(() => control.invalidations > observed, { timeoutMs: 3000 });

	// Removing the entry drops its watch on the next list().
	observed = control.invalidations;
	await writeManifest(root, [entryA]);
	await waitFor(() => control.invalidations > observed);
	observed = control.invalidations;
	const one = await provider.list();
	assert.deepEqual(one.map((candidate) => candidate.name), ['shared-alpha']);
	await sleep(250);
	const settled = control.invalidations;
	assert.ok(settled >= observed);
	await writeSkillFile(skillB.path, 'name: Beta\ndescription: beta v3', '\n# Beta v3\n');
	await sleep(250);
	assert.equal(control.invalidations, settled, 'removed sources must not keep invalidating');

	// dispose() closes every watcher.
	await provider.dispose();
	await sleep(120);
	const afterDispose = control.invalidations;
	await writeSkillFile(skillA.path, 'name: Alpha\ndescription: alpha v3', '\n# Alpha v3\n');
	await sleep(250);
	assert.equal(control.invalidations, afterDispose, 'dispose() must stop all watchers');

	// aborting the provider lifecycle closes every watcher too.
	const control2 = createControl();
	const provider2 = new CodexSkillProvider(createCtx(), control2, { manifestPath, watchIntervalMs: WATCH_INTERVAL_MS });
	await provider2.list();
	control2.abort(new Error('test teardown'));
	await sleep(120);
	const afterAbort = control2.invalidations;
	await writeSkillFile(skillA.path, 'name: Alpha\ndescription: alpha v4', '\n# Alpha v4\n');
	await sleep(250);
	assert.equal(control2.invalidations, afterAbort, 'signal abort must stop all watchers');
	await provider2.dispose();
});
