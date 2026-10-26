#!/usr/bin/env python3
"""Experimental nxnative tvOS host builder. No games, keys or downloads of games."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import sys
import zipfile
import zlib

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / 'patches/manifest.json').read_text())
PAGE = 0x4000
GAME = '0100152000022000'
MK_BUILD = '158c0c7399aabd7bf83795c2ef3484c7'
SOURCE_MARKER = b'// NXNATIVE_TV_MSL_V1\n'


class BuildError(Exception):
    pass


def require(ok, message):
    if not ok:
        raise BuildError(message)


def run(argv, *, env=None, cwd=None, capture=False):
    result = subprocess.run([str(a) for a in argv], env=env, cwd=cwd,
                            check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else ''


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def rows(path):
    return [line.split('\t') for line in Path(path).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def profile_data(profile):
    data = {}
    for key, *values in rows(Path(profile) / 'game.tsv'):
        data.setdefault(key, []).append(values)
    require('title_id' in data and 'modules' in data, 'Incomplete game profile')
    return data


def fixups(path):
    return dict(line[2:].split() for line in Path(path).read_text().splitlines()
                if line.startswith('# '))


def game_env(profile):
    profile = Path(profile).resolve()
    return dict(os.environ, GAME=str(profile), DM_GAME=str(profile),
                DM_GAMES_DIR=str(profile.parent), DM_GAMES_FIRST='1')


def runtime_check(runtime):
    runtime = Path(runtime).resolve()
    for name, digest in MANIFEST['files'].items():
        p = runtime / name
        require(p.is_file() and sha(p) == digest,
                f'Runtime differs from tested patch: {name}. Use bootstrap in a fresh directory.')
    version = run([sys.executable, runtime / 'dm/gfx/tools/dmsh_chain.py'], capture=True)
    require(version == MANIFEST['shader_chain'], 'Shader chain differs from tested translator')
    return runtime


def safe_members(z):
    """Validate the whole archive, including entries we do not extract."""
    seen = set()
    for info in z.infolist():
        p = PurePosixPath(info.filename)
        require(not p.is_absolute() and '..' not in p.parts and '\\' not in info.filename,
                'Unsafe path in IPA')
        require(info.filename not in seen, 'Duplicate path in IPA')
        require(not stat.S_ISLNK(info.external_attr >> 16), 'IPA symlinks are unsupported')
        require(not info.flag_bits & 1, 'Encrypted ZIP entries are unsupported')
        seen.add(info.filename)
    return seen


def macho_sections(data):
    """Read bounded, unencrypted, thin arm64 Mach-O sections without executing code."""
    require(len(data) >= 32 and data[:4] == b'\xcf\xfa\xed\xfe',
            'Only thin 64-bit Mach-O nxnative executables are supported')
    _, cpu, _, _, count, command_bytes, _, _ = struct.unpack_from('<8I', data)
    require(cpu == 0x0100000C, 'IPA executable is not arm64')
    end = 32 + command_bytes
    require(end <= len(data) and count <= 4096, 'Invalid Mach-O load-command bounds')
    pos, sections = 32, {}
    for _ in range(count):
        require(pos + 8 <= end, 'Truncated Mach-O command')
        cmd, size = struct.unpack_from('<II', data, pos)
        require(size >= 8 and pos + size <= end, 'Invalid Mach-O command size')
        if cmd == 0x2C:  # LC_ENCRYPTION_INFO_64
            require(size >= 24, 'Truncated encryption command')
            require(struct.unpack_from('<I', data, pos + 16)[0] == 0,
                    'Encrypted App Store executables cannot be converted')
        if cmd == 0x19:
            require(size >= 72, 'Truncated segment')
            nsect = struct.unpack_from('<I', data, pos + 64)[0]
            require(72 + 80 * nsect <= size, 'Invalid section table')
            for i in range(nsect):
                off = pos + 72 + 80 * i
                name, segment, _, length, fileoff = struct.unpack_from('<16s16sQQI', data, off)
                flags = struct.unpack_from('<I', data, off + 64)[0]
                key = (segment.rstrip(b'\0').decode('ascii'), name.rstrip(b'\0').decode('ascii'))
                if flags & 0xFF in (1, 12, 18):  # zero-fill has no file bytes
                    continue
                require(fileoff + length <= len(data), 'Section extends beyond executable')
                require(key not in sections, 'Duplicate Mach-O section')
                sections[key] = data[fileoff:fileoff + length]
        pos += size
    require(pos == end, 'Mach-O command size mismatch')
    return sections


def ipa_info(path, recover_to=None):
    with zipfile.ZipFile(path) as z:
        names = safe_members(z)
        candidates = [n for n in names if re.fullmatch(r'Payload/[^/]+\.app/Info\.plist', n)]
        require(len(candidates) == 1, 'Expected one application in IPA')
        prefix = candidates[0][:-len('Info.plist')]
        info = plistlib.loads(z.read(candidates[0]))
        exe = info.get('CFBundleExecutable', '')
        require(re.fullmatch(r'[A-Za-z0-9_. -]+', exe or '') is not None,
                'Invalid executable name')
        sections = macho_sections(z.read(prefix + exe))
        require(('__TEXT', '__dmmainx0') in sections,
                'Not a supported nxnative IPA (no embedded rewritten main module)')
        require(('__DATA', '__dm_nro_tab') not in sections,
                'This release does not recover IPAs containing dynamic NRO modules; use --patched')
        fixes = sorted(n for n in names if n.startswith(prefix + 'dm/')
                       and n.endswith('.fixups.tsv') and '/' not in n[len(prefix + 'dm/'):])
        require(fixes, 'IPA has no nxnative fixup metadata')
        modules = {}
        if recover_to:
            Path(recover_to).mkdir(parents=True, exist_ok=False)
        for name in fixes:
            module = PurePosixPath(name).name.removesuffix('.fixups.tsv')
            require(re.fullmatch(r'[a-zA-Z0-9_]+', module) is not None, 'Invalid module name')
            hdr = dict(line[2:].split() for line in z.read(name).decode().splitlines()
                       if line.startswith('# '))
            modules[module] = hdr.get('BUILD_ID', '')
            if not recover_to:
                continue
            te, po = int(hdr['TEXT_END'], 16), int(hdr['PATCH_OFF'], 16)
            a = (te + PAGE - 1) & ~(PAGE - 1)
            wend = int(hdr['FILE_IMAGE_LEN'], 16) if po < 0 else po
            require(0 <= a <= wend <= 512 * 1024 * 1024, 'Invalid module layout')
            keys = [('__TEXT', f'__dm{module}x0'), (f'__DM{module}W', '__img'),
                    ('__TEXT', f'__dm{module}x1')]
            require(all(k in sections for k in keys), f'Missing embedded pieces for {module}')
            parts = [sections[k] for k in keys]
            require(len(parts[0]) == a and len(parts[1]) == wend - a,
                    f'Embedded layout differs from fixups for {module}')
            (Path(recover_to) / f'{module}.patched.bin').write_bytes(b''.join(parts))
        if recover_to:
            for n in names:
                rel = n[len(prefix + 'dm/'):] if n.startswith(prefix + 'dm/') else ''
                if rel and '/' not in rel and rel.endswith(('.fixups.tsv', '.gates.tsv',
                                                         '.cuts.tsv', '.obs.tsv', '.sites.tsv')):
                    (Path(recover_to) / rel).write_bytes(z.read(n))
        chains = sorted({n.split('/msl/b-')[1].split('/')[0] for n in names if '/msl/b-' in n})
        return {'name': info.get('CFBundleDisplayName', info.get('CFBundleName')),
                'bundle_id': info.get('CFBundleIdentifier'), 'modules': modules,
                'shader_chains': chains, 'tested_tv_chain': MANIFEST['shader_chain'],
                'phone_shaders_must_be_rebuilt': chains != [MANIFEST['shader_chain']]}


def bootstrap(args):
    dst = args.out.resolve()
    require(not dst.exists(), f'Output already exists: {dst}')
    run(['git', 'clone', '--no-checkout', args.upstream, dst])
    run(['git', 'checkout', '--detach', MANIFEST['tested_base_commit']], cwd=dst)
    patch = ROOT / 'patches/tvos-runtime.patch'
    require(sha(patch) == MANIFEST['patch_sha256'], 'Runtime patch checksum mismatch')
    run(['git', 'apply', '--check', patch], cwd=dst)
    run(['git', 'apply', patch], cwd=dst)
    runtime_check(dst)
    print(f'TV runtime ready: {dst}')


def rewrite(args):
    runtime = runtime_check(args.runtime)
    profile = args.profile.resolve()
    data = profile_data(profile)
    modules = data['modules'][0][0].split()
    require(not args.out.exists(), 'Rewrite output already exists; choose a fresh directory')
    require(all((args.exefs / m).is_file() for m in modules), 'Missing original ExeFS module')
    if args.mk8_memory:
        require(data['title_id'][0][0] == GAME, 'MK8 adapter only supports Mario Kart 8 Deluxe')
        main = (args.exefs / 'main').read_bytes()
        require(main[:4] == b'NSO0' and main[0x40:0x50].hex() == MK_BUILD
                and main[0x50:0x60] == bytes(16),
                'MK8 memory adapter requires main build ID ' + MK_BUILD)
        # Profile copy isolates the caller's files and preserves other registered fixes.
        target = args.out.resolve().parent / (args.out.name + '-profiles') / profile.name
        require(not target.exists(), f'Adapter profile already exists: {target}')
        shutil.copytree(profile, target, ignore=shutil.ignore_patterns('__pycache__'))
        with (target / 'patches.py').open('a') as f:
            f.write('\n# tv-builder exact-build heap observations\n')
            f.write((ROOT / 'adapters/mk8dx_401.py').read_text().replace('OBS =', 'TV_OBS ='))
            f.write('\nOBS = dict(globals().get("OBS", {}))\n')
            f.write('for _bid, _hooks in TV_OBS.items():\n'
                    '    if _bid in OBS: raise RuntimeError("Heap adapter already present")\n'
                    '    OBS[_bid] = _hooks\n')
        profile = target
    args.out.mkdir(parents=True)
    for module in modules:
        run([sys.executable, runtime / 'dm/tools/patch_nso.py',
             args.exefs.resolve() / module, args.out.resolve(), module], env=game_env(profile))
    print(f'Rewritten modules: {args.out}\nUse profile: {profile}')


def translate(args):
    runtime = runtime_check(args.runtime)
    env = dict(game_env(args.profile), IOSOUT=str(args.out.resolve()), J=str(args.jobs),
               DM_ROOT=str(args.cache.resolve().parent))
    run([sys.executable, runtime / 'dm/gfx/tools/own_compilers.py',
         args.shader_package.resolve(), args.cache.resolve()], env=env)


def validate_shaders(runtime, profile, shaders):
    chain = MANIFEST['shader_chain']
    require(shaders.name == 'b-' + chain,
            f'Expected b-{chain}; rebuild shaders with translate. Do not rename an old cache.')
    stamp = dict(rows(shaders / 'inputs.tsv'))
    data = profile_data(profile)
    digest = run([sys.executable, runtime / 'dm/gfx/tools/dmsh_chain.py', '--inputs'],
                 env=game_env(profile), capture=True)
    require(stamp.get('game') == profile.name == data['title_id'][0][0]
            and stamp.get('inputs') == digest, 'Shader stamp does not match this game profile')
    for table in ('c1_full.tsv', 'cb_extent.tsv'):
        require((profile / table).read_text().startswith(f'# chain b-{chain} inputs {digest} '),
                f'{table} does not match the shader build')
    libs = list(shaders.glob('*.metallib'))
    require(libs, 'No shader libraries')
    # Phone AIR is not a portable tvOS library. Every library must use our explicit source format.
    for p in libs:
        with p.open('rb') as f:
            require(f.read(len(SOURCE_MARKER)) == SOURCE_MARKER,
                    f'{p.name} is not a TV source archive; regenerate the shader build')


def memory_adapter_valid(patched, modules):
    """No hook numbers may silently borrow Mario Kart's register/heap layout."""
    enabled = False
    for module in modules:
        obs = patched / f'{module}.obs.tsv'
        for row in rows(obs) if obs.exists() else []:
            if len(row) > 1 and row[1] in ('22', '23'):
                hdr = fixups(patched / f'{module}.fixups.tsv')
                require(module == 'main' and hdr.get('BUILD_ID') == MK_BUILD,
                        'Heap observation gates belong to an unsupported module/build')
                enabled = True
    if enabled:
        hooks = {(r[0], r[1], r[2]) for r in rows(patched / 'main.obs.tsv') if len(r) >= 3}
        require({('0x605d70', '22', 'aa1303e0'), ('0x6056f0', '23', 'b9410288')} <= hooks,
                'Incomplete or mismatched MK8 heap adapter')
    return enabled


