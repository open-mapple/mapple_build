# MediaTek MT6781 (Helio G96), for mkadt.py: its blocks, and the names the MediaTek kernel line matches.
# Addresses from viva's DTB and the on-device probe (README of ios-viva).
NAMES = dict(platform='mapple,platform', io='mapple-io', display='mapple,display', fbcon='mapple-fbcon',
             ufs='mapple-ufs', firmware='mapple-shim')
# The root's SoC entry and the cores' compatible still say what ios-sweet's tree said; nothing reads them.
ROOT_COMPAT = 'qcom,sm7150'
CPU_COMPAT = ('qcom,kryo470', 'ARM,v8')
GICD = (0x0c000000, 0x40000)
GICR = (0x0c040000, 0x200000)
TIMEBASE = 13000000
VTIMER_INTID = 27       # DT timer <GIC_PPI 11>, the standard virtual timer
CPU_HZ = (2000000000, 2050000000)       # Cortex-A55, Cortex-A76
BIG_FROM = 6
UFS = ('mediatek,mt8183-ufshci', 0x11270000, 0x4000)   # ufshci@11270000 (DT size 0x2300, one 16K page)
