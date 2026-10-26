# Architecture

The patch is tied to one upstream commit. `patches/manifest.json` records that commit, the patch checksum, resulting shader chain and checksums of changed runtime files. `bootstrap` clones a separate checkout and applies it without touching a user's existing installation. Generated projects use absolute local source links and therefore depend on keeping that runtime and the private input folders in place.

## Shared platform changes

The tvOS shell omits unsupported phone facilities, uses a controller-only game view and a `GCEventViewController` with controller interaction disabled for UIKit navigation. Physical controller state continues to reach the game through the normal GameController profiles. A `DM_PRESENT_HEIGHT` cap changes the CAMetalLayer drawable, while `DM_OPMODE=0` asks the game for its native handheld behavior. Games decide their own render targets; the builder cannot promise a particular internal resolution for every title.

On hardware without BC texture support, packaged textures are deswizzled and decoded on the CPU using bcdec. The optional mip limit keeps lower existing levels of sampled 2D BC textures; it does not blindly shrink every render target or single-level UI asset. Decompressed textures can consume much more memory than their Switch representation.

Shader compiler conventions are changed explicitly: translator outputs carry `// NXNATIVE_TV_MSL_V1` before MSL source. The host recognizes that marker and compiles on the actual destination GPU with relaxed math and preserved invariance. These files retain nxnative's `.metallib` filename convention but are **source archives**, not iPhone Metal binaries. The changed tools have their own chain hash. A stamped, matching full shader build is required; old phone shader caches are not relabelled.

The `NXTVSH01` flat shader pack has a file count, total payload size and per-file UTF-8 name, size and CRC32. The runtime validates paths, bounds and checksums while unpacking into the app cache. The Python packer streams files rather than reading the whole multi-GB pack into memory.

The notification wait fix marks a guest thread as sleeping around a blocking host service wait, releasing the scheduler handoff so the guest's main thread can run.

## Game-specific memory adapter

`DM_HEAP_RECLAIM` alone cannot identify a game's freed memory. The Mario Kart adapter inserts observation gates at instruction-verified sites in the registered main NSO. At the free site, the engine has validated ownership and unlinked the dead block while its allocator lock still applies. The callback reads that build's heap and block layout, range-checks the payload, preserves its allocator header and partial edge pages, and uses `MADV_FREE` only on full dead payload pages. Writes revive those pages normally.

Hook numbers 22 and 23 and their callback register layout are currently reserved for that verified adapter. The builder rejects them on other modules/builds. Another engine needs its own allocator analysis, observation gates, register/layout adapter and race tests. RAM limits are shared across games, but allocator layouts are not.

## Open platform limitation

Metal source compilation is synchronous and has no public cancellation path here. Suspending the app while compilation blocks can hold graphics drainage and trigger tvOS's scene watchdog. The current explicit stopgap exits after two seconds if suspension has not finished. Returning from suspension while GPU commits continue in the background would violate tvOS's rules; a robust future solution needs coordinated queue admission, in-flight work and resume handling.

## IPA recovery

For a supported thin-arm64 nxnative executable, the tool reads `__dm<module>x0`, `__DM<module>W,__img` and `__dm<module>x1` sections and reconstructs the signed rewritten image layout described in its fixup tables. It does not decompile or run the IPA. NRO-containing IPAs and encrypted/non-nxnative executables are rejected. Recovery preserves existing rewrite observations; inserting a new heap adapter requires original ExeFS modules and a fresh rewrite.
