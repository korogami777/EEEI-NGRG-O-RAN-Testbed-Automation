#!/usr/bin/env python3
#
# NIST-developed software is provided by NIST as a public service. You may use,
# copy, and distribute copies of the software in any medium, provided that you
# keep intact this entire notice. You may improve, modify, and create derivative
# works of the software or any portion of the software, and you may copy and
# distribute such modifications or works. Modified works should carry a notice
# stating that you changed the software and should note the date and nature of
# any such change. Please explicitly acknowledge the National Institute of
# Standards and Technology as the source of the software.
#
# NIST-developed software is expressly provided "AS IS." NIST MAKES NO WARRANTY
# OF ANY KIND, EXPRESS, IMPLIED, IN FACT, OR ARISING BY OPERATION OF LAW,
# INCLUDING, WITHOUT LIMITATION, THE IMPLIED WARRANTY OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE, NON-INFRINGEMENT, AND DATA ACCURACY. NIST
# NEITHER REPRESENTS NOR WARRANTS THAT THE OPERATION OF THE SOFTWARE WILL BE
# UNINTERRUPTED OR ERROR-FREE, OR THAT ANY DEFECTS WILL BE CORRECTED. NIST DOES
# NOT WARRANT OR MAKE ANY REPRESENTATIONS REGARDING THE USE OF THE SOFTWARE OR
# THE RESULTS THEREOF, INCLUDING BUT NOT LIMITED TO THE CORRECTNESS, ACCURACY,
# RELIABILITY, OR USEFULNESS OF THE SOFTWARE.
#
# You are solely responsible for determining the appropriateness of using and
# distributing the software and you assume all risks associated with its use,
# including but not limited to the risks and costs of program errors, compliance
# with applicable laws, damage to or loss of data, programs or equipment, and
# the unavailability or interruption of operation. This software is not intended
# to be used in any situation where a failure could cause risk of injury or
# damage to property. The software developed by NIST employees is not subject to
# copyright protection within the United States.

# Pre-fetch the base images of a Dockerfile from the O-RAN SC nexus registry.
#
# The nexus registry throttles each connection to a few KB/s and drops long
# connections, so a plain `docker pull` of a large layer can take hours and
# restart from zero. This script downloads each layer in parallel, resumable
# range chunks, verifies the digests, and loads the image with `docker load`
# so that the subsequent `docker build` finds it locally.
#
# Usage: prefetch_docker_image.py <Dockerfile> [--build-arg KEY=VALUE ...]

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SLOW_REGISTRIES = ("nexus3.o-ran-sc.org",)
WORKERS = 24
CHUNK_SIZE = 1024 * 1024
MANIFEST_TYPES = ", ".join([
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
])
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
CACHE_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "image_cache")


def parse_base_images(dockerfile, build_args):
    """Return the external images referenced by FROM lines, with ARGs expanded."""
    args = {}
    stages = set()
    images = []
    with open(dockerfile) as f:
        for line in f:
            line = line.strip()
            m = re.match(r"(?i)^ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(\S*))?", line)
            if m:
                args[m.group(1)] = build_args.get(m.group(1), m.group(2) or "")
                continue
            m = re.match(r"(?i)^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line)
            if m:
                image = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?",
                               lambda v: build_args.get(v.group(1), args.get(v.group(1), "")),
                               m.group(1))
                if image.lower() not in stages and image != "scratch":
                    images.append(image)
                if m.group(2):
                    stages.add(m.group(2).lower())
    return images


def split_image(image):
    """Split host:port/repo:tag into (host, repo, tag)."""
    host, _, rest = image.partition("/")
    repo, _, tag = rest.rpartition(":")
    if not repo or "/" in tag:
        repo, tag = rest, "latest"
    return host, repo, tag


def http_get(url, headers=None, byte_range=None, timeout=60):
    headers = dict(headers or {})
    if byte_range:
        headers["Range"] = "bytes=%d-%d" % byte_range
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def get_manifest(base, repo, tag):
    manifest = json.loads(http_get("%s/v2/%s/manifests/%s" % (base, repo, tag), {"Accept": MANIFEST_TYPES}))
    if "manifests" in manifest:  # Multi-architecture index, pick linux/amd64
        for entry in manifest["manifests"]:
            platform = entry.get("platform", {})
            if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
                return get_manifest(base, repo, entry["digest"])
        raise RuntimeError("no linux/amd64 image in manifest list")
    return manifest


