#!/bin/bash
# mka [target ...] [NAME=value ...], after lunch. Targets (default mapple: all of them, in this order):
#   ipsw headers kernel shim loader adt payload bootimage
# NAME=value:
#   IPSW=path          your own copy of the pinned IPSW (else it is downloaded from Apple into out/ipsw/)
#   ANDROID_BOOT=path  a backup of your phone's boot partition: bootimage chains the loader in front of it
#   KERNEL=path        a kernel already built (skips the kernel build)
#   XCODE15_DEVELOPER_DIR=path  Xcode 15.4's Developer directory, if Spotlight can't find it
# Steps that need sudo print the command and stop; run it, then mka again.
set -euo pipefail
: "${MAPPLE_TOP:?. build/envsetup.sh first}" "${TARGET_DEVICE:?lunch first}"
targets=
for a in "$@"; do
	case $a in
	*=*) export "$a" ;;
	*) targets="$targets $a" ;;
	esac
done
[ -n "$targets" ] || targets=mapple

TOP=$MAPPLE_TOP
BM=$TOP/build/mapple
DEV=$TARGET_DEVICE_DIR
OBJ=$OUT/obj
COMMON_CONF=$TOP/vendor/mapple/config/common.conf
mkdir -p "$OBJ"

conf() {	# conf KEY FILE: the value of KEY (tab-indented in device.conf, at the start of a line in common.conf)
	sed -n "s/^	\{0,1\}$1	//p" "$2" | head -1
}
need() { [ -f "$1" ] || { echo "mka: no $1 ($2)" >&2; exit 1; }; }
say() { echo "== $*"; }

SOC=$(conf soc "$DEV/device.conf")
# Xcode 15.4 builds the kernel (its iig), as the runs so far were built; the loader and the shim use the usual
# Xcode. xcode-select is left alone.
[ -n "${XCODE15_DEVELOPER_DIR:-}" ] || XCODE15_DEVELOPER_DIR=$(mdfind 'kMDItemCFBundleIdentifier == "com.apple.dt.Xcode" && kMDItemVersion == "15.4"' | head -1)/Contents/Developer
X15=$XCODE15_DEVELOPER_DIR
[ "$(DEVELOPER_DIR=$X15 xcrun --find iig 2>/dev/null)" = "$X15/Toolchains/XcodeDefault.xctoolchain/usr/bin/iig" ] ||
	{ echo "mka: needs Xcode 15.4 (beside your usual Xcode is fine): XCODE15_DEVELOPER_DIR=/path/to/Xcode-15.4.app/Contents/Developer" >&2; exit 1; }
IPSW_FILE=${IPSW:-$TOP/out/ipsw/$(basename "$(conf ipsw_url "$COMMON_CONF")")}

step_ipsw() {
	local url size sum stamp
	url=$(conf ipsw_url "$COMMON_CONF") size=$(conf ipsw_size "$COMMON_CONF") sum=$(conf ipsw_sha256 "$COMMON_CONF")
	if [ ! -f "$IPSW_FILE" ]; then
		[ -z "${IPSW:-}" ] || { echo "mka: no $IPSW" >&2; exit 1; }
		echo "Downloading iOS $(conf ios_version "$COMMON_CONF") ($(conf ios_build "$COMMON_CONF")) for the iPhone X from" \
			"Apple's servers; running mka is your consent to that download."
		mkdir -p "$(dirname "$IPSW_FILE")"
		curl -fL -C - -o "$IPSW_FILE.part" "$url"
		mv "$IPSW_FILE.part" "$IPSW_FILE"
	fi
	# the hash is checked once per file (its size and mtime remembered), not at every build
	stamp=$OBJ/ipsw.ok
	[ "$(cat "$stamp" 2>/dev/null)" = "$(stat -f '%z %m' "$IPSW_FILE") $IPSW_FILE" ] && return
	say "checking $IPSW_FILE"
	[ "$(stat -f %z "$IPSW_FILE")" = "$size" ] || { echo "mka: $IPSW_FILE is not $size bytes" >&2; exit 1; }
	[ "$(shasum -a 256 "$IPSW_FILE" | cut -d' ' -f1)" = "$sum" ] || { echo "mka: $IPSW_FILE: SHA-256 is not $sum" >&2; exit 1; }
	echo "$(stat -f '%z %m' "$IPSW_FILE") $IPSW_FILE" > "$stamp"
}

