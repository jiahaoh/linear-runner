"""Review coverage: exact on the pinned wording, tolerant of the marks a reviewer drops when copying (W-282)."""
import copy
import unittest

from linear_runner.engine.runner import align_review_criteria, criterion_key, review_criteria, validate_review_result

IDENTITIES = ('Multi-round identities (check S10). `spot_id` is unique `"0"`…`"N-1"`. A `(spot_namespace, spot_id)` '
              'merge with `validate="one_to_one"` succeeds. These bounds are provisional: a new interface without a '
              '<issue id="c25ea1bf" href="https://linear.example/issue/DEV-66/select">DEV-66</issue> reference. '
              'Evidence: `test_rounds.py`.')
SCALING = 'LoG intensity: a float image outside \\[0, 1\\] raises `ValueError`. Evidence: the *test*.'
ISSUE = {"id": "DEV-1", "description": f"## Acceptance criteria\n\n- [ ] {IDENTITIES}\n- [ ] {SCALING}\n"
                                         "- [ ] The default pytest tier passes. Evidence: the check logs.\n"}
COMMIT = "a" * 40


def result(criteria, **changes):
    return dict({"issue_id": "DEV-1", "commit": COMMIT, "status": "ready", "summary": "ok", "limitations": [],
                 "acceptance": [{"criterion": c, "satisfied": True, "evidence": "checked"} for c in criteria]}, **changes)


class ReviewCriteriaTests(unittest.TestCase):
    def setUp(self):
        self.pinned = review_criteria(ISSUE)
        self.assertEqual(len(self.pinned), 3)

    def test_the_exact_wording_passes_and_is_left_alone(self):
        returned = result(self.pinned)
        before = copy.deepcopy(returned)
        self.assertEqual(align_review_criteria(returned, ISSUE), [])
        validate_review_result(returned, ISSUE, COMMIT)
        self.assertEqual(returned, before)

    def test_dropped_backticks_are_covered_and_the_pinned_wording_is_kept(self):
        # The W-273 review: ready, every criterion satisfied, one copied as "0"…"N-1" without backticks.
        slipped = self.pinned[0].replace('`"0"`…`"N-1"`', '"0"…"N-1"')
        self.assertNotEqual(slipped, self.pinned[0])
        returned = result([slipped, *self.pinned[1:]])
        self.assertEqual(align_review_criteria(copy.deepcopy(returned), ISSUE), [(self.pinned[0], slipped)])
        validate_review_result(returned, ISSUE, COMMIT)
        self.assertEqual([e["criterion"] for e in returned["acceptance"]], self.pinned)
        # A stored result validates again without another change.
        self.assertEqual(align_review_criteria(returned, ISSUE), [])
        validate_review_result(returned, ISSUE, COMMIT)

    def test_other_copy_marks_are_tolerated(self):
        variants = [self.pinned[0].replace("…", "..."),
                    self.pinned[0].replace('<issue id="c25ea1bf" href="https://linear.example/issue/DEV-66/select">'
                                           'DEV-66</issue>', "DEV-66"),
                    self.pinned[0].replace("`", "").replace('"0"', "“0”"),
                    "  ".join(self.pinned[0].split(" "))]
        for text in variants:
            with self.subTest(text=text[:60]):
                self.assertNotEqual(text, self.pinned[0])
                validate_review_result(result([text, *self.pinned[1:]]), ISSUE, COMMIT)
        unescaped = self.pinned[1].replace("\\[", "[").replace("\\]", "]").replace("*test*", "test")
        validate_review_result(result([self.pinned[0], unescaped, self.pinned[2]]), ISSUE, COMMIT)

    def test_a_missing_reworded_or_invented_criterion_still_fails(self):
        with self.assertRaisesRegex(RuntimeError, r"omitted original checklist criteria \(1 missing; 2 returned\)"):
            validate_review_result(result(self.pinned[:2]), ISSUE, COMMIT)
        reworded = self.pinned[2].replace("default pytest tier", "default test tier")
        with self.assertRaisesRegex(RuntimeError, r"omitted original checklist criteria \(1 missing; 3 returned\)"):
            validate_review_result(result([*self.pinned[:2], reworded]), ISSUE, COMMIT)
        shortened = self.pinned[0].split(" These bounds")[0]
        with self.assertRaisesRegex(RuntimeError, "omitted original checklist criteria"):
            validate_review_result(result([shortened, *self.pinned[1:]]), ISSUE, COMMIT)
        with self.assertRaisesRegex(RuntimeError, "unexpected checklist criteria"):
            validate_review_result(result([*self.pinned, "The reviewer liked it."]), ISSUE, COMMIT)

    def test_a_repeated_criterion_still_fails(self):
        slipped = self.pinned[0].replace("`", "")
        with self.assertRaisesRegex(RuntimeError, "unexpected checklist criteria|repeated checklist criteria"):
            validate_review_result(result([self.pinned[0], slipped, *self.pinned[1:]]), ISSUE, COMMIT)
        with self.assertRaisesRegex(RuntimeError, "repeated checklist criteria"):
            validate_review_result(result([self.pinned[0], self.pinned[0], *self.pinned[1:]]), ISSUE, COMMIT)

    def test_criteria_that_share_a_key_keep_exact_matching(self):
        issue = {"id": "DEV-1", "description": "- [ ] Report `a*b` as the product.\n- [ ] Report `ab` as the product.\n"}
        first, second = review_criteria(issue)
        self.assertEqual(criterion_key(first), criterion_key(second))
        validate_review_result(result([first, second]), issue, COMMIT)
        with self.assertRaisesRegex(RuntimeError, "omitted original checklist criteria"):
            validate_review_result(result([first.replace("`", ""), second]), issue, COMMIT)

    def test_an_unsatisfied_criterion_is_still_incomplete(self):
        returned = result([self.pinned[0].replace("`", ""), *self.pinned[1:]])
        returned["acceptance"][0]["satisfied"] = False
        with self.assertRaisesRegex(RuntimeError, "Independent acceptance is incomplete"):
            validate_review_result(returned, ISSUE, COMMIT)


if __name__ == "__main__":
    unittest.main()
