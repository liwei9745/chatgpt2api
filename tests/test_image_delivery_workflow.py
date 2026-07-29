from __future__ import annotations

from pathlib import Path
import unittest


class ImageDeliveryWorkflowTests(unittest.TestCase):
    def test_genbox_delivery_workflow_requires_explicit_publish_and_reports_digest(self) -> None:
        workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "docker-publish.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("confirm_publish:", workflow)
        self.assertIn("inputs.confirm_publish == 'publish'", workflow)
        self.assertNotIn("value=latest", workflow)
        self.assertIn("type=sha", workflow)
        self.assertIn("steps.build.outputs.digest", workflow)
        self.assertIn("ghcr.io/${{ github.repository_owner }}/${{ env.IMAGE_NAME }}@", workflow)


if __name__ == "__main__":
    unittest.main()
