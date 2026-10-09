"""Retain immutable input/output receipts and the measured dock screenshot."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

evidence = Path(os.environ['SIMIT_NIX_BUILD_RESULTS'])
results = json.loads((evidence / 'result.json').read_text())
assert len(results) == 1 and results[0]['outputs'], 'missing selected output'
for output in results[0]['outputs'].values():
    assert output.startswith('/nix/store/') and Path(output).exists(), 'output not realized'
source_tree = subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], text=True).strip()
lock = Path('flake.lock')
shutil.copyfile(lock, evidence / 'flake.lock')
receipt = {
    'revision': (evidence / 'revision').read_text().strip(),
    'sourceTree': source_tree,
    'inputLockSha256': hashlib.sha256(lock.read_bytes()).hexdigest(),
    'installable': (evidence / 'installable').read_text().strip(),
    'derivation': results[0]['drvPath'],
    'outputs': results[0]['outputs'],
    'run': os.environ['GITHUB_RUN_ID'],
    'status': 'passed',
}
for output in results[0]['outputs'].values():
    for name in ['desk.png','real-desk.png']:
        screenshot = Path(output) / name
        if screenshot.is_file():
            shutil.copyfile(screenshot, evidence / name)
            receipt.setdefault('screenshots',{})[name] = hashlib.sha256(screenshot.read_bytes()).hexdigest()
            if name == 'desk.png':
                receipt['screenshotSha256'] = receipt['screenshots'][name]
(evidence / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
