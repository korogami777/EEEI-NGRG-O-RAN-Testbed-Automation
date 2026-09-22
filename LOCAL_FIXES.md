# Local Fixes on Top of Upstream NIST O-RAN-Testbed-Automation

This document describes the changes in this checkout that are **not** part of the upstream
[usnistgov/O-RAN-Testbed-Automation](https://github.com/usnistgov/O-RAN-Testbed-Automation) repository.

- **Upstream base:** `6088882` — *Update automation tool (#19)* (`origin/main`)
- **Local commits:** 9 commits on `main` (2026-09-21), none pushed
- **Result:** the Near-RT RIC installs without hanging, and the OCUDU gNB testbed works with both
  srsRAN_4G UEs (band 3) and Duranta (OpenAirInterface) UEs (band 78) through the ZeroMQ channel emulator.

To list the commits yourself:

```bash
git log --oneline origin/main..main
```

## Summary

| # | Commit | Area | Problem | Fix |
|---|--------|------|---------|-----|
| 1 | `9d1f323` | Near-RT RIC install | Kubernetes install fails with "version not found" | Look up each package's version separately |
| 2 | `9edfcc8` | Run scripts | `sudo -v` fails without a TTY | Use `sudo true` |
| 3 | `7d54c55` | Near-RT RIC install | RIC reset hangs forever while deleting PVCs | `kubectl delete --wait=false` in `delete_namespace.sh` |
| 4 | `9d1d740` | Near-RT RIC install | xApp/E2 simulator base image downloads take ~9 hours | Parallel, resumable pre-fetch of O-RAN SC images |
| 5 | `19fac32` | Configuration | ZeroMQ validation always fails with Duranta UEs | Validate only the channel emulator in Duranta mode |
| 6 | `f465713` | Configuration | — | Enable the Duranta UE by default |
| 7 | `870c52d` | Duranta UE | UE segfault after RRC setup freezes all UEs | Apply upstream OAI PR #561 as a patch |
| 8 | `0148da3` | Configuration | Duranta UEs never register on band 3 | Band 3 for srsRAN UEs, band 78 for Duranta UEs |
| 9 | `543f456` | Grafana/Telegraf | Telegraf crashes with "no outputs found" — Grafana shows no data | Bake config files into Docker image and update Telegraf to 1.35.0 |

Commit `1b917c0` (a local fix for the same segfault as #7) is superseded by `870c52d` and has no net effect.

---

## 1. Kubernetes install: version suffix mismatch (`9d1f323`)

**File:** `RAN_Intelligent_Controllers/Near-Real-Time-RIC/install_patch_files/ric-dep/bin/install_k8s_and_helm.sh`

**Problem:** the script looked up the latest version from `kubeadm` only, then reused that exact version string,
including its platform suffix (for example `ubuntu22.04u11`), for `kubelet` and `kubectl`. The packages in the apt
repository have different suffixes, so installing `kubelet` and `kubectl` failed with "version not found".

**Fix:** look up each package's version individually.

## 2. `sudo -v` fails in non-interactive runs (`9edfcc8`)

**Files:** `run.sh`, `Next_Generation_Node_B/run_background.sh`, `User_Equipment/run_background.sh`,
`OpenAirInterface_Testbed/User_Equipment/run_background.sh`

**Problem:** `sudo -v` validates the cached credentials instead of running a command, and fails without a TTY even when
`NOPASSWD` is configured.

**Fix:** use `sudo true`, which performs the same validation and works in non-interactive environments.

## 3. RIC reset hangs on PVC deletion (`7d54c55`)

**File:** `RAN_Intelligent_Controllers/Near-Real-Time-RIC/install_scripts/delete_namespace.sh`

**Symptom:** reinstalling the RIC hangs after:

```
persistentvolumeclaim "pvc-ricplt-alarmmanager" force deleted
persistentvolumeclaim "pvc-ricplt-e2term-alpha" force deleted
```

**Cause:** the script deletes every namespaced resource type one type at a time, in the order of
`kubectl api-resources`. `kubectl delete` waits by default until the resources are gone. PVCs are listed before pods,
and the `kubernetes.io/pvc-protection` finalizer keeps a PVC until the pods using it are deleted, which only happens in
a later iteration of the loop. The loop therefore waits forever.

**Fix:** pass `--wait=false` so all deletions are issued immediately. The existing namespace wait loop further down
handles completion.

> [!NOTE]
> A pod whose containers never stop (seen once with `r4-infrastructure-kong`) can still keep a namespace in
> `Terminating`. If that happens, force-delete the pods:
> `kubectl delete pods --all -n ricplt --grace-period=0 --force --wait=false`

## 4. Slow downloads from the O-RAN SC registry (`9d1d740`)

**Files:**
- `RAN_Intelligent_Controllers/Near-Real-Time-RIC/install_scripts/prefetch_docker_image.py` (new)
- `RAN_Intelligent_Controllers/Near-Real-Time-RIC/install_scripts/install_xapp_hw-go.sh`
- `RAN_Intelligent_Controllers/Near-Real-Time-RIC/install_scripts/install_e2sim.sh`
- `RAN_Intelligent_Controllers/Near-Real-Time-RIC/.gitignore`

**Problem:** `nexus3.o-ran-sc.org` throttles each connection to roughly 5–70 KB/s and drops long-lived connections.
`docker pull` downloads each layer over a single connection and restarts a dropped layer from zero. The hw-go xApp
base image `bldr-ubuntu20-c-go:1.0.0` (~700 MB, largest layer 313 MB) was on course to take about 9 hours, and one
download restarted from 0 after hours of progress.

**Fix:** before `docker build`, `prefetch_docker_image.py` reads the Dockerfile's `FROM` images and, for those on the
O-RAN SC registry that are not already present:

1. downloads every layer as 1 MB range requests over 24 parallel connections,
2. stores each chunk as its own file, so interrupted downloads and reruns resume per chunk,
3. verifies each blob's SHA-256 digest,
4. loads the image with `docker load`, so the following `docker build` finds it locally.

If the pre-fetch fails, the build pulls the image itself as before. Chunks are cached in
`Near-Real-Time-RIC/image_cache/` (ignored by git) and removed after a successful load.

**Measured:** the 700 MB image downloaded in about 65 minutes (~180 KB/s aggregate). The registry also caps total
throughput per client, so more connections do not help much beyond this.

> [!TIP]
> The RIC pods pull their images through the kubelet, which does not use this pre-fetch. If pods stay in
> `ContainerCreating`/`PodInitializing` for a long time, pre-pull the images with `sudo crictl pull <image>` and then
> delete the stuck pods so they are recreated with the local image (all RIC images use `imagePullPolicy: IfNotPresent`).

## 5. ZeroMQ validation fails with Duranta UEs (`19fac32`)

**Files:** `generate_configurations.sh`, `run.sh`

**Symptom:** with `USE_DURANTA_UE=true`, `./generate_configurations.sh` fails with:

```
ERROR: UE config directory not found: ../User_Equipment/configs
ERROR: Cell 1: gNB tx_port0 must be tcp://127.0.0.1:2000 for channel emulator mode
ERROR: UE 1: missing ../User_Equipment/configs/ue1.conf
```

**Cause:** `validate_zmq_channel_emulator_config.sh` checks srsRAN UE configs in `User_Equipment/configs/ueN.conf`.
Duranta UE configs are generated in `OpenAirInterface_Testbed/User_Equipment/configs/` in the OAI format, and the
Duranta UE takes its ZeroMQ ports from the channel emulator configuration at launch instead. The gNB port errors are
false alarms: the validator only reads the gNB settings when no earlier error was found.

**Fix:** in Duranta mode, both scripts call the validator with `--channel-emulator-only`, as the OpenAirInterface
testbed scripts already do. The channel emulator's cell and UE ports and IP addresses are still validated.

## 6. Duranta UE enabled (`f465713`)

**Files:** `generate_configurations.sh`, `run.sh`

Sets `USE_DURANTA_UE=true` in both scripts, as the README requires for Duranta UEs. This is a configuration choice for
this testbed, not a bug fix. Set it back to `false` in both files to use srsRAN_4G UEs; the band switches automatically
(see [section 8](#8-band-3-for-srsran-ues-band-78-for-duranta-ues-0148da3)).

## 7. Duranta UE segfault on overlapping PUCCH (`870c52d`)

**Files:**
- `OpenAirInterface_Testbed/User_Equipment/install_patch_files/openairinterface5g/openair2/LAYER2/NR_MAC_UE/common_harq_pucch_pr561.patch` (new)
- `OpenAirInterface_Testbed/User_Equipment/install_scripts/apply_patches.sh`

**Symptom:** one Duranta UE crashes right after `Applying CellGroupConfig from gNodeB` (exit code 139). Because the
channel emulator sums the uplinks of all UEs and needs samples from every UE, the whole testbed then freezes and no UE
registers. The kernel log shows:

```
UL__actor: segfault at 20 ... in nr-uesoftmodem
```

which resolves to `get_pucch_start_symbol_length()` (`nr_ue_procedures.c:2007`).

**Cause:** a bug in Duranta OpenAirInterface `2026.w35` and current `develop`, reported upstream as
[issue #493](https://github.com/duranta-project/openairinterface5g/issues/493). The HARQ-ACK for Msg4 is sent on the
common PUCCH, which leaves `pucch_resource` NULL. When a dedicated SR or CSI occasion falls in the same slot,
`multiplex_pucch_resource()` dereferences it. Whether the slots coincide depends on timing, so it affects a random
subset of runs and UEs.

**Fix:** the upstream fix from
[PR #561](https://github.com/duranta-project/openairinterface5g/pull/561) (open, confirmed working by the reporter) is
included unchanged as a patch. In a slot with a common-PUCCH HARQ-ACK, the UE transmits only the HARQ-ACK and drops the
overlapping SR/CSI; the PR also stores `pucch-ResourceCommon` on the HARQ process at DCI time, so a dedicated BWP without
it does not assert. `apply_patches.sh` restores the five affected MAC files, then applies the patch, or skips it when
the code already contains the fix (for example after the pinned OAI version is updated to one with PR #561 merged).

**Applying it to an existing install** requires rebuilding the UE:

```bash
cd OpenAirInterface_Testbed/User_Equipment
./install_scripts/apply_patches.sh
cd openairinterface5g && source oaienv && cd cmake_targets
./build_oai --ninja --nrUE -w ZMQ      # add -C for a clean build
```

## 8. Band 3 for srsRAN UEs, band 78 for Duranta UEs (`0148da3`)

**Files:** `generate_configurations.sh`, `run.sh`, `Next_Generation_Node_B/generate_configurations.sh`,
`OpenAirInterface_Testbed/User_Equipment/run.sh`, `OpenAirInterface_Testbed/User_Equipment/run_gdb.sh`,
`OpenAirInterface_Testbed/User_Equipment/run_background.sh`, `README.md`

**Symptom:** on band 3, Duranta UEs complete random access and RRC setup, but then loop with:

```
[NR_MAC] [UE 0] SR not served! SR counter 64 reached sr_MaxTransmissions 64
```

and never register. This matches
[Duranta issue #508](https://github.com/duranta-project/openairinterface5g/issues/508) (*OAI UE with OCUDU gNB*).

**Cause:** the band 3 gNB profile uses PUCCH format 1 with OCC, and the OCUDU gNB does not answer the Duranta UE's
scheduling requests on it. The band 78 profile uses PUCCH `f0_and_f2` with CSI disabled, which is also the
configuration of Duranta's own OCUDU interoperability CI test (`ci-scripts/yaml_files/5g_zmq_radio_1x1_ocudu`). Band 78
cannot be used for all UEs: srsUE SA only supports 15 kHz SCS, while the band 78 preset uses 30 kHz.

**Fix:** the band now follows `USE_DURANTA_UE`, instead of commented-out presets that had to be switched by hand in
several files:

- `generate_configurations.sh` passes `--band 78` to the gNB generator when `USE_DURANTA_UE=true`, and `--band 3`
  otherwise.
- `Next_Generation_Node_B/generate_configurations.sh` has a new `--band <3|78>` option (default 3) that selects the gNB
  template and ARFCN.
- `run.sh` reads the band from `Next_Generation_Node_B/configs/gnb.yaml` in Duranta mode, exports it as
  `DURANTA_UE_NR_BAND`, and stops with an error if the gNB is not on band 78.
- The Duranta UE's `run.sh`/`run_gdb.sh` use the band 78 radio settings when `DURANTA_UE_NR_BAND=78`, and band 3
  otherwise, so the standalone OpenAirInterface testbed is unchanged. `run_background.sh` passes the variable through
  `sudo`.
- srsRAN UEs already read the band from `gnb.yaml`, so no change was needed there.

To switch UE type, set `USE_DURANTA_UE` in `run.sh` and `generate_configurations.sh`, then rerun
`./generate_configurations.sh`.

---


## 9. Grafana Telegraf config not loaded

**File:** `Next_Generation_Node_B/ocudu/docker/telegraf/Dockerfile`

**Symptom:** Grafana dashboards show no data. Telegraf exits immediately with:

```
Loading config: /etc/telegraf/telegraf.conf
Error running agent: no outputs found, did you provide a valid config file?
```

**Cause:** two issues:

1. The `telegraf/Dockerfile` installs packages but never `COPY`s `telegraf.conf`, `entrypoint.sh`, or `ws_adapter.py`
   into the image. The container falls back to the stock Telegraf config at `/etc/telegraf/telegraf.conf`, which has no
   outputs.
2. The Dockerfile pinned `telegraf:1.31.0`, but the config uses `rate_limit` and `rate_limit_period` fields on
   `outputs.influxdb_v2` that were added in Telegraf 1.35.0 (matching `TELEGRAF_VERSION` in `.env`).

**Fix:** update the Dockerfile to `COPY` all config files into the image and set the entrypoint:

```dockerfile
COPY telegraf.conf /etc/ocudu/telegraf.conf
COPY dump_metrics.conf /etc/ocudu/dump_metrics.conf
COPY load_metrics.conf /etc/ocudu/load_metrics.conf
COPY entrypoint.sh /entrypoint.sh
COPY ws_adapter.py /usr/local/bin/ws_adapter.py
RUN chmod +x /entrypoint.sh /usr/local/bin/ws_adapter.py
ENTRYPOINT ["/entrypoint.sh"]
```

Also update the base image from `telegraf:1.31.0` to `telegraf:1.35.0`.

After applying, rebuild the image with `docker compose build telegraf` then restart with
`./start_grafana_webui.sh` or `docker compose up -d`.

> [!NOTE]
> An earlier version of this fix mounted the config files as volumes in `docker-compose.override.yml`, but
> `start_grafana_webui.sh` regenerates that file on each run, wiping the mounts. Baking the files into the
> image avoids this.

## Local settings not committed

These uncommitted edits are required for this setup and are kept as local configuration:

| File | Change | Why |
|------|--------|-----|
| `OpenAirInterface_Testbed/User_Equipment/full_install.sh` | `RADIO_TYPE="ZMQ"` | Builds the Duranta UE with the ZeroMQ radio library |
| `OpenAirInterface_Testbed/User_Equipment/generate_configurations.sh` | `RADIO_TYPE="ZMQ"` | Same, for the generated configuration |
| `OpenAirInterface_Testbed/User_Equipment/run.sh`, `run_gdb.sh` | `USE_ZMQ_CHANNEL_EMULATOR=true` | Required by `run.sh` for Duranta UEs |
| `Next_Generation_Node_B/install_scripts/get_zmq_channel_emulator_config.sh` | Prefer `../../Next_Generation_Node_B` for the `User_Equipment` case | When the root `run.sh` starts Duranta UEs from `OpenAirInterface_Testbed/User_Equipment`, this points them at the OCUDU channel emulator instead of the OpenAirInterface one |

## Operational notes

Issues hit during installation that needed no code change:

- **Transient DNS failure in `docker build`** (`Could not resolve host: github.com`): happened while Docker was being
  restarted with new DNS servers during the install. Rerunning `./full_install.sh` fixed it.
- **Stuck RIC pods after a slow image pull:** the kubelet's original pull requests never returned even after the images
  were present. Deleting the pods fixed it (see the tip in [section 4](#4-slow-downloads-from-the-o-ran-sc-registry-9d1d740)).
- **Duranta UE build directory from another machine:** `openairinterface5g/cmake_targets/ran_build/build` was configured
  for `/home/ngrg2627-1/...`. The first rebuild needed a clean build (`build_oai ... -C`).
- **`rtmgr` in `CrashLoopBackOff`:** it exits when `appmgr` is not reachable, and recovers once `appmgr` is running.
- **Temporary sudoers rule:** `/etc/sudoers.d/99-claude-crictl` was added to allow `crictl` and `journalctl` without a
  password during debugging. Remove it with `sudo rm /etc/sudoers.d/99-claude-crictl`.

## Verification

- **Near-RT RIC:** all `ricplt` and `ricinfra` pods running, E2 termination registered with the E2 manager.
- **srsRAN_4G UEs (band 3):** 3 UEs registered, IPs `10.45.0.101`–`10.45.0.103`.
- **Duranta UEs (band 78):** 3 UEs registered within about 10 s, one random access each, no `SR not served` and no
  crash. IPs `10.45.0.101`–`10.45.0.103`, each answering `ping` from the host with 0% loss.

## Candidates for upstream

- **NIST (usnistgov/O-RAN-Testbed-Automation):** fixes 1–5 and 8 are general and could be proposed upstream.
  Fix 6 and the local settings are testbed-specific choices.
- **Duranta OpenAirInterface:** fix 7 is already upstream as PR #561; drop the local patch once the pinned version in
  `commit_hashes.json` includes it (`apply_patches.sh` skips it automatically in that case).
