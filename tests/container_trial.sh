#!/bin/sh
# Run from the repository root on a Docker host. Touches only freshly owned test resources.
set -eu
case ${1:-local} in
  local)
    arch=$(uname -m)
    case $arch in
      arm64|aarch64) platform=linux/arm64 ;;
      *) platform=linux/amd64 ;;
    esac
    image=library-reader-obsidian:test-local
    name=library-reader-obsidian-test-local
    ;;
  amd64)
    platform=linux/amd64
    image=library-reader-obsidian:test
    name=library-reader-obsidian-test
    ;;
  rollback)
    image=library-reader-obsidian:test
    name=library-reader-obsidian-test
    ;;
  *)
    echo "Usage: sh tests/container_trial.sh [local|amd64|rollback SCRATCH]" >&2
    exit 2
    ;;
esac
marker=lro-container-trial
docker info >/dev/null
if [ "${1:-}" = rollback ]; then
  scratch=$(cd "${2:?Supply the exact absolute scratch directory from the run.}" && pwd -P)
  case ${scratch##*/} in lro-trial.*) ;; *) exit 2 ;; esac
  [ "$(cat "$scratch/owner")" = "$marker" ] || exit 2
else
  if [ -n "$(docker container ls -a --filter "name=^/$name$" --format '{{.Names}}')" ] ||
     [ -n "$(docker image ls --filter "reference=$image" --format '{{.Tag}}')" ]; then
    echo "Test container or image name already exists." >&2
    exit 1
  fi
  scratch=$(mktemp -d "${TMPDIR:-/tmp}/lro-trial.XXXXXXXX")
  scratch=$(cd "$scratch" && pwd -P)
  printf '%s\n' "$marker" > "$scratch/owner"
  printf 'Trial scratch: %s\n' "$scratch"
fi
container_id=
image_id=
cleanup() {
  rc=$?
  trap - EXIT
  if [ -n "$container_id" ] && docker container inspect "$container_id" >/dev/null 2>&1; then
    if [ "$(docker inspect --format '{{index .Config.Labels "org.lro.suite"}}' "$container_id")" = container-trial ]; then
      state=$(docker inspect --format '{{.State.Running}}' "$container_id")
      if [ "$state" = true ]; then docker stop "$container_id" >/dev/null || rc=1; fi
      docker rm "$container_id" >/dev/null || rc=1
    else
      echo "Could not verify container ownership for cleanup." >&2
      rc=1
    fi
  fi
  if [ -n "$image_id" ] && docker image inspect "$image" >/dev/null 2>&1; then
    if [ "$(docker image inspect --format '{{.Id}}' "$image")" = "$image_id" ]; then
      docker image rm "$image" >/dev/null || rc=1
    else
      echo "Could not verify image ownership for cleanup." >&2
      rc=1
    fi
  fi
  if remaining_container=$(docker container ls -a --filter "name=^/$name$" --format '{{.Names}}') &&
     remaining_image=$(docker image ls --filter "reference=$image" --format '{{.Tag}}') &&
     [ -z "$remaining_container" ] && [ -z "$remaining_image" ]; then
    if [ -f "$scratch/owner" ] && [ "$(cat "$scratch/owner")" = "$marker" ]; then
      if [ -d "$scratch/fixture/Guides/Locked" ]; then
        chmod 0755 "$scratch/fixture/Guides/Locked" || rc=1
      fi
      if rm -r -- "$scratch"; then
        echo "PASS: owned container, image tag and scratch removed"
      else
        rc=1
      fi
    else
      echo "Could not verify scratch ownership for cleanup." >&2
      rc=1
    fi
  else
    echo "Retained owned scratch for rollback." >&2
    rc=1
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "${1:-}" = rollback ]; then
  [ ! -f "$scratch/container-id" ] || container_id=$(cat "$scratch/container-id")
  [ ! -f "$scratch/image-id" ] || image_id=$(cat "$scratch/image-id")
  exit 0
fi

docker build --platform "$platform" --tag "$image" --iidfile "$scratch/image-id" .
image_id=$(docker image inspect --format '{{.Id}}' "$image")
printf '%s\n' "$image_id" > "$scratch/image-id"
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 256m --cpus 1 --pids-limit 64 --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777 \
  "$image" python -B -m unittest discover -s tests -v

python3 -B tests/fixture.py create "$scratch"
python3 -B tests/fixture.py manifest "$scratch" > "$scratch/before.json"
chmod 000 "$scratch/fixture/Guides/Locked"
container_id=$(docker create --cidfile "$scratch/container-id" --name "$name" --label org.lro.suite=container-trial \
  --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 256m --cpus 1 --pids-limit 64 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777 \
  --log-driver json-file --log-opt max-size=1m --log-opt max-file=2 \
  --mount "type=bind,src=$scratch/fixture,dst=/library,readonly" \
  --mount "type=bind,src=$scratch/operator.json,dst=/etc/lro/operator.json,readonly" "$image")
docker start "$container_id" >/dev/null
docker inspect --format '{{.Config.User}} read_only={{.HostConfig.ReadonlyRootfs}} network={{.HostConfig.NetworkMode}} ports={{json .HostConfig.PortBindings}} {{range .Mounts}}{{.Destination}}(rw={{.RW}}) {{end}}' "$container_id"
docker exec "$container_id" python -B /app/tests/container_check.py
docker exec "$container_id" python -B /app/tests/write_denial.py
chmod 0755 "$scratch/fixture/Guides/Locked"
python3 -B tests/fixture.py manifest "$scratch" > "$scratch/after.json"
cmp "$scratch/before.json" "$scratch/after.json"
echo "PASS: fixture manifest unchanged across the read and write-denial checks"

python3 -B - "$scratch" <<'PY' > "$scratch/old-digest"
from hashlib import sha256
from pathlib import Path
import sys
note = Path(sys.argv[1]) / "fixture/Guides/Restore.md"
print(sha256(note.read_bytes()).hexdigest())
note.write_bytes(b"in-place revision\r\n")
PY
docker exec "$container_id" python -B /app/tests/container_check.py --old "$(cat "$scratch/old-digest")"
python3 -B - "$scratch" <<'PY' > "$scratch/old-digest"
from hashlib import sha256
from pathlib import Path
import sys
note = Path(sys.argv[1]) / "fixture/Guides/Restore.md"
print(sha256(note.read_bytes()).hexdigest())
tmp = note.with_suffix(".tmp")
with tmp.open("xb") as stream:
    stream.write(b"replacement revision\n")
tmp.chmod(0o644)
tmp.replace(note)
PY
docker exec "$container_id" python -B /app/tests/container_check.py --old "$(cat "$scratch/old-digest")"
python3 -B tests/fixture.py manifest "$scratch" > "$scratch/revision.json"
python3 -B - "$scratch" <<'PY'
import hashlib
import json
from pathlib import Path
import sys
scratch = Path(sys.argv[1])
expected = json.loads((scratch / "before.json").read_text())
expected["Guides/Restore.md"][-1] = hashlib.sha256(b"replacement revision\n").hexdigest()
assert json.loads((scratch / "revision.json").read_text()) == expected
print("PASS: only the expected operator revision changed the fixture manifest")
PY
