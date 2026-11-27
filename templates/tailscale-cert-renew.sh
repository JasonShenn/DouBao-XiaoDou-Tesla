#!/bin/bash
# Tailscale 证书续期脚本（Funnel 路线用）
# 证书 90 天有效；tailscale cert 只在快到期时才重新领证，没到时间不会重复领，
# 所以每天跑一次即可。证书文件变了才替换并重启收流服务，没变不重启。
# 用法：chmod +x templates/tailscale-cert-renew.sh，cron 里加一行：
#   0 3 * * * /home/<SERVER_USER>/fleet-telemetry-<APP_NAME>/tailscale-cert-renew.sh
set -eu
DOMAIN="<TELEMETRY_DOMAIN>"
CERT_DIR="/etc/tailscale-certs"
TMP_CRT="$(mktemp)"
TMP_KEY="$(mktemp)"
trap 'rm -f "$TMP_CRT" "$TMP_KEY"' EXIT
tailscale cert --cert-file "$TMP_CRT" --key-file "$TMP_KEY" "$DOMAIN" >/dev/null 2>&1
if ! cmp -s "$TMP_CRT" "$CERT_DIR/$DOMAIN.crt"; then
  cp "$TMP_CRT" "$CERT_DIR/$DOMAIN.crt"
  cp "$TMP_KEY" "$CERT_DIR/$DOMAIN.key"
  systemctl restart "fleet-telemetry-<APP_NAME>"
fi
