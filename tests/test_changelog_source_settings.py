"""Checks for the persisted changelog-source preference."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / "helper" / "globalPlugins" / "_addonStoreChangelogs.py"
SPEC = importlib.util.spec_from_file_location("changelogSourceSettings", PATH)
changelogs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(changelogs)


class SourceSettingsTests(unittest.TestCase):
    def test_missing_or_invalid_config_defaults_to_all_sources(self):
        config = types.ModuleType("config")
        config.conf = {"serrebiStore": {}}
        with mock.patch.dict(sys.modules, {"config": config}):
            self.assertEqual("all", changelogs._sourcePreference())
            config.conf["serrebiStore"][changelogs.CHANGELOG_SOURCE_KEY] = "unexpected"
            self.assertEqual("all", changelogs._sourcePreference())

    def test_settings_register_and_save_source_selection(self):
        class Conf(dict):
            spec = {"serrebiStore": {}}
        config = types.ModuleType("config")
        config.conf = Conf(serrebiStore={})
        wx = types.ModuleType("wx")
        class Choice:
            def __init__(self, _parent, choices):
                self.choices = choices
                self.selection = -1
            def SetSelection(self, selection): self.selection = selection
            def GetSelection(self): return self.selection
        wx.Choice = Choice
        gui = types.ModuleType("gui")
        guiHelper = types.ModuleType("gui.guiHelper")
        class Helper:
            def __init__(self, _panel, sizer): self.sizer = sizer
            def addLabeledControl(self, _label, control, choices):
                self.sizer.choice = control(None, choices)
                return self.sizer.choice
        gui.guiHelper = guiHelper
        guiHelper.BoxSizerHelper = Helper
        class Panel:
            def makeSettings(self, _sizer): self.baseMade = True
            def onSave(self): self.baseSaved = True
        class Plugin:
            def _rememberPatch(self, owner, name, replacement): setattr(owner, name, replacement)
        sizer = types.SimpleNamespace()
        modules = {"config": config, "wx": wx, "gui": gui, "gui.guiHelper": guiHelper}
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            changelogs, "isSecureDesktop", return_value=False,
        ):
            changelogs.enableSettings(Plugin(), Panel)
            panel = Panel()
            panel.makeSettings(sizer)
            self.assertTrue(panel.baseMade)
            self.assertEqual(0, sizer.choice.GetSelection())
            sizer.choice.SetSelection(2)
            panel.onSave()
        self.assertTrue(panel.baseSaved)
        self.assertEqual("github", config.conf["serrebiStore"][changelogs.CHANGELOG_SOURCE_KEY])
        self.assertEqual(
                "option('all', 'catalog', 'github', 'repository', 'commits', default='all')",
            config.conf.spec["serrebiStore"][changelogs.CHANGELOG_SOURCE_KEY],
        )

    def test_all_starts_availability_discovery_and_no_direct_history_request(self):
        class Store: pass
        store = Store()
        parent = types.SimpleNamespace()
        feature = changelogs.ChangelogFeature(types.SimpleNamespace())
        feature._dialogs[store] = lambda: parent
        item = types.SimpleNamespace(model=types.SimpleNamespace(displayName="Example"))
        with mock.patch.object(changelogs, "isSecureDesktop", return_value=False), mock.patch.object(
            changelogs, "_sourcePreference", return_value="all",
        ), mock.patch.object(feature, "_discoverSources") as discover, mock.patch.object(
            feature, "_requestHistory",
        ) as request:
            feature.show(item, store)
        discover.assert_called_once_with(item.model, "Example", mock.ANY)
        request.assert_not_called()

    def test_specific_saved_source_does_not_prompt(self):
        class Store: pass
        store = Store()
        parent = types.SimpleNamespace()
        feature = changelogs.ChangelogFeature(types.SimpleNamespace())
        feature._dialogs[store] = lambda: parent
        model = types.SimpleNamespace(displayName="Example")
        item = types.SimpleNamespace(model=model)
        with mock.patch.object(changelogs, "isSecureDesktop", return_value=False), mock.patch.object(
            changelogs, "_sourcePreference", return_value="commits",
        ), mock.patch.object(feature, "_chooseSource") as choose, mock.patch.object(
            feature, "_requestHistory",
        ) as request:
            feature.show(item, store)
        choose.assert_not_called()
        request.assert_called_once_with(model, "Example", mock.ANY, "commits")

    def test_all_source_picker_uses_capability_labels_without_counts(self):
        wx = types.ModuleType("wx")
        wx.ID_OK = 1
        captured = {}
        class Dialog:
            def __init__(self, _parent, message, _caption, choices):
                captured["message"] = message
                captured["choices"] = choices
            def ShowModal(self): return wx.ID_OK
            def GetSelection(self): return 1
            def Destroy(self): captured["destroyed"] = True
        wx.SingleChoiceDialog = Dialog
        feature = changelogs.ChangelogFeature(types.SimpleNamespace())
        with mock.patch.dict(sys.modules, {"wx": wx}):
            self.assertEqual("commits", feature._chooseSource(None, (
                ("catalog", None, 0, False), ("commits", None, 0, False),
            )))
        self.assertEqual(2, len(captured["choices"]))
        self.assertEqual("Catalog or installed manifest notes", captured["choices"][0])
        self.assertEqual("GitHub commits (development history, not releases)", captured["choices"][1])
        self.assertIn("loads only after selection", captured["message"])
        self.assertTrue(captured["destroyed"])


if __name__ == "__main__":
    unittest.main()
