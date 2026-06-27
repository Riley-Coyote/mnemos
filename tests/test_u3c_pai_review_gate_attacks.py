"""Mother-of-all-tests: independent adversarial eval of the U3c diff/intent review gate.

Stance: assume the gate is lying. The goal is to make
    dangerous diff + missing/false proof = GREEN
and encode every successful bypass as a regression. Attacks the gate ALREADY
catches stay as proof; second-generation escapes found against Codex's Step-3.5
hardening are locked in as passing regressions.

Cross-references the bypass IDs in
~/.claude/MEMORY/handoff-2026-06-26-codex-u3c-review-gate-pass3.md and the
probe at scratchpad/probe_codex35.py.

Layout is table-driven. Helpers and GOOD_* fixtures are reused from the main
review-gate test module (pytest prepend import mode puts tests/ on sys.path).
"""

from __future__ import annotations

import subprocess

import pytest

import mnemos.importer.review_gate as rg
from tests.test_u3c_pai_review_gate import _file_texts, _findings

PAI = ["mnemos/importer/pai.py", "tests/test_u3b_pai_importer.py"]
WATCH = ["mnemos/importer/watcher.py", "tests/test_u3c_pai_watcher.py"]
_WF_TAIL = (
    "\n- name: wheel\n  run: |\n    import zipfile\n    archive.namelist()\n    mnemos/importer/review_gate.py\n"
    "- name: twine\n  run: uvx twine check dist/*\n"
    "mnemos/importer/pai.py mnemos/importer/operator.py mnemos/importer/watcher.py mnemos/importer/review_gate.py\n"
)


def _d(path, *lines):
    return f"diff --git a/{path} b/{path}\n@@\n" + "".join("+" + ln + "\n" for ln in lines)


def _crit(findings):
    return any(f.severity == "critical" for f in findings)


def _persist(findings):
    return any("direct state/plist write" in f.description for f in findings)


def _wf(build_step):
    return _findings(
        changed_files=[".github/workflows/release-hardening.yml"],
        file_texts=_file_texts(**{".github/workflows/release-hardening.yml": build_step + _WF_TAIL}),
    )


# =============================================================================
# Class 2 — dangerous lifecycle mutation hiding (DELETE/UPDATE/REPLACE)
# =============================================================================
# (id, added SQL lines, should_flag)
_LIFECYCLE_CASES = [
    ("delete-broad",            ('conn.execute("DELETE FROM engrams")',), True),
    ("update-archive-nowhere",  ('conn.execute("UPDATE engrams SET state = \'archived\'")',), True),
    ("update-reactivate",       ('conn.execute("UPDATE engrams SET state = \'active\'")',), True),
    ("update-table-alias",      ('conn.execute("UPDATE engrams AS e SET state = \'archived\'")',), True),  # B
    ("update-alias-no-as",      ('conn.execute("UPDATE engrams e SET state = \'archived\'")',), True),  # B
    ("printf-literal-table",    ('conn.execute("DELETE FROM %s" % "engrams")',), True),  # C
    ("printf-var-table",        ('conn.execute("DELETE FROM %s" % tbl)',), True),  # C
    ("comment-scope-decoy",     ('conn.execute("UPDATE engrams SET state=\'archived\'")  # WHERE id = upstream',), True),  # A1
    ("replace-compose",         ('conn.execute("DELETE FROM PH".replace("PH", "engrams"))',), True),
    ("join-compose",            ('conn.execute(" ".join(["DELETE", "FROM", "engrams"]))',), True),
    ("concat-var-table",        ('conn.execute("DELETE FROM " + tbl)',), True),
    ("fstring-dynamic",         ('tbl = "engrams"', 'conn.execute(f"DELETE FROM {tbl}")'), True),
    ("archive-table-wipe",      ('conn.execute("DELETE FROM archive")',), True),
    ("replace-into",            ('conn.execute("REPLACE INTO engrams (id, state) VALUES (1, \'x\')")',), True),
    ("multiline-update",        ('conn.execute(', '    "UPDATE engrams"', '    " SET state = \'archived\'")'), True),
    ("state-scoped-not-id",     ('conn.execute("UPDATE engrams SET state=\'archived\' WHERE state=\'active\'")',), True),
    # safe / must NOT flag
    ("scoped-delete-id",        ('conn.execute("DELETE FROM engrams WHERE id = ?", (i,))',), False),
    ("scoped-update-target",    ('conn.execute("UPDATE engrams SET state=\'archived\' WHERE target_id = ?", (t,))',), False),
    ("scoped-delete-jobid",     ('conn.execute("DELETE FROM archive WHERE job_id = ?", (j,))',), False),
    ("select-only",             ('rows = conn.execute("SELECT id FROM engrams")',), False),
]


