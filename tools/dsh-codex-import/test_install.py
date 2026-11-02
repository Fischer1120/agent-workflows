#!/usr/bin/env python3
"""Tests for install.py.

Run with: python3 -m unittest -v test_install.py

Each test builds an isolated DSH home, Codex home, skill, and manifest under a
temporary directory. The real ~/.dsh and ~/.codex are never touched.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parent / "install.py"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def file_mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="codex import test ")).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        self.dsh_home = self.root / "dsh home"
        self.profile = self.dsh_home / "profiles/desktop/cordis.patch.yml"
        self.profile.parent.mkdir(parents=True)
        self.profile.write_text(
            "# existing profile\n- id: other-plugin\n  name: /tmp/other.mjs\n",
            encoding="utf-8",
        )
        self.codex_home = self.root / "codex home"
        self.codex_home.mkdir()
        self.codex_agents = self.codex_home / "AGENTS.md"
        self.codex_agents.write_text("# Codex global instructions\n", encoding="utf-8")
        self.skill = self.root / "codex skills" / "demo" / "SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text(
            "---\nname: Demo\ndescription: demo skill\n---\n\n# Demo\n",
            encoding="utf-8",
        )
        self.manifest = self.root / "manifest.json"
        self.write_manifest([{"name": "shared-demo", "codex_name": "Demo", "path": str(self.skill)}])

    def write_manifest(self, entries, project_skills=None, disabled=None):
        self.manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "project_skills": project_skills or [],
                    "disabled": disabled or [],
                    "entries": entries,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def run_install(self, *extra, apply=False):
        command = [
            sys.executable,
            str(SCRIPT),
            "--manifest",
            str(self.manifest),
            "--dsh-home",
            str(self.dsh_home),
            "--codex-home",
            str(self.codex_home),
        ]
        if apply:
            command.append("--apply")
        command.extend(extra)
        return subprocess.run(command, capture_output=True, text=True)

    def tree(self):
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))

    def test_dry_run_writes_nothing(self):
        before = self.tree()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["status"], "dry-run")
        self.assertFalse(plan["apply"])
        self.assertEqual(plan["shared_global_skills"], 1)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.dsh_home / "codex-shared-skills").exists())
        self.assertFalse((self.dsh_home / "backups").exists())

    def test_apply_installs_backs_up_and_preserves_other_profile_settings(self):
        source_hash = sha256(self.skill)
        result = self.run_install(apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        target = self.dsh_home / "codex-shared-skills"
        for name in ("provider.mjs", "manifest.json"):
            self.assertTrue((target / name).is_file(), name)
            self.assertEqual(file_mode(target / name), 0o600, name)
        self.assertEqual((target / "provider.mjs").read_bytes(), (SCRIPT.parent / "provider.mjs").read_bytes())
        self.assertEqual((target / "manifest.json").read_bytes(), self.manifest.read_bytes())
        self.assertEqual(sha256(self.skill), source_hash, "Codex source must not change")

        agents = self.dsh_home / "AGENTS.md"
        self.assertTrue(agents.is_symlink())
        self.assertEqual(agents.resolve(), self.codex_agents.resolve())

        profile = self.profile.read_text(encoding="utf-8")
        self.assertIn("# existing profile", profile)
        self.assertIn("id: other-plugin", profile)
        self.assertEqual(profile.count("# codex-shared-skills:start"), 1)
        self.assertEqual(profile.count("# codex-shared-skills:end"), 1)
        self.assertIn("id: codex-shared-skills", profile)
        self.assertIn(str(target / "provider.mjs"), profile)
        self.assertIn(str(target / "manifest.json"), profile)

        backup = Path(receipt["backup"])
        self.assertTrue(backup.is_dir())
        self.assertEqual((backup / "cordis.patch.yml").read_text(encoding="utf-8").count("# existing profile"), 1)
        self.assertTrue((backup / "receipt.json").is_file())

    def test_reapply_replaces_one_managed_block_and_makes_another_backup(self):
        self.assertEqual(self.run_install(apply=True).returncode, 0)
        self.assertEqual(self.run_install(apply=True).returncode, 0)
        profile = self.profile.read_text(encoding="utf-8")
        self.assertEqual(profile.count("# codex-shared-skills:start"), 1)
        self.assertEqual(profile.count("# codex-shared-skills:end"), 1)
        backups = list((self.dsh_home / "backups").glob("codex-share-*"))
        self.assertEqual(len(backups), 2)

    def test_existing_regular_agents_file_is_rejected_without_writes(self):
        (self.dsh_home / "AGENTS.md").write_text("# local instructions\n", encoding="utf-8")
        profile_before = self.profile.read_text(encoding="utf-8")
        result = self.run_install(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a symlink", result.stderr)
        self.assertEqual(self.profile.read_text(encoding="utf-8"), profile_before)
        self.assertFalse((self.dsh_home / "codex-shared-skills").exists())

    def test_agents_symlink_to_another_target_is_rejected(self):
        other = self.root / "other AGENTS.md"
        other.write_text("# other\n", encoding="utf-8")
        (self.dsh_home / "AGENTS.md").symlink_to(other)
        result = self.run_install(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("instead of", result.stderr)
        self.assertFalse((self.dsh_home / "codex-shared-skills").exists())

    def test_conflicting_profile_markers_are_rejected(self):
        self.profile.write_text(
            "- id: other\n# codex-shared-skills:start\n- id: broken\n",
            encoding="utf-8",
        )
        before = self.profile.read_text(encoding="utf-8")
        result = self.run_install(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicting managed markers", result.stderr)
        self.assertEqual(self.profile.read_text(encoding="utf-8"), before)
        self.assertFalse((self.dsh_home / "codex-shared-skills").exists())

    def test_unmanaged_plugin_id_is_rejected(self):
        self.profile.write_text(
            "- id: codex-shared-skills\n  name: /tmp/old.mjs\n",
            encoding="utf-8",
        )
        result = self.run_install(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("without managed markers", result.stderr)

    def test_symlink_write_paths_are_rejected_before_any_write(self):
        for location in ("provider.mjs", "manifest.json", "target", "profile", "profile-parent", "temporary", "backups", "dsh-home"):
            with self.subTest(location=location):
                root = self.root / ("case-" + location)
                home = root / "dsh"
                profile = home / "profiles" / "desktop" / "cordis.patch.yml"
                profile.parent.mkdir(parents=True)
                original_profile = b"# original\n"
                profile.write_bytes(original_profile)
                outside = root / "outside"
                outside.mkdir()
                victim = outside / "victim"
                victim.write_bytes(b"user-owned")
                target = home / "codex-shared-skills"
                if location in ("provider.mjs", "manifest.json"):
                    target.mkdir()
                    (target / location).symlink_to(victim)
                elif location == "target":
                    target.symlink_to(outside, target_is_directory=True)
                elif location == "profile":
                    profile.unlink()
                    profile.symlink_to(victim)
                elif location == "profile-parent":
                    profile.rename(outside / profile.name)
                    profile.parent.rmdir()
                    profile.parent.symlink_to(outside, target_is_directory=True)
                elif location == "temporary":
                    profile.with_name(profile.name + ".codex-share-tmp").symlink_to(victim)
                elif location == "backups":
                    (home / "backups").symlink_to(outside, target_is_directory=True)
                elif location == "dsh-home":
                    actual = root / "real-dsh"
                    home.rename(actual)
                    home.symlink_to(actual, target_is_directory=True)
                before = victim.read_bytes()
                result = self.run_install("--dsh-home", str(home), apply=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("symlink", result.stderr.lower())
                self.assertEqual(victim.read_bytes(), before)
                self.assertFalse((outside / "provider.mjs").exists())
                self.assertFalse((outside / "manifest.json").exists())
                self.assertFalse((home / "backups").is_dir() and location != "backups")

    def test_unmanaged_id_outside_valid_managed_block_is_rejected_without_writes(self):
        block = b"# codex-shared-skills:start\r\n- id: codex-shared-skills\r\n# codex-shared-skills:end\r\n"
        extra = b"- id: codex-shared-skills\r\n  name: user-plugin\r\n"
        for before_block in (True, False):
            with self.subTest(before_block=before_block):
                original = extra + block if before_block else block + extra
                self.profile.write_bytes(original)
                result = self.run_install(apply=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("codex-shared-skills", result.stderr)
                self.assertEqual(self.profile.read_bytes(), original)
                self.assertFalse((self.dsh_home / "backups").exists())
                self.assertFalse((self.dsh_home / "codex-shared-skills").exists())

    def test_manifest_same_as_destination_or_hardlinked_is_rejected_before_writes(self):
        for hardlink in (False, True):
            with self.subTest(hardlink=hardlink):
                case = self.root / ("hardlink-case" if hardlink else "same-file-case")
                home = case / "dsh"
                profile = home / "profiles/desktop/cordis.patch.yml"
                profile.parent.mkdir(parents=True)
                profile.write_bytes(b"# original profile\n")
                target = home / "codex-shared-skills"
                target.mkdir()
                installed = target / "manifest.json"
                source = case / "manifest.json" if hardlink else installed
                source.write_bytes(self.manifest.read_bytes())
                if hardlink:
                    os.link(source, installed)
                before_manifest = source.read_bytes()
                before_profile = profile.read_bytes()
                result = self.run_install("--dsh-home", str(home), "--manifest", str(source), apply=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("same file", result.stderr)
                self.assertEqual(source.read_bytes(), before_manifest)
                self.assertEqual(installed.read_bytes(), before_manifest)
                self.assertEqual(profile.read_bytes(), before_profile)
                self.assertFalse((home / "backups").exists())
                self.assertFalse((target / "provider.mjs").exists())
                self.assertFalse((home / "AGENTS.md").exists())

    def test_managed_replacement_preserves_unmanaged_crlf_bytes(self):
        original = b"# before\r\n# codex-shared-skills:start\r\nold\r\n# codex-shared-skills:end\r\n# after\r\n"
        self.profile.write_bytes(original)
        result = self.run_install(apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = self.profile.read_bytes()
        self.assertTrue(actual.startswith(b"# before\r\n"))
        self.assertTrue(actual.endswith(b"\r\n# after\r\n"))

    def test_missing_profile_reports_the_expected_path(self):
        shutil.rmtree(self.dsh_home)
        result = self.run_install(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DSH desktop profile not found", result.stderr)
        self.assertIn(str(self.profile), result.stderr)

    def test_missing_manifest_and_empty_entries_are_rejected(self):
        self.manifest.unlink()
        result = self.run_install(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest not found", result.stderr)

        self.write_manifest([])
        result = self.run_install(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no enabled skill entries", result.stderr)
        self.assertFalse((self.dsh_home / "codex-shared-skills").exists())

    def test_missing_skill_source_is_rejected_before_writes(self):
        self.write_manifest(
            [{"name": "shared-demo", "codex_name": "Demo", "path": str(self.root / "absent" / "SKILL.md")}]
        )
        result = self.run_install(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing SKILL.md", result.stderr)
        self.assertFalse((self.dsh_home / "codex-shared-skills").exists())

    def test_missing_codex_agents_is_a_prerequisite_error(self):
        self.codex_agents.unlink()
        result = self.run_install(apply=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Codex global instructions not found", result.stderr)
        self.assertFalse((self.dsh_home / "codex-shared-skills").exists())

    def test_dry_run_reports_native_and_disabled_skills(self):
        repo_skill = self.root / "project" / "SKILL.md"
        repo_skill.parent.mkdir()
        repo_skill.write_text("---\nname: repo-skill\ndescription: repo\n---\n\n# Repo\n", encoding="utf-8")
        disabled = self.root / "disabled" / "SKILL.md"
        disabled.parent.mkdir()
        disabled.write_text("---\nname: hatch-pet\ndescription: disabled\n---\n\n# Pet\n", encoding="utf-8")
        self.write_manifest(
            [{"name": "shared-demo", "codex_name": "Demo", "path": str(self.skill)}],
            project_skills=[{"name": "repo-skill", "path": str(repo_skill)}],
            disabled=[{"name": "hatch-pet", "path": str(disabled)}],
        )
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["native_project_skills"], ["repo-skill"])
        self.assertEqual(plan["disabled_skills"], ["hatch-pet"])
        self.assertEqual(Path(plan["global_agents_link"]["target"]).resolve(), self.codex_agents.resolve())


if __name__ == "__main__":
    unittest.main()