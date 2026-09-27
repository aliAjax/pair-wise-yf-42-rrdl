import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, BatchValidationError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _litter(rows):
    return [
        {
            "id": rows[0],
            "name": rows[1],
            "sex": rows[2],
            "birth_date": rows[3],
        }
        for rows in rows
    ]


class BirthRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.sire = self.service.create(
            self.admin, "animal", {"name": "Sire", "sex": "male"}
        )
        self.dam = self.service.create(
            self.admin, "animal", {"name": "Dam", "sex": "female"}
        )
        self.pairing = self.service.create(
            self.admin, "pairing", {"proposed_by": "coordinator"}
        )
        self.service.transition(
            self.admin,
            self.pairing["id"],
            "approve",
            {"sire_id": self.sire["id"], "dam_id": self.dam["id"], "approvals": ["vet"]},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _complete(self, offspring, actor=None, expected_version=None):
        return self.service.transition(
            actor or self.admin,
            self.pairing["id"],
            "complete",
            {"offspring": offspring},
            expected_version,
        )

    def test_successful_registration_creates_records_with_parents(self):
        pairing = self._complete(
            _litter([
                ("cub-1", "Baby One", "male", "2026-04-01"),
                ("cub-2", "Baby Two", "female", "2026-04-01"),
            ])
        )
        self.assertEqual(pairing["status"], "completed")
        self.assertEqual(pairing["version"], 3)
        self.assertEqual(pairing["data"]["offspring_ids"], ["cub-1", "cub-2"])

        cub1 = self.service.get("cub-1")
        self.assertEqual(cub1["status"], "active")
        self.assertEqual(cub1["data"]["name"], "Baby One")
        self.assertEqual(cub1["data"]["sire_id"], self.sire["id"])
        self.assertEqual(cub1["data"]["dam_id"], self.dam["id"])
        self.assertEqual(cub1["data"]["birth_date"], "2026-04-01")
        self.assertEqual(cub1["data"]["pairing_id"], self.pairing["id"])

        litter = self.service.offspring_of(self.sire["id"])
        self.assertEqual({item["id"] for item in litter}, {"cub-1", "cub-2"})

    def test_batch_duplicate_id_marks_conflicting_cubs(self):
        with self.assertRaises(BatchValidationError) as caught:
            self._complete(
                _litter([
                    ("dup", "A", "male", "2026-04-01"),
                    ("dup", "B", "female", "2026-04-01"),
                    ("ok", "C", "female", "2026-04-01"),
                ])
            )
        conflict_indexes = {item["index"] for item in caught.exception.conflicts}
        self.assertEqual(conflict_indexes, {0, 1})

    def test_existing_record_marks_only_that_cub(self):
        self.service.create(
            self.admin,
            "animal",
            {"id": "taken", "name": "Taken", "sex": "male"},
        )
        with self.assertRaises(BatchValidationError) as caught:
            self._complete(
                _litter([
                    ("new-1", "A", "male", "2026-04-01"),
                    ("taken", "B", "female", "2026-04-01"),
                ])
            )
        conflicts = caught.exception.conflicts
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["index"], 1)
        self.assertEqual(conflicts[0]["id"], "taken")
        self.assertTrue(
            any("already exists" in reason for reason in conflicts[0]["reasons"])
        )

    def test_invalid_fields_report_each_problem(self):
        with self.assertRaises(BatchValidationError) as caught:
            self._complete([
                {"id": "  ", "name": "", "sex": "unknown", "birth_date": "01/04/2026"},
            ])
        conflict = caught.exception.conflicts[0]
        self.assertEqual(conflict["index"], 0)
        joined = " ".join(conflict["reasons"])
        self.assertIn("id", joined)
        self.assertIn("name", joined)
        self.assertIn("sex", joined)
        self.assertIn("birth_date", joined)

    def test_future_birth_date_is_rejected(self):
        with self.assertRaises(BatchValidationError) as caught:
            self._complete(
                _litter([("cub-x", "A", "male", "2099-01-01")])
            )
        self.assertIn("future", caught.exception.conflicts[0]["reasons"][0])

    def test_failed_batch_writes_nothing_and_keeps_pairing_approved(self):
        with self.assertRaises(BatchValidationError):
            self._complete(
                _litter([
                    ("good", "Good", "male", "2026-04-01"),
                    ("good", "Dup", "female", "2026-04-01"),
                ])
            )
        pairing = self.service.get(self.pairing["id"])
        self.assertEqual(pairing["status"], "approved")
        self.assertEqual(pairing["version"], 2)
        self.assertNotIn("offspring_ids", pairing["data"])
        self.assertEqual(self.service.list("animal"), [self.sire, self.dam])

        # After fixing the conflict the approved pairing can be registered.
        pairing = self._complete(
            _litter([("good", "Good", "male", "2026-04-01")])
        )
        self.assertEqual(pairing["status"], "completed")
        self.assertEqual(self.service.get("good")["data"]["sire_id"], self.sire["id"])

    def test_empty_litter_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._complete([])

    def test_cannot_complete_from_non_approved_status(self):
        self._complete(_litter([("cub-1", "A", "male", "2026-04-01")]))
        from src.domain import InvalidTransition

        with self.assertRaises(InvalidTransition):
            self._complete(_litter([("cub-2", "B", "male", "2026-04-01")]))

    def test_viewer_cannot_register_birth(self):
        with self.assertRaises(PermissionDenied):
            self._complete(
                _litter([("cub-1", "A", "male", "2026-04-01")]),
                actor=Actor("nosey", "viewer"),
            )

    def test_version_conflict_aborts_whole_batch(self):
        with self.assertRaises(Exception):
            self._complete(
                _litter([("cub-1", "A", "male", "2026-04-01")]),
                expected_version=999,
            )
        pairing = self.service.get(self.pairing["id"])
        self.assertEqual(pairing["status"], "approved")
        self.assertEqual(len(self.service.list("animal")), 2)


if __name__ == "__main__":
    unittest.main()
