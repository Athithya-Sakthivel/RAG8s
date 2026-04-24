#!/usr/bin/env bash
set -eux

# Raise kernel-wide max
sysctl -w fs.file-max=2097152

# Persist
echo "fs.file-max=2097152" >> /etc/sysctl.conf

# Raise systemd defaults
mkdir -p /etc/systemd/system.conf.d
cat <<EOF >/etc/systemd/system.conf.d/limits.conf
[Manager]
DefaultLimitNOFILE=262144
EOF

systemctl daemon-reexec