def make_icon(icon, out):
    renderer = out / 'render-icon'
    run(['xcrun', 'swiftc', '-module-cache-path', out / 'swift-cache',
         ROOT / 'render_icon.swift', '-o', renderer])
    catalog = out / 'Assets.xcassets'
    brand = catalog / 'AppIcon.brandassets'
    info = {'author': 'xcode', 'version': 1}

    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + '\n')

    write(catalog / 'Contents.json', {'info': info})
    assets = []
    for name, size, scales in [('App Icon', '400x240', [1, 2]),
                               ('App Icon Large', '1280x768', [1])]:
        stack = brand / (name + '.imagestack')
        write(stack / 'Contents.json', {'info': info, 'layers': [
            {'filename': x + '.imagestacklayer'} for x in ('Front', 'Back')]})
        w, h = map(int, size.split('x'))
        for layer in ('Front', 'Back'):
            directory = stack / (layer + '.imagestacklayer')
            write(directory / 'Contents.json', {'info': info})
            images = directory / 'Content.imageset'
            images.mkdir(parents=True, exist_ok=True)
            entries = []
            for scale in scales:
                file = f'icon-{scale}x.png'
                run([renderer, icon, images / file, w * scale, h * scale, layer.lower()])
                entries.append({'idiom': 'tv', 'filename': file, 'scale': f'{scale}x'})
            write(images / 'Contents.json', {'info': info, 'images': entries})
        assets.append({'idiom': 'tv', 'size': size, 'filename': name + '.imagestack',
                       'role': 'primary-app-icon'})
    write(brand / 'Contents.json', {'info': info, 'assets': assets})


