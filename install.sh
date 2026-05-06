#!/bin/bash
# install.sh — Auction Watcher setup on Mac Mini
# Run once from ~/auction-watcher: bash install.sh
set -e
REPO_DIR="$HOME/auction-watcher"
PLIST="com.auctionwatcher.api.plist"

echo "=== Auction Watcher — Install ==="
mkdir -p "$REPO_DIR/data" "$REPO_DIR/logs"
echo "Directories ready"

cp "$REPO_DIR/$PLIST" "$HOME/Library/LaunchAgents/$PLIST"
launchctl bootout gui/$(id -u) "$HOME/Library/LaunchAgents/$PLIST" 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/$PLIST"
echo "launchd service installed (com.auctionwatcher.api)"

sleep 2
if curl -s http://localhost:7474/health | grep -q '"ok":true'; then
  echo "API server running on :7474"
else
  echo "API server not responding yet — check logs/api_server_launchd.log"
fi

echo ""
echo "=== Next steps ==="
echo "1. Load extension in Brave:"
echo "   brave://extensions -> Developer Mode -> Load unpacked -> $REPO_DIR/extension/"
echo ""
echo "2. Add to iPhone:"
echo "   Safari -> https://ocx11.github.io/auction-watcher -> Share -> Add to Home Screen"
echo ""
echo "Done."
