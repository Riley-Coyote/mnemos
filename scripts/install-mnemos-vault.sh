#!/bin/bash
#
# install-mnemos-vault.sh — DRAFT. David runs this by hand, once, after reading
# every line. The agent DRAFTS it and can only propose diffs; it never runs it.
# That the agent cannot install its own gate IS the security proof (design §6/§8).
#
# What it builds (design §2.1 / §5):
#   - a login-less OS user  `mnemos-vault`  that owns the journal
#   - a group  `mnemos-read`  (David is a member) that may READ the journal
#   - the vault dir + append-only journal file the agent's account cannot write
#   - the TCB `mnemos-decide` installed root-owned outside every agent path
#   - a sudoers rule letting ONLY David run ONLY that binary as mnemos-vault,
#     with timestamp_timeout=0 (password every invocation — design §5 mitigation b)
#
# It is idempotent-safe: re-running it changes nothing already correct. It makes
# no memory.db changes and touches nothing under ~ except group membership.
#
# Requires: macOS, sudo/admin. Run:  sudo bash install-mnemos-vault.sh
# Nothing here is executed until YOU run it. Read first.

set -euo pipefail

VAULT_USER="mnemos-vault"
READ_GROUP="mnemos-read"
VAULT_DIR="/usr/local/var/mnemos-vault"
JOURNAL="${VAULT_DIR}/decisions.jsonl"
LIBEXEC="/usr/local/libexec/mnemos-vault"
TCB_DEST="${LIBEXEC}/mnemos-decide"
TCB_SRC="$(cd "$(dirname "$0")" && pwd)/mnemos-decide"
SUDOERS="/etc/sudoers.d/mnemos-vault"
DAVID="${SUDO_USER:-$(id -un)}"

echo ">> This will create user '${VAULT_USER}', group '${READ_GROUP}', the vault"
echo ">> at ${VAULT_DIR}, install the TCB at ${TCB_DEST}, and add a sudoers rule."
echo ">> David account for the sudoers rule + read group: ${DAVID}"
read -r -p ">> Proceed? [y/N] " ANSWER
[ "${ANSWER}" = "y" ] || { echo "Aborted."; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
  echo "!! Must run as root (sudo bash install-mnemos-vault.sh)."; exit 1
fi

# ── 0. Verify EVERY ancestor of the install paths is root-owned + not writable
#      by David (008e-r4 #4). On Homebrew macOS layouts /usr/local and its
#      subdirs are often owned by David; if any ancestor is writable, the agent
#      can rename or replace the vault dir or the TCB path despite root
#      ownership on the leaf. Fail the ceremony rather than silently ship a
#      compromisable install.
verify_ancestors_root_owned() {
  local target="$1"
  local dir
  dir="$(dirname "${target}")"
  while [ "${dir}" != "/" ]; do
    if [ ! -e "${dir}" ]; then
      dir="$(dirname "${dir}")"
      continue
    fi
    local owner group perms
    owner="$(stat -f '%Su' "${dir}")"
    group="$(stat -f '%Sg' "${dir}")"
    perms="$(stat -f '%Sp' "${dir}")"
    if [ "${owner}" != "root" ]; then
      echo "!! Insecure parent ${dir}: owner=${owner} (must be root)."
      echo "!! Agent can rename/replace this to compromise the vault path."
      return 1
    fi
    # Group-writable or world-writable → compromisable.
    case "${perms}" in
      *w?????|*w??|*w) : ;;  # bit 3/6/9 shouldn't be 'w' outside owner
    esac
    if [ "$(stat -f '%Mp%Lp' "${dir}" | tail -c 4 | cut -c1)" -ge 2 ] 2>/dev/null; then
      : # macOS stat quirk workaround; use plain permission read below
    fi
    # Simpler + portable: check group/other write bits directly.
    local octal
    octal="$(stat -f '%Lp' "${dir}")"
    if [ $(( octal / 10 % 10 & 2 )) -ne 0 ] || [ $(( octal % 10 & 2 )) -ne 0 ]; then
      echo "!! Insecure parent ${dir}: mode ${octal} allows group/other write."
      return 1
    fi
    # 008k-r13 #5: POSIX bits don't reveal macOS ACLs. An ACL granting the
    # agent's account (David's) write on a root-owned, mode-safe ancestor
    # would still let the agent rename/replace the vault or TCB path. Check
    # EFFECTIVE writability as David — test -w honors ACLs. Also surface any
    # explicit ACL entries for a human read.
    if sudo -u "${DAVID}" test -w "${dir}"; then
      echo "!! Insecure parent ${dir}: writable by ${DAVID} despite root"
      echo "!! ownership + safe mode bits (likely a macOS ACL). Inspect:"
      echo "!!   ls -lde ${dir}"
      echo "!! Remove the offending ACL (chmod -a '...') before installing."
      return 1
    fi
    dir="$(dirname "${dir}")"
  done
  return 0
}

