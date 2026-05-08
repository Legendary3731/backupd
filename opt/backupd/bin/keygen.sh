#!/bin/bash
set -euo pipefail

apt-get update -qq
apt-get install -y openssl jq

#####################################################################
# CONFIG
#####################################################################

CONFIG_DIR="/opt/backupd/config"
KEYS_FILE="$CONFIG_DIR/keys.json"
SNIPPET_DIR="/var/lib/vz/snippets"

mkdir -p "$CONFIG_DIR" "$SNIPPET_DIR" /var/log/backupd
chmod 700 "$SNIPPET_DIR"

#####################################################################
# UTILS
#####################################################################

die()     { echo "❌ $1" >&2; exit 1; }
info()    { echo "▶ $1"; }
success() { echo "✅ $1"; }
warn()    { echo "⚠️  $1"; }

#####################################################################
# ID EXPANSION: 100-105,107,109-111
#####################################################################

expand_ids() {
  local input="${1//[[:space:]]/}"
  local out=()

  IFS=',' read -ra parts <<< "$input"

  for part in "${parts[@]}"; do
    if [[ "$part" =~ ^[0-9]+$ ]]; then
      out+=("$part")

    elif [[ "$part" =~ ^([0-9]+)-([0-9]+)$ ]]; then
      local start="${BASH_REMATCH[1]}"
      local end="${BASH_REMATCH[2]}"

      (( start <= end )) || die "Plage invalide : $part"

      for ((i=start; i<=end; i++)); do
        out+=("$i")
      done
    else
      die "Syntaxe invalide : $part"
    fi
  done

  printf "%s\n" "${out[@]}" | sort -n -u
}

#####################################################################
# NEXT FREE MP SLOT
#####################################################################

next_mp_slot() {
  local vmid="$1"

  for i in {0..255}; do
    if ! pct config "$vmid" | grep -q "^mp$i:"; then
      echo "$i"
      return
    fi
  done

  return 1
}

#####################################################################
# VALIDATION ARGUMENT
#####################################################################

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <vmid_expr>"
  echo "Exemples :"
  echo "  $0 100"
  echo "  $0 100-105"
  echo "  $0 100-105,107,109-111"
  exit 1
fi

VMIDS=$(expand_ids "$1")

#####################################################################
# INITIALISATION keys.json
#####################################################################

[ -f "$KEYS_FILE" ] || echo "{}" > "$KEYS_FILE"
chmod 600 "$KEYS_FILE"

#####################################################################
# MAIN LOOP
#####################################################################

for VMID in $VMIDS; do
  info "Traitement VMID $VMID"

  ###################################################################
  # Détection CT / VM
  ###################################################################

  if pct config "$VMID" &>/dev/null; then
    TYPE="CT"

  elif qm config "$VMID" &>/dev/null; then
    TYPE="VM"

  else
    warn "VMID $VMID introuvable, ignoré"
    continue
  fi

  ###################################################################
  # Génération + enregistrement clé
  ###################################################################

  KEY="backup_$(openssl rand -hex 128)"
  GENERATED_AT=$(date "+%Y-%m-%d %H:%M:%S")

  tmp=$(mktemp)

  jq \
    --arg vmid "$VMID" \
    --arg key "$KEY" \
    --arg date "$GENERATED_AT" \
    '.[$vmid] = {key: $key, generated_at: $date}' \
    "$KEYS_FILE" > "$tmp"

  mv "$tmp" "$KEYS_FILE"
  chmod 600 "$KEYS_FILE"

  success "Clé générée pour $TYPE $VMID"

  ###################################################################
  # DÉPLOIEMENT CT
  ###################################################################

  if [ "$TYPE" = "CT" ]; then
    info "Déploiement automatique dans CT $VMID"

    CT_STATUS=$(pct status "$VMID" | awk '{print $2}')
    STARTED_BY_US=false

    if [ "$CT_STATUS" != "running" ]; then
      warn "CT $VMID est $CT_STATUS — démarrage temporaire"

      pct start "$VMID"

      for i in {1..15}; do
        sleep 2

        if pct exec "$VMID" -- true >/dev/null 2>&1; then
          break
        fi

        if [ "$i" -eq 15 ]; then
          die "CT $VMID ne répond pas après démarrage"
        fi

        info "Attente CT $VMID... ($i/15)"
      done

      STARTED_BY_US=true
    fi

    #################################################################
    # Mount readonly
    #################################################################

    if ! pct config "$VMID" | grep -q '/mnt/scripts'; then
      MP_SLOT=$(next_mp_slot "$VMID") \
        || die "Aucun slot mp libre pour CT $VMID"

      info "Ajout bind mount mp$MP_SLOT"

      pct set "$VMID" \
        -mp"$MP_SLOT" /srv/scripts,mp=/mnt/scripts,ro=1
    else
      info "/mnt/scripts déjà configuré"
    fi

    #################################################################
    # Déploiement interne
    #################################################################

    pct exec "$VMID" -- bash <<EOF
