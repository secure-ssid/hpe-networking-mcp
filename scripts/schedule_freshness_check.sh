#!/usr/bin/env bash
# Install/remove a recurring RAG source freshness check.
#
# The job runs `refresh_rag_sources.py --check-only`: it reports which upstream
# doc sources have drifted but never re-scrapes or rebuilds on its own. That is
# deliberate — a full refresh re-crawls thousands of pages and rebuilds an index
# that takes hours, which is not something that should start unattended. The
# check tells you when a refresh is worth running; you then run it yourself:
#
#   uv run python scripts/refresh_rag_sources.py
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

LABEL="com.hpe-networking-mcp.rag-freshness"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${ROOT}/data"
LOG="${LOG_DIR}/freshness-check.log"

# Day 0 = Sunday. Default: Sundays at 04:00 local time.
WEEKDAY="${FRESHNESS_WEEKDAY:-0}"
HOUR="${FRESHNESS_HOUR:-4}"
MINUTE="${FRESHNESS_MINUTE:-0}"

usage() {
  cat <<EOF
Usage: $(basename "$0") <install|uninstall|status|run>

  install    Register a weekly job that reports source drift. Prefers a launchd
             agent; falls back to crontab when ~/Library/LaunchAgents is not
             writable (managed/MDM machines) or the host is not macOS.
  uninstall  Remove whichever of the two is registered.
  status     Show whether the job is registered and when it last ran.
  run        Run the check once, right now, in the foreground.

Schedule is controlled by env vars at install time (defaults: Sunday 04:00):
  FRESHNESS_WEEKDAY=0-6 (0=Sunday)  FRESHNESS_HOUR=0-23  FRESHNESS_MINUTE=0-59
EOF
}

CRON_MARK="# hpe-networking-mcp-rag-freshness"

cron_line() {
  local uv_bin
  uv_bin="$(command -v uv || echo uv)"
  echo "${MINUTE} ${HOUR} * * ${WEEKDAY} cd ${ROOT} && PATH=\"$(dirname "${uv_bin}"):\$PATH\" /bin/bash ${SCRIPT_DIR}/$(basename "$0") run >> ${LOG} 2>&1 ${CRON_MARK}"
}

install_cron() {
  local existing
  existing="$(crontab -l 2>/dev/null | grep -v "${CRON_MARK}" || true)"
  printf '%s\n%s\n' "${existing}" "$(cron_line)" | sed '/^$/d' | crontab -
  echo "Installed weekly cron entry:"
  echo "  $(cron_line)"
  echo "  log: ${LOG}"
  echo
  echo "The job only reports drift. To act on it:"
  echo "  uv run python scripts/refresh_rag_sources.py"
}

uninstall_cron() {
  if ! crontab -l 2>/dev/null | grep -q "${CRON_MARK}"; then
    return 1
  fi
  crontab -l 2>/dev/null | grep -v "${CRON_MARK}" | crontab -
  echo "Removed cron entry."
  return 0
}

do_run() {
  local uv_bin
  uv_bin="$(command -v uv || true)"
  if [[ -z "${uv_bin}" ]]; then
    echo "uv not found on PATH" >&2
    exit 1
  fi
  mkdir -p "${LOG_DIR}"
  cd "${ROOT}"
  echo "=== $(date -u '+%Y-%m-%dT%H:%M:%SZ') freshness check ==="
  # --check-only exits 0 whether or not drift was found, so detect it from the
  # report body rather than the status code.
  local out status
  set +e
  out="$("${uv_bin}" run python scripts/refresh_rag_sources.py --check-only 2>&1)"
  status=$?
  set -e
  echo "${out}"

  if [[ ${status} -ne 0 ]]; then
    echo "freshness check exited ${status}"
    return 0
  fi
  if grep -q '^\[CHANGED\]' <<<"${out}"; then
    local changed
    changed="$(grep -c '^\[CHANGED\]' <<<"${out}")"
    echo "==> ${changed} source(s) drifted; run: uv run python scripts/refresh_rag_sources.py"
    if command -v osascript >/dev/null 2>&1; then
      osascript -e "display notification \"${changed} RAG source(s) changed upstream\" with title \"hpe-networking-mcp\"" \
        >/dev/null 2>&1 || true
    fi
  fi
  return 0
}