def fetch_chunk(url, path, start, end, progress):
    """Download one byte range to its own file, so interrupted downloads resume per chunk."""
    if os.path.exists(path) and os.path.getsize(path) == end - start + 1:
        progress.add(end - start + 1)
        return
    for attempt in range(30):
        try:
            data = http_get(url, byte_range=(start, end))
            if len(data) != end - start + 1:
                raise IOError("short read")
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
            progress.add(len(data))
            return
        except Exception:
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError("failed to download %s bytes %d-%d" % (url, start, end))


def assemble_blob(blob, blob_dir):
    """Join a blob's chunk files and verify the result against its digest."""
    digest = blob["digest"]
    size = blob["size"]
    hex_digest = digest.split(":")[1]
    target = os.path.join(blob_dir, hex_digest)
    if os.path.exists(target):
        return target
    chunk_dir = target + ".chunks"
    sha = hashlib.sha256()
    with open(target + ".part", "wb") as out:
        for start in range(0, size, CHUNK_SIZE):
            with open(os.path.join(chunk_dir, "%012d" % start), "rb") as f:
                data = f.read()
            sha.update(data)
            out.write(data)
    if sha.hexdigest() != hex_digest:
        os.remove(target + ".part")
        subprocess.run(["rm", "-rf", chunk_dir])
        raise RuntimeError("digest mismatch for %s, cached chunks removed" % digest)
    os.replace(target + ".part", target)
    subprocess.run(["rm", "-rf", chunk_dir])
    return target


class Progress:
    def __init__(self, total):
        self.total = total
        self.done = 0
        self.lock = threading.Lock()
        self.start = time.time()

    def add(self, n):
        with self.lock:
            self.done += n

    def report(self, stop):
        while not stop.wait(15):
            elapsed = time.time() - self.start
            print("    %.1f / %.1f MB (%.0f KB/s)" % (self.done / 1e6, self.total / 1e6,
                                                   self.done / 1024 / max(elapsed, 1)), flush=True)


def prefetch(image):
    host, repo, tag = split_image(image)
    base = "https://" + host
    manifest = get_manifest(base, repo, tag)
    blobs = [manifest["config"]] + manifest["layers"]
    blob_dir = os.path.join(CACHE_DIR, "blobs")
    os.makedirs(blob_dir, exist_ok=True)

    progress = Progress(sum(b["size"] for b in blobs))
    print("Pre-fetching %s (%.1f MB) with %d parallel connections..." % (image, progress.total / 1e6, WORKERS),
          flush=True)
    # Queue the chunks of every blob in one pool so no connection idles at blob boundaries
    chunks = []
    for blob in blobs:
        target = os.path.join(blob_dir, blob["digest"].split(":")[1])
        if os.path.exists(target):
            progress.add(blob["size"])
            continue
        os.makedirs(target + ".chunks", exist_ok=True)
        url = "%s/v2/%s/blobs/%s" % (base, repo, blob["digest"])
        for start in range(0, blob["size"], CHUNK_SIZE):
            end = min(start + CHUNK_SIZE, blob["size"]) - 1
            chunks.append((url, os.path.join(target + ".chunks", "%012d" % start), start, end, progress))

    stop = threading.Event()
    threading.Thread(target=progress.report, args=(stop,), daemon=True).start()
    try:
        with ThreadPoolExecutor(WORKERS) as pool:
            for future in [pool.submit(fetch_chunk, *c) for c in chunks]:
                future.result()
    finally:
        stop.set()
    paths = [assemble_blob(b, blob_dir) for b in blobs]

    # Assemble a `docker save` style archive; docker load accepts gzipped layer files
    config_name = os.path.basename(paths[0]) + ".json"
    layer_names = [os.path.basename(p) + "/layer.tar" for p in paths[1:]]
    archive = os.path.join(CACHE_DIR, "image.tar")
    with tarfile.open(archive, "w") as tar:
        tar.add(paths[0], arcname=config_name)
        for path, name in zip(paths[1:], layer_names):
            tar.add(path, arcname=name)
        index = json.dumps([{"Config": config_name, "RepoTags": [image], "Layers": layer_names}]).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(index)
        tar.addfile(info, io.BytesIO(index))
    subprocess.run(["docker", "load", "-i", archive], check=True)
    os.remove(archive)
    for path in paths:
        os.remove(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dockerfile")
    parser.add_argument("--build-arg", action="append", default=[])
    opts = parser.parse_args()
    build_args = dict(a.split("=", 1) for a in opts.build_arg)

    failed = False
    for image in parse_base_images(opts.dockerfile, build_args):
        if not image.startswith(SLOW_REGISTRIES):
            continue
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
            print("Image %s is already present, skipping pre-fetch." % image)
            continue
        try:
            prefetch(image)
        except Exception as e:
            print("WARNING: Could not pre-fetch %s (%s), docker build will pull it instead." % (image, e))
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
