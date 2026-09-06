"""Export the paper's .drawio files to vector PDFs with draw.io Desktop."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIGURES = ROOT / 'paper/figures'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--drawio', default=shutil.which('drawio'))
    p.add_argument('--xvfb', help='optional Xvfb executable for a headless host')
    args = p.parse_args()
    if not args.drawio:
        p.error('Supply --drawio or install draw.io Desktop.')
    env = os.environ.copy()
    display = None
    with tempfile.TemporaryDirectory(prefix='ehr-drawio-export-') as scratch:
        try:
            if args.xvfb:
                # Let Xvfb allocate an unused display and report its number.
                read_fd, write_fd = os.pipe()
                display = subprocess.Popen([args.xvfb, '-displayfd', str(write_fd), '-screen', '0', '1280x900x24', '-nolisten', 'tcp'],
                                           pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                os.close(write_fd)
                with os.fdopen(read_fd) as stream:
                    number = stream.readline().strip()
                if not number:
                    raise RuntimeError(display.stderr.read().decode())
                env['DISPLAY'] = ':' + number
            records = {}
            for name in ('architecture', 'fault_matrix', 'leakage'):
                source, target = FIGURES/(name+'.drawio'), FIGURES/(name+'.pdf')
                cmd = [args.drawio, '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
                       '--user-data-dir='+scratch, '--export', '--format', 'pdf', '--crop', '--border', '8',
                       '--output', str(target), str(source)]
                result = subprocess.run(cmd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
                if result.returncode or not target.exists():
                    raise RuntimeError(result.stdout[-3000:])
                records[name] = {'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                                 'pdf_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
                print('Exported', target.relative_to(ROOT), flush=True)
            (FIGURES/'export_manifest.json').write_text(json.dumps({'renderer':'draw.io Desktop', 'figures':records},indent=2)+'\n')
        finally:
            if display:
                display.terminate()
                display.wait(timeout=10)


if __name__ == '__main__':
    main()
