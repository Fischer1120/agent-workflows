/**
 * Codex shared skill provider for DSH.
 *
 * Shares globally enabled Codex skills in place. The manifest carries only the
 * adapted DSH name and the original absolute SKILL.md path for every entry;
 * `list()` and `get()` re-read that original file (and its optional
 * `agents/openai.yaml` sidecar) on every call, so descriptions, invocation
 * policy, and bodies stay live. Nothing is copied, renamed, or rewritten in the
 * Codex tree, and resources keep pointing at the original skill directory.
 *
 * The module follows the published DSH skill-provider contract:
 * `ctx.skills.registerProvider(control => provider)` with a provider whose
 * `list(options)` returns candidates and whose `get(candidate, options)` returns
 * a definition. Reads go through `ctx.get("fs")` when that service exists — the
 * same resolve/stat/readText sequence `@deepseek-ai/dsh-skill-filesystem` uses,
 * so sandbox policy stays authoritative — and fall back to read-only Node
 * `realpath`/`readFile` otherwise.
 *
 * YAML frontmatter is parsed with the `yaml` package already installed next to
 * `@deepseek-ai/dsh-skill-filesystem`; no package is installed or added here.
 * The DSH dependency root defaults to the macOS application path and can be
 * overridden with `DSH_APP_ROOT` or `DSH_NODE_MODULES`.
 *
 * @module codex-shared-skills
 */
import { existsSync, unwatchFile, watchFile } from 'node:fs';
import { readFile, realpath } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, isAbsolute, join, resolve as resolvePath } from 'node:path';

/** Provider name registered on `ctx.skills`. */
const PROVIDER_NAME = 'codex-shared-skills';
/** Source tag attached to every shared candidate and definition. */
const SKILL_SOURCE = 'codex-shared';
/** Precedence shared with the filesystem provider's custom skill roots. */
const CODEX_SHARED_RANK = 300;
/** Public DSH skill-name grammar, mirrored from `@deepseek-ai/dsh-skill`. */
const SKILL_NAME_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
/** Poll interval for host file watchers, in milliseconds. */
const DEFAULT_WATCH_INTERVAL_MS = 1000;
/** Sidecar location relative to the skill directory (`SKILL.md` parent). */
const SIDECAR_SEGMENTS = ['agents', 'openai.yaml'];
/**
 * Default DSH Desktop application root on macOS. Set `DSH_APP_ROOT` (the
 * directory that contains `node_modules`) or `DSH_NODE_MODULES` (that
 * `node_modules` directory) when DSH lives elsewhere. `DSH_YAML_PACKAGE_JSON`
 * may name the exact `dsh-skill-filesystem/package.json` to resolve from.
 */
const DEFAULT_DSH_APP_ROOT = '/Applications/DSH Desktop.app/Contents/Resources/app';

/**
 * Resolve the installed DSH package.json that depends on `yaml`. Resolving from
 * it reuses the parser shipped with the running DSH Desktop instead of
 * installing a package.
 * @returns absolute path to `@deepseek-ai/dsh-skill-filesystem/package.json`.
 */
function skillFilesystemPackageJson() {
	if (process.env.DSH_YAML_PACKAGE_JSON) return process.env.DSH_YAML_PACKAGE_JSON;
	const nodeModules = process.env.DSH_NODE_MODULES ?? join(process.env.DSH_APP_ROOT ?? DEFAULT_DSH_APP_ROOT, 'node_modules');
	return join(nodeModules, '@deepseek-ai/dsh-skill-filesystem/package.json');
}

/** Module-level registration name consumed by DSH plugin composition. */
export const name = PROVIDER_NAME;
/** Services this plugin injects; the provider registers itself on `skills`. */
export const inject = ['skills'];

let yamlModule;

/**
 * Load the `yaml` parser installed with DSH Desktop.
 * @returns the yaml module namespace.
 */
