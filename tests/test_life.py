"""The closed R24 quiet-hour contract, using only temporary homes and a fake."""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from mnemos.simple_runtime import MnemosRuntime
from mnemos.store.sqlite_store import EngramStore, ReadOnlyEngramStore

SCOPE = {'agent_id': 'claude-code', 'person_id': 'user', 'project_scope': 'global'}
ROOT = Path(__file__).resolve().parents[1]
RECORD_KEYS = ['id', 'kind', 'started_at', 'ended_at', 'model', 'status', 'reason',
               'tokens', 'journal_ids', 'note_ids', 'store', 'session_id', 'since',
               'waiting', 'discarded_ids']


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    folder = tmp_path / 'home'
    (folder / '.mnemos').mkdir(parents=True)
    monkeypatch.setenv('HOME', str(folder))
    monkeypatch.setenv('MNEMOS_LIFE_MNEMOS_BIN', '/opt/fake/mnemos')
    monkeypatch.setenv('MNEMOS_CLAUDE_BIN', str(ROOT / 'tests/fake_claude.py'))
    monkeypatch.setenv('FAKE_CLAUDE_LOG', str(tmp_path / 'fake.json'))
    monkeypatch.setenv('FAKE_CLAUDE_SCENARIO', 'ok')
    # The executable fake uses env python3 and must use this suite's interpreter.
    monkeypatch.setenv('PATH', str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
    return folder


@pytest.fixture
def db(home):
    path = home / '.mnemos/claude-code.db'
    EngramStore(path).close()
    return path


def work(db, **scope):
    runtime = MnemosRuntime(db_path=str(db), use_dedicated_model=False, **(scope or SCOPE))
    try:
        runtime.capture('The ferry timetable changed.', signed_as='claude-opus-5-5',
                        impact='Read the new timetable.')
        runtime.handoff('Check the ferry timetable next session.', signed_as='claude-opus-5-5')
    finally:
        runtime.close()


def rows(db, sql, params=()):
    conn = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def run(db, kind='mine', **kwargs):
    from mnemos.life import run_hour
    return run_hour(kind, db_path=str(db), **SCOPE, **kwargs)


def log(tmp_path):
    return json.loads((tmp_path / 'fake.json').read_text())


def hour_folder(home, record):
    return home / '.mnemos/hours/claude-code' / record['id']


def test_dry_run_prints_words_and_whats_waiting(db, home):
    from mnemos import life
    work(db)
    words = life.studio_dir('claude-code') / 'words'
    words.mkdir(parents=True)
    (words / 'my-hour.md').write_text('My own words.\n\n')
    out = io.StringIO()
    record = run(db, dry_run=True, out=out)
    printed = out.getvalue()
    for text in ('My own words.', "What's waiting", 'Handoffs since',
                 '── command ──', '--model claude-opus-5-5'):
        assert text in printed
    assert 'Captures since' not in printed
    handoff = rows(db, "SELECT id FROM hypomnema_entries WHERE entry_kind='handoff'")[0]
    assert f"[{handoff['id']}]" in record['prompt']
    assert record['prompt'].startswith('My own words.\n\n---\n\n')
    assert '--fallback-model' not in printed
    assert not life.hours_dir('claude-code').exists()


def test_bootstrap_when_no_words_file(db):
    from mnemos.life import BOOTSTRAP
    assert run(db, dry_run=True, out=io.StringIO())['prompt'].startswith(BOOTSTRAP['mine'])


def test_waiting_says_nothing_when_nothing_waits(db):
    for kind in ('mine', 'ours'):
        assert run(db, kind, dry_run=True, out=io.StringIO())['prompt'].endswith("What's waiting\n\nNothing.")


def test_our_hour_skips_without_work(db, tmp_path):
    record = run(db, 'ours')
    assert record['status'] == 'skipped'
    assert record['reason'].startswith('No handoffs or captures since')
    assert record['waiting'] == {'handoffs': 0, 'captures': 0, 'questions': 0}
    assert not (tmp_path / 'fake.json').exists()


def test_since_is_the_last_done_hour_of_the_same_kind(home):
    from mnemos.life import hours_dir, since_for
    records = [
        {'kind': 'ours', 'status': 'done', 'store': 'live', 'ended_at': '2026-10-01T01:00:00+00:00'},
        {'kind': 'ours', 'status': 'skipped', 'store': 'live', 'ended_at': '2026-10-01T02:00:00+00:00'},
        {'kind': 'mine', 'status': 'done', 'store': 'live', 'ended_at': '2026-10-01T03:00:00+00:00'},
        {'kind': 'ours', 'status': 'done', 'store': 'copy', 'ended_at': '2026-10-01T04:00:00+00:00'},
    ]
    folder = hours_dir('claude-code')
    folder.mkdir(parents=True)
    (folder / 'hours.jsonl').write_text('\n'.join(json.dumps(r) for r in records) + '\ninvalid\n')
    assert since_for('ours', records) == records[0]['ended_at']


def test_ok_hour_records_everything(db, home, tmp_path):
    record = run(db)
    assert list(record) == RECORD_KEYS
    assert record['status'] == 'done'
    assert record['model'] == 'claude-opus-5-5'
    assert record['journal_ids'] == [rows(db, 'SELECT id FROM journal_entries')[0]['id']]
    assert record['tokens'] == {'input': 10, 'output': 5, 'cache_creation': 100, 'cache_read': 50}
    assert record['session_id'] == 'fake-session'
    assert record['store'] == 'live'
    assert record['waiting'] == {'handoffs': 0, 'captures': 0, 'questions': 0}
    assert log(tmp_path)['stdin'] == (hour_folder(home, record) / 'prompt.md').read_text()
    assert log(tmp_path)['env_hour'] == record['id']
    assert json.loads((hour_folder(home, record).parent / 'hours.jsonl').read_text()) == record


@pytest.mark.parametrize('kind', ['mine', 'ours'])
def test_command_line_and_fences(db, home, tmp_path, kind):
    from mnemos.life import ALLOW, BASH_DENY, SECRET_PATHS, TOOLS
    (home / 'Documents/Repositories').mkdir(parents=True)
    if kind == 'ours':
        work(db)
    record = run(db, kind)
    argv = log(tmp_path)['argv']
    for flag in ('--restricted', '--strict-mcp-config', '--no-session-persistence'):
        assert flag in argv
    assert argv[argv.index('--permission-prompts') + 1] == 'none'
    assert argv[argv.index('--tools') + 1] == TOOLS[kind]
    for flag in ('--fallback-model', '--bare', '--dangerously-skip-permissions',
                 '--allow-dangerously-skip-permissions'):
        assert flag not in argv
    assert log(tmp_path)['stdin'] not in argv
    folder = hour_folder(home, record)
    settings = json.loads((folder / 'settings.json').read_text())
    assert settings['permissions']['allow'] == list(ALLOW[kind])
    assert list(settings['permissions']) == ['defaultMode', 'allow', 'deny']
    assert settings['sandbox']['enabled'] is True
    assert settings['sandbox']['allowUnsandboxedCommands'] is False
    deny = settings['permissions']['deny']
    read_dir = home / 'Documents/Repositories'
    assert deny.count(f'Edit(/{read_dir}/**)') == 1
    assert not any(item.startswith('Write(') for item in deny)
    assert 'Read(//**/.env)' in deny
    assert 'Read(//**/.env.*)' in deny
    assert 'Read(**/.env)' not in deny
    assert 'Read(**/.env.*)' not in deny
    for secret in SECRET_PATHS:
        path = home / secret
        file_rule = f'Read(/{path})'
        directory_rule = f'Read(/{path}/**)'
        assert file_rule in deny
        assert directory_rule in deny
        assert deny.index(directory_rule) == deny.index(file_rule) + 1
    assert argv[argv.index('--add-dir') + 1] == str(read_dir)
    assert all((item in deny) == (kind == 'mine') for item in BASH_DENY)
    mcp = json.loads((folder / 'mcp.json').read_text())['mcpServers']['mnemos']
    assert mcp['command'] == '/opt/fake/mnemos'
    assert mcp['env']['MNEMOS_HOUR_ID'] == record['id']


def guard(db, home, monkeypatch, scenario):
    monkeypatch.setenv('FAKE_CLAUDE_SCENARIO', scenario)
    start = time.monotonic()
    record = run(db)
    assert time.monotonic() - start < 20
    assert record['status'] == 'skipped'
    assert 'claude-sonnet-5-5' in record['reason']
    assert len(record['discarded_ids']) == 1
    assert record['journal_ids'] == record['note_ids'] == []
    assert not rows(db, 'SELECT id FROM journal_entries WHERE hour_id=?', (record['id'],))


def test_guard_at_init_keeps_nothing(db, home, monkeypatch):
    guard(db, home, monkeypatch, 'wrong_model_init')


def test_guard_mid_hour_keeps_nothing(db, home, monkeypatch):
    guard(db, home, monkeypatch, 'wrong_model_mid')


def test_token_ceiling_stops_the_hour(db, monkeypatch):
    monkeypatch.setenv('FAKE_CLAUDE_SCENARIO', 'tokens')
    start = time.monotonic()
    record = run(db, max_tokens=500)
    assert time.monotonic() - start < 20
    assert record['status'] == 'done'
    assert record['reason'] == 'Stopped at the token ceiling (500).'


def test_repeated_message_counts_once(db, monkeypatch):
    monkeypatch.setenv('FAKE_CLAUDE_SCENARIO', 'dupes')
    record = run(db, max_tokens=300)
    assert record['status'] == 'done'
    assert record['reason'] == ''
    assert record['tokens'] == {'input': 0, 'output': 0, 'cache_creation': 0, 'cache_read': 0}


def test_time_limit_stops_the_hour(db, monkeypatch):
    monkeypatch.setenv('FAKE_CLAUDE_SCENARIO', 'sleep')
    start = time.monotonic()
    record = run(db, max_seconds=2)
    assert time.monotonic() - start < 20
    assert record['status'] == 'done'
    assert record['reason'].startswith('Stopped at the time limit')


def test_crash_is_failed_plainly(db, monkeypatch):
    monkeypatch.setenv('FAKE_CLAUDE_SCENARIO', 'crash')
    record = run(db)
    assert record['status'] == 'failed'
    assert record['reason'].startswith('claude exited with code 3:')
    assert 'boom' in record['reason']


def test_copy_run_leaves_the_live_store_identical(db, home):
    def fingerprint():
        digest = hashlib.sha256(db.read_bytes())
        wal = Path(str(db) + '-wal')
        if wal.exists():
            digest.update(wal.read_bytes())
        return digest.hexdigest()

    # Keep a live connection so committed writes remain in a nonempty WAL.
    writer = EngramStore(db)
    try:
        writer.set_meta('life_test', 'fingerprint includes the WAL')
        assert Path(str(db) + '-wal').stat().st_size > 0
        before = fingerprint()
        record = run(db, store_copy=True)
        assert fingerprint() == before
        assert record['store'] == 'copy'
        assert record['live_store_untouched'] is True
        assert all(key not in record for key in (
            'live_store_sha256_before', 'live_store_sha256_after', 'live_store_unchanged',
        ))
        assert not rows(db, 'SELECT id FROM journal_entries')
        copy = hour_folder(home, record) / 'store.db'
        assert [r['id'] for r in rows(copy, 'SELECT id FROM journal_entries')] == record['journal_ids']
    finally:
        writer.close()


def test_copy_run_notices_a_write_to_the_live_store(db, monkeypatch):
    monkeypatch.setenv('FAKE_CLAUDE_SCENARIO', 'writes_live')
    monkeypatch.setenv('FAKE_LIVE_DB', str(db))
    record = run(db, store_copy=True)
    assert record['status'] == 'done'
    assert record['live_store_untouched'] is False
    assert len(rows(db, 'SELECT id FROM journal_entries WHERE hour_id=?', (record['id'],))) == 1


def test_other_writers_do_not_count_against_the_hour(db, monkeypatch):
    from mnemos import life

    def fingerprint():
        digest = hashlib.sha256(db.read_bytes())
        wal = Path(str(db) + '-wal')
        if wal.exists():
            digest.update(wal.read_bytes())
        return digest.hexdigest()

    def other_entry():
        writer = EngramStore(db)
        try:
            writer.write_journal_entry("an hour's entry", **SCOPE, hour_id=None)
        finally:
            writer.close()

    other_entry()
    before = fingerprint()
    original_watch = life._watch

    def watch(*args, **kwargs):
        outcome = original_watch(*args, **kwargs)
        other_entry()
        return outcome

    monkeypatch.setattr(life, '_watch', watch)
    record = run(db, store_copy=True)
    assert record['live_store_untouched'] is True
    assert fingerprint() != before


def test_second_hour_waits_for_the_first(db):
    from mnemos.life import hours_dir
    folder = hours_dir('claude-code')
    folder.mkdir(parents=True)
    with (folder / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = run(db)
        assert record['status'] == 'skipped'
        assert record['reason'] == 'Another hour is running.'


def test_config_dir_sets_the_login(db, tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', 'inherited')
    run(db, config_dir='~/.claude-2')
    assert log(tmp_path)['env_config'].endswith('/.claude-2')
    run(db)
    assert log(tmp_path)['env_config'] == 'inherited'


def test_cli_life_dry_run(db):
    done = subprocess.run(
        [sys.executable, '-m', 'mnemos.cli', 'life', '--hour', 'mine', '--dry-run',
         '--db-path', str(db), '--agent-id', 'claude-code'],
        env=dict(os.environ), capture_output=True, text=True, timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert 'Dry run · mine hour' in done.stdout


def test_store_methods_read_and_discard(db):
    work(db)
    work(db, **{**SCOPE, 'agent_id': 'other'})
    store = EngramStore(db)
    try:
        since = '2000-01-01T00:00:00+00:00'
        assert [r['content'] for r in store.handoffs_since(since, **SCOPE)] == [
            'Check the ferry timetable next session.']
        assert [r['content'] for r in store.agent_captures_since(since, **SCOPE)] == [
            'The ferry timetable changed.']
        assert store.handoffs_since('9999', **SCOPE) == []
        assert store.agent_captures_since('9999', **SCOPE) == []
        other = {**SCOPE, 'agent_id': 'other'}
        journal = store.write_journal_entry('mine', hour_id='h', **SCOPE)
        foreign_journal = store.write_journal_entry('theirs', hour_id='h', **other)
        note = store.write_note('mine', author='agent', kind='made', hour_id='h', **SCOPE)
        reply = store.write_note('reply', author='person', in_reply_to=note, hour_id='h', **SCOPE)
        foreign_note = store.write_note('theirs', author='agent', kind='made', hour_id='h', **other)
        assert store.hour_rows('', **SCOPE) == {'journal_ids': [], 'note_ids': []}
        assert store.discard_hour_rows('', **SCOPE) == []
        assert store.hour_rows('h', **SCOPE) == {'journal_ids': [journal], 'note_ids': [note]}
        assert store.discard_hour_rows('h', **SCOPE) == [journal, note, reply]
        assert store.hour_rows('h', **SCOPE) == {'journal_ids': [], 'note_ids': []}
        assert store.hour_rows('h', **other) == {'journal_ids': [foreign_journal], 'note_ids': [foreign_note]}
    finally:
        store.close()
    conn = sqlite3.connect(db)
    conn.execute('DROP TABLE journal_entries')
    conn.execute('DROP TABLE notes')
    conn.commit()
    conn.close()
    store = ReadOnlyEngramStore(db)
    try:
        assert store.hour_rows('h', **SCOPE) == {'journal_ids': [], 'note_ids': []}
    finally:
        store.close()


def test_questions_are_counted_in_full_and_shown_with_overflow(db, home):
    from mnemos.life import run_hour
    runtime = MnemosRuntime(db_path=str(db), use_dedicated_model=False, **SCOPE)
    try:
        for index in range(7):
            runtime.capture(f'Timetable number {index} has a new departure.',
                            impact=f'Check departure {index}.', signed_as='claude-opus-5-5')
    finally:
        runtime.close()
    store = EngramStore(db)
    try:
        captures = store.agent_captures_since('2000', **SCOPE)
        for index, capture in enumerate(reversed(captures)):
            store.enqueue_reflection('impact', capture['id'], f'Question {index}?', **SCOPE)
    finally:
        store.close()
    record = run_hour('mine', db_path=str(db), **SCOPE)
    prompt = (hour_folder(home, record) / 'prompt.md').read_text()
    questions = prompt.split('Open questions\n', 1)[1].split('\n\n', 1)[0]
    assert len(questions.splitlines()) == 6
    assert questions.endswith('- and 2 more')
    assert 'Question 4?' in questions
    assert 'Question 5?' not in questions
    assert 'Question 6?' not in questions
    for line, capture in zip(questions.splitlines()[:5], reversed(captures)):
        assert line.endswith(f"[{capture['id']}]")
    assert record['waiting'] == {'handoffs': 0, 'captures': 0, 'questions': 7}
    assert all(row['surfaced_count'] == 0 for row in rows(db, 'SELECT surfaced_count FROM reflection_queue'))


def test_copy_run_with_held_lock_records_null_untouched(db):
    from mnemos.life import hours_dir
    folder = hours_dir('claude-code')
    folder.mkdir(parents=True)
    with (folder / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = run(db, store_copy=True)
    keys = ['live_store_untouched']
    assert list(record) == RECORD_KEYS + keys
    assert all(record[key] is None for key in keys)
    assert record['status'] == 'skipped'
    assert record['reason'] == 'Another hour is running.'
    assert json.loads((folder / 'hours.jsonl').read_text()) == record


def test_synthetic_message_is_not_another_model(db, monkeypatch):
    monkeypatch.setenv('FAKE_CLAUDE_SCENARIO', 'synthetic')
    record = run(db)
    assert record['status'] == 'done'
    assert record['reason'] == "Claude Code said: You've hit your session limit · resets 12:20am"
    assert record['journal_ids'] == [rows(db, 'SELECT id FROM journal_entries')[0]['id']]
    assert record['discarded_ids'] == []


def test_garbage_messages_do_not_raise(db, monkeypatch):
    monkeypatch.setenv('FAKE_CLAUDE_SCENARIO', 'garbage')
    record = run(db)
    assert record['status'] == 'done'
    assert record['reason'] == ''


def test_runner_error_is_recorded(db, home, monkeypatch):
    from mnemos import life

    def fail(*args):
        raise RuntimeError('disk on fire')

    monkeypatch.setattr(life, '_gather', fail)
    record = run(db)
    assert record['status'] == 'failed'
    assert record['reason'] == 'The runner hit an error: RuntimeError: disk on fire'
    assert json.loads((hour_folder(home, record).parent / 'hours.jsonl').read_text()) == record


def test_watch_error_stops_the_process(db, monkeypatch):
    from mnemos import life
    monkeypatch.setenv('FAKE_CLAUDE_SCENARIO', 'sleep')
    processes = []
    popen = subprocess.Popen

    def start(*args, **kwargs):
        proc = popen(*args, **kwargs)
        processes.append(proc)
        return proc

    def fail_parse(*args, **kwargs):
        raise RuntimeError('stream reader failed')

    monkeypatch.setattr(life.subprocess, 'Popen', start)
    monkeypatch.setattr(life.json, 'loads', fail_parse)
    try:
        try:
            run(db)
        except RuntimeError:
            pass  # Round 1 raises; this test isolates whether it leaves a child.
        assert len(processes) == 1
        assert processes[0].poll() is not None
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)


def test_cli_failed_dry_run_says_why(home):
    done = subprocess.run(
        [sys.executable, '-m', 'mnemos.cli', 'life', '--hour', 'mine', '--dry-run',
         '--db-path', str(home / '.mnemos/missing.db'), '--agent-id', 'claude-code'],
        env=dict(os.environ), capture_output=True, text=True, timeout=30,
    )
    assert done.returncode == 1
    assert 'There is no memory store at' in done.stderr


def hour_material(db):
    work(db)
    store = EngramStore(db)
    try:
        capture = store.agent_captures_since('2000', **SCOPE)[0]
        handoff = store.handoffs_since('2000', **SCOPE)[0]
        store.enqueue_reflection('belief', capture['id'], "You keep returning to 'running'", **SCOPE)
        store.enqueue_reflection('lesson', capture['id'], 'What did the timetable teach you?', **SCOPE)
        journal_id = store.write_journal_entry('A journal entry before the hour.', **SCOPE)
        return capture['id'], handoff['id'], journal_id
    finally:
        store.close()


def test_my_hour_leaves_out_captures_and_word_count_questions(db, home, monkeypatch):
    from mnemos import life
    capture_id, handoff_id, journal_id = hour_material(db)

    def forbidden(*args, **kwargs):
        raise AssertionError('mine must not gather captures')

    monkeypatch.setattr(life.ReadOnlyEngramStore, 'agent_captures_since', forbidden)
    dry = run(db, dry_run=True, out=io.StringIO())
    record = run(db)
    actual = (hour_folder(home, record) / 'prompt.md').read_text()
    for prompt in (dry['prompt'], actual):
        assert 'Handoffs since' in prompt
        assert f'Check the ferry timetable next session. [{handoff_id}]' in prompt
        assert f'What did the timetable teach you? · The ferry timetable changed. [{capture_id}]' in prompt
        assert f'A journal entry before the hour. [{journal_id}]' in prompt
        assert 'Captures since' not in prompt
        assert "You keep returning to 'running'" not in prompt
    assert record['waiting'] == {'handoffs': 1, 'captures': 0, 'questions': 1}


def test_our_hour_is_only_the_days_work(db, home, monkeypatch):
    from mnemos import life
    capture_id, handoff_id, journal_id = hour_material(db)

    def forbidden(*args, **kwargs):
        raise AssertionError('ours must not gather questions or journal entries')

    monkeypatch.setattr(life.ReadOnlyEngramStore, 'pending_reflections', forbidden)
    monkeypatch.setattr(life.ReadOnlyEngramStore, 'journal_entries', forbidden)
    dry = run(db, 'ours', dry_run=True, out=io.StringIO())
    record = run(db, 'ours')
    actual = (hour_folder(home, record) / 'prompt.md').read_text()
    for prompt in (dry['prompt'], actual):
        assert 'Handoffs since' in prompt and 'Captures since' in prompt
        assert f'Check the ferry timetable next session. [{handoff_id}]' in prompt
        assert f'The ferry timetable changed. [{capture_id}]' in prompt
        assert 'Open questions' not in prompt
        assert 'Latest journal entry' not in prompt
        assert f'[{journal_id}]' not in prompt
    assert record['waiting'] == {'handoffs': 1, 'captures': 1, 'questions': 0}


def test_claude_that_exits_at_once_is_reported_with_its_stderr(db, home, tmp_path, monkeypatch):
    from mnemos.life import studio_dir
    words = studio_dir('claude-code') / 'words'
    words.mkdir(parents=True)
    (words / 'my-hour.md').write_text('x' * 1_000_000)
    monkeypatch.setenv('FAKE_CLAUDE_SCENARIO', 'early_exit')
    record = run(db)
    assert record['status'] == 'failed'
    assert record['reason'].startswith('claude exited with code 2:')
    assert 'unknown option' in record['reason']
    assert list(log(tmp_path)) == ['argv']
