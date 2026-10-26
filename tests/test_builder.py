import json
from pathlib import Path
import plistlib
import re
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import tv_builder as tv


def executable(sections, encrypted=False):
    """Small synthetic file with the same embedding contract as nxnative."""
    sizes = [72 + 80 for _ in sections]
    total = sum(sizes) + (24 if encrypted else 0)
    offset = 32 + total
    commands, payload = [], []
    for (segment, name), data in sections.items():
        seg = struct.pack('<II16sQQQQIIII', 0x19, 152, segment.encode(),
                          0, len(data), offset, len(data), 7, 5, 1, 0)
        sec = struct.pack('<16s16sQQIIIIIIII', name.encode(), segment.encode(),
                          0, len(data), offset, 0, 0, 0, 0, 0, 0, 0)
        commands.append(seg + sec)
        payload.append(data)
        offset += len(data)
    if encrypted:
        commands.append(struct.pack('<6I', 0x2C, 24, 0, 0, 1, 0))
    return struct.pack('<8I', 0xFEEDFACF, 0x0100000C, 0, 2, len(commands), total, 0, 0) + b''.join(commands + payload)


class ArchiveTests(unittest.TestCase):
    def test_paths_and_symlinks_rejected(self):
        for name, mode in [('../secret', 0), ('/absolute', 0),
                           ('Payload\\escape', 0), ('link', 0o120777 << 16)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                path = Path(d) / 'bad.zip'
                with zipfile.ZipFile(path, 'w') as z:
                    info = zipfile.ZipInfo(name)
                    info.external_attr = mode
                    z.writestr(info, 'x')
                with zipfile.ZipFile(path) as z, self.assertRaises(tv.BuildError):
                    tv.safe_members(z)

    def test_ipa_recovery_preserves_section_bytes(self):
        # Include a BSS/zero region in the W image and a padded patch tail.
        pieces = {('__TEXT', '__dmmainx0'): b'T' * tv.PAGE,
                  ('__DMmainW', '__img'): bytes(tv.PAGE),
                  ('__TEXT', '__dmmainx1'): b'P' * tv.PAGE}
        for po, extra in [('0x8000', ''), ('-0x2000000', '# FILE_IMAGE_LEN\t0x8000\n')]:
            with self.subTest(patch_offset=po), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                with zipfile.ZipFile(root / 'game.ipa', 'w') as z:
                    prefix = 'Payload/Test.app/'
                    z.writestr(prefix + 'Info.plist', plistlib.dumps({'CFBundleExecutable': 'Test'}))
                    z.writestr(prefix + 'Test', executable(pieces))
                    z.writestr(prefix + 'dm/main.fixups.tsv',
                               f'# PATCH_OFF\t{po}\n# TEXT_END\t0x4000\n# BUILD_ID\t1234\n' + extra)
                    z.writestr(prefix + 'dm/main.gates.tsv', '# test\n')
                result = tv.ipa_info(root / 'game.ipa', root / 'recovered')
                self.assertEqual(result['modules'], {'main': '1234'})
                self.assertEqual((root / 'recovered/main.patched.bin').read_bytes(), b''.join(pieces.values()))


class MachOTests(unittest.TestCase):
    def test_encrypted_executable_rejected(self):
        with self.assertRaisesRegex(tv.BuildError, 'Encrypted App Store'):
            tv.macho_sections(executable({('__TEXT', '__dmmainx0'): b'test'}, encrypted=True))

    def test_truncated_command_rejected(self):
        data = executable({('__TEXT', '__dmmainx0'): b'test'})
        with self.assertRaises(tv.BuildError):
            tv.macho_sections(data[:50])

    def test_section_out_of_bounds_rejected(self):
        data = bytearray(executable({('__TEXT', '__dmmainx0'): b'test'}))
        struct.pack_into('<I', data, 32 + 72 + 48, 0xFFFFFFF0)
        with self.assertRaisesRegex(tv.BuildError, 'beyond executable'):
            tv.macho_sections(data)


class MemoryAdapterTests(unittest.TestCase):
    def write_fixture(self, directory, build, hooks):
        (directory / 'main.fixups.tsv').write_text('# BUILD_ID\t' + build + '\n')
        (directory / 'main.obs.tsv').write_text(hooks)

    def test_foreign_build_cannot_enable_heap_callbacks(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self.write_fixture(p, 'other-build', '0x6056f0\t23\tb9410288\n')
            with self.assertRaisesRegex(tv.BuildError, 'unsupported module/build'):
                tv.memory_adapter_valid(p, ['main'])

    def test_partial_adapter_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self.write_fixture(p, tv.MK_BUILD, '0x6056f0\t23\tb9410288\n')
            with self.assertRaisesRegex(tv.BuildError, 'Incomplete'):
                tv.memory_adapter_valid(p, ['main'])

    def test_complete_adapter_and_disabled_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            self.write_fixture(p, tv.MK_BUILD, '')
            self.assertFalse(tv.memory_adapter_valid(p, ['main']))
            self.write_fixture(p, tv.MK_BUILD,
                '0x605d70\t22\taa1303e0\n0x6056f0\t23\tb9410288\n')
            self.assertTrue(tv.memory_adapter_valid(p, ['main']))


class PackTests(unittest.TestCase):
    def test_streamed_pack_round_trip_and_crc(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / 'source'
            source.mkdir()
            files = {'a_b.metallib': tv.SOURCE_MARKER + b'kernel void test() {}',
                     'inputs.tsv': b'game\t1234\n', 'a_b.json': b'{}'}
            for name, data in files.items():
                (source / name).write_bytes(data)
            output = root / 'shaders.pak'
            tv.pack_shaders(source, output)
            with output.open('rb') as f:
                self.assertEqual(f.read(8), b'NXTVSH01')
                count, size = struct.unpack('<IQ', f.read(12))
                self.assertEqual(count, len(files))
                self.assertEqual(size, sum(map(len, files.values())))
                got = {}
                for _ in range(count):
                    n, length, crc = struct.unpack('<HQI', f.read(14))
                    name = f.read(n).decode()
                    data = f.read(length)
                    self.assertEqual(zlib.crc32(data), crc)
                    got[name] = data
                self.assertEqual(f.read(), b'')
                self.assertEqual(got, files)
            with self.assertRaises(tv.BuildError):
                tv.pack_shaders(source, output)


class DistributionTests(unittest.TestCase):
    def test_runtime_patch_checksum(self):
        self.assertEqual(tv.sha(tv.ROOT / 'patches/tvos-runtime.patch'), tv.MANIFEST['patch_sha256'])

    def test_no_game_keys_or_local_identity_in_distribution(self):
        for p in tv.ROOT.rglob('*'):
            if not p.is_file() or '.git' in p.parts or '__pycache__' in p.parts or p == Path(__file__).resolve():
                continue
            if p.suffix in ('.nsp', '.xci', '.ipa', '.p12', '.keys', '.mobileprovision'):
                self.fail(f'Private asset in distribution: {p.name}')
            self.assertIsNone(re.search(r'/Users/[^/\s]+/', p.read_text(errors='replace')), str(p))


if __name__ == '__main__':
    unittest.main()