@pytest.mark.parametrize("cid,lines,should_flag", _LIFECYCLE_CASES, ids=[c[0] for c in _LIFECYCLE_CASES])
def test_lifecycle_mutation(cid, lines, should_flag):
    findings = _findings(changed_files=PAI, diff_text=_d("mnemos/importer/pai.py", *lines))
    assert _crit(findings) is should_flag


def test_lifecycle_mutation_in_new_importer_file():
    # New file outside the original globs + UPDATE -> must flag (allowlist + UPDATE detection)
    findings = _findings(
        changed_files=["mnemos/handlers/sweep.py"],
        diff_text=_d("mnemos/handlers/sweep.py", 'conn.execute("UPDATE engrams SET state=\'archived\'")'),
    )
    assert _crit(findings)


# Second-statement decoy: a separate scoped SELECT must not vouch identity scope
# for an unscoped mutation on the same physical line.
def test_lifecycle_second_statement_scope_decoy():
    findings = _findings(
        changed_files=PAI,
        diff_text=_d(
            "mnemos/importer/pai.py",
            'conn.execute("UPDATE engrams SET state=\'archived\'"); conn.execute("SELECT 1 FROM x WHERE id = ?", (i,))',
        ),
    )
    assert _crit(findings)


# =============================================================================
# Class 1 — proof spoofing (AST authenticity)
# =============================================================================
# (id, test source, expected_is_proof)
_PROOF_CASES = [
    ("assert-true-residual",   "def t():\n    assert True\n", True),   # RESIDUAL: static gate can't judge assertion truth
    ("pass-only",              "def t():\n    pass\n", False),
    ("docstring-only",         'def t():\n    """d"""\n', False),
    ("bare-raises",            "def t():\n    pytest.raises(Exception)\n", False),
    ("inbody-skip",            "def t():\n    pytest.skip('x')\n", False),
    ("decorator-skip",         "import pytest\n@pytest.mark.skip\ndef t():\n    assert True\n", False),
    ("decorator-xfail",        "import pytest\n@pytest.mark.xfail\ndef t():\n    assert True\n", False),
    ("module-pytestmark-skip", "import pytest\npytestmark = pytest.mark.skip\ndef t():\n    assert True\n", False),
    ("aliased-import-skip",    "import pytest as pt\n@pt.mark.skip\ndef t():\n    assert True\n", False),  # F
    ("from-import-mark-skip",  "from pytest import mark\n@mark.skip\ndef t():\n    assert True\n", False),  # F
    ("skipif-true",            "import pytest\n@pytest.mark.skipif(True, reason='x')\ndef t():\n    assert True\n", False),
    ("empty-parametrize-list", "import pytest\n@pytest.mark.parametrize('x', [])\ndef t():\n    assert True\n", False),
    ("empty-parametrize-tuple","import pytest\n@pytest.mark.parametrize('x', ())\ndef t():\n    assert True\n", False),
    ("real-assert",            "def t():\n    assert compute() == 3\n", True),
]


@pytest.mark.parametrize("cid,src,is_proof", _PROOF_CASES, ids=[c[0] for c in _PROOF_CASES])
def test_proof_authenticity(cid, src, is_proof):
    assert rg._test_function_has_proof(src, "t") is is_proof


# Empty parametrize hidden behind a kwarg or a simple indirection variable must
# not count as executable proof.
@pytest.mark.parametrize(
    "src",
    [
        "import pytest\n@pytest.mark.parametrize('x', argvalues=[])\ndef t():\n    assert True\n",
        "import pytest\nEMPTY=[]\n@pytest.mark.parametrize('x', EMPTY)\ndef t():\n    assert True\n",
    ],
    ids=["kwarg-argvalues", "indirection-var"],
)
def test_proof_empty_parametrize_indirection(src):
    assert rg._test_function_has_proof(src, "t") is False


def test_proof_matching_filename_no_assertions_flags():
    # Required doctor tests present by name but gutted to non-substantive bodies -> finding.
    gutted = "\n".join(
        f"def {name}():\n    pass"
        for name in (
            "test_u3c_watch_doctor_passes_with_representative_db_and_plist",
            "test_u3c_watch_doctor_fails_stale_clone_plist",
        )
    ) + "\n"
    findings = _findings(
        changed_files=WATCH + ["tests/test_u3c_pai_watch_doctor.py"],
        file_texts=_file_texts(**{"tests/test_u3c_pai_watch_doctor.py": gutted}),
    )
    assert any(f.action == "must-test" for f in findings)