set -e

mkdir -p /etc/backupctl
chmod 700 /etc/backupctl

cat > /etc/backupctl/credentials <<EOKEY
XKEY=$KEY
EOKEY

chmod 600 /etc/backupctl/credentials
chown root:root /etc/backupctl/credentials

grep -q '/mnt/scripts' /etc/bash.bashrc || \
echo 'export PATH="/mnt/scripts:\$PATH"' >> /etc/bash.bashrc
EOF

    #################################################################
    # Restore state
    #################################################################

    if [ "$STARTED_BY_US" = "true" ]; then
      info "Arrêt CT $VMID (état initial restauré)"
      pct stop "$VMID"
    fi

    echo "$GENERATED_AT | vmid=$VMID type=$TYPE action=new_key" \
      >> /var/log/backupd/keys.log

    success "CT $VMID configurée"

  ###################################################################
  # DÉPLOIEMENT VM
  ###################################################################

  else
    info "Préparation cloud-init pour VM $VMID"

    #################################################################
    # VirtioFS readonly
    #################################################################

    if ! qm config "$VMID" | grep -q 'virtiofs'; then
      info "Ajout VirtioFS readonly"

      qm set "$VMID" -virtiofs0 /srv/scripts
    else
      info "VirtioFS déjà configuré"
    fi

    #################################################################
    # Cloud-init
    #################################################################

    SNIPPET="$SNIPPET_DIR/backupctl-$VMID.yml"

    cat > "$SNIPPET" <<EOF
#cloud-config

package_update: true

packages:
  - qemu-guest-agent

runcmd:
  - mkdir -p /etc/backupctl
  - chmod 700 /etc/backupctl

  - |
      cat > /etc/backupctl/credentials <<EOKEY
      XKEY=$KEY
      EOKEY

  - chmod 600 /etc/backupctl/credentials
  - chown root:root /etc/backupctl/credentials

  - mkdir -p /mnt/scripts

  - |
      grep -q '^scripts /mnt/scripts virtiofs' /etc/fstab || \
      echo 'scripts /mnt/scripts virtiofs ro 0 0' >> /etc/fstab

  - modprobe virtiofs || true

  - mount -a

  - |
      grep -q '/mnt/scripts' /etc/bash.bashrc || \
      echo 'export PATH="/mnt/scripts:\$PATH"' >> /etc/bash.bashrc
EOF

    chmod 600 "$SNIPPET"

    qm set "$VMID" \
      --cicustom "user=local:snippets/backupctl-$VMID.yml"

    #################################################################
    # Reboot VM
    #################################################################

    info "Redémarrage VM $VMID"

    qm reboot "$VMID" || {
      warn "qm reboot échoué, fallback stop/start"

      qm stop "$VMID"
      qm start "$VMID"
    }

    echo "$GENERATED_AT | vmid=$VMID type=$TYPE action=new_key" \
      >> /var/log/backupd/keys.log

    success "VM $VMID configurée"
  fi
done

echo
success "Déploiement terminé pour tous les VMID"