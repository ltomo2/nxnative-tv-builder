# nxnative TV Builder

Experimental tools and patches for building **nxnative games for Apple TV**. The game code runs through nxnative's native execution and graphics translation layers. This is an unofficial tvOS port of [seedds/nxnative](https://github.com/seedds/nxnative), not an upstream release.

**Mario Kart 8 Deluxe 4.0.1 was tested on an A15 Apple TV 4K (third generation), running tvOS 27.0.** At native handheld resolution (720p), with reduced texture detail and the exact-build heap adapter, the tester reported that racing was “pretty much perfect.” This is a single-device result, not a claim of locked 60 fps or compatibility with every course, game, OS or Apple TV.

No games, decrypted assets, keys, firmware, app binaries, Apple signing identities or Nintendo artwork are included. Bring your own game files. This repository does not provide them.

## What it includes

- tvOS app shell and GameController routing: the game's confirm button no longer also triggers tvOS Back/Home.
- Layered Apple TV icons generated from an image you supply locally.
- CPU decoding for BC textures on the tested A15 hardware, with an optional lower-mip texture limit.
- Native handheld mode and a 720p app presentation buffer. This does **not** change the Apple TV's system HDMI setting; the television still scales the picture to its panel.
- Shader source archives compiled on the destination GPU, plus prewarming of observed shader/pipeline lists.
- A scheduler fix for guest threads waiting on host notification services.
- Opt-in reclaim of dead heap pages, **only with verified game-specific allocator hooks**. The included adapter is for one Mario Kart build; it must not be copied to other games or updates.
- Project generation, unsigned or development-signed builds, and explicit installation/data transfer to a paired Apple TV.

## Requirements

