#!/bin/bash
# Deploy script for PythonAnywhere
# Usage: bash deploy.sh

set -e

cd "$(dirname "$0")"

echo "==> Pulling latest code..."
git pull

echo "==> Installing dependencies..."
pip install -e . --quiet

echo "==> Done! Restart the web app from the PythonAnywhere dashboard."
