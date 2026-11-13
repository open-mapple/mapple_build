#!/usr/bin/env python3
"""android-audio-dump.py: the realme 8i's audio state under Android, read only, for the kernel's audio code.

  android-audio-dump.py OUT_DIR pa                which speaker amplifier is fitted (I2C3 0x58 / 0x28; dmesg's probe
                                                  lines, so right after Android boots)
  android-audio-dump.py OUT_DIR vibr              the motor rail's voltage (the vibr regulator, the vibrator's vmax, VIBR)
  android-audio-dump.py OUT_DIR playing LABEL     while Android plays on the speaker (LABEL: max, half, ...):
                                                  the AW87319's enable, codec 0x2200-0x2492, AFE, clocks, pins, mixer
  android-audio-dump.py selftest                  the command check, without a phone

Needs adb and a rooted Android (su). Every command sent goes through checked() and must be one of these forms:
  cat /sys/... or /proc/...                       (debugfs is under /sys/kernel/debug)
  dmesg                                           (alone: no flags)
  echo HEX > PMIC_ACCESS; cat PMIC_ACCESS         one argument only: an address alone is a read, "address value" writes
  dumpsys SERVICE
Anything else is refused. --pmic-access PATH if the node is not /sys/devices/platform/mt-pmic/pmic_access.
debugfs must be mounted already (mounting is not a read: done by hand if /proc/mounts lacks it); realme's kernel has
none, so those reads are skipped there. No i2c-dev either: the AW87319's settings are /odm/firmware/aw87319_spk.bin.
"""
import os
import re
import subprocess
import sys

PMIC_ACCESS = '/sys/devices/platform/1000d000.pwrap/1000d000.pwrap:mt6358-pmic/mt-pmic/pmic_access'
PATH = r'/(?:sys|proc)/(?:[A-Za-z0-9_.*:,-]+/)*[A-Za-z0-9_.*:,-]+'


def forms(pmic):
    p = re.escape(pmic)
    return [
        re.compile(r'cat ' + PATH),
        re.compile(r'dmesg'),
        re.compile(r'echo [0-9a-f]{1,4} > ' + p),
        re.compile(r'dumpsys [A-Za-z0-9_.]+'),
    ]


def checked(command, pmic=PMIC_ACCESS):
    """COMMAND if every '; '-separated part is a read form (and no part climbs out with ..), else SystemExit."""
    ok = re.fullmatch(r'[A-Za-z0-9_./*:,> ;-]+', command) is not None and '..' not in command
    for part in command.split('; '):
        ok = ok and any(f.fullmatch(part) for f in forms(pmic))
        if part.startswith('echo '):
            ok = ok and len(part.split()) == 4      # echo, one hex address, >, the node
    if not ok:
        raise SystemExit('refused (not a read form): ' + command)
    return command


def adb(command, pmic):
    r = subprocess.run(['adb', 'shell', "su -c '%s'" % checked(command, pmic)], capture_output=True, text=True)
    return r.stdout + r.stderr


def pmic_reads(addresses, pmic):
    """Each address written alone to pmic_access (a read) and the node read back: 'addr value' lines."""
    out = []
    for i in range(0, len(addresses), 32):
        batch = addresses[i:i + 32]
        text = adb('; '.join('echo %04x > %s; cat %s' % (a, pmic, pmic) for a in batch), pmic).split()
        values = [t for t in text if t.startswith('0x')]
        out += ['%04x %s' % (a, v) for a, v in zip(batch, values)]
        if len(values) != len(batch):
            out.append('(%d of %d values came back: %s)' % (len(values), len(batch), ' '.join(text)[:200]))
    return '\n'.join(out) + '\n'


def keep(text, pattern):
    return ''.join(line + '\n' for line in text.splitlines() if re.search(pattern, line, re.I))


def debugfs_mounted(pmic):
    if 'debugfs' not in adb('cat /proc/filesystems', pmic):
        print('this kernel has no debugfs: the debugfs reads are skipped')
        return False
    if ' debugfs ' not in adb('cat /proc/mounts', pmic):
        print('debugfs is not mounted: mount it by hand (mount -t debugfs none /sys/kernel/debug) for the debugfs reads')
        return False
    return True


def save(out, name, text):
    with open(os.path.join(out, name), 'w') as f:
        f.write(text)
    print('%s: %d lines' % (os.path.join(out, name), text.count('\n')))


def pa(out, pmic):
    text = ''
    for d in ('3-0058', '3-0028'):
        text += '%s name: %s' % (d, adb('cat /sys/bus/i2c/devices/%s/name' % d, pmic))
        text += '%s uevent:\n%s' % (d, adb('cat /sys/bus/i2c/devices/%s/uevent' % d, pmic))
    text += 'dmesg:\n' + keep(adb('dmesg', pmic), r'aw873|sia81|3-0058|3-0028|i2c.*(ack|err)')
    save(out, 'pa.txt', text)