# The headers made from your own files, never kept in a repository: the iPhone X's device tree and the loader's
# identity (the IPSW), the loader's font, the touch firmware (your phone, through the device's pull-firmware.sh).
step_headers() {
	local H=$OBJ/headers ili
	mkdir -p "$H"
	python3 "$BM/tools/extract-dt.py" "$IPSW_FILE" "$H/d22ap.adt" >/dev/null
	python3 "$BM/tools/mkidentity.py" "$H/d22ap.adt" > "$H/identity.h"
	python3 "$BM/tools/mkfont8x16.py" "$BM/fonts/terminus/ter-u16b.bdf" > "$H/font8x16.h"
	if [ -f "$DEV/tools/mkilifw.py" ]; then
		ili=$(ls "$DEV"/fw-touch/FW_NF_ILI7807S_*.bin 2>/dev/null | head -1)
		[ -n "$ili" ] || { echo "mka: no touch firmware: MAPPLE_SERIAL=<adb serial> $DEV/pull-firmware.sh (phone in Android, rooted)" >&2; exit 1; }
		python3 "$DEV/tools/mkilifw.py" "$ili" "$H/ilifw.h" >/dev/null
	fi
}

# smoke-kernel.sh's build: darwin-xnu-build at its pinned commit, KDK 13.5; the tree is
# kernel/mediatek/common with the SoC's iokit/ and osfmk/ laid over it. Built again only when the sources change.
step_kernel() {
	local K=$OBJ/kernel D kc stamp k
	mkdir -p "$K"
	if [ -n "${KERNEL:-}" ]; then
		need "$KERNEL" "KERNEL="
		cp "$KERNEL" "$OUT/kernel"
		rm -f "$K/stamp"
		return
	fi
	kc=DEVELOPMENT; [ "$TARGET_BUILD_VARIANT" = user ] && kc=RELEASE
	stamp=$( { echo "$kc"; for t in common "$SOC"; do (cd "$TOP/kernel/mediatek/$t" &&
		git ls-files -z -c -o --exclude-standard | xargs -0 shasum); done; } | shasum | cut -d' ' -f1)
	[ -f "$OUT/kernel" ] && [ "$(cat "$K/stamp" 2>/dev/null)" = "$stamp" ] && { say "kernel: sources unchanged"; return; }
	(
	export DEVELOPER_DIR=$X15
	export KDKROOT=${KDKROOT:-/Library/Developer/KDKs/KDK_13.5_22G74.kdk}
	[ -d "$KDKROOT" ] || { echo "mka: no $KDKROOT (KDK 13.5, build 22G74, from developer.apple.com)" >&2; exit 1; }
	D=$K/dxb
	if [ ! -d "$D" ]; then
		git init -q "$D"
		git -C "$D" fetch -q --depth 1 https://github.com/blacktop/darwin-xnu-build 663906528d1cd5ea8fde7de6f16de73db9511549
		git -C "$D" checkout -q FETCH_HEAD
		# the arm64 (non-PAC) kernel needs libfirehose for arm64 too
		sed -i '' 's/ARCHS="x86_64 arm64e" VALID_ARCHS="x86_64 arm64e"/ARCHS="x86_64 arm64e arm64" VALID_ARCHS="x86_64 arm64e arm64"/' "$D/build.sh"
		grep -q 'VALID_ARCHS="x86_64 arm64e arm64"' "$D/build.sh"
	fi
	# gum is only asked when no macOS version is given, xcodes only to install an Xcode: neither happens here
	mkdir -p "$K/bin"
	for t in gum xcodes; do printf '#!/bin/sh\nexit 0\n' > "$K/bin/$t"; chmod +x "$K/bin/$t"; done
	export PATH="$K/bin:$PATH"
	rm -rf "$D/xnu"; mkdir -p "$D/xnu"
	(cd "$TOP/kernel/mediatek/common" && git ls-files -z -c -o --exclude-standard | rsync -a -0 --files-from=- ./ "$D/xnu/")
	(cd "$TOP/kernel/mediatek/$SOC" && git ls-files -z -c -o --exclude-standard iokit osfmk | rsync -a -0 --files-from=- ./ "$D/xnu/")
	# the SDK's target macros beside the staged libpthread and HFS (kernel builds use -nostdinc)
	for d in bsd/pthread/libpthread_staged bsd/hfs_staged; do
		cp "$(xcrun --sdk macosx --show-sdk-path)/usr/include/TargetConditionals.h" "$D/xnu/$d/"
	done
	kl=$(echo "$kc" | tr A-Z a-z)
	rm -f "$D"/build/xnu.obj/kernel.$kl.*     # build.sh skips the kernel when one is there
	say "kernel ($kc), log in $K/build.log"
	(cd "$D" && MACOS_VERSION=13.5 KERNEL_CONFIG=$kc MACHINE_CONFIG=$(echo "$SOC" | tr a-z A-Z) ARCH_CONFIG=ARM64 \
		bash build.sh > "$K/build.log" 2>&1) || { grep -E 'error:' "$K/build.log" | head; exit 1; }
	k=$(ls "$D"/build/xnu.obj/kernel.$kl.* 2>/dev/null | grep -v -E '\.(dSYM|unstripped)$' | head -1)
	[ -n "$k" ] || { echo "mka: no kernel built: see $K/build.log" >&2; exit 1; }
	cp "$k" "$OUT/kernel"
	)
	echo "$stamp" > "$K/stamp"
}

