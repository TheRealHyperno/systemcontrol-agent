#!/usr/bin/env bash
# Builds the distributable: target/systemc-java-linux-x64.tar.gz (+ .sha256)
set -euo pipefail
cd "$(dirname "$0")/.."     # project root

mvn -q package -DskipTests
VER="$(sed -n 's/[[:space:]]*<version>\(.*\)<\/version>/\1/p' pom.xml | head -n1 | tr -d ' ')"
STAGE="$(mktemp -d)/systemc-java/bin"
trap 'rm -rf "$(dirname "$STAGE")"' EXIT

ROOT="$STAGE/.."; LIB="$ROOT/lib"
mkdir -p "$LIB" "$STAGE" "$ROOT/share"
cp target/systemc-java.jar "$LIB/systemc-java-$VER.jar"
cp dist/systemc "$STAGE/systemc"
cp src/main/resources/favicon.png "$ROOT/share/systemc.png"
cp src/main/resources/logo.png "$ROOT/share/logo.png"
ln -f "$LIB/systemc-java-$VER.jar" "$LIB/systemc-java.jar"
cp README.md example.env LICENSE* "$ROOT/" 2>/dev/null || true
cp dist/uninstall.sh "$ROOT/uninstall.sh"

OUT="target/systemc-java-linux-x64.tar.gz"
tar -czf "$OUT" -C "$(dirname "$STAGE")/.." systemc-java
( cd target && sha256sum systemc-java-linux-x64.tar.gz > systemc-java-linux-x64.tar.gz.sha256 )
echo "built: $OUT"
ls -la "$OUT" "$OUT.sha256"
