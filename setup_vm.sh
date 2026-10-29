#!/bin/bash
# Auto YT Shorts — Automatic Install Script for a VM (Linode / Linux VPS)

set -e

echo "════════════════════════════════════════════════════════════"
echo "🚀 Installing Auto YT Shorts on your Linux VM..."
echo "════════════════════════════════════════════════════════════"

# 1. Install system dependencies (FFmpeg, Python, Cron)
echo "📦 1. Installing system packages (FFmpeg, Python, Cron)..."
sudo apt update
sudo apt install -y python3 python3-pip python3-venv ffmpeg cron git

# 2. Create and activate the virtual environment
echo "🐍 2. Setting up the Python virtual environment..."
cd "$(dirname "$0")"
python3 -m venv venv
source venv/bin/activate

# 3. Install Python dependencies
echo "📚 3. Installing dependencies (google-genai, edge-tts, youtube-api)..."
pip install --upgrade pip
pip install -r requirements.txt

# 4. Check FFmpeg
echo "🎬 4. Testing FFmpeg..."
ffmpeg -version | head -n 1

# 5. Set up the cron job (24/7, running at 10:00 AM)
echo "⏰ 5. Setting up the 24/7 scheduler (cron) to run every day at 10:00 AM..."
SCRIPT_DIR="$(pwd)"
CRON_CMD="0 10 * * * cd $SCRIPT_DIR && $SCRIPT_DIR/venv/bin/python daily_pipeline.py >> $SCRIPT_DIR/daily.log 2>&1"

# Add to the crontab without duplicating
(crontab -l 2>/dev/null | grep -v "daily_pipeline.py" ; echo "$CRON_CMD") | crontab -

# Enable the cron service if it isn't running
sudo systemctl enable cron 2>/dev/null || true
sudo systemctl start cron 2>/dev/null || true

echo "════════════════════════════════════════════════════════════"
echo "✅ INSTALLATION COMPLETED SUCCESSFULLY!"
echo "════════════════════════════════════════════════════════════"
echo "   • Directory: $SCRIPT_DIR"
echo "   • Cron 24/7: Runs daily at 10:00 AM"
echo "   • Run log: $SCRIPT_DIR/daily.log"
echo ""
echo "   💡 Tip: Make sure to copy over the credential files:"
echo "      - .env"
echo "      - client_secrets.json"
echo "      - token.pickle"
echo "════════════════════════════════════════════════════════════"
