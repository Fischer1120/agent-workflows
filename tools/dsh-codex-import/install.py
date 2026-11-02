#!/usr/bin/env python3
"""Install the Codex shared-skill provider into a DSH profile.

Dry-run by default; pass --apply to write. The installer:

* copies ``provider.mjs`` and the explicit manifest into
  ``<dsh-home>/codex-shared-skills``;
* links ``<dsh-home>/AGENTS.md`` to ``<codex-home>/AGENTS.md``;
* adds or replaces one managed block in
  ``<dsh-home>/profiles/desktop/cordis.patch.yml`` while leaving every other
  profile setting untouched.

All prerequisites are checked before the first write. The Codex skill files
themselves are never modified, and this script does not read or depend on the
project that contains it.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = SCRIPT_DIR / "manifest.json"
START = "# codex-shared-skills:start"
END = "# codex-shared-skills:end"
PLUGIN_ID = "codex-shared-skills"
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class InstallError(RuntimeError):
    """A prerequisite or conflict prevented a safe installation."""


def sha256(path):
    """Return the SHA-256 hex digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_private(path, text):
    """Write UTF-8 text with mode 0600, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def load_manifest(path):
    """Read and validate the manifest before any write."""
    if not path.is_file():
        raise InstallError(
            f"manifest not found: {path}; generate it with build_manifest.mjs "
            "or pass --manifest PATH"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InstallError(f"cannot read manifest {path}: {error}") from error
    if not isinstance(data, dict):
        raise InstallError(f"manifest {path} must be a JSON object")
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise InstallError(
            f"manifest {path} has no enabled skill entries; regenerate it with build_manifest.mjs"
        )
    validated = []
    seen = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise InstallError(f"manifest entry #{index} must be an object")
        name = entry.get("name")
        source = entry.get("path")
        if not isinstance(name, str) or not SKILL_NAME_PATTERN.fullmatch(name):
            raise InstallError(f"manifest entry #{index} has an invalid DSH skill name: {name!r}")
        if name in seen:
            raise InstallError(f"manifest contains duplicate DSH skill name: {name}")
        if not isinstance(source, str) or not source:
            raise InstallError(f"manifest entry {name} has no source path")
        source_path = Path(source)
        if not source_path.is_absolute():
            raise InstallError(f"manifest entry {name} source path must be absolute: {source}")
        if not source_path.is_file():
            raise InstallError(f"manifest entry {name} points to a missing SKILL.md: {source_path}")
        seen.add(name)
        validated.append({"name": name, "codex_name": entry.get("codex_name"), "path": source_path})
    project_skills = data.get("project_skills", [])
    disabled = data.get("disabled", [])
    if not isinstance(project_skills, list) or not isinstance(disabled, list):
        raise InstallError(f"manifest {path} project_skills and disabled must be arrays when present")
    return data, validated


def build_patch(profile_text, target, profile):
    """Return the updated profile text and whether a managed block existed."""
    block = (
        f"{START}\n- insert:\n"
        f"    - id: {PLUGIN_ID}\n"
        f"      name: {json.dumps(str(target / 'provider.mjs'))}\n"
        "      config:\n"
        f"        manifestPath: {json.dumps(str(target / 'manifest.json'))}\n"
        f"{END}\n"
    )
    start_count = profile_text.count(START)
    end_count = profile_text.count(END)
    if start_count == 0 and end_count == 0:
        if PLUGIN_ID in profile_text:
            raise InstallError(
                f"{profile} already mentions {PLUGIN_ID} without managed markers; refusing to overwrite it"
            )
        separator = "" if not profile_text or profile_text.endswith("\n\n") else ("\n" if profile_text.endswith("\n") else "\n\n")
        return profile_text + separator + block, "append"
    if start_count == 1 and end_count == 1:
        start_index = profile_text.index(START)
        end_index = profile_text.index(END)
        if start_index > end_index:
            raise InstallError(f"{profile} has managed markers in the wrong order; no patch was applied")
        outside = profile_text[:start_index] + profile_text[end_index + len(END):]
        if PLUGIN_ID in outside:
            raise InstallError(f"{profile} mentions {PLUGIN_ID} outside managed markers; refusing to overwrite it")
        return profile_text[:start_index] + block.rstrip("\n") + profile_text[end_index + len(END):], "replace"
    raise InstallError(
        f"{profile} has conflicting managed markers "
        f"(start={start_count}, end={end_count}); no patch was applied"
    )


def check_agents_link(agents, codex_agents):
    """Validate the global instruction link without modifying anything."""
    if not codex_agents.is_file():
        raise InstallError(
            f"Codex global instructions not found: {codex_agents}; create it or pass --codex-home PATH"
        )
    if os.path.lexists(agents):
        if not agents.is_symlink():
            raise InstallError(
                f"{agents} exists and is not a symlink; refusing to replace its content"
            )
        if agents.resolve() != codex_agents.resolve():
            raise InstallError(
                f"{agents} points to {agents.resolve()} instead of {codex_agents.resolve()}; refusing to replace it"
            )


def reject_symlink_components(path):
    """Reject existing symlinks in a destination, including dangling links."""
    for component in reversed((path, *path.parents)):
        if component.is_symlink():
            raise InstallError(f"destination contains a symlink: {component}")


def check_destinations(dsh_home, profile, target):
    """Check all installer write destinations, except the intended AGENTS link."""
    temporary = profile.with_name(profile.name + ".codex-share-tmp")
    for path in (dsh_home, profile, temporary, target, target / "provider.mjs",
                 target / "manifest.json", dsh_home / "backups"):
        reject_symlink_components(path)
    if os.path.lexists(temporary):
        raise InstallError(f"temporary profile already exists: {temporary}")


def reject_same_file_sources(target, sources):
    """Refuse to overwrite any input through the same inode, including hard links."""
    for destination in (target / "provider.mjs", target / "manifest.json"):
        if not destination.exists():
            continue
        for source in sources:
            if os.path.samefile(destination, source):
                raise InstallError(f"destination and source are the same file: {destination} and {source}")


def read_profile(path):
    """Read without newline translation to preserve unmanaged bytes."""
    return path.read_bytes().decode("utf-8")


def copy_destination(source, destination):
    """Copy bytes to an ordinary destination without following a final symlink."""
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output, open(source, "rb") as input_file:
        shutil.copyfileobj(input_file, output)
        os.fchmod(output.fileno(), 0o600)


def preflight(options):
    """Check every prerequisite and return a read-only installation plan."""
    manifest_path = options.manifest.expanduser().resolve()
    requested_home = options.dsh_home.expanduser()
    if ".." in requested_home.parts:
        raise InstallError(f"DSH home must not contain '..': {requested_home}")
    dsh_home = Path(os.path.abspath(requested_home))
    codex_home = options.codex_home.expanduser().resolve()
    provider_source = SCRIPT_DIR / "provider.mjs"
    profile = dsh_home / "profiles/desktop/cordis.patch.yml"
    agents = dsh_home / "AGENTS.md"
    codex_agents = codex_home / "AGENTS.md"
    target = dsh_home / "codex-shared-skills"

    check_destinations(dsh_home, profile, target)

    if not provider_source.is_file():
        raise InstallError(f"provider.mjs not found next to the installer: {provider_source}")
    if not profile.is_file():
        raise InstallError(
            f"DSH desktop profile not found: {profile}; pass --dsh-home PATH or install DSH Desktop first"
        )
    if target.exists() and not target.is_dir():
        raise InstallError(f"{target} exists and is not a directory")
    manifest, entries = load_manifest(manifest_path)
    check_agents_link(agents, codex_agents)

    profile_text = read_profile(profile)
    updated_profile, profile_change = build_patch(profile_text, target, profile)
    source_hashes = {str(codex_agents): sha256(codex_agents)}
    source_hashes[str(manifest_path)] = sha256(manifest_path)
    source_hashes[str(provider_source)] = sha256(provider_source)
    for entry in entries:
        source_hashes[str(entry["path"])] = sha256(entry["path"])
    reject_same_file_sources(target, [profile, *(Path(source) for source in source_hashes)])

    return {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "entries": entries,
        "dsh_home": dsh_home,
        "codex_home": codex_home,
        "profile": profile,
        "agents": agents,
        "codex_agents": codex_agents,
        "target": target,
        "provider_source": provider_source,
        "profile_text": profile_text,
        "updated_profile": updated_profile,
        "profile_change": profile_change,
        "source_hashes": source_hashes,
    }


def describe(plan):
    """Render the dry-run plan as JSON-serializable data."""
    manifest = plan["manifest"]
    return {
        "status": "dry-run",
        "manifest": str(plan["manifest_path"]),
        "shared_global_skills": len(plan["entries"]),
        "entry_names": [entry["name"] for entry in plan["entries"]],
        "native_project_skills": [
            item.get("name") for item in manifest.get("project_skills", []) if isinstance(item, dict)
        ],
        "disabled_skills": [item.get("name") for item in manifest.get("disabled", []) if isinstance(item, dict)],
        "dsh_home": str(plan["dsh_home"]),
        "codex_home": str(plan["codex_home"]),
        "plugin_files": str(plan["target"]),
        "profile_patch": str(plan["profile"]),
        "profile_change": plan["profile_change"],
        "global_agents_link": {"path": str(plan["agents"]), "target": str(plan["codex_agents"])},
        "source_files_checked": len(plan["source_hashes"]),
        "apply": False,
    }


def apply_plan(plan):
    """Back up, install, and verify; called only after preflight succeeded."""
    target = plan["target"]
    profile = plan["profile"]
    backup = plan["dsh_home"] / "backups" / (
        "codex-share-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    )
    check_destinations(plan["dsh_home"], profile, target)
    reject_same_file_sources(target, [profile, *(Path(source) for source in plan["source_hashes"])])
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(profile, backup / "cordis.patch.yml")
    for name in ("provider.mjs", "manifest.json"):
        existing = target / name
        if existing.exists():
            shutil.copy2(existing, backup / name)

    if read_profile(profile) != plan["profile_text"]:
        raise InstallError("DSH profile changed after preflight; no files were installed")

    target.mkdir(mode=0o700, exist_ok=True)
    check_destinations(plan["dsh_home"], profile, target)
    copy_destination(plan["provider_source"], target / "provider.mjs")
    copy_destination(plan["manifest_path"], target / "manifest.json")
    for name, source in (("provider.mjs", plan["provider_source"]), ("manifest.json", plan["manifest_path"])):
        if sha256(target / name) != sha256(source):
            raise InstallError(f"installed {name} does not match its source")

    if not os.path.lexists(plan["agents"]):
        plan["agents"].symlink_to(plan["codex_agents"])
    if plan["agents"].resolve() != plan["codex_agents"].resolve():
        raise InstallError(f"{plan['agents']} does not resolve to {plan['codex_agents']}")

    if plan["updated_profile"] != plan["profile_text"]:
        temporary = profile.with_name(profile.name + ".codex-share-tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(plan["updated_profile"])
            os.fchmod(handle.fileno(), profile.stat().st_mode & 0o777)
        os.replace(temporary, profile)

    for source, expected in plan["source_hashes"].items():
        if sha256(Path(source)) != expected:
            raise InstallError(f"Codex source changed during installation: {source}")

    receipt = {
        "installed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "manifest": str(plan["manifest_path"]),
        "backup": str(backup),
        "plugin_files": str(target),
        "profile_patch": str(profile),
        "profile_change": plan["profile_change"],
        "global_agents_target": str(plan["agents"].resolve()),
        "shared_global_skills": len(plan["entries"]),
        "source_files_unchanged": len(plan["source_hashes"]),
        "source_hashes": plan["source_hashes"],
        "installed_hashes": {
            str(target / name): sha256(target / name) for name in ("provider.mjs", "manifest.json")
        },
    }
    write_private(backup / "receipt.json", json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return receipt


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the installation; without this flag the installer only prints the plan.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"Manifest generated by build_manifest.mjs (default: {DEFAULT_MANIFEST}).",
    )
    parser.add_argument(
        "--dsh-home",
        type=Path,
        default=Path.home() / ".dsh",
        help="DSH home containing profiles/desktop (default: ~/.dsh).",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path.home() / ".codex",
        help="Codex home whose AGENTS.md is linked as the global instructions (default: ~/.codex).",
    )
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    plan = preflight(options)
    if not options.apply:
        print(json.dumps(describe(plan), ensure_ascii=False, indent=2))
        return
    receipt = apply_plan(plan)
    print(
        json.dumps(
            {key: value for key, value in receipt.items() if not key.endswith("hashes")},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (InstallError, OSError) as error:
        print(f"install: {error}", file=sys.stderr)
        sys.exit(1)