function yaml() {
	if (yamlModule === undefined) {
		const packageJson = skillFilesystemPackageJson();
		if (!existsSync(packageJson)) {
			throw new Error(
				`codex-shared-skills: cannot load the yaml parser installed with DSH (${packageJson} not found); set DSH_NODE_MODULES or DSH_APP_ROOT to the installed DSH Desktop resources`,
			);
		}
		const requireFromInstalledDsh = createRequire(packageJson);
		try {
			yamlModule = requireFromInstalledDsh('yaml');
		} catch (error) {
			throw new Error(
				`codex-shared-skills: cannot load the yaml parser installed with DSH (${packageJson}): ${errorMessage(error)}`,
				{ cause: error },
			);
		}
	}
	return yamlModule;
}

/**
 * Parse one YAML document, throwing on syntax errors.
 * @param text - raw YAML text.
 * @returns the parsed value.
 */
function parseYaml(text) {
	return yaml().parse(text);
}

/**
 * Render an arbitrary thrown value safely.
 * @param error - thrown value.
 * @returns a string message.
 */
function errorMessage(error) {
	try {
		return String(error);
	} catch {
		return '[unrenderable thrown value]';
	}
}

/**
 * Return whether an error carries one of the given `code` values.
 * @param error - thrown value.
 * @param codes - expected error codes.
 * @returns whether the error matches one of the codes.
 */
function hasErrorCode(error, ...codes) {
	if (typeof error !== 'object' || error === null) return false;
	let code;
	try {
		code = error.code;
	} catch {
		return false;
	}
	return codes.includes(code);
}

/**
 * Return whether an error means the target path is absent.
 * Covers Node errno codes and the DSH `ctx.fs` typed taxonomy.
 * @param error - thrown value.
 * @returns whether the path should be treated as missing.
 */
function isAbsentPathError(error) {
	return hasErrorCode(error, 'ENOENT', 'ENOTDIR', 'FS_NOT_FOUND', 'FS_NOT_DIRECTORY');
}

/**
 * Render a manifest value safely for a warning message.
 * @param value - arbitrary JSON-ish value.
 * @returns a display string that never throws.
 */
function describeValue(value) {
	try {
		return JSON.stringify(value) ?? String(value);
	} catch {
		return '[unrenderable value]';
	}
}

/**
 * Abort the current lookup if the caller's signal already fired.
 * @param signal - optional abort signal.
 */
function throwIfAborted(signal) {
	if (signal?.aborted !== true) return;
	if (typeof signal.throwIfAborted === 'function') signal.throwIfAborted();
	const reason = signal.reason;
	throw reason instanceof Error ? reason : new Error('codex-shared-skills: lookup aborted');
}

/**
 * Read a UTF-8 text file through `ctx.fs` when available, else through Node.
 * Absent paths return `undefined`; every other I/O failure is rethrown so the
 * skill registry can report an incomplete catalog instead of an empty one.
 * @param ctx - plugin context that may expose the `fs` service.
 * @param path - absolute path to read.
 * @param signal - optional abort signal.
 * @param warn - warning sink for unreadable-text containment.
 * @returns `{ path, content }` for an existing file, or `undefined` when absent.
 */
async function readTextFile(ctx, path, signal, warn) {
	throwIfAborted(signal);
	const fs = typeof ctx.get === 'function' ? ctx.get('fs') : undefined;
	if (fs !== undefined && fs !== null) return await readTextFromFileSystem(fs, path, signal, warn);
	return await readTextFromNode(path, signal);
}

/**
 * Read through the DSH filesystem service, mirroring the filesystem provider's
 * `readSkillTextFromFileSystem` sequence: resolve, stat, then read text.
 * @param fs - the `ctx.fs` service.
 * @param path - absolute path to read.
 * @param signal - optional abort signal.
 * @param warn - warning sink for `FS_NOT_TEXT`.
 * @returns `{ path: processPath, content }` or `undefined` when absent.
 */
