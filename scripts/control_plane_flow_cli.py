#!/usr/bin/env python3
"""Offline lane evidence validation / explicit installed-host preflight. No launch."""
import argparse
import importlib.util
from pathlib import Path
import subprocess

_spec = importlib.util.spec_from_file_location('flow', Path(__file__).with_name('control_plane_flow.py'))
flow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(flow)
HOST = ('/usr/bin/sudo', '-n', '-u', 'astra-control', '/opt/astra/bin/astra-host-control')


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    qualify = commands.add_parser('qualify-lane')
    qualify.add_argument('--report', type=Path, required=True)
    qualify.add_argument('--approval', type=Path, required=True)
    qualify.add_argument('--runtime-sha', required=True)
    probe = commands.add_parser('host-preflight')
    probe.add_argument('--builder', choices=('DEVIN', 'GROK_BUILD', 'GLM'), required=True)
    args = parser.parse_args()
    try:
        if args.command == 'qualify-lane':
            result = flow.qualify_lane(flow.decode(args.report.read_bytes()),
                                       flow.decode(args.approval.read_bytes()), args.runtime_sha)
        else:
            # No bypass: disabled lanes remain denied by the existing protected host policy.
            run = subprocess.run([*HOST, 'preflight', '--builder-id', args.builder],
                                 env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, cwd='/',
                                 capture_output=True, text=True, timeout=30, check=False)
            flow.require(run.returncode == 0, 'host preflight unavailable or denied; not a PASS')
            report = flow.decode(run.stdout)
            flow.require(report.get('builder_id') == args.builder and report.get('status') == 'PASS',
                         'host report mismatch')
            result = {'status': 'HOST_REPORTED_PREFLIGHT', 'builder': args.builder,
                      'report_digest': flow.digest(report), 'production_enabled': False}
        print(flow.canonical(result))
        return 0
    except (flow.FlowError, OSError, ValueError, subprocess.SubprocessError):
        print(flow.canonical({'status': 'BLOCKED', 'production_enabled': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
