#!/bin/bash
# Set up GoWild Finder and add "GoWild Finder" to your Applications folder.
# Run once from the repo folder:  bash scripts/install-mac.sh
# Run it again after a `git pull` that changes requirements.txt.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

echo "Setting up Python packages..."
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "Installing the app's browser (one time, ~150 MB)..."
.venv/bin/python -m playwright install chromium

APP="$HOME/Applications/GoWild Finder.app"
echo "Creating $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>GoWild Finder</string>
  <key>CFBundleIdentifier</key><string>local.gowild-finder</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/launch" <<LAUNCH
#!/bin/bash
# Start the GoWild Finder server if it isn't running, then open it in your browser.
cd "$REPO"
if ! curl -s -o /dev/null http://127.0.0.1:5050/; then
  nohup .venv/bin/python -m gowild serve --phone > server.log 2>&1 &
  for i in \$(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:5050/ && break; sleep 0.25; done
fi
open http://127.0.0.1:5050/
LAUNCH
chmod +x "$APP/Contents/MacOS/launch"

echo
echo "Done. Open Finder > Applications (your home folder's) > GoWild Finder,"
echo "or press Cmd+Space and type GoWild. Drag it to the Dock to keep it handy."
echo "To stop the app's server: pkill -f 'gowild serve'"