def prepare(args):
    runtime = runtime_check(args.runtime)
    profile, out, shaders = args.profile.resolve(), args.out.resolve(), args.shaders.resolve()
    data = profile_data(profile)
    title_id = data['title_id'][0][0]
    require(re.fullmatch(r'[0-9A-Fa-f]{16}', title_id) is not None, 'Invalid title ID')
    modules = data['modules'][0][0].split()
    require(all(re.fullmatch(r'[a-zA-Z0-9_]+', m) for m in modules), 'Invalid module names')
    require(not out.exists(), 'Project output already exists; choose a fresh directory')
    require(args.romfs.is_dir() and (args.romfs / data['romfs_marker'][0][0]).exists(),
            'RomFS is missing the profile marker')
    validate_shaders(runtime, profile, shaders)
    out.mkdir(parents=True)
    patched = args.patched.resolve() if args.patched else out / 'recovered'
    if args.ipa:
        ipa_info(args.ipa, patched)
    for module in modules:
        for suffix in ('patched.bin', 'fixups.tsv', 'gates.tsv'):
            require((patched / f'{module}.{suffix}').is_file(), f'Missing {module}.{suffix}')
    builds = {m: fixups(patched / f'{m}.fixups.tsv').get('BUILD_ID') for m in modules}
    registered = [dict(value.split('=', 1) for value in row[2:] if '=' in value)
                  for row in data.get('build', [])]
    require(any(all(row.get(m) == builds[m] for m in modules) for row in registered),
            'Module build IDs do not match a registered row in this profile. Use the derived profile for this version.')
    reclaim = memory_adapter_valid(patched, modules)
    gen = out / 'Gen'
    resources = gen / 'dm'
    resources.mkdir(parents=True)
    config = run([sys.executable, runtime / 'dm/ios/tools/gen_build.py', runtime / 'dm/host', gen],
                 env=game_env(profile), capture=True)
    flags = run([sys.executable, runtime / 'dm/ios/tools/gen_embed.py',
                 patched, gen / 'embed.S', *modules], capture=True)
    (gen / 'dm.xcconfig').write_text(config + '\nOTHER_LDFLAGS = $(inherited) $(DM_FRAMEWORKS) '
                                   + flags + '\n')
    for p in patched.glob('*.tsv'):
        shutil.copy2(p, resources / p.name)
    for name in ('shader_map.tsv', 'shader_pairs.tsv', 'pso_pairs.tsv',
                 'pso_manifest.tsv', 'c1_full.tsv', 'cb_extent.tsv'):
        if (profile / name).is_file():
            shutil.copy2(profile / name, resources / name)
    if args.prewarm:
        existing = (resources / 'pso_manifest.tsv').read_text().splitlines() if (resources / 'pso_manifest.tsv').exists() else []
        extra = args.prewarm.read_text().splitlines()
        (resources / 'pso_manifest.tsv').write_text('\n'.join(dict.fromkeys(existing + extra)) + '\n')
    font = resources / 'font'
    font.mkdir()
    require(args.standard_font.is_file(), 'Supply --standard-font with a local TTF font')
    # System fonts can carry restricted flags; copy bytes, not filesystem metadata.
    shutil.copyfile(args.standard_font, font / 'standard.ttf')
    run([sys.executable, runtime / 'dm/tools/make_ext_font.py', font / 'nintendo_ext.ttf'])
    env = {'DM_OPMODE': str(args.opmode), 'DM_PRESENT_HEIGHT': str(args.height),
           'DM_RENDER_SCALE': '1.0', 'DM_LAUNCHER': '0', 'DM_PSO_PREBUILD': '0',
           'DM_PSO_PREPARE': '1', 'DM_PSO_PREPARE_THREADS': '2', 'DM_NVN_PREDICT': '0',
           'DM_BC_MAX_DIM': str(args.texture_limit), 'DM_HEAP_RECLAIM': str(int(reclaim)),
           'DM_SHOW_FPS': '1'}
    (resources / 'env.txt').write_text('# Native handheld mode; tvOS presentation cap.\n'
                                     + ''.join(f'{k}={v}\n' for k, v in env.items()))
    (out / 'launcher_stub.c').write_text('#include <stdbool.h>\n'
        'bool dm_show_launcher(void *vc, void (*done)(void)) {(void)vc;(void)done;return false;}\n')
    icon = args.icon.resolve() if args.icon else None
    if icon:
        require(icon.is_file(), 'Icon file does not exist')
        make_icon(icon, out)
    bundle = args.bundle_id or 'org.nxnative.tv.g' + title_id.lower()
    require(re.fullmatch(r'[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+', bundle) is not None,
            'Invalid bundle identifier')
    sources = [{'path': str(runtime / 'dm/ios/Sources'), 'excludes': ['*.swift']},
               {'path': 'Gen/dm', 'type': 'folder', 'buildPhase': 'resources'},
               {'path': 'Gen/embed.S'}, {'path': 'Gen/src'},
               {'path': 'Gen/main/main.c', 'compilerFlags': '-Dmain=dm_main'},
               {'path': 'launcher_stub.c'}]
    settings = {'PRODUCT_BUNDLE_IDENTIFIER': bundle, 'CODE_SIGN_STYLE': 'Automatic',
                'PRODUCT_NAME': 'NativeGameTV', 'CURRENT_PROJECT_VERSION': '1',
                'MARKETING_VERSION': args.version, 'TARGETED_DEVICE_FAMILY': '3',
                'GCC_WARN_INHIBIT_ALL_WARNINGS': 'YES', 'CLANG_ENABLE_OBJC_ARC': 'YES',
                'GCC_C_LANGUAGE_STANDARD': 'gnu11', 'OTHER_CFLAGS': '$(inherited) -mcpu=apple-a15',
                'ENABLE_USER_SCRIPT_SANDBOXING': 'NO'}
    if args.team:
        require(re.fullmatch(r'[A-Z0-9]{10}', args.team) is not None, 'Invalid team ID')
        settings['DEVELOPMENT_TEAM'] = args.team
    if icon:
        sources.append({'path': 'Assets.xcassets'})
        settings['ASSETCATALOG_COMPILER_APPICON_NAME'] = 'AppIcon'
    properties = {'CFBundleDisplayName': args.name or data.get('name', [[title_id]])[0][0],
                  'DMLanguage': args.language, 'DMShowFPS': True, 'LSSupportsGameMode': True,
                  'GCSupportsControllerUserInteraction': True,
                  'GCSupportedGameControllers': [{'ProfileName': 'ExtendedGamepad'}],
                  'UIApplicationSceneManifest': {'UIApplicationSupportsMultipleScenes': False,
                    'UISceneConfigurations': {'UIWindowSceneSessionRoleApplication': [{
                    'UISceneConfigurationName': 'Default', 'UISceneDelegateClassName': 'SceneDelegate'}]}}}
    spec = {'name': 'NativeGameTV', 'options': {'deploymentTarget': {'tvOS': '27.0'}},
            'configFiles': {'Release': 'Gen/dm.xcconfig'}, 'targets': {'NativeGameTV': {
            'type': 'application', 'platform': 'tvOS', 'sources': sources,
            'info': {'path': 'Gen/Info.plist', 'properties': properties}, 'settings': settings,
            'dependencies': [{'sdk': x} for x in ('UIKit.framework', 'AVFAudio.framework', 'libz.tbd')]}}}
    # JSON is a YAML subset; XcodeGen accepts it without a third-party Python dependency.
    (out / 'project.yml').write_text(json.dumps(spec, indent=2) + '\n')
    plan = {'format': 1, 'bundle_id': bundle, 'runtime': str(runtime), 'profile': str(profile),
            'shader_chain': MANIFEST['shader_chain'], 'shaders': str(shaders),
            'romfs': str(args.romfs.resolve()), 'aoc': str(args.aoc.resolve()) if args.aoc else None,
            'memory_adapter': 'mk8dx-4.0.1' if reclaim else None,
            'module_builds': builds,
            'env': env}
    (out / 'tv-build.json').write_text(json.dumps(plan, indent=2) + '\n')
    run(['xcodegen', 'generate', '--spec', out / 'project.yml'], cwd=out)
    print(f'Prepared {out}\nHeap reclaim: {reclaim}\nNext: build --project "{out}"')


