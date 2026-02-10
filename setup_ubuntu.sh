#!/bin/bash

# Ubuntu Setup Script for Nifty Options Algo
# This script handles virtual environment creation and dependency installation.

echo "🚀 Starting Ubuntu Environment Setup..."

# 1. Update system package list
echo "📦 Updating package list..."
sudo apt update

# 2. Install python3-venv if not present
echo "🐍 Ensuring python3-venv is installed..."
sudo apt install -y python3-venv python3-pip

# 3. Create virtual environment
if [ ! -d "venv" ]; then
    echo "⚙️ Creating virtual environment 'venv'..."
    python3 -m venv venv
else
    echo "✅ Virtual environment 'venv' already exists."
fi

# 4. Activate and install dependencies
echo "📥 Installing dependencies from requirements.txt..."
source venv/bin/activate

# Upgrade pip first
pip install --upgrade pip

# Install requirements
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
else
    echo "⚠️ requirements.txt not found! Installing core packages manually..."
    pip install upstox-python-sdk colorama python-dotenv pandas numpy scipy requests matplotlib
fi

# 5. Set permissions for helper script
echo "🔐 Setting execute permissions for run_algo.sh..."
chmod +x run_algo.sh

echo "✅ Setup Complete!"
echo "------------------------------------------------"
echo "To run your algo, use the helper script:"
echo "  ./run_algo.sh run_strategy.py"
echo "------------------------------------------------"
