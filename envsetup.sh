# . build/envsetup.sh            from the top of the tree (bash or zsh)
# lunch mapple_<device>[_single|_split]-<variant>  variant: userdebug (DEVELOPMENT kernel) or user (RELEASE kernel)
#   mapple_<device>: the development layout (the root on xnusys, made by hand); _single: one OS installed by the
#   zip, the root on userdata (device.conf's single_* keys); _split: Android and iOS side by side (split_*)
# mka [target ...] [NAME=value ...]  see build/mapple/core/mka.sh
if [ ! -f build/mapple/envsetup.sh ]; then
	echo "envsetup.sh: run it from the top of the tree (. build/envsetup.sh)" >&2
	return 1
fi
MAPPLE_TOP=$(pwd)
export MAPPLE_TOP

lunch() {
	local p v d c i
	if [ $# -eq 0 ]; then
		echo "products:"
		for c in $(cd "$MAPPLE_TOP" && ls device/*/*/device.conf 2>/dev/null); do
			d=${c%/device.conf}; echo "  mapple_${d##*/}-userdebug  mapple_${d##*/}-user"
			for i in single split; do
				! grep -q "^	${i}_" "$MAPPLE_TOP/$c" || echo "  mapple_${d##*/}_$i-userdebug  mapple_${d##*/}_$i-user"
			done
		done
		return 0
	fi
	p=${1%-*} v=${1##*-}
	[ "$p" != "$1" ] || v=userdebug
	d=${p#mapple_} i=dev
	case $d in *_single) i=single d=${d%_single} ;; *_split) i=split d=${d%_split} ;; esac
	case $v in user|userdebug) ;; *) echo "lunch: variant $v: user or userdebug" >&2; return 1 ;; esac
	c=$(cd "$MAPPLE_TOP" && ls device/*/"$d"/device.conf 2>/dev/null | head -1)
	[ "${p#mapple_}" != "$p" ] && [ -n "$c" ] && { [ $i = dev ] || grep -q "^	${i}_" "$MAPPLE_TOP/$c"; } ||
		{ echo "lunch: no product $p (lunch with no argument lists them)" >&2; return 1; }
	export TARGET_PRODUCT=$p TARGET_DEVICE=$d TARGET_BUILD_VARIANT=$v TARGET_INSTALL=$i
	export TARGET_DEVICE_DIR=$MAPPLE_TOP/${c%/device.conf}
	export OUT=$MAPPLE_TOP/out/target/product/$d
	echo "TARGET_PRODUCT=$p TARGET_BUILD_VARIANT=$v OUT=$OUT"
}

mka() {
	bash "$MAPPLE_TOP/build/mapple/core/mka.sh" "$@"
}