# 008-r14 review (#2): POSIX chmod does NOT clear macOS ACLs. The ancestor
# check above guards the PARENTS, but the leaf paths we create/chown/chmod
# below (VAULT_DIR, JOURNAL, LIBEXEC) can still carry a pre-existing ACL —
# inherited from a parent's inheritable ACL, or set on a leftover dir from a
# prior run — that survives `chmod` and grants David/agent write despite the
# ownership bits, letting them rename the journal or replace the sudoers-
# targeted TCB. For each hardened leaf: strip ALL ACL entries (chmod -N), then
# re-verify David has NO effective write (test -w honors ACLs). Refuse install
# if an ACL survives.
require_no_effective_write() {
  local path="$1"
  chmod -N "${path}" 2>/dev/null || true   # strip every ACL entry from the leaf
  if sudo -u "${DAVID}" test -w "${path}"; then
    echo "!! ${path} is EFFECTIVELY WRITABLE by ${DAVID} after chmod -N."
    echo "!! An ACL survives the ownership bits. Inspect + remove it:"
    echo "!!   ls -lde ${path}"
    echo "!!   sudo chmod -a '...' ${path}"
    echo "!! Refusing to install a vault whose journal/TCB the agent could"
    echo "!! still rename or replace."
    exit 1
  fi
}

for p in "${VAULT_DIR}" "${LIBEXEC}"; do
  if ! verify_ancestors_root_owned "${p}"; then
    echo "!! Refusing to install under a compromisable path prefix: ${p}"
    echo "!! On Homebrew macOS, /usr/local is typically owned by the user."
    echo "!! Fix the ownership so the ancestors are root-owned:"
    echo "!!   sudo chown root:wheel /usr/local /usr/local/var /usr/local/libexec"
    echo "!! 008r-review (unsupported-alternate-vault-path-guidance): do NOT"
    echo "!! 'reinstall under another path' — the store resolver, watchdog, and"
    echo "!! TCB all PIN ${VAULT_DIR} and ${LIBEXEC} as constants (deliberately,"
    echo "!! so no agent-reachable channel can redirect them). A different path"
    echo "!! would build a vault the runtime never reads until the pinned"
    echo "!! constants are changed in ALL of: mnemos/store/sqlite_store.py"
    echo "!! (_CANONICAL_VAULT_DIR/_CANONICAL_VAULT_JOURNAL), scripts/mnemos-"
    echo "!! vault-watchdog.py (CANONICAL_*), scripts/mnemos-decide"
    echo "!! (CANONICAL_JOURNAL_PATH), and this installer — together."
    exit 1
  fi
done

# ── 1. The read group (idempotent) ──
if ! dscl . -read "/Groups/${READ_GROUP}" >/dev/null 2>&1; then
  echo ">> Creating group ${READ_GROUP}"
  NEXT_GID=$(( $(dscl . -list /Groups PrimaryGroupID | awk '{print $2}' | sort -n | tail -1) + 1 ))
  dscl . -create "/Groups/${READ_GROUP}"
  dscl . -create "/Groups/${READ_GROUP}" PrimaryGroupID "${NEXT_GID}"
else
  echo "== Group ${READ_GROUP} already exists"
fi
if ! dseditgroup -o checkmember -m "${DAVID}" "${READ_GROUP}" >/dev/null 2>&1; then
  echo ">> Adding ${DAVID} to ${READ_GROUP}"
  dseditgroup -o edit -a "${DAVID}" -t user "${READ_GROUP}"
else
  echo "== ${DAVID} already in ${READ_GROUP}"
