#!/data/data/com.termux/files/usr/bin/bash
# Sets up Jarvis's hands on this Android phone. Jarvis gives the exact command:
#   curl -fsSL https://<link>/agent/setup.sh | bash -s <link> <pairing code>
set -euo pipefail
HOST="${1:-}"
CODE="${2:-}"
case "$HOST" in
  *.trycloudflare.com) ;;
  *) echo "That isn't a Jarvis link. Ask Jarvis for the phone setup command again."; exit 1 ;;
esac
[[ "$CODE" =~ ^[0-9]{8}$ ]] || { echo "The pairing code is 8 digits. Ask Jarvis for a new setup command."; exit 1; }

echo "1/4 Installing what Jarvis needs (this takes a few minutes)..."
yes | pkg update -y >/dev/null 2>&1 || true
pkg install -y python termux-api android-tools >/dev/null

echo "2/4 Getting Jarvis's phone helper..."
mkdir -p "$HOME/.jarvis" "$HOME/.termux/boot"
curl -fsSL "https://$HOST/agent/jarvis_agent.py" -o "$HOME/.jarvis/jarvis_agent.py.new"
mv "$HOME/.jarvis/jarvis_agent.py.new" "$HOME/.jarvis/jarvis_agent.py"

echo "3/4 Pairing with Jarvis..."
python "$HOME/.jarvis/jarvis_agent.py" pair "$HOST" "$CODE" "$(getprop ro.product.model 2>/dev/null || echo android)"

cat > "$HOME/.termux/boot/jarvis.sh" <<'BOOT'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock 2>/dev/null || true
pgrep -f jarvis_agent.py >/dev/null || nohup python "$HOME/.jarvis/jarvis_agent.py" >> "$HOME/.jarvis/agent.log" 2>&1 &
BOOT
chmod 700 "$HOME/.termux/boot/jarvis.sh"
cat > "$PREFIX/bin/jarvis-link" <<'LINK'
#!/data/data/com.termux/files/usr/bin/bash
# jarvis-link <new link>: after the laptop restarts, point the helper at Jarvis's new link.
set -e
host="${1#https://}"; host="${host%%/*}"
pkill -f jarvis_agent.py 2>/dev/null || true
python "$HOME/.jarvis/jarvis_agent.py" host "$host"
bash "$HOME/.termux/boot/jarvis.sh"
LINK
chmod 700 "$PREFIX/bin/jarvis-link"

echo "4/4 Asking Android for permission (tap Allow on each pop-up)..."
termux-sms-list -l 1 >/dev/null 2>&1 || true
termux-contact-list >/dev/null 2>&1 || true
termux-telephony-deviceinfo >/dev/null 2>&1 || true

pkill -f jarvis_agent.py 2>/dev/null || true
bash "$HOME/.termux/boot/jarvis.sh"
echo "Done. Jarvis can now use this phone. To let him tap and type too, tell him: pair wireless debugging."