do_install() {
  local uv_bin
  uv_bin="$(command -v uv || true)"
  if [[ -z "${uv_bin}" ]]; then
    echo "uv not found on PATH; install it before scheduling." >&2
    exit 1
  fi
  mkdir -p "${LOG_DIR}"

  # launchd is preferred (survives reboots cleanly, has real calendar
  # scheduling), but ~/Library/LaunchAgents is root-owned on managed machines
  # and writing there would need sudo on a path MDM may own. Fall back to the
  # user crontab, which needs no privileges, rather than escalating.
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Host is $(uname -s), not macOS — using cron."
    install_cron
    return 0
  fi
  if ! mkdir -p "$(dirname "${PLIST}")" 2>/dev/null || [[ ! -w "$(dirname "${PLIST}")" ]]; then
    echo "$(dirname "${PLIST}") is not writable (owned by $(stat -f '%Su' "$(dirname "${PLIST}")" 2>/dev/null || echo 'unknown'))."
    echo "Using cron instead — no elevated privileges required."
    echo
    install_cron
    return 0
  fi

  # launchd starts jobs with a minimal environment and no PATH to Homebrew or
  # ~/.local/bin, so the wrapper is invoked by absolute path and re-exports a
  # PATH that includes wherever uv actually lives.
  cat > "${PLIST}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${SCRIPT_DIR}/$(basename "$0")</string>
        <string>run</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(dirname "${uv_bin}"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>${ROOT}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>${WEEKDAY}</integer>
        <key>Hour</key>
        <integer>${HOUR}</integer>
        <key>Minute</key>
        <integer>${MINUTE}</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG}</string>
    <key>StandardErrorPath</key>
    <string>${LOG}</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

  launchctl unload "${PLIST}" >/dev/null 2>&1 || true
  launchctl load "${PLIST}"

  echo "Installed ${LABEL}"
  echo "  schedule: weekday=${WEEKDAY} ${HOUR}:$(printf '%02d' "${MINUTE}") local"
  echo "  plist:    ${PLIST}"
  echo "  log:      ${LOG}"
  echo
  echo "The job only reports drift. To act on it:"
  echo "  uv run python scripts/refresh_rag_sources.py"
}

do_uninstall() {
  local removed=0
  if uninstall_cron; then
    removed=1
  fi
  if [[ -f "${PLIST}" ]]; then
    launchctl unload "${PLIST}" >/dev/null 2>&1 || true
    if rm -f "${PLIST}" 2>/dev/null; then
      echo "Removed ${LABEL} agent."
      removed=1
    else
      echo "Could not remove ${PLIST} (permission denied); remove it with sudo." >&2
    fi
  fi
  [[ ${removed} -eq 1 ]] || echo "Nothing installed."
}

do_status() {
  local found=0
  if crontab -l 2>/dev/null | grep -q "${CRON_MARK}"; then
    echo "cron entry registered:"
    crontab -l 2>/dev/null | grep "${CRON_MARK}"
    found=1
  fi
  if [[ -f "${PLIST}" ]]; then
    echo "plist present: ${PLIST}"
    found=1
    if launchctl list 2>/dev/null | grep -q "${LABEL}"; then
      echo "registered with launchd:"
      launchctl list | grep "${LABEL}"
    else
      echo "plist exists but is not loaded into launchd."
    fi
  fi
  [[ ${found} -eq 1 ]] || echo "Not installed."
  if [[ -f "${LOG}" ]]; then
    echo
    echo "last log lines (${LOG}):"
    tail -n 15 "${LOG}"
  fi
}

case "${1:-}" in
  install)   do_install ;;
  uninstall) do_uninstall ;;
  status)    do_status ;;
  run)       do_run ;;
  *)         usage; exit 1 ;;
esac