fi

# ── 2. The vault user (login-less, no home) (idempotent) ──
if ! dscl . -read "/Users/${VAULT_USER}" >/dev/null 2>&1; then
  echo ">> Creating login-less user ${VAULT_USER}"
  NEXT_UID=$(( $(dscl . -list /Users UniqueID | awk '{print $2}' | sort -n | tail -1) + 1 ))
  dscl . -create "/Users/${VAULT_USER}"
  dscl . -create "/Users/${VAULT_USER}" UserShell /usr/bin/false
  dscl . -create "/Users/${VAULT_USER}" RealName "Mnemos Vault"
  dscl . -create "/Users/${VAULT_USER}" UniqueID "${NEXT_UID}"
  dscl . -create "/Users/${VAULT_USER}" PrimaryGroupID "$(dscl . -read /Groups/${READ_GROUP} PrimaryGroupID | awk '{print $2}')"
  dscl . -create "/Users/${VAULT_USER}" NFSHomeDirectory /var/empty
else
  echo "== User ${VAULT_USER} already exists"
fi

# ── 3. The vault dir + append-only journal ──
echo ">> Ensuring vault dir ${VAULT_DIR} (0750, ${VAULT_USER}:${READ_GROUP})"
mkdir -p "${VAULT_DIR}"
chown "${VAULT_USER}:${READ_GROUP}" "${VAULT_DIR}"
chmod 0750 "${VAULT_DIR}"
require_no_effective_write "${VAULT_DIR}"   # 008-r14 #2: strip+verify leaf ACL
if [ ! -f "${JOURNAL}" ]; then
  echo ">> Creating empty journal ${JOURNAL}"
  touch "${JOURNAL}"
fi
chown "${VAULT_USER}:${READ_GROUP}" "${JOURNAL}"
chmod 0640 "${JOURNAL}"
require_no_effective_write "${JOURNAL}"   # 008-r14 #2: strip+verify leaf ACL (before uappnd)
# user-append-only: even the owner can only append; the agent (not owner) cannot
# unset this flag. This is the OS-level append-only guarantee (design §2.1).
echo ">> Setting append-only flag (uappnd) on ${JOURNAL}"
chflags uappnd "${JOURNAL}"

# ── 4. The TCB, root-owned, outside every agent-writable path ──
# 008g-r7 #1: the ancestor check verified /usr/local/libexec's parents, but
# ${LIBEXEC} itself must be root-owned + non-writable — else the sudoers
# rule points at a TCB inside a writable directory, and the agent can
# replace mnemos-decide after install despite root ownership of the file.
echo ">> Installing TCB ${TCB_SRC} -> ${TCB_DEST} (root:wheel, 0755)"
install -d -o root -g wheel -m 0755 "${LIBEXEC}"
# Re-verify AFTER (mkdir/install -d could silently keep an existing dir's
# permissions; force the state we require and fail if we can't).
chown root:wheel "${LIBEXEC}"
chmod 0755 "${LIBEXEC}"
libexec_perms="$(stat -f '%Lp' "${LIBEXEC}")"
libexec_owner="$(stat -f '%Su' "${LIBEXEC}")"
if [ "${libexec_owner}" != "root" ] || [ $(( libexec_perms % 100 % 10 & 2 )) -ne 0 ] \
   || [ $(( libexec_perms / 10 % 10 & 2 )) -ne 0 ]; then
  echo "!! ${LIBEXEC} is not root:wheel + non-writable (owner=${libexec_owner}, mode=${libexec_perms})."
  echo "!! Sudoers would point at a TCB inside a compromisable directory."
  exit 1
fi
require_no_effective_write "${LIBEXEC}"   # 008-r14 #2: strip+verify leaf ACL (stat bits don't reveal ACLs)
install -o root -g wheel -m 0755 "${TCB_SRC}" "${TCB_DEST}"

