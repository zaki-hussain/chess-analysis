#!/usr/bin/env bash
# Prepare a fresh Ubuntu EC2 instance to run `chess-analysis analyse`.
#
# Usage, from the repo folder copied onto the instance:
#   bash scripts/ec2-setup.sh
# then:
#   nohup bash scripts/ec2-run.sh > analyse.log 2>&1 &   # analyse, then power off
#   tail -f analyse.log
#
# Installs Python, picks the fastest Stockfish 17.1 build the CPU supports,
# and creates the project's virtualenv. Idempotent: safe to re-run.
set -euo pipefail

cd "$(dirname "$0")/.."

SF_VERSION="sf_17.1"
if grep -q avx512_vnni /proc/cpuinfo; then
    SF_BUILD="x86-64-vnni512"      # AMD Genoa (c7a) and recent Intel
elif grep -q avx2 /proc/cpuinfo; then
    SF_BUILD="x86-64-avx2"
else
    SF_BUILD="x86-64"
fi

echo "== Installing system packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip curl > /dev/null

if ! command -v stockfish > /dev/null; then
    echo "== Installing Stockfish ${SF_VERSION} (${SF_BUILD})"
    tmp="$(mktemp -d)"
    curl -sSL -o "$tmp/sf.tar" \
        "https://github.com/official-stockfish/Stockfish/releases/download/${SF_VERSION}/stockfish-ubuntu-${SF_BUILD}.tar"
    tar -xf "$tmp/sf.tar" -C "$tmp"
    sudo install -m 755 "$(find "$tmp" -type f -name "stockfish-ubuntu-${SF_BUILD}")" /usr/local/bin/stockfish
    rm -rf "$tmp"
fi
echo "uci" | stockfish | grep "^id name"

echo "== Creating virtualenv"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -e .

echo "== Ready. $(nproc) CPUs. Single-core speed:"
stockfish bench 16 1 13 2>&1 | grep "Nodes/second"
echo
echo "Run:   nohup bash scripts/ec2-run.sh > analyse.log 2>&1 &   (analyse, then power off)"
echo "Watch: tail -f analyse.log"
