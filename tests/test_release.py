import unittest
from tools.release_metadata import resolve


class ReleaseIntentTests(unittest.TestCase):
    def test_ordinary_ci_does_not_publish(self):
        for event, ref in [('push', 'refs/heads/main'), ('pull_request', 'refs/pull/1/merge')]:
            self.assertFalse(resolve(event, ref, {}, '4.7.0'))

    def test_exact_release_subject(self):
        self.assertTrue(resolve('push', 'refs/heads/main', {'head_commit': {'message': 'chore: release v4.7.0\n\nNotes'}}, '4.7.0'))
        with self.assertRaises(ValueError):
            resolve('push', 'refs/heads/main', {'head_commit': {'message': 'chore: release v4.7.0 extra'}}, '4.7.0')

    def test_tag_version_must_match(self):
        self.assertTrue(resolve('push', 'refs/tags/v4.7.0', {}, '4.7.0'))
        for tag in ['v4.6.3', 'invalid', 'v4.7.0-rc1']:
            with self.assertRaises(ValueError):
                resolve('push', f'refs/tags/{tag}', {}, '4.7.0')

    def test_dispatch_defaults_to_preview(self):
        self.assertFalse(resolve('workflow_dispatch', 'refs/heads/main', {'inputs': {'version': '4.7.0'}}, '4.7.0'))
        self.assertTrue(resolve('workflow_dispatch', 'refs/heads/main', {'inputs': {'version': '4.7.0', 'dry_run': 'false'}}, '4.7.0'))

    def test_dispatch_requires_main_and_matching_version(self):
        for ref, version in [('refs/heads/other', '4.7.0'), ('refs/heads/main', '9.9.9'), ('refs/heads/main', '')]:
            with self.assertRaises(ValueError):
                resolve('workflow_dispatch', ref, {'inputs': {'version': version}}, '4.7.0')
