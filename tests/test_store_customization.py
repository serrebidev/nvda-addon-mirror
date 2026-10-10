"""Pure-function checks for the Add-on Store customization feature."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


PATH = Path(__file__).resolve().parents[1] / "helper" / "globalPlugins" / "_addonStoreCustomization.py"


class StoreCustomizationTests(unittest.TestCase):
    NATIVE_FIELDS = (
        "searchRank", "displayName", "status", "currentAddonVersionName",
        "availableAddonVersionName", "channel", "publisher", "author",
        "publicationDate", "installDate", "minimumNVDAVersion", "lastTestedVersion",
    )

    def setUp(self):
        self.config = types.ModuleType("config")
        self.config.conf = {"serrebiStore": {}}
        self.wx = types.ModuleType("wx")
        self.wx.Dialog = object
        spec = importlib.util.spec_from_file_location("storeCustomizationTest", PATH)
        self.module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"config": self.config, "wx": self.wx}):
            spec.loader.exec_module(self.module)

    def _vm(self, active="displayName", reverse=False, date=None):
        class Field:
            def __init__(self, name): self.name = name
        # Description is a test-only stand-in; futureField proves the feature
        # does not need a release for an NVDA field added later.
        fields = [Field(name) for name in (*self.NATIVE_FIELDS, "description", "futureField")]
        labels = []
        labelsByName = {"searchRank": "Relevance", "displayName": "Name"}
        for field in fields:
            label = labelsByName.get(field.name, field.name)
            labels.extend([label + " ascending", label + " descending"])
        labels.extend(["Last updated ascending", "Last updated descending"])
        vm = types.SimpleNamespace(
            sortableFields=fields,
            presentedFields=[field for field in fields if field.name != "searchRank"],
            _columnSortChoices=labels,
            _sortByModelField=types.SimpleNamespace(name=active),
            _reverseSort=reverse,
        )
        if date is not None:
            vm._serrebiDateSort = date
        return vm

    def test_sort_entries_include_every_native_field_relevance_future_field_and_custom_dates_both_directions(self):
        self.config.conf["serrebiStore"]["sortOrder"] = ["lastUpdated:desc", "description:asc"]
        entries = self.module.sortEntries(self._vm())
        fields = [*self.NATIVE_FIELDS, "description", "futureField"]
        expected = {_key for field in fields if field != "publicationDate"
                    for _key in (field + ":asc", field + ":desc")}
        expected.update({"lastUpdated:asc", "lastUpdated:desc"})
        self.assertEqual(expected, {key for key, _label, _logical in entries})
        self.assertEqual(["lastUpdated:desc", "description:asc"], [key for key, _label, _logical in entries[:2]])
        self.assertEqual(
            {
                "searchRank:asc": ("Relevance ascending", 0),
                "searchRank:desc": ("Relevance descending", 1),
                "lastUpdated:asc": ("Last updated ascending", len(fields) * 2),
                "lastUpdated:desc": ("Last updated descending", len(fields) * 2 + 1),
            },
            {key: (label, logical) for key, label, logical in entries if key in {
                "searchRank:asc", "searchRank:desc", "lastUpdated:asc", "lastUpdated:desc",
            }},
        )

    def test_active_sort_reports_native_and_custom_last_updated_directions(self):
        self.assertEqual("description:desc", self.module.activeSort(self._vm("description", True)))
        self.assertEqual("lastUpdated:asc", self.module.activeSort(self._vm(date=False)))
        self.assertEqual("lastUpdated:desc", self.module.activeSort(self._vm(date=True)))

    def test_previous_publication_preferences_map_to_one_last_updated_pair(self):
        self.config.conf["serrebiStore"].update({
            "sortOrder": ["publicationDate:desc", "lastUpdated:desc"],
            "tabDefaults": '{"AVAILABLE":{"sort":"publicationDate:asc","channel":"STABLE"}}',
        })
        entries = self.module.sortEntries(self._vm())
        self.assertEqual("lastUpdated:desc", entries[0][0])
        self.assertEqual(1, sum(key == "lastUpdated:desc" for key, label, logical in entries))
        self.assertFalse(any(key.startswith("publicationDate:") for key, label, logical in entries))
        self.assertEqual({"AVAILABLE": {"sort": "lastUpdated:asc", "channel": "STABLE"}},
                         self.module.tabDefaults())
        self.assertEqual("lastUpdated:desc", self.module.activeSort(self._vm("publicationDate", True)))

    def test_contextual_layout_is_unchanged_when_disabled_and_includes_all_active_fields_when_enabled(self):
        vm = self._vm()
        layout = [next(field for field in vm.sortableFields if field.name == "description")]
        self.assertIs(layout, self.module.contextualLayout(layout, vm))

        self.config.conf["serrebiStore"]["contextualColumns"] = True
        for field in (*self.NATIVE_FIELDS, "futureField"):
            for reverse in (False, True):
                with self.subTest(field=field, reverse=reverse):
                    vm._sortByModelField = types.SimpleNamespace(name=field)
                    vm._reverseSort = reverse
                    vm._serrebiDateSort = None
                    result = self.module.contextualLayout(layout, vm)
                    expected = ["displayName"] + ([] if field == "displayName" else [field]) + ["description"]
                    self.assertEqual(expected, [item.name for item in result])
                    self.assertEqual(len(result), len({id(item) for item in result}))
        # Last updated reuses the formerly mislabelled native date column once.
        vm._serrebiDateSort = True
        self.assertEqual(["displayName", "publicationDate", "description"], [
            item if isinstance(item, str) else item.name
            for item in self.module.contextualLayout(layout, vm)
        ])

    def test_ordered_keys_and_configuration_validation_keep_only_known_values(self):
        self.assertEqual(
            ["UPDATE", "AVAILABLE", "INSTALLED", "INCOMPATIBLE", "FAVOURITES"],
            self.module.orderedKeys(["UPDATE", "UPDATE", 4, "missing", "AVAILABLE"], self.module.TABS),
        )
        self.config.conf["serrebiStore"].update({
            "tabOrder": "not a list", "tabDefaults": '{"UPDATE":{"sort":"lastUpdated:desc"},"bad":{}}',
            "favourites": [" One ", 5, "", "TWO"],
        })
        self.assertEqual(list(self.module.TABS), self.module.tabOrder())
        self.assertEqual({"UPDATE": {"sort": "lastUpdated:desc"}}, self.module.tabDefaults())
        self.assertEqual({"one", "two"}, self.module.favouriteIds())
        self.config.conf["serrebiStore"]["tabDefaults"] = "{bad json"
        self.assertEqual({}, self.module.tabDefaults())


if __name__ == "__main__":
    unittest.main()
