#!/bin/bash
# Tzu Strategic Momentum — Mac Setup Script
# Run this once to set up your environment

echo "═══════════════════════════════════════════════════"
echo "  Tzu Strategic Momentum — Setup"
echo "═══════════════════════════════════════════════════"

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Install from https://python.org"
    exit 1
fi
echo "✓ Python: $(python3 --version)"

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip
pip install project-x-py python-dotenv pytz polars numpy scipy aiohttp

# Check .env
if [ ! -f ".env" ]; then
    echo ""
    echo "⚠️  Create a .env file with your TopstepX credentials:"
    echo "    PROJECT_X_API_KEY=your_api_key"
    echo "    PROJECT_X_USERNAME=your_username"
    echo "    PROJECT_X_ACCOUNT_NAME=your_account_name"
    exit 1
fi
echo "✓ .env found"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Setup complete! Run the engine with:"
echo ""
echo "  source venv/bin/activate"
echo "  python realtime_engine.py --dry-run    # Test first"
echo "  python realtime_engine.py              # Go live"
echo "═══════════════════════════════════════════════════"
