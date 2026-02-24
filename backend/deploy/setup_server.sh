#!/bin/bash
# setup_server.sh - "Perfect" Haasle-Free Setup for AWS Ubuntu
echo "--- STARTING PERFECT SERVER SETUP ---"

# 1. Update and install dependencies
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv nginx git ufw

# 2. Setup Security (UFW)
sudo ufw allow 'Nginx Full'
sudo ufw allow OpenSSH
sudo ufw --force enable

# 3. Memory Optimization (Swap Space)
echo "--- OPTIMIZING MEMORY (SWAP SPACE) ---"
if [ ! -f /swapfile ]; then
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

# 4. Standard Directory & Permissions
sudo mkdir -p /var/www/crypto-screener
sudo chown -R ubuntu:ubuntu /var/www/crypto-screener
sudo chmod -R 755 /var/www/crypto-screener

echo "--- SYSTEM READY FOR CODE DEPLOYMENT ---"
