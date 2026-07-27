#!/bin/bash
# TopstepX Futures Executor — Mac Setup Script
#
# Run this once to set up your environment.
# For the full step-by-step guide, see SETUP.md.

set -e

echo "═══════════════════════════════════════════════════"
echo "  TopstepX Futures Executor — Setup"
echo "═══════════════════════════════════════════════════"

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Install from https://python.org"
    exit 1
fi
PY_VER=$(python3 --version)
echo "✓ Python: $PY_VER"

# Warn about branch
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
if [ "$BRANCH" != "rebuild-data-layer" ]; then
    echo ""
    echo "⚠️  You're on branch '$BRANCH', not 'rebuild-data-layer'."
    echo "   The working engine lives on 'rebuild-data-layer'. Switching..."
    git checkout rebuild-data-layer 2>/dev/null || {
        echo "❌ Could not switch. Run: git fetch && git checkout rebuild-data-layer"
        exit 1
    }
fi
echo "✓ Branch: rebuild-data-layer"

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate
echo "✓ Virtual environment ready"

# Install dependencies from requirements.txt (single source of truth)
echo "Installing dependencies..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

# Explicit reminder: do NOT install project-x-py.
# The old wrapper library is abandoned + broken. The new stack uses
# `requests` + `signalrcore` directly against ProjectX Gateway API.
if pip show project-x-py > /dev/null 2>&1; then
    echo ""
    echo "⚠️  project-x-py is installed but no longer used. Uninstalling..."
    pip uninstall -y project-x-py > /dev/null
fi
echo "✓ Dependencies installed"

# Check .env
if [ ! -f ".env" ]; then
    echo ""
    if [ -f ".env.example" ]; then
        echo "⚠️  No .env file found. Copying from .env.example..."
        cp .env.example .env
        echo "   Edit .env and fill in your real credentials before running the engine."
    else
        echo "⚠️  Create a .env file with your TopstepX credentials:"
        echo "    PROJECT_X_API_KEY=your_api_key"
        echo "    PROJECT_X_USERNAME=your_username"
        echo "    PROJECT_X_ACCOUNT_NAME=your_account_name"
    fi
    exit 1
fi
echo "✓ .env found"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  1) Verify credentials:  python test_topstep_api.py"
echo "  2) Verify streaming:    python test_topstep_stream.py --minutes 5"
echo "  3) Dry-run engine:      python realtime_engine.py --mes buy --dry-run"
echo "  4) Go live:             python realtime_engine.py --mes buy"
echo ""
echo "  Full guide: SETUP.md"
echo "═══════════════════════════════════════════════════"
