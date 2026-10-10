"""Dev8 regressions for truthful Store dates and unknown-last sorting."""
import importlib.util
from pathlib import Path
import types
import unittest


PATH = Path(__file__).resolve().parents[1] / "helper" / "globalPlugins" / "_addonStoreChangelogs.py"
SPEC = importlib.util.spec_from_file_location("addonStoreDatesDev8", PATH)
dates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dates)


class Model:
    def __init__(self, **values):
        self.__dict__.update(values)


class Plugin:
    def __init__(self):
        self.patches = []

    def _rememberPatch(self, owner, name, replacement):
        original = getattr(owner, name)
        setattr(owner, name, replacement)
        self.patches.append((owner, name, original, replacement))


class DateSemanticsTests(unittest.TestCase):
    def test_first_publication_has_no_addon_specific_overrides(self):
        for addon in ("alpha", "beta", "zoomEnhancements"):
            self.assertEqual((None, None), dates._provenFirstPublication({"addonId": addon}))

    def test_author_identified_initial_release_works_for_unrelated_repositories(self):
        for repository in ("author-one/alpha", "author-two/beta"):
            model = Model(sourceURL="https://github.com/" + repository)
            records = [{"tag_name": "v1.0", "body": "- Initial release.",
                        "published_at": "2020-01-02T12:00:00Z"}]
            dates._learnFirstPublication(model, records)
            self.assertEqual(1577966400000, dates.publicationTime(model))
            self.assertEqual("https://github.com/" + repository + "/releases/tag/v1.0",
                             dates.publicationSource(model))

    def test_oldest_retained_release_or_ambiguous_claim_does_not_set_first_date(self):
        model = Model(sourceURL="https://github.com/owner/project")
        record = {"tag_name": "1.0", "body": "A normal release", "published_at": "2020-01-02T12:00:00Z"}
        dates._learnFirstPublication(model, [record])
        self.assertIsNone(dates.publicationTime(model))
        record["body"] = "Initial release"
        dates._learnFirstPublication(model, [record, dict(record, tag_name="2.0")])
        self.assertIsNone(dates.publicationTime(model))

    def test_explicit_first_publication_requires_provenance(self):
        self.assertEqual(
            (123, "https://evidence.example/initial-release"),
            dates._provenFirstPublication({
                "originalPublicationTime": 123,
                "originalPublicationSource": "https://evidence.example/initial-release",
            }),
        )
        self.assertEqual(
            (None, None),
            dates._provenFirstPublication({"originalPublicationTime": 123}),
        )

    def test_retrieved_history_never_establishes_first_publication(self):
        record = {"tag_name": "1.0", "body": "Initial release",
                  "published_at": "2020-01-02T12:00:00Z"}
        for error, attributes, start_page in (
            ("rateLimit", {}, 1), ("historyLimit", {}, 1),
            (None, {"limitReached": True}, 1), (None, {"nextPage": 2}, 1),
            (None, {"usedCached": True}, 1), (None, {}, 2),
        ):
            with self.subTest(error=error, attributes=attributes, start_page=start_page):
                model = Model(sourceURL="https://github.com/owner/project")
                records = dates.ReleaseRecords([record])
                for name, value in attributes.items():
                    setattr(records, name, value)
                rows = dates._historyRows(model, records, error, "github", start_page)
                self.assertIsNone(dates.publicationTime(model))
                self.assertIsNone(dates.publicationSource(model))
                self.assertIn("Initial release", rows[0][1])
        complete = Model(sourceURL="https://github.com/owner/project")
        dates._historyRows(complete, [record], None, "github")
        self.assertIsNone(dates.publicationTime(complete))
        self.assertIsNone(dates.publicationSource(complete))

    def test_first_release_phrase_without_public_scope_is_not_evidence(self):
        model = Model(sourceURL="https://github.com/owner/project")
        dates._learnFirstPublication(model, [{"tag_name": "1.0", "body": "First release",
                                             "published_at": "2020-01-02T12:00:00Z"}])
        self.assertIsNone(dates.publicationTime(model))

    def test_native_submission_time_is_last_updated_and_never_first_publication(self):
        model = Model(
            submissionTime=300,
            _serrebiLastUpdatedTime=200,
            _serrebiOriginalPublicationTime=100,
            _serrebiOriginalPublicationSource="https://evidence.example/initial",
        )
        self.assertEqual(300, dates.updatedTime(model))
        self.assertEqual(100, dates.publicationTime(model))
        self.assertEqual("https://evidence.example/initial", dates.publicationSource(model))

    def test_date_placeholders_are_unknown(self):
        for value in (0, None, "", "Unknown"):
            self.assertIsNone(dates.updatedTime(Model(submissionTime=value)))
            self.assertIsNone(dates.publicationTime(Model(_serrebiOriginalPublicationTime=value)))

    def _patchedListTypes(self):
        fieldNames = (
            "searchRank", "displayName", "status", "currentAddonVersionName",
            "availableAddonVersionName", "channel", "publisher", "author",
            "publicationDate", "installDate", "minimumNVDAVersion", "lastTestedVersion",
        )
        fields = {name: types.SimpleNamespace(name=name, displayString=name) for name in fieldNames}
        addonListField = types.SimpleNamespace(**fields)

        class StoreModel:
            publicationDate = property(lambda self: "native-current-version-date")

        class Item:
            def __init__(self, identity, model):
                self.Id = identity
                self.model = model

        class List:
            _columnSortChoices = property(lambda self: ["native"])

            def _getAddonFieldText(self, item, field):
                return getattr(item.model, field.name, "")

            def _getFilteredSortedIds(self):
                allowed = list(self.allowed)
                if self._reverseSort:
                    return list(reversed(allowed))
                return allowed

            def setSortField(self, field, reverse=False):
                self._sortByModelField = field
                self._reverseSort = reverse

        listModule = types.SimpleNamespace(AddonListVM=List, AddonListField=addonListField)
        modelModule = types.SimpleNamespace(_AddonStoreModel=StoreModel)
        modules = {
            "gui.addonStoreGui.viewModels.addonList": listModule,
            "addonStore.models.addon": modelModule,
        }

        class Loader:
            @staticmethod
            def import_module(name):
                if name in modules:
                    return modules[name]
                raise ImportError(name)

        feature = dates.ChangelogFeature(Plugin())
        feature._patchSort(Loader,)
        return fields, List, Item, StoreModel

    def test_every_native_text_field_keeps_unknown_last_in_both_directions(self):
        fields, List, Item, _storeModel = self._patchedListTypes()
        for name, field in fields.items():
            if name in ("searchRank", "publicationDate"):
                continue
            for placeholder in (None, "", "Unknown"):
                with self.subTest(field=name, placeholder=placeholder):
                    low = Model(**{name: "Alpha"})
                    high = Model(**{name: "Zulu"})
                    missing = Model(**{name: placeholder})
                    vm = List()
                    vm._addons = {
                        "low": Item("low", low), "high": Item("high", high),
                        "missing": Item("missing", missing),
                    }
                    vm.allowed = ["low", "high", "missing"]
                    vm._sortByModelField = field
                    vm._serrebiDateSort = None
                    vm._reverseSort = False
                    self.assertEqual(["low", "high", "missing"], vm._getFilteredSortedIds())
                    vm._reverseSort = True
                    self.assertEqual(["high", "low", "missing"], vm._getFilteredSortedIds())

    def test_relevance_zero_and_native_filter_membership_are_preserved(self):
        fields, List, Item, _storeModel = self._patchedListTypes()
        vm = List()
        vm._addons = {
            "zero": Item("zero", Model(searchRank=0)),
            "one": Item("one", Model(searchRank=1)),
            "filteredOut": Item("filteredOut", Model(searchRank=2)),
        }
        vm.allowed = ["zero", "one"]
        vm._sortByModelField = fields["searchRank"]
        vm._serrebiDateSort = None
        vm._reverseSort = False
        self.assertEqual(["zero", "one"], vm._getFilteredSortedIds())
        vm._reverseSort = True
        self.assertEqual(["one", "zero"], vm._getFilteredSortedIds())

    def test_former_publication_column_uses_last_updated_and_ignores_first_date(self):
        fields, List, Item, _storeModel = self._patchedListTypes()
        vm = List()
        vm._addons = {
            "oldFirstNewUpdate": Item("oldFirstNewUpdate", Model(
                displayName="A", addonId="a", submissionTime=300,
                _serrebiOriginalPublicationTime=100,
            )),
            "newFirstOldUpdate": Item("newFirstOldUpdate", Model(
                displayName="B", addonId="b", submissionTime=200,
                _serrebiOriginalPublicationTime=200,
            )),
            "missing": Item("missing", Model(
                displayName="C", addonId="c", submissionTime=0,
                _serrebiOriginalPublicationTime=None,
            )),
        }
        vm.allowed = ["oldFirstNewUpdate", "newFirstOldUpdate", "missing"]
        vm._sortByModelField = fields["publicationDate"]
        vm._serrebiDateSort = None
        vm._reverseSort = False
        self.assertEqual(
            ["newFirstOldUpdate", "oldFirstNewUpdate", "missing"],
            vm._getFilteredSortedIds(),
        )
        vm._reverseSort = True
        self.assertEqual(
            ["oldFirstNewUpdate", "newFirstOldUpdate", "missing"],
            vm._getFilteredSortedIds(),
        )
        vm._serrebiDateSort = True
        self.assertEqual(
            ["oldFirstNewUpdate", "newFirstOldUpdate", "missing"],
            vm._getFilteredSortedIds(),
        )

    def test_readonly_details_only_show_catalog_date_with_context_and_unknown(self):
        publicationField = types.SimpleNamespace(name="publicationDate", displayString="Publication date")

        class StoreModel:
            publicationDate = property(lambda self: "native-current-version-date")

            def __init__(self, **values):
                self.__dict__.update(values)

        class List:
            _columnSortChoices = property(lambda self: ["native"])
            _getFilteredSortedIds = lambda self: []
            setSortField = lambda self, *_args, **_kwargs: None
            _getAddonFieldText = lambda self, item, field: ""

        class Details:
            def _appendDetailsLabelValue(self, label, value):
                self.rows.append((label, value))

            def _refresh(self):
                details = self._detailsVM.listItem.model
                if details.publicationDate is not None:
                    self._appendDetailsLabelValue("Publication date:", details.publicationDate)

        modules = {
            "gui.addonStoreGui.viewModels.addonList": types.SimpleNamespace(
                AddonListVM=List,
                AddonListField=types.SimpleNamespace(publicationDate=publicationField),
            ),
            "addonStore.models.addon": types.SimpleNamespace(_AddonStoreModel=StoreModel),
            "gui.addonStoreGui.controls.details": types.SimpleNamespace(AddonDetails=Details),
        }

        modules["gui.addonStoreGui.controls.details"].pgettext = lambda context, text: (
            "Native contextual date:" if context == "addonStore" and text == "Publication date:" else text
        )
        Details._refresh = lambda self: self._appendDetailsLabelValue(
            "Native contextual date:", self._detailsVM.listItem.model.publicationDate
        ) if self._detailsVM.listItem.model.publicationDate is not None else None

        class Loader:
            @staticmethod
            def import_module(name):
                if name in modules:
                    return modules[name]
                raise ImportError(name)

        dates.ChangelogFeature(Plugin())._patchSort(Loader)
        known = StoreModel(
            submissionTime=1600000000000,
            _serrebiOriginalPublicationTime=1599303382000,
            _serrebiOriginalPublicationSource="https://evidence.example/initial",
        )
        view = Details()
        view.rows = []
        view._detailsVM = types.SimpleNamespace(listItem=types.SimpleNamespace(model=known))
        view._refresh()
        self.assertEqual("Last updated:", view.rows[0][0])
        self.assertNotEqual("Unknown", view.rows[0][1])
        self.assertEqual("Date meaning:", view.rows[1][0])
        self.assertEqual(2, len(view.rows))
        self.assertFalse(any("First publication" in label for label, value in view.rows))

        retained_append = view._appendDetailsLabelValue
        view._appendDetailsLabelValue = retained_append
        view.rows = []
        view._refresh()
        self.assertIs(view._appendDetailsLabelValue, retained_append)
        self.assertEqual("Last updated:", view.rows[0][0])

        unknown = StoreModel(submissionTime=0)
        view.rows = []
        view._detailsVM.listItem = types.SimpleNamespace(model=unknown)
        view._refresh()
        self.assertEqual(("Last updated:", "Unknown"), view.rows[0])
        self.assertEqual("Date meaning:", view.rows[1][0])
        self.assertEqual(2, len(view.rows))

    def test_details_refresh_skips_destroyed_views_before_access(self):
        class StoreModel:
            publicationDate = property(lambda self: "native")

        class Details:
            def _appendDetailsLabelValue(self, label, value):
                raise AssertionError("destroyed view was accessed")

            def _refresh(self):
                raise AssertionError("native refresh should not run")

        modules = {
            "gui.addonStoreGui.controls.details": types.SimpleNamespace(AddonDetails=Details),
            "addonStore.models.addon": types.SimpleNamespace(_AddonStoreModel=StoreModel),
        }

        class Loader:
            @staticmethod
            def import_module(name):
                if name in modules:
                    return modules[name]
                raise ImportError(name)

        dates.ChangelogFeature(Plugin())._patchDateDetails(Loader, modules["addonStore.models.addon"])
        view = Details()
        view._isBeingDestroyed = True
        view._refresh()

    def test_details_refresh_rechecks_destruction_and_restores_append_override(self):
        class StoreModel:
            publicationDate = property(lambda self: "native")

        class Details:
            def _appendDetailsLabelValue(self, label, value):
                self.rows.append((label, value))

            def _refresh(self):
                self._isBeingDestroyed = True
                self._appendDetailsLabelValue("Publication date:", "native")

        modules = {
            "gui.addonStoreGui.controls.details": types.SimpleNamespace(AddonDetails=Details),
            "addonStore.models.addon": types.SimpleNamespace(_AddonStoreModel=StoreModel),
        }

        class Loader:
            @staticmethod
            def import_module(name):
                if name in modules:
                    return modules[name]
                raise ImportError(name)

        plugin = Plugin()
        dates.ChangelogFeature(plugin)._patchDateDetails(Loader, modules["addonStore.models.addon"])
        view = Details()
        view.rows = []
        view._detailsVM = types.SimpleNamespace(listItem=types.SimpleNamespace(model=StoreModel()))
        originalAppend = view._appendDetailsLabelValue
        view._refresh()
        self.assertEqual([("Last updated:", "native")], view.rows)
        self.assertIs(view._appendDetailsLabelValue.__func__, originalAppend.__func__)


if __name__ == "__main__":
    unittest.main()
