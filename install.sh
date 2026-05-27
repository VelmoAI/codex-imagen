#!/bin/sh
# install.sh — bootstrap uv (if needed) and run codex-imagen setup
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/VelmoAI/codex-imagen/main/install.sh | sh
#
# POSIX-compatible (sh, bash, zsh, dash).  No bashisms.
set -eu

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

say() {
    printf '%s\n' "$1"
}

die() {
    printf 'Error: %s\n' "$1" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# 1. Ensure uv is available
# ---------------------------------------------------------------------------

if command -v uvx >/dev/null 2>&1; then
    say "uv is already installed."
else
    say "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh || die "uv installation failed."

    # Re-source PATH so the newly-installed uvx is visible in this shell.
    # The official installer drops the binary at ~/.local/bin on most platforms.
    if [ -f "$HOME/.local/bin/env" ]; then
        # shellcheck disable=SC1091
        . "$HOME/.local/bin/env"
    fi

    # Cargo-based installer (older uv versions) sets up ~/.cargo/env
    if [ -f "$HOME/.cargo/env" ]; then
        # shellcheck disable=SC1091
        . "$HOME/.cargo/env"
    fi

    # Last-resort: put ~/.local/bin directly on PATH if it exists
    if [ -d "$HOME/.local/bin" ]; then
        export PATH="$HOME/.local/bin:$PATH"
    fi

    # Confirm we can now find uvx
    if ! command -v uvx >/dev/null 2>&1; then
        # Try explicit path before giving up
        if [ -x "$HOME/.local/bin/uvx" ]; then
            UVX="$HOME/.local/bin/uvx"
        else
            die "uvx was installed but could not be found on PATH. Please open a new shell and run: uvx codex-imagen setup"
        fi
    fi
fi

# Use explicit path if set, otherwise rely on PATH
UVX="${UVX:-uvx}"

# ---------------------------------------------------------------------------
# 2. Run codex-imagen setup
# ---------------------------------------------------------------------------

say "Running codex-imagen setup..."
"$UVX" codex-imagen setup || die "codex-imagen setup failed."

say "Done — your AI clients now know about imagen."
