# . build/envsetup.sh            from the top of the tree (bash or zsh)
# lunch mapple_<device>-<variant>  variant: userdebug (DEVELOPMENT kernel) or user (RELEASE kernel)
# mka [target ...] [NAME=value ...]  see build/mapple/core/mka.sh
if [ ! -f build/mapple/envsetup.sh ]; then
	echo "envsetup.sh: run it from the top of the tree (. build/envsetup.sh)" >&2
	return 1
fi
MAPPLE_TOP=$(pwd)
export MAPPLE_TOP

lunch() {
	local p v d c
	if [ $# -eq 0 ]; then
		echo "products:"
		for c in $(cd "$MAPPLE_TOP" && ls device/*/*/device.conf 2>/dev/null); do
			d=${c%/device.conf}; echo "  mapple_${d##*/}-userdebug  mapple_${d##*/}-user"
		done
		return 0
	fi
	p=${1%-*} v=${1##*-}
	[ "$p" != "$1" ] || v=userdebug
	d=${p#mapple_}
	case $v in user|userdebug) ;; *) echo "lunch: variant $v: user or userdebug" >&2; return 1 ;; esac
	c=$(cd "$MAPPLE_TOP" && ls device/*/"$d"/device.conf 2>/dev/null | head -1)
	[ "$p" = "mapple_$d" ] && [ -n "$c" ] || { echo "lunch: no product $p (lunch with no argument lists them)" >&2; return 1; }
	export TARGET_PRODUCT=$p TARGET_DEVICE=$d TARGET_BUILD_VARIANT=$v
	export TARGET_DEVICE_DIR=$MAPPLE_TOP/${c%/device.conf}
	export OUT=$MAPPLE_TOP/out/target/product/$d
	echo "TARGET_PRODUCT=$p TARGET_BUILD_VARIANT=$v OUT=$OUT"
}

mka() {
	bash "$MAPPLE_TOP/build/mapple/core/mka.sh" "$@"
}
