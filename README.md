# BackupD - Service de Backup pour Proxmox VE

BackupD est un service daemon léger écrit en Python utilisant FastAPI pour gérer les backups automatiques des machines virtuelles (VM) et conteneurs (LXC) sur un cluster Proxmox VE. Il fournit une API REST sécurisée pour déclencher des backups à distance, avec authentification HMAC, protection contre les attaques par rejeu, et gestion des quotas de backups.

## Fonctionnalités

- **API REST sécurisée** : Authentification basée sur des clés HMAC avec protection contre les attaques par rejeu (nonces) et timestamps.
- **Gestion des backups** : Création, liste et suppression automatique des anciens backups selon des quotas configurables.
- **Support Proxmox** : Intégration native avec les commandes `pvesm` et `qm` pour gérer les storages et les VMs/LXCs.
- **Logging structuré** : Logs détaillés pour le débogage et la surveillance.
- **Service système** : Installation en tant que service systemd pour un démarrage automatique.
- **Configuration flexible** : Paramètres par défaut et overrides par VM.

## Prérequis

- Proxmox VE installé et configuré.
- Python 3.8+ avec pip.
- Accès root pour l'installation du service.

## Installation

1. **Cloner ou copier les fichiers** dans `/opt/backupd` :
   ```
   sudo mkdir -p /opt/backupd
   sudo cp -r . /opt/backupd/
   ```

2. **Installer les dépendances Python :**
   ```
   cd /opt/backupd
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Générer les clés :**
   Utilisez le script `bin/keygen.sh` pour générer des clés pour chaque VM :
   ```
   ./bin/keygen.sh <vmid>
   ```
   Cela met à jour `config/keys.json`.

4. **Configurer la rotation des clés :**
   Utilisez le script `/opt/backupd/bin/keycheck.sh` pour vérifier et renouveler régulièrement les clés :
   ```
   /opt/backupd/bin/keycheck.sh <retention>
   ```
   La rétention correspond au nombre de jours maximal de la clé.
   Elle sera automatiquement renouvelée une fois cette ancienneté dépassée via une tâche cron

5. **Configurer le service systemd :**
   Copiez le fichier de service :
   ```
   sudo cp etc/systemd/system/backupd.service /etc/systemd/system/
   sudo systemctl daemon-reload
   ```

6. **Configurez l'IP:port de votre API :**
    Dans mon cas, j'ai ajouté une IP sur le vmbr1 (réseau Proxmox local 192.168.254.0/24) pour que l'API soit accessible depuis 192.168.254.253:8080
    Votre IP:port est à configurer dans le `/etc/systemd/system/backupd.service` et dans le `/srv/script/backupd/backupctl`

7. **Démarrer le service :**
   ```
   sudo systemctl enable backupd
   sudo systemctl start backupd
   ```

8. **Vérifier son état :**
    ```
   journalctl -u backupd -ef
    ```

## Configuration

### config.json
Définit les paramètres par défaut et les overrides par VMID :

```json
{
  "defaults": {
    "max_backups": 3,
    "total_backups_size": 0,
    "cooldown": 1
  },
  "overrides": {
    "100": {
      "max_backups": 3,
      "total_backups_size": 10000
    }
  }
}
```

- `max_backups` : Nombre maximum de backups à conserver (0 = illimité). La création est refusée si la limite serait dépassée.
- `total_backups_size` : Taille totale maximale des backups en MB (0 = illimité). La création est refusée si la limite serait dépassée.
- `cooldown` : Délai minimum en secondes entre deux backups pour la même VM.
- `allowed_networks` : Liste des CIDR autorisés pour accéder à l'API depuis cette VM. Si vide ou absent, aucune restriction réseau n'est appliquée. (simple IP (192.168.254.100) ou CIDR complet (192.168.254.0/24))
- `overrides` : Permet de modifier les valeurs par défaut de certaines VM
    `id`: {
        `valeur`: nouvelle_valeur
    }


### keys.json
Contient les clés HMAC pour chaque VM. Généré par `keygen.sh`.

## Utilisation

### API Endpoints

#### Lister les backups
```
GET /backups
Headers:
  X-Key: <clé>
  Timestamp: <timestamp_unix>
```

#### Voir les tâches en cours
```
GET /backups/status
Headers:
  X-Key: <clé>
  Timestamp: <timestamp_unix>
```

#### Créer une backup
```
POST /backups
Headers:
  X-Key: <clé>
Content-Type: application/json

{
  "timestamp": <timestamp_unix>,
  "nonce": "<nonce_unique>",
  "signature": "<signature_hmac>",
}
```

#### Restaurer une backup
```
POST /backups/{fichier}/restore
Headers:
  X-Key: <clé>
Content-Type: application/json

{
  "timestamp": <timestamp_unix>,
  "nonce": "<nonce_unique>",
  "signature": "<signature_hmac>"
}
```

#### Supprimer une backup
```
DELETE /backups/{fichier}
Headers:
  X-Key: <clé>
Content-Type: application/json

{
  "timestamp": <timestamp_unix>,
  "nonce": "<nonce_unique>",
  "signature": "<signature_hmac>"
}
```

### Authentification
- Utilisez la clé générée pour la VM.
- Le timestamp doit être récent (dans les 30 secondes).
- Le nonce doit être unique et non réutilisé.
- La signature HMAC est calculée sur le payload JSON.

### Script client
Le script `backupctl` se trouve dans `/srv/scripts/backupd/backupctl` sur l'hôte. Il doit être rendu disponible dans la VM/CT (par exemple via un montage dans `/mnt/scripts`) et la clé d'accès doit être placée dans `/etc/backupctl/credentials`.

Exemple d'utilisation depuis la VM :
```
backupctl list
backupctl status
backupctl create
backupctl delete
backupctl restore
```

### Logs
Les logs sont stockés dans `/var/log/backupd/`. Consultez-les pour le débogage.
```
tail -f /var/log/backupd/api.log
tail -f /var/log/backupd/worker.log
tail -f /var/log/backupd/keys.log
```

## Architecture

- **main.py** : API FastAPI principale.
- **worker.py** : Traitement asynchrone des jobs de backup.
- **security.py** : Fonctions d'authentification et sécurité.
- **state.py** : Gestion de l'état persistant des backups.
- **logger.py** : Configuration du logging.

## Sécurité

- Authentification HMAC pour chaque requête.
- Protection contre les attaques par rejeu via nonces.
- Validation des timestamps pour éviter les attaques temporelles.
- Cooldown configurable pour limiter la fréquence des backups.

### Gestion des clés
- Les clés sont générées avec `keygen.sh` et stockées dans `config/keys.json`.
- Utilisez `keycheck.sh` pour vérifier l'expiration et renouveler automatiquement les clés.