"""One command launches four separate local processes and shuts them down together."""
import argparse
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rehearsal', action='store_true', help='Explicit emulator + recorded AI; merchant remains real HTTP')
    args = parser.parse_args()
    load_dotenv()
    env = os.environ.copy()
    env['REWIND_MERCHANT'] = 'http'
    env.setdefault('REWIND_SERVICE_TOKEN', secrets.token_urlsafe(32))
    if not env['REWIND_SERVICE_TOKEN']:
        env['REWIND_SERVICE_TOKEN'] = secrets.token_urlsafe(32)
    if args.rehearsal:
        env.update(REWIND_PROVIDER='emulator', REWIND_AI='fixture')
    services = [('rewind.demo_services:create_inbox', 8012),
                ('rewind.demo_services:create_merchant', 8011)]
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    children = []
    log_dir = Path('logs'); log_dir.mkdir(exist_ok=True)
    logs = []
    try:
        for module, port in services:
            log = (log_dir / (str(port) + '.log')).open('a'); logs.append(log)
            children.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', module, '--factory',
                '--host', '127.0.0.1', '--port', str(port)], env=env, stdout=log, stderr=log))
        log = (log_dir / 'proxy.log').open('a'); logs.append(log)
        children.append(subprocess.Popen([sys.executable, '-m', 'rewind.fault_proxy'], env=env, stdout=log, stderr=log))
        with httpx.Client(timeout=1, trust_env=False) as client:
            for port in [8011, 8012]:
                for _ in range(80):
                    try:
                        if client.get(f'http://127.0.0.1:{port}/health').status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if any(p.poll() is not None for p in children):
                        raise RuntimeError('A service exited; inspect logs/')
                    time.sleep(.1)
                else:
                    raise RuntimeError('Service startup timed out')
        children.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'rewind.app:create_app', '--factory',
            '--host', '127.0.0.1', '--port', '8000'], env=env))
        print('REWIND: http://127.0.0.1:8000 — Ctrl+C stops all four processes.', flush=True)
        print('Mode: explicit fixture rehearsal' if args.rehearsal else 'Mode: configured provider and live AI', flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(.25)
        raise RuntimeError('A service exited; inspect logs/')
    except KeyboardInterrupt:
        pass
    finally:
        for p in children:
            if p.poll() is None:
                p.terminate()
        for p in children:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        for log in logs:
            log.close()


if __name__ == '__main__':
    main()
