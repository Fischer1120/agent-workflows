/**
 * Tests for verify_import.mjs.
 *
 * Run with: node --test test_verify.mjs
 *
 * The manifest, provider copy, Codex skills, and project directory all live in
 * a temporary directory. No real DSH profile, Codex home, or project is read or
 * written; only the ignored .verification report next to the script is updated.
 */
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { copyFile, mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import test from 'node:test';

const execFileAsync = promisify(execFile);
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const SCRIPT = path.join(SCRIPT_DIR, 'verify_import.mjs');

/**
 * Create a temporary workspace removed after the test.
 * @param {import('node:test').TestContext} t - test context.
 * @returns {Promise<string>} absolute temp directory.
 */
async function makeRoot(t) {
	const root = await mkdtemp(path.join(tmpdir(), 'verify-import-'));
	t.after(async () => {
		await rm(root, { recursive: true, force: true });
	});
	return root;
}

/**
 * Write a SKILL.md at `<root>/<segments>/SKILL.md`.
 * @param {string} root - temp workspace.
 * @param {string[]} segments - directory segments below root.
 * @param {string} name - frontmatter skill name.
 * @returns {Promise<string>} SKILL.md path.
 */
async function writeSkill(root, segments, name) {
	const dir = path.join(root, ...segments);
	await mkdir(dir, { recursive: true });
	const skillPath = path.join(dir, 'SKILL.md');
	await writeFile(skillPath, `---\nname: ${name}\ndescription: ${name} test skill\n---\n\n# ${name}\n`, 'utf8');
	return skillPath;
}

/**
 * Run verify_import.mjs.
 * @param {string[]} args - CLI arguments.
 * @returns {Promise<{ stdout: string, stderr: string }>} command output.
 */
async function run(args, env = {}) {
	return execFileAsync(process.execPath, [SCRIPT, ...args], {
		encoding: 'utf8',
		env: { ...process.env, ...env },
		maxBuffer: 10 * 1024 * 1024,
	});
}

test('verifies only the entries, repo skills, and disabled paths the manifest declares', async (t) => {
	const root = await makeRoot(t);
	const pluginRoot = path.join(root, 'plugin');
	await mkdir(pluginRoot, { recursive: true });
	await copyFile(path.join(SCRIPT_DIR, 'provider.mjs'), path.join(pluginRoot, 'provider.mjs'));
	const sharedSkill = await writeSkill(root, ['codex', 'shared'], 'shared-demo');
	const disabledSkill = await writeSkill(root, ['codex', 'hatch'], 'Hatch Pet');
	const nativeSkill = await writeSkill(root, ['project', '.agents', 'skills', 'native-demo'], 'native-demo');
	const manifestPath = path.join(pluginRoot, 'manifest.json');
	await writeFile(
		manifestPath,
		JSON.stringify(
			{
				version: 1,
				project_skills: [{ name: 'native-demo', path: nativeSkill }],
				disabled: [{ name: 'Hatch Pet', path: disabledSkill }],
				entries: [{ name: 'shared-demo', codex_name: 'shared-demo', path: sharedSkill }],
			},
			null,
			2,
		),
		'utf8',
	);

	const { stdout } = await run(['--cwd', path.join(root, 'project'), '--manifest', manifestPath]);
	const report = JSON.parse(stdout);
	assert.equal(report.shared_skills_loaded, 1);
	assert.equal(report.native_project_skills_checked, 1, 'declared repo skill must be checked');
	assert.equal(report.disabled_skills_checked, 1);
	assert.equal(report.original_bodies_and_resource_paths_verified, true);
	assert.equal(report.disabled_skills_not_imported, true);
	assert.equal(report.native_project_skills[0].name, 'native-demo');

	const written = JSON.parse(await readFile(path.join(SCRIPT_DIR, '.verification', 'staged-verification.json'), 'utf8'));
	assert.equal(written.manifest, manifestPath);
	assert.equal(written.native_project_skills_checked, 1);
});

test('verifies both sources when a declared project skill shadows a shared skill', async (t) => {
	const root = await makeRoot(t);
	const project = path.join(root, 'project');
	const shared = await writeSkill(root, ['codex', 'shared'], 'same-name');
	const native = await writeSkill(root, ['project', '.agents', 'skills', 'native'], 'same-name');
	await writeFile(shared, '---\nname: same-name\ndescription: shared\n---\n\n# Shared unique body\n\n');
	await writeFile(native, '---\nname: same-name\ndescription: native\n---\n\n# Native unique body\n\n');
	const manifest = path.join(root, 'manifest.json');
	await writeFile(manifest, JSON.stringify({ entries: [{ name: 'same-name', path: shared }], project_skills: [{ name: 'same-name', path: native }] }));
	const { stdout } = await run(['--cwd', project, '--manifest', manifest]);
	const report = JSON.parse(stdout);
	assert.equal(report.shared_skills_loaded, 1);
	assert.equal(report.shared_skills_effective, 0);
	assert.equal(report.shadowed_shared_skills.length, 1);
	assert.equal(report.shadowed_shared_skills[0].name, 'same-name');
	assert.equal(report.shadowed_shared_skills[0].shared_path, shared);
	assert.equal(await realpath(report.shadowed_shared_skills[0].active_path), await realpath(native));
	assert.notEqual(report.shadowed_shared_skills[0].shared_body_sha256, report.shadowed_shared_skills[0].active_body_sha256);
});

test('rejects an undeclared project skill shadowing a shared skill', async (t) => {
	const root = await makeRoot(t);
	const shared = await writeSkill(root, ['codex', 'shared'], 'same-name');
	await writeSkill(root, ['project', '.agents', 'skills', 'native'], 'same-name');
	const manifest = path.join(root, 'manifest.json');
	await writeFile(manifest, JSON.stringify({ entries: [{ name: 'same-name', path: shared }] }));
	await assert.rejects(run(['--cwd', path.join(root, 'project'), '--manifest', manifest]), (error) => {
		assert.match(error.stderr, /undeclared|unexpected/i);
		return true;
	});
});

test('rejects an active project source that differs from the declared project source', async (t) => {
	const root = await makeRoot(t);
	const shared = await writeSkill(root, ['codex', 'shared'], 'same-name');
	await writeSkill(root, ['project', '.agents', 'skills', 'native'], 'same-name');
	const declared = await writeSkill(root, ['other', 'native'], 'same-name');
	const manifest = path.join(root, 'manifest.json');
	await writeFile(manifest, JSON.stringify({ entries: [{ name: 'same-name', path: shared }], project_skills: [{ name: 'same-name', path: declared }] }));
	await assert.rejects(run(['--cwd', path.join(root, 'project'), '--manifest', manifest]), (error) => {
		assert.match(error.stderr, /undeclared|unexpected|mismatch/i);
		return true;
	});
});

test('rejects a user-level skill declared as a project skill', async (t) => {
	const root = await makeRoot(t);
	const user = await writeSkill(root, ['user-dsh', 'skills', 'native'], 'same-name');
	const project = path.join(root, 'project');
	await mkdir(project);
	const manifest = path.join(root, 'manifest.json');
	await writeFile(manifest, JSON.stringify({ entries: [], project_skills: [{ name: 'same-name', path: user }] }));
	await assert.rejects(run(['--cwd', project, '--manifest', manifest], { DSH_HOME: path.join(root, 'user-dsh') }), (error) => {
		assert.match(error.stderr, /Unexpected native project skill source/);
		return true;
	});
});

test('detects a changed shared-provider body even when a declared native skill shadows it', async (t) => {
	const root = await makeRoot(t);
	const shared = await writeSkill(root, ['codex', 'shared'], 'same-name');
	const native = await writeSkill(root, ['project', '.agents', 'skills', 'native'], 'same-name');
	const provider = await readFile(path.join(SCRIPT_DIR, 'provider.mjs'), 'utf8');
	assert.match(provider, /content: loaded\.content,/);
	await writeFile(path.join(root, 'provider.mjs'), provider.replace('content: loaded.content,', "content: loaded.content + ' ',"));
	const manifest = path.join(root, 'manifest.json');
	await writeFile(manifest, JSON.stringify({ entries: [{ name: 'same-name', path: shared }], project_skills: [{ name: 'same-name', path: native }] }));
	await assert.rejects(run(['--cwd', path.join(root, 'project'), '--manifest', manifest]), (error) => {
		assert.match(error.stderr, /Body mismatch/);
		return true;
	});
});

test('falls back to the script provider when --manifest points outside the plugin directory', async (t) => {
	const root = await makeRoot(t);
	const sharedSkill = await writeSkill(root, ['codex', 'shared'], 'shared-demo');
	const manifestPath = path.join(root, 'manifests', 'manifest.json');
	await mkdir(path.dirname(manifestPath), { recursive: true });
	await writeFile(
		manifestPath,
		JSON.stringify({ entries: [{ name: 'shared-demo', codex_name: 'shared-demo', path: sharedSkill }] }),
		'utf8',
	);
	const project = path.join(root, 'project');
	await mkdir(project);
	const { stdout } = await run(['--cwd', project, '--manifest', manifestPath]);
	const report = JSON.parse(stdout);
	assert.equal(report.shared_skills_loaded, 1);
});

test('fails when a declared shared skill is missing', async (t) => {
	const root = await makeRoot(t);
	const pluginRoot = path.join(root, 'plugin');
	await mkdir(pluginRoot, { recursive: true });
	await copyFile(path.join(SCRIPT_DIR, 'provider.mjs'), path.join(pluginRoot, 'provider.mjs'));
	const manifestPath = path.join(pluginRoot, 'manifest.json');
	await writeFile(
		manifestPath,
		JSON.stringify({ entries: [{ name: 'shared-missing', codex_name: 'Missing', path: path.join(root, 'absent', 'SKILL.md') }] }),
		'utf8',
	);
	const project = path.join(root, 'project');
	await mkdir(project);
	await assert.rejects(run(['--cwd', project, '--manifest', manifestPath]), (error) => {
		assert.notEqual(error.code, 0);
		assert.match(error.stderr, /Missing imported skill shared-missing/);
		return true;
	});
});

test('fails when a disabled Codex skill also appears in entries', async (t) => {
	const root = await makeRoot(t);
	const pluginRoot = path.join(root, 'plugin');
	await mkdir(pluginRoot, { recursive: true });
	await copyFile(path.join(SCRIPT_DIR, 'provider.mjs'), path.join(pluginRoot, 'provider.mjs'));
	const skill = await writeSkill(root, ['codex', 'disabled'], 'Disabled Skill');
	const manifestPath = path.join(pluginRoot, 'manifest.json');
	await writeFile(
		manifestPath,
		JSON.stringify({
			entries: [{ name: 'shared-disabled', codex_name: 'Disabled Skill', path: skill }],
			disabled: [{ name: 'Disabled Skill', path: skill }],
		}),
		'utf8',
	);
	const project = path.join(root, 'project');
	await mkdir(project);
	await assert.rejects(run(['--cwd', project, '--manifest', manifestPath]), (error) => {
		assert.notEqual(error.code, 0);
		assert.match(error.stderr, /Disabled Codex skill/);
		return true;
	});
});

test('installed mode requires the global instruction link before touching instructions', async (t) => {
	const root = await makeRoot(t);
	const pluginRoot = path.join(root, 'dsh', 'codex-shared-skills');
	await mkdir(pluginRoot, { recursive: true });
	await copyFile(path.join(SCRIPT_DIR, 'provider.mjs'), path.join(pluginRoot, 'provider.mjs'));
	const skill = await writeSkill(root, ['codex', 'shared'], 'shared-demo');
	await writeFile(
		path.join(pluginRoot, 'manifest.json'),
		JSON.stringify({ entries: [{ name: 'shared-demo', codex_name: 'shared-demo', path: skill }] }),
		'utf8',
	);
	const project = path.join(root, 'project');
	await mkdir(project);
	await assert.rejects(
		run(['--cwd', project, '--installed', pluginRoot, '--dsh-home', path.join(root, 'dsh')]),
		(error) => {
			assert.notEqual(error.code, 0);
			assert.match(error.stderr, /global instruction link is missing/);
			return true;
		},
	);
});

test('installed mode loads instructions from the explicitly selected DSH home', async (t) => {
	const root = await makeRoot(t);
	const home = path.join(root, 'custom-dsh');
	const pluginRoot = path.join(home, 'codex-shared-skills');
	const project = path.join(root, 'project');
	await mkdir(pluginRoot, { recursive: true });
	await mkdir(project);
	await copyFile(path.join(SCRIPT_DIR, 'provider.mjs'), path.join(pluginRoot, 'provider.mjs'));
	const skill = await writeSkill(root, ['codex', 'shared'], 'shared-demo');
	await writeFile(path.join(pluginRoot, 'manifest.json'), JSON.stringify({ entries: [{ name: 'shared-demo', codex_name: 'shared-demo', path: skill }] }));
	const instructions = path.join(root, 'codex-global.md');
	await writeFile(instructions, '# UNIQUE CUSTOM DSH INSTRUCTIONS\n');
	await symlink(instructions, path.join(home, 'AGENTS.md'));
	const { stdout } = await run(['--cwd', project, '--installed', pluginRoot, '--dsh-home', home]);
	assert.equal(JSON.parse(stdout).global_instructions_loaded_in_full, true);
});

test('rejects conflicting or unknown arguments', async () => {
	await assert.rejects(run(['--installed', '--manifest', '/tmp/x.json']), (error) => {
		assert.notEqual(error.code, 0);
		assert.match(error.stderr, /mutually exclusive/);
		return true;
	});
	await assert.rejects(run(['--bogus']), (error) => {
		assert.notEqual(error.code, 0);
		assert.match(error.stderr, /unknown option/);
		return true;
	});
});