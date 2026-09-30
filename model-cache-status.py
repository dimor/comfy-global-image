#!/usr/bin/env python3
"""Print a readable snapshot of the on-demand model cache."""
import json
import os
from pathlib import Path
import sys
import time

STATUS = Path(os.environ.get('MODEL_CACHE_STATUS', '/workspace/ComfyUI/model-cache-status.json'))


def size(value):
    if value is None:
        return '-'
    return f'{value / 1024**3:.2f} GiB'


def show():
    try:
        data = json.loads(STATUS.read_text(encoding='utf-8'))
    except FileNotFoundError:
        sys.exit('Cache status is not available yet. ComfyUI may still be starting.')
    except json.JSONDecodeError:
        sys.exit('Cache status is being updated. Run this command again in a moment.')
    print(f"Overall: {data.get('state', 'unknown').upper()} - {data.get('message', '')}")
    print(f"Updated: {data.get('updated_at', '-')}")
    models = data.get('models', {})
    if not models:
        print('Models:  none requested yet')
        return
    print('\nSTATE      PROGRESS  COPIED / TOTAL       SPEED       ETA    MODEL')
    for name, model in models.items():
        state = model.get('state', 'unknown').upper()
        percent = model.get('percent', 0)
        copied = size(model.get('copied_bytes'))
        total = size(model.get('total_bytes'))
        speed = model.get('speed_mib_per_second')
        eta = model.get('eta_seconds')
        speed_text = f'{speed:.1f} MiB/s' if speed is not None else '-'
        eta_text = f'{eta:.0f}s' if eta is not None else '-'
        print(f'{state:<10} {percent:>6.1f}%  {copied:>8} / {total:<8}  {speed_text:>10}  {eta_text:>6}  {name}')
        print(f"  Global: {model.get('source', '-')}")
        print(f"  Local:  {model.get('local_copy', '-')}")


def main():
    watch = '--watch' in sys.argv[1:] or '-w' in sys.argv[1:]
    while True:
        if watch:
            print('\033[2J\033[H', end='')
        show()
        if not watch:
            return
        print('\nRefreshing every 2 seconds. Press Ctrl+C to stop.')
        try:
            time.sleep(2)
        except KeyboardInterrupt:
            return


if __name__ == '__main__':
    main()
