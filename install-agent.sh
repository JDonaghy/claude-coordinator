#!/usr/bin/env bash
set -euo pipefail

# Defaults
VENV_DIR="$HOME/.coord-venv"
MACHINE_NAME=""
PORT=7433
INSTALL_SOURCE="claude-coordinator"  # PyPI package name
# Fall back to GitHub install if PyPI isn't published yet
GITHUB_REPO="https://github.com/JDonaghy/claude-coordinator.git"

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --machine) MACHINE_NAME="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --from-github) INSTALL_SOURCE="git+${GITHUB_REPO}"; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "=== claude-coordinator agent installer ==="

# Check Python 3.12+
python3 --version | grep -qE "3\.(1[2-9]|[2-9][0-9])" || {
    echo "error: Python 3.12+ required"; exit 1
}

# Check claude CLI
which claude >/dev/null 2>&1 || {
    echo "warning: 'claude' CLI not found on PATH"
    echo "  Workers need Claude Code CLI installed. Install it before starting the agent."
}

# Create/update venv
if [ -d "$VENV_DIR" ]; then
    echo "Updating existing installation at $VENV_DIR..."
else
    echo "Creating virtual environment at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi

# Install/upgrade
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install --upgrade "$INSTALL_SOURCE" -q
echo "Installed: $("$VENV_DIR/bin/coord" version)"

# Detect machine name if not provided
if [ -z "$MACHINE_NAME" ]; then
    MACHINE_NAME=$(hostname -s)
    echo "Machine name (from hostname): $MACHINE_NAME"
    echo "  Override with: --machine NAME"
fi

# Create systemd user unit
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/coord-agent.service" << UNIT
[Unit]
Description=Coordinator agent server (port $PORT)
After=network-online.target

[Service]
Type=simple
ExecStart=$VENV_DIR/bin/coord agent --machine $MACHINE_NAME --port $PORT
Restart=on-failure
RestartSec=5
Environment=PATH=$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin

[Install]
WantedBy=default.target
UNIT

# Enable and start
systemctl --user daemon-reload
systemctl --user enable coord-agent
systemctl --user restart coord-agent

# Enable lingering so the service runs even when not logged in
loginctl enable-linger "$(whoami)" 2>/dev/null || true

echo ""
echo "=== Agent installed and running ==="
echo "  Machine: $MACHINE_NAME"
echo "  Port: $PORT"
echo "  Service: systemctl --user status coord-agent"
echo "  Logs: journalctl --user -u coord-agent -f"
echo ""
echo "Next steps:"
echo "  1. Ensure coordinator.yml exists on the coordinator machine with this machine listed"
echo "  2. Run 'coord status' from the coordinator to verify connectivity"
echo ""
echo "To update later: re-run this script (it's idempotent)"
