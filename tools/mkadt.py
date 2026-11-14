#!/usr/bin/env python3
"""Build the minimal Apple Device Tree that the MT6781 xnu needs to boot.

Only what the early boot path actually reads (see NOTES.md); every MMIO
address comes from the sweet live FDT. Usage:  mkadt.py OUT.adt [--dump] [--qemu] [--highmem] [--viva|--spaced [--ufs] [--haptics|--audio] [--torch] [--camera] [--resize]] [--canvas=WxH]
"""
import hashlib, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adt  # noqa: E402

# Kernel-managed RAM: above every Qualcomm XPU carve-out (see NOTES.md)
PHYS_BASE = 0xb0000000
MEM_SIZE = 0x4d000000
# Panic log buffer, carved from the TOP of the dram range and exposed to XNU as
# /pram + /chosen/embedded-panic-log-size. Without it, pe_init.c's
# check_for_panic_log() leaves gPanicBase == 0, debug_log_init() bails out twice
# and the kernel hangs right after "kdp_core zlib memory" — reproduced on QEMU
# and independently on ios-fajita.
PANIC_LOG_SIZE = 0x100000
# --highmem (phone only): the whole upper DDR bank. The live FDT memory node is
# <0x80000000 2G>, <0x1_00000000 0xfcc00000>; every fixed reserved-memory
# carve-out is in the low bank and the dynamic pools are limited below 4G.
HIGH_PHYS_BASE = 0x100000000
HIGH_MEM_SIZE = 0xfcc00000
# /arm-io children are relative to this; it must not be 0 (serial_init bails)
SOC_BASE = 0x01000000

GICD, GICD_SIZE = 0x17a00000, 0x10000
GICR, GICR_SIZE = 0x17a60000, 0x100000
FB, FB_SIZE = 0x9c000000, 0x1700000
MAPPLE_ECID = 0x5357454554000001  # 'MT6781' + 1, synthetic
FB_W, FB_H, FB_STRIDE = 1080, 2400, 1080 * 4
# Internal UFS host (ufshc@1d84000 "ufs_mem"); QEMU virt has none
UFS, UFS_SIZE = 0x01d84000, 0x3000
TIMEBASE = 19200000
VTIMER_INTID = 19
# QEMU virt (-M virt,gic-version=3 -cpu cortex-a76): GICD/GICR and the generic timer's CNTFRQ
QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE = 0x08000000, 0x080a0000, 62500000
QEMU_VTIMER_INTID = 27

# --canvas=WxH: the display size IOMobileFramebuffer reads from /arm-io/disp0 (default the presented iPhone X
# size 1125x2436; must match what AppleCLCD reports: 1080x2400 with boot-arg mp_fb_native=1).
CANVAS = [1125, 2436]
# --viva: Redmi Note 11 Pro 4G (MT6781). RAM is 0x40000000-0x1c0000000; every
# LK mblock reservation is below 0xc0000000 except dramc-rk1 at 0x1bffff000.
# start.s maps kernel RAM with 32MB cacheable blocks, so the range stops at the
# last 32MB boundary below dramc-rk1. Addresses from the viva DTB and the
# on-device probe (README of ios-viva).
MAPPLE_PHYS_BASE, MAPPLE_MEM_SIZE = 0xc0000000, 0x1be000000 - 0xc0000000
MAPPLE_GICD, MAPPLE_GICD_SIZE = 0x0c000000, 0x40000
MAPPLE_GICR, MAPPLE_GICR_SIZE = 0x0c040000, 0x200000
MAPPLE_FB, MAPPLE_FB_STRIDE = 0x7bc10000, 1088 * 4
MAPPLE_FB_SIZE = (MAPPLE_FB_STRIDE * 2400 + 0x3fff) & ~0x3fff
MAPPLE_TIMEBASE = 13000000
MAPPLE_VTIMER_INTID = 27  # DT timer <GIC_PPI 11>, the standard virtual timer
MAPPLE_ECID = 0x5649564100000001  # 'VIVA' + 1, synthetic
# --spaced: realme 8i (MT6781, the same SoC as viva). 4 GB of RAM, 0x40000000-0x13fffffff; LK's reservations are
# all below 0xc0000000 but for dramc-rk at 0x13ffff000, so XNU gets 0xc0000000 up to the 32 MB boundary below it.
# LK's framebuffer: atag,videolfb 0x771b0000, the first of three 1080x2412 buffers, pitch 4320.
SPACED_PHYS_BASE, SPACED_MEM_SIZE = 0xc0000000, 0x13e000000 - 0xc0000000
SPACED_FB, SPACED_FB_STRIDE, SPACED_FB_H = 0x771b0000, 1080 * 4, 2412
SPACED_FB_SIZE = (SPACED_FB_STRIDE * SPACED_FB_H + 0x3fff) & ~0x3fff
SPACED_ECID = 0x5350414300000001  # 'SPAC' + 1, synthetic
# MediaTek ufshci@11270000 (DT size 0x2300, one 16K page)
MAPPLE_UFS, MAPPLE_UFS_SIZE = 0x11270000, 0x4000

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