def test_proof_keyword_only_in_comment_does_not_count():
    # The proof marker appears only inside a comment -> must still be missing.
    commented = "# launchd plist static readiness\n# tombstoned\n"
    findings = _findings(
        changed_files=WATCH + ["tests/test_u3c_pai_watch_doctor.py"],
        file_texts=_file_texts(**{"mnemos/importer/watcher.py": commented}),
    )
    assert any(f.status == "missing" for f in findings)


# =============================================================================
# Class 3 — review-gate self-change
# =============================================================================
def test_review_gate_change_requires_its_tests_in_diff():
    findings = _findings(changed_files=["mnemos/importer/review_gate.py"])
    assert any("diff-review gate changed" in f.description for f in findings)


def test_review_gate_change_requires_rule_signature_proof():
    findings = _findings(
        changed_files=["mnemos/importer/review_gate.py", "tests/test_u3c_pai_review_gate.py"],
        file_texts=_file_texts(**{"tests/test_u3c_pai_review_gate.py": "def test_unrelated():\n    assert True\n"}),
    )
    assert any(f.ident == "RG-review-gate-rule-signature" for f in findings)


# =============================================================================
# Class 4 — watcher / operator / pai proof mapping
# =============================================================================
_MAPPING_CASES = [
    ("watcher-no-test", ["mnemos/importer/watcher.py"], "watcher behavior changed"),
    ("operator-no-test", ["mnemos/importer/operator.py"], "operator backup/live-DB behavior changed"),
    ("pai-no-test", ["mnemos/importer/pai.py"], "schema or lifecycle behavior changed"),
    ("cli-no-test", ["mnemos/cli.py"], "CLI behavior changed"),
]


@pytest.mark.parametrize("cid,changed,needle", _MAPPING_CASES, ids=[c[0] for c in _MAPPING_CASES])
def test_proof_surface_mapping(cid, changed, needle):
    findings = _findings(changed_files=changed)
    assert any(needle in f.description for f in findings)
    assert any(f.action == "must-test" for f in findings)


def test_watcher_missing_specific_proofs():
    # Watcher changed + tests in diff, but a required proof marker is absent.
    findings = _findings(
        changed_files=WATCH + ["tests/test_u3c_pai_watch_doctor.py"],
        file_texts=_file_texts(**{"mnemos/importer/watcher.py": "def f():\n    return 1\n"}),
    )
    assert any(f.status == "missing" for f in findings)


# =============================================================================
# Class 5 — cli / packaging / workflow spoofing
# =============================================================================
_WORKFLOW_CASES = [
    ("real-build",        "- name: b\n  run: uv build\n", False),
    ("shell-if-false",    "- name: b\n  run: if false; then uv build; fi\n", True),
    ("or-true",           "- name: b\n  run: uv build || true\n", True),
    ("var-assignment",    '- name: b\n  run: BUILD="uv build"\n', True),
    ("shell-comment",     "- name: b\n  run: |\n    echo go\n    # uv build\n", True),
    ("echo-only",         '- name: b\n  run: echo "uv build"\n', True),
    ("yaml-if-false",     "- name: b\n  if: false\n  run: uv build\n", True),
    ("continue-on-error", "- name: b\n  continue-on-error: true\n  run: uv build\n", True),
]


@pytest.mark.parametrize("cid,step,should_flag", _WORKFLOW_CASES, ids=[c[0] for c in _WORKFLOW_CASES])
def test_workflow_build_proof(cid, step, should_flag):
    findings = _wf(step)
    assert any(f.required_proof == "local build" for f in findings) is should_flag


# =============================================================================
# Class 6 — docs-only safety claims
# =============================================================================
def test_docs_fake_enforcement_links_flag():
    findings = _findings(
        changed_files=["docs/release-hardening.md"],
        file_texts=_file_texts(**{"docs/release-hardening.md":
            "# R\nBackups are verified; the watcher refuses live writes.\n"
            "See tests/does_not_exist.py and mnemos/importer/ghost.py\n"}),
    )
    assert any(f.file == "docs/release-hardening.md" for f in findings)


