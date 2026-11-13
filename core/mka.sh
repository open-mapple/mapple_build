#!/bin/bash
# mka [target ...] [NAME=value ...], after lunch. Targets (default mapple: all of them, in this order):
#   ipsw headers kernel shim loader adt payload installer bootimage userland audio gpu rootimage rootsparse zip
# NAME=value:
#   IPSW=path          your own copy of the pinned IPSW (else it is downloaded from Apple into out/ipsw/)
#   ANDROID_BOOT=path  a backup of your phone's boot partition: bootimage chains the loader in front of it
#   KERNEL=path        a kernel already built (skips the kernel build)
#   TOUCH_FW=path      the phone's FW_NF_ILI7807S_*.bin into the payload (item ilifw); the zip's products otherwise
#                      read it from the tail area, where the zip puts it; the development layout takes fw-touch/'s
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
INSTALL=${TARGET_INSTALL:-dev}
# What a device's products differ in (loader flags, boot-args: the loader, payload, boot image, zip) goes in $P:
# $OUT for the development layout, $OUT/single or $OUT/split for the others; the rest is shared.
P=$OUT
[ "$INSTALL" = dev ] || P=$OUT/$INSTALL
mkdir -p "$OBJ" "$P"

conf() {	# conf KEY FILE: the value of KEY (tab-indented in device.conf, at the start of a line in common.conf)
	sed -n "s/^	\{0,1\}$1	//p" "$2" | head -1
}
# merge LINE PAIRS: LINE's key=value words with PAIRS' values, PAIRS' new keys added
merge() {
	local out=" $1 " kv
	for kv in $2; do
		case $out in
		*" ${kv%%=*}="*) out=$(echo "$out" | sed "s| ${kv%%=*}=[^ ]*| $kv|") ;;
		*) out="$out$kv " ;;
		esac
	done
	echo $out
}
need() { [ -f "$1" ] || { echo "mka: no $1 ($2)" >&2; exit 1; }; }
say() { echo "== $*"; }

# fetch NAME: the source tarball common.conf pins as NAME_url, NAME_size and NAME_sha256, in out/dl; prints its path
fetch() {
	local url f
	url=$(conf "$1_url" "$COMMON_CONF")
	f=$TOP/out/dl/$1-$(basename "$url")
	if [ ! -f "$f" ]; then
		mkdir -p "$(dirname "$f")"
		curl -fsSL -o "$f.part" "$url" && mv "$f.part" "$f"
	fi
	[ "$(stat -f %z "$f")" = "$(conf "$1_size" "$COMMON_CONF")" ] &&
		[ "$(shasum -a 256 "$f" | cut -d' ' -f1)" = "$(conf "$1_sha256" "$COMMON_CONF")" ] ||
		{ echo "mka: $f does not match its pin" >&2; exit 1; }
	echo "$f"
}

SOC=$(conf soc "$DEV/device.conf")
# Xcode 15.4 builds the kernel (its iig) and the phone's programs (its iOS 17.5 SDK), as the runs so far were built;
# the loader, the shim and Mesa's host tools use the usual Xcode. xcode-select is left alone.
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

# The iPhone X's device tree, from your IPSW (never kept in a repository), and the loader's font.
step_headers() {
	local H=$OBJ/headers
	mkdir -p "$H"
	python3 "$BM/tools/extract-dt.py" "$IPSW_FILE" "$H/d22ap.adt" >/dev/null
	python3 "$BM/tools/mkfont8x16.py" "$BM/fonts/terminus/ter-u16b.bdf" > "$H/font8x16.h"
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
	rm -rf "$L"; mkdir -p "$L/mtk/loader"
	(cd "$TOP/mapple/loader/mtk/loader" && git ls-files -z -c -o --exclude-standard | rsync -a -0 --files-from=- ./ "$L/mtk/loader/")
	cp "$OBJ/headers/font8x16.h" "$L/mtk/loader/"
	[ ! -f "$DEV/fw-touch/hdl_firmware.img" ] || touch=$DEV/fw-touch/hdl_firmware.img
	printf 'CONFIG_SOC="%s"\n' "$SOC" > "$L/.config"
	# shellcheck disable=SC2046
	(cd "$L/mtk/loader" && TOUCH_IMG=$touch LLD=${LLD:-ld.lld} ./build.sh $(conf loader "$DEV/device.conf") $(conf "${INSTALL}_loader" "$DEV/device.conf") >/dev/null)
	cp "$L/mtk/loader/mtkloader.bin" "$P/"
}