# The shim, built from a copy (nothing is written into mapple/loader), its objects checked for absolute addresses.
step_shim() {
	local S=$OBJ/shim
	rm -rf "$S"; mkdir -p "$S"
	(cd "$TOP/mapple/loader/shim" && git ls-files -z -c -o --exclude-standard | rsync -a -0 --files-from=- ./ "$S/")
	(cd "$S" && CHECK_ABS=$BM/tools/check-abs.py ./build-"$TARGET_DEVICE".sh >/dev/null)
	cp "$S/out/$TARGET_DEVICE/Image" "$OUT/shim.Image"
}

step_loader() {
	local L=$OBJ/loader touch=none
	rm -rf "$L"; mkdir -p "$L/mtk/loader" "$L/xnuloader"
	(cd "$TOP/mapple/loader/mtk/loader" && git ls-files -z -c -o --exclude-standard | rsync -a -0 --files-from=- ./ "$L/mtk/loader/")
	cp "$OBJ/headers/identity.h" "$L/xnuloader/"
	cp "$OBJ/headers/font8x16.h" "$L/mtk/loader/"
	[ ! -f "$OBJ/headers/ilifw.h" ] || cp "$OBJ/headers/ilifw.h" "$L/mtk/loader/"
	[ ! -f "$DEV/fw-touch/hdl_firmware.img" ] || touch=$DEV/fw-touch/hdl_firmware.img
	printf 'CONFIG_SOC="%s"\n' "$SOC" > "$L/.config"
	# shellcheck disable=SC2046
	(cd "$L/mtk/loader" && TOUCH_IMG=$touch LLD=${LLD:-ld.lld} ./build.sh $(conf loader "$DEV/device.conf") >/dev/null)
	cp "$L/mtk/loader/mtkloader.bin" "$OUT/"
}

step_adt() {
	# shellcheck disable=SC2046
	D22AP_ADT=$OBJ/headers/d22ap.adt python3 "$BM/tools/mkadt.py" "$OUT/$TARGET_DEVICE.adt" $(conf adt "$DEV/device.conf")
}

step_payload() {
	local cmdline size max kver c
	cmdline="$(conf cmdline "$COMMON_CONF") $(conf cmdline "$DEV/device.conf")"
	python3 "$BM/tools/mkpayload.py" "$OUT/kernel" "$OUT/$TARGET_DEVICE.adt" "$OUT/xnu.bin" --cmdline "$cmdline" >/dev/null
	size=$(stat -f %z "$OUT/xnu.bin") max=$(conf payload_max "$DEV/device.conf")
	[ "$size" -lt $((max)) ] || { echo "mka: xnu.bin: $size bytes, over $max" >&2; exit 1; }
	c=$TOP/kernel/mediatek/common
	kver=mapple-$SOC-g$(git -C "$c" rev-parse --short=8 HEAD)
	[ -z "$(git -C "$c" status --porcelain --untracked-files=no)" ] || kver=$kver-dirty
	python3 "$BM/tools/mkxnup.py" -o "$OUT/xnup.bin" --kernel-version "$kver" \
		shim="$OUT/shim.Image" xnu="$OUT/xnu.bin" >/dev/null
}

step_bootimage() {
	if [ -z "${ANDROID_BOOT:-}" ]; then
		say "bootimage skipped: ANDROID_BOOT=<a backup of your phone's boot partition> makes $OUT/boot.img"
		return
	fi
	need "$ANDROID_BOOT" "ANDROID_BOOT="
	python3 "$TOP/mapple/splitboot/tools/chainboot.py" "$ANDROID_BOOT" "$OUT/mtkloader.bin" "$OUT/boot.img" "$OUT/xnup.bin" >/dev/null
	[ "$(stat -f %z "$OUT/boot.img")" -le $(($(conf boot_size "$DEV/device.conf"))) ] || { echo "mka: boot.img too big" >&2; exit 1; }
}

for t in $targets; do
	case $t in
	mapple) set -- ipsw headers kernel shim loader adt payload bootimage ;;
	ipsw|headers|kernel|shim|loader|adt|payload|bootimage) set -- "$t" ;;
	*) echo "mka: no target $t" >&2; exit 1 ;;
	esac
	for s in "$@"; do say "$s"; "step_$s"; done
done
(cd "$OUT" && ls kernel shim.Image mtkloader.bin "$TARGET_DEVICE.adt" xnu.bin xnup.bin boot.img 2>/dev/null |
	xargs shasum -a 256 > SHA256SUMS; cat SHA256SUMS)