def test_docs_real_enforcement_links_pass():
    findings = _findings(
        changed_files=["docs/release-hardening.md"],
        file_texts=_file_texts(**{"docs/release-hardening.md":
            "# R\nBackups are verified before apply.\n"
            "Enforced by tests/test_u3c_pai_watch_doctor.py and mnemos/importer/watcher.py\n"}),
    )
    # references resolve in file_texts -> not flagged as docs-only risk
    assert not any(f.ident.startswith("RG-docs-risk") for f in findings)


def test_docs_evasive_keyword_claim_flag():
    findings = _findings(
        changed_files=["docs/release-hardening.md"],
        file_texts=_file_texts(**{"docs/release-hardening.md":
            "# R\nThis apply path is protected and will not corrupt operator data.\n"}),
    )
    assert any(f.file == "docs/release-hardening.md" for f in findings)


def test_docs_nondocs_markdown_scanned():
    findings = _findings(
        changed_files=["README.md"],
        file_texts=_file_texts(**{"README.md": "# R\nThe watcher refuses live writes. Backup is verified.\n"}),
    )
    assert any(f.file == "README.md" for f in findings)


def test_runtime_docstring_safety_claim_flag():
    findings = _findings(
        changed_files=WATCH,
        diff_text=_d("mnemos/importer/watcher.py", "def x():", '    """This refuses live writes and guarantees safe backup."""'),
    )
    assert any("runtime safety claim" in f.description for f in findings)


# =============================================================================
# Class 4 (persistence) — direct write API shape
# =============================================================================
_WRITE_CASES = [
    ("write_text-direct",   ("state_path.write_text(json.dumps(p))",), True),
    ("open-positional",     ('open(state_path, "w").write(x)',), True),
    ("open-kwarg-mode",     ('open(state_path, mode="w").write(x)',), True),  # E
    ("path-open",           ('Path(args.state).open("w").write(x)',), True),
    ("jsondump-open",       ('json.dump(p, open(state_path, "w"))',), True),
    ("alias-from-assign",   ("cfg = state_path", "cfg.write_text(json.dumps(p))"), True),
    ("safe-tmp",            ("tmp.write_text(json.dumps(p))",), False),
    ("safe-source",         ("source.write_text(data)",), False),
]


@pytest.mark.parametrize("cid,lines,should_flag", _WRITE_CASES, ids=[c[0] for c in _WRITE_CASES])
def test_persistence_write(cid, lines, should_flag):
    findings = _findings(changed_files=WATCH, diff_text=_d("mnemos/importer/watcher.py", *lines))
    assert _persist(findings) is should_flag


# Persistence writes through neutrally named variables are still non-atomic
# persistence writes in watcher-adjacent files unless the receiver is known-safe.
def test_persistence_neutral_var_from_call():
    findings = _findings(
        changed_files=WATCH,
        diff_text=_d("mnemos/importer/watcher.py", "dest = resolve_state_target()", "dest.write_text(json.dumps(p))"),
    )
    assert _persist(findings)


# =============================================================================
# Class 7 — base / path edge cases (real git, temp dir)
# =============================================================================
def _make_repo(tmp_path):
    def g(*a):
        subprocess.run(["git", *a], cwd=tmp_path, capture_output=True, text=True, check=True)
    g("init", "-q")
    g("config", "user.email", "a@b.c")
    g("config", "user.name", "t")
    return g


def test_rename_preserves_proof_requirement(tmp_path):
    g = _make_repo(tmp_path)
    (tmp_path / "mnemos" / "importer").mkdir(parents=True)
    (tmp_path / "mnemos" / "importer" / "watcher.py").write_text("# w\nDANGER = 1\n")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    g("mv", "mnemos/importer/watcher.py", "mnemos/importer/watcher_core.py")
    g("commit", "-q", "-m", "rename")
    changed = rg._git_changed_files(tmp_path, "HEAD~1")
    # rename detection must keep the canonical path so its proof requirement still fires
    assert "mnemos/importer/watcher.py" in changed
    surface = rg._proof_surface_findings(set(changed))
    assert any("watcher behavior changed" in f.description for f in surface)


def test_deleted_proof_test_while_surface_changed():
    # watcher changed + its required doctor test deleted from the tree -> missing proof.
    findings = _findings(
        changed_files=["mnemos/importer/watcher.py", "tests/test_u3c_pai_watcher.py", "tests/test_u3c_pai_watch_doctor.py"],
        file_texts=_file_texts(**{"tests/test_u3c_pai_watch_doctor.py": ""}),  # deleted/empty
    )
    assert any(f.status == "missing" for f in findings)


