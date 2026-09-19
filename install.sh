#!/usr/bin/env bash
# Install xsess.py from this checkout as ~/.local/bin/xsess.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$here/xsess.py" ] || { echo "xsess.py not found next to install.sh" >&2; exit 1; }

command -v python3 >/dev/null \
  || { echo "warning: python3 (>= 3.10) not found on PATH" >&2; }

mkdir -p "$HOME/.local/bin"
chmod +x "$here/xsess.py"
ln -sf "$here/xsess.py" "$HOME/.local/bin/xsess"

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "note: add ~/.local/bin to your PATH, e.g. in ~/.bashrc:"
     echo '  export PATH="$HOME/.local/bin:$PATH"' ;;
esac

echo "installed: $HOME/.local/bin/xsess -> $here/xsess.py"
echo "first run builds the index automatically; try: xsess list -n 10"
