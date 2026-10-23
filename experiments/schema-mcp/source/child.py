"""Synthetic lifecycle worker. No LINE imports, memory, keys, DB, UI or network."""
import argparse
import json
import os
import sys
import threading
import time

STAGES=('identity','memory_scan','candidate_probe','reader_open','sql')
MARKER='QA_ONLY_SENSITIVE_SENTINEL'


def emit(value):
    sys.stdout.write(json.dumps(value)+'\n')
    sys.stdout.flush()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',required=True)
    parser.add_argument('--resource',required=True)
    args=parser.parse_args()
    line=sys.stdin.readline()
    if not line:
        emit({'event':'cleanup_ack','resource_opened':False,'resources_closed':True})
        return 0
    if json.loads(line)!={'op':'start'}:
        return 3
    stop=threading.Event()
    def control():
        for item in sys.stdin:
            try:
                if json.loads(item).get('op')=='cancel' and args.case!='ignore_cancel':stop.set()
            except (ValueError,TypeError):pass
        if args.case!='ignore_cancel':stop.set()
    controller=threading.Thread(target=control,name='synthetic-control',daemon=False)
    controller.start()
    resource=open(args.resource,'wb')
    resource.write(b'synthetic-only')
    resource.flush()
    status='completed'
    try:
        for stage in STAGES:
            emit({'event':'stage_enter','stage':stage})
            delay=.6 if args.case=='slow_'+stage else .03
            if args.case in ('ignore_cancel','missing_ack') and stage=='memory_scan':delay=3
            until=time.monotonic()+delay
            while time.monotonic()<until:
                if stop.wait(.01):
                    status='cancelled'
                    break
            if status=='cancelled':break
            if args.case=='exception' and stage=='candidate_probe':
                sys.stderr.write(MARKER+' private details discarded\n')
                sys.stderr.flush()
                emit({'event':'error','error_type':'ValueError','detail':MARKER})
                raise ValueError(MARKER)
            emit({'event':'stage_exit','stage':stage})
        emit({'event':'result','status':status})
    except BaseException as error:
        emit({'event':'result','status':'failed','error_type':type(error).__name__})
        status='failed'
    finally:
        resource.close()
        emit({'event':'cleanup_ready'})
        controller.join(timeout=1)
        if controller.is_alive():return 125
        if args.case!='missing_ack':
            emit({'event':'cleanup_ack','resource_opened':True,'resources_closed':True})
    return 3 if status=='failed' else 0


if __name__=='__main__':raise SystemExit(main())
