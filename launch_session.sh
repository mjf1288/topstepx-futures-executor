#!/bin/bash
# Launch a Mean Levels session in the background with unbuffered output.
# The cron agent calls this script, which returns immediately.
# The session runner continues in the background, writing to the log file.
#
# Usage: ./launch_session.sh <window_name> <log_file>
# Example: ./launch_session.sh midday /home/user/workspace/cron_tracking/xyz/session.log

WINDOW_NAME="${1:-session}"
LOG_FILE="${2:-/tmp/mean_levels_session.log}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Create log directory if needed
mkdir -p "$(dirname "$LOG_FILE")"

# Source environment
cd "$SCRIPT_DIR"
export $(cat .env | xargs 2>/dev/null)

# Launch with unbuffered Python output
PYTHONUNBUFFERED=1 nohup python run_session.py --interval 10 > "$LOG_FILE" 2>&1 &
PID=$!

echo "Session '$WINDOW_NAME' launched (PID $PID)"
echo "Log: $LOG_FILE"
echo "$PID"