def mapple_devices(ph, phandle, gic_ph, ecid):
    """viva /arm-io devices with their own IOKit drivers (addresses and SPIs from the viva DTB)."""
    io = lambda pa, size: u64(pa - SOC_BASE, size)
    eint_ph = next(phandle)
    return [
        # MediaTek keypad (kp@10010000, SPI 73 edge): iokit/Kernel/arm/hid/MTKKeypad.cpp.
        # keymap = <bit usage> pairs (HID consumer page). Volume down is kpd-hw-init-map
        # index 0 = bit 0 (measured 2026-11-03); power and volume up are PMIC keys.
        node('keypad', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-keypad')),
            ('reg', io(0x10010000, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x49, IRQ_EDGE_RISING)),
            ('keymap', u32(0, HID_CSMR_VOLUME_DOWN)),
        ]),
        # External interrupts (eint@1000b000, GIC SPI 186 level): iokit/Kernel/arm/MTKEINT.cpp,
        # an interrupt controller under the GIC; children use interrupts = <EINT flags>.
        node('eint', [
            ('AAPL,phandle', u32(eint_ph)),
            ('compatible', cstr('mediatek,mt6781-eint')),
            ('reg', io(0x1000b000, 0x1000)),
            ('interrupt-controller', b''),
            ('#interrupt-cells', u32(2)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0xba, IRQ_LEVEL_HIGH)),
        ]),
        # PMIC wrapper (pwrap@1000d000): iokit/Kernel/arm/MTKPwrap.cpp, a bus for the MT6366
        # (MT6358PMIC.cpp, interrupt EINT 144 level high) and its key inputs (hid/MT6358Keys.cpp:
        # keymap <key usage>, key 0 PWRKEY = power, 1 HOMEKEY = volume up).
        node('pwrap', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-pwrap')),
            ('reg', io(0x1000d000, 0x1000)),
        ], [node('pmic', [
            ph(),
            ('compatible', cstr('mediatek,mt6366-pmic')),
            ('interrupt-parent', u32(eint_ph)),
            ('interrupts', u32(144, IRQ_LEVEL_HIGH)),
        ], [node('keys', [
            ph(),
            ('compatible', cstr('mediatek,mt6366-keys')),
            ('keymap', u32(0, HID_CSMR_POWER, 1, HID_CSMR_VOLUME_UP)),
        ])])]),
        # GPIO (gpio@10005000): iokit/Kernel/arm/MTKIO.cpp. Only these pins can be driven:
        # the GT9896's reset (17), dvdd (73) and avdd (166) switches (live DT goodix,*-gpio).
        node('gpio', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-gpio')),
            ('reg', io(0x10005000, 0x1000)),
            ('output-pins', u32(17, 73, 166)),
        ]),
        # I2C0 (i2c0@11007000, SPI 105 level): MTKIO.cpp, MediaTek I2C v2 in FIFO mode.
        # Source clock: CK_CFG_6 i2c_sel = 3 = univpll_d5_d2 = 2496 MHz / 2 / 5 / 2 (univ2pll
        # PCW measured 2026-11-03), divided by clock-div 5. SCL0/SDA0 pin mux is left as LK set it.
        node('i2c0', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-i2c')),
            ('reg', io(0x11007000, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x69, IRQ_LEVEL_HIGH)),
            ('clock-frequency', u32(400000)),
            ('source-clock', u32(124800000)),
            ('clock-div', u32(5)),
            ('ext-time-config', u32(0x1801)),
        ], [
            # Goodix GT9896 touch (gt9896@5d): hid/GT9896.cpp; interrupt EINT 13 falling edge.
            node('touch', [
                ph(),
                ('compatible', cstr('goodix,gt9896')),
                ('i2c-address', u32(0x5d)),
                ('interrupt-parent', u32(eint_ph)),
                ('interrupts', u32(13, IRQ_EDGE_FALLING)),
                ('avdd-gpio', u32(166)),
                ('dvdd-gpio', u32(73)),
                ('reset-gpio', u32(17)),
                ('max-x', u32(8640)),
                ('max-y', u32(19200)),
                # Yellowstone register map from vendor/firmware/goodix_cfg_group.bin
                # (tools/viva-goodix-cfg.py; ic_type "yellowstone", pid "9896")
                ('reg-coor', u32(0x4180)),
                ('reg-pid', u32(0x4022)),
                ('reg-fw-request', u32(0x4180)),
            ]),
        ]),
        # I2C3 (i2c3@1100f000, SPI 108 level, 400 kHz, same source clock as I2C0): the battery bus. LK
        # charges through it, so it is normally clocked; reg 1 + imp-cg-bit = its IMP_IIC_WRAP gate
        # (clk-mt6781.c imp_ap_i2c3, STA 0xe00 / CLR 0xe04 bit 3), opened by MTKI2C if closed.
        node('i2c3', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-i2c')),
            ('reg', io(0x1100f000, 0x1000) + io(0x11017000, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x6c, IRQ_LEVEL_HIGH)),
            ('bus-id', u32(3)),
            ('imp-cg-bit', u32(3)),
            ('clock-frequency', u32(400000)),
            ('source-clock', u32(124800000)),
            ('clock-div', u32(5)),
            ('ext-time-config', u32(0x1801)),
        ], [
            # TI bq27z561 fuel gauge (0x55) + Silergy SYV690 charger (0x6a, INT = EINT 11 falling):
            # iokit/Kernel/arm/BQ27Z561Battery.cpp, the IOPMPowerSource. Charge limits: the live DT's
            # syv690 safety-max-charge-voltage 4450 mV (VREG step: 4448) / safety-max-charge-current 3000 mA.
            node('battery', [
                ph(),
                ('compatible', cstr('ti,bq27z561')),
                ('i2c-address', u32(0x55)),
                ('charger-address', u32(0x6a)),
                ('interrupt-parent', u32(eint_ph)),
                ('interrupts', u32(11, IRQ_EDGE_FALLING)),
                ('design-capacity', u32(5000)),
                ('charge-voltage-max', u32(4448)),
                ('charge-current-max', u32(3000)),
            ]),
        ]),
        # Mali-G57 MC2 (mali@13000000; GIC SPI 274/275/276 = GPU, MMU, JOB, level high; EVENT and PWR
        # belong to ARM's kbase and stay unused): iokit/Kernel/arm/MappleGPU.cpp, started only with the
        # boot-arg mp_gpu=1|2. reg: the Mali, then the MFG power blocks it drives: mfgcfg, SPM,
        # topckgen, apmixed, infracfg_ao (sequences from Xiaomi's gpufreq/clk-mt6781-pg).
        node('gpu', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-mali')),
            ('reg', u64(0x13000000 - SOC_BASE, 0x4000, 0x13fbf000 - SOC_BASE, 0x1000,
                        0x10006000 - SOC_BASE, 0x1000, 0x10000000 - SOC_BASE, 0x1000,
                        0x1000c000 - SOC_BASE, 0x1000, 0x10001000 - SOC_BASE, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x112, IRQ_LEVEL_HIGH) + spi(0x113, IRQ_LEVEL_HIGH) + spi(0x114, IRQ_LEVEL_HIGH)),
        ]),
        # CONNSYS WiFi (MT6781 + MT6631): iokit/Kernel/arm/MT6631WLAN.cpp (registry for configd / wifid) on top of
        # bsd/dev/mt6631_wifi.c + mt6631_wlan.c (the station, en0). launchd starts wifid only when a node named "wlan"
        # exists (_LimitLoadToDeviceTree); MobileGestalt reads local-mac-address here.
        node('wlan', [
            ph(),
            ('device_type', cstr('wlan')),
            ('compatible', cstr('mediatek,mt6781-wlan')),
            ('local-mac-address', wifi_mac(ecid)),
        ]),
    ]


