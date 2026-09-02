#!/usr/bin/env bash
# Clone (or re-point) the StreamController checkout used by this dev container.
#
#   fetch-streamcontroller [REPO_URL] [REF] [DEST]
#
# REF may be a branch, a tag, or a full commit sha. Defaults: upstream repo, `main`,
# /workspaces/StreamController. Safe to re-run inside the container to switch refs, e.g.
#   fetch-streamcontroller "" 1.5.0-beta.15 && uv pip install -r /workspaces/StreamController/requirements.txt
# DEST may already exist and be non-empty (the image pre-creates data/ inside it); untracked
# files there (data/, run_dev.sh) are left alone - only git state is touched.
set -euo pipefail

REPO="${1:-}"
REF="${2:-main}"
DEST="${3:-/workspaces/StreamController}"
[ -z "$REPO" ] && REPO="https://github.com/StreamController/StreamController.git"

mkdir -p "$DEST"
if [ ! -d "$DEST/.git" ]; then
    echo "Initialising $DEST from $REPO"
    git -C "$DEST" init -q
    git -C "$DEST" remote add origin "$REPO"
fi

echo "Fetching $REF"
# Shallow fetch works for branches, tags and (on GitHub) full commit shas alike.
if ! git -C "$DEST" fetch --depth 1 origin "$REF"; then
    echo "Shallow fetch of '$REF' failed; fetching everything"
    git -C "$DEST" fetch origin
    git -C "$DEST" checkout --detach "$REF"
else
    git -C "$DEST" checkout --detach FETCH_HEAD
fi

echo "StreamController is now at: $(git -C "$DEST" log -1 --format='%h %s')"
