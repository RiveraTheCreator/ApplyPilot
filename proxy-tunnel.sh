#!/usr/bin/env bash
# Túneles SSH locales hacia los Squid de los droplets (vía Tailscale).
#   proxy-tunnel.sh start   -> levanta localhost:13128 (droplet2) y localhost:13129 (droplet1)
#   proxy-tunnel.sh stop    -> baja ambos túneles
#   proxy-tunnel.sh status  -> muestra si están arriba
# La contraseña del proxy de egress del runtime rota: se extrae fresca de $https_proxy
# en cada invocación. Nunca se guarda ninguna contraseña en este script.
#
# PROPUESTA PARA EL CRON DIARIO (empleo-scrapeo-diario): antes de run_daily.py,
# correr `proxy-tunnel.sh start` (idempotente: no duplica túneles ya activos) y
# `proxy-tunnel.sh status` para verificar. El scraper ya lee proxy-config.json
# (localhost:13128 y :13129 con rotación) automáticamente. Al terminar la corrida
# los túneles pueden quedarse arriba o bajarse con `proxy-tunnel.sh stop`.
set -euo pipefail
KEY="/home/hatch/.ssh/id_ed25519"
is_up() { ss -tln 2>/dev/null | grep -q ":$1 "; }
mkcfg() {
  local pass cfg
  cfg="$1"
  pass=$(printf '%s' "$https_proxy" | sed -n 's|^http://[^:]*:\([^@]*\)@.*|\1|p')
  cat > "$cfg" <<EOF
Host d1
  HostName 100.107.188.5
  User kavi
  IdentityFile $KEY
  IdentitiesOnly yes
  BatchMode yes
  ConnectTimeout 30
  StrictHostKeyChecking accept-new
  ProxyCommand socat - PROXY:hatch-egress-proxy:%h:%p,proxyport=3130,proxyauth=hatch-runtime:$pass
Host d2
  HostName 100.89.205.9
  User kavi
  IdentityFile $KEY
  IdentitiesOnly yes
  BatchMode yes
  ConnectTimeout 30
  StrictHostKeyChecking accept-new
  ProxyCommand socat - PROXY:hatch-egress-proxy:%h:%p,proxyport=3130,proxyauth=hatch-runtime:$pass
EOF
  chmod 600 "$cfg"
}
cmd="${1:-status}"
case "$cmd" in
  start)
    CFG=$(mktemp /tmp/sshcfg-tunnel.XXXXXX)
    mkcfg "$CFG"
    if is_up 13128; then echo "túnel d2 (13128) ya estaba arriba"; else ssh -F "$CFG" -f -N -L 13128:localhost:3128 d2 && echo "túnel d2 (13128) levantado"; fi
    if is_up 13129; then echo "túnel d1 (13129) ya estaba arriba"; else ssh -F "$CFG" -f -N -L 13129:localhost:3128 d1 && echo "túnel d1 (13129) levantado"; fi
    sleep 2
    rm -f "$CFG"
    "$0" status
    ;;
  stop)
    pkill -f '13128:localhost:3128' || true
    pkill -f '13129:localhost:3128' || true
    sleep 1
    "$0" status
    ;;
  status)
    for p in 13128 13129; do
      if is_up "$p"; then echo "$p: UP"; else echo "$p: DOWN"; fi
    done
    ;;
  *)
    echo "uso: $0 start|stop|status" >&2; exit 1;;
esac
