#!/usr/bin/env python3
"""The quiet-hour runner's fake process; never invokes Claude or MCP."""
import json
import os
from pathlib import Path
import sys
import time

# The runner launches from the studio, so find the repository explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mnemos.store.sqlite_store import EngramStore

argv = sys.argv[1:]
scenario = os.environ.get('FAKE_CLAUDE_SCENARIO', 'ok')
if scenario == 'early_exit':
    os.close(0)
    Path(os.environ['FAKE_CLAUDE_LOG']).write_text(json.dumps({'argv': argv}))
    print("error: unknown option '--restricted'", file=sys.stderr, flush=True)
    sys.exit(2)
Path(os.environ['FAKE_CLAUDE_LOG']).write_text(json.dumps({
    'argv': argv, 'stdin': sys.stdin.read(),
    'env_hour': os.environ.get('MNEMOS_HOUR_ID'),
    'env_config': os.environ.get('CLAUDE_CONFIG_DIR'),
}))
mcp = json.loads(Path(argv[argv.index('--mcp-config') + 1]).read_text())
args = mcp['mcpServers']['mnemos']['args']
path = args[args.index('--db-path') + 1]
model = 'claude-opus-5-5'
usage = {'input_tokens': 10, 'cache_creation_input_tokens': 100,
         'output_tokens': 5, 'cache_read_input_tokens': 50}


def emit(event):
    print(json.dumps(event), flush=True)


def entry():
    store = EngramStore(path)
    try:
        store.write_journal_entry(
            "an hour's entry", agent_id='claude-code', person_id='user',
            project_scope='global', written='between_sessions', model_id=model,
            hour_id=os.environ['MNEMOS_HOUR_ID'],
        )
    finally:
        store.close()


def assistant(seen=model, used=usage):
    emit({'type': 'assistant', 'message': {'id': 'm1', 'model': seen, 'usage': used}})


if scenario == 'crash':
    print('boom', file=sys.stderr, flush=True)
    sys.exit(3)
if scenario == 'wrong_model_init':
    entry()
    emit({'type': 'system', 'subtype': 'init', 'model': 'claude-sonnet-5-5'})
    time.sleep(30)
else:
    emit({'type': 'system', 'subtype': 'init', 'model': model})
    if scenario == 'ok':
        entry()
        assistant()
        assistant()
        emit({'type': 'result', 'is_error': False, 'session_id': 'fake-session', 'usage': usage})
    elif scenario == 'writes_live':
        path = os.environ['FAKE_LIVE_DB']
        entry()
        emit({'type': 'result', 'is_error': False, 'session_id': 'fake-session', 'usage': usage})
        sys.exit(0)
    elif scenario == 'wrong_model_mid':
        entry()
        assistant('claude-sonnet-5-5')
        time.sleep(30)
    elif scenario == 'tokens':
        assistant(used={'input_tokens': 0, 'cache_creation_input_tokens': 0, 'output_tokens': 1000})
        time.sleep(30)
    elif scenario == 'dupes':
        for _ in range(5):
            assistant(used={'output_tokens': 100})
        emit({'type': 'result', 'is_error': False})
    elif scenario == 'sleep':
        time.sleep(30)
    elif scenario == 'synthetic':
        entry()
        emit({'type': 'assistant', 'message': {
            'id': 's1', 'model': '<synthetic>',
            'usage': {key: 0 for key in usage},
            'content': [{'type': 'text', 'text': "You've hit your session limit · resets 12:20am"}],
        }})
        emit({'type': 'result', 'is_error': False})
    elif scenario == 'garbage':
        emit({'type': 'assistant', 'message': {}})
        emit({'type': 'assistant'})
        emit({'type': 'result', 'is_error': False})