def spaced_devices(ph, phandle, gic_ph, ecid, audio=False, torch=False, camera=False):
    """realme 8i /arm-io devices: viva's MT6781 blocks, its own keys and touch screen; nothing for chips it lacks."""
    io = lambda pa, size: u64(pa - SOC_BASE, size)
    eint_ph = next(phandle)
    return [
        # Volume down is bit 1 of KP_MEM1 here (kpd-hw-init-map 0x72); power and volume up are the PMIC's keys.
        node('keypad', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-keypad')),
            ('reg', io(0x10010000, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x49, IRQ_EDGE_RISING)),
            ('keymap', u32(1, HID_CSMR_VOLUME_DOWN)),
        ]),
        node('eint', [
            ('AAPL,phandle', u32(eint_ph)),
            ('compatible', cstr('mediatek,mt6781-eint')),
            ('reg', io(0x1000b000, 0x1000)),
            ('interrupt-controller', b''),
            ('#interrupt-cells', u32(2)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0xba, IRQ_LEVEL_HIGH)),
        ]),
        # The PMIC (MT6358 family, the live DT's mt6358-pmic, interrupt EINT 144): key 0 PWRKEY = power,
        # 1 HOMEKEY = volume up (TOPSTATUS bits 1 and 3).
        node('pwrap', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-pwrap')),
            ('reg', io(0x1000d000, 0x1000)),
        ], [node('pmic', [
            ph(),
            ('compatible', cstr('mediatek,mt6366-pmic')),
            ('interrupt-parent', u32(eint_ph)),
            ('interrupts', u32(144, IRQ_LEVEL_HIGH)),
        ], [node('keys', [
            ph(),
            ('compatible', cstr('mediatek,mt6366-keys')),
            ('keymap', u32(0, HID_CSMR_POWER, 1, HID_CSMR_VOLUME_UP)),
        ])])]),
        # GPIO: only the touch screen's SPI4 pins driven by hand (MOSI 13, CLK 144, CS 145; MISO 14 is an input);
        # with --audio the speaker amplifier's enable / one-wire pin too (164, bsd/dev/mtk_audio.c).
        node('gpio', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-gpio')),
            ('reg', io(0x10005000, 0x1000)),
            ('output-pins', u32(13, 144, 145, *((164,) if audio else ()))),
        ]),
    ] + ([
        # --audio: I2C3 for the speaker amplifier (AW87319 0x58 or SIA8109 0x28, the live DT's aw87319 / sia81xx-i2c);
        # the controller as on viva (same SoC), no children: bsd/dev/mtk_audio.c talks to it by bus number.
        node('i2c3', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-i2c')),
            ('reg', io(0x1100f000, 0x1000) + io(0x11017000, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x6c, IRQ_LEVEL_HIGH)),
            ('bus-id', u32(3)),
            ('imp-cg-bit', u32(3)),
            ('clock-frequency', u32(400000)),
            ('source-clock', u32(124800000)),
            ('clock-div', u32(5)),
            ('ext-time-config', u32(0x1801)),
        ]),
    ] if audio else []) + ([
        # --torch: I2C5 for the flash LED's chip (MT6371 PMU at 0x34, also the charger and the backlight;
        # bsd/dev/mtk_torch.c talks to it by bus number); the live DT's i2c5: SPI 354, gate bit 5, clock-div 5.
        node('i2c5', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-i2c')),
            ('reg', io(0x11016000, 0x1000) + io(0x11017000, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x162, IRQ_LEVEL_HIGH)),
            ('bus-id', u32(5)),
            ('imp-cg-bit', u32(5)),
            ('clock-frequency', u32(400000)),
            ('source-clock', u32(124800000)),
            ('clock-div', u32(5)),
            ('ext-time-config', u32(0x1801)),
        ]),
    ] if torch else []) + ([
        # --camera: libMobileGestalt's rear-facing-camera and still-camera are /arm-io/isp's camera-rear (any value);
        # the camera itself is mapple-captured's.
        node('isp', [ph(), ('camera-rear', u32(1))]),
    ] + [
        # and the camera's buses (bsd/dev/mtk_cam.c, by bus number): I2C2 for its LDO chip (0x29), I2C4 for the rear
        # sensor, its VCM and EEPROM; the live DT's i2c2 SPI 107 and i2c4 SPI 109, gate bits 2 and 4, clock-div 5.
        node(f'i2c{n}', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-i2c')),
            ('reg', io(base, 0x1000) + io(0x11017000, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(irq, IRQ_LEVEL_HIGH)),
            ('bus-id', u32(n)),
            ('imp-cg-bit', u32(n)),
            ('clock-frequency', u32(400000)),
            ('source-clock', u32(124800000)),
            ('clock-div', u32(5)),
            ('ext-time-config', u32(0x1801)),
        ]) for n, base, irq in ((2, 0x11009000, 107), (4, 0x11011000, 109))
    ] if camera else []) + [
        # ILI7807S touch (iokit/Kernel/arm/hid/ILI7807S.cpp): SPI4's pins by hand, a report per low pulse on
        # EINT 12; firmware sent by mtkloader before the shim (mp_ili_fw= for sending it again).
        node('touch', [
            ph(),
            ('compatible', cstr('ilitek,ili7807s')),
            ('spi-gpios', u32(13, 14, 144, 145)),
            ('interrupt-parent', u32(eint_ph)),
            ('interrupts', u32(12, IRQ_EDGE_FALLING)),
            ('max-x', u32(2047)),
            ('max-y', u32(2047)),
            ('panel-size', u32(1080, SPACED_FB_H)),
        ]),
        # Mali-G57 MC2, the same SoC block as viva's (realme dts: mali@13000000, SPI 274-276): MappleGPU.cpp, which does
        # nothing without the boot-arg mp_gpu=1|2.
        node('gpu', [
            ph(),
            ('compatible', cstr('mediatek,mt6781-mali')),
            ('reg', io(0x13000000, 0x4000) + io(0x13fbf000, 0x1000) + io(0x10006000, 0x1000) +
                    io(0x10000000, 0x1000) + io(0x1000c000, 0x1000) + io(0x10001000, 0x1000)),
            ('interrupt-parent', u32(gic_ph)),
            ('interrupts', spi(0x112, IRQ_LEVEL_HIGH) + spi(0x113, IRQ_LEVEL_HIGH) + spi(0x114, IRQ_LEVEL_HIGH)),
        ]),
        # CONNSYS WiFi, as viva's: MT6631WLAN.cpp matches it; launchd starts wifid only with a node named "wlan".
        # wifi-vendor: MT6631WLAN.cpp publishes it as MobileGestalt's WifiVendor; BlueTool's built-in script for
        # this platform is only there for "Murata" and "USI" modules (the MT6631 has none of either).
        node('wlan', [
            ph(),
            ('device_type', cstr('wlan')),
            ('compatible', cstr('mediatek,mt6781-wlan')),
            ('local-mac-address', wifi_mac(ecid)),
            ('wifi-vendor', cstr('Murata')),
        ]),
    ]


