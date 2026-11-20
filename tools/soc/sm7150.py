# Qualcomm SM7150 (Snapdragon 720G/730G), for mkadt.py: its blocks, and the names the Qualcomm kernel line (ios-sweet's)
# matches. From ios-sweet's tools/mkadt.py (dipa-studio, damjankakak3, 2010475745xie; MIT): every address from
# sweet's live FDT.
NAMES = dict(platform='xiaomi,sweet', io='sweet-io', display='sweet,display', fbcon='sweet-fbcon',
             ufs='sweet-ufs', firmware='sweet-shim')
ROOT_COMPAT = 'qcom,sm7150'
CPU_COMPAT = ('qcom,kryo470', 'ARM,v8')
GICD = (0x17a00000, 0x10000)
GICR = (0x17a60000, 0x100000)
TIMEBASE = 19200000
VTIMER_INTID = 19
CPU_HZ = (1804800000, 2304000000)       # six Cortex-A55, two Cortex-A76
BIG_FROM = 6
UFS = ('qcom,ufshc', 0x01d84000, 0x3000)   # ufshc@1d84000 "ufs_mem"
