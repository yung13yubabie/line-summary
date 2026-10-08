import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def invoke(*arguments):
    return subprocess.run([sys.executable,str(ROOT/'tools/mock_cli.py'),*arguments],cwd=ROOT,
                          capture_output=True,text=True,timeout=90)


def test_cli_without_choice_outputs_preview_not_data():
    result=invoke('chats')
    assert result.returncode==0,result.stderr
    payload=json.loads(result.stdout)
    assert payload['status']=='requires_confirmation' and payload['items']==[]
    assert payload['human_confirmation_verified'] is False


def test_cli_explicit_synthetic_choice_uses_real_stdio():
    result=invoke('search','--chat-id','demo-project','--query','報價',
                  '--since','2026-10-01T00:00:00+08:00','--until','2026-10-02T00:00:00+08:00',
                  '--privacy','original')
    assert result.returncode==0,result.stderr
    payload=json.loads(result.stdout)
    assert payload['status']=='ok' and payload['synthetic'] is True
    assert payload['items'] and payload['privacy_processing']=='not_implemented'
    assert payload['human_confirmation_verified'] is False


def test_cli_self_test_all_six_mock_tools_only():
    result=invoke('self-test')
    assert result.returncode==0,result.stderr
    payload=json.loads(result.stdout)
    assert len(payload['results'])==6 and all(x['passed'] for x in payload['results'])
    assert payload['actual_host_tests']=='NOT_RUN' and payload['real_LINE_tests']=='NOT_RUN'


def test_cli_invalid_private_option_is_not_echoed():
    marker='SYNTHETIC_PRIVATE_OPTION_SENTINEL'
    result=invoke('chats','--privacy',marker)
    assert result.returncode==2 and marker not in result.stdout+result.stderr
