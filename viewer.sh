#!/usr/bin/env bash
# KPI Crawler Artifact Viewer & Exporter Launcher Script

cd "$(dirname "$0")"

PORT=${1:-8080}

echo "============================================================"
echo "  🚀 KPI Crawler Artifact Viewer & Exporter Application"
echo "============================================================"
echo ""
echo "Starting local server on http://localhost:${PORT}..."
echo "Press Ctrl+C to stop the server."
echo ""

python3 viewer.py "$PORT"