async function readTextFromFileSystem(fs, path, signal, warn) {
	let target;
	try {
		target = await fs.resolve(path);
	} catch (error) {
		if (isAbsentPathError(error)) return undefined;
		throw error;
	}
	throwIfAborted(signal);
	let info;
	try {
		info = await fs.stat(target, signal);
	} catch (error) {
		throwIfAborted(signal);
		if (isAbsentPathError(error)) return undefined;
		throw error;
	}
	if (info === undefined) return undefined;
	if (info.type !== 'file') throw new TypeError(`codex-shared-skills: expected a regular text file: ${path}`);
	try {
		return {
			path: fs.processPath(target),
			content: await fs.readText(target, signal),
		};
	} catch (error) {
		throwIfAborted(signal);
		if (isAbsentPathError(error)) return undefined;
		if (hasErrorCode(error, 'FS_NOT_TEXT')) {
			warn(`codex-shared-skills: ${path} ignored: file is not readable UTF-8 text: ${errorMessage(error)}`);
		}
		throw error;
	}
}

/**
 * Read-only Node fallback used when no `ctx.fs` service is available.
 * @param path - absolute path to read.
 * @param signal - optional abort signal.
 * @returns `{ path: realpath, content }` or `undefined` when absent.
 */
async function readTextFromNode(path, signal) {
	try {
		const resolvedPath = await realpath(path);
		return {
			path: resolvedPath,
			content: await readFile(resolvedPath, { encoding: 'utf8', signal }),
		};
	} catch (error) {
		throwIfAborted(signal);
		if (isAbsentPathError(error)) return undefined;
		throw error;
	}
}

/**
 * Locate the optional Codex sidecar for a SKILL.md path.
 * @param skillPath - absolute SKILL.md path.
 * @returns the absolute `agents/openai.yaml` path.
 */
function sidecarPathFor(skillPath) {
	return join(dirname(skillPath), ...SIDECAR_SEGMENTS);
}

/**
 * Split a SKILL.md into frontmatter data and the untouched body.
 * Mirrors the filesystem provider's delimiter rules; the body is returned
 * byte-for-byte after the closing delimiter so sharing never rewrites it.
 * @param raw - full file text.
 * @returns `{ data, body }`, or `undefined` when the frontmatter shape is absent.
 */
function parseSkillFrontmatter(raw) {
	const firstLineEnd = raw.indexOf('\n');
	if (firstLineEnd < 0) return undefined;
	if (raw.slice(0, firstLineEnd).replace(/\r$/, '') !== '---') return undefined;
	const start = firstLineEnd + 1;
	const closing = findClosingFrontmatter(raw, start);
	if (closing === undefined) return undefined;
	const data = parseYaml(raw.slice(start, closing.start));
	if (typeof data !== 'object' || data === null || Array.isArray(data)) return undefined;
	return { data, body: raw.slice(closing.bodyStart) };
}

/**
 * Find the closing `---` line of a frontmatter block.
 * @param raw - full file text.
 * @param start - offset of the first frontmatter line.
 * @returns the delimiter/body offsets, or `undefined` when unclosed.
 */
function findClosingFrontmatter(raw, start) {
	let lineStart = start;
	while (lineStart <= raw.length) {
		const nextNewline = raw.indexOf('\n', lineStart);
		const lineEnd = nextNewline < 0 ? raw.length : nextNewline;
		if (raw.slice(lineStart, lineEnd).replace(/\r$/, '') === '---') {
			return {
				start: lineStart,
				bodyStart: nextNewline < 0 ? raw.length : nextNewline + 1,
			};
		}
		if (nextNewline < 0) return undefined;
		lineStart = nextNewline + 1;
	}
}

/** Legacy camelCase invocation keys that must not be silently ignored. */
const LEGACY_INVOCATION_KEYS = [
	['disableModelInvocation', 'disable-model-invocation'],
	['modelInvocable', 'disable-model-invocation'],
	['userInvocable', 'user-invocable'],
];

/**
 * Read a strict boolean frontmatter field.
 * @param data - frontmatter mapping.
 * @param key - canonical kebab-case field name.
 * @returns the boolean value, or `undefined` when the field is absent.
 */