step_adt() {
	# shellcheck disable=SC2046
	D22AP_ADT=$OBJ/headers/d22ap.adt python3 "$BM/tools/mkadt.py" "$OUT/$TARGET_DEVICE.adt" $(conf adt "$DEV/device.conf")
}

step_payload() {
	local cmdline size max kver c fw=${TOUCH_FW:-} ili=
	cmdline=$(merge "$(conf cmdline "$COMMON_CONF") $(conf cmdline "$DEV/device.conf")" "$(conf "${INSTALL}_cmdline" "$DEV/device.conf")")
	python3 "$BM/tools/mkpayload.py" "$OUT/kernel" "$OUT/$TARGET_DEVICE.adt" "$P/xnu.bin" --cmdline "$cmdline" >/dev/null
	echo "$cmdline" > "$P/cmdline"
	size=$(stat -f %z "$P/xnu.bin") max=$(conf payload_max "$DEV/device.conf")
	[ "$size" -lt $((max)) ] || { echo "mka: xnu.bin: $size bytes, over $max" >&2; exit 1; }
	c=$TOP/kernel/mediatek/common
	kver=mapple-$SOC-g$(git -C "$c" rev-parse --short=8 HEAD)
	[ -z "$(git -C "$c" status --porcelain --untracked-files=no)" ] || kver=$kver-dirty
	# the touch firmware (your phone's): TOUCH_FW=, or fw-touch/ for the development layout, which has no tail area;
	# packed before xnu, as the loader unpacks the shim's ramdisk over what follows it
	if [ -z "$fw" ] && [ "$INSTALL" = dev ]; then
		case " $(conf loader "$DEV/device.conf") " in *" -DXNU_TOUCH_ILI=1 "*)
			fw=$(ls "$DEV"/fw-touch/FW_NF_ILI7807S_*.bin 2>/dev/null | head -1)
			[ -n "$fw" ] || { echo "mka: no touch firmware: MAPPLE_SERIAL=<adb serial> $DEV/pull-firmware.sh (phone in Android, rooted)" >&2; exit 1; } ;;
		esac
	fi
	rm -f "$P/ilifw.bin"
	if [ -n "$fw" ]; then
		python3 "$TOP/mapple/loader/mtk/tools/mkilifw.py" "$fw" "$P/ilifw.bin" >/dev/null
		ili=ilifw=$P/ilifw.bin
	fi
	python3 "$BM/tools/mkxnup.py" -o "$P/xnup.bin" --kernel-version "$kver" \
		shim="$OUT/shim.Image" ${ili:+"$ili"} xnu="$P/xnu.bin" >/dev/null
}

