#!/usr/bin/env python3
"""Build the Apple Device Tree an Open Mapple kernel boots with.

Usage:  mkadt.py OUT.adt --soc=SOC.py --device=ADT.py [--dump] [--ufs] [--haptics|--audio] [--torch] [--camera]
                 [--resize] [--canvas=WxH]
The identity is this line's iPhone (release 1: the iPhone X, D22AP); SOC.py (tools/soc/) gives the SoC's blocks and
the names its kernel line matches; ADT.py (the device repository's) gives the phone's RAM, framebuffer, synthetic ECID,
About identity (required) and its own /arm-io devices.
"""
import hashlib, importlib.util, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adt  # noqa: E402

# Panic log buffer, carved from the TOP of the dram range and exposed to XNU as
# /pram + /chosen/embedded-panic-log-size. Without it, pe_init.c's
# check_for_panic_log() leaves gPanicBase == 0, debug_log_init() bails out twice
# and the kernel hangs right after "kdp_core zlib memory" — reproduced on QEMU
# and independently on ios-fajita.
PANIC_LOG_SIZE = 0x100000
# /arm-io children are relative to this; it must not be 0 (serial_init bails)
SOC_BASE = 0x01000000

# --canvas=WxH: the display size IOMobileFramebuffer reads from /arm-io/disp0 (default the presented iPhone X
# size 1125x2436; must match what AppleCLCD reports: 1080x2400 with boot-arg mp_fb_native=1).
CANVAS = [1125, 2436]

BOOT_MANIFEST_HASH = hashlib.sha384(b'sweet boot manifest').digest()

# The iOS userland is the iPhone X (iPhone10,3 / D22AP) IPSW. MobileGestalt
# derives product type, device class, home-button-less UI, artwork etc. from
# the root identity and /product of the real D22AP device tree. That tree is
# an Apple artifact (kept out of git): extract it from the IPSW with
#   tools/remotezip.py URL DeviceTree.d22ap.im4p DIR
#   ipsw img4 im4p extract DIR/Firmware/all_flash/DeviceTree.d22ap.im4p -o d22ap.adt
D22AP_ADT = os.environ.get('D22AP_ADT') or os.path.join(os.path.dirname(os.path.abspath(__file__)), '../research/ipsw-x/d22ap.adt')


def d22ap_product(path=D22AP_ADT):
    """/product properties of the real D22AP tree, without the syscfg
    placeholders iBoot would fill in (flag bit set) and the node bookkeeping."""
    if not os.path.exists(path):
        print(f'warning: {path} missing, no /product node', file=sys.stderr)
        return None
    root, _ = adt.parse(open(path, 'rb').read())
    for c in root.children:
        if dict((k, v) for k, f, v in c.props).get('name') == b'product\0':
            return [(k, v) for k, f, v in c.props if not f and k not in ('name', 'AAPL,phandle')]
    raise SystemExit(f'{path}: no /product node')

u32 = lambda *v: struct.pack('<%dI' % len(v), *v)
u64 = lambda *v: struct.pack('<%dQ' % len(v), *v)
cstr = lambda s: s.encode() + b'\0'
io = lambda pa, size: u64(pa - SOC_BASE, size)


def node(name, props=(), children=()):
    n = adt.Node()
    n.props = [('name', 0, cstr(name))] + [(k, 0, v) for k, v in props]
    n.children = list(children)
    return n


# Linux IRQ_TYPE_* in the second interrupt cell (MappleGIC.cpp)
IRQ_EDGE_RISING, IRQ_EDGE_FALLING, IRQ_LEVEL_HIGH = 1, 2, 4
# HID consumer-page usages (IOHIDUsageTables.h)
HID_CSMR_POWER, HID_CSMR_VOLUME_UP, HID_CSMR_VOLUME_DOWN = 0x30, 0xe9, 0xea


def spi(n, flags):
    """DT <GIC_SPI n flags> -> the ADT's <INTID flags>."""
    return u32(32 + n, flags)


def wifi_mac(ecid):
    """Synthetic, locally administered WiFi MAC (/chosen mac-address-wifi0, the wlan node, en0)."""
    return bytes([0x02]) + struct.pack('>Q', ecid)[3:7] + b'\x01'


