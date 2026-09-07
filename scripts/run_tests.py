#!/usr/bin/env python3
"""Run isolated standalone tests; no live profile or credential environment."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--hermes-root', type=Path, required=True)
parser.add_argument('--python', default=sys.executable)
parser.add_argument('--noctuary-root', type=Path)
args = parser.parse_args()
repo = Path(__file__).resolve().parents[1]
source = args.hermes_root.resolve()
if not (source/'hermes_constants.py').is_file():
    parser.error('--hermes-root must name a Hermes source checkout')
paths = [str(repo), str(source)]
if args.noctuary_root:
    paths.append(str(args.noctuary_root.resolve()))
with tempfile.TemporaryDirectory(prefix='time-awareness-suite-') as temp:
    env = {'PATH':os.defpath, 'HOME':temp, 'HERMES_HOME':str(Path(temp)/'profile'),
           'TMPDIR':temp, 'XDG_CACHE_HOME':str(Path(temp)/'cache'),
           'PYTHONPATH':os.pathsep.join(paths), 'HERMES_AGENT_ROOT':str(source),
           'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONNOUSERSITE':'1', 'HF_HUB_OFFLINE':'1',
           'LANG':'C.UTF-8', 'TZ':'UTC'}
    if os.name == 'nt':
        env.update({key:os.environ[key] for key in ('SYSTEMROOT','WINDIR','COMSPEC') if key in os.environ})
    result = subprocess.run([args.python, '-m', 'pytest', '-q', '-s'], cwd=repo, env=env)
    raise SystemExit(result.returncode)
