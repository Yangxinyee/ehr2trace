"""Build the paper from a clean directory and package only its arXiv source inputs.

Requires pdflatex and bibtex. Figures are TikZ sources under paper/figures and are
compiled as part of this build. This command does not submit.
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PAPER = ROOT / 'paper'


def input_files():
    todo=[Path('main.tex')]
    seen=set()
    while todo:
        name=todo.pop()
        if name in seen:
            continue
        seen.add(name)
        content=(PAPER/name).read_text()
        for x in re.findall(r'\\input\{([^}]+)\}',content):
            todo.append(Path(x if x.endswith('.tex') else x+'.tex'))
        for x in re.findall(r'\\includegraphics(?:\[[^]]*\])?\{([^}]+)\}',content):
            seen.add(Path(x))
    seen.add(Path('refs.bib'))
    return sorted(seen)


def main():
    files=input_files()
    with tempfile.TemporaryDirectory(prefix='ehr-paper-clean-') as name:
        build=Path(name)
        for f in files:
            (build/f).parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(PAPER/f,build/f)
        for cmd in [['pdflatex','-interaction=nonstopmode','-halt-on-error','main.tex'],
                    ['bibtex','main'],
                    ['pdflatex','-interaction=nonstopmode','-halt-on-error','main.tex'],
                    ['pdflatex','-interaction=nonstopmode','-halt-on-error','main.tex'],
                    ['pdflatex','-interaction=nonstopmode','-halt-on-error','main.tex']]:
            p=subprocess.run(cmd,cwd=build,text=True,errors='replace',
                         stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            if p.returncode:
                print(p.stdout[-7000:])
                raise SystemExit(f'Failed: {cmd}')
        log=(build/'main.log').read_text(errors='replace')
        problems=[x for x in log.splitlines() if any(t in x for t in ('Overfull', 'undefined', 'LaTeX Warning:', 'Package natbib Warning:'))]
        if problems:
            print('\n'.join(problems))
            shutil.copy2(build/'main.pdf',PAPER/'main.pdf')
            shutil.copy2(build/'main.log',PAPER/'main.log')
            raise SystemExit('Resolve layout/reference warnings before packaging.')
        for f in ('main.pdf','main.bbl','main.log'):
            shutil.copy2(build/f,PAPER/f)
        with zipfile.ZipFile(PAPER/'arxiv-source.zip','w',zipfile.ZIP_DEFLATED) as archive:
            for f in files:
                archive.write(build/f,str(f))
            archive.write(build/'main.bbl','main.bbl')
        print('Clean LaTeX/BibTeX build passed; wrote paper/main.pdf and paper/arxiv-source.zip.')


if __name__=='__main__':
    main()
