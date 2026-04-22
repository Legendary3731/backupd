#!/bin/bash
set -euo pipefail

apt install -y openssl jq

#####################################################################
# CONFIG
#####################################################################

CONFIG_DIR="/opt/backupd/config"
KEYS_FILE="$CONFIG_DIR/keys.json"
CONFIG_FILE="$CONFIG_DIR/config.json"
STATE_DIR="/var/lib/backupd/state"
SETUP_SCRIPT="/opt/backupd/bin/keygen.sh"
CRON_FILE="/etc/cron.d/backupd-keycheck"
MAX_AGE_DAYS="${1:-30}"

#####################################################################
# UTILS
#####################################################################

die()     { echo "❌ $1" >&2; exit 1; }
info()    { echo "▶ $1"; }
success() { echo "✅ $1"; }
warn()    { echo "⚠️  $1"; }

#####################################################################
# CHECKS
#####################################################################

[ -f "$KEYS_FILE" ]   || die "keys.json introuvable : $KEYS_FILE"
[ -f "$CONFIG_FILE" ] || die "config.json introuvable : $CONFIG_FILE"

#####################################################################
# CRON
#####################################################################

setup_cron() {
  cat > "$CRON_FILE" <<EOF
# Vérification et rotation automatique des clés backupd
# Généré par keycheck.sh le $(date "+%Y-%m-%d %H:%M:%S")
0 3 * * * root /opt/backupd/keycheck.sh $MAX_AGE_DAYS >> /var/log/backupd/keycheck.log 2>&1
EOF

  chmod 644 "$CRON_FILE"

  if [ -f "$CRON_FILE" ]; then
    success "Cron configuré : keycheck tous les jours à 3h (rotation si > ${MAX_AGE_DAYS}j)"
  else
    die "Échec de la création du cron"
  fi
}

#####################################################################
# PURGE VMID (keys.json + config.json + state)
#####################################################################

purge_vmid() {
  local vmid="$1"

  # Suppression dans keys.json
  tmp=$(mktemp)
  jq --arg v "$vmid" 'del(.[$v])' "$KEYS_FILE" > "$tmp"
  mv "$tmp" "$KEYS_FILE"
  chmod 600 "$KEYS_FILE"
  info "VMID $vmid supprimé de keys.json"

  # Suppression dans config.json (overrides uniquement)
  if jq -e --arg v "$vmid" '.overrides[$v]' "$CONFIG_FILE" &>/dev/null; then
    tmp=$(mktemp)
    jq --arg v "$vmid" 'del(.overrides[$v])' "$CONFIG_FILE" > "$tmp"
    mv "$tmp" "$CONFIG_FILE"
    info "VMID $vmid supprimé des overrides de config.json"
  fi

  # Suppression du state
  local state_file="$STATE_DIR/$vmid.json"
  if [ -f "$state_file" ]; then
    rm -f "$state_file"
    info "VMID $vmid state supprimé ($state_file)"
  fi

  mkdir -p /var/log/backupd
  echo "$(date "+%Y-%m-%d %H:%M:%S") | vmid=$vmid action=purged reason=not_found_on_proxmox" \
    >> /var/log/backupd/keys.log
}

#####################################################################
# MAIN
#####################################################################

info "Vérification des clés (max age: ${MAX_AGE_DAYS}j)"
echo

TO_ROTATE=()
NOW=$(date +%s)

while IFS= read -r VMID; do

  KEY_DATA=$(jq -r --arg v "$VMID" '.[$v]' "$KEYS_FILE")
  GENERATED_AT=$(echo "$KEY_DATA" | jq -r '.generated_at // empty')

  # ---------------------------------------------------------------
  # CT/VM existe encore ?
  # ---------------------------------------------------------------
  if pct config "$VMID" &>/dev/null 2>&1; then
    TYPE="CT"
  elif qm config "$VMID" &>/dev/null 2>&1; then
    TYPE="VM"
  else
    warn "VMID $VMID introuvable sur Proxmox — purge et rotation ignorée"
    purge_vmid "$VMID"
    continue
  fi

  # ---------------------------------------------------------------
  # Clé trop ancienne ?
  # ---------------------------------------------------------------
  if [ -z "$GENERATED_AT" ]; then
    warn "VMID $VMID ($TYPE) — pas de date de génération, rotation requise"
    TO_ROTATE+=("$VMID")
    continue
  fi

  GEN_TS=$(date -d "$GENERATED_AT" +%s 2>/dev/null) || {
    warn "VMID $VMID ($TYPE) — date invalide ($GENERATED_AT), rotation requise"
    TO_ROTATE+=("$VMID")
    continue
  }

  AGE_DAYS=$(( (NOW - GEN_TS) / 86400 ))

  if (( AGE_DAYS >= MAX_AGE_DAYS )); then
    warn "VMID $VMID ($TYPE) — clé âgée de ${AGE_DAYS}j (max: ${MAX_AGE_DAYS}j), rotation requise"
    TO_ROTATE+=("$VMID")
  else
    success "VMID $VMID ($TYPE) — clé ok (${AGE_DAYS}j / ${MAX_AGE_DAYS}j)"
  fi

done < <(jq -r 'keys[]' "$KEYS_FILE")

#####################################################################
# ROTATION
#####################################################################

echo

if [ ${#TO_ROTATE[@]} -eq 0 ]; then
  success "Toutes les clés sont à jour, aucune rotation nécessaire"
else
  info "${#TO_ROTATE[@]} clé(s) à renouveler : ${TO_ROTATE[*]}"
  echo
  VMIDS_EXPR=$(IFS=','; echo "${TO_ROTATE[*]}")
  "$SETUP_SCRIPT" "$VMIDS_EXPR"
fi

#####################################################################
# CRON
#####################################################################

echo
if [ -f "$CRON_FILE" ]; then
  if grep -q "keycheck.sh $MAX_AGE_DAYS" "$CRON_FILE"; then
    success "Cron déjà configuré avec max_age=${MAX_AGE_DAYS}j, aucune modification"
  else
    info "Mise à jour du cron (nouvel intervalle: ${MAX_AGE_DAYS}j)"
    setup_cron
  fi
else
  info "Création du cron"
  setup_cron
fi