def pack_shaders(source, output):
    files = sorted(p for p in source.iterdir() if p.is_file() and p.suffix in ('.metallib', '.json', '.tsv'))
    require(files, 'No shader assets to pack')
    require(not output.exists(), f'Pack already exists: {output}')
    total = sum(p.stat().st_size for p in files)
    temporary = output.with_name(output.name + '.partial')
    with temporary.open('wb') as target:
        target.write(b'NXTVSH01' + struct.pack('<IQ', len(files), total))
        for p in files:
            name = p.name.encode()
            require(len(name) < 256 and not p.is_symlink(), 'Invalid shader asset name')
            crc = 0
            with p.open('rb') as f:
                while chunk := f.read(1024 * 1024):
                    crc = zlib.crc32(chunk, crc)
            target.write(struct.pack('<HQI', len(name), p.stat().st_size, crc))
            target.write(name)
            with p.open('rb') as f:
                shutil.copyfileobj(f, target, 1024 * 1024)
    temporary.replace(output)


def build(args):
    project = args.project.resolve()
    require((project / 'tv-build.json').is_file(), 'Not a generated TV project')
    runtime_check(json.loads((project / 'tv-build.json').read_text())['runtime'])
    cmd = ['xcodebuild', '-quiet', '-jobs', args.jobs, '-project', project / 'NativeGameTV.xcodeproj',
           '-scheme', 'NativeGameTV', '-configuration', 'Release', '-sdk', 'appletvos',
           '-destination', 'generic/platform=tvOS', '-derivedDataPath', project / 'derived-data']
    if args.signed:
        cmd += ['-allowProvisioningUpdates']
    else:
        cmd += ['CODE_SIGNING_ALLOWED=NO']
    run(cmd + ['build'])
    print('Built: ' + str(project / 'derived-data/Build/Products/Release-appletvos/NativeGameTV.app'))


