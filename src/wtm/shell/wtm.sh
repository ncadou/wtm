# wtm shell integration.
#
# Source this file from your shell rc (e.g. ~/.zshrc or ~/.bashrc):
#
#     eval "$(wtm shell-init)"                  # from the installed wtm, or
#     . /path/to/wtm/src/wtm/shell/wtm.sh       # by path from a clone
#
# It defines a thin `wtm` wrapper so movers like `enter`, `create -c`, `leave`,
# `rename`, and `workspace enter` can cd this shell and sync the WTM_* context
# environment. Diagnostics from wtm go to stderr; shell directives (a cd plus
# export/unset of the WTM_* session contract) are passed out-of-band via the
# WTM_SHELL_FILE temp file, which the wrapper sources and then removes. Without
# this wrapper, wtm stays silent about the environment (it cannot alter the
# parent shell) and only prints a manual-cd notice.
wtm() {
    local _wtm_file _wtm_rc
    _wtm_file=$(mktemp "${TMPDIR:-/tmp}/wtm-shell.XXXXXX") || return 1
    WTM_SHELL_FILE="$_wtm_file" command wtm "$@"
    _wtm_rc=$?
    if [ -s "$_wtm_file" ]; then
        . "$_wtm_file"
    fi
    rm -f "$_wtm_file"
    return $_wtm_rc
}