function strictFrontmatterBoolean(data, key) {
	if (!Object.hasOwn(data, key)) return undefined;
	const value = data[key];
	if (typeof value !== 'boolean') throw new TypeError(`frontmatter field "${key}" must be a boolean`);
	return value;
}

/**
 * Resolve the invocation policy from frontmatter without widening it.
 * Legacy camelCase keys are rejected so a disable switch cannot be ignored.
 * @param data - frontmatter mapping.
 * @returns `{ modelInvocable, userInvocable }`.
 */
function parseInvocationPolicy(data) {
	for (const [legacy, canonical] of LEGACY_INVOCATION_KEYS) {
		if (Object.hasOwn(data, legacy)) throw new Error(`frontmatter field "${legacy}" is unsupported; use "${canonical}"`);
	}
	return {
		modelInvocable: strictFrontmatterBoolean(data, 'disable-model-invocation') !== true,
		userInvocable: strictFrontmatterBoolean(data, 'user-invocable') !== false,
	};
}

/**
 * Read `policy.allow_implicit_invocation` from an openai.yaml mapping.
 * Only an explicit `false` disables implicit model invocation; `true` never
 * re-enables a skill that frontmatter already disabled.
 * @param data - parsed sidecar mapping.
 * @returns the boolean value, or `undefined` when unspecified.
 */
function readAllowImplicitInvocation(data) {
	const policy = data.policy;
	if (policy === undefined) return undefined;
	if (typeof policy !== 'object' || policy === null || Array.isArray(policy)) throw new TypeError('field "policy" must be a mapping');
	if (!Object.hasOwn(policy, 'allow_implicit_invocation')) return undefined;
	const value = policy.allow_implicit_invocation;
	if (typeof value !== 'boolean') throw new TypeError('field "policy.allow_implicit_invocation" must be a boolean');
	return value;
}

/**
 * Polling watch set over the manifest and every source file.
 * Absent paths are watched too so creation is noticed; the initial zero-stat
 * callback for an absent path is ignored to avoid invalidating on every list.
 */
class SourceWatchSet {
	/**
	 * @param invalidate - provider cache invalidation callback.
	 * @param intervalMs - `fs.watchFile` poll interval.
	 */
	constructor(invalidate, intervalMs) {
		this.invalidate = invalidate;
		this.intervalMs = intervalMs;
		this.listeners = new Map();
		this.disposed = false;
		this.invalidationQueued = false;
	}

	/**
	 * Replace the watched path set.
	 * @param paths - absolute paths that must stay watched.
	 */
	sync(paths) {
		if (this.disposed) return;
		const next = new Set();
		for (const path of paths) if (typeof path === 'string' && path.length > 0) next.add(path);
		for (const path of [...this.listeners.keys()]) if (!next.has(path)) this.remove(path);
		this.add(next);
	}

	/**
	 * Add paths without dropping existing watches.
	 * @param paths - absolute paths to ensure watched.
	 */
	ensure(paths) {
		if (this.disposed) return;
		this.add(new Set(paths));
	}

	/**
	 * Install one polling watcher per new path.
	 * @param paths - absolute paths.
	 */
	add(paths) {
		for (const path of paths) {
			if (this.disposed || this.listeners.has(path)) continue;
			const listener = (current, previous) => {
				if (this.disposed || this.listeners.get(path) !== listener) return;
				if (current.mtimeMs === 0 && previous.mtimeMs === 0) return;
				this.queueInvalidation();
			};
			watchFile(path, { persistent: false, interval: this.intervalMs }, listener);
			this.listeners.set(path, listener);
		}
	}

	/**
	 * Remove one watcher.
	 * @param path - absolute watched path.
	 */
	remove(path) {
		const listener = this.listeners.get(path);
		if (listener === undefined) return;
		this.listeners.delete(path);
		unwatchFile(path, listener);
	}

