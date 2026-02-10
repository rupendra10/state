#!/bin/bash

# Helper script to run Python scripts within the virtual environment

if [ ! -d "venv" ]; then
    echo "❌ Virtual environment 'venv' not found."
    echo "Please run ./setup_ubuntu.sh first."
    exit 1
fi

if [ -z "$1" ]; then
    echo "❌ No script specified."
    echo "Usage: ./run_algo.sh <script_name.py>"
    echo "Example: ./run_algo.sh run_strategy.py"
    exit 1
fi

SCRIPT_NAME=$1

echo "🔄 Activating environment and running $SCRIPT_NAME..."
source venv/bin/activate
python3 "$SCRIPT_NAME"
deactivate
echo "👋 Environment deactivated."
