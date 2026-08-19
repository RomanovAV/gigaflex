from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from gigaflex.planner import clean_plan_output, next_plan_path, slugify


class PlannerTest(unittest.TestCase):
    def test_slugify_ascii_request(self) -> None:
        self.assertEqual("add-user-auth", slugify("Add user auth!"))

    def test_slugify_non_ascii_request_uses_hash_fallback(self) -> None:
        self.assertRegex(slugify("добавить авторизацию"), r"^plan-[a-f0-9]{8}$")

    def test_next_plan_path_uses_date_slug_and_avoids_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plans_dir = Path(tmp)
            now = datetime(2026, 6, 12)
            first = next_plan_path(plans_dir, "Add user auth", now)
            first.write_text("existing", encoding="utf-8")

            second = next_plan_path(plans_dir, "Add user auth", now)

            self.assertEqual(plans_dir / "20260612-add-user-auth.md", first)
            self.assertEqual(plans_dir / "20260612-add-user-auth-2.md", second)

    def test_clean_plan_output_extracts_markdown_fence(self) -> None:
        output = "```md\n# Plan: Demo\n\n### Task 1: Build\n- [ ] Do it\n```"

        self.assertEqual("# Plan: Demo\n\n### Task 1: Build\n- [ ] Do it\n", clean_plan_output(output))


if __name__ == "__main__":
    unittest.main()
