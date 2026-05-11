#!/usr/bin/env bash
# Bootstrap script for the test runner node.
# Installs system packages, kubectl, helm, and then runs the
# init-packages and build-wrk2 steps from setup_social_network.sh.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

mazu_echo() {
    echo -e "\e[1;30;44mMazu:\e[0m $*"
}

# ---------- system packages ----------
mazu_echo "Installing system packages..."
sudo apt-get update
sudo apt-get install -y python3-pip build-essential
sudo apt-get install -y python3-matplotlib

# ---------- gnuplot ----------
sudo apt-get install -y gnuplot

# ---------- kubectl ----------
if command -v kubectl &>/dev/null; then
    mazu_echo "kubectl already installed: $(kubectl version --client --short 2>/dev/null || kubectl version --client)"
else
    mazu_echo "Installing kubectl..."
    curl -fsSL "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
        -o /tmp/kubectl
    sudo install -o root -g root -m 0755 /tmp/kubectl /usr/local/bin/kubectl
    rm -f /tmp/kubectl
    mazu_echo "kubectl installed: $(kubectl version --client)"
fi

# ---------- helm ----------
if command -v helm &>/dev/null; then
    mazu_echo "helm already installed: $(helm version --short)"
else
    mazu_echo "Installing helm..."
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
    mazu_echo "helm installed: $(helm version --short)"
fi

# ---------- kubectl config ----------
if kubectl cluster-info &>/dev/null; then
    mazu_echo "kubectl can reach the cluster"
else
    mazu_echo "WARNING: kubectl cannot reach a cluster. Make sure ~/.kube/config" \
              "is set with the external IP of the k8s control-plane node."
fi

# ---------- init-packages & build-wrk2 ----------
mazu_echo "Running init-packages..."
"$SCRIPT_DIR/setup_social_network.sh" init-packages

mazu_echo "Running build-wrk2..."
"$SCRIPT_DIR/setup_social_network.sh" build-wrk2

mazu_echo "Bootstrap complete"
