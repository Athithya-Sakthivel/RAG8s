#!/usr/bin/env bash
# echo 'export KUBECONFIG=$HOME/.kube/config' >> ~/.bashrc && source ~/.bashrc

# ---------- ensure we have sudo when not root ----------
if [ "$EUID" -ne 0 ]; then
    SUDO="sudo"
else
    SUDO=""
fi

# Define the intended node name (lowercase)
NODE_NAME="k3s-local"

# ============================================================================
# 1. TEARDOWN – completely remove any previous k3s
# ============================================================================
echo "==> Removing existing k3s installations (if any) ..."
$SUDO /usr/local/bin/k3s-uninstall.sh 2>/dev/null || true
$SUDO /usr/local/bin/k3s-agent-uninstall.sh 2>/dev/null || true
$SUDO rm -rf /var/lib/rancher/k3s
$SUDO rm -f /usr/local/bin/k3s /usr/local/bin/kubectl /usr/local/bin/crictl

# ============================================================================
# 2. INSTALL – fresh single-node k3s with a deterministic lowercase node name
#               and without Traefik
# ============================================================================
echo "==> Installing k3s ..."
curl -sfL https://get.k3s.io | $SUDO sh -s - --write-kubeconfig-mode 644 --node-name "$NODE_NAME" --disable traefik

# ============================================================================
# 3. PERSIST KUBECONFIG – copy to ~/.kube/config and set up shell integration
# ============================================================================
K3S_KUBECONFIG="/etc/rancher/k3s/k3s.yaml"
USER_KUBECONFIG="$HOME/.kube/config"

echo "==> Persisting kubeconfig for user '$USER' ..."
mkdir -p "$HOME/.kube"
$SUDO k3s kubectl config view --raw > "$USER_KUBECONFIG"
chmod 600 "$USER_KUBECONFIG"

# Add the KUBECONFIG export to the user's .bashrc if not already present
if ! grep -q "export KUBECONFIG=$USER_KUBECONFIG" "$HOME/.bashrc" 2>/dev/null; then
    echo "export KUBECONFIG=$USER_KUBECONFIG" >> "$HOME/.bashrc"
fi

# Also export it for the current session
export KUBECONFIG="$USER_KUBECONFIG"

# ============================================================================
# 4. WAIT – node becomes Ready
# ============================================================================
echo "==> Waiting for node '${NODE_NAME}' to register (this can take a minute) ..."

# Loop until the node appears, up to 5 minutes
TIMEOUT=300
INTERVAL=5
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
    if kubectl get node "$NODE_NAME" &>/dev/null; then
        echo "==> Node '${NODE_NAME}' is registered. Waiting for it to become Ready ..."
        kubectl wait --for=condition=Ready node/"$NODE_NAME" --timeout=120s
        break
    fi
    echo "   ... still waiting (${ELAPSED}s elapsed)"
    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
done

# Check if the node was found and is Ready
if ! kubectl get node "$NODE_NAME" &>/dev/null; then
    echo "ERROR: Node '${NODE_NAME}' did not appear after ${TIMEOUT}s."
    echo "Check the k3s service status with: sudo systemctl status k3s"
    echo "Check the k3s logs with: sudo journalctl -u k3s -f"
    exit 1
fi

kubectl label node k3s-local node-type=compute

sudo k3s kubectl config view --raw > ~/.kube/config
chmod 600 ~/.kube/config
export KUBECONFIG=$HOME/.kube/config

# ============================================================================
# 5. DONE
# ============================================================================
echo ""
echo "=================================="
echo "  k3s cluster is up and running"
echo "  Node name: ${NODE_NAME}"
echo "=================================="
echo ""
echo "Kubeconfig has been persisted to:"
echo "  $USER_KUBECONFIG"
echo ""
echo "From any new terminal you can immediately use kubectl."
echo "If you want to use kubectl in your current terminal, run:"
echo "  source ~/.bashrc"