# ── 5. The sudoers rule: only David, only this binary, password every time ──
echo ">> Writing sudoers rule ${SUDOERS} (timestamp_timeout=0)"
TMP_SUDO="$(mktemp)"
cat > "${TMP_SUDO}" <<EOF
# Only ${DAVID} may run ONLY the vault TCB as ${VAULT_USER}, password every time.
# 008r-review (sudoers-allows-tcb-redirect-args): restrict to the EXACT invocation
# forms — no args (interactive decide), --witness-legacy, and --initial-rollout.
# An unrestricted rule would let --db/--journal redirect the canonical journal/DB
# under sudo while printing a successful decision. "" matches a no-argument
# invocation; the other forms match exactly --witness-legacy / --initial-rollout.
# Anything else (e.g. --journal /tmp/fake) is denied by sudo. (The TCB ALSO
# self-refuses the redirect flags when SUDO_USER is set.)
# DAVID-10 / 013b: --initial-rollout is the ONE-TIME ceremony pass that witnesses
# the review_only curated hypomnema corpus and promotes it operational; David
# runs it once, standalone, at the ceremony (same standalone form as
# --witness-legacy). Coherence is mandatory (013b fix 4): this rule, the TCB
# argument handling, and the ceremony walkthrough all name `--initial-rollout`.
Defaults!${TCB_DEST} timestamp_timeout=0
${DAVID} ALL=(${VAULT_USER}) PASSWD: ${TCB_DEST} "", ${TCB_DEST} --witness-legacy, ${TCB_DEST} --initial-rollout
EOF
# Validate BEFORE installing — a broken sudoers file can lock you out.
visudo -cf "${TMP_SUDO}"
install -o root -g wheel -m 0440 "${TMP_SUDO}" "${SUDOERS}"
rm -f "${TMP_SUDO}"
visudo -cf "${SUDOERS}"

# ── 6. Read-access smoke check ──
# The vault user must be able to TRAVERSE David's home and READ memory.db. On
# modern macOS, home dirs are often staff-750 and mnemos-vault is not in staff —
# so the ceremony would fail at attack-checklist step B with a bare permission
# error. Check now and, on failure, print the minimal read-only ACLs to grant.
DAVID_HOME="$(dscl . -read "/Users/${DAVID}" NFSHomeDirectory | awk '{print $2}')"
DB_FOR_CHECK="${DAVID_HOME}/.mnemos/memory.db"
echo ">> Smoke check: can ${VAULT_USER} open ${DB_FOR_CHECK} via SQLite (WAL)?"
# 008e-r4 #5: the TCB opens memory.db via SQLite in read-only URI mode. Raw
# open(...).read(1) doesn't exercise the WAL sidecar files (memory.db-wal /
# -shm) — a missing sidecar grant passes the raw check but the actual TCB
# query fails or reads a stale checkpoint. Smoke-test the real query path.
if sudo -u "${VAULT_USER}" /usr/bin/python3 -c "
import sqlite3
c = sqlite3.connect('file:${DB_FOR_CHECK}?mode=ro', uri=True)
c.execute('SELECT COUNT(*) FROM proposal_ledger').fetchone()
c.close()" >/dev/null 2>&1; then
  echo "== read access OK (SQLite + WAL sidecars readable)"
else
  echo "!! ${VAULT_USER} cannot read the DB — home dir is likely staff-750."
  echo "!! The install is COMPLETE; only a minimal read grant is pending. Run:"
  echo "     sudo chmod +a \"${VAULT_USER} allow read,execute\" \"${DAVID_HOME}\""
  echo "     sudo chmod +a \"${VAULT_USER} allow read,execute\" \"${DAVID_HOME}/.mnemos\""
  echo "     sudo chmod +a \"${VAULT_USER} allow read\" \"${DB_FOR_CHECK}\""
  # 008e-r4 #5: WAL sidecars — the actual SQLite reader needs these too.
  echo "     sudo chmod +a \"${VAULT_USER} allow read\" \"${DB_FOR_CHECK}-wal\" 2>/dev/null || true"
  echo "     sudo chmod +a \"${VAULT_USER} allow read\" \"${DB_FOR_CHECK}-shm\" 2>/dev/null || true"
  echo ">> Then re-run the eight-step attack checklist. (exit 7 = read grant pending)"
  exit 7
fi

echo ">> Done. Verify with the eight-step attack checklist (design §4)."
echo ">>   ls -lO ${VAULT_DIR}   (expect uappnd + ${VAULT_USER} ownership)"
echo ">>   sudo -u ${VAULT_USER} ${TCB_DEST}   (expect a password prompt)"