	/** Coalesce watcher callbacks into one provider invalidation. */
	queueInvalidation() {
		if (this.disposed || this.invalidationQueued) return;
		this.invalidationQueued = true;
		queueMicrotask(() => {
			this.invalidationQueued = false;
			if (this.disposed) return;
			try {
				this.invalidate();
			} catch {
				// Invalidation only drops registry caches; never let it crash the host.
			}
		});
	}

	/** Close every watcher. */
	dispose() {
		if (this.disposed) return;
		this.disposed = true;
		for (const [path, listener] of this.listeners) unwatchFile(path, listener);
		this.listeners.clear();
	}
}

/**
 * DSH skill provider that shares Codex skills in place.
 */
export class CodexSkillProvider {
	/**
	 * @param ctx - DSH plugin context.
	 * @param control - registry-provided lifecycle and invalidation control.
	 * @param config - `{ manifestPath, watchIntervalMs? }`.
	 */
	constructor(ctx, control, config = {}) {
		if (ctx === undefined || ctx === null) throw new TypeError('codex-shared-skills: a plugin context is required');
		const manifestPath = config?.manifestPath;
		if (typeof manifestPath !== 'string' || manifestPath.trim().length === 0) {
			throw new TypeError('codex-shared-skills: config.manifestPath must be a non-empty string');
		}
		if (control === undefined || control === null || typeof control.invalidate !== 'function') {
			throw new TypeError('codex-shared-skills: a provider control with invalidate() and signal is required');
		}
		const watchIntervalMs = config?.watchIntervalMs ?? DEFAULT_WATCH_INTERVAL_MS;
		if (!Number.isInteger(watchIntervalMs) || watchIntervalMs < 1) {
			throw new TypeError('codex-shared-skills: config.watchIntervalMs must be a positive integer');
		}
		this.ctx = ctx;
		this.name = PROVIDER_NAME;
		this.manifestPath = resolvePath(manifestPath);
		this.watcher = new SourceWatchSet(() => control.invalidate(), watchIntervalMs);
		this.signal = control.signal;
		this.onAbort = () => {
			void this.dispose();
		};
		this.disposed = false;
		this.disposal = undefined;
		if (this.signal !== undefined && this.signal !== null) {
			if (this.signal.aborted === true) this.dispose();
			else this.signal.addEventListener('abort', this.onAbort, { once: true });
		}
	}

	/**
	 * Discover shared Codex skills by re-reading every manifest source file.
	 * @param options - DSH lookup options; `signal` cancels reads.
	 * @returns provider candidates in manifest order.
	 */
	async list(options = {}) {
		const signal = options?.signal;
		const entries = await this.readManifest(signal);
		const watchPaths = new Set([this.manifestPath]);
		const candidates = [];
		for (const entry of entries) {
			watchPaths.add(entry.path);
			watchPaths.add(sidecarPathFor(entry.path));
			const loaded = await this.loadSkill(entry, signal);
			if (loaded !== undefined) candidates.push(toCandidate(entry, loaded));
		}
		this.watcher.sync(watchPaths);
		return candidates;
	}

	/**
	 * Load one shared skill from its original SKILL.md.
	 * @param candidate - candidate previously returned by this provider.
	 * @param options - DSH lookup options; `signal` cancels reads.
	 * @returns the full definition, or `undefined` when the source is gone or invalid.
	 */
	async get(candidate, options = {}) {
		const signal = options?.signal;
		const locator = candidate?.locator;
		if (typeof locator !== 'object' || locator === null || typeof locator.path !== 'string') {
			throw new TypeError('codex-shared-skills: get() requires a candidate produced by this provider');
		}
		if (!isAbsolute(locator.path)) return undefined;
		const definitionName = typeof candidate.name === 'string' ? candidate.name : locator.name;
		if (typeof definitionName !== 'string' || !SKILL_NAME_PATTERN.test(definitionName)) return undefined;
		const entry = {
			name: definitionName,
			path: locator.path,
			codexName: typeof locator.codexName === 'string' ? locator.codexName : undefined,
		};
		const loaded = await this.loadSkill(entry, signal);
		if (loaded === undefined) return undefined;
		this.watcher.ensure([this.manifestPath, entry.path, sidecarPathFor(entry.path)]);
		return {
			name: entry.name,
			description: loaded.description,
			invocation: loaded.invocation,
			source: SKILL_SOURCE,
			provider: this.name,
			resourceBase: { kind: 'directory', path: dirname(loaded.path) },
			path: loaded.path,
			content: loaded.content,
		};
	}

