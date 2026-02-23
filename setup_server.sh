#!/bin/bash
# setup_server.sh - Automated setup for Crypto Volume Screener on Ubuntu 22.04
echo "--- STARTING SERVER SETUP ---"
apt update && apt upgrade -y
apt install -y python3-pip python3-venv nginx git ufw

ufw allow 'Nginx Full'
ufw allow OpenSSH
ufw --force enable

mkdir -p /var/www/crypto-screener
chown -R www-data:www-data /var/www/crypto-screener
echo "--- SYSTEM DEPENDENCIES INSTALLED ---"
