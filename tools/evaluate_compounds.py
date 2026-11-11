#!/usr/bin/env python3
"""Recheck the original explicit compound cases against the current code.

This is targeted real-model regression evidence, not a full-suite or accuracy claim.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pdfdesk.core import Store
from pdfdesk.model import Ollama
from pdfdesk.selection import question_clauses
from pdfdesk.service import Service
from tools.evaluate_live import answer_checks, write_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='qwen3:4b-instruct')
    parser.add_argument('--base-url', default='http://127.0.0.1:11434')
    parser.add_argument('--output', type=Path, default=Path('evidence/compounds.json'))
    args = parser.parse_args()
    manifest_path = ROOT / 'examples/evaluation.json'
    manifest = json.loads(manifest_path.read_text())
    cases = [case for case in manifest['cases'] if len(question_clauses(case['question'])) > 1]
    report = {'status': 'running', 'scope': 'Targeted original compound cases; not the complete suite',
              'model': args.model, 'started_at': datetime.now(timezone.utc).isoformat(),
              'planned_case_ids': [case['id'] for case in cases],
              'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
              'code_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                             for name in ['pdfdesk/core.py', 'pdfdesk/model.py', 'pdfdesk/service.py',
                                          'pdfdesk/selection.py', 'pdfdesk/summary.py', 'tools/evaluate_compounds.py']},
              'cases': [], 'manual_review_required': True}
    write_report(args.output, report)
    model = Ollama(base_url=args.base_url, model=args.model)
    try:
        with tempfile.TemporaryDirectory(prefix='pdfdesk-compounds-') as temporary:
            store = Store(Path(temporary))
            docs = {spec['file']: store.import_pdf((ROOT / 'examples' / spec['file']).read_bytes(), spec['file'])
                    for spec in manifest['documents']}
            service = Service(store, model)
            for case in cases:
                start = len(model.trace)
                document_id = docs[case['document']]['id']
                actual = service.answer(document_id, case['question'])
                checks = answer_checks(actual, case, document_id, store)
                entry = {'id': case['id'], 'expected': case, 'actual': actual, 'checks': checks,
                         'passed': all(check['passed'] for check in checks), 'model_trace': model.trace[start:]}
                report['cases'].append(entry)
                write_report(args.output, report)
                print(case['id'], entry['passed'], flush=True)
            report['status'] = ('smoke_checks_passed_manual_review_required'
                                if len(report['cases']) == len(cases) and all(e['passed'] for e in report['cases']) else 'failed')
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = {'type': type(error).__name__, 'message': str(error)}
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        write_report(args.output, report)
    print(report['status'], args.output, flush=True)
    return 0 if report['status'] == 'smoke_checks_passed_manual_review_required' else 1


if __name__ == '__main__':
    raise SystemExit(main())