def vibr(out, pmic):
    text = ''
    if debugfs_mounted(pmic):
        text += keep(adb('cat /sys/kernel/debug/regulator/regulator_summary', pmic), r'vibr|regulator\s+use')
    for r in range(40):
        d = '/sys/class/regulator/regulator.%d' % r
        if adb('cat %s/name' % d, pmic).strip() == 'vibr':
            text += 'regulator vibr: %s' % adb('cat %s/microvolts; cat %s/state' % (d, d), pmic)
    text += 'vibrator vmax, vmax_max: %s' % adb('cat /sys/class/leds/vibrator/vmax; cat /sys/class/leds/vibrator/vmax_max', pmic)
    text += 'pmic (0x1e44 [11:8] = VOSEL: 9 = 2.8 V, 11 = 3.0 V, 13 = 3.3 V):\n'
    text += pmic_reads([0x1e44, 0x1d08, 0x1d0a, 0x1d16, 0x1a56], pmic)
    save(out, 'vibr.txt', text)


def playing(out, label, pmic):
    save(out, 'pa-%s.txt' % label, 'aw87319 hwen: ' + adb('cat /sys/bus/i2c/devices/3-0058/hwen', pmic))
    save(out, 'codec-%s.txt' % label, pmic_reads([0x0008, 0x000a, 0x07ac, 0x1ad2, 0x1aaa] + list(range(0x2200, 0x2494, 2)), pmic))
    if debugfs_mounted(pmic):
        save(out, 'afe-%s.txt' % label, adb('cat /sys/kernel/debug/mtksocaudio', pmic))
        save(out, 'codec-debugfs-%s.txt' % label, adb('cat /sys/kernel/debug/mtksocanaaudio', pmic))
        save(out, 'clk-%s.txt' % label, keep(adb('cat /sys/kernel/debug/clk/clk_summary', pmic), r'aud|apll|i2c|clock\s+enable'))
    save(out, 'pins-%s.txt' % label, keep(adb('cat /sys/devices/platform/*pinctrl*/mt_gpio', pmic), r'^\s*(164|183|184|185|186)\s*:'))
    save(out, 'mixer-%s.txt' % label, adb('cat /proc/asound/cards', pmic) + adb('dumpsys media.audio_flinger', pmic) +
         keep(adb('dumpsys audio', pmic), r'speaker|route|device|volume'))


def selftest():
    good = ['cat /sys/kernel/debug/mtksocaudio', 'cat /proc/asound/cards', 'cat /sys/devices/platform/*pinctrl*/mt_gpio',
            'dmesg', 'echo 1e44 > %s; cat %s' % (PMIC_ACCESS, PMIC_ACCESS), 'dumpsys media.audio_flinger']
    bad = ['echo 1e44 0b00 > %s' % PMIC_ACCESS, 'echo 1e44 > /sys/class/leds/vibrator/activate', 'i2cset -f -y 3 0x58 0x01 0x07', 'i2cget -f -y 3 0x58 0x00', 'dmesg -c', 'dmesg -C',
           'cat /sys/../data/x', 'cat /data/local/tmp/x', 'cat /sys/x; reboot', 'cat /sys/x && reboot', 'cat /sys/x | sh',
           "cat /sys/x'; reboot '", 'cat /sys/x > /sys/y', 'mount -t debugfs none /sys/kernel/debug', 'dumpsys audio; setprop a b',
           'echo 1e44 > %s; echo 1e44 > /sys/y' % PMIC_ACCESS, 'cat $(reboot)', 'cat /sys/`id`']
    fails = 0
    for c in good:
        try:
            checked(c)
        except SystemExit:
            print('FAILED: refused', c)
            fails += 1
    for c in bad:
        try:
            checked(c)
            print('FAILED: accepted', c)
            fails += 1
        except SystemExit:
            pass
    print('android-audio-dump selftest: %s' % ('FAILED' if fails else 'ok'))
    return fails == 0


def main():
    args = sys.argv[1:]
    pmic = PMIC_ACCESS
    if '--pmic-access' in args:
        i = args.index('--pmic-access')
        pmic = args[i + 1]
        del args[i:i + 2]
        if not re.fullmatch(r'/sys/(?:[A-Za-z0-9_.:,-]+/)+pmic_access', pmic):
            sys.exit('--pmic-access: a /sys path ending in pmic_access')
    if args == ['selftest']:
        sys.exit(0 if selftest() else 1)
    if len(args) < 2 or args[1] not in ('pa', 'vibr', 'playing') or (args[1] == 'playing') != (len(args) == 3):
        sys.exit(__doc__)
    out = args[0]
    os.makedirs(out, exist_ok=True)
    if args[1] == 'pa':
        pa(out, pmic)
    elif args[1] == 'vibr':
        vibr(out, pmic)
    else:
        if not re.fullmatch(r'[a-z0-9-]+', args[2]):
            sys.exit('LABEL: lower-case letters, digits, -')
        playing(out, args[2], pmic)


if __name__ == '__main__':
    main()
