#!/bin/sh
# Install a precompiled Zed adapter. Only the adapter is managed here, not its native LSP.
set -eu

repo=lazysnail1024/zed-argocd
extension=argocd-lsp
version=0.0.1-alpha.2
assets_dir=
rollback=false

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
usage() {
  printf 'Usage: sh install.sh [--version VERSION] [--assets-dir DIR] [--rollback]\n'
  exit 0
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --version|--assets-dir)
      [ "$#" -ge 2 ] || die "$1 requires a value"
      if [ "$1" = --version ]; then version=$2; else assets_dir=$2; fi
      shift 2 ;;
    --rollback) rollback=true; shift ;;
    --help|-h) usage ;;
    *) die "Unknown option: $1" ;;
  esac
done
case "$version" in
  ''|*[!0-9A-Za-z.+-]*) die 'Invalid version' ;;
esac
case "$(uname -s)" in
  Darwin) installed="${HOME:?}/Library/Application Support/Zed/extensions/installed" ;;
  Linux) installed="${XDG_DATA_HOME:-${HOME:?}/.local/share}/zed/extensions/installed" ;;
  *) die 'Use install.ps1 on Windows; only Linux and macOS are supported here' ;;
esac

target="$installed/$extension"
state_dir="${installed%/installed}/.alpha-installer"
previous="$state_dir/$extension.previous"
mkdir -p "$installed"
[ ! -L "$state_dir" ] || die "Refusing symlinked installer state: $state_dir"
mkdir -p "$state_dir"
for path in "$target" "$previous"; do
  [ ! -L "$path" ] || die "Refusing to replace a symlink: $path"
  [ ! -e "$path" ] || [ -d "$path" ] || die "Expected an extension directory: $path"
done

# Recover an interrupted previous upgrade before starting another one.
if [ ! -e "$target" ] && [ -d "$previous" ]; then
  mv "$previous" "$target"
  printf 'Recovered previous installation at %s\n' "$target"
  [ "$rollback" = false ] || exit 0
fi

work=$(mktemp -d "$state_dir/.$extension.install.XXXXXXXX") || die 'Cannot create staging directory'
rotated_previous=false
rotated_current=false
activated=false
cleanup() {
  code=$1
  trap - 0 HUP INT TERM
  if [ "$code" -ne 0 ]; then
    restored=true
    if [ "$activated" = true ] && [ -d "$target" ]; then
      mv "$target" "$work/failed-new" || restored=false
    fi
    if [ "$rotated_current" = true ] && [ -d "$previous" ]; then
      mv "$previous" "$target" || restored=false
    fi
    if [ "$rotated_previous" = true ] && [ -d "$work/older" ]; then
      mv "$work/older" "$previous" || restored=false
    fi
    if [ "$restored" = false ]; then
      printf 'Recovery needs attention: preserved staging directory %s\n' "$work" >&2
      exit "$code"
    fi
  fi
  rm -rf "$work"
  exit "$code"
}
trap 'cleanup $?' 0
trap 'exit 1' 1 2 3 15

staged="$work/payload/$extension"
mkdir -p "$work/payload"
if [ "$rollback" = true ]; then
  [ -d "$previous" ] || die "No previous version to roll back to: $previous"
  cp -R "$previous" "$staged"
else
  archive="${repo##*/}-$version.zip"
  if [ -n "$assets_dir" ]; then
    [ -f "$assets_dir/$archive" ] && [ -f "$assets_dir/SHA256SUMS" ] ||
      die "Expected $archive and SHA256SUMS in $assets_dir"
    cp "$assets_dir/$archive" "$work/$archive"
    cp "$assets_dir/SHA256SUMS" "$work/SHA256SUMS"
  else
    command -v curl >/dev/null 2>&1 || die 'curl is required'
    base="https://github.com/$repo/releases/download/zed-v$version"
    curl -fLsS --retry 2 --connect-timeout 15 --max-time 120 "$base/SHA256SUMS" -o "$work/SHA256SUMS"
    curl -fLsS --retry 2 --connect-timeout 15 --max-time 120 "$base/$archive" -o "$work/$archive"
  fi
  expected=$(awk -v name="$archive" '$2 == name && NF == 2 { print $1 }' "$work/SHA256SUMS")
  [ "${#expected}" -eq 64 ] || die "Missing, duplicate or malformed checksum for $archive"
  case "$expected" in *[!0-9a-fA-F]*) die 'Malformed SHA-256 checksum' ;; esac
  if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "$work/$archive" | awk '{ print $1 }')
  elif command -v shasum >/dev/null 2>&1; then
    actual=$(shasum -a 256 "$work/$archive" | awk '{ print $1 }')
  else
    die 'sha256sum or shasum is required'
  fi
  [ "$(printf '%s' "$actual" | tr 'A-F' 'a-f')" = "$(printf '%s' "$expected" | tr 'A-F' 'a-f')" ] ||
    die "SHA-256 verification failed for $archive"
  command -v unzip >/dev/null 2>&1 || die 'unzip is required'
  unzip -Z1 "$work/$archive" > "$work/entries" || die 'Invalid release ZIP'
  [ -s "$work/entries" ] || die 'Empty release ZIP'
  while IFS= read -r entry; do
    case "$entry" in "$extension/"*) ;; *) die "Unexpected ZIP entry: $entry" ;; esac
    case "$entry" in *'/../'*|*'/./'*|*'/..'|*'/.'|*'//'*|*'\'*|*':'*) die "Unsafe ZIP entry: $entry" ;; esac
  done < "$work/entries"
  unzip -q "$work/$archive" -d "$work/payload" || die 'Cannot extract release ZIP'
  [ -f "$staged/extension.wasm" ] && [ -f "$staged/extension.toml" ] &&
    [ -f "$staged/MANIFEST.json" ] || die 'Release is missing required adapter files'
  [ -z "$(find "$staged" -type l -print)" ] || die 'Release contains symlinks'
  grep -Fqx "id = \"$extension\"" "$staged/extension.toml" || die 'Wrong extension ID'
  grep -Fqx "version = \"$version\"" "$staged/extension.toml" || die 'Wrong adapter version'
  grep -Fq "\"extensionId\": \"$extension\"" "$staged/MANIFEST.json" || die 'Wrong release identity'
  grep -Fq "\"version\": \"$version\"" "$staged/MANIFEST.json" || die 'Wrong release version'
fi

if [ -d "$target" ] && diff -qr "$staged" "$target" >/dev/null 2>&1; then
  printf 'Already installed: %s\n' "$target"
  exit 0
fi

# All untrusted inputs are checked before touching the active directory.
# Moves are on the same filesystem; on failure the previous state is restored.
if [ -d "$previous" ]; then
  mv "$previous" "$work/older"
  rotated_previous=true
fi
if [ -d "$target" ]; then
  mv "$target" "$previous"
  rotated_current=true
fi
mv "$staged" "$target"
activated=true
if [ "$rollback" = true ]; then
  printf 'Restored previous %s installation at %s\n' "$extension" "$target"
else
  printf 'Installed %s (%s) at %s\n' "$extension" "$version" "$target"
fi
printf 'Restart Zed to load the adapter. Roll back: sh install.sh --rollback\n'
