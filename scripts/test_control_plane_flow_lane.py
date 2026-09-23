"""A verified provider report must not authorize a different configured lane."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('gateway', Path(__file__).with_name('control_plane_flow_gateway.py'))
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class LaneBindingTests(unittest.TestCase):
    def test_cross_builder_report_cannot_authorize_dispatch(self):
        policy = {'enabled': True, 'lanes': {'GLM': {'enabled': True, 'binding': {}, 'runtime_sha': 'a'*40}}}
        ports = gateway.GithubPorts(Mock(), policy)
        ports.bound_comment = Mock(return_value=({'report': {'builder_id': 'DEVIN'},
                                                 'independent_audit': 'PASS'}, {}))
        snapshot = {'repository': 'owner/repo', 'builder_id': 'GLM', 'dispatch_authorized': True,
                    'blockers': [], 'dependencies_verified': True}
        with self.assertRaisesRegex(gateway.flow.FlowError, 'another builder'):
            ports.dispatch_authorized(snapshot)


if __name__ == '__main__':
    unittest.main()