def load(kind, path, names):
    """The SoC or device file at path; each of names must be there and not empty."""
    spec = importlib.util.spec_from_file_location(f'mkadt_{kind}', path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    for n in names:
        if getattr(m, n, None) in (None, '', (), [], {}):
            sys.exit(f'mkadt.py: {path}: {n} is missing or empty')
    return m


SOC_KEYS = ('NAMES', 'ROOT_COMPAT', 'GICD', 'GICR', 'TIMEBASE', 'VTIMER_INTID', 'CPU_COMPAT', 'CPU_HZ', 'BIG_FROM', 'UFS')
DEVICE_KEYS = ('RAM', 'FB', 'ECID', 'PRODUCT_NAME', 'DISPLAY_GAMUT', 'ABOUT', 'devices')


def build(soc, dev, seed=None, ufs=False, audio=False, haptics=False, torch=False, camera=False, resize=False):
    """ufs: the /<ufs> node, the UFS host the phone's boot loader leaves running; without it the kernel never touches
    storage. audio, haptics, torch, camera: the device's optional parts (device.conf's adt flags)."""
    names = soc.NAMES
    phys_base, mem_size = dev.RAM
    # Carve the panic log off the top so the kernel never allocates over it.
    panic_base = phys_base + mem_size - PANIC_LOG_SIZE
    mem_size -= PANIC_LOG_SIZE
    ecid = dev.ECID
    seed = seed if seed is not None else bytes(64)    # the loader writes a fresh one at every boot
    phandle = iter(range(1, 1000))
    ph = lambda: ('AAPL,phandle', u32(next(phandle)))

    chosen = node('chosen', [
        ph(),
        ('dram-base', u64(phys_base)),
        ('dram-size', u64(mem_size)),
        ('random-seed', seed),
        ('debug-enabled', u32(1)),
        ('embedded-panic-log-size', u32(PANIC_LOG_SIZE)),
        # --resize (device.conf's resize): the display pipe's resizer shows the whole picture at its own aspect under a
        # black strip; the kernel's mp_fb_rsz overrides it.
        *((('mapple-resize', u32(1)),) if resize else ()),
        ('firmware-version', cstr(names['firmware'])),
        ('system-firmware-version', cstr(names['firmware'])),
        # iBoot's SHA-384 of the boot manifest; /sbin/mount -P 1 exits unless
        # it exists (bind mounts /private/preboot/<hex>/usr/standalone/firmware,
        # tolerating a missing directory). Fixed stand-in value.
        ('boot-manifest-hash', BOOT_MANIFEST_HASH),
        # libimg4 (cryptexd) aborts "failed to get crypto algorithm" without
        # it; real A11+ device trees say sha2-384.
        ('crypto-hash-method', cstr('sha2-384')),
        # MobileGestalt's screen class: display-scale (u32, default 0) comes
        # from /chosen, the pixel size from IOMobileFramebuffer. Scale 0 makes
        # SpringBoard's FBSDisplayMode "infxinf@0x" invalid. 1080x2400 @3x =
        # 360x800 points (like iPhone X class devices).
        ('display-scale', u32(3)),
        # XNU derives IOPlatformUUID (gethostuuid) from these on arm; without
        # them CFPreferences cannot use kCFPreferencesCurrentHost (no ByHost
        # files), so e.g. DataMigration never records LastSystemVersion and
        # migrates on every boot. Synthetic ECID (not the phone's); chip-id
        # of the A11 that the iPhone X userland expects.
        ('unique-chip-id', struct.pack('<Q', ecid)),
        ('chip-id', u32(0x8015)),
        # Security status as on a dev-fused, undemoted board (tuple 0,0,1,1):
        # MobileGestalt reads these from /chosen, and mobileactivationd's
        # DeviceType then takes its native "Hactivation" branch (state
        # Activated without an activation record; no SEP, no baseband here).
        ('certificate-production-status', u32(0)),
        ('effective-production-status-ap', u32(0)),
        ('certificate-security-mode', u32(1)),
        ('effective-security-mode-sep', u32(1)),
        # MobileGestalt's WifiAddress (and UniqueDeviceID, built from it on
        # A11) reads these; without mac-address-wifi0 it waits for an
        # sdio/wlan/marconi-wifi IOService that never appears, which hung lsd
        # in every plugin registration. Synthetic, locally administered MACs
        # from the synthetic ECID.
        ('mac-address-wifi0', wifi_mac(ecid)),
        ('mac-address-bluetooth0', bytes([0x02]) + struct.pack('>Q', ecid)[3:7] + b'\x02'),
    ], [node('memory-map', [ph()]),
        # Local boot policy (kern_csr.c): SIP relaxed like `csrutil disable`
        # (lp-sip0: untrusted kexts, unrestricted FS, task_for_pid, kernel
        # debugger, dtrace, nvram), lp-sip1: unauthenticated (unsealed) root.
        # The iOS shared cache is in no trust cache, so dyld can only map it
        # with CSR_ALLOW_UNRESTRICTED_FS (vm_unix.c shared region checks).
        node('asmb', [ph(), ('lp-sip0', u64(0x6f)), ('lp-sip1', u32(1))])])

    # SMP: all eight cores in physical-core order (the little ones first, then
    # from BIG_FROM the big ones), one homogeneous cluster. The kernel defaults
    # to one and uses cpus=N to select an initial prefix (N <= 8).
    cpu_nodes = []
    for core in range(8):
        cpu_nodes.append(node(f'cpu{core}', [
            ph(),
            ('device_type', cstr('cpu')),
            ('compatible', b''.join(cstr(c) for c in soc.CPU_COMPAT)),
            ('reg', u32(core << 8)),  # actual MPIDR Aff1:Aff0, NOT logical ID
            ('cpu-id', u32(core)),   # descriptive: XNU assigns IDs by DT order
            ('cluster-id', u32(0)),  # descriptive; see ml_parse_cpu_topology
            ('cluster-type', u32(0)),  # homogeneous CLUSTER_TYPE_SMP, not AMP
            ('die-cluster-id', u32(0)),
            ('cluster-core-id', u32(core)),
            ('l2-cache-id', u32(core)),  # distinct per-core L2 nodes in live FDT
            ('l3-cache-id', u32(0)),     # shared L3
            ('state', cstr('running' if core == 0 else 'stopped')),
            ('timebase-frequency', u32(soc.TIMEBASE)),
            ('clock-frequency', u32(soc.CPU_HZ[1] if core >= soc.BIG_FROM else soc.CPU_HZ[0])),
            ('bus-frequency', u32(100000000)),
            ('memory-frequency', u32(1866000000)),
            ('peripheral-frequency', u32(soc.TIMEBASE)),
            ('fixed-frequency', u32(soc.TIMEBASE)),
        ]))
    cpus = node('cpus', [ph(), ('#address-cells', u32(1)),
                         ('#size-cells', u32(0))], cpu_nodes)

    # SPI interrupt controller of the /arm-io nubs (iokit/Kernel/arm/MappleGIC.cpp):
    # a device node has interrupt-parent = gic_ph and interrupts = <INTID flags>.
    gic_ph = next(phandle)
    gic = node('gic', [
        ('AAPL,phandle', u32(gic_ph)),
        ('compatible', cstr('arm,gic-v3')),
        ('interrupt-controller', b''),
        ('#interrupt-cells', u32(2)),
        ('virtual-timer-intid', u32(soc.VTIMER_INTID)),
        ('reg', u64(soc.GICD[0] - SOC_BASE, soc.GICD[1], soc.GICR[0] - SOC_BASE, soc.GICR[1])),
    ])
    flags = dict(audio=audio, torch=torch, camera=camera)
    arm_io = node('arm-io', [
        ph(),
        ('device_type', cstr(names['io'])),
        ('compatible', cstr(names['io']) + cstr('arm-io')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
        ('ranges', u64(0, SOC_BASE, 0x100000000 - SOC_BASE)),
    ], [gic,
        # No hardware: watchdogd checks IODeviceTree:/arm-io/wdt for no-pmu and
        # otherwise crashes trying to set the PMU boot stage (customer builds
        # ignore the -wdt_no_pmu boot-arg).
        node('wdt', [ph(), ('no-pmu', u32(1))]),
        # Main display: IOMobileFramebuffer.framework counts /arm-io children
        # with this device_type and reads the canvas size from the node
        # (the presented iPhone X size, see MappleFramebuffer.cpp);
        # the kernel's AppleCLCD matches the compatible string.
        node('disp0', [
            ph(),
            ('device_type', cstr('display-subsystem')),
            ('compatible', cstr(names['display'])),
            ('canvas-width', u32(CANVAS[0])),
            ('canvas-height', u32(CANVAS[1])),
        ])] + dev.devices(sys.modules[__name__], ph, phandle, gic_ph, ecid, flags))

    fb = dev.FB
    fbcon = node(names['fbcon'], [
        ph(),
        ('reg', u64(fb['base'], fb['size'])),
        ('width', u32(fb['width'])),
        ('height', u32(fb['height'])),
        ('stride', u32(fb['stride'])),
    ])

    # The UFS host the phone's boot loader leaves running (absolute physical reg).
    ufs_node = node(names['ufs'], [
        ph(),
        ('compatible', cstr(soc.UFS[0])),
        ('reg', u64(soc.UFS[1], soc.UFS[2])),
    ])

    # pmap_compute_max_asids requires the node even when no overrides are set.
    # An absent pmap-max-asids property selects XNU's own MAX_ASIDS default.
    # No effaceable storage: keybagd then reads /private/var/keybags/systembag.kb
    # as a plain plist instead of decrypting it with the effaceable key.
    defaults = node('defaults', [ph(), ('no-effaceable-storage', u32(1))])

    # MobileGestalt HomeButtonType = IODeviceTree:/buttons home-button-type
    # (D22AP: 2, no Home button; absent = 0 = Home-button device).
    buttons = node('buttons', [
        ph(),
        ('button-names', cstr('volup') + cstr('voldown') + cstr('hold')),
        ('home-button-type', u32(2)),
        ('opposed-power-vol-buttons', u32(1)),
    ])

    product_props = d22ap_product()
    # The device file's PRODUCT_NAME, and DISPLAY_GAMUT: the iPhone X says P3, and CoreAnimation then composes in
    # extended-range 10-bit formats (w30r/w40a); a panel shown as 8-bit BGRA says sRGB (AppleCLCD also converts XR10).
    over = {'product-name': cstr(dev.PRODUCT_NAME), 'artwork-display-gamut': cstr(dev.DISPLAY_GAMUT)}
    if product_props:
        product_props = [(k, over.get(k, v)) for k, v in product_props]
    # --haptics (no hardware: for the audio plug-in alone), and --audio with it: /product/haptics as the iPhone X has it. MobileGestalt's DeviceSupportsClosedLoopHaptics comes from it,
    # and only then does iOS play its haptics as waveforms on the audio plug-in's "Actuator" (/dev/mapple-haptic).
    haptics = [node('haptics', [ph(), ('closed-loop', u32(1)), ('supports-3rd-party-haptics', u32(1))])] if audio or haptics else []
    # --torch: /product/camera's "flash" (any value: libMobileGestalt only asks whether it is there) makes camera-flash
    # true, which shows the Control Center and lock-screen torch buttons.
    camera = [node('camera', [ph(), ('flash', u32(1))])] if torch else []
    product = [node('product', [ph()] + product_props, haptics + camera)] if product_props else []

    # What About shows (MobileGestalt): model-number + region-info, regulatory-model-number, /product product-name.
    # The device file's ABOUT: serial-number (a placeholder its shim may overwrite at each boot), model-number,
    # region-info (may be empty), regulatory-model-number.
    pad32 = lambda t: t.encode().ljust(32, b'\0')
    about = dev.ABOUT
    identity = [('serial-number', pad32(about['serial'])), ('model-number', pad32(about['model'])),
                ('region-info', pad32(about['region'])), ('regulatory-model-number', pad32(about['regulatory']))]

    # Panic log region. /pram reg = <phys size>; it sits just above the
    # dram-size we expose, so XNU maps it but never allocates into it.
    pram = node('pram', [ph(), ('reg', u64(panic_base) + u64(PANIC_LOG_SIZE))])

    # Identity of the iPhone X whose userland runs here: compatible[0] is
    # hw.model/hw.target (D22AP), model is hw.machine/hw.product
    # (iPhone10,3). The third entry is what the kernel line's platform expert matches.
    return node('device-tree', [
        ph(),
        ('compatible', cstr('D22AP') + cstr('iPhone10,3') + cstr(names['platform']) + cstr(soc.ROOT_COMPAT) + cstr('AppleARM')),
        ('model', cstr('iPhone10,3')),
        ('target-type', cstr('D22')),
        ('target-sub-type', cstr('D22AP')),
        ('manufacturer', cstr('Apple Inc.')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ] + identity, [chosen, defaults, cpus, arm_io, fbcon, buttons, pram] + product + ([ufs_node] if ufs else []))


if __name__ == '__main__':
    opts, bools = {}, set()
    for a in sys.argv[2:]:
        k, eq, v = a.partition('=')
        if k in ('--soc', '--device', '--canvas') and eq:
            opts[k] = v
        elif a in ('--dump', '--ufs', '--audio', '--haptics', '--torch', '--camera', '--resize'):
            bools.add(a)
        else:
            sys.exit(f'mkadt.py: unknown argument {a}')
    if '--soc' not in opts or '--device' not in opts:
        sys.exit(__doc__)
    if '--canvas' in opts:     # presented display size (P8: 1080x2400 with mp_fb_native=1)
        w, h = opts['--canvas'].split('x')
        CANVAS[0], CANVAS[1] = int(w), int(h)
    soc = load('soc', opts['--soc'], SOC_KEYS)
    dev = load('device', opts['--device'], DEVICE_KEYS)
    for k in ('serial', 'model', 'regulatory'):
        if not dev.ABOUT.get(k):
            sys.exit(f'mkadt.py: {opts["--device"]}: ABOUT {k} is missing or empty')
    if 'region' not in dev.ABOUT:
        sys.exit(f'mkadt.py: {opts["--device"]}: ABOUT region is missing')
    root = build(soc, dev, ufs='--ufs' in bools, audio='--audio' in bools, haptics='--haptics' in bools,
                 torch='--torch' in bools, camera='--camera' in bools, resize='--resize' in bools)
    blob = adt.serialize(root)
    open(sys.argv[1], 'wb').write(blob)
    if '--dump' in bools:
        adt.dump(adt.parse(blob)[0])
    print(f'{sys.argv[1]}: {len(blob)} bytes', file=sys.stderr)
