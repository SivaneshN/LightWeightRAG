#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE_DIR="$ROOT/desktop/src-tauri/target/release/bundle"
APP_PATH="$BUNDLE_DIR/macos/LightWeightRAG.app"

if [[ ! -d "$APP_PATH" ]]; then
  echo "The LightWeightRAG app bundle was not found at $APP_PATH" >&2
  echo "Build it first with: cargo tauri build --bundles app --no-sign" >&2
  exit 1
fi

if [[ $# -gt 0 ]]; then
  ARTIFACT_NAME="$1"
else
  case "$(uname -m)" in
    arm64) ARTIFACT_NAME="macos-arm64" ;;
    x86_64) ARTIFACT_NAME="macos-x64" ;;
    *) echo "Unsupported Mac architecture: $(uname -m)" >&2; exit 1 ;;
  esac
fi

if [[ ! "$ARTIFACT_NAME" =~ ^[a-z0-9-]+$ ]]; then
  echo "Use a lowercase artifact name containing only letters, numbers, and hyphens." >&2
  exit 1
fi

STAGING_DIR="$(mktemp -d "${TMPDIR:-/tmp}/lightweightrag-dmg.XXXXXX")"
trap 'rm -rf "$STAGING_DIR"' EXIT

ditto --rsrc --extattr "$APP_PATH" "$STAGING_DIR/LightWeightRAG.app"
ln -s /Applications "$STAGING_DIR/Applications"

DMG_DIR="$BUNDLE_DIR/dmg"
DMG_PATH="$DMG_DIR/LightWeightRAG-${ARTIFACT_NAME}.dmg"
mkdir -p "$DMG_DIR"
hdiutil create \
  -volname "LightWeightRAG" \
  -srcfolder "$STAGING_DIR" \
  -ov \
  -format UDZO \
  "$DMG_PATH"
hdiutil verify "$DMG_PATH"

echo "Created $DMG_PATH"