# mapple-install (installer/), static for the phone's recovery and for this Mac (bootimage, dry runs), with zig from
# the pinned zlib.
step_installer() {
	local I=$OBJ/installer z
	rm -rf "$I"; mkdir -p "$I/src"
	tar -xzf "$(fetch zlib)" -C "$I/src"
	z=$(echo "$I"/src/zlib-*)
	set -- "$z/adler32.c" "$z/crc32.c" "$z/deflate.c" "$z/inflate.c" "$z/inffast.c" "$z/inftrees.c" "$z/trees.c" \
		"$z/zutil.c" "$BM/installer/mapple-install.c"
	# splitboot's verbs (split-*), when its repository is in the tree
	[ ! -d "$TOP/mapple/splitboot/installer" ] || set -- "$@" -DMAPPLE_SPLIT -I"$BM/installer" "$TOP"/mapple/splitboot/installer/*.c
	command -v zig >/dev/null || { echo "mka: needs zig (brew install zig)" >&2; exit 1; }
	zig cc -target aarch64-linux-musl -static -O2 -I"$z" "$@" -o "$OUT/mapple-install"
	zig cc -O2 -I"$z" "$@" -o "$I/mapple-install"
}

step_bootimage() {
	if [ -z "${ANDROID_BOOT:-}" ]; then
		say "bootimage skipped: ANDROID_BOOT=<a backup of your phone's boot partition> makes $P/boot.img"
		return
	fi
	need "$ANDROID_BOOT" "ANDROID_BOOT="
	"$OBJ/installer/mapple-install" chain "$ANDROID_BOOT" "$P/mtkloader.bin" "$P/xnup.bin" --out "$P/boot.img" >/dev/null
}

# What the root gets on top of the IPSW's files and root-tweaks.py, staged in $OUT/root (installed by rootimage):
# mappled and its tools in a userdebug build only (mapple/userland's README), the audio server's plug-in when
# device.conf has audio 1, the Metal driver.
step_userland() {
	local R=$OUT/root
	rm -rf "$R/usr/libexec/mappled" "$R/private/var/root/mapple-dev"
	[ "$TARGET_BUILD_VARIANT" = userdebug ] || return 0
	DEVELOPER_DIR=$X15 bash "$TOP/mapple/userland/tools/mappled/build.sh" "$OBJ/userland/mappled" >/dev/null
	mkdir -p "$R/usr/libexec" "$R/private/var/root/mapple-dev"
	cp "$OBJ/userland/mappled" "$R/usr/libexec/"
	for f in vln bootrec mstat mapple-usbd; do cp "$OBJ/userland/$f" "$R/private/var/root/mapple-dev/"; done
}

step_audio() {
	local H=$OUT/root/System/Library/Audio/Plug-Ins/HAL
	rm -rf "$H/MappleAudio.driver"
	[ "$(conf audio "$DEV/device.conf")" = 1 ] || return 0
	DEVELOPER_DIR=$X15 bash "$TOP/mapple/userland/audio/build.sh" "$OBJ/audio" >/dev/null
	mkdir -p "$H"
	cp -R "$OBJ/audio/MappleAudio.driver" "$H/"
}

# Mesa from its release tarball (pinned), mapple_gpu_mali's patches on it, then its own build scripts in a copy
# of the repository (they build in place); the source is unpacked again when the patches or the Xcode change.
step_gpu() {
	local G=$OBJ/gpu tar p stamp
	tar=$(fetch mesa)
	mkdir -p "$G"
	(cd "$TOP/mapple/gpu/mali" && git ls-files -z -c -o --exclude-standard | rsync -a -0 --files-from=- ./ "$G/")
	stamp=$( { echo "$(conf mesa_sha256 "$COMMON_CONF") $X15"; cat "$G"/mesa/patches/*.patch; } | shasum | cut -d' ' -f1)
	if [ "$(cat "$G/mesa/src.stamp" 2>/dev/null)" != "$stamp" ]; then
		rm -rf "$G/mesa/src" "$G/mesa/build-macos" "$G/mesa/build-ios"
		mkdir -p "$G/mesa/src"
		tar -xJf "$tar" -C "$G/mesa/src" --strip-components 1
		for p in "$G"/mesa/patches/*.patch; do git -C "$G/mesa/src" apply "$p"; done
		echo "$stamp" > "$G/mesa/src.stamp"
	fi
	say "gpu: Mesa for the Mac (its compilers), then for iOS, then the driver; log in $G/build.log"
	(cd "$G" && bash mesa/build.sh fn build_libdrm_xnu configure_macos build_macos package_macos &&
		DEVELOPER_DIR=$X15 bash ios/build.sh mesa && DEVELOPER_DIR=$X15 bash ios/build.sh driver) > "$G/build.log" 2>&1 || { tail -20 "$G/build.log"; exit 1; }
	rm -rf "$OUT/root/System/Library/Extensions/MappleMTLDriver.bundle"
	mkdir -p "$OUT/root/System/Library/Extensions"
	cp -R "$G/ios/out/MappleMTLDriver.bundle" "$OUT/root/System/Library/Extensions/"
}

# The root image needs sudo (owners kept, the image mounted): printed when system.img is missing or older than
# anything staged, and the build stops there.
step_rootimage() {
	local img=$OUT/system.img
	if [ ! -f "$img" ] || [ -n "$(find "$OUT/root" -newer "$img" -print -quit)" ]; then
		echo "mka: the root image needs sudo. Run, then mka again:"
		echo "  rm -f $img; sudo $BM/tools/mkiosroot-macos.sh $IPSW_FILE $img $(conf root_size "$DEV/device.conf") $TARGET_DEVICE $OUT/root"
		exit 2
	fi
}

# The root for fastboot: system.img's allocated blocks as a sparse image, and their ranges for the zip's check.
step_rootsparse() {
	need "$OUT/system.img" "mka rootimage"
	[ "$OUT/root.img" -nt "$OUT/system.img" ] && [ -f "$OUT/root.ranges" ] && return
	python3 "$BM/tools/rootranges.py" "$OUT/system.img" "$OUT/root.img" "$OUT/root.ranges"
}

# The recovery zip (installer/update-binary), for the single and split products: personal, as the payload carries
# your IPSW's device tree. A split product also gets its uninstall zip.
step_zip() {
	local Z=$OBJ/zip f
	[ "$INSTALL" != dev ] || { echo "mka: the development layout has no zip: lunch mapple_${TARGET_DEVICE}_single (or _split)" >&2; exit 1; }
	for f in "$OUT/mapple-install" "$P/mtkloader.bin" "$P/xnup.bin" "$OUT/root.ranges"; do need "$f" "mka mapple"; done
	rm -rf "$Z"; mkdir -p "$Z/META-INF/com/google/android"
	cp "$BM/installer/update-binary" "$Z/META-INF/com/google/android/"
	echo "#dummy" > "$Z/META-INF/com/google/android/updater-script"
	cp "$OUT/mapple-install" "$P/mtkloader.bin" "$P/xnup.bin" "$OUT/root.ranges" "$Z/"
	set -- mapple-install mtkloader.bin xnup.bin root.ranges
	if [ "$INSTALL" = split ]; then
		grep -q split-carve "$OUT/mapple-install" ||
			{ echo "mka: mapple-install has no split- verbs: mapple/splitboot/installer/ in the tree, then mka installer" >&2; exit 1; }
		conf split_size "$DEV/device.conf" > "$Z/split_size"
		set -- "$@" split_size
	fi
	(cd "$Z" && shasum -a 256 "$@" > SHA256SUMS)
	f=$P/mapple-$(conf ios_build "$COMMON_CONF")-$(date +%Y%m%d)-${TARGET_PRODUCT#mapple_}
	rm -f "$P"/mapple-*-"${TARGET_PRODUCT#mapple_}"-*.zip
	(cd "$Z" && zip -q -X -r "$f-PERSONAL.zip" META-INF "$@" SHA256SUMS)
	say "$f-PERSONAL.zip"
	if [ "$INSTALL" = split ]; then
		echo split-uninstall > "$Z/uninstall"
		(cd "$Z" && shasum -a 256 mapple-install uninstall > SHA256SUMS &&
			zip -q -X -r "$f-uninstall.zip" META-INF mapple-install uninstall SHA256SUMS)
		say "$f-uninstall.zip"
	fi
}

for t in $targets; do
	case $t in
	mapple) set -- ipsw headers kernel shim loader adt payload installer bootimage userland audio gpu rootimage rootsparse zip ;;
	ipsw|headers|kernel|shim|loader|adt|payload|installer|bootimage|userland|audio|gpu|rootimage|rootsparse|zip) set -- "$t" ;;
	*) echo "mka: no target $t" >&2; exit 1 ;;
	esac
	for s in "$@"; do say "$s"; "step_$s"; done
done
r=${P#"$OUT"}; r=${r#/}${r:+/}
(cd "$OUT" && for f in kernel shim.Image "$TARGET_DEVICE.adt" root.ranges "${r}mtkloader.bin" "${r}xnu.bin" \
	"${r}xnup.bin" "${r}boot.img"; do [ ! -f "$f" ] || shasum -a 256 "$f"; done > "${r}SHA256SUMS"; cat "${r}SHA256SUMS")