def deploy(args):
    project = args.project.resolve()
    plan = json.loads((project / 'tv-build.json').read_text())
    app = project / 'derived-data/Build/Products/Release-appletvos/NativeGameTV.app'
    require(app.is_dir(), 'Build the signed app first')
    run(['codesign', '--verify', '--strict', app])
    ent = run(['codesign', '-d', '--entitlements', ':-', app], capture=True)
    require('application-identifier' in ent and (app / 'embedded.mobileprovision').exists(),
            'App needs a development provisioning profile; build --signed first')
    with (app / 'Info.plist').open('rb') as f:
        require(plistlib.load(f)['CFBundleIdentifier'] == plan['bundle_id'], 'Bundle ID differs')
    device = args.device
    run(['xcrun', 'devicectl', 'device', 'install', 'app', '--device', device, app])
    base = ['xcrun', 'devicectl', 'device', 'copy', 'to', '--device', device,
            '--domain-type', 'appDataContainer', '--domain-identifier', plan['bundle_id']]
    pack = project / ('shaders-' + plan['shader_chain'] + '.pak')
    if not pack.exists():
        pack_shaders(Path(plan['shaders']), pack)
    run(base + ['--source', pack, '--destination', 'Library/Caches/' + pack.name])
    run(base + ['--source', plan['romfs'], '--destination', 'Library/Caches/romfs'])
    if plan.get('aoc'):
        run(base + ['--source', plan['aoc'], '--destination', 'Library/Caches/aoc'])
    if args.launch:
        run(['xcrun', 'devicectl', 'device', 'process', 'launch', '--device', device,
             '--terminate-existing', plan['bundle_id']])


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    b = commands.add_parser('bootstrap', help='Clone pinned nxnative and apply the TV patch')
    b.add_argument('--out', type=Path, required=True)
    b.add_argument('--upstream', default=MANIFEST['upstream'], help=argparse.SUPPRESS)
    i = commands.add_parser('inspect-ipa', help='Inspect an nxnative IPA without running it')
    i.add_argument('ipa', type=Path)
    for name in ('rewrite', 'translate', 'prepare'):
        c = commands.add_parser(name)
        c.add_argument('--runtime', type=Path, required=True)
        c.add_argument('--profile', type=Path, required=True)
        c.add_argument('--out', type=Path, required=True)
        if name == 'rewrite':
            c.add_argument('--exefs', type=Path, required=True)
            c.add_argument('--mk8-memory', action='store_true')
        elif name == 'translate':
            c.add_argument('--shader-package', type=Path, required=True)
            c.add_argument('--cache', type=Path, required=True)
            c.add_argument('--jobs', type=int, default=8)
        else:
            inputs = c.add_mutually_exclusive_group(required=True)
            inputs.add_argument('--patched', type=Path)
            inputs.add_argument('--ipa', type=Path)
            c.add_argument('--romfs', type=Path, required=True)
            c.add_argument('--shaders', type=Path, required=True)
            c.add_argument('--aoc', type=Path)
            c.add_argument('--icon', type=Path)
            c.add_argument('--prewarm', type=Path)
            c.add_argument('--standard-font', type=Path,
                           default=Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf'))
            c.add_argument('--team')
            c.add_argument('--bundle-id')
            c.add_argument('--name')
            c.add_argument('--version', default='1.0')
            c.add_argument('--language', default='en-US')
            c.add_argument('--height', type=int, choices=(720, 1080), default=720)
            c.add_argument('--opmode', type=int, choices=(0, 1), default=0)
            c.add_argument('--texture-limit', type=int, choices=(0, 128, 256, 512), default=256)
    for name in ('build', 'deploy'):
        c = commands.add_parser(name)
        c.add_argument('--project', type=Path, required=True)
        if name == 'build':
            c.add_argument('--signed', action='store_true')
            c.add_argument('--jobs', type=int, default=8)
        else:
            c.add_argument('--device', required=True, help='Paired Apple TV identifier from xcrun devicectl list devices')
            c.add_argument('--launch', action='store_true')
    return p


def main():
    args = parser().parse_args()
    try:
        if args.command == 'inspect-ipa':
            print(json.dumps(ipa_info(args.ipa), indent=2))
        else:
            globals()[args.command](args)
    except (BuildError, OSError, ValueError, KeyError, subprocess.CalledProcessError,
            zipfile.BadZipFile, struct.error) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