def build(seed=None, qemu=False, highmem=False, viva=False, mapple_ufs=False, spaced=False, audio=False, haptics=False,
          torch=False, camera=False, resize=False):
    """qemu=True: same tree, but with QEMU virt's GIC addresses and CNTFRQ (tools/qemu-sweet.py).
    viva=True: Redmi Note 11 Pro 4G addresses. Without mapple_ufs there is no UFS node, so the
    kernel never touches storage; with it the kernel drives the MediaTek host LK left running."""
    viva = viva or spaced           # the same SoC: viva's addresses but for RAM, framebuffer and devices
    phys_base, mem_size = (HIGH_PHYS_BASE, HIGH_MEM_SIZE) if highmem else (PHYS_BASE, MEM_SIZE)
    if viva:
        phys_base, mem_size = MAPPLE_PHYS_BASE, MAPPLE_MEM_SIZE
    if spaced:
        phys_base, mem_size = SPACED_PHYS_BASE, SPACED_MEM_SIZE
    # Carve the panic log off the top so the kernel never allocates over it.
    panic_base = phys_base + mem_size - PANIC_LOG_SIZE
    mem_size -= PANIC_LOG_SIZE
    gicd, gicr, timebase = (QEMU_GICD, QEMU_GICR, QEMU_TIMEBASE) if qemu else (GICD, GICR, TIMEBASE)
    gicd_size, gicr_size, vtimer = GICD_SIZE, GICR_SIZE, (QEMU_VTIMER_INTID if qemu else VTIMER_INTID)
    fb, fb_size, fb_stride, ecid = FB, FB_SIZE, FB_STRIDE, MAPPLE_ECID
    if viva:
        gicd, gicr, timebase = MAPPLE_GICD, MAPPLE_GICR, MAPPLE_TIMEBASE
        gicd_size, gicr_size, vtimer = MAPPLE_GICD_SIZE, MAPPLE_GICR_SIZE, MAPPLE_VTIMER_INTID
        fb, fb_size, fb_stride, ecid = MAPPLE_FB, MAPPLE_FB_SIZE, MAPPLE_FB_STRIDE, MAPPLE_ECID
    fb_h = FB_H
    if spaced:
        fb, fb_size, fb_stride, ecid, fb_h = SPACED_FB, SPACED_FB_SIZE, SPACED_FB_STRIDE, SPACED_ECID, SPACED_FB_H
    seed = seed if seed is not None else os.urandom(64)
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
        ('firmware-version', cstr('mapple-shim')),
        ('system-firmware-version', cstr('mapple-shim')),
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

    # SMP: all eight cores in physical-core order (0-5 Cortex-A55,
    # 6-7 Cortex-A76), one homogeneous cluster. The kernel defaults to one
    # and uses cpus=N to select an initial prefix (N <= 8).
    # QEMU retains its original single-CPU tree: its MPIDRs differ from sweet.
    cpu_nodes = []
    for core in range(1 if qemu else 8):
        cpu_nodes.append(node(f'cpu{core}', [
            ph(),
            ('device_type', cstr('cpu')),
            ('compatible', cstr('qcom,kryo470') + cstr('ARM,v8')),
            ('reg', u32(core << 8)),  # actual MPIDR Aff1:Aff0, NOT logical ID
            ('cpu-id', u32(core)),   # descriptive: XNU assigns IDs by DT order
            ('cluster-id', u32(0)),  # descriptive; see ml_parse_cpu_topology
            ('cluster-type', u32(0)),  # homogeneous CLUSTER_TYPE_SMP, not AMP
            ('die-cluster-id', u32(0)),
            ('cluster-core-id', u32(core)),
            ('l2-cache-id', u32(core)),  # distinct per-core L2 nodes in live FDT
            ('l3-cache-id', u32(0)),     # shared L3
            ('state', cstr('running' if core == 0 else 'stopped')),
            ('timebase-frequency', u32(timebase)),
            ('clock-frequency', u32((2050000000 if core >= 6 else 2000000000) if viva else (2304000000 if core >= 6 else 1804800000))),
            ('bus-frequency', u32(100000000)),
            ('memory-frequency', u32(1866000000)),
            ('peripheral-frequency', u32(timebase)),
            ('fixed-frequency', u32(timebase)),
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
        ('virtual-timer-intid', u32(vtimer)),
        ('reg', u64(gicd - SOC_BASE, gicd_size, gicr - SOC_BASE, gicr_size)),
    ])
    arm_io = node('arm-io', [
        ph(),
        ('device_type', cstr('mapple-io')),
        ('compatible', cstr('mapple-io') + cstr('arm-io')),
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
            ('compatible', cstr('mapple,display')),
            ('canvas-width', u32(CANVAS[0])),
            ('canvas-height', u32(CANVAS[1])),
        ])] + (spaced_devices(ph, phandle, gic_ph, ecid, audio, torch, camera) if spaced else mapple_devices(ph, phandle, gic_ph, ecid) if viva else []))

    fbcon = node('mapple-fbcon', [
        ph(),
        ('reg', u64(fb, fb_size)),
        ('width', u32(FB_W)),
        ('height', u32(fb_h)),
        ('stride', u32(fb_stride)),
    ])

    # bsd/dev/mtk_ufs.c: the UFS host ABL leaves running (absolute physical reg).
    ufs = node('mapple-ufs', [
        ph(),
        ('compatible', cstr('mediatek,mt8183-ufshci') if viva else cstr('qcom,ufshc')),
        ('reg', u64(MAPPLE_UFS, MAPPLE_UFS_SIZE) if viva else u64(UFS, UFS_SIZE)),
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
    if product_props and viva:
        # The iPhone X is a P3 display: CoreAnimation would then compose in extended-range 10-bit formats
        # (w30r/w40a). viva's panel is shown as 8-bit BGRA, so say sRGB (AppleCLCD also converts XR10).
        product_props = [(k, cstr('sRGB') if k == 'artwork-display-gamut' else v) for k, v in product_props]
    if product_props and spaced:
        product_props = [(k, cstr('realme 8i') if k == 'product-name' else v) for k, v in product_props]
    # --haptics (no hardware: for the audio plug-in alone), and --audio with it: /product/haptics as the iPhone X has it. MobileGestalt's DeviceSupportsClosedLoopHaptics comes from it,
    # and only then does iOS play its haptics as waveforms on the audio plug-in's "Actuator" (/dev/mapple-haptic).
    haptics = [node('haptics', [ph(), ('closed-loop', u32(1)), ('supports-3rd-party-haptics', u32(1))])] if audio or haptics else []
    # --torch: /product/camera's "flash" (any value: libMobileGestalt only asks whether it is there) makes camera-flash
    # true, which shows the Control Center and lock-screen torch buttons.
    camera = [node('camera', [ph(), ('flash', u32(1))])] if torch else []
    product = [node('product', [ph()] + product_props, haptics + camera)] if product_props else []

    # What About shows (MobileGestalt): model-number + region-info, regulatory-model-number, /product product-name.
    # serial-number is a placeholder the shim overwrites at each boot with LK's androidboot.serialno.
    pad32 = lambda t: t.encode().ljust(32, b'\0')
    identity = [('serial-number', pad32('MAPPLE0000000000')), ('model-number', pad32('RMX3151')),
                ('region-info', pad32('')), ('regulatory-model-number', pad32('RMX3151'))] if spaced else []

    # Panic log region. /pram reg = <phys size>; it sits just above the
    # dram-size we expose, so XNU maps it but never allocates into it.
    pram = node('pram', [ph(), ('reg', u64(panic_base) + u64(PANIC_LOG_SIZE))])

    # Identity of the iPhone X whose userland runs here: compatible[0] is
    # hw.model/hw.target (D22AP), model is hw.machine/hw.product
    # (iPhone10,3). 'mapple,platform' stays for MapplePlatformExpert matching.
    return node('device-tree', [
        ph(),
        ('compatible', cstr('D22AP') + cstr('iPhone10,3') + cstr('mapple,platform') + cstr('qcom,sm7150') + cstr('AppleARM')),
        ('model', cstr('iPhone10,3')),
        ('target-type', cstr('D22')),
        ('target-sub-type', cstr('D22AP')),
        ('manufacturer', cstr('Apple Inc.')),
        ('#address-cells', u32(2)),
        ('#size-cells', u32(2)),
    ] + identity, [chosen, defaults, cpus, arm_io, fbcon, buttons, pram] + product + ([ufs] if (mapple_ufs if viva else not qemu) else []))


if __name__ == '__main__':
    for a in sys.argv:
        if a.startswith('--canvas='):     # presented display size (P8: 1080x2400 with mp_fb_native=1)
            w, h = a.split('=', 1)[1].split('x')
            CANVAS[0], CANVAS[1] = int(w), int(h)
    root = build(qemu='--qemu' in sys.argv, highmem='--highmem' in sys.argv, viva='--viva' in sys.argv,
                 mapple_ufs='--ufs' in sys.argv, spaced='--spaced' in sys.argv, audio='--audio' in sys.argv,
                 haptics='--haptics' in sys.argv, torch='--torch' in sys.argv, camera='--camera' in sys.argv,
                 resize='--resize' in sys.argv)
    blob = adt.serialize(root)
    open(sys.argv[1], 'wb').write(blob)
    if '--dump' in sys.argv:
        adt.dump(adt.parse(blob)[0])
    print(f'{sys.argv[1]}: {len(blob)} bytes', file=sys.stderr)
