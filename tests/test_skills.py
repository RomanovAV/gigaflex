from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigalphex.skills import (
    install_planning_skill,
    install_superpowers_converter_skill,
    planning_skill_installed,
    planning_skill_path,
    planning_skill_text,
    superpowers_converter_skill_installed,
    superpowers_converter_skill_path,
    superpowers_converter_skill_text,
)


class SkillsTest(unittest.TestCase):
    def test_bundled_planning_skill_has_expected_metadata(self) -> None:
        text = planning_skill_text()

        self.assertIn("name: planning", text)
        self.assertIn("Do not begin implementation.", text)
        self.assertIn("checkboxes only inside executable task sections", text)
        self.assertIn("Keep task ownership mutually exclusive", text)

    def test_bundled_superpowers_converter_skill_has_expected_metadata(self) -> None:
        text = superpowers_converter_skill_text()

        self.assertIn("name: superpowers-to-gigalphex", text)
        self.assertIn("Convert a Superpowers design spec", text)
        self.assertIn("docs/superpowers/specs", text)
        self.assertIn("Remove Superpowers-only process directives", text)
        self.assertIn("Do not include a commit step as a task checkbox", text)
        self.assertIn("Remove process-only test-first sequencing", text)
        self.assertIn("Preserve the required automated test coverage", text)
        self.assertNotIn("Keep TDD requirements", text)

    def test_install_is_idempotent_for_bundled_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"

            first_path, first_written = install_planning_skill(skills_dir)
            second_path, second_written = install_planning_skill(skills_dir)

            self.assertEqual(planning_skill_path(skills_dir), first_path)
            self.assertEqual(first_path, second_path)
            self.assertTrue(first_written)
            self.assertFalse(second_written)
            self.assertTrue(planning_skill_installed(skills_dir))

    def test_install_superpowers_converter_is_idempotent_for_bundled_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"

            first_path, first_written = install_superpowers_converter_skill(skills_dir)
            second_path, second_written = install_superpowers_converter_skill(skills_dir)

            self.assertEqual(superpowers_converter_skill_path(skills_dir), first_path)
            self.assertEqual(first_path, second_path)
            self.assertTrue(first_written)
            self.assertFalse(second_written)
            self.assertTrue(superpowers_converter_skill_installed(skills_dir))

    def test_force_overwrites_modified_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            target = planning_skill_path(skills_dir)
            target.parent.mkdir(parents=True)
            target.write_text("custom\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                install_planning_skill(skills_dir)

            path, written = install_planning_skill(skills_dir, force=True)

            self.assertEqual(target, path)
            self.assertTrue(written)
            self.assertEqual(planning_skill_text(), target.read_text(encoding="utf-8"))

    def test_force_overwrites_modified_superpowers_converter_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_dir = Path(tmp) / "skills"
            target = superpowers_converter_skill_path(skills_dir)
            target.parent.mkdir(parents=True)
            target.write_text("custom\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                install_superpowers_converter_skill(skills_dir)

            path, written = install_superpowers_converter_skill(skills_dir, force=True)

            self.assertEqual(target, path)
            self.assertTrue(written)
            self.assertEqual(
                superpowers_converter_skill_text(),
                target.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
