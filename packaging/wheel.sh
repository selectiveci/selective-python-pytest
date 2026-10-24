#!/usr/bin/env bash
set -euo pipefail
binary=${1:?usage: packaging/wheel.sh <transport binary> <platform tag> [out dir]}
tag=${2:?usage: packaging/wheel.sh <transport binary> <platform tag> [out dir]}
out=$(realpath -m "${3:-dist}")
root=$(cd "$(dirname "$0")/.." && pwd)
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
git -C "$root" archive HEAD | tar -x -C "$stage"
install -D -m 755 "$binary" "$stage/src/selective_pytest/bin/transport"
python -m pip wheel --quiet --no-deps --wheel-dir "$stage/dist" "$stage"
python -m wheel tags --remove --platform-tag "$tag" "$stage"/dist/*.whl
mkdir -p "$out"
cp "$stage"/dist/*.whl "$out"/
ls "$out"/*"$tag".whl
