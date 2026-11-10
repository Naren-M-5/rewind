"""Resumable real-sandbox runner. Talks to the running app, never opens a second task writer."""
import argparse
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2))


def request(client, method, path, body=None):
    r = client.request(method, path, json=body)
    if r.status_code != 200:
        try:
            detail = r.json().get('detail', 'Request failed')
        except ValueError:
            detail = 'Request failed'
        raise ValueError(f'REWIND HTTP {r.status_code}: {detail}')
    return r.json()


def check(report, name, passed, **facts):
    report['checks'][name] = {'passed': bool(passed), 'checked_at': stamp(), **facts}


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'resume'])
    parser.add_argument('--scenario', choices=['capture_timeout','delivery_loss','true_failure'], default='true_failure')
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--url', default=os.getenv('REWIND_PUBLIC_URL','http://127.0.0.1:8000'))
    args = parser.parse_args(argv)
    report = {'format': 'rewind-verification-v1', 'provider': 'sandbox', 'created_at': stamp(),
              'scenario': args.scenario, 'checks': {}, 'complete': False}
    if args.action == 'resume':
        report = json.loads(args.report.read_text())
    elif args.report.exists():
        parser.error('Report already exists; use resume to preserve the original task')
    try:
        with httpx.Client(base_url=args.url.rstrip('/'), timeout=float(os.getenv('REWIND_VERIFY_TIMEOUT', '360')),
            headers={'X-Rewind-Token':os.getenv('REWIND_API_TOKEN','')}, follow_redirects=False,
            trust_env=not args.url.startswith(('http://127.0.0.1:', 'http://localhost:'))) as client:
            config = request(client, 'GET', '/api/config')
            if config['provider'] != 'sandbox' or config.get('read_only'):
                raise ValueError('Runner requires the actual sandbox app, not emulator or replay mode')
            if config.get('merchant') != 'http':
                raise ValueError('Runner requires the separate HTTP merchant')
            if args.action == 'start':
                task = request(client,'POST','/api/tasks',{'scenario':args.scenario})
                report['task_id'] = task['id']
                save(args.report, report)  # persist task before attempting order creation
                task = request(client,'POST','/api/tasks/'+task['id']+'/order',{})
                report['order_id'] = task['order_id']
                check(report,'order_created',bool(task['order_id']) and not task['order_id'].startswith('SIM-'))
                save(args.report, report)
                print('Order created. Approve using your personal sandbox buyer at:')
                print(task.get('approval_url') or 'Open the task dashboard and select Approve in sandbox.')
                print('Then run: python -m rewind.verify_sandbox resume --report ' + str(args.report))
                return 0
            tid = report['task_id']; base = '/api/tasks/' + tid
            task = request(client, 'GET', base)
            if task['provider'] != 'sandbox' or task['scenario'] != report['scenario']:
                raise ValueError('Report/task binding mismatch')
            if not task['order_id']:
                task = request(client,'POST',base+'/order',{})
                report['order_id']=task['order_id'];save(args.report,report)
                print('Order recovered. Approve it through the dashboard, then resume.');return 2
            if report.get('order_id') and task['order_id'] != report['order_id']:
                raise ValueError('Order changed; refusing to operate on a different payment')
            report['order_id']=task['order_id']
            check(report,'order_created',True)
            task = request(client,'POST',base+'/capture',{'drop_response':report['scenario']=='capture_timeout'})
            if report['scenario']=='capture_timeout':
                fault = request(client,'GET',base+'/fault-evidence')
                check(report,'capture_socket_dropped',fault.get('mode')=='drop_after' and 200<=fault.get('upstream_status',0)<300,
                      **fault)
                task = request(client,'POST',base+'/capture',{})
            task = request(client,'POST',base+'/reconcile',{})
            report['capture_id']=task['capture_id']
            check(report,'single_capture',task.get('capture_count')==1 and task['payment'] in {'captured','refunded'},
                  capture_count=task.get('capture_count'),payment=task['payment'])
            save(args.report,report)
            if task['payment']=='captured':
                task=request(client,'POST',base+'/merchant',{})
            if not task.get('diagnosis',{} ) or not task['diagnosis'].get('live_ai') or task.get('needs_diagnosis'):
                task=request(client,'POST',base+'/diagnose',{})
            diagnosis=task.get('diagnosis') or {}
            check(report,'live_ai',diagnosis.get('live_ai',False),provider=diagnosis.get('provider'),
                  model=diagnosis.get('model'),latency_ms=diagnosis.get('latency_ms'),
                  recommendation=diagnosis.get('recommendation'),conflict_type=diagnosis.get('conflict_type'),
                  evidence_ids=diagnosis.get('evidence_ids',[]))
            save(args.report,report)
            if not diagnosis.get('live_ai'):
                raise ValueError('Live diagnosis not verified. Configure a hosted key or Ollama, restart, then resume.')
            task=request(client,'POST',base+'/recover',{})
            check(report,'recovery',task['payment']=='refunded' if report['scenario']=='true_failure' else
                  task['payment']=='captured' and task['delivery']=='delivered' and bool(task.get('receipt_id')),
                  payment=task['payment'],delivery=task['delivery'],receipt_id=task.get('receipt_id'),
                  action=(task.get('decision') or {}).get('action'))
            report['refund_id']=task['refund_id']
            if report['scenario']=='true_failure':
                check(report,'refund_id_recorded',bool(task['refund_id']))
            report['complete']=all(c['passed'] for c in report['checks'].values())
            report['finished_at']=stamp()
            report.pop('error',None)
            save(args.report,report)
            print('Verified report saved: '+str(args.report))
            print('PASS' if report['complete'] else 'INCOMPLETE: inspect failed checks; no success is inferred.')
            return 0 if report['complete'] else 2
    except (ValueError,KeyError,httpx.HTTPError) as exc:
        report['error']=str(exc) if isinstance(exc,ValueError) else 'Transport or response validation failed; inspect task before resuming.'
        save(args.report,report)
        print(report['error'],file=sys.stderr)
        print('Progress saved without credentials: '+str(args.report),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
