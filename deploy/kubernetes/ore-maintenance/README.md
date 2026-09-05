# Preserved development state during Ore maintenance

This optional overlay retains the exact current Frigate image and bounded
emptyDir volumes. It adds an init container that restores config, SQLite data
and media from the private immutable Secret
`frigate-dev-maintenance-state-20260905`, key `state.tar.gz`. The Secret is
owned and provisioned by the private operator, never this public repository.
The current Application continues to use `../local-dev`; merging this directory
does not activate it.

The restoration helper validates every archive path, size and SHA256 before
writing. It rejects traversal, links, unknown roots, extra or missing members,
a live WAL file, nonempty destinations and a snapshot that does not attest
writer quiescence. SQLite integrity is checked before any restored file is
written. Generated authentication state is restored privately; logs contain
only the archive digest, file count and integrity result. Config seeding keeps
the restored configuration. The archive has a 64MiB expanded bound.

Before activation, the operator must qualify the helper against the actual
image and preserve the exact original Pod UID and complete private snapshot.
The current s6 Frigate finish hook stops the entire container; do not use a
service-down command assuming the container will stay available. Final writer
quiescence requires an exact process-identity pause and an independent timed
resume guard, or another qualified stable snapshot handoff. The snapshot
manifest may set writer_quiesced=true only after actual writers are verified
stopped. The baseline online SQLite backup is intentionally insufficient.

Provision the immutable snapshot Secret before changing the owning Argo
Application path to this overlay. Preserve the previous Application source,
Pod identity and source-owned node label inverse. The private Proxmox placement
source temporarily selects the off-Ore CPU worker through the unchanged
frigate-dev-cpu label. Do not alter production placement or camera credentials.
No source acceptance alone is a live migration or continuity receipt.

After rollout, verify the actual init receipt, private config/authentication
hashes, SQLite data, all retained media, image digest, readiness and camera
operation. Before returning to the original placement, capture a fresh
quiesced snapshot and use a newly named immutable Secret so intervening writes
are preserved. Retain original and handback snapshots until restoration is
verified; never remove a snapshot still referenced by a Pod template.

The separate `render_resume_guard.py` emits the private operator recovery graph:
an immutable source/intent ConfigMap, one Job on an off-Ore worker, and RBAC
restricted to GET and EXEC of the exact original development Pod. It uses the
existing operator-runtime pull identity in agent-fleet-repo-auth; the Frigate
workload continues using its distinct Frigate-only identity. The operator must
supply an actually qualified runtime digest. The guard verifies the Pod UID,
waits no more than five minutes, then resumes only recorded process start times
and command hashes. A matching private marker inside the original Pod closes
the get-to-exec replacement race. The caller must create this marker and observe
the actual Job's ready receipt before invoking `process_pause.py pause`.

The pause helper discovers exactly one Frigate Python root and one go2rtc root,
then their bounded descendants. It refuses changed identities and resumes any
partially stopped original processes on failure. No init or unrelated process
may be paused. The guard independently resumes the exact originals even if the
operator disconnects. If the original Pod is gone, it does not target a successor.
The operator retains the terminal guard receipt and removes only the exact owned
Job, ConfigMap, RBAC and NetworkPolicy UIDs after restoration is verified.