- An Apple Silicon Mac, full Xcode with the tvOS 27 SDK, Python 3.9+, Git and [XcodeGen](https://github.com/yonaskolb/XcodeGen).
- For translating shaders or extracting NSPs, nxnative's additional prerequisites: .NET, SPIRV-Cross, the Metal toolchain, and Python packages `capstone`, `lz4` and `fonttools`. Follow the README in the pinned runtime checkout. Do not assume an arbitrary newer nxnative revision is compatible with this patch.
- An Apple TV paired in Xcode's Devices and Simulators window. Only the A15 third-generation Apple TV 4K on tvOS 27.0 has been tested here.
- An Apple signing team capable of provisioning your device for tvOS development, for installation. The default build is unsigned; signing and deployment are explicit commands.
- A controller paired to the Apple TV. Game button names follow Nintendo's layout: on an Xbox controller, the right face button (B) is Nintendo A/confirm.
- Local extracted game data, matching profile, rewritten modules and shaders as described below. Leave substantial free disk space: extraction, shader generation and Xcode builds can consume tens of GB.

## Start here

Run commands from this repository. Keep private game/build files **outside** this repository.

```sh
python3 -m venv /path/to/private-tools-venv
source /path/to/private-tools-venv/bin/activate
python -m pip install capstone lz4 fonttools

python scripts/tv_builder.py bootstrap --out /path/to/nxnative-tv-runtime
```

`bootstrap` checks out nxnative commit `2e655b79bfdc86b2967ad0c0b3305e260ad6228f` and applies the checked-in patch. It leaves the checkout detached and locally modified. It does not alter an existing nxnative installation. The resulting shader chain is `142a25fea94c`.

### If you already have an nxnative/NS Port Builder IPA

```sh
python scripts/tv_builder.py inspect-ipa /path/to/YourGame.ipa
```

This identifies embedded nxnative modules, their build IDs and the shader chain. It does not run the IPA. An ordinary iPhone App Store IPA is not supported.

**A phone IPA alone is usually insufficient.** Its compiled iPhone Metal libraries cannot simply be renamed or copied into a tvOS build. You need the matching extracted RomFS, game profile and a shader package from the original build, then regenerate the shaders using the patched runtime. The tool can recover rewritten module images from a supported thin-arm64 nxnative IPA using `prepare --ipa`; the memory adapter still requires rewriting the original ExeFS. IPA recovery with dynamic NRO modules is not implemented in this release; `--patched` supports upstream's NRO embedding instead.

### Obtain the private build inputs

Use your own dumps with nxnative's build script. If you already have its `<title ID>_build` folder, keep it: this contains the useful intermediate files.

For a fresh build, use the **patched runtime** and the pinned upstream script's normal preparation workflow:

```sh
python /path/to/nxnative-tv-runtime/scripts/build_ipa_from_nsp.py \
  /path/to/your-game-nsps -k /path/to/your/prod.keys \
  --prepare-xcode --keep-extracted --gpu none --progress lines
```

This upstream step extracts, surveys, rewrites and translates the game and generates an iOS project as an intermediate artifact. **Do not install that iOS project on the TV.** The next command generates the separate tvOS project. For unsupported titles, upstream's provisional profile may still fail to boot; this tool does not automatically port their engine or services.

Common paths in the resulting game work folder:

| Input | Typical location |
| --- | --- |
| Extracted original modules | `extract/<package>/ExeFS` |
| Game data | `extract/<package>/RomFS` |
| Rewritten modules | `patched_<package>` |
| Shader package | `shader/pkg` |
| TV shader source archives | `shader/ios/b-142a25fea94c` |
| Generated shader tables | Effective profile's `c1_full.tsv`, `cb_extent.tsv` |
| Derived profile (when upstream makes one) | `profile/<title ID>` |
| Original game icon | `extract/<package>/icon.jpg` |

If no derived profile exists, the effective profile is `dm/games/<title ID>` in the runtime checkout. The shader build writes its tables there. Use the exact profile used by that build, including the generated tables, not a fresh empty copy. Preserve `shader_map.tsv.conflicts.tsv` too if present: it contributes to the shader-input fingerprint.

### Mario Kart 8 Deluxe: enable its verified memory adapter

This step is essential to reproduce the tested low-memory Mario Kart setup. It checks the original main NSO build ID and asserts the original instructions before inserting hooks.

```sh
python scripts/tv_builder.py rewrite \
  --runtime /path/to/nxnative-tv-runtime \
  --profile /path/to/effective-profile/0100152000022000 \
  --exefs /path/to/game-work/extract/PACKAGE/ExeFS \
  --out /path/to/game-work/tv-patched --mk8-memory
```

The command prints a new private profile path next to `tv-patched`. Use that profile and `tv-patched` in the next step. Your original profile is not modified. Only main build `158c0c7399aabd7bf83795c2ef3484c7` is supported. Update labels and NSP filenames are not a substitute for the build-ID check. Other versions stop with an error; they need allocator analysis before an adapter can be added.

### Generate the TV project

```sh
python scripts/tv_builder.py prepare \
  --runtime /path/to/nxnative-tv-runtime \
  --profile /path/to/game-work/tv-patched-profiles/0100152000022000 \
  --patched /path/to/game-work/tv-patched \
  --romfs /path/to/game-work/extract/PACKAGE/RomFS \
  --shaders /path/to/game-work/shader/ios/b-142a25fea94c \
  --icon /path/to/game-work/extract/PACKAGE/icon.jpg \
  --name 'Mario Kart TV' --version 4.0.1 \
  --team YOURTEAMID --out /path/to/private-tv-project
```

Use a new output directory for each preparation. For other games, substitute their effective profile, matching rewritten modules and assets; omit `--mk8-memory` in the rewrite step. Their TV compatibility is unverified. You can use `--ipa /path/to/Game.ipa` instead of `--patched` to recover supported embedded modules, but this will not retroactively insert Mario Kart's allocator hooks.

Optional arguments:

- `--aoc /path/to/aoc`: a directory containing extracted add-on content under numeric index folders, as expected by nxnative. Nothing is unlocked or downloaded by this tool.
- `--prewarm /path/to/pso_user.tsv`: merge a shader/pipeline list captured from your own device, for this same game/shader build.
- `--bundle-id org.example.yourgame.tv`: set a stable app identity. Updates installed under the same identity preserve that app's data. A different identity creates a separate installation.
- `--language en-GB`: game language; default `en-US`.
- `--height 1080 --opmode 1`: docked-mode experiment. The tested Mario Kart default is `--height 720 --opmode 0`. A presentation cap alone does not change a game's internal render targets.
- `--texture-limit 0`: disable lower-mip selection; default `256`.
- `--standard-font /path/to/local-font.ttf`: override the locally copied macOS font stand-in. Fonts and icon art are not redistributed here.

### Build, sign and install

```sh
# Compile-only check; no installation or signing:
python scripts/tv_builder.py build --project /path/to/private-tv-project

# Development signing, using the team set at prepare time:
python scripts/tv_builder.py build --project /path/to/private-tv-project --signed

xcrun devicectl list devices
python scripts/tv_builder.py deploy --project /path/to/private-tv-project \
  --device YOUR_PAIRED_APPLE_TV_ID --launch
```

Deployment installs the app, streams a shader pack to its cache, then transfers RomFS and optional add-on data. The first launch unpacks shaders; leave the app open. The `.app` is intentionally small and does not contain the game data. Sharing it alone will not reproduce the installation. Use Xcode to manage provisioning or pairing errors; this tool does not bypass Apple's signing requirements.

## Existing shader package, new TV shaders

If you have the original build's `shader/pkg` and effective profile, you can regenerate just its shaders:

```sh
python scripts/tv_builder.py translate \
  --runtime /path/to/nxnative-tv-runtime \
  --profile /path/to/effective-profile/TITLE_ID \
  --shader-package /path/to/game-work/shader/pkg \
  --cache /path/to/game-work/tv-shader-cache \
  --out /path/to/game-work/tv-shader-ios
```

Use the resulting `tv-shader-ios/b-142a25fea94c` with `prepare`. Translation also regenerates the profile's shader guard/extent tables. Runtime code, chain hash, library stamp, profile digest and table headers must agree. The builder refuses incompatible caches rather than relabelling them.

## Limitations and troubleshooting

See [testing and limitations](docs/TESTING.md) and [how the changes work](docs/ARCHITECTURE.md). There is no universal memory patch or automatic compatibility guarantee for another game. The physical 4 GB of RAM is not an app's usable budget: this test device exposed an effective app limit around 2 GB.

Known shader lists preload before the first frame. Reaching the title screen means that recorded preload has finished, **not** that every shader in every course is compiled. A new course, character or effect can still stall on first use. After playing, you can copy `Library/Caches/pso_user.tsv` from the app data container and pass it to the next build with `--prewarm`.

Assets and saves currently use tvOS's **purgeable cache storage**. This is an experimental limitation: tvOS may remove them, requiring retransfer, and saves should be backed up. Backgrounding while Metal is blocked can also cause the app to close and restart at the title screen. Do not treat this as a production-ready console replacement.

## Development and credits

```sh
python -m unittest discover -s tests -v
```

The original native execution system, rewriter, service host and Metal graphics layer are **nxnative**, by seedds, with its upstream credits including d2r-mobile, Ryujinx, SPIRV-Cross and the other projects listed in the upstream README. This repository packages experimental tvOS changes and a reproducible builder around that work. See [LICENSE](LICENSE), [LICENSE.nxnative](LICENSE.nxnative) and [third-party notices](THIRD_PARTY_NOTICES.md). Not affiliated with Nintendo or Apple.