	/** Stop every watcher; safe to call more than once. */
	dispose() {
		if (this.disposal === undefined) {
			this.disposed = true;
			if (this.signal !== undefined && this.signal !== null) this.signal.removeEventListener('abort', this.onAbort);
			this.watcher.dispose();
			this.disposal = Promise.resolve();
		}
		return this.disposal;
	}

	/**
	 * Read and validate the manifest's enabled-skill entries.
	 * @param signal - optional abort signal.
	 * @returns accepted entries in manifest order.
	 */
	async readManifest(signal) {
		const raw = await readTextFile(this.ctx, this.manifestPath, signal, (message) => this.warn(message));
		if (raw === undefined) return [];
		let data;
		try {
			data = JSON.parse(raw.content);
		} catch (error) {
			throw new Error(`codex-shared-skills: manifest ${this.manifestPath} is not valid JSON: ${errorMessage(error)}`, { cause: error });
		}
		if (typeof data !== 'object' || data === null || Array.isArray(data)) {
			throw new Error(`codex-shared-skills: manifest ${this.manifestPath} must be a JSON object with an "entries" array`);
		}
		const entries = data.entries;
		if (!Array.isArray(entries)) {
			throw new Error(`codex-shared-skills: manifest ${this.manifestPath} must contain an "entries" array`);
		}
		const accepted = [];
		const seen = new Set();
		entries.forEach((entry, index) => {
			const label = `entry #${index}`;
			if (typeof entry !== 'object' || entry === null || Array.isArray(entry)) {
				this.warn(`codex-shared-skills: manifest ${label} ignored: entry is not an object`);
				return;
			}
			const entryName = entry.name;
			if (typeof entryName !== 'string' || !SKILL_NAME_PATTERN.test(entryName)) {
				this.warn(`codex-shared-skills: manifest ${label} ignored: invalid DSH skill name ${describeValue(entryName)}`);
				return;
			}
			if (seen.has(entryName)) {
				this.warn(`codex-shared-skills: manifest ${label} ignored: duplicate skill name "${entryName}"`);
				return;
			}
			const entryPath = entry.path;
			if (typeof entryPath !== 'string' || entryPath.length === 0 || !isAbsolute(entryPath)) {
				this.warn(`codex-shared-skills: manifest entry "${entryName}" ignored: path must be an absolute SKILL.md path`);
				return;
			}
			seen.add(entryName);
			accepted.push({
				name: entryName,
				path: entryPath,
				codexName: typeof entry.codex_name === 'string' && entry.codex_name.length > 0 ? entry.codex_name : undefined,
			});
		});
		return accepted;
	}

