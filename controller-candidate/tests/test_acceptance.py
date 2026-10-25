import tempfile
import unittest
from pathlib import Path

from acceptance import evaluate, manifest, reject


def fixture():
    manifest_digest = 'a' * 64
    sub = dict(task='B', revision='revB', epoch=1, current_epoch=1, author='E2',
               dependencies=[], files=['count.py', 'tests/test_count.py', 'brief.md'],
               manifest_digest=manifest_digest)
    policy = dict(required={'correctness': 'R', 'tests': 'R'}, checks=['unit'],
                  revision='revB', task='B', integration=False, manifest_digest=manifest_digest)
    reviews = [dict(reviewer='R', scope=scope, approve=True, revision='revB', epoch=1,
                    manifest_digest=manifest_digest, blockers=[], independently_reproduced=True)
               for scope in policy['required']]
    checks = [dict(name='unit', revision='revB', epoch=1, manifest_digest=manifest_digest,
                   producer='R', exit_code=0)]
    state = dict(rejected=[], withdrawn=[], dependencies={'combined': ['B']},
                 accepted=['B', 'combined'], integrated=[])
    return sub, reviews, checks, policy, state


class AcceptanceTests(unittest.TestCase):
    def test_every_scope_and_independent_current_check_is_required(self):
        sub, reviews, checks, policy, state = fixture()
        self.assertEqual(evaluate(sub, reviews, checks, policy, state), (True, []))
        for mutated in (reviews[:1], [dict(r, approve=False) for r in reviews],
                        [dict(r, reviewer='E2') for r in reviews],
                        [dict(r, revision='old') for r in reviews]):
            self.assertFalse(evaluate(sub, mutated, checks, policy, state)[0])
        self.assertFalse(evaluate(sub, reviews, [], policy, state)[0])
        self.assertFalse(evaluate(sub, reviews, [dict(checks[0], exit_code=1)], policy, state)[0])
        self.assertFalse(evaluate(dict(sub, current_epoch=2), reviews, checks, policy, state)[0])
        self.assertFalse(evaluate(sub, [dict(r, blockers=['bad']) for r in reviews], checks, policy, state)[0])

    def test_combined_requires_own_integration_coverage_and_dependency_acceptance(self):
        sub, reviews, checks, policy, state = fixture()
        sub.update(task='combined', revision='revCombined', dependencies=['B'], author='E1')
        policy.update(task='combined', revision='revCombined', integration=True,
                      required={'integration': 'R'}, manifest_digest='b' * 64)
        sub['manifest_digest'] = 'b' * 64
        reviews = [dict(reviewer='R', scope='integration', approve=True, revision='revCombined',
                        epoch=1, manifest_digest='b' * 64, blockers=[], independently_reproduced=True)]
        checks = [dict(name='unit', revision='revCombined', epoch=1, manifest_digest='b' * 64,
                       producer='R', exit_code=0)]
        self.assertTrue(evaluate(sub, reviews, checks, policy, state)[0])
        self.assertFalse(evaluate(sub, [], checks, policy, state)[0])
        self.assertFalse(evaluate(sub, reviews, checks, policy, dict(state, accepted=[]))[0])

    def test_rejection_withdraws_dependent_acceptance_preserves_evidence(self):
        sub, reviews, checks, policy, state = fixture()
        updated = reject(state, 'revB', 'unsuitable', task='B')
        self.assertEqual(state['accepted'], ['B', 'combined'])
        self.assertEqual(updated['accepted'], [])
        self.assertEqual(set(updated['withdrawn']), {'B', 'combined'})
        self.assertEqual(updated['rejected'][0]['revision'], 'revB')
        self.assertFalse(evaluate(sub, reviews, checks, policy, updated)[0])
        with self.assertRaises(ValueError):
            reject(dict(state, integrated=['B']), 'revB', 'unsuitable', task='B')

    def test_manifest_changes_on_test_or_brief_byte_and_rejects_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'code.py').write_text('a', encoding='utf-8')
            (root / 'brief.md').write_text('b', encoding='utf-8')
            (root / 'test.py').write_text('c', encoding='utf-8')
            first, entries = manifest(root)
            self.assertEqual(len(entries), 3)
            (root / 'test.py').write_text('d', encoding='utf-8')
            self.assertNotEqual(manifest(root)[0], first)
            (root / 'brief.md').write_text('e', encoding='utf-8')
            self.assertNotEqual(manifest(root)[0], first)


if __name__ == '__main__':
    unittest.main()
