"""Fast characterization checks for the isolated studio presentation layer."""

import importlib
import os
import unittest
from unittest.mock import patch
from pathlib import Path

import moment_retrieval.ui_assets as ui_assets


class StudioLayoutAssetsTests(unittest.TestCase):
    def test_classic_mode_keeps_the_original_assets_only(self):
        with patch.dict(os.environ, {"CUT_VIDEO_UI_LAYOUT": "classic"}, clear=False):
            assets = importlib.reload(ui_assets)
            self.assertFalse(assets.UI_STUDIO)
            asset_dir = Path(assets.__file__).resolve().parent.parent / "assets"
            self.assertEqual(assets._APP_CSS, (asset_dir / "app.css").read_text(encoding="utf-8"))
            self.assertEqual(
                assets._INTUITIVE_EDITOR_JS,
                (asset_dir / "intuitive_editor.js").read_text(encoding="utf-8"),
            )

    def test_studio_mode_composes_scoped_controls_after_classic_bridge(self):
        with patch.dict(os.environ, {"CUT_VIDEO_UI_LAYOUT": "studio"}, clear=False):
            assets = importlib.reload(ui_assets)
            self.assertTrue(assets.UI_STUDIO)
            self.assertIn("#intuitive-editor-tab.studio-layout", assets._APP_CSS)
            self.assertIn("__intuitiveEditorInstalled", assets._INTUITIVE_EDITOR_JS)
            self.assertIn("__studioLayoutInstalled", assets._INTUITIVE_EDITOR_JS)
            self.assertIn("cut-video-studio-layout-v1", assets._INTUITIVE_EDITOR_JS)

    def tearDown(self):
        importlib.reload(ui_assets)


if __name__ == "__main__":
    unittest.main()