	/**
	 * Read one SKILL.md plus its optional sidecar into shared metadata.
	 * @param entry - accepted manifest entry.
	 * @param signal - optional abort signal.
	 * @returns parsed skill data, or `undefined` after warning when invalid.
	 */
	async loadSkill(entry, signal) {
		const warn = (message) => this.warn(message);
		const raw = await readTextFile(this.ctx, entry.path, signal, warn);
		if (raw === undefined) {
			this.warn(`codex-shared-skills: skill "${entry.name}" ignored: source file ${entry.path} is missing`);
			return undefined;
		}
		let parsed;
		try {
			parsed = parseSkillFrontmatter(raw.content);
		} catch (error) {
			this.warn(`codex-shared-skills: skill "${entry.name}" ignored: invalid YAML frontmatter in ${entry.path}: ${errorMessage(error)}`);
			return undefined;
		}
		if (parsed === undefined) {
			this.warn(`codex-shared-skills: skill "${entry.name}" ignored: ${entry.path} has no YAML frontmatter mapping`);
			return undefined;
		}
		if (typeof parsed.data.name !== 'string' || parsed.data.name.trim().length === 0) {
			this.warn(`codex-shared-skills: skill "${entry.name}" ignored: frontmatter requires a non-empty name`);
			return undefined;
		}
		const description = parsed.data.description;
		if (typeof description !== 'string' || description.trim().length === 0) {
			this.warn(`codex-shared-skills: skill "${entry.name}" ignored: frontmatter requires a non-empty description`);
			return undefined;
		}
		let invocation;
		try {
			invocation = parseInvocationPolicy(parsed.data);
		} catch (error) {
			this.warn(`codex-shared-skills: skill "${entry.name}" ignored: invalid invocation frontmatter in ${entry.path}: ${errorMessage(error)}`);
			return undefined;
		}
		const allowImplicitInvocation = await this.readSidecarPolicy(entry.path, signal);
		return {
			description,
			invocation: {
				modelInvocable: invocation.modelInvocable && allowImplicitInvocation !== false,
				userInvocable: invocation.userInvocable,
			},
			path: raw.path,
			content: parsed.body,
		};
	}

	/**
	 * Read the optional `agents/openai.yaml` policy without copying its content.
	 * A malformed sidecar fails closed: implicit model invocation is disabled
	 * while user invocation keeps the frontmatter policy.
	 * @param skillPath - absolute SKILL.md path.
	 * @param signal - optional abort signal.
	 * @returns `allow_implicit_invocation`, or `undefined` when unspecified.
	 */
	async readSidecarPolicy(skillPath, signal) {
		const sidecarPath = sidecarPathFor(skillPath);
		const raw = await readTextFile(this.ctx, sidecarPath, signal, (message) => this.warn(message));
		if (raw === undefined) return undefined;
		let data;
		try {
			data = parseYaml(raw.content);
		} catch (error) {
			this.warn(`codex-shared-skills: ${sidecarPath} ignored: invalid YAML: ${errorMessage(error)}; model invocation is disabled for this skill`);
			return false;
		}
		if (typeof data !== 'object' || data === null || Array.isArray(data)) {
			this.warn(`codex-shared-skills: ${sidecarPath} ignored: expected a YAML mapping; model invocation is disabled for this skill`);
			return false;
		}
		try {
			return readAllowImplicitInvocation(data);
		} catch (error) {
			this.warn(`codex-shared-skills: ${sidecarPath} ignored: ${errorMessage(error)}; model invocation is disabled for this skill`);
			return false;
		}
	}

	/**
	 * Emit one DSH warning through the plugin logger when present.
	 * @param message - warning text.
	 */
	warn(message) {
		const logger = this.ctx?.logger;
		if (logger !== undefined && logger !== null && typeof logger.warn === 'function') logger.warn(message);
	}
}

/**
 * Build a candidate from an accepted entry and freshly parsed source data.
 * @param entry - manifest entry whose name is authoritative.
 * @param loaded - parsed source metadata.
 * @returns the provider candidate.
 */
function toCandidate(entry, loaded) {
	const directory = dirname(loaded.path);
	return {
		name: entry.name,
		description: loaded.description,
		invocation: loaded.invocation,
		source: SKILL_SOURCE,
		provider: PROVIDER_NAME,
		rank: CODEX_SHARED_RANK,
		locator: {
			name: entry.name,
			path: entry.path,
			directory: dirname(entry.path),
			...(entry.codexName === undefined ? {} : { codexName: entry.codexName }),
		},
		path: loaded.path,
		resourceBase: { kind: 'directory', path: directory },
	};
}

/**
 * Register the shared Codex skill provider on `ctx.skills`.
 * @param ctx - DSH plugin context.
 * @param config - `{ manifestPath, watchIntervalMs? }`.
 */
export function apply(ctx, config = {}) {
	ctx.skills.registerProvider((control) => new CodexSkillProvider(ctx, control, config));
}
