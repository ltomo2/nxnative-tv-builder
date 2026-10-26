# Testing and current limits

## Hardware result

Tested: Mario Kart 8 Deluxe display version 4.0.1, main build ID `158c0c7399aabd7bf83795c2ef3484c7`, on Apple TV 4K third generation (A15), tvOS 27.0. A physical controller was used. The tester confirmed that the confirm button stays in the game after the GameController routing fix, and reported the 720p setup as “pretty much perfect.”

Before the heap fix, a race approached the approximately 2,097 MiB effective process budget and was killed. Reclaiming already-freed payload pages lowered the observed race footprint to approximately 1.65–1.8 GiB in one Stadium test. These are observations from this setup, not guarantees for other tracks or content. A five-minute diagnostic run reached lap 2/3 and ended at its deliberate test timeout; it did not verify a complete Grand Prix. The normal generated project has no test timeout or scripted input.

At 1080p, logs showed both GPU frames exceeding the 16.7 ms budget and large first-use shader compilation stalls. The accepted 720p configuration uses native handheld mode, a 720p presentation buffer, a 256-pixel lower-mip texture cap, heap reclaim and known-pipeline prewarming. The resolution reduction does not eliminate shader compilation stalls, and no 30 fps game patch has been added.

Full-game completion, every DLC course, split-screen, multiplayer, networking, sleep/resume, older Apple TVs and earlier tvOS releases remain unverified. Upstream game-specific issues also apply, including missing Mii head system-data stand-ins for Mario Kart.

## Build verification

The following checks passed locally for this initial source release:

- Clean pinned-checkout patch application and runtime-file checksum checks.
- Matching shader chain, input stamp, profile digest and generated guard/extent tables.
- Unit tests for archive path rejection, Mach-O parsing/recovery, encryption rejection, exact-build heap adapter checks and shader pack integrity.
- Fresh generated tvOS project compilation with the real private Mario Kart inputs.
- Upstream freed-page range test with AddressSanitizer/UndefinedBehaviorSanitizer, plus the scheduler preemption suite (both default and all-peers modes).
- Fresh rewrite of all three Mario Kart modules, byte-for-byte identical to the installed working build.
- Recovery of all three modules from a real nxnative IPA, identical to their original rewritten images apart from zero alignment padding.
- Project generation through both the rewritten-directory and IPA-recovery inputs.

The fresh generated project compiled unsigned. The builder's separate signing/deployment commands have not been used to replace the working TV installation; that installed build was signed and transferred with the equivalent Xcode/devicectl workflow during the original hardware test. CI results are separate and should be checked on GitHub after publication.

CI runs the Python tests using synthetic artifacts. It does not contain or build a game, sign apps or test an Apple TV.

## Common problems

**Shader chain mismatch:** regenerate from the original shader package with the patched runtime. Never rename `b-<hash>` or edit `DMSH_CHAIN` to force a cache to load.

**Missing c1_full.tsv/cb_extent.tsv:** use the effective profile from the shader build. These generated tables are private build outputs and are not shipped in this repository.

**Memory adapter rejected:** check the original ExeFS build ID. Only the registered build is supported. Do not transplant hook offsets to another update or engine.

**Home returns to the title screen next time:** there is a documented stopgap for a blocked Metal compiler. If graphics suspension has not completed within two seconds, the process exits before tvOS's longer scene watchdog. Fast normal suspension uses the usual pause/resume path. The timeout fallback has not been comprehensively tested and is not an asynchronous cancellation implementation.

**Assets or saves disappear:** current storage is `Library/Caches`, which tvOS can purge. Re-run deployment for game/shader data; keep separate backups of saves. Moving reliable persistence out of purgeable storage is open work.

**Wrong confirm button:** Nintendo uses the right face button for A. The tvOS fix prevents duplicate UIKit navigation; it does not change Nintendo's displayed button labels.

**Signing fails:** open the generated Xcode project, choose the appropriate signing team and paired device, and resolve the provisioning error there. Then rebuild with `--signed`.

**New game fails:** test its nxnative profile first and investigate the actual service, graphics or memory failure. A profile listed as playable on Mac/iPhone is not proof of Apple TV compatibility.
