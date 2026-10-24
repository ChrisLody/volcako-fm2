#!/usr/bin/env python3
"""Patch Volcako's source/sysex.cpp for FM2 operator mute support.

MIT-licensed modification to the original Volcako project by Luomu.
Run: python patch_volcako.py path/to/volcako
"""
from pathlib import Path
import sys

OLD_SERIALISE = '''    op1.to_message(m);
    op2.to_message(m);
    op3.to_message(m);
    op4.to_message(m);
    op5.to_message(m);
    op6.to_message(m);
'''
NEW_SERIALISE = OLD_SERIALISE + '''
    // Volca FM2 / DX7 compatibility mode uses a standard DX7 voice dump.
    // Unlike the original Volca FM, this dump has NO operator on/off byte.
    // Match Synthmata FM2: transmit level 0 for muted operators, while
    // retaining their real levels in the editor for instant restoration.
    // Do this before calculating the Yamaha checksum below.
    // Source operator order is 1..6; on-wire DX7 order is 6..1.
    if (dx7_compat)
    {
        if (!op1.is_on) m[SX_OP1_OUTPUT_LEVEL] = 0;
        if (!op2.is_on) m[SX_OP2_OUTPUT_LEVEL] = 0;
        if (!op3.is_on) m[SX_OP3_OUTPUT_LEVEL] = 0;
        if (!op4.is_on) m[SX_OP4_OUTPUT_LEVEL] = 0;
        if (!op5.is_on) m[SX_OP5_OUTPUT_LEVEL] = 0;
        if (!op6.is_on) m[SX_OP6_OUTPUT_LEVEL] = 0;
    }
'''
OLD_CHECKSUM = '''    return 128 - sum;
'''
NEW_CHECKSUM = '''    // MIDI data/checksums must be 7-bit; the old code returned 128 for
    // a zero-modulo-128 sum, which would produce an invalid SysEx byte.
    return (128 - sum) & 0x7f;
'''

def patch_file(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"Cannot locate Volcako source file: {path}")
    source = path.read_text(encoding='utf-8')
    if NEW_SERIALISE in source and NEW_CHECKSUM in source:
        print('Already patched:', path)
        return
    if source.count(OLD_SERIALISE) != 1 or source.count(OLD_CHECKSUM) != 1:
        raise RuntimeError('Original source differs from expected version. No changes made.')
    updated = source.replace(OLD_SERIALISE, NEW_SERIALISE, 1).replace(OLD_CHECKSUM, NEW_CHECKSUM, 1)
    backup = path.with_suffix(path.suffix + '.original')
    if not backup.exists():
        backup.write_text(source, encoding='utf-8')
    path.write_text(updated, encoding='utf-8')
    print('Patched:', path)
    print('Original source backup:', backup)

if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise RuntimeError('Usage: python patch_volcako.py <volcako-source-folder>')
        patch_file(Path(sys.argv[1]) / 'source' / 'sysex.cpp')
    except Exception as e:
        print('ERROR:', e, file=sys.stderr)
        sys.exit(1)