def test_nested_runtime_path_classified():
    # A deeply nested new runtime module is still in scope (allowlist policy).
    assert rg._is_runtime_source_file("mnemos/importer/sub/deep/sweep.py")
    findings = _findings(
        changed_files=["mnemos/importer/sub/deep/sweep.py"],
        diff_text=_d("mnemos/importer/sub/deep/sweep.py", 'conn.execute("DELETE FROM engrams")'),
    )
    assert _crit(findings)


# =============================================================================
# Pass 4 — adversarial eval of Codex's Step-3.5-FINAL patches
# (statement-split, any-receiver persistence, parametrize indirection, dedup).
# =============================================================================

# Area 1a — identity scope must require WHOLE-WORD id columns: a column merely
# CONTAINING "id" (valid/paid/void/...) or a tautology must NOT vouch scope.
@pytest.mark.parametrize(
    "where",
    ["valid = 1", "paid = 1", "void = 1", "rapid = 1", "grid = 1", "rowid = rowid"],
    ids=["valid", "paid", "void", "rapid", "grid", "rowid-tautology"],
)
def test_identity_scope_requires_whole_word_id(where):
    findings = _findings(
        changed_files=PAI,
        diff_text=_d("mnemos/importer/pai.py", f"conn.execute(\"UPDATE engrams SET state='archived' WHERE {where}\")"),
    )
    assert _crit(findings)


# Area 1b — a ';' inside a SQL string VALUE must not shatter a scoped statement
# into a false unscoped mutation (NOISE).
@pytest.mark.parametrize("val", ["a;b", "x; y", "k=1;k=2"], ids=["a;b", "x; y", "k=1;k=2"])
def test_semicolon_in_value_not_noisy(val):
    findings = _findings(
        changed_files=PAI,
        diff_text=_d("mnemos/importer/pai.py", f"conn.execute(\"UPDATE engrams SET note = '{val}' WHERE id = ?\", (i,))"),
    )
    assert not _crit(findings)


# Area 4 — dedup must collapse a double-counted single line but keep DISTINCT
# dangerous surfaces (two different tables) as separate findings.
def test_dedup_keeps_distinct_table_findings():
    diff = (
        "diff --git a/mnemos/importer/pai.py b/mnemos/importer/pai.py\n@@\n"
        '+conn.execute("DELETE FROM engrams")\n'
        '+conn.execute("DELETE FROM beliefs")\n'
    )
    crit = [f for f in rg._forbidden_diff_findings(diff) if f.severity == "critical"]
    assert len(crit) >= 2


def test_dedup_collapses_identical_double_count():
    # The same unscoped line counted by per-line + window scans -> one finding.
    findings = _findings(changed_files=PAI, diff_text=_d("mnemos/importer/pai.py", 'conn.execute("DELETE FROM engrams")'))
    crit = [f for f in findings if f.severity == "critical"]
    assert len(crit) == 1


def test_cross_table_id_false_scope():
    findings = _findings(
        changed_files=PAI,
        diff_text=_d("mnemos/importer/pai.py", "conn.execute(\"UPDATE engrams SET state='archived' FROM other WHERE other.id = ?\", (x,))"),
    )
    assert _crit(findings)


@pytest.mark.parametrize(
    "recv", ["report_path", "digest_path", "log_file", "summary_path"],
)
def test_persistence_noise_on_nonstate_writes(recv):
    findings = _findings(changed_files=WATCH, diff_text=_d("mnemos/importer/watcher.py", f"{recv}.write_text(data)"))
    assert not _persist(findings)


@pytest.mark.parametrize(
    "src",
    [
        "import pytest\nEMPTY=tuple()\n@pytest.mark.parametrize('x', EMPTY)\ndef t():\n    assert True\n",
        "import pytest\nfrom c import EMPTY\n@pytest.mark.parametrize('x', EMPTY)\ndef t():\n    assert True\n",
    ],
    ids=["tuple-call", "imported-empty"],
)
def test_empty_parametrize_tuple_call_and_imported_constant(src):
    assert rg._test_function_has_proof(src, "t") is False


# =============================================================================
# Real-diff guard: the actual U3c worktree diff must remain GREEN.
# =============================================================================
def test_real_worktree_diff_is_green():
    report = rg.run_pai_diff_review_gate(
        repo_root=None,
        base_ref="HEAD",
        intent_path="docs/u3c-step3-launch-intent.md",
    )
    assert report.ok, [f.ident for f in report.findings]
